"""调度与场景的离线回归。"""
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import core
import execution
import scenario
from agent import goal, notify, schedule, session, tools, workflow


class ScheduleAuditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        state = patch.object(core, "STATE_DIR", self.state)
        state.start()
        self.addCleanup(state.stop)

    def test_group_only_check_sends_recovery_on_positive_evidence(self):
        check_id = "local/resources/disk"
        notify.save_config({"url": "https://webhook.example.test/alerts"})
        run = {"task_id": "sch-audit", "status": "succeeded", "verdict": "不完整"}
        signal = {"targets": ["local"], "groups": ["resources"],
                  "target_groups": {"local": ["resources"]},
                  "target_complete": {"local": False}, "complete": False}
        anomaly = dict(signal, anomalies=[{"id": check_id, "target": "local",
                                           "status": "critical", "name": "磁盘"}],
                       check_statuses={check_id: "critical"})
        healthy = dict(signal, check_statuses={check_id: "ok"})
        with patch.object(notify, "_post", return_value=(200, "")) as post:
            notify.process_run(run, anomaly)
            result = notify.process_run(run, healthy)
        self.assertEqual(post.call_count, 2)
        self.assertEqual([item["fingerprint"] for item in result["notification"]["recoveries"]],
                         [f"check:{check_id}"])

    def test_email_mixed_alert_filters_recovery_body_by_actual_recipient(self):
        delivered, other = "primary@example.test", "other@example.test"
        cfg = notify._validate_config({"channel": "email", "smtp_host": "smtp.example.test",
                                      "smtp_port": 465, "mail_from": "ops@example.test",
                                      "mail_to": [delivered, other]})
        server = MagicMock()
        server.__enter__.return_value = server
        server.send_message.return_value = {}
        alert = {"fingerprint": "run:new", "title": "新的巡检失败", "severity": "critical"}
        recovery = {"fingerprint": "run:old", "title": "旧的巡检已恢复",
                    "recipient_ids": [notify._recipient_id(delivered)]}
        payload = notify._compose([alert], [recovery], {}, 100)
        with patch.object(notify.smtplib, "SMTP_SSL", return_value=server):
            notify._send_email(cfg, payload)
        contents = {addr: args.args[0].get_content() for args in server.send_message.call_args_list
                    for addr in args.kwargs["to_addrs"]}
        self.assertIn(recovery["title"], contents[delivered])
        self.assertNotIn(recovery["title"], contents[other])
        self.assertIn(alert["title"], contents[other])

    def test_scenario_substitution_preserves_literal_values_and_templates(self):
        rendered = scenario.substitute("{source} {name} {{name}}",
                                       {"source": "{name}", "name": "value"})
        self.assertEqual(rendered, "{name} value {{name}}")

    def test_agent_scenario_uses_definition_that_was_confirmed(self):
        original = {"name": "fixture", "target": None, "description": "fixture", "params": {},
                    "exec": {"cmd": "echo first", "snapshot_files": [], "snapshot_cmds": []},
                    "precheck": [], "postcheck": [{"cmd": "echo original"}]}
        replacement = dict(original, postcheck=[{"cmd": "echo replacement"}])
        ctx = tools.ToolContext(ask_user=lambda _question: "yes")
        with patch.object(scenario, "load", side_effect=[original, replacement]), \
                patch.object(scenario, "_run_scenario", return_value={}) as run, \
                patch.object(scenario, "format_result", return_value="fixture"):
            tools.dispatch("scenario_run", {"name": "fixture"}, ctx)
        self.assertEqual(run.call_args.args[0]["postcheck"], original["postcheck"])

    def test_scenario_approval_binds_the_rendered_snapshot_plan(self):
        definition = {"name": "fixture", "target": None, "description": "fixture",
                      "params": {"name": {"default": "sample"}},
                      "exec": {"cmd": "echo fixture", "snapshot_files": ["config/{name}"],
                               "snapshot_cmds": [{"command": "echo {name}", "name": "state"}]},
                      "precheck": [], "postcheck": []}
        ctx = tools.ToolContext(ask_user=lambda _question: "yes")
        with patch.object(scenario, "load", return_value=definition), \
                patch.object(scenario, "_run_scenario", return_value={}), \
                patch.object(scenario, "format_result", return_value="fixture"), \
                patch.object(core, "classify", return_value=("R1", None)), \
                patch.object(core, "approve") as approve:
            tools.dispatch("scenario_run", {"name": "fixture"}, ctx)
        self.assertEqual(approve.call_args.kwargs["snapshot_files"], ["config/sample"])
        self.assertEqual(approve.call_args.kwargs["snapshot_cmds"], [("echo sample", "state")])

    def test_newer_other_thread_record_does_not_intercept_current_execution(self):
        cmd = "echo fixture"
        started = time.time()
        first = execution.begin("exec", cmd, phase="lock")
        with ThreadPoolExecutor(max_workers=1) as executor:
            other = executor.submit(execution.begin, "exec", cmd, phase="lock").result()
        self.assertNotEqual(first["id"], other["id"])
        record = execution.find_latest(cmd, since=started, pid=os.getpid(),
                                       thread_id=threading.get_ident(), kind="exec")
        self.assertEqual(record["id"], first["id"])

    def test_successful_scenario_rollback_reports_unusable_updated_postcheck(self):
        definition = {"name": "fixture", "params": {"optional": {"required": False}},
                      "postcheck": [{"cmd": "echo {optional}"}]}
        previous = {"id": "original", "scenario": "fixture", "scenario_params": {},
                    "snapshot_id": "snapshot"}
        with patch.object(execution, "get", return_value=previous), \
                patch.object(execution, "find_latest", return_value={"id": "rolled",
                                                                     "status": "succeeded"}), \
                patch.object(scenario, "load", return_value=definition), \
                patch.object(core, "rollback", return_value="已恢复"):
            result = scenario.rollback("original", confirm=True)
        self.assertEqual(result["rollback_status"], "succeeded")
        self.assertFalse(result["verify"])
        self.assertTrue(result["report_path"])

    def test_reconciliation_does_not_refresh_scheduler_heartbeat(self):
        schedule._save([], {"last_scan": 600})
        entry = {"id": "sch-audit", "prompt": "fixture"}
        schedule._begin_run(entry, None)
        with patch.object(schedule, "_pid_alive", return_value=False):
            self.assertEqual(schedule.reconcile_runs(), 1)
        self.assertEqual(schedule._load_state()[1]["last_scan"], 600)

    def test_corrupt_schedule_interval_and_heartbeat_do_not_crash_scheduler(self):
        entry = {"id": "sch-audit", "prompt": "fixture",
                 "every_seconds": 10 ** 400, "next_run": time.time() - 1}
        schedule._save([entry], {"last_scan": 1e200})
        self.assertEqual(schedule.daemon_health()["state"], "never")
        with patch.object(schedule, "_run_entry") as run:
            records = schedule.run_due()
        run.assert_not_called()
        self.assertEqual(records[0]["status"], "failed")
        self.assertFalse(schedule.list_entries()[0]["enabled"])

    def test_workflow_catalog_excludes_invalid_user_override(self):
        builtin, user = self.state / "builtin", self.state / "user"
        builtin.mkdir()
        user.mkdir()
        (builtin / "fixture.json").write_text('{"steps":[{"prompt":"fixture"}]}')
        (user / "fixture.json").write_text('{"description":"missing steps"}')
        with patch.object(workflow, "DIRS", [builtin, user]):
            self.assertEqual(workflow.catalog(), [])
            with self.assertRaises(workflow.WorkflowError):
                workflow.load("fixture")

    def test_interruption_after_schedule_advance_has_a_run_to_reconcile(self):
        entry = schedule.add("fixture", every_seconds=60)
        items = schedule.list_entries()
        items[0]["next_run"] = 1
        schedule._save(items, {})
        write = schedule._save

        def interrupt_after_advance(items, meta):
            write(items, meta)
            if items[0]["next_run"] != 1:
                raise KeyboardInterrupt

        with patch.object(schedule, "_save", side_effect=interrupt_after_advance), \
                self.assertRaises(KeyboardInterrupt):
            schedule.run_due()
        with patch.object(schedule, "_pid_alive", return_value=False):
            recovered = schedule.reconcile_runs(records=True)
        self.assertEqual([(rec["task_id"], rec["status"]) for rec in recovered],
                         [(entry["id"], "interrupted")])

    def test_schedule_history_shows_actual_delivery_status_after_retry(self):
        notify.save_config({"url": "https://webhook.example.test/alerts"})
        entry = {"id": "sch-audit", "prompt": "fixture"}
        rec = schedule._finish_run(schedule._begin_run(entry, None), entry,
                                   "failed", error="fixture")
        with patch.object(notify, "_post", side_effect=notify.NotifyError("offline")):
            schedule._notify_run(rec)
        with patch.object(notify, "_post", return_value=(200, "")):
            notify.retry_pending(notif_id=rec["notification_id"])
        self.assertIn(f"{rec['notification_id']} sent", schedule.format_runs())

    def test_configuration_error_preserves_pending_recovery(self):
        cfg = {"url": "https://webhook.example.test/alerts"}
        notify.save_config(cfg)
        failed = {"task_id": "sch-audit", "status": "failed"}
        healthy = dict(failed, status="succeeded")
        with patch.object(notify, "_post", return_value=(200, "")) as post:
            notify.process_run(failed)
            notify._config_path().write_text("broken", encoding="utf-8")
            self.assertIsNotNone(notify.process_run(healthy)["error"])
            notify.save_config(cfg)
            self.assertEqual(notify.process_run(healthy)["notification"]["kind"], "recovery")
        self.assertEqual(post.call_count, 2)

    def test_rejects_email_with_a_missing_domain_label(self):
        with self.assertRaises(notify.NotifyError):
            notify._validate_mail_addr("person@example..test", "mail_to")

    def test_goal_keeps_sensitive_text_out_of_storage_and_continuation(self):
        active = session.Session()
        active.append({"role": "user", "content": "test"})
        synthetic = "sk-ONLYTESTSYNTHETICDATA012345"
        goal.create(active.id, f"请轮转 api_key={synthetic}")
        self.assertNotIn(synthetic, (self.state / "goals.json").read_text(encoding="utf-8"))
        self.assertNotIn(synthetic, goal.continuation(active.id))
        self.assertEqual((self.state / "goals.json").stat().st_mode & 0o777, 0o600)
