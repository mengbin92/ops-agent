#!/usr/bin/env python3
"""ops-guard MCP server —— core.py 的 MCP 薄壳（stdio JSON-RPC，纯 stdlib）。

协议子集：initialize / notifications.initialized / tools/list / tools/call / ping。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import remote  # noqa: E402

SERVER_NAME = "ops-guard"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION_DEFAULT = "2024-11-05"


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _send(resp):
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(payload):
    # MCP 客户端（模型宿主）也是出口：工具结果统一脱敏后再回传。
    return {"content": [{"type": "text",
                         "text": json.dumps(core.redact_obj(payload), ensure_ascii=False)}]}


def _tool_error(message):
    return {"isError": True, "content": [{"type": "text", "text": core.redact(message)}]}


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
            "uses": {"type": "integer", "default": 1,
                     "description": "该授权允许执行几次；默认 1 = 一次批准只生效一次"},
        },
        "required": ["command"],
    }),
    _tool("ops_revoke", "撤销审批戳（用户取消变更）", {
        "type": "object",
        "properties": {"command": {"type": "string"}},
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
    _tool("ops_rollback", "按快照回滚：先返回目标/文件/差异预览，confirm=true 才执行", {
        "type": "object",
        "properties": {"snapshot_id": {"type": "string"},
                       "confirm": {"type": "boolean", "default": False}},
        "required": ["snapshot_id"],
    }),
    _tool("ops_executions", "变更执行记录：查状态与待核实项（只读；核实请用 opsx executions resolve）", {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "show"], "default": "list"},
            "execution_id": {"type": "string"},
            "tail": {"type": "integer", "default": 20},
            "status": {"type": "string", "enum": ["running", "succeeded", "failed",
                                                  "unknown", "aborted"]},
        },
        "required": [],
    }, read_only=True),
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


def call_tool(name, args):
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise core.OpsxError(f"未知工具: {name}")
    core.validate_arguments(args, tool["inputSchema"])
    if name == "ops_check":
        cmd = _require(args, "command")
        level, _ = core.classify(cmd)
        code, message = core.check(cmd)
        return _result({"level": level, "allowed": code == 0, "message": message,
                        "env": core.format_env(core.resolve_env(cmd))})
    if name == "ops_snapshot_file":
        sid = core.snapshot_file(_require(args, "path"))
        return _result({"snapshot_id": sid})
    if name == "ops_snapshot_cmd":
        sid = core.snapshot_cmd(_require(args, "command"), args.get("name", ""))
        return _result({"snapshot_id": sid})
    if name == "ops_approve":
        cmd = _require(args, "command")
        path = core.approve(
            cmd,
            ttl_seconds=args.get("ttl_seconds", 900),
            force=args.get("force", False),
            uses=args.get("uses", 1),
        )
        stamp = json.loads(path.read_text(encoding="utf-8"))
        return _result({"stamp_file": path.name, "level": stamp["level"],
                        "ttl_seconds": stamp["ttl"], "uses": stamp.get("uses", 1),
                        "env": core.format_env(stamp.get("env") or core.local_env())})
    if name == "ops_revoke":
        revoked = core.revoke(_require(args, "command"))
        return _result({"revoked": revoked})
    if name == "ops_exec":
        snap_cmds = [(c["command"], c.get("name", "")) for c in args.get("snapshot_cmds", [])]
        started = time.time()
        code, sid, output = core.exec_change(
            _require(args, "command"),
            args.get("snapshot_files", []),
            snap_cmds,
        )
        import execution
        record = execution.find_latest(_require(args, "command"), since=started,
                                       pid=os.getpid(), kind="exec")
        payload = {"exit_code": code, "snapshot_id": sid, "output": output[:4000]}
        if record is not None:
            payload["execution_id"] = record["id"]
            payload["execution_status"] = record["status"]
        return _result(payload)
    if name == "ops_executions":
        import execution
        # 不落盘对账：本工具标了 readOnlyHint，list_records 的视图已按 unknown/aborted 呈现
        if args.get("action", "list") == "show":
            record = execution.get(_require(args, "execution_id"))
            if record is None:
                raise core.OpsxError(f"没有这条执行记录: {args.get('execution_id')}")
            return _result(record)
        if args.get("tail", 20) <= 0:
            raise core.OpsxError("tail 须为正整数")
        summary = execution.summary()
        return _result({"summary": summary,
                        "records": execution.list_records(
                            tail=args.get("tail", 20),
                            status=args.get("status") or None)})
    if name == "ops_rollback":
        sid = _require(args, "snapshot_id")
        plan = remote.rollback_plan(sid)
        if plan["files"] and not args.get("confirm", False):
            return _result({"needs_confirm": True,
                            "plan": core.format_rollback_plan(plan),
                            "hint": "回滚会覆盖文件：核对目标与差异后带 confirm=true 重试"})
        import execution
        started, error = time.time(), None
        try:
            report = remote.rollback(sid, confirm=True)
        except (core.OpsxError, remote.RemoteError) as e:
            error = e
        record = execution.find_latest(f"rollback {sid}", plan.get("target"),
                                       since=started, pid=os.getpid(), kind="rollback")
        if error is not None:
            reference = (f"\nexecution_id={record['id']}（状态 {record['status']}；"
                         f"查看：opsx executions show {record['id']}）") if record else ""
            return _tool_error(str(error) + reference)
        payload = {"report": report}
        if record is not None:
            payload.update(execution_id=record["id"], execution_status=record["status"])
        return _result(payload)
    if name == "ops_list":
        return _result(core.collect_state())
    if name == "ops_audit":
        tail = args.get("tail", 20)
        if tail < 0:
            raise core.OpsxError("tail 不能为负数")
        path = core.STATE_DIR / "audit.jsonl"
        events = []
        if tail and path.exists():
            for line in path.read_text(encoding="utf-8").strip().splitlines()[-tail:]:
                events.append(json.loads(line))
        return _result({"events": events})


def handle_message(msg):
    """返回响应 dict；通知（无 id）返回 None。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "请求必须是 JSON 对象")
    method = msg.get("method")
    msg_id = msg.get("id")
    if (msg.get("jsonrpc") != "2.0" or not isinstance(method, str)
            or ("id" in msg and msg_id is not None and type(msg_id) not in (str, int))):
        return _err(None, -32600, "请求格式异常")
    if "id" not in msg:
        return None  # JSON-RPC 通知，一律不响应
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return _err(msg_id, -32602, "params 必须是对象")
    if method == "initialize":
        if "protocolVersion" in params and (not isinstance(params["protocolVersion"], str)
                                              or not params["protocolVersion"]):
            return _err(msg_id, -32602, "protocolVersion 必须是非空字符串")
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
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        try:
            result = call_tool(tool_name, tool_args)
        except (core.OpsxError, remote.RemoteError) as e:
            result = _tool_error(str(e))
        except Exception as e:  # noqa: BLE001 —— 未预期异常走 JSON-RPC 层
            print(core.redact(traceback.format_exc()), file=sys.stderr)
            return _err(msg_id, -32603, core.redact(f"工具内部错误: {e}"))
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
            print(core.redact(traceback.format_exc()), file=sys.stderr)
            resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603,
                        core.redact(str(e)))
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    serve()
