"""W7 regression coverage: deterministic budgets and visible diagnostics."""
from __future__ import annotations

import contextlib
import concurrent.futures
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import core
import doctor
import usage
from agent import llm, llm_anthropic


class Response:
    def __init__(self, data=None, chunks=None):
        self.data = data
        self.chunks = chunks or []
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.data).encode()

    def __iter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield b'data: ' + json.dumps(chunk).encode() + b'\n'


def message_response(usage_data=None):
    result = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    if usage_data is not None:
        result["usage"] = usage_data
    return Response(result)


class ReliabilityReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ops-review-reliability-")
        self.addCleanup(self.tmp.cleanup)
        state = mock.patch.object(core, "STATE_DIR", Path(self.tmp.name))
        state.start()
        self.addCleanup(state.stop)
        env = mock.patch.dict(os.environ, {"OPSAGENT_PROVIDER": "openai",
                                          "OPSAGENT_API_KEY": "test-review-api-key"})
        env.start()
        self.addCleanup(env.stop)
        usage.save_budget({"enabled": True, "daily_calls": 100,
                           "daily_tokens": None})

    def test_failures_and_retries_consume_call_budget(self):
        usage.save_budget({"enabled": True, "daily_calls": 1, "daily_tokens": None})
        with mock.patch.object(llm, "_post", side_effect=llm.LLMError("HTTP 429")) as post, \
                mock.patch.object(llm.time, "sleep"):
            with self.assertRaises(llm.BudgetExhausted):
                llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(usage.today()["calls"], 1)
        self.assertTrue(usage.recent()[0]["failed"])

    def test_budget_reservation_serializes_concurrent_calls(self):
        usage.save_budget({"enabled": True, "daily_calls": 1, "daily_tokens": None})
        barrier = threading.Barrier(8)

        def invoke():
            barrier.wait()
            try:
                llm.chat([{"role": "user", "content": "hi"}])
                return "ok"
            except llm.BudgetExhausted:
                return "blocked"

        with mock.patch.object(llm, "_post", side_effect=lambda *args: message_response()), \
                concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: invoke(), range(8)))
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(usage.today()["calls"], 1)

    def test_missing_or_malformed_provider_usage_is_estimated(self):
        invalid = [{"prompt_tokens": "bad", "completion_tokens": 2},
                   {"prompt_tokens": -1, "completion_tokens": 2},
                   {"prompt_tokens": True, "completion_tokens": 2},
                   {"completion_tokens": 2}]
        for raw in invalid:
            with self.subTest(raw=raw), \
                    mock.patch.object(llm, "_post", return_value=message_response(raw)):
                self.assertEqual(llm.chat([{"role": "user", "content": "hi"}])["content"], "ok")
        self.assertEqual(usage.today()["calls"], len(invalid))
        self.assertGreater(usage.today()["tokens"], 0)
        self.assertTrue(all(row["estimated"] for row in usage.recent()))

    def test_estimate_includes_tool_schema_and_call_arguments(self):
        baseline = usage.estimate_tokens([], "")
        result = usage.estimate_tokens([], {"tool_calls": [{"arguments": "a" * 4000}]},
                                       tools=[{"description": "b" * 4000}])
        self.assertGreater(result[0], baseline[0] + 900)
        self.assertGreater(result[1], baseline[1] + 900)

    def test_openai_stream_usage_is_recorded(self):
        chunks = [{"choices": [{"delta": {"content": "ok"}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 80,
                                              "completion_tokens": 20}}]
        with mock.patch.object(llm, "_post", return_value=Response(chunks=chunks)):
            result = llm.chat([{"role": "user", "content": "hi"}], on_delta=lambda _: None)
        self.assertEqual(result["content"], "ok")
        self.assertEqual(usage.today()["tokens"], 100)
        self.assertNotIn("estimated", usage.recent()[0])

    def test_anthropic_stream_usage_is_recorded(self):
        chunks = [{"type": "message_start", "message": {"usage": {"input_tokens": 80,
                                                                   "output_tokens": 1}}},
                  {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
                  {"type": "message_delta", "usage": {"output_tokens": 20}},
                  {"type": "message_stop"}]
        with mock.patch.dict(os.environ, {"OPSAGENT_PROVIDER": " Anthropic "}), \
                mock.patch.object(llm_anthropic, "_request", return_value=Response(chunks=chunks)):
            result = llm.chat([{"role": "user", "content": "hi"}], on_delta=lambda _: None)
        self.assertEqual(result["content"], "ok")
        self.assertEqual(usage.today()["tokens"], 100)
        self.assertEqual(usage.recent()[0]["model"], llm_anthropic.DEFAULT_MODEL)

    def test_partial_text_stream_is_never_retried(self):
        chunks = [{"choices": [{"delta": {"content": "first"}}]}, OSError("connection reset")]
        output = []
        with mock.patch.object(llm, "_post", return_value=Response(chunks=chunks)) as post, \
                mock.patch.object(llm.time, "sleep") as sleep:
            with self.assertRaises(llm.LLMError):
                llm.chat([{"role": "user", "content": "hi"}], on_delta=output.append)
        self.assertEqual(output, ["first"])
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_incomplete_tool_stream_is_rejected(self):
        chunks = [{"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call-1", "function": {"name": "ops_exec", "arguments": "{}"}}
        ]}}]}]
        with mock.patch.object(llm, "_post", return_value=Response(chunks=chunks)) as post:
            with self.assertRaisesRegex(llm.LLMError, "未收到结束标记"):
                llm.chat([{"role": "user", "content": "hi"}], on_delta=lambda _: None)
        self.assertEqual(post.call_count, 1)

    def test_subsecond_deadline_is_not_rounded_up(self):
        timeouts = []

        def request(body, timeout):
            timeouts.append(timeout)
            return message_response()

        with mock.patch.object(llm, "_post", side_effect=request):
            llm.chat([{"role": "user", "content": "hi"}], deadline=time.monotonic() + 0.15)
        self.assertTrue(0 < timeouts[0] <= 0.15)

    def test_nested_state_corruption_fails_closed(self):
        for day_value in ({}, None, {"calls": -2, "tokens": 0},
                          {"calls": "3", "tokens": 0}, {"calls": 1, "tokens": False}):
            with self.subTest(day=day_value):
                usage._state_file().write_text(json.dumps({"days": {usage._day_key(): day_value}}))
                with self.assertRaises(core.OpsxError):
                    usage.check_budget()

    def test_nonwritable_budget_state_blocks_api(self):
        with mock.patch.object(usage, "_save_state", side_effect=PermissionError("readonly")), \
                mock.patch.object(llm, "_post") as post:
            with self.assertRaisesRegex(llm.LLMError, "已停止调用"):
                llm.chat([{"role": "user", "content": "hi"}])
        post.assert_not_called()

    def test_reserved_call_stays_on_start_day(self):
        with mock.patch.object(usage, "_day_key", return_value="2026-10-21"):
            day, reason = usage.reserve_call()
        self.assertIsNone(reason)
        with mock.patch.object(usage, "_day_key", return_value="2026-10-22"):
            usage.record("model", 10, 20, reserved_day=day)
        state = usage._load_state()
        self.assertEqual(state["days"]["2026-10-21"], {"calls": 1, "tokens": 30})
        self.assertNotIn("2026-10-22", state["days"])

    def test_recent_zero_is_empty(self):
        usage.record("model", 1, 2)
        self.assertEqual(usage.recent(0), [])

    def test_doctor_probe_preserves_existing_file(self):
        original = core.STATE_DIR / ".doctor-probe"
        original.write_text("keep this")
        doctor._check_state_dir()
        self.assertEqual(original.read_text(), "keep this")
        self.assertEqual(list(core.STATE_DIR.glob(".doctor-probe-*")), [])

    def test_doctor_rejects_unknown_provider_without_network(self):
        with mock.patch.dict(os.environ, {"OPSAGENT_PROVIDER": "wrong"}), \
                mock.patch.object(doctor.urllib.request, "urlopen") as urlopen:
            self.assertEqual(doctor._check_llm_config()[0]["status"], doctor.FAIL)
            self.assertEqual(doctor._probe_llm_api()[0]["status"], doctor.FAIL)
        urlopen.assert_not_called()

    def test_doctor_disabled_notifications_are_visible(self):
        from agent import notify
        config = {"url": "https://example.com/private", "format": "generic", "enabled": False}
        with mock.patch.object(notify, "load_config", return_value=config), \
                mock.patch.object(notify, "list_notifications", return_value=[]):
            self.assertEqual(doctor._check_notify()[0]["status"], doctor.WARN)

    def test_doctor_help_never_probes(self):
        with mock.patch.object(doctor, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                doctor.cli(["--help"])
        self.assertEqual(raised.exception.code, 0)
        run.assert_not_called()

    def test_doctor_internal_failure_is_not_reported_healthy(self):
        def broken():
            raise RuntimeError("cannot inspect")

        with mock.patch.object(doctor, "SECTIONS", (("broken", broken),)):
            _, counts = doctor.run()
            self.assertEqual(counts[doctor.FAIL], 1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(doctor.cli([]), 1)

    def test_doctor_unreadable_budget_is_failure(self):
        with mock.patch.object(usage, "today", side_effect=PermissionError("readonly")):
            self.assertEqual(doctor._check_budget()[0]["status"], doctor.FAIL)

    def test_provider_error_never_echoes_configured_key(self):
        key = os.environ["OPSAGENT_API_KEY"]
        self.assertNotIn(key, str(llm.LLMError("proxy echoed " + key)))
        self.assertNotIn(key, doctor._item("test", doctor.FAIL, "proxy echoed " + key)["detail"])

    def test_doctor_masks_base_url_credentials(self):
        with mock.patch.dict(os.environ, {"OPSAGENT_BASE_URL": "https://user:password@example.com/private?token=raw"}):
            detail = doctor._check_llm_config()[0]["detail"]
        self.assertNotIn("user:", detail)
        self.assertNotIn("private", detail)
        self.assertNotIn("raw", detail)

    def test_timer_uses_oneshot_service(self):
        text = (core.REPO_DIR / "docs/ops/opsagent-schedule.timer").read_text()
        self.assertIn("Unit=opsagent-schedule-oneshot.service", text)


if __name__ == "__main__":
    unittest.main()
