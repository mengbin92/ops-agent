"""长目标：跨自动续跑持久化的多步目标（REPL 每轮结束后自动续跑）。

状态存 ~/.ops-agent/goals.json（按 session id）。goal 只负责"别停下来"：
写操作仍逐次走审批，轮次上限防止无限续跑；-p/RPC/定时任务不会自动续跑。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from .schedule import _locked  # noqa: E402

DEFAULT_MAX_ROUNDS = 6
MAX_ROUNDS = 20
OBJECTIVE_LIMIT = 500


class GoalError(Exception):
    pass


def _file() -> Path:
    return core.STATE_DIR / "goals.json"


def _load() -> dict:
    p = _file()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GoalError(f"目标文件损坏: {p}（{e}）") from e
    if not isinstance(data, dict):
        raise GoalError(f"目标文件格式异常: {p}")
    for st in data.values():
        if (not isinstance(st, dict)
                or not isinstance(st.get("id"), str) or not st["id"]
                or not isinstance(st.get("objective"), str) or not st["objective"].strip()
                or st.get("status") not in ("active", "paused", "completed", "blocked")
                or type(st.get("max_rounds")) is not int
                or not 1 <= st["max_rounds"] <= MAX_ROUNDS
                or type(st.get("rounds_used")) is not int
                or not 0 <= st["rounds_used"] <= st["max_rounds"]):
            raise GoalError(f"目标文件格式异常: {p}")
    return data


def _save(state: dict) -> None:
    # 会话被删（保留上限轮转）后目标也一并清掉，goals.json 不会只增不减
    sessions = core.STATE_DIR / "sessions"
    live = {k: v for k, v in state.items() if (sessions / f"{k}.jsonl").exists()}
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_name(f".goals-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def get(session_id: str) -> dict | None:
    return _load().get(session_id)


def create(session_id: str, objective: str, max_rounds: int = DEFAULT_MAX_ROUNDS) -> dict:
    objective = (objective or "").strip()
    if not objective:
        raise GoalError("objective 不能为空")
    if len(objective) > OBJECTIVE_LIMIT:
        raise GoalError(f"objective 太长（上限 {OBJECTIVE_LIMIT} 字符）")
    try:
        rounds = int(max_rounds or DEFAULT_MAX_ROUNDS)
    except (TypeError, ValueError) as e:
        raise GoalError("max_rounds 必须是整数") from e
    rounds = max(1, min(rounds, MAX_ROUNDS))
    with _locked("goals.mutex"):
        state = _load()
        current = state.get(session_id)
        if current and current.get("status") in ("active", "paused"):
            raise GoalError(f"已有进行中的目标 {current['id']}（{current['status']}）："
                            "先 complete/blocked，或用已有目标继续")
        now = time.time()
        st = {"id": "g-" + uuid.uuid4().hex[:6], "objective": objective, "status": "active",
              "rounds_used": 0, "max_rounds": rounds, "created": now, "updated": now, "reason": ""}
        state[session_id] = st
        _save(state)
        core.audit("goal-create", session=session_id, id=st["id"], objective=objective)
        return st


def update(session_id: str, action: str, reason: str = "") -> dict:
    with _locked("goals.mutex"):
        state = _load()
        st = state.get(session_id)
        if not st:
            raise GoalError("当前没有目标")
        reason = (reason or "").strip()
        if action == "complete":
            st["status"], st["reason"] = "completed", reason
        elif action == "blocked":
            if not reason:
                raise GoalError("blocked 需要说明原因（reason）")
            st["status"], st["reason"] = "blocked", reason
        elif action == "pause":
            if st["status"] not in ("active", "paused"):
                raise GoalError(f"目标状态为 {st['status']}，不能暂停")
            if st["status"] == "active":
                st["reason"] = ""
            st["status"] = "paused"
        elif action == "resume":
            if st["status"] == "completed":
                raise GoalError("目标已完成；要开新目标请 create")
            st["status"], st["reason"] = "active", ""
            st["rounds_used"] = 0  # 显式恢复给一轮新预算
        else:
            raise GoalError(f"未知动作: {action}")
        st["updated"] = time.time()
        state[session_id] = st
        _save(state)
        core.audit("goal-update", session=session_id, id=st["id"], action=action, status=st["status"])
        return st


def format_one(st: dict | None) -> str:
    if not st:
        return "(无目标)"
    lines = [f"目标 {st['id']}：{st['objective']}",
             f"状态 {st['status']}，续跑 {st['rounds_used']}/{st['max_rounds']}"]
    if st.get("reason"):
        lines.append(f"原因：{st['reason']}")
    return "\n".join(lines)


def continuation(session_id: str) -> str | None:
    """需要续跑时返回注入文本并消耗一轮预算；否则 None（到上限自动暂停）。"""
    with _locked("goals.mutex"):
        state = _load()
        st = state.get(session_id)
        if not st or st.get("status") != "active":
            return None
        if st["rounds_used"] >= st["max_rounds"]:
            st.update(status="paused", reason="已达续跑上限；/goal resume 可再给一轮预算")
            st["updated"] = time.time()
            _save(state)
            return None
        st["rounds_used"] += 1
        st["updated"] = time.time()
        _save(state)
        return (f"[目标自动续跑 {st['rounds_used']}/{st['max_rounds']}] 目标：{st['objective']}\n"
                "继续推进该目标，不要重复已完成的工作；完成后用 goal 工具 action=complete，"
                "受阻则 action=blocked 并说明原因。")
