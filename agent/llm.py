"""LLM provider 分发：OpenAI 兼容（本模块）与 Anthropic 原生（llm_anthropic.py）。

环境变量（统一 OPSAGENT_ 前缀，不绑定任何具体厂商）：
  OPSAGENT_PROVIDER   openai（默认）| anthropic
  OPSAGENT_API_KEY    API 密钥（必填）
  OPSAGENT_BASE_URL   端点，默认随 provider 而定（OpenAI 兼容 → api.deepseek.com，
                      可指向任意兼容服务；anthropic → api.anthropic.com）
  OPSAGENT_MODEL      模型名，默认随 provider 而定

chat(on_delta=None)  → 非流式，一次返回完整 message。
chat(on_delta=fn)    → SSE 流式：每个 content 增量回调 fn(text)，
                       tool_calls 增量在内部组装，返回完整 message。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class LLMError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("OPSAGENT_API_KEY")
    if not key:
        raise LLMError("缺少 API key：请设置 OPSAGENT_API_KEY（配合 OPSAGENT_BASE_URL 指向的兼容服务）")
    return key


def _request(url: str, body: dict, headers: dict, timeout: int):
    """POST JSON 并返回响应对象；网络/HTTP 错误统一转 LLMError。"""
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise LLMError(f"LLM API HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMError(f"LLM API 请求失败: {e}") from e


def _post(body: dict, timeout: int):
    base = os.environ.get("OPSAGENT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    return _request(base + "/chat/completions", body,
                    {"Content-Type": "application/json",
                     "Authorization": f"Bearer {_api_key()}"}, timeout)


def _accumulate_tool_call(tools_acc: dict, delta: dict) -> None:
    """流式 tool_calls 增量按 index 组装。"""
    idx = delta.get("index", 0)
    acc = tools_acc.setdefault(idx, {"id": "", "type": "function",
                                     "function": {"name": "", "arguments": ""}})
    if delta.get("id"):
        acc["id"] = delta["id"]
    fn = delta.get("function") or {}
    if fn.get("name"):
        acc["function"]["name"] += fn["name"]
    if fn.get("arguments"):
        acc["function"]["arguments"] += fn["arguments"]


def _chat_stream(body: dict, timeout: int, on_delta) -> dict:
    content_parts: list[str] = []
    tools_acc: dict[int, dict] = {}
    with _post(body, timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue  # 忽略心跳/注释等无法解析的行
            delta = (event.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
                on_delta(piece)
            for tc in delta.get("tool_calls") or []:
                _accumulate_tool_call(tools_acc, tc)
    if not content_parts and not tools_acc:
        raise LLMError("流式响应为空（端点可能不支持 stream，或返回了非 SSE 内容）")
    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tools_acc:
        msg["tool_calls"] = [tools_acc[i] for i in sorted(tools_acc)]
    return msg


def chat(messages: list[dict], tools: list[dict] | None = None,
         model: str | None = None, timeout: int = 300, on_delta=None) -> dict:
    """调用当前 provider，返回 OpenAI 形状的 message dict（含可选 tool_calls）。

    provider 由 OPSAGENT_PROVIDER 选择；两种 provider 的返回值对 agent loop 无差别。
    """
    provider = os.environ.get("OPSAGENT_PROVIDER", "openai").strip().lower() or "openai"
    if provider == "anthropic":
        from . import llm_anthropic  # 延迟导入：默认 provider 不加载
        return llm_anthropic.chat(messages, tools, model, timeout, on_delta)
    if provider != "openai":
        raise LLMError(f"未知 provider: {provider}（可选 openai / anthropic）")
    body: dict = {
        "model": model or os.environ.get("OPSAGENT_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "stream": on_delta is not None,
    }
    if tools:
        body["tools"] = tools
    if on_delta is not None:
        return _chat_stream(body, timeout, on_delta)
    try:
        with _post(body, timeout) as resp:
            data = json.loads(resp.read().decode())
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM 响应解析失败: {e}") from e
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"LLM 响应格式异常: {json.dumps(data)[:500]}") from e
