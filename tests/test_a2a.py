"""A2A 形态的纯标准库回归：安全闸门、context 校验、确认中断语义。

不依赖 a2a-sdk —— 这些是「不装 SDK 也必须成立」的约束，CI 里照常执行。
真正的协议端到端在 test_a2a_e2e.py（无 SDK 时自动跳过）。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402
from agent import (a2a_cli, a2a_client, a2a_executor, a2a_server,  # noqa: E402
                   a2a_sdk, session as sess_mod, tools)


class SecurityGateTests(unittest.TestCase):
    """A2A 是网络入口：鉴权配置错了必须在启动时就拒绝，而不是运行时提醒。"""

    def test_non_loopback_without_token_is_refused(self):
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with self.assertRaises(a2a_server.StartupRefused):
                a2a_server.require_token(host, "")

    def test_non_loopback_with_token_is_allowed(self):
        a2a_server.require_token("0.0.0.0", "token-abc")  # 不抛即通过

    def test_loopback_allows_empty_token(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            a2a_server.require_token(host, "")

    def test_token_prefers_env_over_arg_order(self):
        with mock.patch.dict(os.environ, {a2a_server.TOKEN_ENV: ""}, clear=False):
            self.assertEqual(a2a_server.resolve_token(None), "")
            self.assertEqual(a2a_server.resolve_token("explicit-token"), "explicit-token")


class ContextValidationTests(unittest.TestCase):
    """context_id 会拼进会话文件名，必须在上游就挡住异常输入。"""

    def test_accepts_uuid_and_simple_ids(self):
        for value in ("75b3a7ff-8a3d-43dd-95e7-e1620cc551a7", "abc", "a_b-c", "a" * 101):
            self.assertIsNotNone(a2a_executor._CONTEXT_RE.match(value), value)

    def test_rejects_traversal_and_empty(self):
        for value in ("", "../etc", "a/b", "a b", ".hidden", "-lead", "a" * 102,
                      "a\nb"):
            self.assertIsNone(a2a_executor._CONTEXT_RE.match(value), repr(value))


class ConfirmationInterruptTests(unittest.TestCase):
    """确认中断是写操作安全的唯一闸门，语义错了会静默放行。"""

    def test_need_user_input_bypasses_except_exception(self):
        """tools._dispatch 用 `except Exception` 兜底，中断必须穿透它。"""
        self.assertFalse(issubclass(a2a_executor.NeedUserInput, Exception))
        self.assertTrue(issubclass(a2a_executor.NeedUserInput, BaseException))

        def dispatch_like():
            try:
                raise a2a_executor.NeedUserInput("批准此变更？")
            except Exception:  # noqa: BLE001 —— 与 tools._dispatch 同构
                return "swallowed"
            except BaseException:
                raise

        with self.assertRaises(a2a_executor.NeedUserInput):
            dispatch_like()

    def test_settle_pending_marks_unqueued_call_as_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "STATE_DIR", Path(tmp)):
                sess = sess_mod.Session(session_id="a2a-test")
                sess.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "ops_approve", "arguments": "{}"}}]})
                self.assertEqual(a2a_executor._settle_pending(
                    sess, a2a_executor.SETTLE_NOTE), 1)
                tool_rows = [m for m in sess.messages if m.get("role") == "tool"]
                self.assertEqual(len(tool_rows), 1)
                self.assertEqual(tool_rows[0]["tool_call_id"], "call_1")
                self.assertIn("未执行", tool_rows[0]["content"])
                # 二次调用不应重复补（否则续跑时消息会翻倍）
                self.assertEqual(a2a_executor._settle_pending(
                    sess, a2a_executor.SETTLE_NOTE), 0)

    def test_resume_prompt_demands_reapproval(self):
        """续跑必须让模型重新建戳——直接 ops_exec 会被守门拒绝。"""
        prompt = a2a_executor._resume_prompt("确认执行")
        self.assertIn("ops_approve", prompt)
        self.assertIn("ops_exec", prompt)
        self.assertIn("确认执行", prompt)


def _stub_sdk():
    """SDK 替身：build_executor 在类定义期只用到 AgentExecutor 基类与 pb/ph 符号。

    不 import a2a-sdk 就能构造真执行器，测试 _run 的会话/ask_user 机制。
    """
    return SimpleNamespace(pb=SimpleNamespace(), ph=SimpleNamespace(),
                           AgentExecutor=object)


class _ExecutorLoopMixin:
    """3.9 上 asyncio.Semaphore() 构造会取当前事件循环；同进程里别的测试
    asyncio.run 过之后主线程 loop 被置空，get_event_loop 直接 RuntimeError。
    构造执行器前补一个一次性 loop，用完还原。3.10+ 上构造不再取 loop，无副作用。
    """

    def setUp(self):
        super().setUp()
        self._fresh_loop = None
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            self._fresh_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._fresh_loop)

    def tearDown(self):
        if self._fresh_loop is not None:
            asyncio.set_event_loop(None)
            self._fresh_loop.close()
        super().tearDown()


class SingleUseApprovalTests(_ExecutorLoopMixin, unittest.TestCase):
    """单次使用不变量：一次存储答复只批准**一个**变更。

    续跑轮里模型重新提交 ops_approve 时，ask_user 第一次返回存储答复；
    同一轮的第二个写操作必须重新中断（NeedUserInput → INPUT_REQUIRED），
    不能被同一个答复顺带批准。
    """

    def _run_turn(self, read_only, resuming, answer, simulate):
        """真执行器 + 假 run_turn：simulate 扮演模型对 ctx.ask_user 的调用序列。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "STATE_DIR", Path(tmp)), \
                 mock.patch.object(a2a_executor, "sdk", _stub_sdk), \
                 mock.patch.object(a2a_executor.loop, "run_turn", simulate):
                ex = a2a_executor.build_executor(read_only=read_only)
                sess = ex._session("inv1")
                return ex._run(sess, "[A2A 人工确认] ...", resuming, answer)

    def test_second_write_in_same_resumed_turn_reinterrupts(self):
        """同轮第二个 ops_approve 必须重新中断。

        若有人把 ask_user 改成「每次都返回存储答复」，第二个变更会拿到
        旧答复而非 NeedUserInput，simulate 走错分支，本测试失败。
        """
        def simulate(prompt, sess, ctx, model=None, on_event=None):
            first = ctx.ask_user("批准变更 A？")   # 重新提交的第一个 ops_approve
            assert first == "确认执行", f"首次确认应返回存储答复，得到 {first!r}"
            try:
                second = ctx.ask_user("批准变更 B？")   # 同轮第二个变更
            except a2a_executor.NeedUserInput:
                return "re-interrupted"
            return f"auto-approved-second-with:{second!r}"

        out = self._run_turn(read_only=False, resuming=True, answer="确认执行",
                             simulate=simulate)
        self.assertEqual(out, "re-interrupted")

    def test_new_turn_gets_no_stale_answer(self):
        """新任务轮（非续跑）第一次确认就必须中断：答复不能跨轮残留。"""
        def simulate(prompt, sess, ctx, model=None, on_event=None):
            with self.assertRaises(a2a_executor.NeedUserInput):
                ctx.ask_user("批准此变更？")
            return "interrupted"

        out = self._run_turn(read_only=False, resuming=False, answer="",
                             simulate=simulate)
        self.assertEqual(out, "interrupted")


class ReadOnlyModeTests(_ExecutorLoopMixin, unittest.TestCase):
    """--read-only 起服：不建人工确认通道，写操作降级为明确拒绝（不是崩溃）。"""

    def test_cli_threads_read_only_flag_to_serve(self):
        with mock.patch.object(a2a_server, "serve", return_value=0) as serve:
            self.assertEqual(a2a_cli.cli(["serve", "--read-only"]), 0)
            self.assertIs(serve.call_args.kwargs["read_only"], True)
            serve.reset_mock()
            self.assertEqual(a2a_cli.cli(["serve"]), 0)   # 默认行为不变
            self.assertIs(serve.call_args.kwargs["read_only"], False)

    def test_serve_threads_read_only_to_executor(self):
        captured = {}

        def fake_build_executor(**kwargs):
            captured.update(kwargs)
            raise a2a_sdk.A2AUnavailable("stop before uvicorn")

        with mock.patch.object(a2a_executor, "build_executor", fake_build_executor), \
                redirect_stderr(StringIO()):
            self.assertEqual(a2a_server.serve(read_only=True), 2)
        self.assertIs(captured["read_only"], True)

    def test_read_only_context_refuses_write_without_crash(self):
        captured = {}

        def simulate(prompt, sess, ctx, model=None, on_event=None):
            captured["ctx"] = ctx
            return "done"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "STATE_DIR", Path(tmp)), \
                 mock.patch.object(a2a_executor, "sdk", _stub_sdk), \
                 mock.patch.object(a2a_executor.loop, "run_turn", simulate):
                ex = a2a_executor.build_executor(read_only=True)
                sess = ex._session("ro1")
                # 续跑轮同样安全：模型重提 ops_approve 也只会拿到拒绝文案
                self.assertEqual(ex._run(sess, "重启 nginx", True, "确认执行"), "done")
        self.assertIsNone(captured["ctx"].ask_user)
        # 写尝试经真实 dispatch 落成「拒绝」字符串返回给模型，不抛异常
        result = tools.dispatch("ops_approve", {"command": "systemctl restart nginx"},
                                captured["ctx"])
        self.assertIn("拒绝", result)
        self.assertIn("不创建审批戳", result)


class ClientHelperTests(unittest.TestCase):
    """客户端纯函数：不依赖 SDK，但必须跨版本稳定。"""

    def test_base_url_ipv4(self):
        self.assertEqual(a2a_server.base_url("127.0.0.1", 8080), "http://127.0.0.1:8080")

    def test_base_url_ipv6_needs_brackets(self):
        # 没括号时 AgentCard 里的 URL 客户端根本解析不了
        self.assertEqual(a2a_server.base_url("::1", 8080), "http://[::1]:8080")

    def test_format_result_hint_is_conditional(self):
        """客户端不知道对端是不是 ops-agent，不能无条件让它去 /resume。"""
        out = a2a_client.format_result({"state": "TASK_STATE_COMPLETED",
                                        "task_id": "t1", "context_id": "c1", "text": "ok"})
        self.assertIn("若对端是 ops-agent", out)
        self.assertIn("a2a-c1", out)
        self.assertIn("ok", out)

    def test_send_inside_running_loop(self):
        """在事件循环里调用 send 不能因为 asyncio.run 嵌套而崩。"""
        import asyncio

        async def fake_send(url, text, task_id, context_id, token, timeout, proxy):
            return {"state": "TASK_STATE_COMPLETED", "text": text,
                    "task_id": "t", "context_id": "c", "needs_input": False}

        with mock.patch.object(a2a_client, "_send_async", fake_send):
            async def caller():
                return a2a_client.send("http://example.invalid", "hello")
            result = asyncio.run(caller())
        self.assertEqual(result["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(result["text"], "hello")

    def test_send_outside_loop(self):
        async def fake_send(url, text, task_id, context_id, token, timeout, proxy):
            return {"state": "TASK_STATE_COMPLETED", "text": text,
                    "task_id": "t", "context_id": "c", "needs_input": False}

        with mock.patch.object(a2a_client, "_send_async", fake_send):
            result = a2a_client.send("http://example.invalid", "hi")
        self.assertEqual(result["text"], "hi")


class InputBoundaryTests(unittest.TestCase):
    def test_input_limit_is_bounded(self):
        """入口必须有上限：整本日志贴进来会一次烧光预算与轮次。"""
        self.assertGreater(a2a_executor.MAX_INPUT_CHARS, 0)
        self.assertLessEqual(a2a_executor.MAX_INPUT_CHARS, 100_000)

    def test_semaphore_wait_is_bounded(self):
        self.assertGreater(a2a_executor.SEMAPHORE_WAIT, 0)
        self.assertLessEqual(a2a_executor.SEMAPHORE_WAIT, 120)


class SdkAvailabilityTests(unittest.TestCase):
    def test_available_returns_reason_not_raise(self):
        ok, reason = a2a_sdk.available()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(reason, str)
        self.assertTrue(reason)

    def test_install_hint_mentions_requirements_file(self):
        self.assertIn("requirements-a2a.txt", a2a_sdk.INSTALL_HINT)
        self.assertIn("3.10", a2a_sdk.INSTALL_HINT)

    @unittest.skipUnless(a2a_sdk.available()[0], "未安装 a2a-sdk")
    def test_sdk_exposes_expected_symbols(self):
        s = a2a_sdk.sdk()
        for name in ("pb", "ph", "AgentExecutor", "DefaultRequestHandler",
                     "create_jsonrpc_routes", "create_agent_card_routes",
                     "PROTOCOL_VERSION_1_0"):
            self.assertTrue(hasattr(s, name), name)
        self.assertEqual(s.PROTOCOL_VERSION_1_0, "1.0")

    @unittest.skipUnless(a2a_sdk.available()[0], "未安装 a2a-sdk")
    def test_agent_card_declares_jsonrpc_and_no_streaming(self):
        card = a2a_server.build_agent_card("http://127.0.0.1:8080")
        self.assertEqual(card.name, "ops-agent")
        self.assertEqual(len(card.supported_interfaces), 1)
        iface = card.supported_interfaces[0]
        self.assertEqual(iface.protocol_binding, "JSONRPC")
        self.assertEqual(iface.protocol_version, "1.0")
        self.assertEqual(iface.url, "http://127.0.0.1:8080/")
        # 首版不声明 streaming：没验证过的能力不写在 card 上骗客户端
        self.assertFalse(card.capabilities.streaming)

    @unittest.skipUnless(a2a_sdk.available()[0], "未安装 a2a-sdk")
    def test_session_table_is_bounded(self):
        """常驻会话必须有上限：Session 缓存全部 messages，不设限就是内存泄漏。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "STATE_DIR", Path(tmp)):
                ex = a2a_executor.build_executor()
                for i in range(a2a_executor.MAX_SESSIONS + 20):
                    ex._session(f"ctx{i}")
                self.assertLessEqual(len(ex._sessions), a2a_executor.MAX_SESSIONS)
                # 锁表不能比会话表无限膨胀
                self.assertLessEqual(len(ex._locks), a2a_executor.MAX_SESSIONS + 20)

    @unittest.skipUnless(a2a_sdk.available()[0], "未安装 a2a-sdk")
    def test_locked_session_survives_eviction(self):
        """正在执行（持锁）的会话不能被淘汰。

        否则同一个 sid 会同时存在两把锁，串行写入的保证就失效了——
        LRU 淘汰"最久未访问"是对的，但必须跳过在跑的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "STATE_DIR", Path(tmp)):
                ex = a2a_executor.build_executor()
                hot = ex._session("hot")
                lock = ex._locks[hot.id]
                lock.acquire()  # 模拟该会话正在执行
                try:
                    for i in range(a2a_executor.MAX_SESSIONS + 10):
                        ex._session(f"cold{i}")
                    self.assertIn(hot.id, ex._sessions,
                                  "持锁会话被淘汰会导致同 sid 出现两把锁")
                finally:
                    lock.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
