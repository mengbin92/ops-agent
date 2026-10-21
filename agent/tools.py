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
from . import goal as goal_mod  # noqa: E402
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
    deadline：整任务时间预算（time.monotonic 秒），None = 不设限；轮次边界停止。
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
    if target:  # 远程只读：经 SSH 目标执行
        code, out = remote.run(target, cmd)
        return f"[target={target} exit={code}]\n" + _cap(out, f"bash-{target}")
    try:
        r = core.run_shell(cmd, timeout=BASH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"[超时] 命令超过 {BASH_TIMEOUT}s 未结束，已终止"
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
            or core.sensitive_path(str(real))):
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
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for _ in range(offset - 1):
                part = f.readline(OUTPUT_LIMIT + 2)
                if not part:
                    return f"[错误] offset={offset} 超出文件行数"
                while part and not part.endswith("\n"):
                    part = f.readline(OUTPUT_LIMIT + 2)
            for _ in range(limit):
                line = f.readline(OUTPUT_LIMIT - size + 2)
                if not line:
                    break
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
    return ctx.ask_user(args["question"])


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
        warning = "\n不可逆，无回滚方案。" if level == "R3" else ""
        answer = ctx.ask_user(f"批准此变更？\n执行环境：{core.format_env(env)}\n"
                              f"风险级别：{level}{warning}\n"
                              f"完整命令：\n{args['command']}\n"
                              "输入“确认执行”或 yes 批准，其他回答均拒绝。")
        if not isinstance(answer, str) or answer.strip().casefold() not in {"确认执行", "确认", "yes", "y"}:
            core.revoke(args["command"], target)
            core.audit("approve-deny", cmd=args["command"], level=level, target=target)
            return "[opsx 拒绝] 用户未明确批准此命令，不创建审批戳。"
        path = core.approve(args["command"], ttl_seconds=args.get("ttl_seconds", 900),
                            force=args.get("force", False), target=target, env=env)
    except (core.OpsxError, remote.RemoteError) as e:
        return f"[opsx 错误] {e}"
    stamp = json.loads(path.read_text(encoding="utf-8"))
    return (f"审批戳已创建（级别 {stamp['level']}，TTL {stamp['ttl']}s，"
            f"{core.format_env(env)}）。现在可重新执行该命令；环境变化后旧戳失效。")


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
    if target:
        code, sid, output = remote.exec_change(target, cmd, _snapshot_filespec(args), snap_cmds)
    else:
        code, sid, output = core.exec_change(cmd, _snapshot_filespec(args), snap_cmds)
    out_text = _cap(output, f"exec-{target or 'local'}") if output else "(无输出)"
    sid_text = f"snapshot_id={sid}（回滚：ops_rollback）" if sid else "无快照"
    where = f"目标 {target} " if target else "本机"
    if code == 0:
        return f"[完成] {where}执行成功（exit=0），{sid_text}。\n{out_text}"
    return f"[失败] {where}执行未完成（exit={code}），{sid_text}。\n{out_text}"


def _ops_rollback(args: dict, ctx: ToolContext) -> str:
    sid = args["snapshot_id"]
    try:
        plan = remote.rollback_plan(sid)
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
        return remote.rollback(sid, confirm=True)  # 远程快照推回，本机走 core
    except (core.OpsxError, remote.RemoteError) as e:
        return f"[opsx 错误] {e}"


SUBAGENT_TOOLS = frozenset({"bash", "read", "skill", "ops_list"})
# 无人值守定时巡检只能用只读工具：没有人类可以确认，approve/exec 一律不可见。
# inspect 是确定性检查（规则先跑，模型只解读），因此允许出现在无人值守上下文。
SCHEDULED_TOOLS = frozenset({"bash", "read", "skill", "workflow", "subagent",
                             "inspect", "ops_list"})
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


def _inspect(args: dict, ctx: ToolContext) -> str:
    """确定性检查工具：规则结论不依赖模型，报告全文落盘。"""
    try:
        report = checks.run_checks(targets=args.get("targets") or "all",
                                   groups=args.get("groups") or None)
    except checks.CheckError as e:
        return f"[检查错误] {e}"
    text = checks.format_report(report)
    path = checks.save_report("inspect", text)
    if len(text) > OUTPUT_LIMIT:
        tail = (f"\n...[报告共 {len(text)} 字符，已截断；完整内容: {path}"
                "（用 read 的 offset/limit 分段读取）]" if path else
                f"\n...[报告共 {len(text)} 字符，已截断；报告未能落盘]")
        return text[:OUTPUT_LIMIT] + tail
    return f"{text}\n\n" + (f"[全量报告] {path}" if path else "[报告未能落盘，以上为完整内容]")


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
                                       groups=wf["checks"]["groups"])
            checks_text = checks.format_report(report)
        except checks.CheckError as e:
            checks_note = str(e)
            checks_text = ""
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
    pointer = f"[全量报告] {path}" if path else "[报告未能落盘，以上为可见内容]"
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
    "ask_user": {
        "schema": _schema("ask_user", "向用户提问并等待回答。写操作审批、歧义澄清时使用。",
                          {"question": {"type": "string", "description": "向用户展示的问题（含变更单）"}},
                          ["question"]),
        "handler": _ask_user, "read_only": True,
    },
    "subagent": {
        "schema": _schema("subagent",
                          f"只读排查子代理：并行执行多个独立检查任务（最多 {SUBAGENT_MAX_TASKS} 个），"
                          "各自返回结论与证据。子代理只能用 bash(R0)/read/skill/ops_list，不能写；"
                          "需要变更时把建议写进结论，由主代理走变更单流程。",
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
                          "提交变更单后调用：工具向用户确认此命令，明确批准后才创建审批戳（R3 需 force=true）。"
                          "戳绑定实际主机、工作目录、k8s context（kubectl 变更须在命令中写 --context）；"
                          "带 target 时只对该目标有效。环境变化后旧戳失效。",
                          {"command": {"type": "string"},
                           "target": {"type": "string", "description": "已注册的 SSH 目标名；省略=本机"},
                           "ttl_seconds": {"type": "integer", "default": 900},
                           "force": {"type": "boolean", "default": False}}, ["command"]),
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
                          "快照拉回本机存档。",
                          {"command": {"type": "string"},
                           "target": {"type": "string", "description": "已注册的 SSH 目标名；省略=本机"},
                           "snapshot_file": {"type": "string", "description": "执行前需快照的文件（可选）"},
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
