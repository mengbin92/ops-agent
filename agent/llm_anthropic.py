"""Anthropic 原生 Messages API provider（纯 stdlib），OPSAGENT_PROVIDER=anthropic 启用。

agent loop 只认 OpenAI 形状，本模块在边界处双向转换：
  messages：system 抽为顶层 system；assistant.tool_calls → tool_use 块；
            role=tool → user 消息里的 tool_result 块（连续工具结果合并）。
  tools：   {type:function, function:{name,description,parameters}}
            → {name, description, input_schema}
  响应：    text 块 → content；tool_use 块 → tool_calls（arguments 为 JSON 字符串）。

BASE_URL 末尾带 /v1 时按代理惯例只补 /messages，否则补 /v1/messages。
"""
from __future__ import annotations

import json
import http.client
import os

from .llm import LLMError, _api_key, _request, _validate_message

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-sonnet-4-5"
API_VERSION = "2023-06-01"
MAX_TOKENS = 8192


def _endpoint() -> str:
    base = os.environ.get("OPSAGENT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    return base + ("/messages" if base.endswith("/v1") else "/v1/messages")


def _headers() -> dict:
    return {"Content-Type": "application/json", "x-api-key": _api_key(),
            "anthropic-version": API_VERSION}


def _text_blocks(text: str) -> list[dict]:
    return [{"type": "text", "text": text}] if text else []


def _append(messages: list[dict], role: str, blocks: list[dict]) -> None:
    """追加消息；Anthropic 要求 user/assistant 交替，连续同角色合并为一条。"""
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})


def _tool_use_blocks(tool_calls: list[dict]) -> list[dict]:
    blocks = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            inp = {}
        blocks.append({"type": "tool_use", "id": tc.get("id") or "",
                       "name": fn.get("name") or "", "input": inp})
    return blocks


def _build_messages(messages: list[dict]) -> tuple[list[str], list[dict]]:
    system: list[str] = []
    out: list[dict] = []
    pending_results: list[dict] = []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "system":
            if content:
                system.append(content)
            continue
        if role == "tool":
            pending_results.append({"type": "tool_result",
                                    "tool_use_id": m.get("tool_call_id") or "",
                                    "content": content or "(无输出)"})
            continue
        _append(out, "user", pending_results)  # 工具结果必须先于后续消息回填
        pending_results = []
        if role == "assistant":
            blocks = _text_blocks(content) + _tool_use_blocks(m.get("tool_calls") or [])
            _append(out, "assistant", blocks)
        else:
            _append(out, "user", _text_blocks(content))
    _append(out, "user", pending_results)
    if not out:
        raise LLMError("Anthropic 请求缺少对话消息")
    return system, out


def _convert_tools(tools: list[dict] | None) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function") or {}
        out.append({"name": fn.get("name") or "",
                    "description": fn.get("description") or "",
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _body(messages: list[dict], tools: list[dict] | None, model: str | None,
          stream: bool) -> dict:
    system, converted = _build_messages(messages)
    body: dict = {"model": model or os.environ.get("OPSAGENT_MODEL", DEFAULT_MODEL),
                  "max_tokens": MAX_TOKENS, "messages": converted, "stream": stream}
    if system:
        body["system"] = "\n\n".join(system)
    converted_tools = _convert_tools(tools)
    if converted_tools:
        body["tools"] = converted_tools
    return body


def _message(data: dict) -> dict:
    """Anthropic 响应 → OpenAI 形状 message。"""
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise LLMError("LLM 响应格式异常：缺少 content 数组")
    if data.get("stop_reason") == "max_tokens":
        raise LLMError("LLM 响应未完成: max_tokens")
    content_parts, tool_calls = [], []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            raise LLMError("LLM 响应格式异常：无效内容块")
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise LLMError("LLM 响应格式异常：text 必须是字符串")
            content_parts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            if not isinstance(block.get("input"), dict):
                raise LLMError("LLM 响应格式异常：工具 input 必须是对象")
            tool_calls.append({"id": block.get("id") or "", "type": "function",
                               "function": {"name": block.get("name") or "",
                                            "arguments": json.dumps(block.get("input") or {},
                                                                    ensure_ascii=False)}})
    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if not msg["content"] and not tool_calls:
        raise LLMError(f"LLM 响应格式异常: {json.dumps(data)[:500]}")
    return _validate_message(msg)


def _chat_stream(body: dict, timeout: int, on_delta) -> dict:
    content_parts: list[str] = []
    tools_acc: dict[int, dict] = {}  # 块 index → 未完成的 tool_use
    finished = False
    with _request(_endpoint(), body, _headers(), timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):  # event: / ping 等行跳过
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError as e:
                raise LLMError(f"LLM 流式响应解析失败: {e}") from e
            if not isinstance(event, dict):
                raise LLMError("LLM 流式响应格式异常")
            etype = event.get("type")
            if etype == "error":
                raise LLMError(f"LLM 流式错误: {json.dumps(event)[:500]}")
            if etype == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "text" and block.get("text"):
                    if not isinstance(block["text"], str):
                        raise LLMError("LLM 流式响应格式异常：text 必须是字符串")
                    content_parts.append(block["text"])
                    on_delta(block["text"])
                elif block.get("type") == "tool_use":
                    tools_acc[event.get("index", 0)] = {
                        "id": block.get("id") or "", "name": block.get("name") or "",
                        "json": "", "input": block.get("input") or {}}
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    piece = delta.get("text") or ""
                    if not isinstance(piece, str):
                        raise LLMError("LLM 流式响应格式异常：text 必须是字符串")
                    if piece:
                        content_parts.append(piece)
                        on_delta(piece)
                elif delta.get("type") == "input_json_delta":
                    acc = tools_acc.setdefault(event.get("index", 0),
                                               {"id": "", "name": "", "json": "", "input": {}})
                    acc["json"] += delta.get("partial_json") or ""
            elif etype == "message_delta" and (event.get("delta") or {}).get("stop_reason") == "max_tokens":
                raise LLMError("LLM 流式响应未完成: max_tokens")
            elif etype == "message_stop":
                finished = True
                break
    if not content_parts and not tools_acc:
        raise LLMError("流式响应为空（端点可能不支持 stream，或返回了非 SSE 内容）")
    if not finished:
        raise LLMError("LLM 流式响应中断，未收到结束标记")
    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tools_acc:
        msg["tool_calls"] = []
        for idx in sorted(tools_acc):
            acc = tools_acc[idx]
            arguments = acc["json"] or json.dumps(acc["input"], ensure_ascii=False)
            msg["tool_calls"].append({"id": acc["id"], "type": "function",
                                      "function": {"name": acc["name"], "arguments": arguments}})
    return _validate_message(msg)


def chat(messages: list[dict], tools: list[dict] | None = None,
         model: str | None = None, timeout: int = 300, on_delta=None) -> dict:
    body = _body(messages, tools, model, stream=on_delta is not None)
    try:
        if on_delta is not None:
            return _chat_stream(body, timeout, on_delta)
        with _request(_endpoint(), body, _headers(), timeout) as resp:
            data = json.loads(resp.read().decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LLMError(f"LLM 响应解析失败: {e}") from e
    except (OSError, http.client.HTTPException) as e:
        raise LLMError(f"LLM API 读取失败: {e}") from e
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError(f"LLM 流式响应格式异常: {e}") from e
    return _message(data)
