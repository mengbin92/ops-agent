"""W8 review: shared credential boundaries and CLI integration."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import core
from agent import tools


class W8ReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="opsx-w8-integration-review-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "active-state"
        self.state.mkdir(mode=0o700)

    def test_interrupted_backup_without_manifest_remains_protected(self):
        backup = self.root / "interrupted-backup"
        backup.mkdir(mode=0o700)
        archive = backup / "state.tar"
        archive.write_text("unstructured-original-private-data")
        alias = self.root / "archive-alias"
        alias.symlink_to(archive)
        self.assertFalse((backup / "manifest.json").exists())
        with patch.object(core, "STATE_DIR", self.state):
            for path in (archive, alias):
                result = tools.dispatch("read", {"path": str(path)}, tools.ToolContext())
                self.assertIn("拒绝", result)
                self.assertNotIn("original-private-data", result)
                self.assertNotEqual(core.check("cat " + str(path))[0], 0)
            self.assertNotEqual(core.check("grep -R original " + str(backup))[0], 0)
            self.assertEqual(core.check("ls -l " + str(backup))[0], 0)

    def test_state_cli_errors_do_not_write_or_emit_traceback(self):
        repo = Path(__file__).resolve().parents[1]
        destination = self.root / "restored"
        env = dict(os.environ, OPSX_STATE_DIR=str(self.state))
        for args in (("verify", str(self.root / "missing-backup")),
                     ("restore", str(self.root / "missing-backup"), "--to", str(destination), "--quiesced")):
            result = subprocess.run([sys.executable, str(repo / "opsx"), "state", *args],
                                    capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(destination.exists())
            self.assertEqual(list(self.state.iterdir()), [])

    def test_unencodable_recovery_json_fails_without_traceback(self):
        repo = Path(__file__).resolve().parents[1]
        backup, destination = self.root / "backup", self.root / "restored"
        (self.state / "notify.json").write_text(
            '{"url":"https://example.invalid/\\ud800","enabled":true}', encoding="utf-8")
        env = dict(os.environ, OPSX_STATE_DIR=str(self.state))
        created = subprocess.run([sys.executable, str(repo / "opsx"), "state", "backup",
                                  str(backup), "--quiesced"], capture_output=True,
                                 text=True, env=env, timeout=10)
        self.assertEqual(created.returncode, 0, created.stderr)
        result = subprocess.run([sys.executable, str(repo / "opsx"), "state", "restore",
                                 str(backup), "--to", str(destination), "--quiesced"],
                                capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
