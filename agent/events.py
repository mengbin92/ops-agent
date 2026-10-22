"""轻量事件时间线：~/.ops-agent/events.jsonl（append-only，0600，按大小轮转一次）。

时间线只记**状态变化**，不记每次巡检的正常结果：
异常出现 / 升级 / 提醒 / 恢复、通知发送 / 失败 / 静默、运行失败 / 超时 / 中断 / 错过。
否则每 5 分钟一条"正常"，时间线会变成第二个日志洪水，人还是不看。

JSONL 的好处是不需要数据库、崩溃只会丢最后半行；查询 `tail` 即可还原最近发生了什么。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

try:  # Windows 无 fcntl：退化为无锁（本项目运行在 macOS/Linux）
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

MAX_BYTES = 5 * 1024 * 1024   # 5MB 约 4 万条事件，对个人运维足够长
ROTATE_KEEP = 1               # 只留一代备份，需要更长历史时改按日期分目录
TAIL_DEFAULT = 30
FIELD_LIMIT = 400             # 单个字段保留字符数：时间线是索引，不是证据仓库
MAX_LIST = 30
MAX_DICT = 20

# 已使用的事件类型（写新类型不强制登记，这里只作为阅读代码时的清单）
KINDS = (
    "anomaly-new", "anomaly-escalated", "anomaly-changed", "anomaly-reminder",
    "anomaly-recovered",
    "notify-sent", "notify-failed", "notify-muted", "notify-retry-sent",
    "notify-retry-failed", "notify-retry-skipped", "notify-config",
    "notify-config-error",
    "run-failed", "run-timeout", "run-interrupted", "run-missed",
    "schedule-checks-error",
)

_KIND_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_SUMMARY_KEYS = ("title", "detail", "reason", "error", "status", "verdict", "count")


def _path() -> Path:
    return core.STATE_DIR / "events.jsonl"


def _now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def _clean(value, depth: int = 0):
    """字段归一化：脱敏、截断、拒绝不可序列化对象。

    事件里的内容来自报告与异常信息，可能带凭据；写事件前必须先脱敏，
    否则"轻量时间线"会变成明文密钥的长期副本。
    """
    if depth > 3:
        return "…"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int,)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        return core.redact(value)[:FIELD_LIMIT]
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(value)[:MAX_LIST]]
    if isinstance(value, dict):
        return {str(k)[:40]: _clean(v, depth + 1) for k, v in list(value.items())[:MAX_DICT]}
    return core.redact(str(value))[:FIELD_LIMIT]


def _rotate_if_needed() -> None:
    path = _path()
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass  # 轮转失败也要继续写：宁可文件长大，不能丢事件


def append(kind: str, **fields) -> bool:
    """追加一条事件；写失败返回 False（时间线不能拖垮巡检）。"""
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise ValueError(f"非法事件类型: {kind!r}")
    rec = {"ts": round(time.time(), 3), "at": _now_iso(), "kind": kind}
    for key, value in fields.items():
        if isinstance(key, str) and key and key not in rec:
            rec[key] = _clean(value)
    try:
        core._ensure_state_dir()
        path = _path()
        with open(core.STATE_DIR / "events.lock", "a") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            _rotate_if_needed()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def tail(n: int = TAIL_DEFAULT, kind: str | None = None) -> list[dict]:
    """最近 n 条事件（按写入顺序倒序）。损坏行跳过，不让半行拖垮查询。"""
    if type(n) is not int or n < 0:
        raise ValueError("n 须为非负整数")
    path = _path()
    if n == 0:
        return []
    lines = []
    for candidate in (path.with_name(path.name + ".1"), path):
        # 轮转后的旧文件在前：tail 要能跨轮转继续往回看，否则历史会突然断档
        try:
            if candidate.is_file():
                lines += candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
    out = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("kind"), str):
            continue
        if kind and rec["kind"] != kind:
            continue
        out.append(rec)
        if len(out) >= n:
            break
    return out


def _summary(rec: dict) -> str:
    parts = [str(rec[key]) for key in _SUMMARY_KEYS
             if rec.get(key) not in (None, "", 0, False)]
    if not parts:  # 例如 notify-config：字段是 endpoint/format，用 k=v 兜底显示
        parts = [f"{key}={value}" for key, value in rec.items()
                 if key not in ("ts", "at", "kind", "task", "title", "detail")
                 and isinstance(value, (str, int, float)) and value not in (None, "")]
        parts = parts[:3]
    text = re.sub(r"\s+", " ", "；".join(parts))[:200]
    # 没有 title 的行（例如 run-failed）用任务 id 打头，否则看不出是哪个任务出的问题
    task = rec.get("task")
    if task and not rec.get("title"):
        text = f"[{task}] {text}"
    return text


def format_tail(n: int = TAIL_DEFAULT, kind: str | None = None) -> str:
    records = tail(n=n, kind=kind)
    if not records:
        return "(无事件)"
    width = max(len(r["kind"]) for r in records)
    lines = []
    for rec in reversed(records):  # 输出按时间正序，读起来像一条时间线
        lines.append(f"{rec.get('at', '?')}  {rec['kind'].ljust(width)}  {_summary(rec)}")
    return "\n".join(lines)
