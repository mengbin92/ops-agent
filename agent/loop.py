"""agent loop：消息装配 → LLM 调用 → 工具分发 → 结果回填，最大轮次保护。

与前端解耦：前端提供 ToolContext（人机回路）与 on_event 回调（渲染）。
"""
from __future__ import annotations

import json

from . import llm, session as sess_mod, tools

MAX_TURNS = 40

# basic compaction：发送给 LLM 的消息总字符数超阈值时，把早期 tool result 裁剪为占位符。
# 只裁剪"发送内容"，session 文件保留完整记录（审计与追溯不受影响）。
COMPACT_THRESHOLD = 60_000
COMPACT_KEEP_RECENT = 8       # 最近 N 条消息不裁剪
COMPACT_MIN_CONTENT = 200     # 短输出不值得裁剪
COMPACT_PLACEHOLDER = "[历史工具输出已裁剪，如需细节请重新执行命令]"


def _message_size(m: dict) -> int:
    return len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or ""))


def compact(messages: list[dict]) -> list[dict]:
    if sum(_message_size(m) for m in messages) <= COMPACT_THRESHOLD:
        return messages
    cutoff = len(messages) - COMPACT_KEEP_RECENT
    out = []
    for i, m in enumerate(messages):
        if (i < cutoff and m.get("role") == "tool"
                and len(str(m.get("content") or "")) > COMPACT_MIN_CONTENT):
            m = dict(m, content=COMPACT_PLACEHOLDER)
        out.append(m)
    return out

SYSTEM_PROMPT = """你是 ops-agent，一个独立运维智能体。负责本机 macOS、远程 Linux（SSH）、K8s、Docker 环境的运维操作。

## 最高原则
1. **默认只读**：优先用只读命令查状态、看日志、定位问题。写操作是例外。
2. **写操作强制流程**：输出变更单（目标环境、完整命令、风险说明、回滚计划、快照计划）→
   用 ask_user 请用户确认 → 用 ops_approve 创建审批戳 → 用 ops_exec（带快照参数）原子执行。
   用户取消变更时，用 ops_revoke 立即撤销已建审批戳。
3. **禁止拆分命令规避审批**、禁止在复合命令里夹带未申报的命令段。

## 风险分级
- R0 只读：kubectl get/logs、systemctl status、docker ps/logs 等，bash 直接执行。
- R1 可逆低风险写、R2 高风险写：走上述流程。
- R3 不可逆（rm -rf、kubectl delete、DROP 等）：默认拒绝；仅用户会话内显式确认后，
  ops_approve force=true 才可执行，变更单须注明"不可逆，无回滚方案"。

## 远程目标（SSH）
- 写操作只能发往 ops_list 已列出的目标（注册表 `~/.ops-agent/targets.json`）；
  不要对未注册主机做写操作。
- 只读排查：优先用 bash 的 target 参数（命令仍须为 R0，如 systemctl status / journalctl /
  cat / kubectl get）。白名单允许的只读 ssh 直连（`ssh 主机 "systemctl status"` 等）仍可用，但仅限读。
- 写操作：变更单写明目标；ops_approve 与 ops_exec 必须带同一个 target——审批戳按「目标+命令」
  绑定，本机戳不能用于远程目标。
- 快照在目标机采集后拉回本机存档（snapshot_file / snapshot_cmds 用法不变）；
  ops_rollback 按快照自动识别，远程文件推回目标机。

## 技能库（skills）
- 标准运维场景（nginx 5xx、磁盘满、服务重启、k8s 排查等）先用 `skill` 查看 playbook
  （不带 name 列目录），再按步骤执行；playbook 里的只读命令直接用，写操作仍走变更单流程。

## 子代理（只读排查）
- 需要同时确认多个独立方向（多台主机、多个命名空间、日志+配置+资源）时，用 `subagent`
  一次下发 2–4 个任务并行排查；每个任务写清目标、路径与要回答的问题。
- 子代理只读（bash R0 / read / skill / ops_list），会独立跑 8 轮以内并返回结论与证据。
- 子代理给出的变更建议由你汇总后走变更单流程，不要直接执行它建议的写命令。

## 定时巡检（schedule）
- 用户要"每天 / 每 N 分钟检查一次"时，用 `schedule` 工具 action=add（`every_seconds` 或
  `daily: "HH:MM"` 二选一）；action=list 查看、action=remove 取消。
- 定时任务在无人值守环境只读运行：prompt 要写清"只读检查并报告异常，需要变更只给建议"。

## 巡检工作流（workflow）
- 需要按固定步骤反复巡检时，先 `workflow` 列目录，再 `workflow {"name": "..."}` 运行；
  每步是独立只读子代理，后一步可用 `{prev}` 引用前一步结论。

## 长目标（goal）
- 用户在 REPL 里要求"持续推进 / 做到完成为止"这类多轮目标时，用 `goal` action=create 登记；
  每轮结束会自动续跑，完成用 action=complete，受阻用 action=blocked（说明原因）。
- 单轮能完成的任务不要建目标；目标续跑中的写操作仍逐次需要用户确认。

## 报告义务
每次变更执行后报告：目标（本机/远程）、执行的命令、退出码、snapshot_id（用 ops_list 查询）、回滚命令 opsx rollback <id>。

## 输出
- 默认使用中文。
- 只读排查时先给结论再给证据（命令+关键输出摘要）。"""


SUBAGENT_PROMPT = """你是 ops-agent 的只读排查子代理。主代理把独立检查任务并行下发给你，
你只负责查清事实并给出结论，不做变更。

## 规则
- 只读：bash 仅执行 R0 命令；可用 read 看文件、skill 查 playbook、ops_list 看快照与目标。
- 需要标准步骤时先 `skill` 查看 playbook（不带 name 列目录）。
- 不做写操作，不向用户提问，不等待确认；需要变更时把建议写进结论，由主代理走变更单流程。
- 先给结论（最可能原因/当前状态），再给证据（命令 + 关键输出摘要）。
- 输出简洁，便于主代理汇总；不确定就说不确定，不要编造。
- 默认中文。"""


def run_turn(user_input: str, session: sess_mod.Session, ctx: tools.ToolContext,
             on_event=None, model: str | None = None, on_delta=None,
             system: str | None = None, max_turns: int = MAX_TURNS) -> str:
    """执行一轮用户输入直到模型给出最终回复。on_event(kind, payload) 用于前端渲染。

    kind: "text"（助手完整文本，流式模式下仅在无 on_delta 时发出）| "tool" | "info"
    on_delta：流式文本增量回调（传入即启用 SSE 流式）。
    system/max_turns：子代理用独立的只读提示与轮次上限。
    返回最终文本。
    """
    emit = on_event or (lambda kind, payload: None)
    session.append({"role": "user", "content": user_input})
    final_text = ""
    for _ in range(max_turns):
        messages = compact([{"role": "system", "content": system or SYSTEM_PROMPT}]
                           + session.messages)
        msg = llm.chat(messages, tools=tools.tool_schemas(ctx), model=model, on_delta=on_delta)
        # 规范化为可持久化结构
        record = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            record["tool_calls"] = msg["tool_calls"]
        session.append(record)
        if msg.get("content"):
            # 非流式：一次性发完整文本；流式：通知前端收尾该段流
            emit("text" if on_delta is None else "stream_end", msg["content"])
            final_text = msg["content"]
        if not msg.get("tool_calls"):
            return final_text
        for call in msg["tool_calls"]:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            emit("tool", {"phase": "call", "name": name, "args": args})
            result = tools.dispatch(name, args, ctx)
            emit("tool", {"phase": "result", "name": name, "result": result[:500]})
            session.append({"role": "tool", "tool_call_id": call["id"],
                            "name": name, "content": result})
    return final_text or "[已达最大轮次保护，终止]"
