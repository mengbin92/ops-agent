"""Roadmap review: health evidence, target identity and paginated secret output."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import scenario
from agent import checks, tools


class ScenarioChecksReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.dirs_patch = patch.object(scenario, "_DIR_CACHE", [self.state / "scenarios",
                                                               core.REPO_DIR / "scenarios"])
        self.dirs_patch.start()
        self.addCleanup(self.dirs_patch.stop)

    def write_scenario(self, name, body):
        path = self.state / "scenarios" / (name + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")

    def test_fixed_remote_target_cannot_be_overridden_with_local(self):
        with self.assertRaises(scenario.ScenarioError):
            scenario._exec_target({"name": "fixed", "target": "prod"}, "local")

    def test_checks_evaluate_full_output_and_report_keeps_evidence(self):
        output = "x" * 1800 + "\nunhealthy\n"
        self.write_scenario("long-probe", {
            "exec": {"cmd": "echo changed"},
            "postcheck": [{"cmd": "echo probe", "expect_not_contains": "unhealthy"}]})
        original = core.run_shell

        def shell(cmd, timeout):
            if cmd == "echo probe":
                return subprocess.CompletedProcess(cmd, 0, output, "")
            return original(cmd, timeout)

        with patch.object(core, "run_shell", side_effect=shell):
            result = scenario.run("long-probe")
        self.assertEqual(result["verdict"], "verify_failed")
        self.assertIn(output.strip(), Path(result["report_path"]).read_text())

    def test_missing_postcheck_cannot_report_verified_healthy(self):
        self.write_scenario("no-check", {"exec": {"cmd": "echo changed"}})
        result = scenario.run("no-check")
        self.assertEqual(result["verdict"], "unverified")
        self.assertNotIn("后验全部健康", scenario.format_result(result))
        cli = subprocess.run([sys.executable, str(core.REPO_DIR / "opsx"),
                              "scenario", "run", "no-check"], capture_output=True,
                             text=True, env=dict(os.environ, OPSX_STATE_DIR=str(self.state)))
        self.assertEqual(cli.returncode, 7, cli.stderr)

    def test_remote_scenario_keeps_endpoint_when_registry_changes(self):
        self.write_scenario("fixed-endpoint", {"target": "prod", "exec": {"cmd": "echo changed"}})
        registry = self.state / "targets.json"
        registry.write_text(json.dumps({"prod": {"host": "original.example"}}))

        def run_scenario(*args):
            registry.write_text(json.dumps({"prod": {"host": "replacement.example"}}))
            self.assertEqual(tools.remote.get_target("prod")["host"], "original.example")
            return {"verdict": "ok"}

        with patch.object(scenario, "_run_scenario", side_effect=run_scenario):
            scenario.run("fixed-endpoint")
        self.assertEqual(tools.remote.get_target("prod")["host"], "replacement.example")

    def test_remote_rollback_verification_uses_restored_endpoint(self):
        import execution
        rec = {"id": "original", "snapshot_id": "snap", "target": "prod",
               "scenario": "systemd-restart", "scenario_params": {"service": "nginx"}}
        registry = self.state / "targets.json"
        registry.write_text(json.dumps({"prod": {"host": "original.example"}}))

        def restore(*args, **kwargs):
            registry.write_text(json.dumps({"prod": {"host": "replacement.example"}}))
            return "已恢复"

        def postcheck(*args):
            self.assertEqual(tools.remote.get_target("prod")["host"], "original.example")
            return True, []

        with patch.object(execution, "get", return_value=rec), \
                patch.object(tools.remote, "rollback", side_effect=restore), \
                patch.object(execution, "find_latest", return_value={"id": "rollback", "status": "succeeded"}), \
                patch.object(scenario, "_run_checks", side_effect=postcheck) as probe, \
                patch.object(scenario, "_attach"):
            result = scenario.rollback("original", confirm=True)
        self.assertEqual(result["rollback_status"], "succeeded")
        probe.assert_called_once()

    def test_docker_starting_is_not_a_successful_postcheck(self):
        check = scenario.load("docker-restart")["postcheck"][0]
        self.assertFalse(scenario._evaluate(check, "running/starting")[0])
        self.assertTrue(scenario._evaluate(check, "running/healthy")[0])
        self.assertTrue(scenario._evaluate(check, "running/none")[0])

    def test_failed_rollback_does_not_emit_recovery_or_run_postcheck(self):
        rec = {"id": "original", "snapshot_id": "snap", "target": None,
               "scenario": "systemd-restart", "scenario_params": {"service": "nginx"}}
        import execution
        from agent import events
        with patch.object(execution, "get", return_value=rec), \
                patch.object(core, "rollback", return_value="恢复失败"), \
                patch.object(execution, "find_latest", return_value={"id": "rollback", "status": "failed"}), \
                patch.object(scenario, "_run_checks") as run_checks, \
                patch.object(events, "append") as append:
            result = scenario.rollback("original", confirm=True)
        run_checks.assert_not_called()
        append.assert_not_called()
        self.assertIn("未确认完成", result["verify_note"])

    def test_disconnected_rollback_keeps_unknown_status_and_report(self):
        import execution
        from agent import events
        rec = {"id": "original", "snapshot_id": "snap", "target": "prod"}
        (self.state / "targets.json").write_text(json.dumps({"prod": {"host": "original.example"}}))
        with patch.object(execution, "get", return_value=rec), \
                patch.object(tools.remote, "rollback", side_effect=tools.remote._UncertainChange("断线")), \
                patch.object(execution, "find_latest", return_value={"id": "rollback", "status": "unknown"}), \
                patch.object(events, "append") as append:
            result = scenario.rollback("original", confirm=True)
        self.assertEqual(result["rollback_status"], "unknown")
        self.assertTrue(Path(result["report_path"]).is_file())
        self.assertEqual(result["verify"], [])
        append.assert_not_called()

    def test_scenario_report_redacts_parameter_and_expectation_secrets(self):
        path = scenario.save_report("secrets", "parameter password=sentinel-secret\n")
        self.assertNotIn("sentinel-secret", path.read_text())

    def test_probe_failure_redacts_before_shortening_diagnostic(self):
        output = ("-----BEGIN RSA PRIVATE KEY-----\nprivate-sentinel-material" * 4
                  + "\n-----END RSA PRIVATE KEY-----")
        for code in (0, 1):
            with patch.object(scenario, "_probe", return_value=(code, output)):
                passed, results = scenario._run_checks(
                    [{"cmd": "echo probe", "expect": "healthy"}], {}, None)
            self.assertFalse(passed)
            self.assertNotIn("private-sentinel-material", json.dumps(results))

    def test_probe_timeout_is_clipped_to_remaining_budget(self):
        deadline = time.monotonic() + 0.1
        with patch.object(core, "check", return_value=(0, "ok")), \
                patch.object(checks, "_exec", return_value=(0, "Linux")) as execute:
            checks._probe(None, "uname -s", 300, {"left": 2, "deadline": deadline})
        self.assertGreater(execute.call_args.args[2], 0)
        self.assertLessEqual(execute.call_args.args[2], 0.1)

    def test_bash_timeout_is_clipped_for_local_and_remote_calls(self):
        for target in (None, "prod"):
            ctx = tools.ToolContext(deadline=time.monotonic() + 0.1)
            with patch.object(core, "check", return_value=(0, "ok")), \
                    patch.object(core, "run_shell", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as local, \
                    patch.object(tools.remote, "run", return_value=(0, "ok")) as remote:
                tools._bash({"command": "uname -s", "target": target}, ctx)
            call = remote.call_args if target else local.call_args
            self.assertLessEqual(call.kwargs["timeout"], 0.1)

    def test_service_parsers_cannot_read_health_from_stderr(self):
        self.assertEqual(checks.parse_systemd_show("\n[stderr]\nActiveState=active")["active"], "")
        self.assertIsNone(checks.parse_docker_inspect("\n[stderr]\nrunning,healthy,0"))

    def test_invalid_docker_health_is_unknown(self):
        with patch.object(checks, "_probe", return_value=(0, "running,broken,0")):
            result = checks._docker_item({"name": "local", "target": None},
                                         {"timeout": 20},
                                         {"name": "app", "expect": "running"}, "now", {})
        self.assertEqual(result["status"], checks.UNKNOWN)

    def test_paginated_read_never_exposes_pem_body(self):
        path = self.state / "application.log"
        path.write_text("prefix\n-----BEGIN RSA PRIVATE KEY-----\n"
                        "private-sentinel-material\n-----END RSA PRIVATE KEY-----\nsuffix\n")
        for offset, limit in ((1, 3), (3, 1), (2, 1)):
            result = tools.dispatch("read", {"path": str(path), "offset": offset,
                                             "limit": limit}, tools.ToolContext())
            self.assertNotIn("private-sentinel-material", result)
            self.assertIn(core.REDACTED, result)
        self.assertEqual(tools.dispatch("read", {"path": str(path), "offset": 5,
                                                  "limit": 1}, tools.ToolContext()), "suffix")


if __name__ == "__main__":
    unittest.main()
