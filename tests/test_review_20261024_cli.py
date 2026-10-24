"""Single-prompt CLI failures remain controlled and never expose credentials."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("review_opsagent_cli", str(REPO / "opsagent"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class SinglePromptCliReviewTests(unittest.TestCase):
    def test_invalid_state_directory_returns_controlled_failure_without_network(self):
        with tempfile.TemporaryDirectory(prefix="ops-review-cli-") as temporary:
            state = Path(temporary) / "state"
            state.write_text("not a directory", encoding="utf-8")
            result = subprocess.run([sys.executable, str(REPO / "opsagent"), "-p", "inspect"],
                                    env=dict(os.environ, OPSX_STATE_DIR=str(state)),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("[opsagent] 错误:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), "not a directory")

    def test_session_initialization_error_is_caught_and_redacted(self):
        secret = "sk-CLIERRORSENTINEL0123456789"
        error = io.StringIO()
        with patch.object(cli.sess_mod, "Session", side_effect=ValueError("api_key=" + secret)), \
                patch.object(cli.loop, "run_turn") as run, contextlib.redirect_stderr(error):
            self.assertEqual(cli._print_mode("inspect", None), 1)
        run.assert_not_called()
        self.assertNotIn(secret, error.getvalue())
        self.assertIn(cli.core.REDACTED, error.getvalue())

    def test_audit_with_partial_utf8_tail_preserves_readable_entries(self):
        with tempfile.TemporaryDirectory(prefix="ops-review-audit-") as temporary:
            audit = Path(temporary) / "audit.jsonl"
            original = b'{"event":"exec","result":"preserved-evidence"}\n\xe4\xb8'
            audit.write_bytes(original)
            result = subprocess.run([sys.executable, str(REPO / "opsx"), "audit"],
                                    env=dict(os.environ, OPSX_STATE_DIR=temporary),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preserved-evidence", result.stdout)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(audit.read_bytes(), original)

    def test_unreadable_audit_has_a_controlled_cli_error(self):
        with tempfile.TemporaryDirectory(prefix="ops-review-audit-") as temporary:
            (Path(temporary) / "audit.jsonl").mkdir()
            result = subprocess.run([sys.executable, str(REPO / "opsx"), "audit"],
                                    env=dict(os.environ, OPSX_STATE_DIR=temporary),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("无法读取审计日志", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
