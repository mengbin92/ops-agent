"""Regression coverage for the 2026-10-24 agent and session review."""
from __future__ import annotations

import json
import io
import contextlib
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import core
from agent import llm, llm_anthropic, loop, memory, rpc, session, tools


class AgentReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ops-agent-review-")
        self.addCleanup(self.tmp.cleanup)
        state = mock.patch.object(core, "STATE_DIR", Path(self.tmp.name))
        state.start()
        self.addCleanup(state.stop)

    def test_imported_memory_is_redacted_before_model_request(self):
        secret = "sk-IMPORTEDSECRET0123456789"
        original = memory.HEADER + f"- api_key={secret}\n- db-01: OS = Linux\n"
        memory.path().write_text(original, encoding="utf-8")
        requests = []

        def chat(messages, **kwargs):
            requests.append(messages)
            return {"role": "assistant", "content": "已核实"}

        with mock.patch.object(loop.llm, "chat", side_effect=chat):
            loop.run_turn("读取现有事实", session.Session(), tools.ToolContext())
        self.assertNotIn(secret, json.dumps(requests))
        self.assertIn(core.REDACTED, requests[0][0]["content"])
        self.assertIn("db-01: OS = Linux", requests[0][0]["content"])
        self.assertEqual(memory.path().read_text(encoding="utf-8"), original)

    def test_memory_redaction_precedes_tail_truncation(self):
        secret = ("Q" * 100 + "\n") * 100
        memory.path().write_text("-----BEGIN PRIVATE KEY-----\n" + secret
                                 + "-----END PRIVATE KEY-----\n- useful fact\n",
                                 encoding="utf-8")
        self.assertNotIn("Q" * 20, memory.recall())
        self.assertIn("useful fact", memory.recall())

    def write_session(self, record):
        path = core.STATE_DIR / "sessions" / "legacy.jsonl"
        path.parent.mkdir(exist_ok=True)
        header = {"format": 1, "id": "legacy"}
        path.write_text(json.dumps(header) + "\n" + json.dumps(record) + "\n",
                        encoding="utf-8")
        return path

    def test_resume_rejects_malformed_tool_calls_with_controlled_error(self):
        invalid = [[], "bad", {"function": {"name": "ops_list"}},
                   {"id": "c1", "function": {"name": "ops_list", "arguments": {}}}]
        for call in invalid:
            with self.subTest(call=call):
                self.write_session({"role": "assistant", "content": "",
                                    "tool_calls": [call]})
                with self.assertRaisesRegex(ValueError, "会话"):
                    session.Session("legacy")
                self.assertEqual(session.list_sessions(), [])

    def test_resume_rejects_invalid_message_content_and_call_container(self):
        invalid = [{"role": "user", "content": {"unexpected": "object"}},
                   {"role": [], "content": "bad role"},
                   {"role": {}, "content": "bad role"},
                   {"role": "assistant", "content": "", "tool_calls": {}},
                   {"role": "assistant", "content": "", "tool_calls": "bad"}]
        for record in invalid:
            with self.subTest(record=record):
                self.write_session(record)
                with self.assertRaisesRegex(ValueError, "会话"):
                    session.Session("legacy")

    def test_resume_preserves_valid_unfinished_call_and_legacy_tool_record(self):
        call = {"id": "pending", "type": "function", "function": {
            "name": "ops_exec", "arguments": '{"command":"systemctl restart nginx"}'}}
        self.write_session({"role": "assistant", "content": "", "tool_calls": [call]})
        restored = session.Session("legacy")
        requests = []
        with mock.patch.object(loop.llm, "chat", side_effect=lambda messages, **kwargs: (
                requests.append(messages) or {"role": "assistant", "content": "先核实"})), \
                mock.patch.object(tools, "dispatch") as dispatch:
            loop.run_turn("继续", restored, tools.ToolContext())
        dispatch.assert_not_called()
        self.assertTrue(any(m.get("tool_call_id") == "pending"
                            and "勿直接重试写操作" in m["content"] for m in requests[0]))
        self.write_session({"role": "tool", "content": "旧版工具输出"})
        self.assertEqual(session.Session("legacy").messages[0]["content"], "旧版工具输出")

    def test_unanswered_callback_stays_a_valid_tool_result(self):
        script = [{"role": "assistant", "content": "", "tool_calls": [{
            "id": "ask", "function": {"name": "ask_user",
            "arguments": '{"question":"环境是什么？"}'}}]},
            {"role": "assistant", "content": "等待环境信息"}]
        conversation = session.Session()
        with mock.patch.object(loop.llm, "chat", side_effect=script):
            result = loop.run_turn("检查服务", conversation,
                                   tools.ToolContext(ask_user=lambda _: None))
        self.assertEqual(result, "等待环境信息")
        self.assertIn("视为用户拒绝", conversation.messages[-2]["content"])
        self.assertEqual(session.Session(conversation.id).messages, conversation.messages)

    def test_deep_rpc_json_does_not_terminate_reader(self):
        deep = "[" * 2000 + "0" + "]" * 2000
        ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        loads = json.loads

        def decoding_limit(value, **kwargs):
            if value == deep:
                raise RecursionError("decoder recursion limit")
            return loads(value, **kwargs)

        # Decoder recursion limits differ between Python versions. Exercise real input
        # and independently force the failure branch without depending on that limit.
        for force_limit in (False, True):
            with self.subTest(force_limit=force_limit):
                output = io.StringIO()
                decoding = mock.patch.object(rpc.json, "loads", side_effect=decoding_limit) \
                    if force_limit else contextlib.nullcontext()
                with decoding, \
                        mock.patch.object(sys, "stdin", io.StringIO(deep + "\n" + ping + "\n")), \
                        mock.patch.object(sys, "stdout", output):
                    rpc.serve()
                replies = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertIn(replies[0]["error"]["code"], {-32700, -32600})
                if force_limit:
                    self.assertEqual(replies[0]["error"]["code"], -32700)
                self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_deep_session_json_is_hidden_from_resume_picker(self):
        path = self.write_session({"role": "user", "content": "before"})
        with path.open("a") as handle:
            handle.write("[" * 2000 + "0" + "]" * 2000 + "\n")
        with self.assertRaisesRegex(ValueError, "会话"):
            session.Session("legacy")
        self.assertEqual(session.list_sessions(), [])
        loads = json.loads

        def decoding_limit(value):
            if value.startswith(b"["):
                raise RecursionError("decoder recursion limit")
            return loads(value)

        with mock.patch.object(session.json, "loads", side_effect=decoding_limit):
            with self.assertRaisesRegex(ValueError, "会话文件损坏"):
                session.Session("legacy")

    def test_deep_tool_arguments_complete_or_report_controlled_error(self):
        arguments = '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"
        script = [{"role": "assistant", "content": "", "tool_calls": [{
            "id": "bad", "function": {"name": "ops_list", "arguments":
            arguments}}]}, {"role": "assistant", "content": "参数已处理"}]
        loads = json.loads

        def decoding_limit(value):
            if value == arguments:
                raise RecursionError("decoder recursion limit")
            return loads(value)

        for force_limit in (False, True):
            with self.subTest(force_limit=force_limit):
                conversation = session.Session()
                decoding = mock.patch.object(loop.json, "loads", side_effect=decoding_limit) \
                    if force_limit else contextlib.nullcontext()
                with decoding, mock.patch.object(loop.llm, "chat", side_effect=script), \
                        mock.patch.object(tools, "dispatch", return_value="state") as dispatch:
                    self.assertEqual(loop.run_turn("检查", conversation, tools.ToolContext()),
                                     "参数已处理")
                if dispatch.called:
                    self.assertFalse(force_limit)
                    self.assertIsInstance(dispatch.call_args.args[1]["nested"], list)
                    self.assertEqual(conversation.messages[-2]["content"], "state")
                else:
                    self.assertIn("有效 JSON 对象", conversation.messages[-2]["content"])

    def test_deep_provider_json_is_a_controlled_llm_error(self):
        payload = b"[" * 2000 + b"0" + b"]" * 2000
        for provider in ("openai", "anthropic"):
            for force_limit in (False, True):
                with self.subTest(provider=provider, force_limit=force_limit):
                    decoding = mock.patch.object(llm.json, "loads", side_effect=RecursionError(
                        "decoder recursion limit")) if force_limit else contextlib.nullcontext()
                    with decoding, \
                            mock.patch.dict("os.environ", {"OPSAGENT_PROVIDER": provider,
                                                            "OPSAGENT_API_KEY": "review-key"}), \
                            mock.patch.object(llm, "_post", return_value=io.BytesIO(payload)), \
                            mock.patch.object(llm_anthropic, "_request", return_value=io.BytesIO(payload)):
                        with self.assertRaisesRegex(llm.LLMError, "响应") as error:
                            llm._chat_once([{"role": "user", "content": "hi"}], None, None, 10, None)
                    if force_limit:
                        self.assertIn("响应解析失败", str(error.exception))

    def test_anthropic_can_continue_after_invalid_tool_arguments(self):
        for arguments in ("null", "[1,2]", "true", "broken",
                          '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"):
            with self.subTest(arguments=arguments[:30]):
                history = [{"role": "user", "content": "检查"},
                           {"role": "assistant", "content": "", "tool_calls": [{
                               "id": "bad", "function": {"name": "ops_list",
                                                            "arguments": arguments}}]},
                           {"role": "tool", "tool_call_id": "bad", "content": "参数错误"}]
                body = llm_anthropic._body(history, None, None, False)
                try:
                    expected = json.loads(arguments)
                except (ValueError, RecursionError):
                    expected = {}
                if not isinstance(expected, dict):
                    expected = {}
                # Compare serialized forms; unittest's recursive diff has a separate
                # recursion limit unrelated to what the provider can handle.
                self.assertEqual(json.dumps(body["messages"][1]["content"][0]["input"]),
                                 json.dumps(expected))
                self.assertEqual(body["messages"][2]["content"][0]["content"], "参数错误")
        with mock.patch.object(llm_anthropic.json, "loads", side_effect=RecursionError(
                "decoder recursion limit")):
            blocks = llm_anthropic._tool_use_blocks([{"id": "bad", "function": {
                "name": "ops_list", "arguments": '{"nested":[]}'}}])
        self.assertEqual(blocks[0]["input"], {})

    @staticmethod
    def nested_metadata(depth=600):
        value = "leaf"
        for _ in range(depth):
            value = {"nested": value}
        return value

    def test_decodable_nested_session_metadata_loads_or_is_controlled(self):
        record = {"role": "user", "content": "message", "metadata": self.nested_metadata()}
        path = self.write_session(record)
        # The JSON decoder succeeds; the failure used to occur later while redacting.
        json.loads(path.read_text().splitlines()[1])
        try:
            loaded = session.Session("legacy")
        except ValueError as error:
            self.assertIn("会话消息嵌套过深", str(error))
            self.assertEqual(session.list_sessions(), [])
        else:
            self.assertEqual(loaded.messages[0]["content"], "message")
            self.assertEqual(session.list_sessions()[0]["id"], "legacy")
        with mock.patch.object(core, "redact_obj", side_effect=RecursionError(
                "redaction recursion limit")):
            with self.assertRaisesRegex(ValueError, "会话消息嵌套过深"):
                session.Session("legacy")
            self.assertEqual(session.list_sessions(), [])

    def test_append_nested_metadata_is_atomic_on_redaction_failure(self):
        conversation = session.Session()
        record = {"role": "user", "content": "message", "metadata": self.nested_metadata()}
        try:
            conversation.append(record)
        except ValueError as error:
            self.assertIn("会话消息嵌套过深", str(error))
            self.assertEqual(conversation.messages, [])
            self.assertFalse(conversation.path.exists())
        else:
            self.assertEqual(conversation.messages[0]["content"], "message")
            self.assertEqual(session.Session(conversation.id).messages[0]["content"], "message")
        conversation = session.Session()
        conversation.append({"role": "user", "content": "before"})
        before = conversation.path.read_bytes()
        with mock.patch.object(core, "redact_obj", side_effect=RecursionError(
                "redaction recursion limit")):
            with self.assertRaisesRegex(ValueError, "会话消息嵌套过深"):
                conversation.append(record)
        self.assertEqual(conversation.messages, [{"role": "user", "content": "before"}])
        self.assertEqual(conversation.path.read_bytes(), before)

    def test_provider_nested_metadata_is_discarded_before_persistence(self):
        metadata = self.nested_metadata()
        call = {"id": "list", "type": "function", "metadata": metadata,
                "function": {"name": "ops_list", "arguments": "{}", "metadata": metadata}}
        script = [{"role": "assistant", "content": "", "tool_calls": [call]},
                  {"role": "assistant", "content": "已完成"}]
        conversation = session.Session()
        with mock.patch.object(loop.llm, "chat", side_effect=script), \
                mock.patch.object(tools, "dispatch", return_value="state") as dispatch:
            self.assertEqual(loop.run_turn("检查", conversation, tools.ToolContext()), "已完成")
        dispatch.assert_called_once()
        saved_call = conversation.messages[1]["tool_calls"][0]
        self.assertEqual(set(saved_call), {"id", "type", "function"})
        self.assertEqual(set(saved_call["function"]), {"name", "arguments"})
        self.assertEqual(session.Session(conversation.id).messages, conversation.messages)

    def test_decodable_nested_rpc_params_and_tool_arguments_can_complete(self):
        metadata = self.nested_metadata()
        request = {"jsonrpc": "2.0", "id": 7, "method": "prompt", "params": {
            "text": "检查", "stream": True, "metadata": metadata}}
        script = [{"role": "assistant", "content": "", "tool_calls": [{
            "id": "list", "function": {"name": "ops_list", "arguments":
                json.dumps({"metadata": metadata})}}]},
            {"role": "assistant", "content": "已完成"}]
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), \
                mock.patch.object(sys, "stdout", output), \
                mock.patch.object(loop.llm, "chat", side_effect=script):
            rpc.serve()
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(replies[-1]["id"], 7)
        self.assertEqual(replies[-1]["result"]["text"], "已完成")
        self.assertTrue(any(reply.get("params", {}).get("kind") == "tool" for reply in replies))

    def test_rpc_rejects_overflowed_ids_and_preserves_finite_ids(self):
        invalid = ['{"jsonrpc":"2.0","id":1e999,"method":"ping"}',
                   '{"jsonrpc":"2.0","id":-1e999,"method":"ping"}']
        valid = [{"jsonrpc": "2.0", "id": value, "method": "ping"}
                 for value in (1, 1.25, "request-3", None)]
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(
                "\n".join(invalid + [json.dumps(msg) for msg in valid]) + "\n")), \
                mock.patch.object(sys, "stdout", output):
            rpc.serve()
        replies = [json.loads(line, parse_constant=rpc._invalid_json_constant)
                   for line in output.getvalue().splitlines()]
        invalid_replies = [reply for reply in replies if "error" in reply]
        self.assertEqual(len(invalid_replies), 2)
        for reply in invalid_replies:
            self.assertEqual(reply, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32600, "message": "Invalid Request"}})
        self.assertEqual([reply["id"] for reply in replies if "result" in reply],
                         [1, 1.25, "request-3", None])


if __name__ == "__main__":
    unittest.main()
