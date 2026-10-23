"""W8 review regressions: evidence contracts, export safety and helper isolation."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "w8_acceptance.py"
spec = importlib.util.spec_from_file_location("w8_review_acceptance", SCRIPT)
w8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w8)


class W8ReviewDrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="opsx-w8-review-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def valid_checks(self):
        return [{"id": name, "status": "passed", "evidence": {"observed": True}}
                for name in w8.CHECK_IDS]

    def test_worker_contract_rejects_incomplete_duplicate_and_malformed_checks(self):
        valid = self.valid_checks()
        cases = [None, True, "passed", {}, [], valid[:-1], valid + [valid[0]],
                 [dict(valid[0], id="invented")] + valid[1:],
                 [valid[0]] * len(valid),
                 [dict(valid[0], evidence={})] + valid[1:],
                 [dict(valid[0], evidence=[])] + valid[1:],
                 [dict(valid[0], status=True)] + valid[1:],
                 [dict(valid[0], status="unknown")] + valid[1:],
                 [dict(valid[0], extra="unvalidated")] + valid[1:],
                 ["passed"] + valid[1:]]
        for checks in cases:
            with self.subTest(checks=checks):
                with self.assertRaises(ValueError):
                    w8._validate_worker_report({"checks": checks, "source_sha256": {}}, {})
        self.assertEqual(w8._validate_worker_report({"checks": valid, "source_sha256": {}}, {}), valid)

    def test_source_manifest_is_required_and_binds_policy_and_implementation(self):
        manifest = w8._source_manifest()
        for name in ("drill", "core.py", "execution.py", "scenario.py", "usage.py",
                     "remote.py", "config.yaml", "agent/llm.py", "agent/notify.py"):
            self.assertRegex(manifest[name], r"^[0-9a-f]{64}$")
        for source in (None, {}, dict(manifest, **{"core.py": "0" * 64})):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    w8._validate_worker_report({"checks": self.valid_checks(), "source_sha256": source}, manifest)

    def test_exit_code_cannot_turn_missing_local_or_external_checks_into_success(self):
        report = {"local_status": "passed", "checks": self.valid_checks(),
                  "external_gates": w8.external_gates()}
        self.assertEqual(w8.exit_code(report), 0)
        self.assertEqual(w8.exit_code(report, True), 2)
        self.assertEqual(w8.exit_code(dict(report, checks=[])), 1)
        self.assertEqual(w8.exit_code(dict(report, checks=True)), 1)
        self.assertEqual(w8.exit_code(dict(report, checks=[dict(self.valid_checks()[0], status="failed")]
                                                      + self.valid_checks()[1:])), 1)
        for gates in ([], None, [{"id": "invented", "status": "passed"}] * 4):
            self.assertEqual(w8.exit_code(dict(report, external_gates=gates), True), 2)
        passed_gates = deepcopy(report["external_gates"])
        for gate in passed_gates:
            gate.update(status="passed", evidence={"executed": True})
        self.assertEqual(w8.exit_code(dict(report, external_gates=passed_gates), True), 0)
        passed_gates[0]["evidence"]["executed"] = False
        self.assertEqual(w8.exit_code(dict(report, external_gates=passed_gates), True), 2)

    def test_malformed_worker_output_is_failed_without_exporting_its_content(self):
        secret = "private-raw-worker-secret"
        fixture = self.root / "malformed.py"
        fixture.write_text(f"print({secret!r})\n", encoding="utf-8")
        with patch.object(w8, "SCRIPT", fixture):
            report = w8.run_acceptance()
        self.assertEqual(report["local_status"], "failed")
        self.assertEqual(report["checks"][0]["id"], "isolated_worker")
        self.assertNotIn(secret, json.dumps(report))

    def test_worker_output_is_bounded_and_timeout_cannot_pass(self):
        fixture = self.root / "output.py"
        checks = self.valid_checks()
        checks[0]["evidence"]["padding"] = "x" * 2048
        source = {"drill": "1" * 64}
        fixture.write_text("import json\nprint(json.dumps(" + repr({
            "checks": checks, "source_sha256": source}) + "))\n", encoding="utf-8")
        with patch.object(w8, "SCRIPT", fixture), patch.object(w8, "_source_manifest", return_value=source):
            self.assertEqual(w8.run_acceptance()["local_status"], "passed")
            with patch.object(w8, "MAX_WORKER_BYTES", 1024):
                limited = w8.run_acceptance()
                self.assertEqual(limited["local_status"], "failed")
                self.assertTrue(limited["checks"][0]["evidence"]["output_limit_exceeded"])
        fixture.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        with patch.object(w8, "SCRIPT", fixture), patch.object(w8, "WORKER_TIMEOUT", 0.05):
            report = w8.run_acceptance()
        self.assertEqual(report["local_status"], "failed")
        self.assertTrue(report["checks"][0]["evidence"]["timed_out"])

    def test_worker_source_change_cannot_receive_a_passing_report(self):
        fixture = self.root / "report.py"
        source = {"drill": "1" * 64}
        fixture.write_text("import json\nprint(json.dumps(" + repr({
            "checks": self.valid_checks(), "source_sha256": source}) + "))\n", encoding="utf-8")
        with patch.object(w8, "SCRIPT", fixture), patch.object(w8, "_source_manifest", side_effect=[
                source, {"drill": "2" * 64}]):
            report = w8.run_acceptance()
        self.assertEqual(report["local_status"], "failed")
        self.assertEqual(report["environment"]["drill_source_sha256"], "1" * 64)

    def test_timeout_runs_worker_cleanup_for_a_separate_child_session(self):
        fixture = self.root / "graceful.py"
        marker = self.root / "late-write"
        pidfile = self.root / "child-pid"
        child = "import time;from pathlib import Path;time.sleep(1);Path(" + repr(str(marker)) + ").write_text('late')"
        fixture.write_text(
            "import os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
            "def stop(*_args): raise SystemExit(143)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            f"child=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True)\n"
            f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
            "try:\n time.sleep(30)\n"
            "finally:\n os.killpg(child.pid,signal.SIGKILL)\n child.wait()\n", encoding="utf-8")
        try:
            with patch.object(w8, "SCRIPT", fixture), patch.object(w8, "WORKER_TIMEOUT", 0.3):
                report = w8.run_acceptance()
            self.assertEqual(report["local_status"], "failed")
            self.assertTrue(pidfile.exists(), "worker did not reach its child launch")
            time.sleep(1)
            self.assertFalse(marker.exists(), "child outlived worker timeout")
        finally:
            if pidfile.exists():
                try:
                    os.killpg(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_cli_sigterm_cleans_worker_server_and_temporary_state(self):
        env = dict(os.environ, TMPDIR=str(self.root))
        with subprocess.Popen([sys.executable, "-I", "-B", str(SCRIPT)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as parent:
            try:
                deadline = time.monotonic() + 5
                portfile = None
                while time.monotonic() < deadline:
                    ports = list(self.root.glob("opsx-w8-*/http-port"))
                    if ports:
                        portfile = ports[0]
                        break
                    if parent.poll() is not None:
                        self.fail("drill exited before its HTTP server started")
                    time.sleep(0.01)
                self.assertIsNotNone(portfile, "HTTP server did not start")
                port = int(portfile.read_text())
                parent.terminate()
                parent.communicate(timeout=10)
                self.assertEqual(parent.returncode, 143)
                self.assertFalse(portfile.parent.exists(), "parent SIGTERM leaked temporary state")
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", port), timeout=0.2)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.communicate(timeout=5)

    def test_inherited_shell_python_loader_and_provider_environment_is_removed(self):
        inherited = {name: "unsafe-value" for name in (
            "BASH_ENV", "ENV", "ZDOTDIR", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES", "AWS_SECRET_ACCESS_KEY", "OPSAGENT_BASE_URL",
            "OPSX_W8_SESSION", "HTTPS_PROXY", "http_proxy", "PATH", "HOME", "TMPDIR")}
        with patch.dict(os.environ, inherited):
            env = w8._isolated_env(self.root)
        for name in inherited:
            self.assertNotEqual(env.get(name), "unsafe-value", name)
        self.assertEqual(env["HOME"], str(self.root / "home"))
        self.assertEqual(env["TMPDIR"], str(self.root))
        self.assertEqual(env["PATH"], os.defpath)
        self.assertEqual(env["OPSX_STATE_DIR"], str(self.root / "state"))

    def test_real_drill_ignores_inherited_import_hooks_and_path_shims(self):
        marker = self.root / "escaped-write"
        hook_dir = self.root / "hooks"
        hook_dir.mkdir()
        (hook_dir / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('import hook')\n")
        executable_dir = self.root / "bin"
        executable_dir.mkdir()
        shim = executable_dir / "sleep"
        shim.write_text("#!/bin/sh\nprintf escaped > " + str(marker) + "\nexec /bin/sleep \"$@\"\n")
        shim.chmod(0o700)
        env = dict(os.environ, PYTHONPATH=str(hook_dir), PATH=str(executable_dir))
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT)],
                                cwd=self.root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["local_status"], "passed")
        self.assertFalse(marker.exists(), "an inherited import hook or executable wrote outside the sandbox")

    def test_hidden_helpers_cannot_operate_on_arbitrary_directory(self):
        state = self.root / "real-state"
        state.mkdir(mode=0o700)
        sentinel = state / "sentinel"
        sentinel.write_text("keep")
        env = dict(os.environ, OPSX_STATE_DIR=str(state / "state"))
        env.pop("OPSX_W8_SESSION", None)
        env.pop("OPSX_W8_SANDBOX", None)
        for args in (("--_worker", str(state)), ("--_server", str(state)),
                     ("--_probe", "http://127.0.0.1:1/health")):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *args],
                                        env=env, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(sorted(path.name for path in state.iterdir()), ["sentinel"])

    def test_export_rejects_source_and_state_including_parent_symlink(self):
        repo = self.root / "repo"
        repo.mkdir()
        source = repo / "config.yaml"
        source.write_text("keep-policy")
        state = self.root / "state"
        state.mkdir()
        (state / "notify.json").write_text("keep-state")
        alias = self.root / "state-alias"
        alias.symlink_to(state, target_is_directory=True)
        with patch.object(w8, "REPO", repo), patch.dict(os.environ, {"OPSX_STATE_DIR": str(state)}):
            for output in (source, state / "notify.json", alias / "notify.json"):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    w8.write_report(output, {"schema_version": 1})
            w8.write_report(repo / "docs" / "validation" / "evidence.json", {"schema_version": 1})
        self.assertEqual(source.read_text(), "keep-policy")
        self.assertEqual((state / "notify.json").read_text(), "keep-state")

    def test_probe_rejects_redirect_without_contacting_redirect_destination(self):
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/escaped")
                self.end_headers()

        class Destination(BaseHTTPRequestHandler):
            hits = 0

            def log_message(self, *_args):
                pass

            def do_GET(self):
                Destination.hits += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"healthy":true}')

        target = w8._LoopbackHTTPServer(("127.0.0.1", 0), Destination)
        source = w8._LoopbackHTTPServer(("127.0.0.1", 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (source, target)]
        for thread in threads:
            thread.start()
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(w8._probe(f"http://127.0.0.1:{source.server_port}/health"), 1)
            self.assertEqual(Destination.hits, 0)
        finally:
            for server in (source, target):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)

    def test_probe_url_cannot_smuggle_credentials_query_or_fragment(self):
        for url in ("http://a:b@127.0.0.1:1/health", "http://127.0.0.1:1/health?forward=1",
                    "http://127.0.0.1:1/health#part", "http://127.0.0.1/health"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                w8._probe(url)


if __name__ == "__main__":
    unittest.main()
