"""工具注册表：{name: {schema, handler, read_only}}。内核只分发，前端只渲染。

守门在内核之内：bash handler 直接调 core.check()——R0 放行；其余要求
审批戳（模型须先走变更单 + ask_user + ops_approve 流程，戳由 ops_approve 工具创建）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

BASH_TIMEOUT = 120
OUTPUT_LIMIT = 30_000  # 超出截断，M2 再做 spill


class ToolContext:
    """前端注入的人机回路：ask_user(question) -> str；不可用返回 None。"""

    def __init__(self, ask_user=None):
        self.ask_user = ask_user


def _schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _truncate(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT] + f"\n...[已截断，共 {len(text)} 字符]"


def _bash(args: dict, ctx: ToolContext) -> str:
    cmd = args["command"]
    level, matched = core.classify(cmd)
    if level != "R0":
        # bash 只执行只读命令：写操作必须走 ops_exec（快照+审批戳+审计一体化），
        # 防止带戳写命令绕过快照直接执行。
        core.audit("bash-deny", cmd=cmd, level=level)
        hit = f"（命中分段：{matched}）" if matched else ""
        return (f"[opsx 拒绝] 该命令判定为 {level}{hit}，bash 仅执行只读命令。\n"
                "写操作流程：向用户提交变更单（风险说明+回滚计划）→ ask_user 确认 → "
                "ops_approve 建审批戳 → ops_exec（可带快照参数）执行。")
    code, msg = core.check(cmd)  # R0 直接放行（check 内会写审计）
    if code != 0:
        return f"[opsx 拒绝] {msg}"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=BASH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"[超时] 命令超过 {BASH_TIMEOUT}s 未结束，已终止"
    out = (r.stdout + ("\n[stderr]\n" + r.stderr if r.stderr.strip() else ""))
    return f"[exit={r.returncode}]\n" + _truncate(out)


def _read(args: dict, ctx: ToolContext) -> str:
    path = Path(args["path"]).expanduser()
    if not path.is_file():
        return f"[错误] 文件不存在: {path}"
    try:
        return _truncate(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        return f"[错误] 读取失败: {e}"


def _ask_user(args: dict, ctx: ToolContext) -> str:
    if ctx.ask_user is None:
        return "[ask_user 不可用] 当前形态无交互终端。写操作安全方向降级：视为用户拒绝。"
    return ctx.ask_user(args["question"])


def _ops_approve(args: dict, ctx: ToolContext) -> str:
    try:
        path = core.approve(args["command"], ttl_seconds=int(args.get("ttl_seconds", 900)),
                            force=bool(args.get("force", False)))
    except core.OpsxError as e:
        return f"[opsx 错误] {e}"
    stamp = json.loads(path.read_text(encoding="utf-8"))
    return f"审批戳已创建（级别 {stamp['level']}，TTL {stamp['ttl']}s），现在可重新执行该命令。"


def _snapshot_filespec(args: dict) -> list[str]:
    return [args["snapshot_file"]] if args.get("snapshot_file") else []


def _ops_exec(args: dict, ctx: ToolContext) -> str:
    """带快照的原子执行：等价于 opsx exec。"""
    cmd = args["command"]
    snap_cmds = []
    for spec in args.get("snapshot_cmds", []):
        snap_cmds.append((spec["command"], spec.get("name", "")))
    code, sid, output = core.exec_change(cmd, _snapshot_filespec(args), snap_cmds)
    out_text = _truncate(output) if output else "(无输出)"
    sid_text = f"snapshot_id={sid}（回滚：ops_rollback）" if sid else "无快照"
    if code == 0:
        return f"[完成] 执行成功（exit=0），{sid_text}。\n{out_text}"
    if code == 2:
        return ("[opsx 拒绝] 无有效审批戳。流程：变更单 → ask_user 确认 → "
                "ops_approve 建戳 → 重新 ops_exec。")
    reason = {3: "快照失败，已中止", 4: "超时终止"}.get(code, "命令非零退出")
    return f"[失败] {reason}（exit={code}），{sid_text}。\n{out_text}"


def _ops_rollback(args: dict, ctx: ToolContext) -> str:
    try:
        return core.rollback(args["snapshot_id"])
    except core.OpsxError as e:
        return f"[opsx 错误] {e}"


def _ops_list(args: dict, ctx: ToolContext) -> str:
    state = core.collect_state()
    lines = ["== snapshots =="] + state["snapshots"] if state["snapshots"] else []
    if state["approvals"]:
        lines.append("== approvals ==")
        lines += [f"{a['cmd'][:80]}  (level={a['level']}, ttl={a['ttl']}s)"
                  for a in state["approvals"]]
    return "\n".join(lines) or "(空)"


REGISTRY: dict[str, dict] = {
    "bash": {
        "schema": _schema("bash", "执行 shell 命令。只读命令直接放行；写命令需有效审批戳（走变更单流程）。",
                          {"command": {"type": "string", "description": "完整 shell 命令"}}, ["command"]),
        "handler": _bash, "read_only": False,
    },
    "read": {
        "schema": _schema("read", "读取本地文本文件内容。",
                          {"path": {"type": "string"}}, ["path"]),
        "handler": _read, "read_only": True,
    },
    "ask_user": {
        "schema": _schema("ask_user", "向用户提问并等待回答。写操作审批、歧义澄清时使用。",
                          {"question": {"type": "string", "description": "向用户展示的问题（含变更单）"}},
                          ["question"]),
        "handler": _ask_user, "read_only": True,
    },
    "ops_approve": {
        "schema": _schema("ops_approve", "为已获用户确认的命令创建审批戳（R3 需 force=true）。",
                          {"command": {"type": "string"},
                           "ttl_seconds": {"type": "integer", "default": 900},
                           "force": {"type": "boolean", "default": False}}, ["command"]),
        "handler": _ops_approve, "read_only": False,
    },
    "ops_exec": {
        "schema": _schema("ops_exec", "原子执行变更：校验审批戳→快照→执行→审计。",
                          {"command": {"type": "string"},
                           "snapshot_file": {"type": "string", "description": "执行前需快照的文件（可选）"},
                           "snapshot_cmds": {"type": "array", "items": {"type": "object",
                                             "properties": {"command": {"type": "string"},
                                                            "name": {"type": "string"}},
                                             "required": ["command"]}}}, ["command"]),
        "handler": _ops_exec, "read_only": False,
    },
    "ops_rollback": {
        "schema": _schema("ops_rollback", "按 snapshot_id 回滚。",
                          {"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
        "handler": _ops_rollback, "read_only": False,
    },
    "ops_list": {
        "schema": _schema("ops_list", "列出快照与审批戳。", {}, []),
        "handler": _ops_list, "read_only": True,
    },
}


def tool_schemas() -> list[dict]:
    return [t["schema"] for t in REGISTRY.values()]


def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    tool = REGISTRY.get(name)
    if tool is None:
        return f"[错误] 未知工具: {name}"
    try:
        return tool["handler"](args, ctx)
    except (KeyError, TypeError) as e:
        return f"[错误] 工具参数异常: {e}"
    except Exception as e:  # noqa: BLE001 —— 工具异常不炸 loop，回给模型
        return f"[错误] 工具执行异常: {e}"
