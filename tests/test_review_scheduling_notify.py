"""调度、通知与证据恢复的离线回归；不连接模型、主机或 webhook。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core
from agent import checks, events, notify, schedule, session


class SchedulingNotifyReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.state_patch = patch.object(core, "STATE_DIR", self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def entry(self, **overrides):
        return dict(id="sch-review", prompt="检查本机", every_seconds=60,
                    enabled=True, next_run=0, **overrides)

    def signal(self, ts, anomalous=False):
        return {
            "version": 1, "created_ts": ts, "complete": True, "truncated": False,
            "verdict": "异常" if anomalous else "正常",
            "targets": ["local"], "groups": list(checks.GROUPS),
            "target_groups": {"local": list(checks.GROUPS)},
            "target_complete": {"local": True},
            "scope_observed_ts": {f"local/{group}": ts for group in checks.GROUPS},
            "check_statuses": {"local/resources/disk": "critical" if anomalous else "ok"},
            "report_paths": [],
            "anomalies": [{"id": "local/resources/disk", "target": "local",
                           "name": "disk", "status": "critical", "line": "磁盘 99%"}]
            if anomalous else [],
        }

    def rec(self, name, started, finished, anomalous=False):
        return {"task_id": name, "run_id": name, "status": "succeeded",
                "started_ts": started, "finished_ts": finished,
                "verdict": "异常" if anomalous else "正常"}

    def test_prepare_error_finishes_original_run_instead_of_leaving_running(self):
        with patch.object(session, "Session", side_effect=ValueError("损坏会话")):
            rec = schedule._execute_run(self.entry(), None)
        saved = schedule.list_runs()
        self.assertEqual(len(saved), 1)
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(saved[0]["run_id"], rec["run_id"])
        self.assertEqual(saved[0]["status"], "failed")
        self.assertIn("损坏会话", saved[0]["error"])

    def test_unexpected_signal_error_preserves_original_run_and_session(self):
        with patch.object(schedule, "_run_entry", return_value="结论：正常"), \
                patch.object(schedule, "_load_run_signal", side_effect=ValueError("坏摘要")):
            rec = schedule._execute_run(self.entry(), None)
        saved = schedule.list_runs()
        self.assertEqual(len(saved), 1)
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(rec["attempts"], 1)
        self.assertTrue(saved[0]["session_id"])
        self.assertIn("坏摘要", rec["error"])

    def test_missing_full_report_cannot_be_reported_as_success(self):
        report = {"verdict": "正常", "summary": {
            "critical": 0, "warn": 0, "unknown": 0, "error": 0}}
        with patch.object(checks, "run_checks", return_value=report), \
                patch.object(checks, "format_report", return_value="完整原始证据"), \
                patch.object(checks, "save_report", return_value=None), \
                patch.object(checks, "save_signal", return_value=self.state / "signal.json"), \
                patch.object(schedule, "_run_entry", return_value="结论：正常") as run:
            rec = schedule._execute_run(self.entry(checks={"targets": "local"}), None)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("完整报告写入失败", rec["error"])
        self.assertIn("完整原始证据", run.call_args.kwargs["extra"])

    def test_slow_model_old_anomaly_cannot_reopen_newer_recovery(self):
        notify.save_config({"url": "https://hooks.example.test/token", "format": "generic"})
        with patch.object(notify.time, "time", return_value=400), \
                patch.object(notify, "_post", return_value=(200, "ok")) as post:
            healthy = self.signal(200)
            notify.process_run(self.rec("fast", 190, 210), healthy)
            outcome = notify.process_run(self.rec("slow", 90, 300, True), self.signal(100, True))
        self.assertIsNone(outcome["notification"])
        self.assertEqual(post.call_count, 0)
        self.assertNotIn("check:local/resources/disk", notify._load_state()["items"])

    def test_slow_model_old_healthy_sample_cannot_clear_newer_anomaly(self):
        notify.save_config({"url": "https://hooks.example.test/token", "format": "generic"})
        with patch.object(notify.time, "time", return_value=400), \
                patch.object(notify, "_post", return_value=(200, "ok")) as post:
            notify.process_run(self.rec("fast", 190, 210, True), self.signal(200, True))
            outcome = notify.process_run(self.rec("slow", 90, 300), self.signal(100))
        self.assertIsNone(outcome["notification"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(notify._load_state()["items"]["check:local/resources/disk"]["severity"],
                         "critical")

    def test_merged_other_group_or_target_does_not_refresh_old_observation(self):
        for target in ("local", "other"):
            for legacy in (False, True):
                with self.subTest(target=target, legacy=legacy):
                    previous = self.signal(100, True)
                    if legacy:
                        previous.pop("scope_observed_ts")
                    current = self.signal(300)
                    current.update(targets=[target], groups=["services"],
                                   target_groups={target: ["services"]},
                                   target_complete={target: False}, complete=False,
                                   scope_observed_ts={f"{target}/services": 300},
                                   check_statuses={f"{target}/services/app": "ok"})
                    combined = checks._merge_signal(previous, current)
                    self.assertEqual(combined["scope_observed_ts"]["local/resources"], 100)
                    self.assertEqual(combined["scope_observed_ts"][f"{target}/services"], 300)
                    state = {"observations": {"check:local/resources": 200}}
                    self.assertTrue(notify._older_observation(
                        state, "check:local/resources/disk", self.rec("slow", 90, 400),
                        400, combined))

    def test_signal_scope_timestamps_are_saved_and_legacy_signals_load(self):
        report = {"target_names": ["local"], "groups": ["resources"], "targets": [],
                  "anomalies": [], "complete": False, "verdict": "不完整"}
        with patch.object(checks.time, "time", return_value=100):
            payload = checks._signal_payload(report, None)
        self.assertEqual(payload["scope_observed_ts"], {"local/resources": 100})
        for legacy in (False, True):
            signal = self.signal(100)
            if legacy:
                signal.pop("scope_observed_ts")
            checks._save_signal_payload("timestamp-test", signal)
            self.assertIsNotNone(checks.load_signal("timestamp-test"))
        signal["scope_observed_ts"] = {"local/resources": float("nan")}
        checks._save_signal_payload("timestamp-test", signal)
        self.assertIsNone(checks.load_signal("timestamp-test"))

    def test_failed_recovery_uses_outbox_backoff_and_attempt_limit(self):
        notify.save_config({"url": "https://hooks.example.test/token", "format": "generic"})
        with patch.object(notify.time, "time", return_value=100), \
                patch.object(notify, "_post", return_value=(200, "ok")):
            notify.process_run(self.rec("task", 90, 100, True), self.signal(100, True))
        healthy = self.rec("task", 190, 200)
        with patch.object(notify.time, "time", return_value=200), \
                patch.object(notify, "_post", side_effect=notify.NotifyError("offline")) as post:
            failed = notify.process_run(healthy, self.signal(200))["notification"]
            for _ in range(10):
                self.assertIsNone(notify.process_run(healthy, self.signal(200))["notification"])
            self.assertEqual(notify.retry_pending(), [])
            self.assertEqual(post.call_count, 1)
        self.assertEqual(len(notify.list_notifications(tail=100)), 2)
        for attempt in range(2, notify.MAX_ATTEMPTS + 1):
            with patch.object(notify.time, "time", return_value=failed["next_attempt_ts"]), \
                    patch.object(notify, "_post", side_effect=notify.NotifyError("offline")):
                failed = notify.retry_pending()[0]
                self.assertEqual(failed["attempts"], attempt)
                self.assertIsNone(notify.process_run(healthy, self.signal(200))["notification"])
        with patch.object(notify.time, "time", return_value=failed["next_attempt_ts"] + 10000), \
                patch.object(notify, "_post") as post:
            self.assertEqual(notify.retry_pending(), [])
            self.assertIsNone(notify.process_run(healthy, self.signal(200))["notification"])
            post.assert_not_called()
        self.assertTrue(notify._load_state()["items"]["check:local/resources/disk"]
                        ["pending_recovery"])

    def test_session_load_redacts_legacy_secret_before_reusing_messages(self):
        secret = "sk-LEGACYSECRETSENTINEL0123456789"
        path = self.state / "sessions" / "legacy.jsonl"
        path.parent.mkdir()
        records = [{"format": 1, "id": "legacy"},
                   {"role": "tool", "content": f"api_key={secret}"}]
        path.write_text("".join(json.dumps(rec) + "\n" for rec in records))
        loaded = session.Session("legacy")
        self.assertNotIn(secret, json.dumps(loaded.messages))
        self.assertIn(core.REDACTED, loaded.messages[0]["content"])

    def test_event_after_torn_tail_is_visible_and_existing_permissions_tightened(self):
        path = self.state / "events.jsonl"
        path.write_bytes(b'{"kind":"anomaly-new","title":"torn')
        os.chmod(path, 0o644)
        self.assertTrue(events.append("anomaly-recovered", title="可查询的新事件"))
        self.assertEqual(events.tail(1)[0]["title"], "可查询的新事件")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
