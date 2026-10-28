"""工具注册表：{name: {schema, handler, read_only}}。内核只分发，前端只渲染。

守门在内核之内：bash handler 直接调 core.check()——R0 放行；其余要求
审批戳（模型须先提交变更单，ops_approve 工具自行取得用户明确确认后创建戳）。
带 target 时经 remote.py 下发到已注册的 SSH 目标（bash 仍限 R0，写操作走 ops_exec）；
审批戳按「目标+命令」绑定，本机戳不能用于远程。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
import remote  # noqa: E402
from . import checks  # noqa: E402
from . import events as events_mod  # noqa: E402
from . import goal as goal_mod  # noqa: E402
from . import logscan  # noqa: E402
from . import memory as memory_mod  # noqa: E402
from . import schedule as schedule_mod  # noqa: E402
from . import session as session_mod  # noqa: E402
from . import skills  # noqa: E402
from . import workflow  # noqa: E402

BASH_TIMEOUT = 120
OUTPUT_LIMIT = 30_000   # 超限输出落盘，预览只留前 3 万字符
SPILL_DIR = core.STATE_DIR / "outputs"
SPILL_KEEP = 200        # 落盘输出保留份数，超出按时间删最旧
READ_LIMIT = 2000       # read 默认返回行数


class ToolContext:
    """前端注入的人机回路与运行参数。

    ask_user(question) -> str；不可用返回 None。
    allowed：工具白名单（子代理只读），None = 全部。
    model：父代理使用的模型，子代理沿用（--model 覆盖时保持一致）。
    session_id：长目标按会话绑定；没有会话 id 时 goal 不可用。
    goal_continuation：只有 REPL 会跨轮续跑，-p/RPC/定时任务为 False。
    deadline：整任务时间预算（time.monotonic 秒），None = 不设限；到点不再启动后续调用。
    evidence_errors：规则采集/摘要保存错误，由调度程序读取，不能由模型的正常结论覆盖。
    """

    def __init__(self, ask_user=None, allowed: set[str] | None = None,
                 model: str | None = None, session_id: str | None = None,
                 goal_continuation: bool = False, deadline: float | None = None):
        self.ask_user = ask_user
        self.allowed = allowed
        self.model = model
        self.session_id = session_id
        self.goal_continuation = goal_continuation
        self.deadline = deadline
        self.evidence_errors: list[str] = []


def _schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _prune_outputs(keep: int = SPILL_KEEP) -> None:
    # shortcut: 全局按 mtime 轮转；REPL/MCP 多进程同时落盘时可能删掉另一进程刚写的文件，
    # 需要更久保留时改按进程/日期分目录
    try:
        files = sorted(SPILL_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    except OSError:  # 并发删除时 stat 会失败，跳过本次清理
        return
    for p in files[:-keep]:
        try:
            p.unlink()
        except OSError:
            pass


def _cap(text: str, label: str) -> str:
    """超长输出落盘（日志可能含密钥 → 0600），返回预览 + 路径供 read 分段读取。

    落盘与回显前统一脱敏：模型、会话、审计、落盘文件都不保存明文密钥。
    """
    text = core.redact(text)
    if len(text) <= OUTPUT_LIMIT:
        return text
    try:
        SPILL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{label[:40]}-{uuid.uuid4().hex[:6]}.txt"
        path = SPILL_DIR / name
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                       "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        # 磁盘满/文件名过长/无权限：降级为纯截断，别把"执行成功"变成工具报错
        return text[:OUTPUT_LIMIT] + f"\n...[已截断，共 {len(text)} 字符；落盘失败，无法读取全文]"
    _prune_outputs()
    return (text[:OUTPUT_LIMIT]
            + f"\n...[输出共 {len(text)} 字符，已截断；完整内容: {path}"
              "（用 read 工具的 offset/limit 分段读取）]")


def _bash(args: dict, ctx: ToolContext) -> str:
    cmd = args["command"]
    target = args.get("target") or None
    level, matched = core.classify(cmd)
    if level != "R0":
        # bash 只执行只读命令：写操作必须走 ops_exec（快照+审批戳+审计一体化），
        # 防止带戳写命令绕过快照直接执行。
        core.audit("bash-deny", cmd=cmd, level=level, target=target)
        hit = f"（命中分段：{matched}）" if matched else ""
        return (f"[opsx 拒绝] 该命令判定为 {level}{hit}，bash 仅执行只读命令。\n"
                "写操作流程：向用户提交变更单（风险说明+回滚计划+目标）→ "
                "ops_approve 询问确认并建戳 → ops_exec（可带快照参数）执行。")
    code, msg = core.check(cmd, target=target)  # R0 直接放行（check 内会写审计）
    if code != 0:
        return f"[opsx 拒绝] {msg}"
    timeout = BASH_TIMEOUT
    if ctx.deadline is not None:
        remaining = ctx.deadline - time.monotonic()
        if remaining <= 0:
            return "[任务超时] 时间预算耗尽，命令未执行"
        timeout = min(timeout, remaining)
    if target:  # 远程只读：经 SSH 目标执行
        code, out = remote.run(target, cmd, timeout=timeout)
        return f"[target={target} exit={code}]\n" + _cap(out, f"bash-{target}")
    try:
        child_env = None
        if core.is_kubectl_diff(cmd):
            # diff 尊重 KUBECTL_EXTERNAL_DIFF（可借此执行任意命令）：
            # R0 预演剔除该变量后执行，"只读预演"才名实相符。
            child_env = {k: v for k, v in os.environ.items()
                         if k != "KUBECTL_EXTERNAL_DIFF"}
        r = core.run_shell(cmd, timeout=timeout, env=child_env)
    except subprocess.TimeoutExpired:
        return f"[超时] 命令超过 {timeout:g}s 未结束，已终止"
    out = (r.stdout + ("\n[stderr]\n" + r.stderr if r.stderr.strip() else ""))
    return f"[exit={r.returncode}]\n" + _cap(out, "bash")


def _skill(args: dict, ctx: ToolContext) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        entries = skills.catalog()
        if not entries:
            return (f"(无可用 skill：在 {skills.SEARCH_DIRS[0]} 或 "
                    f"{skills.SEARCH_DIRS[1]} 放 *.md)")
        return "可用 skill：\n" + "\n".join(
            f"{e['name']} — {e['description']}" if e["description"] else e["name"]
            for e in entries)
    return skills.load(name)


def _read(args: dict, ctx: ToolContext) -> str:
    path = Path(args["path"]).expanduser()
    try:
        real = path.resolve()
    except OSError:
        real = path
    if (core.sensitive_path(str(path)) or core.sensitive_dir(str(path))
            or core.sensitive_path(str(real)) or core.sensitive_dir(str(real))):
        return ("[opsx 拒绝] 该文件是私钥/凭据/敏感配置，不读取到模型上下文。"
                "确认存在与权限请用 bash 的 ls -l。")
    try:
        snapshots = (core.STATE_DIR / "snapshots").resolve()
        if real == snapshots or snapshots in real.parents:
            return ("[opsx 拒绝] 快照保存原始内容用于恢复，不读取到模型上下文；"
                    "恢复请用 ops_rollback（展示差异并确认）。")
    except OSError:
        pass
    if not path.is_file():
        return f"[错误] 文件不存在: {path}"
    offset = max(1, args.get("offset", 1))
    limit = args.get("limit", READ_LIMIT)
    if limit <= 0:
        return "[错误] limit 必须是正整数"
    lines, size = [], 0
    key_open, key_tail = False, ""

    def contains_private_key(part: str) -> bool:
        # offset/limit 会切掉 PEM 包围标记，必须在跳过前文时仍跟踪私钥状态。
        # 只保留标记所需的小段上下文，长行读取仍然有界。
        nonlocal key_open, key_tail
        was_open = key_open
        scan = key_tail + part
        markers = [m for m in re.finditer(r"-----(BEGIN|END)[ A-Z0-9]*PRIVATE KEY-----", scan)
                   if m.end() > len(key_tail)]
        for marker in markers:
            key_open = marker.group(1) == "BEGIN"
        key_tail = scan[-128:]
        return was_open or any(m.group(1) == "BEGIN" for m in markers)

    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for _ in range(offset - 1):
                part = f.readline(OUTPUT_LIMIT + 2)
                contains_private_key(part)
                if not part:
                    return f"[错误] offset={offset} 超出文件行数"
                while part and not part.endswith("\n"):
                    part = f.readline(OUTPUT_LIMIT + 2)
                    contains_private_key(part)
            for _ in range(limit):
                line = f.readline(OUTPUT_LIMIT - size + 2)
                if not line:
                    break
                if contains_private_key(line):
                    return "[已脱敏] 读取范围含私钥内容，不返回分页片段。"
                line = line.rstrip("\r\n")
                size += len(line) + bool(lines)
                lines.append(line)
                if size > OUTPUT_LIMIT:
                    kept = "\n".join(lines)[:OUTPUT_LIMIT]
                    shown = kept.count("\n") + (not kept.endswith("\n"))
                    return kept + (f"\n...[单次读取超过 {OUTPUT_LIMIT} 字符，已截断"
                                   f"（当前行余下部分未返回）；继续读取 offset={offset + shown}]")
            if f.read(1):
                end = offset + len(lines) - 1
                return "\n".join(lines) + f"\n...[第 {offset}-{end} 行；继续读取 offset={end + 1}]"
    except OSError as e:
        return f"[错误] 读取失败: {e}"
    if not lines and offset > 1:
        return f"[错误] offset={offset} 超出文件行数"
    return "\n".join(lines)


def _ask_user(args: dict, ctx: ToolContext) -> str:
    if ctx.ask_user is None:
        return "[ask_user 不可用] 当前形态无交互终端。写操作安全方向降级：视为用户拒绝。"
    answer = ctx.ask_user(args["question"])
    return answer if isinstance(answer, str) else "[ask_user 不可用] 用户未回答，视为用户拒绝。"


def _ops_approve(args: dict, ctx: ToolContext) -> str:
    if ctx.ask_user is None:  # 没有确认通道时不能自建审批戳（否则可绕过人类授权）
        return ("[opsx 拒绝] 当前形态没有可确认的用户通道，不创建审批戳。"
                "请由人执行 opsx approve（或改用 REPL / MCP 弹窗）后重试。")
    target = args.get("target") or None
    try:
        if target:  # 只允许给注册过的目标建戳
            remote.get_target(target)
        level, _ = core.classify(args["command"])
        # 审批绑定实际执行环境（主机/cwd/k8s context），批准后环境变化即失效。
        env = core.resolve_env(args["command"], target)
        impact = (args.get("impact") or "").strip()
        if not impact:
            # 人确认的是 would-be 变更，不是一条孤立的命令字符串：影响面必须来自
            # 只读预演（kubectl diff / --dry-run=server / 目标内容与现内容对比）。
            return ("[opsx 拒绝] 变更单缺少影响面（impact）：先用只读预演确认 would-be 变更"
                    "（k8s: kubectl diff -f ... 或 kubectl apply --dry-run=server "
                    "--context <c> -f ...；文件: 对比目标内容与当前内容），"
                    "写清将改什么状态、影响哪些服务，再提交审批。")
        snap_files = sorted({str(f).strip() for f in (args.get("snapshot_files") or [])
                             if str(f).strip()})
        snap_cmds = [(str(c["command"]), str(c.get("name", "")))
                     for c in (args.get("snapshot_cmds") or []) if str(c["command"]).strip()]
        warning = "\n不可逆，无回滚方案。" if level == "R3" else ""
        uses = args.get("uses", 1)
        # 次数必须出现在确认框里：否则模型用 uses=100 换来的授权，人看起来和 1 次一样
        times = "1 次（执行后即失效，再执行需重新批准）" if uses == 1 \
            else f"{uses} 次（同一授权可重复执行 {uses} 次）"
        if snap_files or snap_cmds:
            rollback = (f"回滚点: 快照文件 {len(snap_files)} 个、快照命令 {len(snap_cmds)} 条"
                        "（ops_exec 必须带同一份快照参数）")
        elif level == "R3":
            rollback = "回滚点: 无（R3 不强制快照；执行结果不可逆）"
        else:
            rollback = ("⚠ 本次未声明任何快照：执行后无自动回滚点，"
                        "回滚只能按命令语义人工进行")
        answer = ctx.ask_user(f"批准此变更？\n执行环境：{core.format_env(env)}\n"
                              f"风险级别：{level}{warning}\n"
                              f"可用次数：{times}\n"
                              f"影响面（would-be 变更）:\n{core.redact(impact)}\n"
                              f"{rollback}\n"
                              f"完整命令：\n{args['command']}\n"
                              "输入“确认执行”或 yes 批准，其他回答均拒绝。")
        if not isinstance(answer, str) or answer.strip().casefold() not in {"确认执行", "确认", "yes", "y"}:
            core.revoke(args["command"], target)
            core.audit("approve-deny", cmd=args["command"], level=level, target=target)
            return "[opsx 拒绝] 用户未明确批准此命令，不创建审批戳。"
        path = core.approve(args["command"], ttl_seconds=args.get("ttl_seconds", 900),
                            force=args.get("force", False), target=target, env=env,
                            uses=uses, impact=impact, snapshot_files=snap_files,
                            snapshot_cmds=snap_cmds)
    except (core.OpsxError, remote.RemoteError) as e:
        return f"[opsx 错误] {e}"
    stamp = json.loads(path.read_text(encoding="utf-8"))
    return (f"审批戳已创建（级别 {stamp['level']}，TTL {stamp['ttl']}s，"
            f"可用 {stamp.get('uses', 1)} 次，{core.format_env(env)}）。"
            "现在可重新执行该命令；执行一次即消耗一次，环境变化后旧戳失效。")


def _ops_revoke(args: dict, ctx: ToolContext) -> str:
    target = args.get("target") or None
    existed = core.revoke(args["command"], target)
    where = f"目标 {target}" if target else "本机"
    return (f"[完成] 已撤销{where}的审批戳" if existed
            else f"没有可撤销的{where}审批戳（可能已过期或未创建）")


def _snapshot_filespec(args: dict) -> list[str]:
    return [args["snapshot_file"]] if args.get("snapshot_file") else []


def _ops_exec(args: dict, ctx: ToolContext) -> str:
    """带快照的原子执行：等价于 opsx exec；带 target 时在目标机执行。"""
    cmd = args["command"]
    target = args.get("target") or None
    snap_cmds = []
    for spec in args.get("snapshot_cmds", []):
        snap_cmds.append((spec["command"], spec.get("name", "")))
    started = time.time()
    if target:
        code, sid, output = remote.exec_change(target, cmd, _snapshot_filespec(args), snap_cmds)
    else:
        code, sid, output = core.exec_change(cmd, _snapshot_filespec(args), snap_cmds)
    out_text = _cap(output, f"exec-{target or 'local'}") if output else "(无输出)"
    sid_text = f"snapshot_id={sid}（回滚：ops_rollback）" if sid else "无快照"
    where = f"目标 {target} " if target else "本机"
    import execution
    record = execution.find_latest(cmd, target, since=started, pid=os.getpid(), kind="exec")
    exec_text = ""
    if record is not None:
        exec_text = f"；execution_id={record['id']}（状态 {record['status']}）"
        if record["status"] == "unknown":
            # 这一条最重要：状态未知时不能让人以为"没生效"而直接重跑
            exec_text += ("\n**结果未知**：断线/超时导致无法确认命令是否完成。"
                          "不要重复执行同一条命令（会二次生效）；"
                          "同目标同命令的新执行会被拒绝，直到有人核实："
                          f"`opsx executions resolve {record['id']} "
                          "--observed-ok|--observed-failed`")
    # "没执行"的判据是"本次没有一条说明命令已下发的记录"，而不是退出码：
    # 命令自身也可能返回 2（审批未通过）之外的同名码，光看数字会互相误判。
    executed = record is not None and record.get("status") in ("succeeded", "failed",
                                                               "unknown")
    if not executed and code != 0:
        snapshot = f"，{sid_text}" if sid else ""
        return f"[opsx 拒绝] {where}本次未执行（exit={code}）{snapshot}{exec_text}。\n{out_text}"
    if record is not None and record["status"] == "unknown":
        return f"[结果未知] {where}执行结果需核实（exit={code}），{sid_text}{exec_text}。\n{out_text}"
    if code == 0:
        return f"[完成] {where}执行成功（exit=0），{sid_text}{exec_text}。\n{out_text}"
    return f"[失败] {where}执行未完成（exit={code}），{sid_text}{exec_text}。\n{out_text}"


def _ops_rollback(args: dict, ctx: ToolContext) -> str:
    sid = args["snapshot_id"]
    started, target = None, None
    try:
        plan = remote.rollback_plan(sid)
        target = plan.get("target")
        if plan["files"]:
            preview = core.format_rollback_plan(plan)
            if ctx.ask_user is None:
                return ("[opsx 拒绝] 回滚是写操作，当前形态没有可确认的用户通道，未改动任何文件。\n"
                        + preview)
            answer = ctx.ask_user(preview + "\n\n输入“确认回滚”或 yes 执行，其他回答均拒绝。")
            if not isinstance(answer, str) or answer.strip().casefold() not in {
                    "确认回滚", "确认", "yes", "y"}:
                core.audit("rollback-deny", snapshot_id=sid)
                return "[opsx 拒绝] 用户未批准回滚，文件未改动。"
        started = time.time()
        result = remote.rollback(sid, confirm=True)  # 远程快照推回，本机走 core
    except (core.OpsxError, remote.RemoteError) as e:
        result = f"[opsx 错误] {e}"
    if started is not None:
        import execution
        record = execution.find_latest(f"rollback {sid}", target, since=started,
                                       pid=os.getpid(), kind="rollback")
        if record is not None:
            result += (f"\nexecution_id={record['id']}（状态 {record['status']}；"
                       f"查看：opsx executions show {record['id']}）")
    return result


def _ops_executions(args: dict, ctx: ToolContext) -> str:
    """只读查询变更执行记录。刻意不提供"核实"动作：unknown 必须由人关闭。"""
    import execution
    # 不调 reconcile：这是声明为只读的工具，对账落盘交给 opsx executions / daemon。
    # list_records 会把"进程已死但仍是 running"的记录按 unknown/aborted 呈现。
    if args.get("action", "list") == "show":
        record = execution.get(args.get("execution_id") or "")
        if record is None:
            return f"[错误] 没有这条执行记录: {args.get('execution_id')!r}"
        return execution.format_record(record)
    tail = args.get("tail", 20)
    if type(tail) is not int or tail <= 0:
        return "[错误] tail 须为正整数"
    summary = execution.summary()
    text = execution.format_list(execution.list_records(
        tail=tail, status=args.get("status") or None))
    if summary["pending"]:
        text += (f"\n\n待核实 {summary['pending']} 条："
                 f"{', '.join(summary['pending_ids'])}\n"
                 "这些记录的变更结果未知，同目标同命令不会被放行重放。"
                 "请让人执行 `opsx executions resolve <id> --observed-ok|--observed-failed`。")
    return text


def _scenario_run(args: dict, ctx: ToolContext) -> str:
    """运行固定变更场景：预检→快照→执行→后验→回滚建议。

    审批在执行之前就绪（用户确认后建戳，exec_change 消耗）：预检失败时戳保留
    未消耗，修好预检项重跑即可，不需要重新确认。
    """
    import scenario
    name = args.get("name") or ""
    params = args.get("params") or {}
    target = args.get("target") or None
    try:
        scen = scenario.load(name)
        resolved = scenario.resolve_params(scen, params)
        exec_cmd = scenario.substitute(scen["exec"]["cmd"], resolved)
        tgt = scenario._exec_target(scen, target)
        env = core.resolve_env(exec_cmd, tgt)
    except (scenario.ScenarioError, core.OpsxError, remote.RemoteError) as e:
        return f"[场景错误] {core.redact(str(e))}"
    level, _ = core.classify(exec_cmd)

    # 确认框必须给出完整命令与全部探针：人批准的是整条链路，不是"跑个场景"
    preview = [f"变更场景 {name}：{scen['description']}",
               f"目标: {tgt or '本机'}｜参数: {resolved or '无'}"]
    preview += [f"预检 {i}: {scenario.substitute(c['cmd'], resolved)}"
                for i, c in enumerate(scen["precheck"], 1)]
    preview.append(f"执行: {exec_cmd}")
    preview += [f"快照文件: {scenario.substitute(pp, resolved)}"
                for pp in scen["exec"]["snapshot_files"]]
    preview += [f"快照命令 {i}: {scenario.substitute(c['command'], resolved)}"
                for i, c in enumerate(scen["exec"]["snapshot_cmds"], 1)]
    preview += [f"后验 {i}: {scenario.substitute(c['cmd'], resolved)}"
                for i, c in enumerate(scen["postcheck"], 1)]
    preview.append(f"风险级别: {level}｜执行环境: {core.format_env(env)}")
    preview_text = "\n".join(preview)

    if ctx.ask_user is None:
        return ("[opsx 拒绝] 变更场景需要人工确认，当前形态没有确认通道，"
                "未执行任何变更。\n" + preview_text)
    if level == "R3":
        prompt = (preview_text
                  + "\n\n**不可逆操作（R3）**：必须输入“确认执行”，其他回答均拒绝。")
        valid = {"确认执行"}
    else:
        prompt = preview_text + "\n\n输入“确认执行”或 yes 批准，其他回答均拒绝。"
        valid = {"确认执行", "确认", "yes", "y"}
    answer = ctx.ask_user(prompt)
    if not isinstance(answer, str) or answer.strip().casefold() not in valid:
        core.audit("scenario-deny", scenario=name, cmd=exec_cmd, target=tgt)
        return "[opsx 拒绝] 用户未批准，未执行任何变更。"
    if level != "R0":   # 只读执行命令没有也不需要审批戳
        try:
            core.approve(exec_cmd, target=tgt, env=env, force=(level == "R3"))
        except core.OpsxError as e:
            return f"[opsx 错误] 建审批戳失败（未执行任何变更）: {core.redact(str(e))}"
    try:
        result = scenario.run(name, params, target=target, actor="agent")
    except scenario.ScenarioError as e:
        return f"[场景错误] {core.redact(str(e))}"
    return scenario.format_result(result, with_suggestion=True)


SUBAGENT_TOOLS = frozenset({"bash", "read", "skill", "ops_list", "events", "ops_executions",
                            "logscan"})
# 无人值守定时巡检只能用只读工具：没有人类可以确认，approve/exec 一律不可见。
# inspect/logscan 是确定性检查（规则先跑，模型只解读），因此允许出现在无人值守上下文。
SCHEDULED_TOOLS = frozenset({"bash", "read", "skill", "workflow", "subagent",
                             "inspect", "logscan", "events", "ops_executions", "ops_list"})
SUBAGENT_MAX_TASKS = 4
SUBAGENT_MAX_TURNS = 8
WORKFLOW_PREV_LIMIT = 6000
CHECKS_PROMPT_LIMIT = 12000    # 注入 {checks} 的规则报告字符数
STEP_INDEX_CLIP = 400          # {steps} 里每步摘要的字符数

# 单次替换：替入内容里再出现 {prev} 之类的文本不会被二次替换成别的内容。
_PLACEHOLDER = re.compile(r"\{(prev|checks|steps)\}")


def _run_isolated(task: str, ctx: ToolContext, system: str | None = None) -> str:
    """跑一个只读子代理（子代理与 workflow 步骤共用）。时间预算沿父上下文。"""
    from . import loop  # 延迟导入：loop 依赖本模块
    sub_session = session_mod.Session()
    sub_ctx = ToolContext(ask_user=None, allowed=SUBAGENT_TOOLS, model=ctx.model,
                          session_id=sub_session.id, deadline=ctx.deadline)
    return loop.run_turn(task, sub_session, sub_ctx,
                         system=system or loop.SUBAGENT_PROMPT,
                         model=ctx.model, max_turns=SUBAGENT_MAX_TURNS)


def _subagent(args: dict, ctx: ToolContext) -> str:
    """并行只读排查：每个任务一个受限子代理，返回各自的结论与证据。"""
    if ctx.deadline is not None and time.monotonic() > ctx.deadline:
        return "[任务超时] 时间预算耗尽，未派生子代理"
    tasks = [str(t).strip() for t in args.get("tasks") or [] if str(t).strip()]
    if not tasks:
        return "[错误] tasks 不能为空：每个子任务写清目标与要回答的问题"
    if len(tasks) > SUBAGENT_MAX_TASKS:
        return f"[错误] 最多 {SUBAGENT_MAX_TASKS} 个并行子任务（收到 {len(tasks)} 个）"

    results: list[str] = [""] * len(tasks)

    def run(i: int, task: str) -> None:
        try:
            results[i] = _run_isolated(task, ctx)
        except Exception as e:  # noqa: BLE001 —— 单个子任务失败不拖垮其余
            results[i] = f"[失败] {e}"

    threads = [threading.Thread(target=run, args=(i, task), daemon=True)
               for i, task in enumerate(tasks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    report = "\n\n".join(f"## 子任务 {i}: {task}\n{results[i - 1] or '(无输出)'}"
                          for i, task in enumerate(tasks, 1))
    return _cap(report, "subagent")


def _step_reason(result: str) -> str:
    """步骤没真正完成的原因；空串表示完成。

    只认异常会漏判：模型空回复、run_turn 到 max_turns、以及被整任务时间预算
    截断，都不是完整解读，不能算成功。
    """
    from . import loop  # 标记常量与 loop 同源，避免两处各写一份字符串
    if not (result or "").strip():
        return "空输出"
    if result.startswith("[失败]"):
        return "异常"
    if loop.DEADLINE_MARK in result:
        return "时间预算耗尽"
    if loop.MAX_TURNS_MARK in result:
        return "超轮次保护"
    return ""


def _fill_prompt(template: str, values: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _steps_index(index: list) -> str:
    """前序步骤索引：名称、状态与摘要——汇总时显式引用所有步骤，而不是只看最后一步。"""
    if not index:
        return "（无）"
    lines = []
    for entry in index:
        summary = re.sub(r"\s+", " ", entry["text"]).strip()[:STEP_INDEX_CLIP]
        lines.append(f"- 步骤 {entry['n']}「{entry['name']}」："
                     f"{'完成' if entry['ok'] else '失败'}；{summary or '（无输出）'}")
    return "\n".join(lines)


def _workflow_footer(report: dict | None, index: list, failed: int,
                     checks_note: str) -> str:
    """汇总完整性由程序生成：规则结论、全部异常、每步状态都不经模型改写。"""
    lines = ["## 汇总完整性（程序生成，未经模型改写）"]
    if report is not None:
        s = report["summary"]
        lines.append(f"- 规则检查：{s['checks']} 项，异常 {s['critical']} / 警告 {s['warn']} / "
                     f"未知 {s['unknown']} / 错误 {s['error']}；结论 **{report['verdict']}**；"
                     f"完整性 {'完整' if report['complete'] else '不完整'}")
        anomalies = report["anomalies"]
        if anomalies:
            lines.append(f"- 异常与未采集项（规则检查，共 {len(anomalies)} 项，全部列出）：")
            lines += [f"  {i}. {item['line']}" for i, item in enumerate(anomalies, 1)]
        else:
            lines.append("- 异常与未采集项（规则检查）：无")
    elif checks_note:
        lines.append(f"- 规则检查：未执行（{checks_note}）")
        lines.append("- 结论：没有规则结论，本次不得汇总为正常")
    else:
        lines.append("- 规则检查：本 workflow 未配置 checks 块（只有模型解读，无确定性结论）")
    ok_steps = sum(1 for entry in index if entry["ok"])
    if index:
        lines.append("- 步骤状态：" + "｜".join(
            f"{entry['n']}「{entry['name']}」"
            + ("完成" if entry["ok"] else f"失败（{entry['reason']}）")
            for entry in index))
    lines.append(f"- LLM 解读：{ok_steps}/{len(index)} 步成功"
                 + ("；**解读不完整，需人工核对**" if failed else ""))
    if report is not None and report["verdict"] != "正常":
        lines.append("- 结论：本次巡检存在异常、未采集项或未检查分组，不得汇总为正常")
    elif report is not None and failed:
        lines.append("- 结论：规则检查通过；模型解读不完整，不影响规则结论")
    return "\n".join(lines)


def _evidence_error(ctx: ToolContext, message: str) -> str:
    note = core.redact(message)[:300]
    if note not in ctx.evidence_errors:
        ctx.evidence_errors.append(note)
    return f"\n[规则证据错误] {note}；解读不完整，需人工核对。"


def _inspect(args: dict, ctx: ToolContext) -> str:
    """确定性检查工具：规则结论不依赖模型，报告全文落盘。"""
    try:
        report = checks.run_checks(targets=args.get("targets") or "all",
                                   groups=args.get("groups") or None,
                                   deadline=ctx.deadline)
    except checks.CheckError as e:
        return f"[检查错误] {e}" + _evidence_error(ctx, "inspect 规则检查未能执行")
    text = checks.format_report(report)
    path = checks.save_report("inspect", text)
    note = ""
    if path is None:
        note = _evidence_error(ctx, "inspect 规则完整报告写入失败，无法保存完整检查证据")
    # 规则结果按会话落盘：通知与运行记录据此上报"模型不可用也成立"的异常
    if checks.save_signal(ctx.session_id, report, path) is None:
        note += _evidence_error(ctx, "inspect 规则结果摘要写入失败，无法保存检查证据")
    if len(text) > OUTPUT_LIMIT:
        tail = (f"\n...[报告共 {len(text)} 字符，已截断；完整内容: {path}"
                "（用 read 的 offset/limit 分段读取）]" if path else
                f"\n...[报告共 {len(text)} 字符，已截断；报告未能落盘]")
        # 异常清单是判断依据，不能被正常项表格或固定字符数挤掉。
        summary_end = text.find("\n\n## ", text.find("## 全部异常与未采集项"))
        keep = max(OUTPUT_LIMIT, summary_end) if summary_end != -1 else OUTPUT_LIMIT
        return text[:keep] + tail + note
    return (f"{text}\n\n" + (f"[全量报告] {path}" if path else "[报告未能落盘，以上为完整内容]")
            + note)


def _logscan(args: dict, ctx: ToolContext) -> str:
    """确定性日志粗筛（只读）：级别/关键词收口 + 聚类 + 时间分布。

    模型读语义前的规则第一道；样本经统一脱敏出口，命中敏感路径直接拒绝。
    """
    path = (args.get("path") or "").strip()
    target = (args.get("target") or "").strip()
    if path:
        real = os.path.realpath(os.path.expanduser(path))
        if (core.sensitive_path(path) or core.sensitive_path(real)
                or core.sensitive_dir(path) or core.sensitive_dir(real)):
            core.audit("logscan-deny", path=path, target=target or None)
            return ("[opsx 拒绝] 该路径命中敏感文件/目录规则（凭据、私钥、快照原件等），"
                    "日志粗筛同样不读取。确需排查请由人直接操作。")
    lines = args.get("lines") or logscan.DEFAULT_LINES
    top = args.get("top") or logscan.DEFAULT_TOP
    if type(lines) is not int or not 1 <= lines <= 2_000_000:
        return "[错误] lines 须为 1-2000000 的整数"
    if type(top) is not int or not 1 <= top <= 50:
        return "[错误] top 须为 1-50 的整数"
    try:
        desc, log_lines = logscan.detect_source(
            path=path, unit=(args.get("unit") or "").strip(),
            container=(args.get("container") or "").strip(),
            since=(args.get("since") or "").strip(),
            until=(args.get("until") or "").strip(),
            lines=lines, target=target)
        report = logscan.scan(log_lines, top=top,
                              pattern=(args.get("pattern") or "").strip() or None)
    except logscan.ScanSourceError as e:
        return f"[logscan 错误] {e}"
    except re.error as e:
        return f"[logscan 错误] pattern 正则非法: {e}"
    except (core.OpsxError, remote.RemoteError) as e:
        return f"[logscan 错误] {e}"
    core.audit("logscan", desc=desc, scanned=report["scanned"],
               groups=report["group_total"], partial=report["partial"],
               target=target or None)
    return _cap(logscan.format_report(desc, report), "logscan")


def _events(args: dict, ctx: ToolContext) -> str:
    """事件时间线查询：只读本地 events.jsonl，不触网、不调模型。"""
    n = args.get("tail", 20)
    if type(n) is not int or not 0 <= n <= 200:
        return "[错误] tail 须为 0-200 的整数"
    kind = (args.get("kind") or "").strip() or None
    if kind and not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", kind):
        return "[错误] kind 须为小写事件类型（如 anomaly-new、notify-failed）"
    return events_mod.format_tail(n=n, kind=kind)


def _workflow(args: dict, ctx: ToolContext) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        entries = workflow.catalog()
        if not entries:
            return (f"(无 workflow：在 {workflow.DIRS[0]} 或 "
                    f"{workflow.DIRS[1]} 放 *.json)")
        return "可用 workflow：\n" + "\n".join(
            f"{e['name']} — {e['description']}" if e["description"] else e["name"]
            for e in entries)
    try:
        wf = workflow.load(name)
    except workflow.WorkflowError as e:
        return f"[错误] {e}"

    sections: list[str] = []
    report, checks_text, checks_note = None, "", ""
    if wf.get("checks"):
        # 固定检查先跑：不调用模型，API 完全不可用时报告仍然成立。
        try:
            report = checks.run_checks(targets=wf["checks"]["targets"],
                                       groups=wf["checks"]["groups"],
                                       deadline=ctx.deadline)
            checks_text = checks.format_report(report)
        except checks.CheckError as e:
            checks_note = str(e)
            checks_text = ""
            _evidence_error(ctx, "workflow 规则检查未能执行")
        sections.append("## 规则检查（确定性探针，模型未参与）\n"
                        + (checks_text or f"[规则检查未执行] {checks_note}"))

    if checks_text:
        checks_prompt = checks_text[:CHECKS_PROMPT_LIMIT]
    elif checks_note:
        checks_prompt = (f"（规则检查未能执行：{checks_note}）"
                         "没有规则结论，本次不得给出「正常」结论。")
    else:
        checks_prompt = "（本 workflow 未配置规则检查，没有规则结论，不得据此宣称正常）"

    prev, index, failed = "", [], 0
    for i, step in enumerate(wf["steps"], 1):
        if ctx.deadline is not None and time.monotonic() > ctx.deadline:
            result = (f"[失败] 时间预算耗尽，本步未执行"
                      f"（含本步在内剩余 {len(wf['steps']) - i + 1} 步同样不再运行）")
            sections.append(f"## 步骤 {i}: {step['name']}\n{result}")
            index.append({"n": i, "name": step["name"], "ok": False,
                          "reason": "时间预算耗尽", "text": result})
            prev = result
            failed += 1
            continue
        prompt = _fill_prompt(step["prompt"], {
            "prev": prev[:WORKFLOW_PREV_LIMIT] if prev else "（无）",
            "checks": checks_prompt,
            "steps": _steps_index(index),
        })
        try:
            result = _run_isolated(prompt, ctx)
        except Exception as e:  # noqa: BLE001 —— 单步失败仍保留前述结果（API 不可用即此路径）
            result = f"[失败] {e}"
        reason = _step_reason(result)
        if reason:
            failed += 1
        # 步骤原文完整保留：报告文件是证据，不能在这里先截断
        sections.append(f"## 步骤 {i}: {step['name']}\n{result}")
        index.append({"n": i, "name": step["name"], "ok": not reason,
                      "reason": reason, "text": result})
        prev = result

    footer = _workflow_footer(report, index, failed, checks_note)
    body = "\n\n".join(sections)
    full = f"{body}\n\n{footer}"
    path = checks.save_report(f"workflow-{name}", full)
    note = ""
    if path is None:
        note = _evidence_error(ctx, "workflow 完整报告写入失败，无法保存完整检查证据")
    if report is not None:  # 只有真跑过规则检查才有信号可写
        if checks.save_signal(ctx.session_id, report, path) is None:
            note += _evidence_error(ctx, "workflow 规则结果摘要写入失败，无法保存检查证据")
    pointer = f"[全量报告] {path}" if path else "[报告未能落盘，以上为可见内容]"
    pointer += note
    if len(full) <= OUTPUT_LIMIT:
        return f"{full}\n\n{pointer}"
    # 截断时必须保住尾部汇总：它是"不得谎报正常"的最后一道程序约束，
    # 不能被步骤正文挤出可见范围。
    keep = max(1000, OUTPUT_LIMIT - len(footer) - 500)
    return (body[:keep] + f"\n...[中段已截断，原文 {len(body)} 字符]...\n\n"
            + footer + f"\n\n{pointer}")


def _schedule(args: dict, ctx: ToolContext) -> str:
    action = args.get("action")
    try:
        if action == "list":
            return schedule_mod.format_list()
        if action == "add":
            entry = schedule_mod.add(args.get("prompt") or "",
                                     every_seconds=args.get("every_seconds"),
                                     daily=args.get("daily"),
                                     timeout_seconds=args.get("timeout_seconds"))
            return (f"[schedule] 已添加 {entry['id']}：{schedule_mod.describe(entry)}，"
                    f"下次 {schedule_mod.fmt_ts(entry['next_run'])}"
                    "（无人值守只读运行，写操作会被拒绝）")
        if action == "remove":
            entry = schedule_mod.remove(args.get("id") or "")
            return f"[schedule] 已删除 {entry['id']}"
        if action == "pause":
            entry = schedule_mod.pause(args.get("id") or "")
            return f"[schedule] 已暂停 {entry['id']}（resume 恢复，暂停期不补跑）"
        if action == "resume":
            entry = schedule_mod.resume(args.get("id") or "")
            return (f"[schedule] 已恢复 {entry['id']}，下次 "
                    f"{schedule_mod.fmt_ts(schedule_mod._run_time(entry))}")
        if action == "runs":
            # 查询前对账：daemon 被 kill 后遗留的 running 记录应显示为中断
            schedule_mod.reconcile_runs()
            key = (args.get("id") or "").strip()
            prefix = False
            if key:
                try:
                    key = schedule_mod.resolve_id(key)
                except schedule_mod.ScheduleError:
                    prefix = True  # 任务已删除：按 id 前缀查历史
            tail = args.get("tail", 20)
            if type(tail) is not int or tail < 0:
                return "[错误] tail 须为非负整数"
            return schedule_mod.format_runs(task_id=key or None, tail=tail, prefix=prefix)
        return "[错误] action 应为 list / runs / add / remove / pause / resume"
    except schedule_mod.ScheduleError as e:
        return f"[schedule] {e}"


def _goal(args: dict, ctx: ToolContext) -> str:
    if not ctx.session_id:
        return "[目标] 当前上下文没有会话 id，goal 不可用"
    action = args.get("action")
    try:
        if action == "status":
            return goal_mod.format_one(goal_mod.get(ctx.session_id))
        if action == "create":
            if not ctx.goal_continuation:
                return ("[目标] 当前形态（-p / RPC / 定时任务）不会自动续跑："
                        "请直接在本轮完成；需要跨轮推进请在 REPL 里创建目标。")
            st = goal_mod.create(ctx.session_id, args.get("objective") or "",
                                 args.get("max_rounds") or goal_mod.DEFAULT_MAX_ROUNDS)
            return (f"[目标] 已创建 {st['id']}：{st['objective']}"
                    f"（最多续跑 {st['max_rounds']} 轮）")
        st = goal_mod.update(ctx.session_id, action, reason=args.get("reason") or "")
        return "[目标] " + goal_mod.format_one(st)
    except goal_mod.GoalError as e:
        return f"[目标] {e}"


def _remember(args: dict, ctx: ToolContext) -> str:
    try:
        entry = memory_mod.add(args.get("text") or "")
    except memory_mod.MemoryError as e:
        return f"[记忆] {e}"
    return (f"[记忆] 已记录：{entry.splitlines()[0]}"
            f"（之后每轮自动加载；文件 {memory_mod.path()}，过时条目可手动删除）")


def _ops_list(args: dict, ctx: ToolContext) -> str:
    targets = remote.describe() or ["(未注册，配置 ~/.ops-agent/targets.json)"]
    return "\n".join(core.format_state(core.collect_state()) + ["== targets =="] + targets)


REGISTRY: dict[str, dict] = {
    "bash": {
        "schema": _schema("bash",
                          "执行 shell 命令，仅放行 R0 只读命令；写命令必须走 ops_approve / ops_exec。"
                          "带 target 时在已注册的 SSH 目标机上执行（仅 R0）。",
                          {"command": {"type": "string", "description": "完整 shell 命令"},
                           "target": {"type": "string",
                                      "description": "已注册的 SSH 目标名（ops_list 可查）；省略=本机"}},
                          ["command"]),
        "handler": _bash, "read_only": False,
    },
    "read": {
        "schema": _schema("read",
                          f"读取本地文本文件（默认前 {READ_LIMIT} 行）；大文件用 offset/limit 分段读取。"
                          "私钥/凭据/敏感配置与快照原件不读取到模型上下文。",
                          {"path": {"type": "string"},
                           "offset": {"type": "integer", "description": "起始行号（1-based），默认 1"},
                           "limit": {"type": "integer", "description": f"最多读取行数，默认 {READ_LIMIT}"}},
                          ["path"]),
        "handler": _read, "read_only": True,
    },
    "skill": {
        "schema": _schema("skill",
                          "运维 playbook：不带 name 列出可用 skill，带 name 载入正文。"
                          "标准场景（如 nginx 5xx、磁盘满、服务重启）先查 playbook 再动手；"
                          "playbook 里的写操作仍须走变更单流程。",
                          {"name": {"type": "string",
                                    "description": "skill 名（文件名，可不带 .md）；省略=列目录"}},
                          []),
        "handler": _skill, "read_only": True,
    },
    "workflow": {
        "schema": _schema("workflow",
                          "巡检工作流：不带 name 列目录，带 name 按 JSON 定义的步骤顺序执行。"
                          "workflow 若配置了 checks 块，会先跑确定性规则检查（连通性/资源/服务，"
                          "不依赖模型），再逐步由只读子代理解读；"
                          "占位符 {checks} 规则报告、{prev} 上一步结论、{steps} 前序步骤索引。"
                          "汇总完整性由程序生成，全部异常都会列出。",
                          {"name": {"type": "string",
                                    "description": "workflow 名（workflows/*.json，可不带 .json）；省略=列目录"}},
                          []),
        "handler": _workflow, "read_only": True,
    },
    "events": {
        "schema": _schema("events",
                          "事件时间线：最近发生的异常出现/升级/提醒/恢复、通知发送/失败/静默、"
                          "运行失败/超时/中断/错过。只读本地记录，用来回答"
                          "\"最近有什么告警、通知发出去没有\"。",
                          {"tail": {"type": "integer",
                                    "description": "条数（默认 20，上限 200）"},
                           "kind": {"type": "string",
                                    "description": "只看某类事件，如 anomaly-new / notify-failed"}},
                          []),
        "handler": _events, "read_only": True,
    },
    "inspect": {
        "schema": _schema("inspect",
                          "确定性巡检：对已配置目标跑连通性/资源（磁盘·内存·负载）/服务三组固定探针，"
                          "返回结构化结果（观测值、阈值、采样时间、证据）。"
                          "规则结论由代码判定，不依赖模型；不可达或解析失败标 unknown/error，"
                          "不会汇总成正常。需要解释异常时你自己解读，或看返回的报告文件。",
                          {"targets": {"type": "string",
                                       "description": "\"all\"（本机+全部已注册目标）或单个目标名；默认 all"},
                           "groups": {"type": "array",
                                      "items": {"type": "string",
                                                "enum": ["connectivity", "resources", "services"]},
                                      "description": "只跑指定分组；省略=三组全跑"}},
                          []),
        "handler": _inspect, "read_only": True,
    },
    "logscan": {
        "schema": _schema("logscan",
                          "确定性日志粗筛（只读）：按错误级别/关键词收口、模式聚类去重、"
                          "时间桶分布，只把可疑聚类的少量样本交给你读语义。日志量大时先跑它，"
                          "不要直接用 grep/tail 逐轮硬翻原始日志。无命中≠没问题：粗筛规则外的"
                          "语义异常仍需针对性复核。",
                          {"path": {"type": "string", "description": "本机日志文件路径（三选一）"},
                           "unit": {"type": "string",
                                    "description": "journalctl 单元名，如 nginx.service（三选一）"},
                           "container": {"type": "string", "description": "docker 容器名（三选一）"},
                           "since": {"type": "string",
                                     "description": "起始时间 ISO（journalctl/docker 有效）"},
                           "until": {"type": "string", "description": "结束时间 ISO（journalctl 有效）"},
                           "lines": {"type": "integer", "default": 200000,
                                     "description": "最多扫描的最近行数（1-2000000）"},
                           "top": {"type": "integer", "default": 10,
                                   "description": "报告的可疑聚类数（1-50）"},
                           "pattern": {"type": "string",
                                       "description": "额外聚焦的正则（只分析命中的行）"},
                           "target": {"type": "string",
                                      "description": "SSH 目标名；远程仅支持 unit/container"}},
                          []),
        "handler": _logscan, "read_only": True,
    },
    "ask_user": {
        "schema": _schema("ask_user", "向用户提问并等待回答。写操作审批、歧义澄清时使用。",
                          {"question": {"type": "string", "description": "向用户展示的问题（含变更单）"}},
                          ["question"]),
        "handler": _ask_user, "read_only": True,
    },
    "subagent": {
        "schema": _schema("subagent",
                          f"只读排查子代理：并行执行多个独立检查任务（最多 {SUBAGENT_MAX_TASKS} 个），"
                          "各自返回结论与证据。子代理只能用 bash(R0)/read/skill/logscan/ops_list，"
                          "不能写；需要变更时把建议写进结论，由主代理走变更单流程。",
                          {"tasks": {"type": "array", "items": {"type": "string"},
                                     "description": "子任务描述（写清目标主机、命名空间、日志路径与要回答的问题）"}},
                          ["tasks"]),
        "handler": _subagent, "read_only": True,
    },
    "schedule": {
        "schema": _schema("schedule",
                          "定时巡检：list 查看；add 添加（every_seconds 或 daily 'HH:MM' 二选一，"
                          "timeout_seconds 是整任务时间预算，默认 900s）；pause/resume 暂停/恢复"
                          "（暂停期不补跑）；runs 查看运行历史（含中断/错过的执行）；"
                          "remove 删除。任务 id 可用唯一前缀。",
                          {"action": {"type": "string",
                                      "enum": ["list", "runs", "add", "remove", "pause", "resume"]},
                           "prompt": {"type": "string", "description": "巡检指令（无人值守、只读）"},
                           "every_seconds": {"type": "integer", "description": "每隔多少秒（>= 60）"},
                           "daily": {"type": "string", "description": "每天本地时间 HH:MM"},
                           "timeout_seconds": {"type": "integer",
                                               "description": "整任务时间预算秒数（60-86400，默认 900）"},
                           "id": {"type": "string", "description": "任务 id（可用唯一前缀）"},
                           "tail": {"type": "integer", "description": "runs 的条数，默认 20"}},
                          ["action"]),
        "handler": _schedule, "read_only": False,
    },
    "goal": {
        "schema": _schema("goal",
                          "长目标：跨自动续跑推进的多步目标（仅 REPL 会自动续跑）。"
                          "action=create 登记（objective 必填）；status 查看；"
                          "complete 完成；blocked 受阻（需 reason）；pause/resume 暂停/恢复。"
                          "单轮能完成的任务不要建目标。",
                          {"action": {"type": "string",
                                      "enum": ["create", "status", "complete", "blocked",
                                               "pause", "resume"]},
                           "objective": {"type": "string", "description": "目标描述"},
                           "max_rounds": {"type": "integer",
                                          "description": f"最多续跑轮数（默认 {goal_mod.DEFAULT_MAX_ROUNDS}，上限 {goal_mod.MAX_ROUNDS}）"},
                           "reason": {"type": "string", "description": "blocked 的原因"}},
                          ["action"]),
        "handler": _goal, "read_only": False,
    },
    "remember": {
        "schema": _schema("remember",
                          "记入长期记忆（跨会话自动加载）：确认过的主机事实（OS/服务清单/已排除项）、"
                          "用户偏好、环境约定。只记结论（不超过 500 字符）；一次性状态不要记。"
                          "写法约定：`主机: 属性 = 值`、依赖记 `X depends_on Y`，同类事实用词一致。",
                          {"text": {"type": "string", "description": "要记住的事实结论"}},
                          ["text"]),
        "handler": _remember, "read_only": False,
    },
    "ops_approve": {
        "schema": _schema("ops_approve",
                          "提交变更单后调用：工具向用户确认完整变更单（影响面+快照声明+命令），"
                          "明确批准后才创建审批戳（R3 需 force=true）。"
                          "戳绑定实际主机、工作目录、k8s context（kubectl 变更须在命令中写 --context）；"
                          "带 target 时只对该目标有效。环境变化后旧戳失效。",
                          {"command": {"type": "string"},
                           "impact": {"type": "string",
                                      "description": "影响面（必填）：将改什么状态、影响哪些服务。"
                                                     "先跑只读预演再填写：k8s 用 kubectl diff 或 "
                                                     "kubectl apply --dry-run=server --context <c> -f ...；"
                                                     "文件对比目标内容与当前内容"},
                           "snapshot_files": {"type": "array", "items": {"type": "string"},
                                              "description": "执行前需快照的文件（声明后 ops_exec 必须带同一份）"},
                           "snapshot_cmds": {"type": "array", "items": {"type": "object",
                                             "properties": {"command": {"type": "string"},
                                                            "name": {"type": "string"}},
                                             "required": ["command"]}},
                           "target": {"type": "string", "description": "已注册的 SSH 目标名；省略=本机"},
                           "ttl_seconds": {"type": "integer", "default": 900},
                           "force": {"type": "boolean", "default": False},
                           "uses": {"type": "integer", "default": 1,
                                    "description": "该授权允许执行几次（默认 1）"}}, ["command"]),
        "handler": _ops_approve, "read_only": False,
    },
    "ops_revoke": {
        "schema": _schema("ops_revoke",
                          "撤销审批戳：用户取消变更、或建戳后决定不执行时使用。",
                          {"command": {"type": "string"},
                           "target": {"type": "string", "description": "已注册的 SSH 目标名；省略=本机"}},
                          ["command"]),
        "handler": _ops_revoke, "read_only": False,
    },
    "ops_exec": {
        "schema": _schema("ops_exec",
                          "原子执行变更：校验审批戳→快照→执行→审计。带 target 时经 SSH 在目标机执行，"
                          "快照拉回本机存档。审批戳若声明过快照计划，本工具的快照参数必须与之完全一致，"
                          "否则拒绝执行。",
                          {"command": {"type": "string"},
                           "target": {"type": "string", "description": "已注册的 SSH 目标名；省略=本机"},
                           "snapshot_file": {"type": "string", "description": "执行前需快照的文件（可选；"
                                                                              "审批声明过则必须与之一致）"},
                           "snapshot_cmds": {"type": "array", "items": {"type": "object",
                                             "properties": {"command": {"type": "string"},
                                                            "name": {"type": "string"}},
                                             "required": ["command"]}}}, ["command"]),
        "handler": _ops_exec, "read_only": False,
    },
    "ops_rollback": {
        "schema": _schema("ops_rollback",
                          "按 snapshot_id 回滚：先展示实际目标、影响文件与差异并请求用户确认，"
                          "执行前自动保存回滚前副本（本机快照恢复文件，远程快照推回目标机）。",
                          {"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
        "handler": _ops_rollback, "read_only": False,
    },
    "ops_executions": {
        "schema": _schema("ops_executions",
                          "查变更执行记录（只读）：status=running/unknown 表示结果尚未确定；"
                          "unknown 需由人核实（opsx executions resolve），模型不能自行关闭。",
                          {"action": {"type": "string", "enum": ["list", "show"],
                                      "default": "list"},
                           "execution_id": {"type": "string"},
                           "tail": {"type": "integer", "default": 20},
                           "status": {"type": "string",
                                      "enum": ["running", "succeeded", "failed", "unknown",
                                               "aborted"]}},
                          []),
        "handler": _ops_executions, "read_only": True,
    },
    "scenario_run": {
        "schema": _schema("scenario_run",
                          "运行固定变更场景：预检→快照→执行→后验→回滚建议。"
                          "执行前会向用户请求确认；后验失败时即使命令 exit=0 也报验证失败。"
                          "无人值守上下文不可用。",
                          {"name": {"type": "string", "description": "场景名（opsx scenario list 可查）"},
                           "params": {"type": "object",
                                      "description": "场景参数，如 {\"service\": \"nginx\"}"},
                           "target": {"type": "string",
                                      "description": "SSH 目标名；省略=场景声明或本机"}},
                          ["name"]),
        "handler": _scenario_run, "read_only": False,
    },
    "ops_list": {
        "schema": _schema("ops_list", "列出快照、审批戳与已注册的 SSH 目标。", {}, []),
        "handler": _ops_list, "read_only": True,
    },
}


def tool_schemas(ctx: ToolContext | None = None) -> list[dict]:
    """按上下文白名单过滤：子代理只看到只读工具。"""
    allowed = ctx.allowed if ctx else None
    return [t["schema"] for name, t in REGISTRY.items()
            if allowed is None or name in allowed]


def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    """工具出口统一脱敏：模型收到的工具结果与会话记录都不含明文密钥。"""
    return core.redact(_dispatch(name, args, ctx))


def _dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    if ctx.allowed is not None and name not in ctx.allowed:
        return (f"[opsx 拒绝] 当前为只读上下文（子代理 / 定时任务），不能使用 {name}；"
                "需要变更请把建议写进结论，由人走变更单流程。")
    tool = REGISTRY.get(name)
    if tool is None:
        return f"[错误] 未知工具: {name}"
    try:
        core.validate_arguments(args, tool["schema"]["function"]["parameters"])
        return tool["handler"](args, ctx)
    except (KeyError, TypeError) as e:
        return f"[错误] 工具参数异常: {e}"
    except (core.OpsxError, remote.RemoteError) as e:
        return f"[opsx 错误] {e}"
    except Exception as e:  # noqa: BLE001 —— 工具异常不炸 loop，回给模型
        return f"[错误] 工具执行异常: {e}"
