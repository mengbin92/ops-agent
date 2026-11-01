"""Regression coverage for the 2026-11-01 agent audit fixes.

Covers: bash 子进程凭据剔除（1a）、dispatch 精确值抹除（1b）、快照参数归一化
approve/exec 对齐（2）、tail 上限（3）、会话修剪 24h 保护（4）、list_sessions
纯文本流式读取（5）、以及 llm.chat 流式输出后不重试的护栏。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import core
import usage
from agent import llm, session, tools


class Response:
    """SSE 假响应：逐块产出 data: 行，块为异常实例时在流中抛出。"""

    def __init__(self, chunks=None):
        self.chunks = chunks or []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield b"data: " + json.dumps(chunk).encode() + b"\n"


class AgentFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ops-agent-fixes-")
        self.addCleanup(self.tmp.cleanup)
        state = mock.patch.object(core, "STATE_DIR", Path(self.tmp.name))
        state.start()
        self.addCleanup(state.stop)
        self.secrets = {
            "OPSAGENT_API_KEY": "opsagent-test-key-9f8e7d6c5b4a",
            "OPSAGENT_A2A_TOKEN": "A2A-tok-plain-value-112233445566",
        }
        # 两个值都不匹配 core.redact 的正则（原样通过），测试因此只依赖
        # 环境剔除（1a）与逐值抹除（1b），不会被正则脱敏偶然救场
        for value in self.secrets.values():
            self.assertIn(value, core.redact(value))
        env = mock.patch.dict(os.environ, self.secrets)
        env.start()
        self.addCleanup(env.stop)

    # ---- fix 1a：R0 子进程环境不含 agent 凭据 ----

    def test_bash_env_output_hides_agent_secrets(self):
        result = tools.dispatch("bash", {"command": "env"}, tools.ToolContext())
        for name, value in self.secrets.items():
            self.assertNotIn(value, result, f"env 输出泄漏 {name}")
        self.assertIn("exit=0", result)
        # 只是剔除已知键，不是清空环境：无关变量仍然可见
        with mock.patch.dict(os.environ, {"OPSAGENT_TEST_MARKER": "still-here"}):
            result = tools.dispatch("bash", {"command": "env"}, tools.ToolContext())
        self.assertIn("still-here", result)

    def test_bash_child_env_drops_secrets_and_diff_var(self):
        captured = {}

        def fake_shell(cmd, timeout, env=None):
            captured[cmd] = env
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.dict(os.environ, {"KUBECTL_EXTERNAL_DIFF": "kubectl-diff-wrapper"}), \
                mock.patch.object(tools.core, "run_shell", side_effect=fake_shell):
            tools.dispatch("bash", {"command": "env"}, tools.ToolContext())
            tools.dispatch("bash", {"command": "kubectl diff -f /tmp/x.yaml"},
                           tools.ToolContext())
        for cmd, env in captured.items():
            # 三个键对所有命令一律剔除：diff 外部执行器只可能被 kubectl diff
            # 消费，无条件剔除更稳（识别漏一段就是任意命令执行）
            self.assertNotIn("OPSAGENT_API_KEY", env, cmd)
            self.assertNotIn("OPSAGENT_A2A_TOKEN", env, cmd)
            self.assertNotIn("KUBECTL_EXTERNAL_DIFF", env, cmd)
        self.assertIn("PATH", captured["env"])  # 其余环境原样传递

    # ---- fix 1b：dispatch 出口按已知配置值精确抹除 ----

    def test_dispatch_scrubs_exact_secret_values(self):
        with tempfile.TemporaryDirectory() as other:
            note = Path(other) / "note.txt"
            note.write_text("prefix " + self.secrets["OPSAGENT_API_KEY"] + " suffix",
                            encoding="utf-8")
            result = tools.dispatch("read", {"path": str(note)}, tools.ToolContext())
        self.assertNotIn(self.secrets["OPSAGENT_API_KEY"], result)
        self.assertIn(core.REDACTED, result)
        self.assertIn("prefix", result)

    # ---- fix 2：approve 与 exec 共用同一份快照参数归一化 ----

    def test_normalize_snapshot_args_filters_and_unions(self):
        args = {"snapshot_files": ["/b.conf", " /a.conf ", "", "  ", "/a.conf"],
                "snapshot_file": "/c.conf",
                "snapshot_cmds": [{"command": " cmd ok ", "name": "n"},
                                  {"command": "   "}]}
        snap_files, snap_cmds = tools._normalize_snapshot_args(args)
        self.assertEqual(snap_files, ["/a.conf", "/b.conf", "/c.conf"])
        self.assertEqual(snap_cmds, [(" cmd ok ", "n")])
        # 兼容只带旧版单文件参数的调用
        snap_files, snap_cmds = tools._normalize_snapshot_args({"snapshot_file": "/c.conf"})
        self.assertEqual(snap_files, ["/c.conf"])
        self.assertEqual(snap_cmds, [])

    def test_ops_exec_accepts_multi_file_snapshot_declaration(self):
        import execution
        with mock.patch.object(tools.core, "exec_change",
                               return_value=(0, "snap-1", "ok")) as exec_change, \
                mock.patch.object(execution, "find_latest", return_value=None):
            result = tools.dispatch("ops_exec", {
                "command": "systemctl restart nginx",
                "snapshot_files": ["/etc/nginx/nginx.conf", "/etc/app.conf",
                                   "/etc/app.conf", "  "]}, tools.ToolContext())
        self.assertIn("执行成功", result)
        self.assertEqual(exec_change.call_args.args[1],
                         ["/etc/app.conf", "/etc/nginx/nginx.conf"])
        with mock.patch.object(tools.core, "exec_change",
                               return_value=(0, "snap-2", "ok")) as exec_change, \
                mock.patch.object(execution, "find_latest", return_value=None):
            tools.dispatch("ops_exec", {"command": "systemctl restart nginx",
                                        "snapshot_file": "/etc/legacy.conf"},
                           tools.ToolContext())
        self.assertEqual(exec_change.call_args.args[1], ["/etc/legacy.conf"])

    # ---- fix 3：查询类工具 tail 上限 200 ----

    def test_query_tail_is_capped_at_200(self):
        self.assertIn("1-200", tools.dispatch(
            "ops_executions", {"tail": 500}, tools.ToolContext()))
        self.assertIn("0-200", tools.dispatch(
            "schedule", {"action": "runs", "tail": 500}, tools.ToolContext()))
        self.assertIn("1-200", tools.dispatch(
            "ops_executions", {"tail": 0}, tools.ToolContext()))

    # ---- fix 4：修剪跳过近 24h 活跃会话 ----

    def test_prune_keeps_sessions_active_within_24h(self):
        d = core.STATE_DIR / "sessions"
        d.mkdir(parents=True)
        now = time.time()
        recent = [d / f"recent-{i}.jsonl" for i in range(session.SESSION_KEEP + 2)]
        for i, p in enumerate(recent):
            p.write_text("x", encoding="utf-8")
            os.utime(p, (now - 600 - i, now - 600 - i))
        old = [d / f"old-{i}.jsonl" for i in range(2)]
        for i, p in enumerate(old):
            p.write_text("x", encoding="utf-8")
            stamp = now - 48 * 3600 - i
            os.utime(p, (stamp, stamp))
        session._prune_sessions()
        self.assertTrue(all(p.exists() for p in recent))  # 超额但 24h 内：全保留
        self.assertTrue(all(not p.exists() for p in old))  # 两天前的超额文件照删

    # ---- fix 5：list_sessions 纯文本流式读取，不改会话文件 ----

    def test_list_sessions_streams_without_touching_files(self):
        d = core.STATE_DIR / "sessions"
        d.mkdir(parents=True)
        path = d / "streamed.jsonl"
        records = [{"format": 1, "id": "streamed", "created": 1},
                   {"role": "user", "content": "第一个问题"},
                   {"role": "assistant", "content": "回答"},
                   {"role": "user", "content": "第二个问题"}]
        # 末行故意不写换行：旧实现经 Session._load 会补写换行（列表产生写操作）
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
        path.write_text(body, encoding="utf-8")
        before = path.read_bytes()
        listing = session.list_sessions()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], "streamed")
        self.assertEqual(listing[0]["turns"], 2)
        self.assertEqual(listing[0]["preview"], "第一个问题")

    def test_list_sessions_still_hides_unloadable_sessions(self):
        d = core.STATE_DIR / "sessions"
        d.mkdir(parents=True)
        path = d / "broken.jsonl"
        path.write_text(json.dumps({"format": 1, "id": "broken"}) + "\n"
                        + json.dumps({"role": "assistant", "content": "",
                                      "tool_calls": [["bad"]]}) + "\n", encoding="utf-8")
        self.assertEqual(session.list_sessions(), [])

    # ---- fix 11：llm.chat 已流式输出后绝不重试 ----

    def test_chat_never_retries_after_streamed_output(self):
        usage.save_budget({"enabled": True, "daily_calls": 100, "daily_tokens": None})
        pieces = []
        # 第一次尝试：转发了一个增量后连接被瞬断（可重试类错误）；
        # 若护栏被移除，这里会发起第二次请求并成功 → 断言失败。
        chunks = [{"choices": [{"delta": {"content": "partial "}}]},
                  llm.LLMError("LLM API HTTP 429: transient")]
        with mock.patch.object(llm, "_post", return_value=Response(chunks=chunks)) as post, \
                mock.patch.object(llm.time, "sleep") as sleep:
            with self.assertRaises(llm.LLMError):
                llm.chat([{"role": "user", "content": "hi"}], on_delta=pieces.append)
        self.assertEqual(pieces, ["partial "])  # 增量确实已转发（护栏条件成立）
        self.assertEqual(post.call_count, 1)    # 只尝试了一次
        sleep.assert_not_called()               # 没有进入退避重试


if __name__ == "__main__":
    unittest.main()
