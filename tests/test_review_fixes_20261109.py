"""2026-11-09 审查修复回归（R01–R09）。

每一条都对应 `.codex/code-review-2026-11-09.md` 里的反例，断言的是"应当安全/
正确"的行为。所有网络、SSH、SMTP、模型端点都用桩或本地 /bin/sh 代替：
不连真实目标、不发真实邮件、不用真实凭据。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402
import doctor  # noqa: E402
import remote  # noqa: E402
import scenario  # noqa: E402
from agent import a2a_executor, llm, logscan, notify, schedule, tools  # noqa: E402


def _stub_sdk():
    return SimpleNamespace(pb=SimpleNamespace(), ph=SimpleNamespace(),
                           AgentExecutor=object)


class _LoopMixin:
    """3.9 上构造 asyncio.Semaphore 需要当前事件循环（同 test_a2a 的处理）。"""

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


class _StateMixin:
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-review-20261109-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.root / "state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)


def _executor(read_only: bool = False):
    with patch.object(a2a_executor, "sdk", _stub_sdk):
        return a2a_executor.build_executor(read_only=read_only)


# --------------------------------------------------------------- R01

class ConfirmationBindingTests(_LoopMixin, _StateMixin, unittest.TestCase):
    """答复必须绑定上一轮展示的那张变更单（工具名 + 参数 + 执行环境）。"""

    BASE = {"command": "systemctl restart alpha", "impact": "review fixture"}

    def _run_turn(self, ex, sess, args, resuming=False, answer="yes", task_id=None):
        def simulate(prompt, sess, ctx, **kwargs):
            return tools.dispatch("ops_approve", dict(args), ctx)

        with patch.object(a2a_executor.loop, "run_turn", side_effect=simulate):
            return ex._run(sess, "批准变更单", resuming, answer, task_id)

    def test_swapped_command_reinterrupts_and_creates_no_stamp(self):
        ex = _executor()
        sess = ex._session("bind-command")
        with self.assertRaises(a2a_executor.NeedUserInput) as first:
            self._run_turn(ex, sess, self.BASE)
        self.assertIn("systemctl restart alpha", first.exception.question)

        swapped = dict(self.BASE, command="systemctl restart beta")
        with self.assertRaises(a2a_executor.NeedUserInput) as second:
            self._run_turn(ex, sess, swapped, resuming=True)
        # 重新中断时必须把新命令完整展示出来，人批准的是 beta 这张新单
        self.assertIn("systemctl restart beta", second.exception.question)
        self.assertNotEqual(core.check("systemctl restart beta")[0], 0)
        self.assertNotEqual(core.check("systemctl restart alpha")[0], 0)

    def test_reconfirmed_request_then_succeeds(self):
        """重新展示过之后，同一次答复才创建审批戳（流程不会被卡死）。"""
        ex = _executor()
        sess = ex._session("bind-reconfirm")
        with self.assertRaises(a2a_executor.NeedUserInput):
            self._run_turn(ex, sess, self.BASE)
        out = self._run_turn(ex, sess, self.BASE, resuming=True)
        self.assertIn("审批戳已创建", out)
        self.assertEqual(core.check("systemctl restart alpha")[0], 0)

    def test_same_count_but_different_snapshot_path_is_another_ticket(self):
        """快照只显示数量时，同数量不同路径会得到相同文案——必须按结构化参数比对。"""
        ex = _executor()
        sess = ex._session("bind-snapshot")
        base = dict(self.BASE, snapshot_files=["/etc/app.conf"])
        with self.assertRaises(a2a_executor.NeedUserInput) as first:
            self._run_turn(ex, sess, base)
        # 展示完整路径，人才核对得了批准的是哪一份
        self.assertIn("/etc/app.conf", first.exception.question)

        swapped = dict(self.BASE, snapshot_files=["/etc/secret.conf"])
        with self.assertRaises(a2a_executor.NeedUserInput) as second:
            self._run_turn(ex, sess, swapped, resuming=True)
        self.assertIn("/etc/secret.conf", second.exception.question)
        self.assertNotEqual(core.check("systemctl restart alpha")[0], 0)

    def test_uses_and_impact_changes_are_another_ticket(self):
        for change in ({"uses": 3}, {"impact": "另一套影响面"}, {"force": True}):
            with self.subTest(change=change):
                ex = _executor()
                sess = ex._session("bind-" + next(iter(change)))
                with self.assertRaises(a2a_executor.NeedUserInput):
                    self._run_turn(ex, sess, self.BASE)
                with self.assertRaises(a2a_executor.NeedUserInput):
                    self._run_turn(ex, sess, dict(self.BASE, **change), resuming=True)
                self.assertNotEqual(core.check("systemctl restart alpha")[0], 0)

    def test_pending_is_dropped_when_turn_completes(self):
        ex = _executor()
        sess = ex._session("bind-cleanup")
        with self.assertRaises(a2a_executor.NeedUserInput):
            self._run_turn(ex, sess, self.BASE)
        self.assertIn(sess.id, ex._pending)
        self._run_turn(ex, sess, self.BASE, resuming=True)
        self.assertNotIn(sess.id, ex._pending)


# --------------------------------------------------------------- R02

class ReadOnlyToolSetTests(_LoopMixin, _StateMixin, unittest.TestCase):
    """--read-only 必须同时限制 schema 与 dispatch，而不是只靠"没有确认通道"。"""

    def test_preapproved_write_is_rejected_and_stamp_not_consumed(self):
        marker = self.root / "marker.txt"
        marker.write_text("before")
        command = "printf after > " + shlex.quote(str(marker))
        core.approve(command)   # 已存在合法审批戳：只读形态仍不能消费它
        ex = _executor(read_only=True)

        def simulate(prompt, sess, ctx, **kwargs):
            return tools.dispatch("ops_exec", {"command": command}, ctx)

        with patch.object(a2a_executor.loop, "run_turn", side_effect=simulate):
            out = ex._run(ex._session("ro-write"), "只读排查", False, "")
        self.assertEqual(marker.read_text(), "before")
        self.assertIn("只读", out)
        self.assertEqual(core.check(command)[0], 0, "审批戳不应被只读执行消耗")

    def test_write_and_state_changing_tools_are_not_exposed(self):
        captured = {}

        def simulate(prompt, sess, ctx, **kwargs):
            captured["ctx"] = ctx
            return "ok"

        ex = _executor(read_only=True)
        with patch.object(a2a_executor.loop, "run_turn", side_effect=simulate):
            ex._run(ex._session("ro-schema"), "只读排查", False, "")
        ctx = captured["ctx"]
        self.assertEqual(ctx.allowed, tools.SCHEDULED_TOOLS)
        names = {s["function"]["name"] for s in tools.tool_schemas(ctx)}
        for denied in ("ops_approve", "ops_exec", "ops_revoke", "ops_rollback",
                       "schedule", "remember", "scenario_run"):
            self.assertNotIn(denied, names)
            self.assertIn("只读", tools.dispatch(denied, {}, ctx))
        self.assertIn("logscan", names)


# --------------------------------------------------------------- R03

def _redirect_fixture(code, location):
    """构造一个"上游返回 3xx"的 HTTPS handler 类 + 记录是否被跟进的列表。

    必须返回真正的类：build_opener 会对传入的 handler 类做 isinstance 检查。
    """
    forwarded = []

    class Fixture(urllib.request.HTTPSHandler):
        def https_open(self, req):
            headers = Message()
            if "upstream.invalid" in req.full_url:
                headers["Location"] = location
                response = urllib.response.addinfourl(
                    io.BytesIO(b"{}"), headers, req.full_url, code)
                response.msg = "Found"
                return response
            forwarded.append(bool(req.get_header("Authorization")
                                   or req.get_header("X-api-key")))
            response = urllib.response.addinfourl(
                io.BytesIO(b"{}"), headers, req.full_url, 200)
            response.msg = "OK"
            return response

    return Fixture, forwarded


class RedirectCredentialTests(unittest.TestCase):
    def _exercise(self, code, location, headers):
        fixture, forwarded = _redirect_fixture(code, location)
        with patch.object(urllib.request, "HTTPSHandler", fixture):
            with self.assertRaises(llm.LLMError):
                with llm._request("https://upstream.invalid/chat/completions",
                                  {}, headers, 5):
                    pass
        return forwarded

    def test_cross_origin_302_does_not_forward_credentials(self):
        forwarded = self._exercise(302, "https://redirect-target.invalid/collect",
                                   {"Authorization": "Bearer synthetic-sentinel"})
        self.assertEqual(forwarded, [])

    def test_cross_origin_303_does_not_forward_api_key(self):
        forwarded = self._exercise(303, "https://redirect-target.invalid/collect",
                                   {"x-api-key": "synthetic-sentinel"})
        self.assertEqual(forwarded, [])

    def test_https_downgrade_to_http_is_refused(self):
        forwarded = self._exercise(302, "http://plain-target.invalid/collect",
                                   {"Authorization": "Bearer synthetic-sentinel"})
        self.assertEqual(forwarded, [])

    def test_doctor_probe_does_not_follow_redirect(self):
        fixture, forwarded = _redirect_fixture(
            302, "https://redirect-target.invalid/collect")
        with patch.dict(os.environ, {"OPSAGENT_API_KEY": "synthetic-key",
                                     "OPSAGENT_PROVIDER": "openai",
                                     "OPSAGENT_BASE_URL": "https://upstream.invalid"}), \
                patch.object(urllib.request, "HTTPSHandler", fixture):
            items = doctor._probe_llm_api()
        self.assertEqual(items[0]["status"], doctor.WARN)
        self.assertIn("302", items[0]["detail"])
        self.assertEqual(forwarded, [])


# --------------------------------------------------------------- R04

class ResolvedSensitiveSnapshotTests(_StateMixin, unittest.TestCase):
    """普通路径解析成凭据文件后，必须在读取内容之前再次拒绝。"""

    def test_resolved_credential_path_is_rejected_before_read(self):
        commands = []

        def fake_ssh(name, cmd, stdin=None, timeout=120):
            commands.append(cmd)
            if "OPSX-RESOLVED" in cmd:
                return subprocess.CompletedProcess(
                    [], 0, b"OPSX-RESOLVED\x00/root/.ssh/id_rsa\x00", b"")
            raise AssertionError(f"解析到敏感真实路径后仍发起了读取: {cmd}")

        d = self.root / "snap"
        d.mkdir()
        with patch.object(remote, "_ssh", fake_ssh):
            with self.assertRaises(remote.RemoteError) as ctx:
                remote._pull_file("web1", "/etc/app.conf", d, [])
        self.assertIn("敏感", str(ctx.exception))
        self.assertFalse(any("OPSX-EXISTS-" in c for c in commands),
                         "不得发起内容读取")
        self.assertFalse(any(p.is_file() for p in d.rglob("*")), "不得留下快照内容")

    def test_sensitive_input_path_still_rejected_without_ssh(self):
        d = self.root / "snap2"
        d.mkdir()
        with patch.object(remote, "_ssh",
                          side_effect=AssertionError("must not ssh")):
            with self.assertRaises(remote.RemoteError):
                remote._pull_file("web1", "/root/.ssh/id_rsa", d, [])


# --------------------------------------------------------------- R05 / R06

class RemoteByteTransportTests(_StateMixin, unittest.TestCase):
    """远程读取：失败必须中止；传输必须逐字节保真（真实 /bin/sh 执行生成脚本）。"""

    def _local_ssh(self, banner=b"", path_prefix=None):
        def fake(name, cmd, stdin=None, timeout=120):
            env = None
            if path_prefix is not None:
                env = dict(os.environ, PATH=f"{path_prefix}:/usr/bin:/bin")
            kwargs = {"capture_output": True, "env": env}
            if stdin is not None:
                kwargs["input"] = stdin
            result = subprocess.run(["/bin/sh", "-c", cmd], **kwargs)
            if banner and "OPSX-EXISTS-" in cmd:
                result.stdout = banner + result.stdout
            return result
        return fake

    def test_failed_cat_aborts_read(self):
        source = self.root / "app.conf"
        source.write_bytes(b"original configuration")
        bindir = self.root / "bin"
        bindir.mkdir()
        cat = bindir / "cat"
        cat.write_text('#!/bin/sh\nprintf "fixture: Permission denied\\n" >&2\nexit 1\n')
        cat.chmod(0o700)
        with patch.object(remote, "_ssh",
                          self._local_ssh(path_prefix=str(bindir))):
            with self.assertRaises(remote.RemoteError) as ctx:
                remote._remote_read("web1", str(source))
        # cat 的退出码不能被后面的 printf 洗成 0，错误信息也要带回来
        self.assertIn("读取远程文件失败", str(ctx.exception))
        self.assertIn("Permission denied", str(ctx.exception))

    def test_read_missing_file_reports_missing(self):
        with patch.object(remote, "_ssh", self._local_ssh()):
            exists, data = remote._remote_read("web1", str(self.root / "nope"))
        self.assertFalse(exists)
        self.assertEqual(data, b"")

    def test_pull_file_ignores_login_banner_and_preserves_bytes(self):
        payload = b"before\nOPSX-EXISTS\nafter\nOPSX-EXISTS-END\n\x00\xff\r\n"
        source = self.root / "app.conf"
        source.write_bytes(payload)
        d = self.root / "snap"
        d.mkdir()
        items = []
        with patch.object(remote, "_target_resolved_path", return_value=str(source)), \
                patch.object(remote, "_target_stat", return_value=(0o600, "ops:ops")), \
                patch.object(remote, "_ssh",
                             self._local_ssh(banner=b"Welcome to test host\n")):
            remote._pull_file("web1", "/etc/app.conf", d, items)
        self.assertEqual((d / items[0]["dest"]).read_bytes(), payload)

    def test_pull_file_missing_source_is_an_error(self):
        d = self.root / "snap"
        d.mkdir()
        with patch.object(remote, "_target_resolved_path",
                          return_value=str(self.root / "nope")), \
                patch.object(remote, "_ssh", self._local_ssh()):
            with self.assertRaises(remote.RemoteError):
                remote._pull_file("web1", "/etc/app.conf", d, [])
        self.assertFalse(any(p.is_file() for p in d.rglob("*")), "不得留下快照内容")

    def test_rollback_backup_read_failure_aborts_before_restore(self):
        """读不到当前内容就不能声称"已保存回滚前副本"，恢复命令也不得下发。"""
        (core.STATE_DIR).mkdir(parents=True, exist_ok=True)
        (core.STATE_DIR / "targets.json").write_text(
            json.dumps({"web1": {"host": "10.0.0.11"}}), encoding="utf-8")
        cmd = "systemctl restart rollback-read-failure"
        core.approve(cmd, ttl_seconds=60, target="web1")
        state = {"read_fails": False, "restore": []}

        def fake(name, command, stdin=None, timeout=120):
            if "OPSX-RESOLVED" in command:
                return subprocess.CompletedProcess(
                    [], 0, b"OPSX-RESOLVED\x00/etc/app.conf\x00", b"")
            if "OPSX-HOST " in command:
                return subprocess.CompletedProcess(
                    [], 0, b"OPSX-HOST 10.0.0.11\nOPSX-MACHINE-ID abcdef0123456789\n", b"")
            if "OPSX-EXISTS-" in command:
                if state["read_fails"]:
                    return subprocess.CompletedProcess(
                        [], 1, b"", b"cat: Permission denied")
                return subprocess.CompletedProcess(
                    [], 0, _remote_read_stdout(command, b"previous-v2\n"), b"")
            if command.startswith("stat "):
                return subprocess.CompletedProcess([], 0, b"600 ops:ops\n", b"")
            if ".opsx-restore." in command:
                state["restore"].append(stdin)
                return subprocess.CompletedProcess(
                    [], 0, f"opsx-restored had=1 size={len(stdin or b'')} mode=600".encode(), b"")
            return subprocess.CompletedProcess([], 0, b"", b"")

        def _remote_read_stdout(command, payload):
            marker = re.search(r"OPSX-EXISTS-[0-9a-f]{16}", command).group(0).encode()
            return marker + b"\n" + payload + b"\n" + marker + b"-END\n"

        with patch.object(remote, "_ssh", fake):
            code, sid, _ = remote.exec_change(
                "web1", cmd, snapshot_files=["/etc/app.conf"])
            self.assertEqual(code, 0)
            state["read_fails"] = True
            with self.assertRaises(remote.RemoteError):
                remote.rollback(sid, confirm=True)
        self.assertEqual(state["restore"], [], "读取失败后不得下发恢复命令")

    def test_roundtrip_cases(self):
        cases = [b"", b"no-newline", b"ends-with-newline\n",
                 b"a\nOPSX-EXISTS-0123456789abcdef\nb\n", bytes(range(256))]
        for payload in cases:
            with self.subTest(payload=payload[:20]):
                source = self.root / "case.conf"
                source.write_bytes(payload)
                with patch.object(remote, "_ssh",
                                  self._local_ssh(banner=b"motd noise\n")):
                    exists, data = remote._remote_read("web1", str(source))
                self.assertTrue(exists)
                self.assertEqual(data, payload)


# --------------------------------------------------------------- R07

class SmtpPartialRejectionTests(unittest.TestCase):
    CFG = {"channel": "email", "smtp_host": "smtp.example.test", "smtp_port": 465,
           "mail_from": "ops@example.test", "smtp_password": "secret",
           "mail_to": ["ok@example.test", "rejected@example.test"],
           "timeout_seconds": 3}

    @staticmethod
    def _server(refused):
        server = MagicMock()
        server.__enter__.return_value = server
        server.send_message.return_value = refused
        return server

    @staticmethod
    def _recipients(server):
        return list(server.send_message.call_args.kwargs["to_addrs"])

    def test_partial_rejection_is_failed_and_remembers_accepted(self):
        notif = {"id": "n1", "title": "t", "text": "x", "attempts": 0}
        server = self._server({"rejected@example.test": (550, b"no such user")})
        with patch.object(notify.smtplib, "SMTP_SSL", return_value=server):
            result = notify._deliver(self.CFG, notif)
        self.assertEqual(result["status"], "failed")
        self.assertIn("拒收", result["last_error"])
        accepted = notif.get("smtp_accepted_recipient_ids") or []
        self.assertEqual(len(accepted), 1, "只应记住已成功的那位收件人")

    def test_retry_only_delivers_rejected_recipients(self):
        notif = {"id": "n2", "title": "t", "text": "x", "attempts": 0}
        server = self._server({"rejected@example.test": (550, b"temporary")})
        with patch.object(notify.smtplib, "SMTP_SSL", return_value=server):
            notify._deliver(self.CFG, notif)
        self.assertEqual(self._recipients(server), self.CFG["mail_to"])

        # 第二次：被拒的那位这次接受，另一位不应重复投递
        second = self._server({})
        with patch.object(notify.smtplib, "SMTP_SSL", return_value=second):
            result = notify._deliver(self.CFG, notif)
        self.assertEqual(self._recipients(second), ["rejected@example.test"])
        self.assertEqual(result["status"], "sent")
        # 记账保留完整受众（恢复消息要按它定向发送），两位都在
        self.assertEqual(len(notif["smtp_accepted_recipient_ids"]), 2)

    def test_full_success_sends_once_and_records_recipients(self):
        notif = {"id": "n3", "title": "t", "text": "x", "attempts": 0}
        server = self._server({})
        with patch.object(notify.smtplib, "SMTP_SSL", return_value=server):
            result = notify._deliver(self.CFG, notif)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self._recipients(server), self.CFG["mail_to"])
        self.assertEqual(len(notif["smtp_accepted_recipient_ids"]), 2)


# --------------------------------------------------------------- R08

class DaemonHeartbeatRecoveryTests(_StateMixin, unittest.TestCase):
    CFG = {"channel": "webhook", "url": "http://localhost/unused", "format": "generic",
           "timeout_seconds": 3, "renotify_after": 21600, "quiet_hours": None,
           "mute_until": 0, "enabled": True}

    def _run_one_round(self, once):
        schedule._save([], {"last_scan": time.time() - 1800})
        with patch.object(notify, "load_config", return_value=self.CFG), \
                patch.object(notify, "_post", return_value=(200, "")), \
                patch.object(schedule.time, "sleep",
                             side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()):
            if once:
                code = schedule.daemon(once=True)
                self.assertEqual(code, 0)
            else:
                self.assertEqual(schedule.daemon(once=False), 0)

    def _severity(self):
        return notify._load_state()["items"]["run:schedule-daemon"].get("severity")

    def _kinds(self):
        return [r.get("kind") for r in notify.list_notifications(tail=50)]

    def test_permanent_daemon_recovers_stale_heartbeat(self):
        self._run_one_round(once=False)
        self.assertEqual(schedule.daemon_health()["state"], "fresh")
        self.assertIsNone(self._severity(), "常驻 daemon 恢复后仍留着 critical 告警")
        kinds = self._kinds()
        self.assertEqual(kinds.count("anomaly"), 1, kinds)
        self.assertEqual(kinds.count("recovery"), 1, kinds)

    def test_once_mode_recovers_stale_heartbeat(self):
        self._run_one_round(once=True)
        self.assertEqual(schedule.daemon_health()["state"], "fresh")
        self.assertIsNone(self._severity())
        self.assertEqual(self._kinds().count("recovery"), 1)


# --------------------------------------------------------------- R09

class LocalLogCollectionTests(unittest.TestCase):
    def test_stderr_only_docker_logs_are_scanned(self):
        def run(argv, **kwargs):
            merged = kwargs.get("stderr") == subprocess.STDOUT
            return subprocess.CompletedProcess(
                argv, 0, "ERROR worker failed\n" if merged else "",
                "" if merged else "ERROR worker failed\n")

        with patch.object(logscan.subprocess, "run", side_effect=run):
            _, lines = logscan.from_docker("review-fixture")
        result = logscan.scan(lines)
        self.assertEqual(result["group_total"], 1)

    def test_both_streams_are_kept(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0,
                                               "out: started\nERROR boom\n", "")

        with patch.object(logscan.subprocess, "run", side_effect=run):
            _, lines = logscan.from_journal("app")
        self.assertEqual(len(lines), 2)
        self.assertEqual(logscan.scan(lines)["group_total"], 1)

    def test_nonzero_exit_is_a_collection_error(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "some partial output\n", "")

        with patch.object(logscan.subprocess, "run", side_effect=run):
            with self.assertRaises(logscan.ScanSourceError) as ctx:
                logscan.from_docker("review-fixture")
        self.assertIn("退出码 1", str(ctx.exception))


class RemoteLogCollectionTests(unittest.TestCase):
    def test_nonzero_exit_with_output_is_a_collection_error(self):
        output = "[stderr]\nError response: unavailable\npassword=review-fixture-secret"
        for collect, name in ((logscan.from_docker, "missing"),
                              (logscan.from_journal, "app")):
            with self.subTest(source=collect.__name__), \
                    patch.object(remote, "run", return_value=(1, output)):
                with self.assertRaises(logscan.ScanSourceError) as ctx:
                    collect(name, target="fixture")
                self.assertIn("exit=1", str(ctx.exception))
                self.assertIn("unavailable", str(ctx.exception))
                self.assertNotIn("review-fixture-secret", str(ctx.exception))

    def test_successful_remote_output_is_preserved(self):
        output = "started\nERROR worker failed\n"
        with patch.object(remote, "run", return_value=(0, output)):
            _, lines = logscan.from_docker("fixture", target="fixture")
        self.assertEqual(lines, output.splitlines())


# ------------------------------------------------ 复审 F01（完整执行环境）

class FullEnvironmentBindingTests(_LoopMixin, _StateMixin, unittest.TestCase):
    """kubeconfig / SSH identity / options 变了就是另一单，不能复用旧答复。"""

    CMD = "systemctl restart alpha"

    def _targets(self, identity="~/.ssh/id_a", options=None):
        target = {"host": "10.0.0.11", "identity": identity}
        if options:
            target["options"] = options
        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (core.STATE_DIR / "targets.json").write_text(
            json.dumps({"web1": target}), encoding="utf-8")

    def _turn(self, ex, sess, resuming, answer="yes"):
        def simulate(prompt, session, ctx, **kwargs):
            return tools.dispatch("ops_approve", {
                "command": self.CMD, "target": "web1",
                "impact": "review fixture"}, ctx)
        with patch.object(a2a_executor.loop, "run_turn", side_effect=simulate):
            return ex._run(sess, "review", resuming, answer, "env-task")

    def test_kubeconfig_change_requires_new_confirmation(self):
        ex, sess = _executor(), None
        sess = ex._session("env-kubeconfig")
        command = "kubectl --context review scale deployment alpha --replicas=1"

        def turn(kubeconfig, resuming):
            def simulate(prompt, session, ctx, **kwargs):
                return tools.dispatch("ops_approve", {
                    "command": command, "impact": "review fixture"}, ctx)
            with patch.dict(os.environ, {"KUBECONFIG": kubeconfig}), \
                    patch.object(a2a_executor.loop, "run_turn", side_effect=simulate):
                return ex._run(sess, "review", resuming, "yes", "kube-task")

        with self.assertRaises(a2a_executor.NeedUserInput):
            turn("/fixture/cluster-a", False)
        with self.assertRaises(a2a_executor.NeedUserInput):
            turn("/fixture/cluster-b", True)
        self.assertNotEqual(core.check(command)[0], 0)

    def test_ssh_identity_change_requires_new_confirmation(self):
        self._targets(identity="~/.ssh/id_a")
        ex = _executor()
        sess = ex._session("env-identity")
        with self.assertRaises(a2a_executor.NeedUserInput):
            self._turn(ex, sess, False)
        self._targets(identity="~/.ssh/id_b")
        with self.assertRaises(a2a_executor.NeedUserInput):
            self._turn(ex, sess, True)
        self.assertNotEqual(core.check(self.CMD, target="web1")[0], 0)

    def test_ssh_options_change_requires_new_confirmation(self):
        self._targets(options=["-o", "ProxyJump=jump-a"])
        ex = _executor()
        sess = ex._session("env-options")
        with self.assertRaises(a2a_executor.NeedUserInput):
            self._turn(ex, sess, False)
        self._targets(options=["-o", "ProxyJump=jump-b"])
        with self.assertRaises(a2a_executor.NeedUserInput):
            self._turn(ex, sess, True)


class ScenarioEnvironmentBindingTests(_StateMixin, unittest.TestCase):
    """场景确认同样绑定完整环境（preview 文本只是展示）。"""

    SCEN = {"name": "review-fake", "description": "review fixture", "params": {},
            "target": "local", "precheck": [], "postcheck": [],
            "exec": {"cmd": "kubectl --context review apply -f x.yaml",
                     "snapshot_files": [], "snapshot_cmds": []}}

    def _confirm_key(self, kubeconfig):
        recorded = []

        def confirm(question, key):
            recorded.append(key)
            raise a2a_executor.NeedUserInput(question)

        ctx = tools.ToolContext(ask_user=None, confirm=confirm)
        with patch.dict(os.environ, {"KUBECONFIG": kubeconfig}), \
                patch.object(scenario, "load", return_value=dict(self.SCEN)):
            with self.assertRaises(a2a_executor.NeedUserInput):
                tools.dispatch("scenario_run", {"name": "review-fake"}, ctx)
        self.assertTrue(recorded)
        return recorded[-1]

    def test_kubeconfig_change_is_a_different_ticket(self):
        key_a = self._confirm_key("/fixture/cluster-a")
        key_b = self._confirm_key("/fixture/cluster-b")
        self.assertNotEqual(key_a, key_b)


# ------------------------------------------------ 复审 F02（确认生命周期）

class ClarificationLifecycleTests(_LoopMixin, _StateMixin, unittest.TestCase):
    """对普通问题回答 yes，不能顺带批准之前被拒绝的变更单。"""

    CMD = "systemctl restart alpha"

    def _approve_attempt(self, ex, sess, resuming, answer="yes"):
        def simulate(prompt, session, ctx, **kwargs):
            return tools.dispatch("ops_approve", {
                "command": self.CMD, "impact": "review fixture"}, ctx)
        with patch.object(a2a_executor.loop, "run_turn", side_effect=simulate):
            return ex._run(sess, "review", resuming, answer, "clarify-task")

    def test_clarification_invalidates_pending_change(self):
        ex = _executor()
        sess = ex._session("clarify")
        with self.assertRaises(a2a_executor.NeedUserInput):
            self._approve_attempt(ex, sess, resuming=False)

        def clarify(prompt, session, ctx, **kwargs):
            # 第一问消费"no"，第二问是新的普通问题 → 必须重新中断
            tools.dispatch("ask_user", {"question": "first clarification"}, ctx)
            return tools.dispatch(
                "ask_user", {"question": "continue read-only diagnostics?"}, ctx)

        with patch.object(a2a_executor.loop, "run_turn", side_effect=clarify):
            with self.assertRaises(a2a_executor.NeedUserInput) as question:
                ex._run(sess, "no", True, "no", "clarify-task")
        self.assertIn("read-only diagnostics", question.exception.question)

        # 对只读诊断回答 yes：旧变更单的身份已作废，必须重新展示确认
        with self.assertRaises(a2a_executor.NeedUserInput):
            self._approve_attempt(ex, sess, resuming=True, answer="yes")
        self.assertNotEqual(core.check(self.CMD)[0], 0)


# ------------------------------------------------ 复审 F03（预览读取边界）

class RollbackPreviewGuardTests(_StateMixin, unittest.TestCase):
    """回滚预览发生在确认之前：改向/敏感必须在读取内容之前拒绝。"""

    def _patches(self, commands):
        def local_ssh(name, cmd, stdin=None, timeout=120):
            commands.append(cmd)
            return subprocess.run(["/bin/sh", "-c", cmd], input=stdin,
                                  capture_output=True, timeout=timeout)

        return [patch.object(remote, "_ssh", side_effect=local_ssh),
                patch.object(remote, "_target_stat", return_value=(0o600, "ops:ops")),
                patch.object(remote, "_target_machine", return_value="fixture/abc")]

    def _snapshot(self, source, commands):
        (core.STATE_DIR).mkdir(parents=True, exist_ok=True)
        (core.STATE_DIR / "targets.json").write_text(
            json.dumps({"fixture": {"host": "fixture.invalid"}}), encoding="utf-8")
        with contextlib.ExitStack() as stack:
            for patcher in self._patches(commands):
                stack.enter_context(patcher)
            return remote.snapshot_file("fixture", str(source))

    def _preview(self, sid, commands):
        commands.clear()
        with contextlib.ExitStack() as stack:
            for patcher in self._patches(commands):
                stack.enter_context(patcher)
            return remote.rollback_plan(sid)

    def test_final_symlink_redirect_is_rejected_before_read(self):
        source = self.root / "app.conf"
        source.write_bytes(b"original configuration\n")
        key = self.root / ".ssh" / "id_rsa"
        key.parent.mkdir()
        key.write_bytes(b"SYNTHETIC_PRIVATE_CONTENT_SENTINEL\n")
        commands = []
        sid = self._snapshot(source, commands)

        source.unlink()
        source.symlink_to(key)
        with self.assertRaises(remote.RemoteError) as ctx:
            self._preview(sid, commands)
        self.assertIn("真实路径已改变", str(ctx.exception))
        self.assertFalse(any("OPSX-EXISTS-" in c for c in commands), commands)
        self.assertNotIn("SYNTHETIC_PRIVATE_CONTENT_SENTINEL", str(ctx.exception))

    def test_parent_directory_redirect_is_rejected_before_read(self):
        source = self.root / "conf" / "app.conf"
        source.parent.mkdir()
        source.write_bytes(b"original configuration\n")
        commands = []
        sid = self._snapshot(source, commands)

        evil = self.root / "evil"
        evil.mkdir()
        (evil / "app.conf").write_bytes(b"SYNTHETIC_PRIVATE_CONTENT_SENTINEL\n")
        source.parent.rename(self.root / "conf-old")
        (self.root / "conf").symlink_to(evil)
        with self.assertRaises(remote.RemoteError) as ctx:
            self._preview(sid, commands)
        self.assertIn("真实路径已改变", str(ctx.exception))
        self.assertFalse(any("OPSX-EXISTS-" in c for c in commands), commands)
        self.assertNotIn("SYNTHETIC_PRIVATE_CONTENT_SENTINEL", str(ctx.exception))

    def test_legacy_snapshot_resolving_to_credential_path_is_rejected(self):
        source = self.root / "app.conf"
        source.write_bytes(b"original configuration\n")
        commands = []
        sid = self._snapshot(source, commands)
        # 旧快照没有记录真实路径：只能靠敏感路径判定兜住
        meta_path = core.STATE_DIR / "snapshots" / sid / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for item in meta["items"]:
            item.pop("resolved_path", None)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        key = self.root / ".ssh" / "id_rsa"
        key.parent.mkdir(exist_ok=True)
        key.write_bytes(b"SYNTHETIC_PRIVATE_CONTENT_SENTINEL\n")
        source.unlink()
        source.symlink_to(key)
        with self.assertRaises(remote.RemoteError) as ctx:
            self._preview(sid, commands)
        self.assertIn("敏感", str(ctx.exception))
        self.assertFalse(any("OPSX-EXISTS-" in c for c in commands), commands)
        self.assertNotIn("SYNTHETIC_PRIVATE_CONTENT_SENTINEL", str(ctx.exception))


# ------------------------------------------------ 复审 F04（部分送达与恢复）

class PartialDeliveryRecoveryTests(_StateMixin, unittest.TestCase):
    """有人收到异常就必须收到恢复；没收到的人不能收到无来源的恢复。"""

    CFG_RAW = {"channel": "email", "smtp_host": "smtp.example.test",
               "smtp_port": 465, "mail_from": "ops@example.test",
               "mail_to": ["ok@example.test", "rejected@example.test"]}

    def test_recovery_goes_only_to_recipients_who_got_the_anomaly(self):
        cfg = notify._validate_config(dict(self.CFG_RAW))
        server = MagicMock()
        server.__enter__.return_value = server
        server.send_message.return_value = {
            "rejected@example.test": (550, b"fixture rejected")}
        failed = {"task_id": "sch-fixture", "run_id": "run-failed", "status": "failed",
                  "error": "synthetic error", "finished_ts": time.time() - 10}
        healthy = {"task_id": "sch-fixture", "run_id": "run-healthy",
                   "status": "succeeded", "finished_ts": time.time()}
        with patch.object(notify, "load_config", return_value=cfg), \
                patch.object(notify.smtplib, "SMTP_SSL", return_value=server):
            bad = notify.process_run(failed)
            self.assertEqual(bad["notification"]["status"], "failed")
            accepted = bad["notification"]["smtp_accepted_recipient_ids"]
            self.assertEqual(len(accepted), 1)

            server.send_message.return_value = {}
            good = notify.process_run(healthy)

            self.assertIsNotNone(good["notification"],
                                 "收到过异常的人没有收到恢复")
            self.assertEqual(good["notification"]["recipient_ids"], accepted)
            self.assertEqual(server.send_message.call_args.kwargs["to_addrs"],
                             ["ok@example.test"])

            # 旧异常已恢复：不再补发，也不会给没收到过的人发无来源的恢复
            sent_before = server.send_message.call_count
            notify.retry_pending(notif_id=bad["notification"]["id"])
            self.assertEqual(server.send_message.call_count, sent_before)

        statuses = {n["id"]: n["status"] for n in notify.list_notifications(tail=50)}
        self.assertEqual(statuses[bad["notification"]["id"]], "superseded")

    def test_newly_accepted_recipient_from_partial_retry_gets_recovery(self):
        """三人、重试新增部分成功：A/B 都已收到异常，恢复必须发给 A/B、排除 C。"""
        recipients = ["a@example.test", "b@example.test", "c@example.test"]
        cfg = notify._validate_config(dict(self.CFG_RAW, mail_to=recipients))
        server = MagicMock()
        server.__enter__.return_value = server
        server.send_message.side_effect = [
            {"b@example.test": (450, b"later"), "c@example.test": (450, b"later")},
            {"c@example.test": (450, b"later")},
            {},
        ]
        failed = {"task_id": "sch-three", "run_id": "run-failed", "status": "failed",
                  "error": "synthetic error", "finished_ts": time.time() - 10}
        healthy = {"task_id": "sch-three", "run_id": "run-healthy",
                   "status": "succeeded", "finished_ts": time.time()}
        with patch.object(notify, "load_config", return_value=cfg), \
                patch.object(notify.smtplib, "SMTP_SSL", return_value=server):
            bad = notify.process_run(failed)
            retried = notify.retry_pending(notif_id=bad["notification"]["id"])[0]
            # 仍有人拒收：不能提前宣称整条通知完成，但新增受众必须进状态机
            self.assertEqual(retried["status"], "failed")
            self.assertEqual(len(retried["smtp_accepted_recipient_ids"]), 2)
            self.assertEqual(server.send_message.call_args.kwargs["to_addrs"],
                             ["b@example.test", "c@example.test"])
            state = notify._load_state()["items"]["run:sch-three"]
            self.assertEqual(len(state.get("delivered_recipient_ids") or []), 2)
            good = notify.process_run(healthy)

        self.assertEqual(good["notification"]["kind"], "recovery")
        self.assertEqual(server.send_message.call_args.kwargs["to_addrs"],
                         ["a@example.test", "b@example.test"])

    def test_failed_recovery_does_not_narrow_the_anomaly_audience(self):
        """渠道切换后恢复部分失败：不能把原异常受众缩小成"已收到恢复的人"。"""
        cfg_webhook = notify._validate_config(
            {"channel": "webhook", "url": "http://localhost/unused", "format": "generic"})
        cfg_email = notify._validate_config(dict(self.CFG_RAW))
        failed = {"task_id": "sch-switch", "run_id": "run-failed", "status": "failed",
                  "error": "synthetic error", "finished_ts": time.time() - 10}
        healthy = {"task_id": "sch-switch", "run_id": "run-healthy",
                   "status": "succeeded", "finished_ts": time.time()}
        server = MagicMock()
        server.__enter__.return_value = server
        server.send_message.return_value = {
            "rejected@example.test": (450, b"later")}
        with patch.object(notify, "_post", return_value=(200, "")):
            with patch.object(notify, "load_config", return_value=cfg_webhook):
                notify.process_run(failed)
            with patch.object(notify, "load_config", return_value=cfg_email), \
                    patch.object(notify.smtplib, "SMTP_SSL", return_value=server):
                notify.process_run(healthy)
        entry = notify._load_state()["items"]["run:sch-switch"]
        self.assertNotIn("delivered_recipient_ids", entry,
                         "纯恢复的部分失败不应把原异常受众写窄")

    def test_full_delivery_then_recovery_reaches_everyone(self):
        cfg = notify._validate_config(dict(self.CFG_RAW))
        server = MagicMock()
        server.__enter__.return_value = server
        server.send_message.return_value = {}
        failed = {"task_id": "sch-all", "run_id": "run-failed", "status": "failed",
                  "error": "synthetic error", "finished_ts": time.time() - 10}
        healthy = {"task_id": "sch-all", "run_id": "run-healthy",
                   "status": "succeeded", "finished_ts": time.time()}
        with patch.object(notify, "load_config", return_value=cfg), \
                patch.object(notify.smtplib, "SMTP_SSL", return_value=server):
            notify.process_run(failed)
            good = notify.process_run(healthy)
        self.assertIsNotNone(good["notification"])
        ids = {notify._recipient_id(addr) for addr in self.CFG_RAW["mail_to"]}
        self.assertEqual(set(good["notification"]["recipient_ids"]), ids)
        self.assertEqual(server.send_message.call_args.kwargs["to_addrs"],
                         self.CFG_RAW["mail_to"])


# ------------------------------------------------ 复审 F05（心跳容错）

class HeartbeatSaveFailureTests(_StateMixin, unittest.TestCase):
    """首次心跳写盘失败不能丢掉恢复意图：下一轮成功后仍要发恢复。"""

    CFG = {"channel": "webhook", "url": "http://localhost/unused", "format": "generic",
           "timeout_seconds": 3, "renotify_after": 21600, "quiet_hours": None,
           "mute_until": 0, "enabled": True}

    def test_recovery_survives_first_heartbeat_save_failure(self):
        schedule._save([], {"last_scan": time.time() - 1800})
        original_save = schedule._safe_save
        scans = [0]
        sleeps = [0]

        def save(items, meta=None):
            scans[0] += 1
            if scans[0] == 1:
                return False
            return original_save(items, meta)

        def sleep(seconds):
            sleeps[0] += 1
            if sleeps[0] >= 2:
                raise KeyboardInterrupt

        with patch.object(notify, "load_config", return_value=self.CFG), \
                patch.object(notify, "_post", return_value=(200, "")), \
                patch.object(schedule, "_safe_save", side_effect=save), \
                patch.object(schedule.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(schedule.daemon(once=False), 0)
        self.assertEqual(scans[0], 2)
        self.assertEqual(schedule.daemon_health()["state"], "fresh")
        entry = notify._load_state()["items"]["run:schedule-daemon"]
        self.assertIsNone(entry["severity"])
        kinds = [r.get("kind") for r in notify.list_notifications(tail=50)]
        self.assertEqual(kinds.count("recovery"), 1, kinds)


if __name__ == "__main__":
    unittest.main()
