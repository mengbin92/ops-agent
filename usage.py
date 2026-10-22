#!/usr/bin/env python3
"""LLM 调用记账与日预算。

为什么需要这一层：无人值守巡检一晚上可以打几千次 LLM 调用，429/预算都是第二天
看账单才发现。预算的意义不是"省钱统计"，而是一条**确定性熔断**：超预算立即停止
新的 LLM 调用并留下报告，而确定性规则检查（inspect / 场景预检后验）不经过 LLM，
照常运行——这是"LLM 不可用仍能运维"这一设计原则在预算维度的落地。

记账：`~/.ops-agent/usage.jsonl`（逐条，0600，超过 10MB 轮转一代）。
计数：`~/.ops-agent/usage-state.json`（按自然日聚合，与记账文件独立损坏互不影响）。
预算：`~/.ops-agent/budget.json`（0600）：{"enabled": true, "daily_calls": 500,
"daily_tokens": 500000}。0/None 表示该项不限。

已知边界：预算是本机自我约束，不是账单对账——多进程并发下计数可能差几次；
token 数以 provider 返回的 usage 为准，流式响应没有 usage 时按字符粗估。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

DEFAULT_BUDGET = {"enabled": True, "daily_calls": 500, "daily_tokens": 500_000}
BUDGET_FIELDS = ("enabled", "daily_calls", "daily_tokens")
USAGE_ROTATE_BYTES = 10 * 1024 * 1024
MAX_RECORDED_TOKENS = 100_000_000  # 单条记账的 token 数上限（防 provider 返回垃圾）


def _usage_file() -> Path:
    return core.STATE_DIR / "usage.jsonl"


def _state_file() -> Path:
    return core.STATE_DIR / "usage-state.json"


def _budget_file() -> Path:
    return core.STATE_DIR / "budget.json"


def _day_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


def _state_lock():
    """usage-state.json 的互斥。POSIX 用 flock；不支持的平台退化为无锁。

    **不可重入**：同进程在锁内再次进入会永久阻塞（fcntl 按 open file
    description 计数，每次新 open 都是独立请求）。所有 state 写者都必须
    过这一把锁——无锁写者会让"告警去重标记/计数"被整文件覆盖丢失。"""
    import contextlib
    try:
        import fcntl
    except ImportError:
        @contextlib.contextmanager
        def _noop():
            yield
        return _noop()

    @contextlib.contextmanager
    def _locked():
        core._ensure_state_dir()
        with open(core.STATE_DIR / "usage-state.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    return _locked()


# ---------------------------------------------------------------- 预算配置

def load_budget() -> dict:
    path = _budget_file()
    cfg = dict(DEFAULT_BUDGET)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise core.OpsxError(f"budget.json 损坏: {e}") from e
        if not isinstance(raw, dict):
            raise core.OpsxError("budget.json 须为 JSON 对象")
        extra = sorted(set(raw) - set(BUDGET_FIELDS))
        if extra:
            raise core.OpsxError(f"budget.json 有未知字段: {', '.join(extra)}")
        for key in ("daily_calls", "daily_tokens"):
            if key in raw:
                value = raw[key]
                if value is not None and (type(value) is not int or value < 0):
                    raise core.OpsxError(f"budget.json 的 {key} 须为非负整数或 null（0/null=不限）")
                cfg[key] = value or None
        if "enabled" in raw:
            if type(raw["enabled"]) is not bool:
                raise core.OpsxError("budget.json 的 enabled 须为布尔值")
            cfg["enabled"] = raw["enabled"]
    return cfg


def save_budget(cfg: dict) -> Path:
    if not isinstance(cfg, dict):
        raise core.OpsxError("预算配置须为对象")
    for key in ("daily_calls", "daily_tokens"):
        value = cfg.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise core.OpsxError(f"{key} 须为非负整数或 null（0/null=不限）")
    path = _budget_file()
    core._ensure_state_dir()
    out = {"enabled": bool(cfg.get("enabled", True)),
           "daily_calls": cfg.get("daily_calls") or None,
           "daily_tokens": cfg.get("daily_tokens") or None}
    tmp = path.with_name(f".budget-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- 记账

def _backup_corrupt(path: Path, error: str) -> None:
    """损坏文件改名备份 + 写事件，然后抛 OpsxError（fail-closed）。

    已知取舍：备份后文件不存在，下一次判定按空状态继续（fail-open 恢复）。
    这是有意的——熔断的目的是"别在看不见时烧钱"，损坏已经告警留痕，
    让服务自动恢复比永久卡死更符合个人运维场景。
    """
    try:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        os.replace(path, backup)
    except OSError:
        backup = None
    try:
        from agent import events
        events.append("usage-state-corrupt", error=error,
                      backup=str(backup) if backup else None)
    except Exception:  # noqa: BLE001 —— 事件旁路失败不影响熔断
        pass
    raise core.OpsxError(f"usage-state.json 损坏: {error}"
                         + (f"（已备份为 {backup.name}，计数清零）" if backup else ""))


def _load_state(strict: bool = False) -> dict:
    """strict=True（预算判定路径）时损坏即抛 OpsxError：熔断不能 fail-open。

    strict=False（record 旁路）时退回空状态：记账可以丢，不能反过来影响调用。
    损坏文件改名备份一次（.corrupt-<ts>），并写事件——否则"计数清零"无痕发生。
    """
    path = _state_file()
    if not path.exists():
        return {"version": 1, "days": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        if strict:
            _backup_corrupt(path, str(e)[:200])
        return {"version": 1, "days": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), dict):
        if strict:
            _backup_corrupt(path, "结构非法（days 字段缺失）")
        return {"version": 1, "days": {}}
    return raw


def _save_state(state: dict) -> None:
    path = _state_file()
    core._ensure_state_dir()
    tmp = path.with_name(f".usage-state-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)
    os.replace(tmp, path)


def record(model: str, prompt_tokens=None, completion_tokens=None,
           source: str = "agent", *, estimated: bool = False) -> None:
    """记一条调用。失败不外抛：记账是旁路，不能让"记账坏了"变成"模型坏了"。"""
    try:
        prompt = int(prompt_tokens or 0)
        completion = int(completion_tokens or 0)
        if prompt < 0 or completion < 0 or prompt > MAX_RECORDED_TOKENS \
                or completion > MAX_RECORDED_TOKENS:
            return  # provider 返回的垃圾：不记账，比记错强
        total = prompt + completion
        now = time.time()
        rec = {"ts": round(now, 3), "day": _day_key(now), "model": str(model or "?")[:80],
               "prompt_tokens": prompt, "completion_tokens": completion,
               "source": str(source)[:40]}
        if estimated:
            rec["estimated"] = True
        path = _usage_file()
        core._ensure_state_dir()
        if path.exists() and path.stat().st_size > USAGE_ROTATE_BYTES:
            try:
                path.replace(path.with_suffix(".jsonl.1"))
            except OSError:
                pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with _state_lock():
            state = _load_state()
            day = state["days"].setdefault(_day_key(now), {"calls": 0, "tokens": 0})
            day["calls"] += 1
            day["tokens"] += total
            # 只保留最近 31 天聚合
            if len(state["days"]) > 31:
                for old_day in sorted(state["days"])[:-31]:
                    state["days"].pop(old_day, None)
            _save_state(state)
    except Exception:  # noqa: BLE001 —— 记账旁路失败静默（审计无意义，重试浪费）
        pass


def mark_budget_alerted() -> bool:
    """预算耗尽告警去重标记：同一天只返回一次 True（调用方据此决定是否发事件）。

    读-改-写在 _state_lock 内：与 record 的计数写入互斥，去重标记不会被
    并发进程的整文件覆盖丢失（那会让同一天告警重复刷屏）。
    """
    with _state_lock():
        state = _load_state()
        today_key = _day_key()
        if state.get("budget_alerted_day") == today_key:
            return False
        state["budget_alerted_day"] = today_key
        _save_state(state)
        return True


def estimate_tokens(messages: list[dict] | None, output: str) -> tuple[int, int]:
    """流式响应没有 usage 字段时的粗估（4 字符 ≈ 1 token）。"""
    try:
        text = json.dumps(messages or [], ensure_ascii=False)
        return max(1, len(text) // 4), max(1, len(output or "") // 4)
    except Exception:  # noqa: BLE001
        return 1, 1


# ---------------------------------------------------------------- 预算检查

def today() -> dict:
    state = _load_state(strict=True)  # 预算判定路径：损坏即报错，不静默清零
    day = state["days"].get(_day_key(), {"calls": 0, "tokens": 0})
    return {"calls": day["calls"], "tokens": day["tokens"], "day": _day_key()}


def check_budget() -> str | None:
    """返回 None 表示可调用；否则返回给调用方展示的说明。"""
    cfg = load_budget()
    if not cfg["enabled"]:
        return None
    used = today()
    reasons = []
    calls_limit = cfg.get("daily_calls")
    tokens_limit = cfg.get("daily_tokens")
    if calls_limit is not None and used["calls"] >= calls_limit:
        reasons.append(f"调用次数 {used['calls']}/{calls_limit}")
    if tokens_limit is not None and used["tokens"] >= tokens_limit:
        reasons.append(f"token {used['tokens']}/{tokens_limit}")
    if not reasons:
        return None
    return ("LLM 调用预算已耗尽（" + "；".join(reasons)
            + "）。已停止新的 LLM 调用；确定性规则检查不受影响。"
              "按本机自然日跨天自动恢复；如需立即调整: opsagent budget set "
              "--calls/--tokens；查看: opsagent budget status")


def ensure_budget() -> None:
    """兼容占位：预算耗尽返回说明由 llm.chat 转成 LLMError 子类抛出。
    直接调用本函数的 CLI 场景用 check_budget 的返回值。"""
    reason = check_budget()
    if reason:
        raise core.OpsxError(reason)


# ---------------------------------------------------------------- 展示

def format_status() -> str:
    cfg = load_budget()
    used = today()
    lines = [f"日期: {used['day']}（按本机本地日界滚动）"]
    if not cfg["enabled"]:
        lines.append("预算: 已停用（不限制 LLM 调用）")
    else:
        calls_limit = cfg.get("daily_calls")
        tokens_limit = cfg.get("daily_tokens")
        calls_text = f"{used['calls']} / {calls_limit}" if calls_limit is not None \
            else f"{used['calls']} / 不限"
        tokens_text = f"{used['tokens']} / {tokens_limit}" if tokens_limit is not None \
            else f"{used['tokens']} / 不限"
        lines.append(f"今日调用: {calls_text} 次")
        lines.append(f"今日 token: {tokens_text}")
        reason = check_budget()
        if reason:
            lines.append(f"状态: **已耗尽**——{reason}")
        else:
            lines.append("状态: 未超限")
    state = _load_state()
    if state["days"]:
        lines.append("近几天: " + "；".join(
            f"{d} {state['days'][d]['calls']}次/{state['days'][d]['tokens']}tok"
            for d in sorted(state["days"], reverse=True)[:7]))
    return "\n".join(lines)


def recent(tail: int = 20) -> list[dict]:
    lines: list[str] = []
    for candidate in (_usage_file().with_suffix(".jsonl.1"), _usage_file()):
        # 轮转后的旧文件在前：recent 要能跨轮转往回看
        try:
            if candidate.exists():
                lines += candidate.read_text(encoding="utf-8",
                                             errors="replace").splitlines()
        except OSError:
            continue
    out = []
    for line in lines[-tail:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
