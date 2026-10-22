"""Roadmap W1/W5 safety regressions; no real SSH or service writes."""
import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import execution
import mcp_server
import remote


class ChangeSafetyReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-review-safety-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.root / "state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.source = self.root / "app.conf"
        self.source.write_bytes(b"archived")

    def unknown(self, sid, target=None):
        env = remote.target_env(target) if target else core.local_env()
        rec = execution.begin("rollback", f"rollback {sid}", target, env,
                              snapshot_id=sid, phase="restore")
        execution.finish(rec["id"], "unknown", error="lost acknowledgement")
        return rec

    @staticmethod
    def local_ssh(name, command, stdin=None, timeout=120):
        return subprocess.run(["/bin/sh", "-c", command], input=stdin,
                              capture_output=True, timeout=timeout)

    def register_target(self):
        core._ensure_state_dir()
        (core.STATE_DIR / "targets.json").write_text(json.dumps({
            "test": {"host": "review.invalid", "user": "ops"}}))

    def test_local_unknown_rollback_blocks_until_resolved(self):
        sid = core.snapshot_file(str(self.source))
        self.source.write_bytes(b"later changes")
        rec = self.unknown(sid)
        with self.assertRaisesRegex(core.OpsxError, rec["id"]):
            core.rollback(sid, confirm=True)
        self.assertEqual(self.source.read_bytes(), b"later changes")
        self.assertEqual(len(list((core.STATE_DIR / "snapshots").iterdir())), 1)
        execution.resolve(rec["id"], "observed-failed")
        core.rollback(sid, confirm=True)
        self.assertEqual(self.source.read_bytes(), b"archived")

    def test_local_rollback_rechecks_unknown_after_taking_lock(self):
        sid = core.snapshot_file(str(self.source))
        self.source.write_bytes(b"later changes")
        real_lock = core.target_lock
        @contextlib.contextmanager
        def race(*args, **kwargs):
            with real_lock(*args, **kwargs):
                self.unknown(sid)
                yield
        with patch.object(core, "target_lock", side_effect=race):
            with self.assertRaisesRegex(core.OpsxError, "未核实"):
                core.rollback(sid, confirm=True)
        self.assertEqual(self.source.read_bytes(), b"later changes")
        self.assertEqual(execution.list_records()[0]["status"], "unknown")
        self.assertEqual(sorted(r["status"] for r in execution.list_records()),
                         ["aborted", "unknown"])

    def test_remote_lost_restore_ack_cannot_be_replayed(self):
        self.register_target()
        with patch.object(remote, "_ssh", side_effect=self.local_ssh):
            sid = remote.snapshot_file("test", str(self.source))
        self.source.write_bytes(b"change before rollback")
        def disconnected(name, command, stdin=None, timeout=120):
            result = self.local_ssh(name, command, stdin, timeout)
            if ".opsx-restore." in command:
                return subprocess.CompletedProcess([], 255, b"", b"connection reset")
            return result
        with patch.object(remote, "_ssh", side_effect=disconnected):
            with self.assertRaises(remote._UncertainChange):
                remote.rollback(sid, confirm=True)
        self.assertEqual(self.source.read_bytes(), b"archived")
        rec = execution.find_latest(f"rollback {sid}", "test")
        self.assertEqual(rec["status"], "unknown")
        self.source.write_bytes(b"later changes")
        with patch.object(remote, "_ssh", side_effect=self.local_ssh), \
                patch.object(core, "_rollback_backup") as unused:
            with self.assertRaisesRegex(core.OpsxError, rec["id"]):
                remote.rollback(sid, confirm=True)
            unused.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b"later changes")
        execution.resolve(rec["id"], "observed-failed")
        with patch.object(remote, "_ssh", side_effect=self.local_ssh):
            remote.rollback(sid, confirm=True)
        self.assertEqual(self.source.read_bytes(), b"archived")

    def test_remote_rollback_rechecks_unknown_after_taking_lock(self):
        self.register_target()
        with patch.object(remote, "_ssh", side_effect=self.local_ssh):
            sid = remote.snapshot_file("test", str(self.source))
        self.source.write_bytes(b"later changes")
        real_lock = core.target_lock
        @contextlib.contextmanager
        def race(*args, **kwargs):
            with real_lock(*args, **kwargs):
                self.unknown(sid, "test")
                yield
        with patch.object(core, "target_lock", side_effect=race), \
                patch.object(remote, "_ssh", side_effect=self.local_ssh):
            with self.assertRaisesRegex(core.OpsxError, "未核实"):
                remote.rollback(sid, confirm=True)
        self.assertEqual(self.source.read_bytes(), b"later changes")
        self.assertEqual(sorted(r["status"] for r in execution.list_records()),
                         ["aborted", "unknown"])

    def test_mcp_confirm_does_not_override_unknown(self):
        sid = core.snapshot_file(str(self.source))
        rec = self.unknown(sid)
        self.source.write_bytes(b"later changes")
        result = mcp_server.call_tool("ops_rollback", {"snapshot_id": sid, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertIn(rec["id"], result["content"][0]["text"])
        self.assertEqual(self.source.read_bytes(), b"later changes")

    def test_corrupt_execution_evidence_blocks_rollback(self):
        sid = core.snapshot_file(str(self.source))
        rec = self.unknown(sid)
        execution._path(rec["id"]).write_text("truncated")
        self.source.write_bytes(b"later changes")
        with self.assertRaisesRegex(core.OpsxError, "执行记录损坏"):
            core.rollback(sid, confirm=True)
        self.assertEqual(self.source.read_bytes(), b"later changes")

    def test_signaled_local_shell_is_unknown_and_blocks_replay(self):
        command = "kill -TERM $$"
        core.approve(command)
        code, _, output = core.exec_change(command)
        self.assertEqual(code, -signal.SIGTERM)
        self.assertIn("需先核实", output)
        rec = execution.find_latest(command)
        self.assertEqual(rec["status"], "unknown")
        core.approve(command)
        self.assertEqual(core.exec_change(command)[0], core.EXEC_ABORT_UNKNOWN)

    def test_timeout_with_terminated_process_group_remains_failed(self):
        with patch.object(core, "run_shell", side_effect=subprocess.TimeoutExpired("sleep", 1)):
            self.assertEqual(core._run_change("sleep 100", 1)[1], "failed")

    def alternate_group(self):
        group = next((group for group in os.getgroups() if group != os.getgid()), None)
        if group is None:
            self.skipTest("requires membership in an alternate file group")
        try:
            os.chown(self.source, -1, group)
        except PermissionError:
            self.skipTest("filesystem does not allow changing to an alternate file group")
        return group

    def test_local_atomic_restore_preserves_existing_group(self):
        group = self.alternate_group()
        sid = core.snapshot_file(str(self.source))
        self.source.write_bytes(b"later changes")
        core.rollback(sid, confirm=True)
        self.assertEqual(self.source.stat().st_gid, group)
        self.assertEqual(self.source.read_bytes(), b"archived")

    def test_local_owner_failure_does_not_replace_original(self):
        self.alternate_group()
        sid = core.snapshot_file(str(self.source))
        self.source.write_bytes(b"later changes")
        with patch.object(core.os, "chown", side_effect=PermissionError("owner denied")):
            with self.assertRaisesRegex(core.OpsxError, "owner denied"):
                core.rollback(sid, confirm=True)
        self.assertEqual(self.source.read_bytes(), b"later changes")
        self.assertEqual(execution.find_latest(f"rollback {sid}")["status"], "aborted")

    def test_quoted_context_text_is_not_a_kubectl_scope(self):
        for command in (
            'kubectl annotate pod app note=" --context=prod"',
            'kubectl annotate pod app --overwrite note=" --context prod"',
            'kubectl apply --filename=" --context=prod"',
            'kubectl apply -f --context=prod',
            'kubectl apply --field-manager --context=prod',
            'kubectl apply -- --context=prod',
            'sh -c "kubectl apply --context=prod -f deploy.yaml"',
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(core.OpsxError, "--context"):
                    core.approve(command)

    def test_real_kubectl_scope_ignores_context_text_in_annotation(self):
        command = 'kubectl annotate pod app --context=real -nprod note=" --context=other"'
        stamp = json.loads(core.approve(command).read_text())
        self.assertEqual(stamp["env"]["k8s"], {"context": "real", "namespace": "prod"})
        self.assertTrue(core.check_stamp(command))
        self.assertEqual(core.k8s_scope('sudo kubectl apply --context "prod" --namespace="ops" -f x'),
                         {"context": "prod", "namespace": "ops"})

    def test_mcp_internal_exception_is_redacted_on_stderr(self):
        secret = "MCP-TRACEBACK-SENTINEL"
        stderr = io.StringIO()
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "ops_list", "arguments": {}}}
        with patch.object(mcp_server, "call_tool", side_effect=RuntimeError("password=" + secret)), \
                contextlib.redirect_stderr(stderr):
            response = mcp_server.handle_message(request)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn(secret, json.dumps(response))
        self.assertIn(core.REDACTED, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
