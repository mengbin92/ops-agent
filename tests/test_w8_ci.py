"""W8 CI regressions: DNS-independent startup and slow HTTP failure accounting."""
from __future__ import annotations

import importlib.util
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch

import core
import usage
from agent import llm

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "w8_acceptance.py"
spec = importlib.util.spec_from_file_location("w8_ci_acceptance", SCRIPT)
w8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w8)


class W8CIRegressions(unittest.TestCase):
    def test_numeric_loopback_bind_never_uses_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("reverse DNS unavailable")) as fqdn, \
                patch("socket.gethostbyaddr", side_effect=AssertionError("resolver unavailable")) as reverse:
            with w8._LoopbackHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertEqual(server.server_port, server.socket.getsockname()[1])
                self.assertGreater(server.server_port, 0)
            fqdn.assert_not_called()
            reverse.assert_not_called()

    def test_slow_real_http_503_is_charged_once_and_then_recovers(self):
        class Handler(BaseHTTPRequestHandler):
            statuses = []

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                status = 503 if not self.statuses else 200
                self.statuses.append(status)
                if status == 503:
                    # Exceed the old 0.8-second deadline without mocking HTTP.
                    time.sleep(1.0)
                payload = {"error": "synthetic unavailable"} if status == 503 else {
                    "choices": [{"message": {"role": "assistant", "content": "recovered"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        with tempfile.TemporaryDirectory(prefix="opsx-w8-ci-") as temporary, \
                w8._LoopbackHTTPServer(("127.0.0.1", 0), Handler) as server, ExitStack() as cleanup:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cleanup.callback(thread.join, 3)
            cleanup.callback(server.shutdown)
            env = {"OPSAGENT_PROVIDER": "openai", "OPSAGENT_API_KEY": "local-test-only",
                   "OPSAGENT_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                   "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
            original_base = llm.TRANSIENT_BASE
            with patch.object(core, "STATE_DIR", Path(temporary)), patch.dict(os.environ, env):
                usage.save_budget({"enabled": True, "daily_calls": 10, "daily_tokens": None})
                self.assertIn("HTTP 503", w8._llm_failure_probe(llm))
                self.assertEqual(llm.TRANSIENT_BASE, original_base)
                self.assertEqual(usage.today()["calls"], 1)
                self.assertTrue(usage.recent()[0]["failed"])
                response = llm.chat([{"role": "user", "content": "recover"}], timeout=3)
                self.assertEqual(response["content"], "recovered")
                self.assertEqual(usage.today()["calls"], 2)
                self.assertEqual(sum(bool(row.get("failed")) for row in usage.recent()), 1)
            self.assertEqual(Handler.statuses, [503, 200])

    def test_failure_probe_restores_backoff_after_unexpected_error(self):
        original_base = llm.TRANSIENT_BASE
        with patch.object(llm, "chat", side_effect=RuntimeError("unexpected")):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                w8._llm_failure_probe(llm)
        self.assertEqual(llm.TRANSIENT_BASE, original_base)


if __name__ == "__main__":
    unittest.main()
