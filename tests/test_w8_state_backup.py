"""W8 offline backups: raw recovery data, safe restores and hostile archive rejection."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import core
import execution
from agent import notify, schedule
import state_backup as backup


class StateBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-w8-backup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "state"
        self.source.mkdir(mode=0o700)
        self.archive = self.root / "backup"
        self.restore = self.root / "restored"
        self.patch = patch.object(core, "STATE_DIR", self.source)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.write("report.txt", b"evidence\x00\xff\n")

    def write(self, name, data):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def create(self):
        return backup.backup_state(self.source, self.archive, quiesced=True)

    def load_manifest(self):
        return json.loads((self.archive / "manifest.json").read_text())

    def save_manifest(self, manifest):
        (self.archive / "manifest.json").write_text(json.dumps(manifest))

    def forge(self, members, entries=None):
        """Update outer hash too, ensuring the member validator does actual work."""
        self.create()
        with tarfile.open(self.archive / "state.tar", "w") as tar:
            for member, data in members:
                tar.addfile(member, io.BytesIO(data) if member.isreg() else None)
        manifest = self.load_manifest()
        if entries is not None:
            manifest["entries"] = entries
        manifest["archive_sha256"] = hashlib.sha256((self.archive / "state.tar").read_bytes()).hexdigest()
        self.save_manifest(manifest)

    def assert_rejected(self):
        with self.assertRaises(backup.StateBackupError):
            backup.verify_backup(self.archive)
        with self.assertRaises(backup.StateBackupError):
            backup.restore_state(self.archive, self.restore, quiesced=True)
        self.assertFalse(self.restore.exists())

    @staticmethod
    def member(name="report.txt", data=b"evidence\x00\xff\n", kind=tarfile.REGTYPE):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = len(data) if kind == tarfile.REGTYPE else 0
        return info, data

    def test_raw_roundtrip_snapshot_mode_and_secrets(self):
        original = self.root / "private.conf"
        original.write_bytes(b"password=real-recovery-secret\n\x00\xff")
        original.chmod(0o640)
        sid = core.snapshot_file(str(original))
        before_meta = (self.source / "snapshots" / sid / "meta.json").read_bytes()
        raw_config = b'{"url":"https://example.invalid/production-secret","enabled":true}'
        self.write("notify.json", raw_config)
        raw_targets = b'{"test":{"host":"test.invalid","user":"ops"}}'
        self.write("targets.json", raw_targets)
        raw_budget = b'{"enabled":true,"daily_calls":17,"daily_tokens":1000}'
        self.write("budget.json", raw_budget)
        self.write("usage-state.json", b'{"2026-10-22":{"calls":5,"tokens":50}}')
        usage = (self.source / "usage-state.json").read_bytes()
        result = self.create()
        self.assertTrue(result["verified"])
        restored = backup.restore_state(self.archive, self.restore, quiesced=True)
        self.assertTrue(restored["notification_disabled"])
        with tarfile.open(self.archive / "state.tar") as tar:
            self.assertEqual(tar.extractfile("notify.json").read(), raw_config)
        self.assertEqual((self.restore / "targets.json").read_bytes(), raw_targets)
        self.assertEqual((self.restore / "budget.json").read_bytes(), raw_budget)
        self.assertEqual((self.restore / "usage-state.json").read_bytes(), usage)
        self.assertEqual((self.restore / "report.txt").read_bytes(), b"evidence\x00\xff\n")
        self.assertEqual((self.restore / "snapshots" / sid / "meta.json").read_bytes(), before_meta)
        with patch.object(core, "STATE_DIR", self.restore):
            directory, meta = core.load_snapshot(sid)
        archived_file = directory / meta["items"][0]["dest"]
        self.assertEqual(archived_file.read_bytes(), original.read_bytes())
        self.assertEqual(meta["items"][0]["mode"], 0o640)
        self.assertFalse(json.loads((self.restore / "notify.json").read_text())["enabled"])
        self.assertEqual((self.source / "notify.json").read_bytes(), raw_config)

    def test_all_output_directories_and_files_are_private(self):
        self.write("nested/deeper/secret", b"secret")
        (self.source / "nested").chmod(0o755)
        (self.source / "nested/deeper/secret").chmod(0o644)
        with patch.object(os, "umask", wraps=os.umask):
            old = os.umask(0)
            try:
                self.create()
                backup.restore_state(self.archive, self.restore, quiesced=True)
            finally:
                os.umask(old)
        for root in (self.archive, self.restore):
            for directory, subdirs, files in os.walk(root):
                self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
                for name in files:
                    self.assertEqual(stat.S_IMODE((Path(directory) / name).stat().st_mode), 0o600)

    def test_approvals_including_expired_and_consumed_are_quarantined(self):
        command = "systemctl restart backup-test"
        env = {"host": "isolated-test"}
        approval = core.approve(command, env=env, uses=3)
        raw = approval.read_bytes()
        self.write("approvals/expired.json", b'{"created":1,"ttl":1,"used":0}')
        self.write("approvals/consumed.json", b'{"created":9999999999,"ttl":100,"used":1,"uses":1}')
        self.create()
        result = backup.restore_state(self.archive, self.restore, quiesced=True)
        self.assertEqual(list((self.restore / "approvals").iterdir()), [])
        quarantine = self.restore / result["approval_quarantine"]
        self.assertEqual((quarantine / approval.name).read_bytes(), raw)
        self.assertTrue((quarantine / "expired.json").exists())
        self.assertEqual(approval.read_bytes(), raw)
        with patch.object(core, "STATE_DIR", self.restore):
            self.assertFalse(core.check_stamp(command, env=env))

    def test_running_records_are_not_revived_and_unknown_blocks_replay(self):
        command = "systemctl restart backup-test"
        rec = execution.begin("exec", command, env={"host": "isolated-test"})
        run = {"run_id": "run-test", "task_id": "sch-test", "status": "running",
               "pid": os.getpid(), "started": "2026-10-22 00:00:00", "started_ts": 1}
        self.write("runs/run-test.json", json.dumps(run).encode())
        self.write("schedules.json", json.dumps({"items": [
            {"id": "sch-test", "prompt": "check", "enabled": True,
             "next_run": 1, "last_status": "running"}],
            "meta": {"last_scan": 9999999999}}).encode())
        self.create()
        result = backup.restore_state(self.archive, self.restore, quiesced=True)
        self.assertEqual((result["running_executions"], result["running_schedules"], result["paused_schedules"]), (1, 1, 1))
        with patch.object(core, "STATE_DIR", self.restore):
            recovered = execution.get(rec["id"])
            self.assertEqual(recovered["status"], "unknown")
            self.assertIsNone(recovered["pid"])
            self.assertIsNone(recovered["resolved"])
            self.assertTrue(execution.pending_unknown(None, core.cmd_hash(command), env=rec["env"]))
            self.assertEqual(schedule.list_runs()[0]["status"], "interrupted")
            self.assertFalse(schedule._load()[0]["enabled"])
            self.assertNotIn("last_scan", schedule._load_state()[1])
        self.assertEqual(execution.get(rec["id"], view=False)["status"], "running")

    def test_notify_retry_cannot_send_after_restore(self):
        self.write("notify.json", json.dumps({"channel": "webhook", "url": "https://example.invalid/secret", "enabled": True}).encode())
        notif = {"id": "ntf-test", "status": "failed", "attempts": 0, "created": 1,
                 "next_attempt_ts": 0, "items": [], "recoveries": [], "fingerprints": [],
                 "title": "old notification", "severity": "warning"}
        self.write("notifications/ntf-test.json", json.dumps(notif).encode())
        self.create()
        backup.restore_state(self.archive, self.restore, quiesced=True)
        with patch.object(core, "STATE_DIR", self.restore), patch.object(notify, "_deliver") as deliver:
            notify.retry_pending()
            deliver.assert_not_called()

    def test_backup_and_restore_require_explicit_offline_assertion(self):
        with self.assertRaisesRegex(backup.StateBackupError, "停止全部"):
            backup.backup_state(self.source, self.archive)
        self.assertFalse(self.archive.exists())
        self.create()
        with self.assertRaisesRegex(backup.StateBackupError, "停止全部"):
            backup.restore_state(self.archive, self.restore)
        self.assertFalse(self.restore.exists())

    def test_existing_destinations_are_never_overwritten(self):
        self.create()
        original_archive = (self.archive / "state.tar").read_bytes()
        with self.assertRaises(backup.StateBackupError):
            self.create()
        self.assertEqual((self.archive / "state.tar").read_bytes(), original_archive)
        self.restore.mkdir()
        sentinel = self.restore / "active.json"
        sentinel.write_bytes(b"do not replace")
        with self.assertRaises(backup.StateBackupError):
            backup.restore_state(self.archive, self.restore, quiesced=True)
        self.assertEqual(sentinel.read_bytes(), b"do not replace")
        with self.assertRaises(backup.StateBackupError):
            backup.restore_state(self.archive, self.source, quiesced=True)

    def test_source_symlinks_hardlinks_and_special_files_are_rejected(self):
        victim = self.root / "outside"
        victim.write_bytes(b"outside secret")
        link = self.source / "linked"
        link.symlink_to(victim)
        with self.assertRaises(backup.StateBackupError):
            self.create()
        link.unlink()
        os.link(victim, link)
        with self.assertRaises(backup.StateBackupError):
            self.create()
        link.unlink()
        os.mkfifo(link)
        with self.assertRaises(backup.StateBackupError):
            self.create()
        self.assertFalse(self.archive.exists())

    def test_symlink_source_destination_or_parent_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(backup.StateBackupError):
            backup.backup_state(alias, self.archive, quiesced=True)
        with self.assertRaises(backup.StateBackupError):
            backup.backup_state(self.source, alias / "new", quiesced=True)
        self.create()
        with self.assertRaises(backup.StateBackupError):
            backup.restore_state(self.archive, alias / "restore", quiesced=True)

    def test_backup_inside_source_rejected(self):
        with self.assertRaises(backup.StateBackupError):
            backup.backup_state(self.source, self.source / "backup", quiesced=True)

    def test_tar_corruption_rejected(self):
        self.create()
        path = self.archive / "state.tar"
        data = bytearray(path.read_bytes())
        data[512] ^= 1
        path.write_bytes(data)
        self.assert_rejected()

    def test_file_digest_mismatch_rejected_even_with_updated_archive_hash(self):
        self.forge([self.member(data=b"different")])
        self.assert_rejected()

    def test_tar_path_traversal_and_absolute_path_rejected(self):
        for name in ("../escape", "/tmp/opsx-w8-escape", "a/../../escape", "a\\escape"):
            with self.subTest(name=name):
                if self.archive.exists():
                    import shutil
                    shutil.rmtree(self.archive)
                self.forge([self.member(name=name)])
                self.assert_rejected()
        self.assertFalse((self.root / "escape").exists())

    def test_tar_symlink_hardlink_fifo_and_device_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                if self.archive.exists():
                    import shutil
                    shutil.rmtree(self.archive)
                member, data = self.member(kind=kind)
                member.linkname = "../outside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
                self.forge([(member, data)])
                self.assert_rejected()

    def test_duplicate_extra_and_missing_tar_members_rejected(self):
        cases = [[self.member(), self.member()], [self.member(), self.member(name="extra")], []]
        for members in cases:
            with self.subTest(count=len(members)):
                if self.archive.exists():
                    import shutil
                    shutil.rmtree(self.archive)
                self.forge(members)
                self.assert_rejected()

    def test_manifest_traversal_duplicate_and_file_parent_rejected(self):
        self.create()
        original = self.load_manifest()
        for entries in ([{**original["entries"][0], "path": "../escape"}],
                        original["entries"] * 2,
                        [original["entries"][0], {**original["entries"][0], "path": "report.txt/child"}]):
            with self.subTest(entries=entries):
                self.save_manifest({**original, "entries": entries})
                self.assert_rejected()

    def test_unsafe_container_permissions_or_symlink_rejected(self):
        self.create()
        self.archive.chmod(0o755)
        self.assert_rejected()
        self.archive.chmod(0o700)
        manifest = self.archive / "manifest.json"
        manifest.chmod(0o644)
        self.assert_rejected()
        manifest.chmod(0o600)
        original = manifest.read_bytes()
        target = self.root / "manifest-original"
        target.write_bytes(original)
        manifest.unlink()
        manifest.symlink_to(target)
        self.assert_rejected()

    def test_invalid_runtime_json_fails_closed_and_cleans_only_new_restore(self):
        self.write("executions/exe-broken.json", b"not-json")
        self.create()
        self.assertTrue(backup.verify_backup(self.archive)["verified"])
        with self.assertRaises(backup.StateBackupError):
            backup.restore_state(self.archive, self.restore, quiesced=True)
        self.assertFalse(self.restore.exists())
        self.assertEqual((self.source / "executions/exe-broken.json").read_bytes(), b"not-json")

    def test_detects_concurrent_source_mutation_and_removes_incomplete_backup(self):
        original = backup._hash_file
        mutated = False
        def changing(handle):
            nonlocal mutated
            value = original(handle)
            if not mutated:
                mutated = True
                (self.source / "report.txt").write_bytes(b"concurrent write")
            return value
        with patch.object(backup, "_hash_file", side_effect=changing):
            with self.assertRaises(backup.StateBackupError):
                self.create()
        self.assertFalse(self.archive.exists())
        self.assertEqual((self.source / "report.txt").read_bytes(), b"concurrent write")

    def test_standalone_cli_roundtrip_and_missing_quiescence(self):
        script = str(Path(backup.__file__).resolve())
        denied = subprocess.run([sys.executable, script, "backup", "--source", str(self.source),
                                 "--destination", str(self.archive)], capture_output=True, text=True)
        self.assertEqual(denied.returncode, 1)
        self.assertIn("停止全部", denied.stderr)
        saved = subprocess.run([sys.executable, script, "backup", "--source", str(self.source),
                                "--destination", str(self.archive), "--quiesced"], capture_output=True, text=True)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        verified = subprocess.run([sys.executable, script, "verify", str(self.archive)], capture_output=True, text=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        restored = subprocess.run([sys.executable, script, "restore", str(self.archive),
                                   "--destination", str(self.restore), "--quiesced"], capture_output=True, text=True)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((self.restore / "report.txt").read_bytes(), b"evidence\x00\xff\n")


if __name__ == "__main__":
    unittest.main()
