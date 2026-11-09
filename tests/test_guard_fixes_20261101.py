"""Guard-fix regressions (2026-11-01 audit): sensitive snapshot gate, banner-tolerant
remote read, ssh options validation, cross-target anti-replay, MCP torn audit lines.

No real SSH, no service writes: all remote interaction goes through patched _ssh.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import execution
import mcp_server
import remote


class SensitiveSnapshotGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-gate-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.root / "state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.key = self.root / "id_rsa"
        self.key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nraw\n")

    def test_snapshot_file_rejects_credential_named_file(self):
        # cat 被拒的凭据文件，快照通道也必须拒绝：否则 raw 副本落进
        # snapshots/ 并被 state_backup 打包，回滚预览还会把内容带进确认框。
        with self.assertRaises(core.OpsxError) as ctx:
            core.snapshot_file(str(self.key))
        self.assertIn("敏感", str(ctx.exception))
        sdir = core.STATE_DIR / "snapshots"
        self.assertFalse(sdir.exists() and any(sdir.iterdir()),
                         "拒绝的快照不得留下半成品目录")

    def test_snapshot_file_rejects_shadow(self):
        shadow = self.root / "shadow"
        shadow.write_text("root:$6$aa$hash:19000:0:99999:7:::\n")
        with self.assertRaises(core.OpsxError):
            core.snapshot_file(str(shadow))

    def test_snapshot_file_allows_normal_config(self):
        conf = self.root / "nginx.conf"
        conf.write_text("server {}\n")
        sid = core.snapshot_file(str(conf))
        self.assertTrue(sid)

    def test_exec_change_aborts_when_snapshot_file_is_sensitive(self):
        cmd = f"printf changed > {self.root / 'nginx.conf'}"
        core.approve(cmd, impact="改配置", snapshot_files=[str(self.key)])
        code, sid, output = core.exec_change(cmd, snapshot_files=[str(self.key)])
        self.assertEqual(code, 3)
        self.assertIn("敏感", output)
        self.assertIsNone(sid)

    def test_remote_pull_file_rejects_sensitive_path_before_ssh(self):
        def no_ssh(*args, **kwargs):
            raise AssertionError("敏感路径必须在发起 SSH 之前被拒绝")

        d = self.root / "snapdir"
        d.mkdir()
        with patch.object(remote, "_ssh", no_ssh):
            with self.assertRaises(remote.RemoteError) as ctx:
                remote._pull_file("web1", "/root/.ssh/id_rsa", d, [])
        self.assertIn("敏感", str(ctx.exception))

    def test_remote_pull_file_rejects_shadow(self):
        d = self.root / "snapdir"
        d.mkdir()
        with patch.object(remote, "_ssh", lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not ssh"))):
            with self.assertRaises(remote.RemoteError):
                remote._pull_file("web1", "/etc/shadow", d, [])


class RemoteReadBannerTolerantTests(unittest.TestCase):
    """_remote_read 的标记从尾部解析：rc/MOTD 噪声不再让远程回滚变砖。

    标记每次调用随机生成（避免文件正文伪装成结束标记截断内容），所以桩必须
    从**本次下发脚本**里取出标记再拼输出，不能写死字符串。
    """

    @staticmethod
    def marker_of(cmd):
        match = re.search(r"OPSX-EXISTS-[0-9a-f]{16}", cmd)
        assert match, f"脚本里没有随机标记: {cmd}"
        return match.group(0).encode()

    @classmethod
    def stdout_for(cls, cmd, payload, banner=b"", missing=False):
        marker = cls.marker_of(cmd)
        if missing:
            return banner + marker + b"-MISSING\n"
        return banner + marker + b"\n" + payload + b"\n" + marker + b"-END\n"

    def assert_read(self, payload, expected, banner=b""):
        def fake_ssh(name, cmd, stdin=None, timeout=120):
            return subprocess.CompletedProcess([], 0, self.stdout_for(cmd, payload, banner), b"")

        with patch.object(remote, "_ssh", fake_ssh):
            exists, content = remote._remote_read("web1", "/etc/app.conf")
        self.assertTrue(exists)
        self.assertEqual(content, expected)

    def test_banner_before_exists_marker(self):
        self.assert_read(b"line1\nline2", b"line1\nline2",
                         banner=b"Welcome to Ubuntu 22.04\nlast login: ...\n")

    def test_payload_without_trailing_newline_roundtrips(self):
        self.assert_read(b"no-newline", b"no-newline")

    def test_payload_with_trailing_newline_roundtrips(self):
        self.assert_read(b"ends-with-newline\n", b"ends-with-newline\n")

    def test_empty_file(self):
        self.assert_read(b"", b"")

    def test_payload_that_looks_like_an_old_marker_is_not_truncated(self):
        payload = b"before\nOPSX-EXISTS\nafter\nOPSX-EXISTS-END\n"
        self.assert_read(payload, payload)

    def test_binary_payload_roundtrips(self):
        payload = bytes(range(256)) + b"\r\n\x00\xff"
        self.assert_read(payload, payload)

    def test_missing_with_banner(self):
        def fake_ssh(name, cmd, stdin=None, timeout=120):
            return subprocess.CompletedProcess(
                [], 0, self.stdout_for(cmd, b"", b"banner noise\n", missing=True), b"")

        with patch.object(remote, "_ssh", fake_ssh):
            exists, content = remote._remote_read("web1", "/etc/app.conf")
        self.assertFalse(exists)
        self.assertEqual(content, b"")

    def test_undecidable_output_still_fails_closed(self):
        with patch.object(remote, "_ssh",
                          lambda *a, **k: subprocess.CompletedProcess([], 0, b"garbage", b"")):
            with self.assertRaises(remote.RemoteError):
                remote._remote_read("web1", "/etc/app.conf")


class SshOptionsValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-sshopt-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.root / "state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        core._ensure_state_dir()

    def register(self, options):
        (core.STATE_DIR / "targets.json").write_text(json.dumps(
            {"web1": {"host": "10.0.0.5", "options": options}}))

    def test_option_without_dash_is_rejected(self):
        # 不带 - 的 option 会被 ssh 当成目标主机：命令报的错指向一个不存在的
        # "主机名"，而审批提示里显示的是注册 host——错得莫名其妙。发 SSH 前拒绝。
        self.register(["StrictHostKeyChecking=accept-new"])
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.ssh_argv("web1", "true")
        self.assertIn("options", str(ctx.exception))

    def test_wellformed_options_still_work(self):
        self.register(["-o", "StrictHostKeyChecking=accept-new"])
        argv = remote.ssh_argv("web1", "true")
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.register(["-oStrictHostKeyChecking=accept-new"])
        argv = remote.ssh_argv("web1", "true")
        self.assertIn("-oStrictHostKeyChecking=accept-new", argv)


class CrossTargetAntiReplayTests(unittest.TestCase):
    """同一 host:port 换个目标名，也不能绕过"未核实 unknown 不自动重放"。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-replay-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.root / "state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        core._ensure_state_dir()
        (core.STATE_DIR / "targets.json").write_text(json.dumps({
            "web1": {"host": "10.0.0.5", "user": "ops"},
            "web2": {"host": "10.0.0.5", "user": "ops"},  # 同机不同名
            "other": {"host": "10.0.0.99", "user": "ops"},
        }))

    def test_unknown_on_web1_blocks_same_command_via_web2(self):
        cmd = "systemctl restart nginx"
        env = remote.target_env("web1")
        rec = execution.begin("exec", cmd, "web1", env, phase="exec")
        execution.finish(rec["id"], "unknown", error="ssh 255, 无法确认")
        code, msg = core._check_pending_unknown(cmd, "web2")
        self.assertEqual(code, core.EXEC_ABORT_UNKNOWN)
        self.assertIn("未核实", msg)

    def test_unknown_on_web1_does_not_block_unrelated_host(self):
        cmd = "systemctl restart nginx"
        env = remote.target_env("web1")
        rec = execution.begin("exec", cmd, "web1", env, phase="exec")
        execution.finish(rec["id"], "unknown", error="ssh 255, 无法确认")
        code, _ = core._check_pending_unknown(cmd, "other")
        self.assertEqual(code, 0)


class McpAuditTornLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-mcpaudit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.root / "state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        core._ensure_state_dir()

    def test_torn_final_line_does_not_break_ops_audit(self):
        # 崩溃瞬间的半条审计记录不能让 MCP 的审计查询变砖（CLI 早已容忍）。
        log = core.STATE_DIR / "audit.jsonl"
        log.write_text('{"event": "check", "cmd": "ls"}\n{"kind": "tor', encoding="utf-8")
        payload = json.loads(mcp_server.call_tool("ops_audit", {"tail": 5})["content"][0]["text"])
        events = payload["events"]
        self.assertEqual(events[0]["event"], "check")
        self.assertEqual(events[-1]["event"], "audit-corrupt-line")

    def test_clean_log_unchanged(self):
        core.audit("check", cmd="ls", level="R0", result="allow")
        payload = json.loads(mcp_server.call_tool("ops_audit", {"tail": 3})["content"][0]["text"])
        self.assertTrue(all(e.get("event") != "audit-corrupt-line" for e in payload["events"]))


if __name__ == "__main__":
    unittest.main()
