"""OpenAI 兼容 LLM provider：纯 stdlib（urllib），M0 非流式。

环境变量（统一 OPSAGENT_ 前缀，不绑定任何具体厂商）：
  OPSAGENT_API_KEY   API 密钥（必填）
  OPSAGENT_BASE_URL  OpenAI 兼容端点，默认 https://api.deepseek.com，
                     可指向任意兼容服务（DeepSeek/OpenAI/Kimi/Qwen/vLLM 等）
  OPSAGENT_MODEL     模型名，默认 deepseek-chat（随 BASE_URL 指向的服务而定）
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


def chat(messages: list[dict], tools: list[dict] | None = None,
         model: str | None = None, timeout: int = 300) -> dict:
    """调用 /chat/completions（非流式），返回 message dict（含可选 tool_calls）。"""
    base = os.environ.get("OPSAGENT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    body: dict = {
        "model": model or os.environ.get("OPSAGENT_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_api_key()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise LLMError(f"LLM API HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise LLMError(f"LLM API 请求失败: {e}") from e
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"LLM 响应格式异常: {json.dumps(data)[:500]}") from e
