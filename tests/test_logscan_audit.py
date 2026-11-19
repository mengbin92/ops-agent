from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import core
from agent import logscan, tools


class LogScanAuditTests(unittest.TestCase):
    def test_nginx_time_buckets_keep_hour_and_round_minute(self):
        report = logscan.scan(["[27/Oct/2026:14:21:00 +0800] ERROR worker failed",
                               "[27/Oct/2026:14:36:00 +0800] ERROR worker failed"])
        self.assertEqual(set(report["buckets"]),
                         {"27/Oct/2026:14:20", "27/Oct/2026:14:30"})

    def test_capture_rejects_output_over_limit(self):
        with patch.object(core, "READ_OUTPUT_LIMIT", 1024 * 1024):
            with self.assertRaisesRegex(logscan.ScanSourceError, "1048576 字节"):
                logscan._run_local([sys.executable, "-c", "print('x' * (1024 * 1024))"])

    def test_shared_capture_counts_diagnostic_and_standard_output_together(self):
        argv = [sys.executable, "-c",
                "import sys; sys.stdout.write('abc'); sys.stderr.write('def')"]
        result = core.capture_read_output(argv, 3, 6, merge_stderr=False)
        self.assertEqual((result.stdout, result.stderr), (b"abc", b"def"))
        with self.assertRaisesRegex(core.OpsxError, "5 字节"):
            core.capture_read_output(argv, 3, 5, merge_stderr=False)

    def test_file_collection_reads_only_requested_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "example.log"
            path.write_text("old\nnew\nlatest\n", encoding="utf-8")
            _, lines = logscan.from_file(str(path), lines=2)
        self.assertEqual(lines, ["new", "latest"])

    def test_focused_scan_applies_regex_and_honors_timeout(self):
        lines = ["INFO ready", "ERROR worker failed"]
        report = logscan.scan_with_timeout(lines, top=1, pattern="worker", timeout=3)
        self.assertEqual(report["level_counts"], {"error": 1})
        with patch.object(logscan.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("fixture", 1)):
            with self.assertRaisesRegex(logscan.ScanSourceError, "正则扫描超时"):
                logscan.scan_with_timeout(lines, top=1, pattern="worker", timeout=1)

    def test_expired_task_does_not_start_log_collection(self):
        ctx = tools.ToolContext(deadline=time.monotonic() - 1)
        with patch.object(logscan, "detect_source") as collect:
            self.assertIn("时间预算耗尽", tools._logscan({"unit": "example"}, ctx))
        collect.assert_not_called()
