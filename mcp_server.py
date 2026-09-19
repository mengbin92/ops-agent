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


def _result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _tool_error(message):
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _tool(name, description, schema, read_only=False):
    t = {"name": name, "description": description, "inputSchema": schema}
    if read_only:
        t["annotations"] = {"readOnlyHint": True}
    return t


TOOLS = [
    _tool("ops_check", "判定命令风险级别与放行状态（只读）", {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }, read_only=True),
    _tool("ops_snapshot_file", "对文件做执行前快照", {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }),
    _tool("ops_snapshot_cmd", "执行命令并捕获其输出作为状态快照", {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["command"],
    }),
    _tool("ops_approve", "为命令创建审批戳（R3 需 force=true）", {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "ttl_seconds": {"type": "integer", "default": 900},
            "force": {"type": "boolean", "default": False},
        },
        "required": ["command"],
    }),
    _tool("ops_exec", "原子执行：校验审批戳→快照→执行→审计", {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "snapshot_files": {"type": "array", "items": {"type": "string"}},
            "snapshot_cmds": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["command"],
                },
            },
        },
        "required": ["command"],
    }),
    _tool("ops_rollback", "按快照回滚", {
        "type": "object",
        "properties": {"snapshot_id": {"type": "string"}},
        "required": ["snapshot_id"],
    }),
    _tool("ops_list", "列出快照与审批戳（只读）", {
        "type": "object", "properties": {},
    }, read_only=True),
    _tool("ops_audit", "查看审计日志（只读）", {
        "type": "object",
        "properties": {"tail": {"type": "integer", "default": 20}},
    }, read_only=True),
]


def _require(args, key):
    if not isinstance(args, dict) or key not in args or args[key] in (None, ""):
        raise core.OpsxError(f"缺少参数: {key}")
    return args[key]


def _last_exec_snapshot_id():
    path = core.STATE_DIR / "audit.jsonl"
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").strip().splitlines()):
        rec = json.loads(line)
        if rec.get("event") == "exec":
            return rec.get("snapshot_id")
    return None


def call_tool(name, args):
    if name == "ops_check":
        cmd = _require(args, "command")
        level, _ = core.classify(cmd)
        code, message = core.check(cmd)
        return _result({"level": level, "allowed": code == 0, "message": message})
    if name == "ops_snapshot_file":
        sid = core.snapshot_file(_require(args, "path"))
        return _result({"snapshot_id": sid})
    if name == "ops_snapshot_cmd":
        sid = core.snapshot_cmd(_require(args, "command"), args.get("name", ""))
        return _result({"snapshot_id": sid})
    if name == "ops_approve":
        path = core.approve(
            _require(args, "command"),
            ttl_seconds=int(args.get("ttl_seconds", 900)),
            force=bool(args.get("force", False)),
        )
        stamp = json.loads(path.read_text(encoding="utf-8"))
        return _result({"stamp_file": path.name, "level": stamp["level"], "ttl_seconds": stamp["ttl"]})
    if name == "ops_exec":
        snap_cmds = [(c["command"], c.get("name", "")) for c in args.get("snapshot_cmds", [])]
        code = core.exec_change(
            _require(args, "command"),
            args.get("snapshot_files", []),
            snap_cmds,
        )
        payload = {"exit_code": code}
        if code == 0:
            payload["snapshot_id"] = _last_exec_snapshot_id()
        return _result(payload)
    if name == "ops_rollback":
        report = core.rollback(_require(args, "snapshot_id"))
        return _result({"report": report})
    if name == "ops_list":
        sdir = core.STATE_DIR / "snapshots"
        snapshots = sorted(p.name for p in sdir.iterdir()) if sdir.exists() else []
        approvals = []
        adir = core.STATE_DIR / "approvals"
        if adir.exists():
            for p in sorted(adir.glob("*.json")):
                st = json.loads(p.read_text(encoding="utf-8"))
                approvals.append({"cmd": st["cmd"], "level": st["level"], "ttl": st["ttl"]})
        return _result({"snapshots": snapshots, "approvals": approvals})
    if name == "ops_audit":
        tail = int(args.get("tail", 20))
        path = core.STATE_DIR / "audit.jsonl"
        events = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").strip().splitlines()[-tail:]:
                events.append(json.loads(line))
        return _result({"events": events})
    raise core.OpsxError(f"未知工具: {name}")


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
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        try:
            result = call_tool(tool_name, tool_args)
        except core.OpsxError as e:
            result = _tool_error(str(e))
        except Exception as e:  # noqa: BLE001 —— 未预期异常走 JSON-RPC 层
            traceback.print_exc(file=sys.stderr)
            return _err(msg_id, -32603, f"工具内部错误: {e}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
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
