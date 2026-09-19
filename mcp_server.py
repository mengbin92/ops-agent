#!/usr/bin/env python3
"""ops-guard MCP server —— core.py 的 MCP 薄壳（stdio JSON-RPC，纯 stdlib）。

协议子集：initialize / notifications.initialized / tools/list / tools/call / ping。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

SERVER_NAME = "ops-guard"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION_DEFAULT = "2024-11-05"


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _send(resp):
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_message(msg):
    """返回响应 dict；通知（无 id）返回 None。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "请求必须是 JSON 对象")
    method = msg.get("method")
    msg_id = msg.get("id")
    if "id" not in msg:
        return None  # JSON-RPC 通知，一律不响应
    if method == "initialize":
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION_DEFAULT),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method is None:
        return _err(msg_id, -32600, "缺少 method 字段")
    return _err(msg_id, -32601, f"未知方法: {method}")


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send(_err(None, -32700, "Parse error"))
            continue
        try:
            resp = handle_message(msg)
        except Exception as e:  # noqa: BLE001 —— 单条消息异常不崩循环
            traceback.print_exc(file=sys.stderr)
            resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, str(e))
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    serve()
