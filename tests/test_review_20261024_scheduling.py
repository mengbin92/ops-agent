"""本轮调度、巡检、体检回归；状态与执行均在临时目录/离线桩中。"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import core
import doctor
from agent import checks, events, llm, schedule, session, tools


class SchedulingReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="ops-review-20261024-")
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        state_patch = patch.object(core, "STATE_DIR", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def entry(self):
        return {"id": "sch-review", "prompt": "只读巡检", "enabled": True,
                "every_seconds": 60, "next_run": 0, "timeout_seconds": 60}

    def test_ambiguous_or_qualified_verdict_is_not_a_final_verdict(self):
        for text in ("结论：正常/异常，尚未判定", "结论：正常 (未检查服务)",
                     "结论：正常？", "结论：正常，仍需核实"):
            with self.subTest(text=text):
                self.assertEqual(schedule._detect_verdict(text), "未判定")

    def test_last_unquoted_verdict_wins_across_report_and_standalone_forms(self):
        self.assertEqual(schedule._detect_verdict(
            "结论：正常\n- 结论: **异常**｜完整性: 完整"), "异常")
        self.assertEqual(schedule._detect_verdict(
            "- 结论: **异常**｜完整性: 完整\n结论：正常"), "正常")

    def test_quoted_old_report_cannot_override_live_verdict(self):
        quoted = ("~~~text\n结论：正常\n~~~",
                  "````markdown\n```\n结论：正常\n```\n````",
                  "````markdown\n```\n结论：正常",
                  "    结论：正常", "\t结论：正常")
        for text in quoted:
            with self.subTest(text=text):
                self.assertEqual(schedule._detect_verdict("结论：异常\n" + text), "异常")

    def test_text_after_matching_fence_is_parsed(self):
        self.assertEqual(schedule._detect_verdict(
            "~~~\n结论：正常\n~~~~\n**结论：异常**"), "异常")

    def test_uname_stderr_cannot_supply_a_successful_platform(self):
        with patch.object(checks, "_probe", return_value=(0, "\n[stderr]\nLinux")):
            result, platform = checks._connectivity({"name": "local", "target": None}, 20, {})
        self.assertEqual(result["status"], checks.ERROR)
        self.assertEqual(platform, "")
        self.assertIn("无输出", result["detail"])

    def test_uname_stdout_still_supplies_platform_with_stderr(self):
        with patch.object(checks, "_probe", return_value=(0, "Linux\n\n[stderr]\nwarning")):
            result, platform = checks._connectivity({"name": "local", "target": None}, 20, {})
        self.assertEqual((result["status"], platform), (checks.OK, "Linux"))

    def test_retry_sleep_is_clipped_and_no_attempt_starts_after_deadline(self):
        clock = [0.0]

        def fail(*args, **kwargs):
            clock[0] = 59.5
            raise OSError("temporary failure")

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(schedule.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(schedule.time, "sleep", side_effect=sleep) as sleeper, \
                patch.object(schedule, "_run_entry", side_effect=fail) as run:
            result = schedule._execute_run(self.entry(), None)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(run.call_count, 1)
        sleeper.assert_called_once_with(0.5)
        self.assertEqual(schedule.list_runs()[0]["status"], "timeout")

    def test_failed_last_attempt_at_deadline_is_timeout(self):
        clock = [0.0]

        def fail(*args, **kwargs):
            clock[0] = 60.0
            raise OSError("request timed out")

        with patch.object(schedule.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(schedule, "RUN_RETRIES", 0), \
                patch.object(schedule, "_run_entry", side_effect=fail), \
                patch.object(schedule.time, "sleep") as sleeper:
            result = schedule._execute_run(self.entry(), None)
        self.assertEqual(result["status"], "timeout")
        sleeper.assert_not_called()

    def test_doctor_checks_registered_underscore_targets_but_skips_local_config(self):
        targets = {"_local": {"enabled": True}, "_production": {"host": "example.invalid"}}
        response = subprocess.CompletedProcess([], 1, b"", b"unreachable")
        with patch.object(doctor.remote, "load_targets", return_value=targets), \
                patch.object(doctor.remote, "_ssh", return_value=response) as ssh:
            results = doctor._check_ssh_targets()
        ssh.assert_called_once_with("_production", "true", timeout=doctor.SSH_PROBE_TIMEOUT)
        self.assertEqual(results[0]["name"], "ssh:_production")
        self.assertEqual(results[0]["status"], doctor.FAIL)

    def test_legacy_event_reads_redact_secrets_without_changing_evidence(self):
        secret = "sk-LEGACYEVENTSENTINEL0123456789"
        raw = json.dumps({"kind": "run-failed", "error": "api_key=" + secret,
                          "nested": {"password": "legacy-event-password"}}) + "\n"
        for filename in ("events.jsonl", "events.jsonl.1"):
            (self.state / filename).write_text(raw)
        encoded = json.dumps(events.tail(10))
        self.assertNotIn(secret, encoded)
        self.assertNotIn("legacy-event-password", encoded)
        self.assertNotIn(secret, events.format_tail(10))
        self.assertIn(core.REDACTED, events.format_tail(10))
        self.assertEqual((self.state / "events.jsonl").read_text(), raw)

    def test_deeply_nested_broken_event_is_skipped(self):
        path = self.state / "events.jsonl"
        path.write_text('{"kind":"run-failed","data":' + '[' * 2000 + '0' + ']' * 2000
                        + '}\n{"kind":"run-failed","error":"visible"}\n')
        self.assertEqual(len(events.tail(10)), 1)
        self.assertEqual(events.tail(1)[0]["error"], "visible")

    def _assert_rule_evidence_failure_downgrades_model(self, tool_name, collection_error=False,
                                                     report_error=False):
        check = checks._item("local", "resources", "disk", checks.CRITICAL,
                             display="99%", detail="磁盘已满")
        report = {
            "report_id": "review", "generated_at": "2026-10-24T12:00:00+08:00",
            "duration_ms": 0, "groups": list(checks.GROUPS), "complete": True,
            "coverage": {"groups_requested": list(checks.GROUPS),
                         "groups_skipped": [], "targets_skipped": []},
            "target_names": ["local"], "verdict": "异常",
            "summary": {"targets": 1, "checks": 1, "ok": 0, "warn": 0,
                        "critical": 1, "unknown": 0, "error": 0},
            "targets": [{"name": "local", "label": "本机", "verdict": "异常",
                         "checks": [check]}],
            "anomalies": [{"id": check["id"], "target": "local", "name": "disk",
                           "status": checks.CRITICAL, "line": "磁盘已满"}],
        }
        if report_error:
            check.update(status=checks.OK, display="30%", detail="磁盘空间充足")
            report.update(verdict="正常", anomalies=[])
            report["targets"][0]["verdict"] = "正常"
            report["summary"].update(ok=1, critical=0)
        wf = {"name": "review", "checks": {"targets": "all", "groups": list(checks.GROUPS)},
              "steps": [{"name": "解读", "prompt": "解读规则结果"}]}
        arguments = {} if tool_name == "inspect" else {"name": "review"}
        script = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "rule-check", "type": "function", "function": {
                    "name": tool_name, "arguments": json.dumps(arguments)}}]},
            {"role": "assistant", "content": "结论：正常"},
        ]
        probe_patch = (patch.object(checks, "run_checks", side_effect=checks.CheckError("无法采集"))
                       if collection_error else patch.object(checks, "run_checks", return_value=report))
        signal_writer = checks.save_signal
        report_writer = checks.save_report
        signal_patch = (patch.object(checks, "save_signal", wraps=signal_writer)
                        if report_error else patch.object(checks, "save_signal", return_value=None))
        report_patch = (patch.object(checks, "save_report", return_value=None)
                        if report_error else patch.object(checks, "save_report", wraps=report_writer))
        with probe_patch, signal_patch, report_patch, \
                patch.object(tools.workflow, "load", return_value=wf), \
                patch.object(tools, "_run_isolated", return_value="结论：正常"), \
                patch.object(llm, "chat", side_effect=script):
            result = schedule._execute_run(self.entry(), None)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["verdict"], "不完整")
        self.assertEqual(schedule.runs_exit_code([result]), 3)
        self.assertIn("规则证据不完整", result["result_preview"])
        tool_results = [message["content"] for message in session.Session(result["session_id"]).messages
                        if message.get("role") == "tool"]
        self.assertTrue(any("未执行" in text or "无法保存检查证据" in text
                            or "检查错误" in text or "完整报告写入失败" in text for text in tool_results))
        if report_error:
            self.assertIsNotNone(checks.load_signal(result["session_id"]))

    def test_inspect_signal_write_failure_downgrades_healthy_model_final(self):
        self._assert_rule_evidence_failure_downgrades_model("inspect")

    def test_workflow_signal_write_failure_downgrades_healthy_model_final(self):
        self._assert_rule_evidence_failure_downgrades_model("workflow")

    def test_inspect_collection_failure_downgrades_healthy_model_final(self):
        self._assert_rule_evidence_failure_downgrades_model("inspect", collection_error=True)

    def test_workflow_collection_failure_downgrades_healthy_model_final(self):
        self._assert_rule_evidence_failure_downgrades_model("workflow", collection_error=True)

    def test_inspect_report_write_failure_with_signal_saved_downgrades_model_final(self):
        self._assert_rule_evidence_failure_downgrades_model("inspect", report_error=True)

    def test_workflow_report_write_failure_with_signal_saved_downgrades_model_final(self):
        self._assert_rule_evidence_failure_downgrades_model("workflow", report_error=True)


if __name__ == "__main__":
    unittest.main()
