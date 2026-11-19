import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core
import execution
import remote


class GuardAuditTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        state = mock.patch.object(core, "STATE_DIR", self.root / "state")
        state.start()
        self.addCleanup(state.stop)

    def test_command_only_snapshot_declaration_is_enforced(self):
        cmd = "printf example"
        core.approve(cmd, snapshot_cmds=[("echo snapshot", "status")])
        self.assertIsNotNone(core.check_snapshot_declaration(cmd, None, [], []))
        self.assertIsNone(core.check_snapshot_declaration(
            cmd, None, [], [("echo snapshot", "status")]))

    def test_reapproved_snapshot_plan_must_still_match_at_consumption(self):
        cmd = "printf example"
        source = self.root / "example.conf"
        source.write_text("before")
        core.approve(cmd, snapshot_files=[str(source)])
        capture = core._snapshot_file_into

        def capture_then_reapprove(*args):
            capture(*args)
            core.approve(cmd, snapshot_files=[str(self.root / "new.conf")])

        with mock.patch.object(core, "_snapshot_file_into", side_effect=capture_then_reapprove), \
                mock.patch.object(core, "_run_change", return_value=(0, "succeeded", "", None)) as dispatch:
            code, _, output = core.exec_change(cmd, snapshot_files=[str(source)])
        self.assertEqual(code, 2, output)
        self.assertEqual(core.get_stamp(cmd)["used"], 0)
        dispatch.assert_not_called()

    def test_explicit_rollback_replaces_a_dangling_link_to_a_normal_file(self):
        source = self.root / "app.conf"
        source.write_text("saved")
        snapshot_id = core.snapshot_file(str(source))
        source.unlink()
        source.symlink_to(self.root / "missing.conf")
        self.assertFalse(core.rollback_plan(snapshot_id)["files"][0]["exists"])
        core.rollback(snapshot_id, confirm=True)
        self.assertFalse(source.is_symlink())
        self.assertEqual(source.read_text(), "saved")

    def test_local_rollback_refuses_to_overwrite_nonregular_entry(self):
        source = self.root / "app.conf"
        source.write_text("saved")
        snapshot_id = core.snapshot_file(str(source))
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(core.OpsxError):
            core.rollback_plan(snapshot_id)
        with self.assertRaises(core.OpsxError):
            core.rollback(snapshot_id, confirm=True)
        self.assertEqual(execution.list_records()[0]["status"], "aborted")

    def test_remote_rollback_checks_sensitive_path_before_reading(self):
        snapshot = {"target": "test", "items": [
            {"type": "file", "src": "/etc/example.conf", "dest": "files/example"}]}
        with mock.patch.object(core, "load_snapshot", return_value=(self.root, snapshot)), \
                mock.patch.object(remote, "get_target", return_value={"host": "test.invalid"}), \
                mock.patch.object(remote, "_target_resolved_path", return_value="/etc/example.conf"), \
                mock.patch.object(core, "sensitive_snapshot_path", return_value="protected"), \
                mock.patch.object(remote, "_remote_read", return_value=(False, b"")) as read:
            with self.assertRaisesRegex(remote.RemoteError, "敏感"):
                remote.rollback("snapshot", confirm=True)
        read.assert_not_called()

    def test_stale_view_does_not_prune_raw_running_record(self):
        rec = execution.begin("exec", "echo example", phase="lock")
        with mock.patch.object(execution, "_pid_alive", return_value=False):
            execution.prune(keep=0)
        self.assertEqual(execution.get(rec["id"], view=False)["status"], "running")

    def test_execution_with_missing_ledger_cannot_dispatch(self):
        cmd = "printf example"
        source = self.root / "example.conf"
        source.touch()
        core.approve(cmd, snapshot_files=[str(source)])
        capture = core._snapshot_file_into

        def capture_then_remove_record(*args):
            capture(*args)
            for path in execution._dir().glob("*.json"):
                path.unlink()

        with mock.patch.object(core, "_snapshot_file_into", side_effect=capture_then_remove_record), \
                mock.patch.object(core, "_run_change", return_value=(0, "succeeded", "", None)) as dispatch:
            with self.assertRaisesRegex(core.OpsxError, "执行记录"):
                core.exec_change(cmd, snapshot_files=[str(source)])
        self.assertEqual(core.get_stamp(cmd)["used"], 0)
        dispatch.assert_not_called()

    def test_snapshot_prune_ignores_concurrent_removal(self):
        source = self.root / "example.conf"
        source.touch()
        core.snapshot_file(str(source))
        with mock.patch.object(core.shutil, "rmtree", side_effect=FileNotFoundError()):
            core.prune_snapshots(keep=0)

    def test_kubectl_diff_drops_external_helper_from_local_environment(self):
        with mock.patch.dict(os.environ, {"KUBECTL_EXTERNAL_DIFF": "example-diff"}), \
                mock.patch.object(core.subprocess, "Popen") as popen:
            process = popen.return_value.__enter__.return_value
            process.communicate.return_value = ("", "")
            core.run_shell("kubectl diff -f example.yaml", timeout=1)
        child_env = popen.call_args.kwargs.get("env")
        self.assertIsNotNone(child_env)
        self.assertNotIn("KUBECTL_EXTERNAL_DIFF", child_env)

    def test_kubectl_diff_drops_external_helper_from_remote_environment(self):
        with mock.patch.object(remote, "get_target", return_value={"host": "test.invalid"}), \
                mock.patch.object(remote.subprocess, "run", return_value=
                                  subprocess.CompletedProcess([], 0, b"", b"")) as run:
            remote._ssh("test", "kubectl diff -f example.yaml")
        command = run.call_args.args[0][-1]
        self.assertTrue(command.startswith("unset KUBECTL_EXTERNAL_DIFF || exit $?\n"), command)

    def test_kubectl_preview_requires_an_actual_flag(self):
        command = "kubectl apply -f example.yaml --dry-run=server"
        self.assertEqual(core.classify(command)[0], "R0")
        parsed = ["kubectl", "apply", "-f", "example.yaml"]
        with mock.patch.object(core.shlex, "split", return_value=parsed):
            self.assertNotEqual(core.classify(command)[0], "R0")

    def test_mcp_audit_only_splits_physical_newlines(self):
        import mcp_server

        core._ensure_state_dir()
        audit = core.STATE_DIR / "audit.jsonl"
        audit.write_text('{"message": "before\u2028after"}\n')
        result = mcp_server.call_tool("ops_audit", {"tail": 1})
        self.assertEqual(json.loads(result["content"][0]["text"])["events"],
                         [{"message": "before\u2028after"}])

    def test_ssh_target_validation_uses_one_registry_read(self):
        with mock.patch.object(remote, "get_target", side_effect=[
                {"host": "first.example"}, {"host": ""}]) as lookup:
            self.assertEqual(remote.target_env("test")["host"], "first.example")
        lookup.assert_called_once_with("test")

    def test_mcp_decoder_depth_error_leaves_loop_running(self):
        import io
        import mcp_server

        ping = json.dumps({"jsonrpc": "2.0", "id": "p", "method": "ping"})
        output = io.StringIO()
        with mock.patch.object(mcp_server.json, "loads", side_effect=[RecursionError(), json.loads(ping)]), \
                mock.patch.object(mcp_server.sys, "stdin", io.StringIO(f"invalid\n{ping}\n")), \
                mock.patch.object(mcp_server.sys, "stdout", output):
            mcp_server.serve()
        invalid, valid = (json.loads(line) for line in output.getvalue().splitlines())
        self.assertEqual(invalid["error"]["code"], -32700)
        self.assertEqual(valid["id"], "p")
