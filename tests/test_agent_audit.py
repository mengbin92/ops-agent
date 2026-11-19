"""Local regressions for the runtime audit."""
import asyncio
import http.client
import io
import json
import os
import runpy
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlparse

import core
from agent import (a2a_cli, a2a_client, a2a_executor, a2a_server, checks, llm,
                   logscan, loop, markdown, repl, session, tools)


class RuntimeAuditTests(unittest.TestCase):
    def test_probe_does_not_accept_a_general_command_authorization(self):
        with patch.object(core, "classify", return_value=("R1", None)), \
                patch.object(core, "check_stamp", return_value=True) as check_stamp, \
                patch.object(core, "audit"), \
                patch.object(checks, "_exec", return_value=(0, "fixture")) as execute:
            code, _ = checks._probe(None, "fixture", 1)
        self.assertEqual(code, 126)
        check_stamp.assert_not_called()
        execute.assert_not_called()

    def test_log_focus_keeps_timestamp_coverage_based_on_all_scanned_rows(self):
        report = logscan.scan(["2026-01-01 01:21:00 warning alpha\n",
                               "2026-01-01 01:21:01 warning beta\n"], pattern="alpha")
        self.assertEqual(report["scanned"], 2)
        self.assertEqual(report["time_parse_rate"], 1)

    def test_log_timestamps_use_ten_minute_buckets(self):
        report = logscan.scan(["2026-01-01 01:21:00 warning alpha",
                               "2026-01-01 01:29:01 warning beta",
                               "2026-01-01 01:30:01 warning gamma"])
        self.assertEqual(report["buckets"], {"2026-01-01 01:20": {"warn": 2},
                                             "2026-01-01 01:30": {"warn": 1}})

    def test_client_uses_immediate_message_as_a_successful_response(self):
        message = SimpleNamespace(
            parts=[SimpleNamespace(text="done", HasField=lambda name: name == "text")])
        envelope = SimpleNamespace(message=message, HasField=lambda name: name == "message")

        async def send_message(_request):
            yield envelope

        async def run():
            http = AsyncMock()
            symbols = SimpleNamespace(
                httpx=SimpleNamespace(AsyncClient=lambda **kwargs: http),
                create_client=AsyncMock(return_value=SimpleNamespace(send_message=send_message)),
                ClientConfig=lambda **kwargs: kwargs)
            sdk = SimpleNamespace(
                pb=SimpleNamespace(Message=SimpleNamespace, ROLE_USER=1,
                                   SendMessageRequest=SimpleNamespace),
                ph=SimpleNamespace(new_text_part=lambda text: text))
            with patch.object(a2a_client, "client_symbols", return_value=symbols), \
                    patch.object(a2a_client, "sdk", return_value=sdk):
                return await a2a_client._send_async("http://example.invalid", "hello", None,
                                                    None, None, 1, None)

        response = asyncio.run(run())
        self.assertEqual(response["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(response["text"], "done")

    def test_interactive_client_requests_each_required_answer(self):
        responses = [
            {"needs_input": True, "state": "TASK_STATE_INPUT_REQUIRED",
             "text": "first", "task_id": "t1", "context_id": "c1"},
            {"needs_input": True, "state": "TASK_STATE_INPUT_REQUIRED",
             "text": "second", "task_id": "t1", "context_id": "c1"},
            {"needs_input": False, "state": "TASK_STATE_COMPLETED",
             "text": "done", "task_id": "t1", "context_id": "c1"},
        ]
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=["answer1", "answer2"]), \
                patch.object(a2a_client, "send", side_effect=responses) as send, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(a2a_cli.cli(["call", "http://example.invalid", "hello"]), 0)
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         ["hello", "answer1", "answer2"])

    def test_client_limits_credential_attachment_to_requested_origin(self):
        async def run():
            http = AsyncMock()
            httpx_client = Mock(return_value=http)

            async def open_client(*_args):
                same_site = SimpleNamespace(url=urlparse("https://example.invalid/rpc"), headers={})
                other_site = SimpleNamespace(url=urlparse("https://other.invalid/rpc"), headers={})
                authorize = httpx_client.call_args.kwargs["event_hooks"]["request"][0]
                await authorize(same_site)
                self.assertIn("Authorization", same_site.headers)
                with self.assertRaisesRegex(RuntimeError, "不同源"):
                    await authorize(other_site)
                self.assertEqual(other_site.headers, {})
                return SimpleNamespace(send_message=send_message)

            async def send_message(_request):
                yield SimpleNamespace(HasField=lambda _name: False)

            symbols = SimpleNamespace(
                httpx=SimpleNamespace(AsyncClient=httpx_client, URL=urlparse),
                create_client=open_client, ClientConfig=lambda **kwargs: kwargs)
            sdk = SimpleNamespace(
                pb=SimpleNamespace(Message=SimpleNamespace, ROLE_USER=1,
                                   SendMessageRequest=SimpleNamespace),
                ph=SimpleNamespace(new_text_part=lambda text: text))
            with patch.object(a2a_client, "client_symbols", return_value=symbols), \
                    patch.object(a2a_client, "sdk", return_value=sdk):
                await a2a_client._send_async("https://example.invalid", "hello", None,
                                             None, "fixture-token", 1, None)

        asyncio.run(run())

    def test_bearer_middleware_handles_unrecognized_header_bytes(self):
        async def run():
            app, send = AsyncMock(), AsyncMock()
            middleware = a2a_server.BearerAuthMiddleware(app, "fixture")
            await middleware({"type": "http", "path": "/",
                              "headers": [(b"authorization", "Bearer résumé".encode())]},
                             AsyncMock(), send)
            app.assert_not_awaited()
            self.assertEqual(send.await_args_list[0].args[0]["status"], 401)

        asyncio.run(run())

    def test_session_cache_preserves_incoming_context_when_all_entries_are_busy(self):
        async def run():
            with tempfile.TemporaryDirectory() as folder, \
                    patch.object(core, "STATE_DIR", Path(folder)), \
                    patch.object(a2a_executor, "MAX_SESSIONS", 1), \
                    patch.object(a2a_executor, "sdk", return_value=SimpleNamespace(
                        pb=SimpleNamespace(), ph=SimpleNamespace(), AgentExecutor=object)):
                executor = a2a_executor.build_executor()
                active = executor._session("active")
                with executor._locks[active.id]:
                    with patch.object(executor, "_run", return_value="done"):
                        self.assertEqual(executor._locked_run("next", "hello", False, ""), "done")
                    self.assertIn(active.id, executor._sessions)
                self.assertEqual(len(executor._sessions), 1)

        asyncio.run(run())

    def test_cancel_stops_later_tools_and_keeps_the_canceled_status(self):
        started, finish = threading.Event(), threading.Event()
        updater = SimpleNamespace(start_work=AsyncMock(), update_status=AsyncMock(),
                                  add_artifact=AsyncMock(), complete=AsyncMock(),
                                  failed=AsyncMock())
        fake_sdk = SimpleNamespace(
            pb=SimpleNamespace(TASK_STATE_SUBMITTED=1, TASK_STATE_COMPLETED=2,
                               TASK_STATE_FAILED=3, TASK_STATE_CANCELED=4,
                               TASK_STATE_REJECTED=5, TASK_STATE_INPUT_REQUIRED=6),
            ph=SimpleNamespace(new_task=lambda **kw: kw,
                               new_text_message=lambda text, **kw: text),
            AgentExecutor=object, TaskUpdater=lambda *args: updater)
        ctx = SimpleNamespace(task_id="next", context_id="fixture", current_task=None,
                              get_user_input=lambda: "inspect", message=None)
        reply = {"role": "assistant", "content": "",
                 "tool_calls": [
                     {"id": name, "type": "function",
                      "function": {"name": name, "arguments": "{}"}}
                     for name in ("first", "second")]}

        def dispatch(name, *_args):
            self.assertEqual(name, "first")
            started.set()
            if not finish.wait(5):
                raise TimeoutError("fixture release")
            return "done"

        async def run():
            with tempfile.TemporaryDirectory() as folder, \
                    patch.object(core, "STATE_DIR", Path(folder)), \
                    patch.object(a2a_executor, "sdk", return_value=fake_sdk), \
                    patch.object(llm, "chat", return_value=reply), \
                    patch.object(tools, "dispatch", side_effect=dispatch) as tool:
                executor = a2a_executor.build_executor(max_concurrent=1)
                queue = SimpleNamespace(enqueue_event=AsyncMock())
                running = asyncio.create_task(executor.execute(ctx, queue))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    await executor.cancel(ctx, queue)
                    # SDK 在发布取消状态后取消协议协程，线程仍占用一个执行槽。
                    running.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await running
                    self.assertTrue(executor._sem.locked())
                finally:
                    finish.set()
                    if not running.done():
                        await running
                await asyncio.wait_for(executor._sem.acquire(), timeout=2)
                executor._sem.release()
                tool.assert_called_once()
                updater.update_status.assert_awaited_once()
                self.assertEqual(updater.update_status.await_args.args[0],
                                 fake_sdk.pb.TASK_STATE_CANCELED)
                updater.add_artifact.assert_not_awaited()
                updater.complete.assert_not_awaited()
                updater.failed.assert_not_awaited()

        asyncio.run(run())

    def test_regular_reply_and_events_are_redacted_before_delivery(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(core, "STATE_DIR", Path(folder)), \
                patch.dict(os.environ, {"OPSAGENT_API_KEY": "fixture-example-value"}):
            conversation = session.Session()
            provider_response = {"role": "assistant", "content": "fixture-example-value",
                                 "tool_calls": [{"id": "1", "type": "function",
                                                 "function": {"name": "probe", "arguments":
                                                              '{"query":"fixture-example-value"}'}}]}
            end_response = {"role": "assistant", "content": "fixture-example-value"}
            events = []
            with patch.object(llm, "chat", side_effect=[provider_response, end_response]), \
                    patch.object(tools, "dispatch", return_value="done"):
                response = loop.run_turn("hello", conversation, tools.ToolContext(),
                                         on_event=lambda kind, value: events.append((kind, value)))
            self.assertNotIn("fixture-example-value", response)
            self.assertNotIn("fixture-example-value", json.dumps(events))
            self.assertNotIn("fixture-example-value", conversation.path.read_text())

    def test_provider_response_read_has_a_limit(self):
        payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        with patch.dict(os.environ, {"OPSAGENT_PROVIDER": "openai"}), \
                patch.object(llm, "MAX_RESPONSE_BYTES", 16, create=True), \
                patch.object(llm, "_post", return_value=io.BytesIO(payload.encode())):
            with self.assertRaisesRegex(llm.LLMError, "响应.*上限"):
                llm._chat_once([{"role": "user", "content": "hello"}], None, None, 1, None)

    def test_bounded_provider_read_rejects_a_short_http_body(self):
        body = b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
        wire = (f"HTTP/1.1 200 OK\r\nContent-Length: {len(body) + 4}\r\n\r\n".encode()
                + body)
        response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda _mode: io.BytesIO(wire)))
        response.begin()
        with patch.dict(os.environ, {"OPSAGENT_PROVIDER": "openai"}), \
                patch.object(llm, "_post", return_value=response):
            with self.assertRaisesRegex(llm.LLMError, "读取失败"):
                llm._chat_once([{"role": "user", "content": "hello"}], None, None, 1, None)

    def test_oneshot_safely_displays_terminal_output_and_preserves_pipe_output(self):
        cli = runpy.run_path(str(Path(__file__).resolve().parents[1] / "opsagent"))
        message = "fixture\x1b"
        for tty in (True, False):
            with self.subTest(tty=tty), \
                    patch.object(session, "Session", return_value=SimpleNamespace(id="fixture")), \
                    patch.object(repl, "_terminal_ask", return_value="no") as ask, \
                    patch.object(loop, "run_turn", return_value=message) as turn, \
                    patch("sys.stdin.isatty", return_value=tty), \
                    redirect_stdout(io.StringIO()) as output, \
                    patch.object(output, "isatty", return_value=tty):
                self.assertEqual(cli["_print_mode"]("inspect", None), 0)
                self.assertIs(turn.call_args.args[2].ask_user, ask if tty else None)
                self.assertEqual(output.getvalue(), (r"fixture\x1b" if tty else message) + "\n")

    def test_cancel_stops_a_worker_waiting_for_the_session_lock(self):
        entered = threading.Event()
        cancelled = threading.Event()

        class SessionLock:
            def acquire(self, *, timeout):
                entered.set()
                # Simulate a held lock that can only time out.
                threading.Event().wait(timeout)
                return False

        async def run():
            sdk = SimpleNamespace(pb=SimpleNamespace(), ph=SimpleNamespace(), AgentExecutor=object)
            with tempfile.TemporaryDirectory() as folder, \
                    patch.object(core, "STATE_DIR", Path(folder)), \
                    patch.object(a2a_executor, "sdk", return_value=sdk):
                executor = a2a_executor.build_executor(timeout=2)
                sess = executor._session("fixture")
                executor._locks[sess.id] = SessionLock()
                worker = asyncio.create_task(asyncio.to_thread(
                    executor._locked_run, "fixture", "inspect", False, "", "fixture", cancelled))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    cancelled.set()
                    with self.assertRaises(loop.TurnCancelled):
                        await asyncio.wait_for(asyncio.shield(worker), timeout=1)
                finally:
                    await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(run())

    def test_split_provider_chunks_are_redacted_before_the_frontend_callback(self):
        chunks = [
            {"choices": [{"delta": {"content": "fixture-plain-"}}]},
            {"choices": [{"delta": {"content": "value"}, "finish_reason": "stop"}]},
        ]
        payload = "".join("data: " + json.dumps(chunk) + "\n" for chunk in chunks)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(core, "STATE_DIR", Path(folder)), \
                patch.dict(os.environ, {"OPSAGENT_PROVIDER": "openai",
                                        "OPSAGENT_API_KEY": "fixture-plain-value"}), \
                patch.object(llm, "_post", return_value=io.BytesIO(payload.encode())):
            conversation = session.Session()
            output = []
            self.assertEqual(loop.run_turn("hello", conversation, tools.ToolContext(),
                                           on_delta=output.append), core.REDACTED)
            self.assertEqual(output, [core.REDACTED])

    def test_terminal_displays_control_characters_visibly(self):
        message = "café\u202eexample\x1b\x07"
        self.assertIn(markdown.BOLD, markdown.render(f"**{message}**"))
        self.assertEqual(markdown.render(message, color=False),
                         r"café\u202eexample\x1b\x07")
        with patch("builtins.input", return_value="no"), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(repl._terminal_ask(message), "no")
        self.assertIn(r"\u202eexample\x1b\x07", output.getvalue())


if __name__ == "__main__":
    unittest.main()
