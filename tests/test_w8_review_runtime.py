"""W8 review regressions: restored state stays inert and is readable by runtime loaders."""
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import core
import execution
from agent import goal, notify, schedule, session
import state_backup as backup


class RecoveryRuntimeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="opsx-w8-runtime-review-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "state"
        self.source.mkdir(mode=0o700)
        self.archive = self.root / "backup"
        self.restored = self.root / "restored"
        self.state_patch = patch.object(core, "STATE_DIR", self.source)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def write(self, relative, value):
        path = self.source / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        path.write_bytes(data)
        path.chmod(0o600)
        return data

    def restore(self):
        backup.backup_state(self.source, self.archive, quiesced=True)
        return backup.restore_state(self.archive, self.restored, quiesced=True)

    def assert_restore_rejected(self, relative, value):
        original = self.write(relative, value)
        backup.backup_state(self.source, self.archive, quiesced=True)
        archive = (self.archive / "state.tar").read_bytes()
        with self.assertRaises(backup.StateBackupError):
            backup.restore_state(self.archive, self.restored, quiesced=True)
        self.assertFalse(self.restored.exists())
        self.assertEqual((self.source / relative).read_bytes(), original)
        self.assertEqual((self.archive / "state.tar").read_bytes(), archive)

    def test_active_goals_require_explicit_resume_and_keep_budget(self):
        active = {"id": "g-review", "objective": "inspect service", "status": "active",
                  "max_rounds": 6, "rounds_used": 3, "created": 1, "updated": 2}
        states = {"active-session": active}
        for status in ("paused", "completed", "blocked"):
            states[status + "-session"] = {**active, "status": status}
        original = self.write("goals.json", states)
        session.Session("active-session").append({"role": "user", "content": "inspect"})
        result = self.restore()
        self.assertEqual(result["paused_goals"], 1)
        with patch.object(core, "STATE_DIR", self.restored):
            self.assertIsNone(goal.continuation("active-session"))
            recovered = goal.get("active-session")
            self.assertEqual((recovered["status"], recovered["rounds_used"],
                              recovered["max_rounds"]), ("paused", 3, 6))
            for status in ("paused", "completed", "blocked"):
                self.assertEqual(goal.get(status + "-session")["status"], status)
        self.assertEqual((self.source / "goals.json").read_bytes(), original)

    def test_malformed_goals_fail_closed(self):
        self.assert_restore_rejected("goals.json", {"session": {
            "id": "g-review", "objective": "inspect", "status": "active",
            "max_rounds": 6, "rounds_used": True}})

    def test_malformed_execution_record_cannot_be_restored_as_success(self):
        self.assert_restore_rejected("executions/exe-review.json", {
            "id": "exe-review", "status": "running"})

    def test_mismatched_run_filename_cannot_be_restored_as_success(self):
        self.assert_restore_rejected("runs/run-review.json", {
            "run_id": "run-other", "status": "running", "started": "2026-10-22"})

    def test_invalid_run_status_cannot_be_restored_as_success(self):
        self.assert_restore_rejected("runs/run-review.json", {
            "run_id": "run-review", "status": "active", "started": "2026-10-22"})

    def test_invalid_schedule_entry_cannot_be_restored_as_success(self):
        self.assert_restore_rejected("schedules.json", {"items": [{}]})

    def test_invalid_schedule_enabled_is_not_silently_repaired(self):
        self.assert_restore_rejected("schedules.json", {"items": [
            {"id": "sch-review", "prompt": "inspect", "enabled": "yes"}]})

    def test_duplicate_schedule_ids_cannot_be_restored_as_success(self):
        entry = {"id": "sch-review", "prompt": "inspect", "enabled": True}
        self.assert_restore_rejected("schedules.json", {"items": [entry, entry]})

    def test_invalid_notify_config_cannot_be_restored_as_success(self):
        self.assert_restore_rejected("notify.json", {})

    def test_notify_unknown_fields_are_rejected_without_secret_in_error(self):
        self.write("notify.json", {"url": "https://example.invalid/credential",
                                   "secret-configuration-field": "private"})
        backup.backup_state(self.source, self.archive, quiesced=True)
        with self.assertRaises(backup.StateBackupError) as error:
            backup.restore_state(self.archive, self.restored, quiesced=True)
        self.assertNotIn("credential", str(error.exception))
        self.assertNotIn("secret-configuration-field", str(error.exception))

    def test_runtime_record_directory_cannot_be_a_file(self):
        self.assert_restore_rejected("executions", b"unusable runtime directory")

    def test_notification_outbox_directory_cannot_be_a_file(self):
        self.assert_restore_rejected("notifications", b"unusable outbox")

    def test_duplicate_json_keys_are_not_silently_discarded(self):
        self.assert_restore_rejected("schedules.json", b'{"items":[],"items":[]}')

    def test_non_json_constants_are_rejected(self):
        self.assert_restore_rejected("schedules.json", b'{"items":[],"meta":{"x":NaN}}')

    def test_nonfinite_numeric_exponent_is_rejected(self):
        self.assert_restore_rejected("schedules.json", b'{"items":[],"meta":{"x":1e999}}')

    def test_recovery_json_over_byte_cap_is_rejected_without_changing_archive(self):
        oversized = b" " * backup.RECOVERY_JSON_LIMIT + b"{}"
        self.assert_restore_rejected("goals.json", oversized)

    def test_recovery_json_reads_at_most_byte_cap_plus_one(self):
        class TrackedBytes(io.BytesIO):
            def read(inner_self, size=-1):
                self.assertEqual(size, 17)
                return super().read(size)
        with patch.object(backup, "RECOVERY_JSON_LIMIT", 16), \
                patch.object(Path, "open", return_value=TrackedBytes(b" " * 100 + b"{}")):
            with self.assertRaises(backup.StateBackupError):
                backup._load_json(self.source / "goals.json")

    def test_recovery_json_exact_byte_cap_is_accepted(self):
        self.write("goals.json", b" " * 14 + b"{}")
        with patch.object(backup, "RECOVERY_JSON_LIMIT", 16):
            self.assertEqual(backup._load_json(self.source / "goals.json"), {})

    def test_oversized_integer_rejected_even_without_python_integer_guard(self):
        old_limit = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
        try:
            if old_limit is not None:
                sys.set_int_max_str_digits(0)
            self.assert_restore_rejected("schedules.json",
                b'{"items":[],"meta":{"untrusted":' + b"9" * 10000 + b"}}")
        finally:
            if old_limit is not None:
                sys.set_int_max_str_digits(old_limit)

    def test_utf16_goals_cannot_be_accepted_then_fail_actual_loader(self):
        self.assert_restore_rejected("goals.json", "{}".encode("utf-16"))

    def test_utf8_bom_is_rejected_consistently_with_runtime_loaders(self):
        self.assert_restore_rejected("goals.json", b"\xef\xbb\xbf{}")

    def test_deeply_nested_json_has_controlled_error_and_cleanup(self):
        self.assert_restore_rejected("schedules.json", b"[" * 1500 + b"0" + b"]" * 1500)

    def test_failed_json_serialization_does_not_truncate_existing_evidence(self):
        path = self.source / "evidence.json"
        path.write_bytes(b"original evidence")
        with patch.object(backup, "_json_bytes", side_effect=ValueError("serialize failure")):
            with self.assertRaises(ValueError):
                backup._replace_json(path, {})
        self.assertEqual(path.read_bytes(), b"original evidence")
        self.assertEqual(list(self.source.glob(".*.recovery-*")), [])

    def test_previous_recovery_evidence_survives_repeated_restore(self):
        original = self.write("recovery.json", b'{"restored_unix":12,"evidence":"old"}\n')
        self.write(".ops-agent-recovered", b"ops-agent recovered state v1\n")
        result = self.restore()
        self.assertEqual((self.restored / result["previous_recovery"]).read_bytes(), original)
        first_marker = (self.restored / "recovery.json").read_bytes()
        second_backup, second_restore = self.root / "backup2", self.root / "restored2"
        backup.backup_state(self.restored, second_backup, quiesced=True)
        second = backup.restore_state(second_backup, second_restore, quiesced=True)
        self.assertEqual((second_restore / second["previous_recovery"]).read_bytes(), first_marker)
        self.assertTrue((second_restore / result["previous_recovery"]).is_file())

    def test_restore_is_protected_before_raw_extraction_starts(self):
        self.write("notify.json", {"url": "https://example.invalid/credential"})
        backup.backup_state(self.source, self.archive, quiesced=True)
        original = backup._walk_archive
        extraction_observed = []
        def checked(*args, **kwargs):
            destination = kwargs.get("destination")
            if destination:
                extraction_observed.append(True)
                self.assertTrue((destination / ".ops-agent-recovered").is_file())
                self.assertTrue(core._protected_state_copy(str(destination / "notify.json")))
            return original(*args, **kwargs)
        with patch.object(backup, "_walk_archive", side_effect=checked):
            backup.restore_state(self.archive, self.restored, quiesced=True)
        self.assertEqual(extraction_observed, [True])

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "requires POSIX kill")
    def test_killed_restore_retains_protection_marker_for_raw_residue(self):
        self.write("notify.json", {"url": "https://example.invalid/credential"})
        backup.backup_state(self.source, self.archive, quiesced=True)
        code = ("import os,signal,sys,state_backup as b\n"
                "original=b._walk_archive\n"
                "def stop(*a,**k):\n"
                " original(*a,**k)\n"
                " if k.get('destination'): os.kill(os.getpid(),signal.SIGKILL)\n"
                "b._walk_archive=stop\n"
                "b.restore_state(sys.argv[1],sys.argv[2],quiesced=True)\n")
        result = subprocess.run([sys.executable, "-c", code, str(self.archive), str(self.restored)],
                                cwd=Path(backup.__file__).parent, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertTrue((self.restored / "notify.json").is_file())
        self.assertTrue(core._protected_state_copy(str(self.restored / "notify.json")))

    def test_keyboard_interrupt_cleans_the_new_restore(self):
        backup.backup_state(self.source, self.archive, quiesced=True)
        with patch.object(backup, "_safe_recovery", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                backup.restore_state(self.archive, self.restored, quiesced=True)
        self.assertFalse(self.restored.exists())

    def test_failure_cleanup_never_removes_replaced_destination(self):
        backup.backup_state(self.source, self.archive, quiesced=True)
        moved = self.root / "moved-restore"
        def replace_destination(*_args):
            self.restored.rename(moved)
            self.restored.mkdir()
            (self.restored / "keep").write_bytes(b"unrelated state")
            raise RuntimeError("replaced externally")
        with patch.object(backup, "_safe_recovery", side_effect=replace_destination):
            with self.assertRaises(RuntimeError):
                backup.restore_state(self.archive, self.restored, quiesced=True)
        self.assertEqual((self.restored / "keep").read_bytes(), b"unrelated state")
        self.assertTrue((moved / ".ops-agent-recovered").is_file())

    def test_valid_restore_is_accepted_by_actual_runtime_readers(self):
        rec = execution.begin("exec", "systemctl restart isolated-service")
        schedule.add("inspect isolated-service", every_seconds=60)
        notify.save_config({"url": "https://example.invalid/credential"})
        result = self.restore()
        self.assertEqual((result["running_executions"], result["paused_schedules"]), (1, 1))
        with patch.object(core, "STATE_DIR", self.restored):
            self.assertEqual(execution.get(rec["id"])["status"], "unknown")
            self.assertIsNotNone(execution.pending_unknown(None, core.cmd_hash(rec["cmd"])))
            self.assertFalse(schedule._load()[0]["enabled"])
            self.assertFalse(notify.load_config()["enabled"])


if __name__ == "__main__":
    unittest.main()
