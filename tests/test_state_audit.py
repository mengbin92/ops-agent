import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core
import usage
from agent import events


class StateAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        state_dir = patch.object(core, "STATE_DIR", self.state)
        state_dir.start()
        self.addCleanup(state_dir.stop)

    def test_event_log_preserves_unicode_line_separators(self):
        # Existing logs may contain literal Unicode separators inside JSON strings.
        path = self.state / "events.jsonl"
        detail = "first\u2028second"
        path.write_text(json.dumps({"kind": "run-failed", "detail": detail},
                                   ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(events.tail(1)[0]["detail"], detail)

    def test_event_fields_are_redacted_before_persistence(self):
        events.append("notify-failed", smtp_password="fixture-value",
                      nested={"access_token": "opaque-fixture"})
        raw = (self.state / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("fixture-value", raw)
        self.assertNotIn("opaque-fixture", raw)

    def test_usage_append_survives_a_partial_line(self):
        path = self.state / "usage.jsonl"
        path.write_text('{"unfinished":', encoding="utf-8")
        usage.record("next-model", 1, 2)
        self.assertEqual([row["model"] for row in usage.recent(1)], ["next-model"])

    def test_audit_after_interrupted_write_is_a_separate_record(self):
        path = self.state / "audit.jsonl"
        path.write_bytes(b'{"unfinished":')
        core.audit("local-check", detail="\ud800")
        self.assertEqual(json.loads(path.read_bytes().split(b"\n")[1])["event"], "local-check")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_usage_recent_counts_valid_rows_across_rotation(self):
        (self.state / "usage.jsonl.1").write_text(
            '{"model":"old\u2028alias"}\n', encoding="utf-8")
        (self.state / "usage.jsonl").write_text(
            '{"model":"new"}\n{"unfinished":', encoding="utf-8")
        self.assertEqual([row["model"] for row in usage.recent(2)], ["old\u2028alias", "new"])
