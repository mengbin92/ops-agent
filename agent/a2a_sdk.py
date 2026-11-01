"""A2A 官方 SDK 的懒加载入口——全项目唯一 import `a2a` 的地方。

设计约束：ops-agent 内核（REPL / 单发 / RPC / MCP / 巡检 / 调度）保持零第三方依赖，
A2A 只是可选形态。因此这里把 `import a2a` 收进函数体，其他模块一律经 sdk() 取符号：
缺依赖时给一条可执行的安装命令，而不是把 ImportError 堆栈丢给用户。

为什么集中在一处：A2A SDK 1.x 的符号位置分散（types / helpers / server.routes ...），
散落 import 会让「有没有装依赖」这件事变成十几个 try/except，也没法在 CLI 启动时一次性
给出准确提示。

版本基线（实测组合见 requirements-a2a.txt）：
  a2a-sdk 1.3.0 —— protobuf 消息 + PascalCase JSON-RPC 方法名（SendMessage），
  协议版本走 A2A-Version: 1.0 头；与 0.3 的 `message/send` 不兼容。
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

INSTALL_HINT = (
    "缺少 A2A 依赖（官方 a2a-sdk）。安装后重试：\n"
    "  python3 -m pip install -r requirements-a2a.txt\n"
    "注意：A2A 形态要求 Python >= 3.10（当前 {}），其余形态不受影响。"
)

MIN_PYTHON = (3, 10)


class A2AUnavailable(RuntimeError):
    """SDK 不可用：未安装、Python 版本过低，或 SDK 版本与预期不符。"""


_cache: SimpleNamespace | None = None


def _import_all() -> SimpleNamespace:
    """导入并打包 A2A 形态需要的全部符号。任何一步失败都收敛成 A2AUnavailable。"""
    try:
        from a2a.types import a2a_pb2 as pb
        from a2a.helpers import proto_helpers as ph
        from a2a.server.agent_execution import AgentExecutor, RequestContext
        from a2a.server.events import EventQueue
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.routes.agent_card_routes import create_agent_card_routes
        from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
        from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
        from a2a.utils.constants import (
            PROTOCOL_VERSION_1_0,
            TransportProtocol,
        )
        from starlette.applications import Starlette
    except ImportError as e:  # 未安装 / 装了个不兼容的旧版
        raise A2AUnavailable(INSTALL_HINT.format(".".join(map(str, sys.version_info[:2])))
                             + f"\n原始错误：{e}") from e

    # 只做「确认符号真的存在」的轻量校验：旧版 SDK 会在这里就露馅，
    # 而不是等到起服务时才 AttributeError。
    missing = [name for name in ("AgentCard", "TaskStatusUpdateEvent", "SendMessageRequest")
               if not hasattr(pb, name)]
    if missing:
        raise A2AUnavailable(
            "A2A SDK 版本不匹配：缺少 protobuf 消息 "
            f"{missing}。本项目面向 a2a-sdk 1.x（protobuf 类型），"
            f"请安装 requirements-a2a.txt 指定的版本。")

    return SimpleNamespace(
        pb=pb, ph=ph,
        AgentExecutor=AgentExecutor, RequestContext=RequestContext, EventQueue=EventQueue,
        DefaultRequestHandler=DefaultRequestHandler,
        InMemoryTaskStore=InMemoryTaskStore, TaskUpdater=TaskUpdater,
        create_agent_card_routes=create_agent_card_routes,
        create_jsonrpc_routes=create_jsonrpc_routes,
        Starlette=Starlette,
        PROTOCOL_VERSION_1_0=PROTOCOL_VERSION_1_0,
        TransportProtocol=TransportProtocol,
    )


def sdk() -> SimpleNamespace:
    """返回 SDK 符号表（首次调用时导入并缓存）。不可用时抛 A2AUnavailable。"""
    global _cache
    if _cache is None:
        if sys.version_info < MIN_PYTHON:
            raise A2AUnavailable(INSTALL_HINT.format(".".join(map(str, sys.version_info[:2]))))
        _cache = _import_all()
    return _cache


def available() -> tuple[bool, str]:
    """CLI 用：返回 (是否可用, 原因)。不抛异常，便于 doctor/启动自检。"""
    try:
        sdk()
    except A2AUnavailable as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001 —— 依赖装了但坏了也要能报出来
        return False, f"A2A SDK 加载失败：{e}"
    return True, "ok"


def client_symbols() -> SimpleNamespace:
    """客户端专用符号（create_client / ClientConfig / httpx）。

    与服务端分开加载：只做客户端调用时不需要 starlette 等 HTTP 服务依赖。
    """
    try:
        from a2a.client import ClientConfig, create_client
        import httpx
    except ImportError as e:
        raise A2AUnavailable(INSTALL_HINT.format(".".join(map(str, sys.version_info[:2])))
                             + f"\n原始错误：{e}") from e
    return SimpleNamespace(create_client=create_client, ClientConfig=ClientConfig, httpx=httpx)
