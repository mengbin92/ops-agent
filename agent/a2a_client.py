"""A2A 客户端：发现 AgentCard、发消息、带 task_id 续跑确认。

用官方 `create_client`（自动按 card 的 supported_interfaces 选传输），而不是手搓
JSON-RPC：协议版本、方法名、内容协商都由 SDK 维护，对端升级时不用跟着改。

一个必须记住的坑：`httpx.AsyncClient` 默认 `trust_env=True`，会读取 HTTP_PROXY。
在配了代理的环境里，对回环地址的请求也可能被转发，路径变形后服务端按 404 处理——
表现为「第一次成功、后续全 404」。所以这里默认 `trust_env=False`，
需要走代理时显式 `--proxy`。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from .a2a_sdk import A2AUnavailable, client_symbols, sdk  # noqa: E402

TIMEOUT_DEFAULT = 300
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NEEDS_INPUT = 4


def _text_parts(message) -> str:
    """取 Message 里的文本段。

    手动遍历 parts，不依赖 SDK 的 `get_message_text` 辅助函数：它的签名随版本变，
    而 `Part.text` 是 protobuf 字段，跨版本稳定。非文本段（文件/结构化数据）跳过，
    不猜内容。
    """
    out = []
    for part in getattr(message, "parts", []) or []:
        try:
            if part.HasField("text"):
                out.append(part.text)
        except (AttributeError, ValueError):
            continue
    return "\n".join(out)


def _task_text(task) -> str:
    """任务正文：产物优先，其次终态消息。

    与 ops-agent 服务端的约定一致：正文放 artifact（name=reply），状态消息只放摘要。
    """
    chunks = []
    for artifact in getattr(task, "artifacts", []) or []:
        for part in artifact.parts:
            if part.HasField("text"):
                chunks.append(part.text)
    if chunks:
        return "\n".join(chunks)
    if task.HasField("status") and task.status.HasField("message"):
        return _text_parts(task.status.message)
    return ""


def _state_name(state) -> str:
    try:
        return sdk().pb.TaskState.Name(state)
    except Exception:  # noqa: BLE001
        return str(state)


async def _send_async(url: str, text: str, task_id: str | None,
                      context_id: str | None, token: str | None,
                      timeout: int, proxy: str | None) -> dict:
    sym = client_symbols()
    pb = sdk().pb
    headers = {"Authorization": f"Bearer {token}"} if token else None
    kwargs = {"timeout": timeout, "headers": headers, "trust_env": False}
    if proxy:
        kwargs.update(trust_env=True, proxy=proxy)

    async with sym.httpx.AsyncClient(**kwargs) as hc:
        client = await sym.create_client(url, sym.ClientConfig(httpx_client=hc,
                                                              streaming=False))
        message = pb.Message(
            message_id=uuid.uuid4().hex,
            role=pb.ROLE_USER,
            parts=[sdk().ph.new_text_part(text)],
        )
        if task_id:
            message.task_id = task_id
        if context_id:
            message.context_id = context_id

        result = {"state": "TASK_STATE_UNKNOWN", "text": "", "task_id": task_id or "",
                  "context_id": context_id or "", "needs_input": False}
        async for resp in client.send_message(pb.SendMessageRequest(message=message)):
            if resp.HasField("task"):
                task = resp.task
                result.update(task_id=task.id, context_id=task.context_id,
                              state=_state_name(task.status.state),
                              text=_task_text(task))
            elif resp.HasField("message"):
                result["text"] = _text_parts(resp.message)
            elif resp.HasField("status_update"):
                upd = resp.status_update
                result.update(task_id=upd.task_id, context_id=upd.context_id,
                              state=_state_name(upd.status.state))
                if upd.status.HasField("message"):
                    result["text"] = _text_parts(upd.status.message)
        result["needs_input"] = result["state"] == "TASK_STATE_INPUT_REQUIRED"
        return result


def send(url: str, text: str, task_id: str | None = None, context_id: str | None = None,
         token: str | None = None, timeout: int = TIMEOUT_DEFAULT,
         proxy: str | None = None) -> dict:
    """发一条消息并等结果（同步封装，便于 CLI / 脚本调用）。

    调用方可能已经在事件循环里（例如 ops-agent 自己作为 A2A 服务端时，工具里再调
    外部 agent）。`asyncio.run()` 在这种情况下会直接 RuntimeError，所以检测到运行中
    循环就改到新线程里起一个独立循环。
    """
    def _run():
        return asyncio.run(_send_async(url, text, task_id, context_id, token, timeout, proxy))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        in_loop = False
    else:
        in_loop = True

    try:
        if not in_loop:
            return _run()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run).result(timeout=timeout + 30)
    except A2AUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 —— 网络/协议错误统一成可读信息
        raise RuntimeError(core.redact(f"调用失败：{type(e).__name__}: {e}")) from e


def format_result(result: dict) -> str:
    lines = [f"状态: {result['state']}"]
    if result.get("task_id"):
        lines.append(f"task_id: {result['task_id']}")
        lines.append(f"context_id: {result['context_id']}")
        # 会话 id 是对端服务端拼的（a2a-<context_id>），所以这里只能给条件性提示：
        # 客户端无法知道对端是不是 ops-agent，不能无条件让它去 /resume。
        if result.get("context_id"):
            lines.append(f"（若对端是 ops-agent，可在其主机上 /resume a2a-{result['context_id']}）")
    body = (result.get("text") or "").strip()
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(line for line in lines if line != "")
