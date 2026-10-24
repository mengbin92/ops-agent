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
import http.client
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402  # OpsxError：预算配置损坏时的统一异常
import usage  # noqa: E402  # 顶层模块：预算与记账（与 agent 包平级）

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# provider 临时错误退避：429/5xx/网络瞬断重试，401/400 这类永久错误绝不重试
TRANSIENT_RETRIES = 2
TRANSIENT_BASE = 3.0
TRANSIENT_CAP = 30.0
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


class LLMError(Exception):
    def __init__(self, message):
        # 代理的错误正文可能原样回显密钥；已知配置值不依赖正则格式识别。
        message = str(message)
        key = os.environ.get("OPSAGENT_API_KEY", "")
        if len(key) > 4:
            message = message.replace(key, core.REDACTED)
        super().__init__(core.redact(message))


class BudgetExhausted(LLMError):
    """日预算耗尽：LLMError 的子类（既有调用方照常接住），但跨天才恢复，
    重试无意义——schedule 的执行循环对它不重试，只报告。"""


def is_budget_exhausted(err: BaseException) -> bool:
    return isinstance(err, BudgetExhausted)


def is_transient(err: LLMError) -> bool:
    """临时错误判定：HTTP 状态码优先；网络层错误按文案归类。"""
    text = str(err)
    m = _HTTP_STATUS_RE.search(text)
    if m:
        return int(m.group(1)) in _TRANSIENT_STATUS
    return ("请求失败" in text or "读取失败" in text or "timed out" in text
            or "TimeoutError" in text)


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动：3s, 6s,（封顶 30s），抖动避免多个任务同一秒重试。"""
    base = min(TRANSIENT_CAP, TRANSIENT_BASE * (2 ** attempt))
    return base + random.uniform(0, TRANSIENT_BASE * 0.5)


def _alert_budget_once(reason: str) -> None:
    """预算耗尽每天只报一次事件（去重标记在 usage-state 的锁内维护）。"""
    try:
        if usage.mark_budget_alerted():
            from agent import events
            events.append("budget-exhausted", reason=reason[:300])
    except Exception:  # noqa: BLE001 —— 告警旁路失败不能影响预算阻断本身
        pass


def _api_key() -> str:
    key = os.environ.get("OPSAGENT_API_KEY")
    if not key:
        raise LLMError("缺少 API key：请设置 OPSAGENT_API_KEY（配合 OPSAGENT_BASE_URL 指向的兼容服务）")
    return key


def _request(url: str, body: dict, headers: dict, timeout: int):
    """POST JSON 并返回响应对象；网络/HTTP 错误统一转 LLMError。"""
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise LLMError(f"LLM API HTTP {e.code}: {detail}") from e
    except ValueError as e:
        raise LLMError(f"LLM API 配置无效: {e}") from e
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


def _validate_message(msg: dict) -> dict:
    """模型响应是执行边界；不完整的调用不能进入会话或工具分发。"""
    if not isinstance(msg, dict) or msg.get("role") != "assistant" \
            or (msg.get("content") is not None and not isinstance(msg["content"], str)):
        raise LLMError("LLM 响应格式异常：缺少有效 assistant 消息")
    calls = msg.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise LLMError("LLM 响应格式异常：tool_calls 必须为数组")
    ids = set()
    normalized_calls = []
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                or not call["id"] or call["id"] in ids or not isinstance(fn, dict)
                or call.get("type", "function") != "function"
                or not isinstance(fn.get("name"), str) or not fn["name"]
                or not isinstance(fn.get("arguments"), str)):
            raise LLMError("LLM 响应格式异常：工具调用缺少 id、名称或参数")
        ids.add(call["id"])
        normalized_calls.append({"id": call["id"], "type": "function", "function": {
            "name": fn["name"], "arguments": fn["arguments"]}})
    if not msg.get("content") and not calls:
        raise LLMError("LLM 响应为空")
    # 仅支持的协议字段进入会话与下次请求。额外 provider metadata 既不参与执行，
    # 也不应将任意深度对象带进递归脱敏/持久化路径。
    normalized = {"role": "assistant", "content": msg.get("content")}
    if normalized_calls:
        normalized["tool_calls"] = normalized_calls
    return normalized


def _usage_tokens(raw, input_key="prompt_tokens", output_key="completion_tokens"):
    """usage 是不可信的 provider 数据；非法/缺字段时退回估算而非跳过记账。"""
    if not isinstance(raw, dict):
        return None
    values = (raw.get(input_key), raw.get(output_key))
    if any(type(value) is not int or not 0 <= value <= usage.MAX_RECORDED_TOKENS
           for value in values):
        return None
    return values


def _chat_stream(body: dict, timeout: int, on_delta, usage_out=None) -> dict:
    content_parts: list[str] = []
    tools_acc: dict[int, dict] = {}
    finished = False
    with _post(body, timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                finished = True
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError as e:
                raise LLMError(f"LLM 流式响应解析失败: {e}") from e
            if not isinstance(event, dict) or event.get("error"):
                raise LLMError(f"LLM 流式错误: {json.dumps(event)[:500]}")
            used = _usage_tokens(event.get("usage"))
            if used is not None and usage_out is not None:
                usage_out["tokens"] = used
            choice = (event.get("choices") or [{}])[0]
            if choice.get("finish_reason"):
                if choice["finish_reason"] in {"length", "content_filter"}:
                    raise LLMError(f"LLM 流式响应未完成: {choice['finish_reason']}")
                finished = True
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece is not None and not isinstance(piece, str):
                raise LLMError("LLM 流式响应格式异常：content 必须是字符串")
            if piece:
                content_parts.append(piece)
                on_delta(piece)
            for tc in delta.get("tool_calls") or []:
                _accumulate_tool_call(tools_acc, tc)
    if not content_parts and not tools_acc:
        raise LLMError("流式响应为空（端点可能不支持 stream，或返回了非 SSE 内容）")
    if not finished:
        raise LLMError("LLM 流式响应中断，未收到结束标记")
    msg: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tools_acc:
        msg["tool_calls"] = [tools_acc[i] for i in sorted(tools_acc)]
    return _validate_message(msg)


def _chat_once(messages: list[dict], tools: list[dict] | None, model: str | None,
               timeout: int, on_delta) -> tuple[dict, tuple[int, int] | None]:
    """单次调用（不重试）。返回 (message, (prompt_tokens, completion_tokens) | None)。

    provider 由 OPSAGENT_PROVIDER 选择；未返回有效 usage 时由调用方按字符粗估。
    """
    provider = os.environ.get("OPSAGENT_PROVIDER", "openai").strip().lower() or "openai"
    if provider == "anthropic":
        from . import llm_anthropic  # 延迟导入：默认 provider 不加载
        return llm_anthropic.chat_once(messages, tools, model, timeout, on_delta)
    if provider != "openai":
        raise LLMError(f"未知 provider: {provider}（可选 openai / anthropic）")
    body: dict = {
        "model": model or os.environ.get("OPSAGENT_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "stream": on_delta is not None,
    }
    if tools:
        body["tools"] = tools
    try:
        if on_delta is not None:
            used_stream = {}
            msg = _chat_stream(body, timeout, on_delta, used_stream)
            return msg, used_stream.get("tokens")
        with _post(body, timeout) as resp:
            data = json.loads(resp.read().decode())
    except (ValueError, RecursionError) as e:
        raise LLMError(f"LLM 响应解析失败: {e}") from e
    except (OSError, http.client.HTTPException) as e:
        raise LLMError(f"LLM API 读取失败: {e}") from e
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError(f"LLM 流式响应格式异常: {e}") from e
    try:
        choice = data["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise LLMError(f"LLM 响应未完成: {choice['finish_reason']}")
        msg = _validate_message(choice["message"])
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError(f"LLM 响应格式异常: {json.dumps(data)[:500]}") from e
    raw_usage = data.get("usage") if isinstance(data, dict) else None
    return msg, _usage_tokens(raw_usage)


def chat(messages: list[dict], tools: list[dict] | None = None,
         model: str | None = None, timeout: int = 300, on_delta=None,
         deadline: float | None = None) -> dict:
    """调用当前 provider，返回 OpenAI 形状的 message dict（含可选 tool_calls）。

    统一入口：锁内预留预算（耗尽即 BudgetExhausted，不重试）→ 瞬时错误退避重试
    → 每次尝试记账。deadline（monotonic 秒）是任务级时间预算：单次尝试的超时与
    退避都会裁剪到剩余时间内，累计等待不会突破任务预算。
    """
    # 流式：第一次尝试可能已把半段内容推给用户，已发过内容后绝不重试（防重复输出）
    emitted = {"any": False}

    def tracked_delta(piece):
        emitted["any"] = True
        on_delta(piece)

    cb = tracked_delta if on_delta is not None else None
    attempt = 0
    actual_model = model or os.environ.get(
        "OPSAGENT_MODEL", "claude-sonnet-4-5"
        if os.environ.get("OPSAGENT_PROVIDER", "").strip().lower() == "anthropic"
        else DEFAULT_MODEL)
    while True:
        attempt_timeout = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError("LLM 调用超出任务时间预算（deadline）")
            # 重试与超时都受任务预算约束：单次尝试最多用掉剩余时间
            attempt_timeout = min(timeout, remaining)
        try:
            reserved_day, reason = usage.reserve_call()
        except (core.OpsxError, OSError) as e:
            raise LLMError(f"调用预算配置异常（无法判断是否超限，已停止调用）: {e}。"
                           "修 ~/.ops-agent/budget.json / usage-state.json，"
                           "或删除后 opsagent budget set 重建") from e
        if reason:
            _alert_budget_once(reason)
            raise BudgetExhausted(reason)
        try:
            msg, used = _chat_once(messages, tools, model, attempt_timeout, cb)
        except LLMError as e:
            prompt, completion = usage.estimate_tokens(messages, "", tools)
            usage.record(actual_model, prompt, completion, estimated=True,
                         reserved_day=reserved_day, failed=True)
            if attempt >= TRANSIENT_RETRIES or not is_transient(e) or emitted["any"]:
                raise
            delay = _backoff(attempt)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= delay + 2:   # 退避后没时间再试，直接按本次失败处理
                    raise
                delay = min(delay, max(0.0, remaining - 2))
            time.sleep(delay)
            attempt += 1
            continue
        if used is None:
            prompt, completion = usage.estimate_tokens(messages, msg, tools)
            usage.record(actual_model, prompt, completion, estimated=True,
                         reserved_day=reserved_day)
        else:
            usage.record(actual_model, used[0], used[1], reserved_day=reserved_day)
        return msg
