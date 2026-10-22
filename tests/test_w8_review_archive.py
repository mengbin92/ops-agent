"""Review regressions for W8 offline archive boundaries and interruption paths."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import state_backup as backup


class ArchiveReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opsx-w8-archive-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "state"
        self.source.mkdir(mode=0o700)
        self.archive = self.root / "backup"
        self.data = b"private-recovery-secret\x00\xff"
        (self.source / "report").write_bytes(self.data)

    def create(self):
        return backup.backup_state(self.source, self.archive, quiesced=True)

    def rehash(self, entries=None):
        path = self.archive / "manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["archive_sha256"] = hashlib.sha256((self.archive / "state.tar").read_bytes()).hexdigest()
        if entries is not None:
            manifest["entries"] = entries
        path.write_text(json.dumps(manifest))

    def test_symlink_dotdot_source_is_rejected_before_normalization(self):
        outside = self.root / "outside"
        (outside / "child").mkdir(parents=True)
        (self.root / "alias").symlink_to(outside / "child", target_is_directory=True)
        with self.assertRaisesRegex(backup.StateBackupError, "软链接"):
            backup.backup_state(self.root / "alias" / ".." / "state", self.archive, quiesced=True)
        self.assertFalse(self.archive.exists())

    def test_symlink_dotdot_destination_is_rejected_before_normalization(self):
        (self.root / "alias").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(backup.StateBackupError, "软链接"):
            backup.backup_state(self.source, self.root / "alias" / ".." / "backup", quiesced=True)
        self.assertFalse(self.archive.exists())

    def test_nonexistent_and_file_dotdot_ancestors_are_not_silently_removed(self):
        (self.root / "file").write_bytes(b"not a directory")
        for component in ("missing", "file"):
            with self.subTest(component=component):
                with self.assertRaises(backup.StateBackupError):
                    backup.backup_state(self.source, self.root / component / ".." / "backup", quiesced=True)
                self.assertFalse(self.archive.exists())

    def test_existing_directory_dotdot_paths_keep_normal_semantics(self):
        (self.root / "directory").mkdir()
        result = backup.backup_state(self.source, self.root / "directory" / ".." / "backup", quiesced=True)
        self.assertEqual(result["backup"], str(self.archive.resolve()))
        self.assertTrue(backup.verify_backup(self.archive)["verified"])

    def test_keyboard_interrupt_and_system_exit_remove_private_partial_backup(self):
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error=error.__name__):
                with patch.object(backup, "verify_backup", side_effect=error):
                    with self.assertRaises(error):
                        self.create()
                self.assertFalse(self.archive.exists())
                self.assertEqual((self.source / "report").read_bytes(), self.data)

    def test_chmod_initialization_failure_removes_new_output(self):
        with patch.object(backup.os, "chmod", side_effect=OSError("initialization failed")):
            with self.assertRaises(OSError):
                self.create()
        self.assertFalse(self.archive.exists())

    def test_cleanup_does_not_delete_a_replacement_output_directory(self):
        moved = self.root / "moved-backup"
        def replace_then_interrupt(*args):
            self.archive.rename(moved)
            self.archive.mkdir(mode=0o700)
            (self.archive / "unrelated").write_bytes(b"keep this")
            raise KeyboardInterrupt
        with patch.object(backup, "verify_backup", side_effect=replace_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.create()
        self.assertEqual((self.archive / "unrelated").read_bytes(), b"keep this")
        self.assertTrue(moved.is_dir())

    def test_fifo_manifest_and_archive_fail_without_blocking(self):
        script = str(Path(backup.__file__).resolve())
        for name in ("manifest.json", "state.tar"):
            with self.subTest(name=name):
                if self.archive.exists():
                    shutil.rmtree(self.archive)
                self.create()
                path = self.archive / name
                path.unlink()
                os.mkfifo(path, 0o600)
                result = subprocess.run([sys.executable, script, "verify", str(self.archive)],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("拒绝特殊文件", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_opened_file_descriptor_is_closed_if_fstat_fails(self):
        opened = []
        real_open, real_fstat = os.open, os.fstat
        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        with patch.object(backup.os, "open", side_effect=record_open), \
                patch.object(backup.os, "fstat", side_effect=OSError("fstat failed")):
            with self.assertRaises(OSError):
                backup._open_file(self.source.resolve(), "report")
        self.assertEqual(len(opened), 2)
        for fd in opened:
            with self.assertRaises(OSError):
                real_fstat(fd)

    def test_inventory_entry_limit_stops_directory_enumeration_early(self):
        def entries(*args):
            yield self.source / "report"
            yield self.source / "second"
            raise AssertionError("enumerated past entry limit")
        with patch.object(backup, "MAX_ENTRIES", 1), patch.object(Path, "iterdir", entries):
            with self.assertRaisesRegex(backup.StateBackupError, "条目过多"):
                backup._inventory(self.source)

    def test_unexpected_backup_entry_stops_directory_enumeration_early(self):
        def entries(*args):
            yield self.archive / "unexpected"
            raise AssertionError("enumerated the rest of an unbounded directory")
        with patch.object(Path, "iterdir", entries):
            with self.assertRaisesRegex(backup.StateBackupError, "须只含"):
                backup._manifest(self.archive)

    def test_oversized_pax_metadata_is_rejected_before_its_read(self):
        self.create()
        header = tarfile.TarInfo("pax")
        header.type = tarfile.XHDTYPE
        header.size = 2 ** 63
        (self.archive / "state.tar").write_bytes(header.tobuf(format=tarfile.GNU_FORMAT) + bytes(1024))
        self.rehash(entries=[])
        with self.assertRaisesRegex(backup.StateBackupError, "扩展头过大"):
            backup.verify_backup(self.archive)

    def test_concatenated_archive_and_truncated_zero_trailer_are_rejected(self):
        self.create()
        original = (self.archive / "state.tar").read_bytes()
        for data in (original + original, original[:1024]):
            with self.subTest(size=len(data)):
                (self.archive / "state.tar").write_bytes(data)
                self.rehash()
                with self.assertRaises(backup.StateBackupError):
                    backup.verify_backup(self.archive)

    def test_unicode_pax_and_long_names_still_roundtrip(self):
        name = "恢复-" + "x" * 120
        (self.source / name).write_bytes(b"unicode and long pax path")
        self.create()
        destination = self.root / "restored"
        backup.restore_state(self.archive, destination, quiesced=True)
        self.assertEqual((destination / name).read_bytes(), b"unicode and long pax path")

    def test_large_file_content_is_streamed_with_bounded_tar_reads(self):
        data = self.data * (backup.TAR_READ_LIMIT // len(self.data) * 3)
        (self.source / "report").write_bytes(data)
        self.create()
        destination = self.root / "restored"
        backup.restore_state(self.archive, destination, quiesced=True)
        self.assertEqual((destination / "report").read_bytes(), data)

    def test_nested_and_duplicate_manifest_json_fail_as_controlled_errors(self):
        self.create()
        path = self.archive / "manifest.json"
        original = path.read_text()
        for data in ("[" * 1500 + "0" + "]" * 1500,
                     original.replace('"format": 1', '"format": 0, "format": 1'),
                     original.replace('"format": 1', '"format": ' + "1" * 4097)):
            with self.subTest(prefix=data[:30]):
                path.write_text(data)
                with self.assertRaises(backup.StateBackupError):
                    backup.verify_backup(self.archive)

    def test_unpaired_surrogate_json_encoding_is_a_controlled_error(self):
        with self.assertRaisesRegex(backup.StateBackupError, "Unicode") as raised:
            backup._json_bytes({"token": "never-print-secret-\ud800"})
        self.assertNotIn("never-print-secret", str(raised.exception))

    def test_skip_write_keeps_precreated_marker_but_still_checks_its_content(self):
        marker = ".ops-agent-recovered"
        (self.source / marker).write_bytes(b"old recovery marker")
        self.create()
        destination = self.root / "restored"
        destination.mkdir(mode=0o700)
        (destination / marker).write_bytes(b"new recovery marker")
        manifest = backup._manifest(self.archive.resolve())
        backup._walk_archive(self.archive.resolve(), manifest, destination=destination, skip_write={marker})
        self.assertEqual((destination / marker).read_bytes(), b"new recovery marker")
        manifest["entries"] = [{**e, "sha256": "0" * 64} if e["path"] == marker else e
                               for e in manifest["entries"]]
        with self.assertRaisesRegex(backup.StateBackupError, "SHA-256"):
            backup._walk_archive(self.archive.resolve(), manifest, skip_write={marker})


if __name__ == "__main__":
    unittest.main()
