"""W8 harness integration: real subprocesses/loopback requests, no external services."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "w8_acceptance.py"
spec = importlib.util.spec_from_file_location("w8_acceptance", SCRIPT)
w8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w8)


class W8AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="opsx-w8-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.existing_state = cls.root / "existing-state"
        cls.existing_state.mkdir()
        (cls.existing_state / "sentinel").write_text("do not modify")
        cls.output = cls.root / "acceptance.json"
        cls.output.write_text("previous public report")
        cls.output.chmod(0o644)
        cls.secret = "w8-test-real-provider-key-must-not-leak"
        cls.original_config = (ROOT / "config.yaml").read_bytes()
        cls.process = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(cls.output)], cwd=cls.root,
            env=dict(os.environ, OPSX_STATE_DIR=str(cls.existing_state),
                     OPSAGENT_BASE_URL="https://invalid.example.test/do-not-contact",
                     OPSAGENT_API_KEY=cls.secret,
                     HTTPS_PROXY="http://invalid.example.test:9"),
            text=True, capture_output=True, timeout=30)
        cls.report = json.loads(cls.output.read_text())

    def test_real_local_drills_all_pass(self):
        self.assertEqual(self.process.returncode, 0, self.process.stderr)
        self.assertEqual(self.report["local_status"], "passed")
        self.assertEqual(len(self.report["checks"]), 10)
        self.assertTrue(all(item["status"] == "passed" for item in self.report["checks"]),
                        self.report["checks"])
        checks = {item["id"]: item["evidence"] for item in self.report["checks"]}
        self.assertEqual(checks["exit_zero_unhealthy_application"]["verdict"], "verify_failed")
        self.assertEqual(checks["confirmed_config_permission_and_service_recovery"]["mode_after"], "0o640")
        self.assertEqual(checks["unknown_execution_blocks_replay"]["replay_exit_code"], 120)
        self.assertFalse(checks["local_timeout_stops_child_writes"]["delayed_child_write"])

    def test_http_evidence_proves_failure_and_recovery_paths(self):
        evidence = next(item["evidence"] for item in self.report["checks"]
                        if item["id"] == "loopback_server_request_evidence")
        self.assertEqual(evidence["bind_address"], "127.0.0.1")
        requests = {(row["path"], row["http_status"]) for row in evidence["requests"]}
        for path in ("/health", "/v1/chat/completions", "/hook"):
            self.assertIn((path, 200), requests)
            self.assertIn((path, 503), requests)

    def test_existing_state_and_configuration_are_untouched(self):
        self.assertEqual(sorted(path.name for path in self.existing_state.iterdir()), ["sentinel"])
        self.assertEqual((self.existing_state / "sentinel").read_text(), "do not modify")
        self.assertEqual((ROOT / "config.yaml").read_bytes(), self.original_config)
        self.assertNotIn(self.secret, self.output.read_text())
        self.assertNotIn("invalid.example.test", self.output.read_text())
        self.assertTrue(self.report["environment"]["sandbox_removed"])

    def test_export_is_private_and_stdout_roundtrips(self):
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.process.stdout), self.report)
        self.assertEqual(self.report["schema_version"], 1)
        for key in ("started_at_utc", "finished_at_utc"):
            self.assertRegex(self.report[key], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_external_work_is_blocked_and_cannot_be_counted_as_passed(self):
        self.assertEqual(self.report["overall_status"], "blocked")
        self.assertEqual({item["id"] for item in self.report["external_gates"]}, {
            "real_ssh_recovery", "real_llm_provider", "real_notification_channel",
            "seven_day_readonly_trial"})
        for item in self.report["external_gates"]:
            self.assertEqual(item["status"], "blocked")
            self.assertFalse(item["evidence"]["executed"])
        self.assertEqual(w8.exit_code(self.report), 0)
        self.assertEqual(w8.exit_code(self.report, require_external=True), 2)
        failed = dict(self.report, local_status="failed")
        self.assertEqual(w8.exit_code(failed), 1)
        self.assertEqual(w8.exit_code(failed, require_external=True), 1)

    def test_cli_require_external_returns_blocked(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--require-external"],
                                cwd=self.root, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["local_status"], "passed")
        self.assertEqual(report["overall_status"], "blocked")

    def test_export_replaces_symlink_without_overwriting_target(self):
        target = self.root / "keep.json"
        target.write_text("keep")
        link = self.root / "symlink-report.json"
        link.symlink_to(target)
        w8.write_report(link, {"schema_version": 1})
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse(link.is_symlink())
        self.assertEqual(stat.S_IMODE(link.stat().st_mode), 0o600)

    def test_crashed_worker_cannot_orphan_its_http_server(self):
        port_path = self.root / "crash-http-port"
        pid_path = self.root / "crash-http-pid"
        fixture = self.root / "crashing-worker.py"
        server_source = (
            "from http.server import HTTPServer,BaseHTTPRequestHandler\n"
            "from pathlib import Path\n"
            "server=HTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)\n"
            f"Path({str(port_path)!r}).write_text(str(server.server_port))\n"
            "server.serve_forever()\n")
        fixture.write_text(
            "import os,subprocess,sys,time\nfrom pathlib import Path\n"
            f"child=subprocess.Popen([sys.executable,'-c',{server_source!r}],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            f"Path({str(pid_path)!r}).write_text(str(child.pid))\n"
            "deadline=time.monotonic()+5\n"
            f"while not Path({str(port_path)!r}).exists():\n"
            " if time.monotonic()>deadline: raise RuntimeError('server startup')\n"
            " time.sleep(0.01)\n"
            "os._exit(7)\n")
        try:
            with patch.object(w8, "SCRIPT", fixture):
                report = w8.run_acceptance()
            self.assertEqual(report["local_status"], "failed")
            self.assertEqual(report["checks"][0]["evidence"]["exit_code"], 7)
            port = int(port_path.read_text())
            deadline = time.monotonic() + 2
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        pass
                except OSError:
                    break
                if time.monotonic() >= deadline:
                    self.fail("HTTP server survived its crashed worker")
                time.sleep(0.01)
        finally:
            if pid_path.exists():
                try:
                    os.kill(int(pid_path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_isolated_worker_disables_persistent_import_caches(self):
        env = w8._isolated_env(self.root)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")


if __name__ == "__main__":
    unittest.main()
