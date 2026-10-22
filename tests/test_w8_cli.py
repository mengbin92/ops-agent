"""W8 offline backup commands exposed through the supported CLI."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
from agent import tools


REPO = Path(__file__).resolve().parent.parent


class StateBackupCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        (self.state / "memory.md").write_text("test-host: role = isolated\n")
        self.env = dict(os.environ, OPSX_STATE_DIR=str(self.state))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(REPO / "opsx"), *args],
                              capture_output=True, text=True, env=self.env, timeout=20)

    def test_help_and_quiescence_are_explicit(self):
        help_result = self.cli("state", "backup", "--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--quiesced", help_result.stdout)
        result = self.cli("state", "backup", str(self.root / "backup"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "backup").exists())

    def test_roundtrip_and_existing_destination_are_safe(self):
        backup = self.root / "backup"
        restored = self.root / "restored"
        for args in (("state", "backup", str(backup), "--quiesced"),
                     ("state", "verify", str(backup)),
                     ("state", "restore", str(backup), "--to", str(restored), "--quiesced")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
            self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertEqual((restored / "memory.md").read_text(), (self.state / "memory.md").read_text())
        existing = self.cli("state", "restore", str(backup), "--to", str(restored), "--quiesced")
        self.assertNotEqual(existing.returncode, 0)
        self.assertEqual((restored / "memory.md").read_text(), (self.state / "memory.md").read_text())

    def test_backup_commands_are_not_hook_exemptions(self):
        request = {"tool_input": {"command": "./opsx state restore /tmp/archive --to /tmp/state --quiesced"}}
        result = subprocess.run([sys.executable, str(REPO / "opsx"), "hook"],
                                input=json.dumps(request), capture_output=True,
                                text=True, env=self.env, timeout=10)
        self.assertNotEqual(result.returncode, 0)

    def test_raw_backups_are_not_exposed_to_model_tools(self):
        backup = self.root / "offline-backup"
        backup.mkdir(mode=0o700)
        (backup / "manifest.json").write_text('{}')
        archive = backup / "state.tar"
        archive.write_text("unformatted-private-original")
        alias = self.root / "archive-alias"
        alias.symlink_to(archive)
        with patch.object(core, "STATE_DIR", self.state):
            for path in (archive, alias):
                self.assertIn("拒绝", tools.dispatch("read", {"path": str(path)}, tools.ToolContext()))
                self.assertNotEqual(core.check("cat " + str(path))[0], 0)
            self.assertNotEqual(core.check("grep -R value " + str(backup))[0], 0)
            self.assertEqual(core.check("ls -l " + str(backup))[0], 0)

    def test_restored_state_is_protected_before_switching_active_directory(self):
        backup, restored = self.root / "backup", self.root / "recovered"
        (self.state / "notify.json").write_text(json.dumps({
            "enabled": True, "url": "https://example.invalid/private-webhook-token"}))
        (self.state / "snapshots").mkdir()
        (self.state / "snapshots" / "raw-original").write_text("snapshot-original-secret")
        for args in (("state", "backup", str(backup), "--quiesced"),
                     ("state", "restore", str(backup), "--to", str(restored), "--quiesced")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(core, "STATE_DIR", self.state):
            for path in (restored / "notify.json", restored / "snapshots" / "raw-original"):
                self.assertIn("拒绝", tools.dispatch("read", {"path": str(path)}, tools.ToolContext()))
                self.assertNotEqual(core.check("cat " + str(path))[0], 0)
        with patch.object(core, "STATE_DIR", restored):
            self.assertEqual(tools.dispatch("read", {"path": str(restored / "memory.md")},
                                             tools.ToolContext()), "test-host: role = isolated")
            for path in (restored / "notify.json", restored / "snapshots" / "raw-original"):
                self.assertIn("拒绝", tools.dispatch("read", {"path": str(path)}, tools.ToolContext()))


if __name__ == "__main__":
    unittest.main()
