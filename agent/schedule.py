"""定时巡检：schedules.json 注册表 + 到期执行 + 守护进程 / --once（cron）。

无人值守运行：ask_user 不可用，只读排查；写操作会被守门拒绝（安全方向）。
同一时刻只允许一个调度进程（flock）；注册表的读改写另有短锁，daemon 与 CLI
同时增删任务不会丢更新。

W3 起每次执行都有运行记录（~/.ops-agent/runs/<run_id>.json）：
run_id、task_id、session_id、pid、started/finished、status、verdict、error、
attempts、attempt_errors、report_path。status 取值：running / succeeded /
failed / timeout / interrupted / missed。

- interrupted：进程退出时状态还是 running，下次启动/查询时对账标出。
- missed：调度进程停机期间错过的执行时点。判定依据是心跳（schedules.json 的
  meta.last_scan，每次扫描与每次任务结束时刷新），而不是"到期时刻距今多久"——
  否则任务自己跑得久会被误记成停机。

退出码（`run --once`，与 opsagent inspect 同一约定）：
0 全部正常；2 检查发现警告/异常；3 不完整/未判定/有错过，或本次因已有调度进程而未执行；
1 有任务运行失败/超时，或启动时对账出中断。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from . import session as sess_mod  # noqa: E402

try:  # Windows 无 fcntl：退化为无锁（本项目运行在 macOS/Linux）
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

MIN_EVERY = 60
SCHEDULE_MAX_TURNS = 12
DAILY_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# 整任务时间预算（在轮次边界停止：单轮含一次 LLM 调用，自有超时）
DEFAULT_TASK_TIMEOUT = 900
MIN_TASK_TIMEOUT, MAX_TASK_TIMEOUT = 60, 86400
# 只读巡检的瞬时失败（SSH/网络）有限重试：不会放大成风暴，也不会重放写操作
RUN_RETRIES = 1
RUN_RETRY_DELAY = 3
RUNS_KEEP = 200           # 运行记录保留份数，超出按时间删最旧
RESULT_PREVIEW = 400      # 运行记录里保留的结论摘要字符数
MISSED_CAP = 10000        # 停机很久时错过次数的统计上限

RUN_STATUSES = ("running", "succeeded", "failed", "timeout", "interrupted", "missed")

# 结论解析只认两种形态（避免被日志/引用里的"结论：正常"翻转）：
#   1) 单独一行的 `结论：正常`（调度提示词要求模型写在最后一行）
#   2) workflow/inspect 报告头 `结论: **异常**｜完整性: ...`
_VERDICT_LINE = re.compile(
    r"^[ \t]*\*{0,2}结论[:：][ \t]*\*{0,2}(正常|警告|异常|不完整)\*{0,2}"
    r"(?![\u4e00-\u9fff])", re.M)
_VERDICT_REPORT = re.compile(
    r"^[ \t]*-?[ \t]*结论[:：][ \t]*\*\*(正常|警告|异常|不完整)\*\*｜完整性", re.M)
_TRUNCATED_HINTS = ("解读不完整", "[失败]")

SCHEDULE_PROMPT = """你是 ops-agent 的定时巡检任务，在无人值守环境运行（没有用户可回答问题）。

## 规则
- 只读：bash 只执行 R0 命令；可用 read / skill / workflow / inspect / ops_list。
- 不要调用 ask_user（不可用，会被视为拒绝）；不要尝试写操作。
- 需要变更时：把建议写进报告（命令、风险、回滚方式），由人后续走变更单流程。
- 判断主机/服务健康时先用 inspect：规则结论由代码判定，不依赖你；unknown/error 不等于正常。
- 报告格式：先给结论（正常/异常/不完整），再列异常项与证据（命令 + 关键输出摘要），最后给建议。
- 最后单独一行必须是 `结论：<正常|警告|异常|不完整>`——程序只认这一行（退出码依赖它）。
- 没有异常就明确写"未发现异常"，不要编造；不确定就说不确定。默认中文。"""


class ScheduleError(Exception):
    pass


def _file() -> Path:
    return core.STATE_DIR / "schedules.json"


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def _safe_audit(event: str, **fields) -> None:
    """审计写失败（ENOSPC/只读 home）不能打断巡检：记录尽力而为。"""
    try:
        core.audit(event, **fields)
    except OSError:
        pass


def _safe_save(items: list[dict], meta: dict | None = None) -> bool:
    """注册表写盘：失败返回 False 让调用方降级（审计备份 + 退避），不抛异常。"""
    try:
        _save(items, meta)
        return True
    except OSError as e:
        _safe_audit("schedule-save-error", error=str(e))
        return False


def _meta_ts(meta: dict, key: str) -> float:
    """meta 里的时间戳：非数值一律按"没有心跳"处理，不让手工改坏的字段停摆巡检。"""
    value = meta.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(value)


def _load_state() -> tuple[list[dict], dict]:
    """读注册表与元信息（meta.last_scan 是心跳，用于区分停机错过与自身执行慢）。"""
    p = _file()
    if not p.exists():
        return [], {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ScheduleError(f"调度文件损坏: {p}（{e}）") from e
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ScheduleError(f"调度文件格式异常: {p}")
    if any(not isinstance(it, dict)
           or not isinstance(it.get("id"), str) or not it["id"]
           or not re.fullmatch(r"[A-Za-z0-9._-]+", it["id"])
           or not isinstance(it.get("prompt"), str) or not it["prompt"].strip()
           or not isinstance(it.get("enabled", True), bool) for it in items):
        raise ScheduleError(f"调度文件格式异常: {p}")
    meta = data.get("meta") if isinstance(data, dict) else None
    return items, meta if isinstance(meta, dict) else {}


def _load() -> list[dict]:
    return _load_state()[0]


def _save(items: list[dict], meta: dict | None = None) -> None:
    if meta is None:  # 保留已有 meta（心跳/未来字段），别因为写条目把它冲掉
        try:
            _, meta = _load_state()
        except ScheduleError:
            meta = {}
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_name(f".schedules-{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"items": items, "meta": meta or {}}, ensure_ascii=False,
                              indent=2), encoding="utf-8")
    os.replace(tmp, p)


@contextmanager
def _locked(name: str, block: bool = True):
    """flock 文件锁；yield 是否拿到锁。无 fcntl 时视为拿到。"""
    if fcntl is None:
        yield True
        return
    path = core.STATE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = open(path, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            if block:
                raise
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _next_daily(hhmm: str, now: float) -> float:
    if not isinstance(hhmm, str) or not DAILY_RE.fullmatch(hhmm):
        raise ScheduleError(f"daily 时间格式错误: {hhmm!r}（应为 HH:MM）")
    hour, minute = (int(part) for part in hhmm.split(":"))
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    if target <= now:
        # 按本地日历推进；夏令时切换的两天不一定相隔 86400 秒。
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                              hour, minute, 0, 0, 0, -1))
    return target


def _interval(value) -> int:
    """间隔归一化：接受 60、"60"、60.0（值等于整数），拒绝 bool/非整数/过小值。"""
    try:
        if isinstance(value, bool):
            raise ValueError
        every = int(value)
        if isinstance(value, float) and value != every:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as e:
        raise ScheduleError("every_seconds 必须是整数秒") from e
    if every < MIN_EVERY:
        raise ScheduleError(f"间隔不能小于 {MIN_EVERY} 秒（避免打爆端点与主机）")
    return every


def _next_run(entry: dict, now: float) -> float:
    every, daily = entry.get("every_seconds"), entry.get("daily")
    if (every is not None) == (daily is not None):
        raise ScheduleError("every_seconds 与 daily 必须二选一")
    if every is not None:
        try:
            next_run = now + _interval(every)
        except OverflowError as e:
            raise ScheduleError("every_seconds 过大") from e
        _run_time({"next_run": next_run})
        return next_run
    return _next_daily(daily, now)


def _run_time(entry: dict) -> float:
    value = entry.get("next_run", 0)
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError
        time.localtime(value)
    except (TypeError, ValueError, OverflowError, OSError) as e:
        raise ScheduleError(f"任务 {entry.get('id')} 的 next_run 损坏") from e
    return value


def _validate_schedule(entry: dict) -> None:
    """时间配置可解析性检查（不改状态）。损坏条目由 run_due 停用。"""
    every, daily = entry.get("every_seconds"), entry.get("daily")
    if (every is not None) == (daily is not None):
        raise ScheduleError("every_seconds 与 daily 必须二选一")
    if every is not None:
        _interval(every)
    elif not isinstance(daily, str) or not DAILY_RE.fullmatch(daily):
        raise ScheduleError(f"daily 时间格式错误: {daily!r}")


def _task_timeout(entry: dict) -> int:
    """整任务时间预算。

    配置坏了退回默认值而不是停用任务：巡检是安全网，宁可少一个自定义预算，
    也不能因为一个字段写错就永久不再巡检（`schedule list` 会显示生效值）。
    """
    raw = entry.get("timeout_seconds", DEFAULT_TASK_TIMEOUT)
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)
        if isinstance(raw, float) and raw != value:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_TASK_TIMEOUT
    return value if MIN_TASK_TIMEOUT <= value <= MAX_TASK_TIMEOUT else DEFAULT_TASK_TIMEOUT


def describe(entry: dict) -> str:
    if entry.get("every_seconds"):
        return f"每 {entry['every_seconds']} 秒"
    return f"每天 {entry.get('daily') or '（时间配置损坏）'}"


def fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def add(prompt: str, every_seconds=None, daily=None, timeout_seconds=None) -> dict:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ScheduleError("prompt 不能为空")
    if (every_seconds is not None) == (daily is not None):
        raise ScheduleError("every_seconds 与 daily 必须二选一")
    if timeout_seconds is not None and (type(timeout_seconds) is not int
                                        or not MIN_TASK_TIMEOUT <= timeout_seconds <= MAX_TASK_TIMEOUT):
        raise ScheduleError(
            f"timeout_seconds 须为 {MIN_TASK_TIMEOUT}-{MAX_TASK_TIMEOUT} 的整数秒")
    now = time.time()
    entry = {"id": "sch-" + uuid.uuid4().hex[:6], "prompt": prompt, "enabled": True,
             "every_seconds": None, "daily": None, "created": now, "last_run": None}
    if timeout_seconds is not None:
        entry["timeout_seconds"] = timeout_seconds
    if every_seconds is not None:
        entry["every_seconds"] = _interval(every_seconds)
        entry["next_run"] = _next_run(entry, now)
    else:
        if not isinstance(daily, str) or not DAILY_RE.fullmatch(daily):
            raise ScheduleError("daily 格式应为 HH:MM（24 小时制）")
        entry["daily"] = daily
        entry["next_run"] = _next_daily(daily, now)
    with _locked("schedules.mutex"):
        items, meta = _load_state()
        items.append(entry)
        _save(items, meta)
    _safe_audit("schedule-add", id=entry["id"], prompt=prompt[:200],
               every_seconds=entry["every_seconds"], daily=entry["daily"],
               timeout_seconds=entry.get("timeout_seconds"))
    return entry


def _find(key: str, items: list[dict]) -> dict:
    matches = [it for it in items if it["id"] == key] or \
              [it for it in items if it["id"].startswith(key)]
    if not matches:
        raise ScheduleError(f"没有匹配的任务: {key}")
    if len(matches) > 1:
        raise ScheduleError("id 前缀不唯一: " + ", ".join(it["id"] for it in matches))
    return matches[0]


def resolve_id(key: str) -> str:
    """把 id 或唯一前缀解析成完整 id（CLI / 工具 / 历史查询共用）。"""
    key = (key or "").strip()
    if not key:
        raise ScheduleError("缺少任务 id")
    with _locked("schedules.mutex"):
        return _find(key, _load())["id"]


def remove(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise ScheduleError("缺少任务 id")
    with _locked("schedules.mutex"):
        items, meta = _load_state()
        target = _find(key, items)
        _save([it for it in items if it["id"] != target["id"]], meta)
    _safe_audit("schedule-remove", id=target["id"])
    return target


def _set_enabled(key: str, enabled: bool) -> dict:
    key = (key or "").strip()
    if not key:
        raise ScheduleError("缺少任务 id")
    with _locked("schedules.mutex"):
        items, meta = _load_state()
        target = _find(key, items)
        was_enabled = target.get("enabled", True)
        target["enabled"] = enabled
        if enabled and not was_enabled:
            # 只对"原本暂停"的任务重算：恢复从现在起算，暂停期不补跑、不记 missed。
            # 对已启用的任务重复 resume 不动 next_run（否则会把刚排好的执行推后一个周期）。
            try:
                target["next_run"] = _next_run(target, time.time())
            except ScheduleError:
                pass  # 时间配置损坏留给 run_due 停用
        _save(items, meta)
    _safe_audit("schedule-pause" if not enabled else "schedule-resume", id=target["id"])
    return target


def pause(key: str) -> dict:
    """暂停任务：保留配置；恢复时从当前时间重新起算。"""
    return _set_enabled(key, False)


def resume(key: str) -> dict:
    return _set_enabled(key, True)


def list_entries() -> list[dict]:
    return _load()


def format_list() -> str:
    items = list_entries()
    if not items:
        return '(无定时任务；添加：opsagent schedule add --daily 09:00 "检查磁盘占用")'
    lines = []

    def sort_key(entry):
        try:
            return _run_time(entry)
        except ScheduleError:
            return 0

    for it in sorted(items, key=sort_key):
        state = "启用" if it.get("enabled", True) else "暂停"
        try:
            when = fmt_ts(_run_time(it))
        except ScheduleError:
            when = "时间配置损坏"
        line = (f"{it['id']}  {describe(it)}  预算 {_task_timeout(it)}s  下次 {when}  "
                f"{state}  {it['prompt'][:60]}")
        if it.get("last_run_id") and it.get("last_status"):
            verdict = f"/{it['last_verdict']}" if it.get("last_verdict") else ""
            line += f"  上次 {fmt_ts(it.get('last_run') or 0)} {it['last_status']}{verdict}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------- 运行记录


def _runs_dir() -> Path:
    return core.STATE_DIR / "runs"


def _new_run_id(task_id: str) -> str:
    return (time.strftime("%Y%m%d-%H%M%S") + "-" + task_id
            + "-" + uuid.uuid4().hex[:6])


def _write_run(rec: dict, locked: bool = False) -> None:
    """单个 run 一个文件，原子重写。

    全局 runs.mutex 让"对账读-判-写"与"执行结束写终态"不会互相覆盖。
    flock 按 fd 计数，同一进程内重复加锁会死锁——内部路径用 locked=True。
    """
    if not locked:
        with _locked("runs.mutex"):
            _write_run(rec, locked=True)
        return
    d = _runs_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    tmp = d / f".{rec['run_id']}-{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(core.redact_obj(rec), ensure_ascii=False) + "\n")
    os.replace(tmp, d / f"{rec['run_id']}.json")


def _prune_runs(keep: int = RUNS_KEEP) -> None:
    try:
        files = sorted(_runs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:  # 与 prune_outputs 同源：并发删除时 stat 会失败，跳过本次
        return
    for p in files[:-keep]:
        try:
            p.unlink()
        except OSError:
            pass


def _read_run(path: Path) -> dict | None:
    """读一条运行记录；字段不全/被改坏的记录直接跳过，不让它拖垮查询与对账。"""
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("run_id") != path.stem:
        return None
    if rec.get("status") not in RUN_STATUSES or not isinstance(rec.get("started"), str):
        return None
    pid = rec.get("pid")
    if pid is not None and (type(pid) is not int or not 0 < pid < 2 ** 31):
        return None
    for key in ("started_ts", "finished_ts"):
        value = rec.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value)):
            return None
    return rec


def list_runs(task_id: str | None = None, tail: int = 20,
              prefix: bool = False) -> list[dict]:
    """按开始时间倒序的运行历史；损坏记录跳过。

    prefix=True 用于任务已被删除、只剩 id 前缀可查的场景。
    """
    d = _runs_dir()
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.json"):
        rec = _read_run(p)
        if rec is None:
            continue
        name = str(rec.get("task_id") or "")
        if task_id:
            if not (name.startswith(task_id) if prefix else name == task_id):
                continue
        out.append(rec)
    out.sort(key=lambda r: float(r.get("started_ts") or 0), reverse=True)
    return out[:tail]


def _pid_alive(pid) -> bool:
    if type(pid) is not int or not 0 < pid < 2 ** 31:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True     # 进程在，只是不归我们
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _update_entry_state(task_id: str, run_id: str, status: str,
                        verdict=None, reanchor: bool = False) -> None:
    """把最后状态写回任务条目；顺带刷新心跳（长任务执行期间也算存活）。"""
    try:
        with _locked("schedules.mutex"):
            items, meta = _load_state()
            now = time.time()
            for it in items:
                if it["id"] == task_id:
                    it["last_run_id"] = run_id
                    it["last_status"] = status
                    it["last_verdict"] = verdict
                    if reanchor and it.get("enabled", True):
                        try:
                            it["next_run"] = _next_run(it, now)
                        except ScheduleError:
                            pass  # 损坏的时间留给 run_due 停用
            meta["last_scan"] = now
            _safe_save(items, meta)
    except (ScheduleError, OSError):
        pass  # 注册表坏了/写不进去：运行记录仍在 runs/ 里，不影响本次结论


def reconcile_runs() -> int:
    """把状态仍是 running 但进程已死的记录标为 interrupted。

    已知边界：pid 可能被复用（重启后同号进程是别的程序），此时会误判为仍在运行；
    个人单机场景概率低，真需要时再以 boot_id/启动时间校验。
    """
    d = _runs_dir()
    if not d.is_dir():
        return 0
    updated = []
    with _locked("runs.mutex"):
        for p in sorted(d.glob("*.json")):
            rec = _read_run(p)
            if rec is None or rec.get("status") != "running":
                continue
            if _pid_alive(rec.get("pid")):
                continue
            fresh = _read_run(p)  # 锁内重读：确认还是同一条 running，避免覆盖刚写好的终态
            if (fresh is None or fresh.get("status") != "running"
                    or fresh.get("started_ts") != rec.get("started_ts")):
                continue
            fresh["status"] = "interrupted"
            fresh["finished"] = _iso()
            fresh["finished_ts"] = time.time()
            fresh["error"] = "进程退出，本次执行中断（未完成）"
            _write_run(fresh, locked=True)
            updated.append(fresh)
    for rec in updated:
        _safe_audit("schedule-interrupted", run_id=rec["run_id"], task_id=rec.get("task_id"))
        _update_entry_state(rec.get("task_id") or "", rec["run_id"], "interrupted", None)
    return len(updated)


def format_runs(task_id: str | None = None, tail: int = 20,
                prefix: bool = False) -> str:
    records = list_runs(task_id=task_id, tail=tail, prefix=prefix)
    if not records:
        return "(无运行记录)"
    lines = []
    for rec in records:
        verdict = f" {rec['verdict']}" if rec.get("verdict") else ""
        duration = (f"{rec['duration_s']}s" if isinstance(rec.get("duration_s"), (int, float))
                    else "-")
        line = (f"{rec.get('started') or '-'}  {rec['run_id']}  {rec.get('task_id')}  "
                f"{rec['status']}{verdict}  {duration}")
        if rec.get("error"):
            line += f"\n    {str(rec['error'])[:160]}"
        if rec.get("session_id"):
            line += f"\n    会话: {rec['session_id']}"
        if rec.get("report_path"):
            line += f"\n    报告: {rec['report_path']}"
        lines.append(line)
    return "\n".join(lines)


_REPORT_PATH_RE = re.compile(r"^\[全量报告\][ \t]*(.+?)[ \t]*$", re.M)


def _detect_verdict(text: str) -> str:
    """从结果文本提取结论；没有可解析结论就是"未判定"，不许默认成正常。

    只认"单独一行的结论"与"报告头的加粗结论"，取文本中位置最后的那一个。
    这样日志/引用里的 `结论：正常` 不能翻转最终判定。
    """
    text = _strip_quoted(text or "")
    # 单独成行的结论优先（模型最后写的）；否则退回本轮报告头里的加粗结论。
    for pattern in (_VERDICT_LINE, _VERDICT_REPORT):
        hits = [(m.start(), m.group(1)) for m in pattern.finditer(text)]
        if hits:
            return max(hits)[1]
    return "未判定"


def _strip_quoted(text: str) -> str:
    """去掉代码围栏与引用块：里面粘贴的旧报告/日志不是本次结论。"""
    kept, in_fence = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _run_entry(entry: dict, model: str | None, session=None,
               deadline: float | None = None) -> str:
    """执行一条任务的 prompt 并返回结果文本；记录由 _execute_run 负责。"""
    from . import loop, tools  # 延迟导入：tools 依赖本模块
    session = session or sess_mod.Session()
    ctx = tools.ToolContext(ask_user=None, model=model, allowed=tools.SCHEDULED_TOOLS,
                            session_id=session.id, deadline=deadline)
    return loop.run_turn(entry["prompt"], session, ctx,
                         system=loop._default_system(SCHEDULE_PROMPT),
                         max_turns=SCHEDULE_MAX_TURNS,
                         model=model)


def _begin_run(entry: dict, session_id: str | None) -> dict:
    rec = {
        "run_id": _new_run_id(entry["id"]),
        "task_id": entry["id"],
        "prompt": entry["prompt"][:200],
        "session_id": session_id,
        "pid": os.getpid(),
        "status": "running",
        "verdict": None,
        "started": _iso(),
        "started_ts": time.time(),
        "finished": None,
        "finished_ts": None,
        "duration_s": None,
        "attempts": 0,
        "attempt_errors": [],
        "error": None,
        "result_preview": "",
        "report_path": None,
    }
    try:
        _write_run(rec)
        _prune_runs()
    except OSError as e:
        # 记录写不进去不能拦住执行：巡检结果比"能不能记账"重要
        rec["unrecorded"] = True
        _safe_audit("schedule-record-error", id=entry["id"], error=str(e))
    return rec


def _finish_run(rec: dict, entry: dict, status: str, verdict=None, error=None,
                attempts: int = 0, attempt_errors=None, result_text: str = "") -> dict:
    rec["status"] = status
    rec["verdict"] = verdict
    # 先脱敏再截断：异常信息与模型复述都可能带凭据，落盘与内存里的记录都不留明文
    rec["error"] = core.redact(error) if error else None
    rec["attempts"] = attempts
    rec["attempt_errors"] = [core.redact(e)[:200] for e in (attempt_errors or [])][-3:]
    rec["finished"] = _iso()
    rec["finished_ts"] = time.time()
    rec["duration_s"] = round(rec["finished_ts"] - rec["started_ts"], 1)
    rec["result_preview"] = core.redact(result_text or "")[:RESULT_PREVIEW]
    m = _REPORT_PATH_RE.search(result_text or "")
    if m:
        # 路径来自模型文本，可能是复述/伪造：只接受状态目录 reports/ 下的真实路径
        try:
            base = (core.STATE_DIR / "reports").resolve()
            candidate = Path(m.group(1)).expanduser().resolve()
            if (candidate != base and base in candidate.parents
                    and candidate.is_file()):
                rec["report_path"] = str(candidate)
        except (OSError, RuntimeError):
            pass
    try:
        _write_run(rec)
    except OSError:
        rec["unrecorded"] = True
        _safe_audit("schedule-record-error", id=entry["id"], error="write run record failed",
                   status=status)
    if status == "missed":
        return rec  # 错过的执行不是任务的"上次运行"，不动 last_* / 不重排
    _update_entry_state(entry["id"], rec["run_id"], status, verdict, reanchor=True)
    return rec


def _record_missed(entry: dict, missed: int) -> dict:
    """停机错过的执行时点：与 running/interrupted 明确区分。"""
    rec = _begin_run(entry, None)
    rec["missed_count"] = missed
    return _finish_run(rec, entry, "missed",
                       error=f"调度进程停机或未运行：错过 {missed} 个应执行时点"
                             "（本次运行不补跑历史）")


def _occurrences(entry: dict, anchor: float, since: float, now: float) -> int:
    """统计 [max(anchor, since), now] 内的应执行时点数（anchor 是排期相位起点）。"""
    start = max(anchor, since)
    if now < start:
        return 0
    every = entry.get("every_seconds")
    if every is not None:
        every = _interval(every)
        k_first = max(0, math.ceil((start - anchor) / every))
        k_last = math.floor((now - anchor) / every)
        return max(0, k_last - k_first + 1)
    count, t = 0, _next_daily(entry["daily"], start - 1)
    for _ in range(MISSED_CAP + 1):
        if t > now:
            break
        count += 1
        t = _next_daily(entry["daily"], t)
    return count


def _missed_count(entry: dict, anchor: float, since: float, now: float) -> int:
    """since（上次心跳）之后到 now 之间被跳过的执行时点数。

    只统计"能证明 daemon 活着"的窗口之后的部分：任务自己跑得久、
    或前面任务排队，不会因为在执行期间没扫描而被记成停机错过。
    没有心跳（首次运行）或 next_run 本身损坏/缺失时不做推断——那会把
    "从未排期过"算成"从 epoch 起错过上万次"。
    """
    if since <= 0 or anchor <= 0:
        return 0
    count = _occurrences(entry, anchor, since, now) - 1  # 减去本次要执行的这一次
    return max(0, min(MISSED_CAP, count))


def _execute_run(entry: dict, model: str | None) -> dict:
    """执行一次任务并写运行记录：开始记 running，结束落终态。"""
    from . import loop  # 延迟导入
    deadline = time.monotonic() + _task_timeout(entry)
    rec = _begin_run(entry, None)
    text, attempt_errors, attempts = None, [], 0
    for attempt in range(1, RUN_RETRIES + 2):
        attempts = attempt
        # 每次尝试独立会话：重试不重复追加同一条 prompt，也不复用上次的半截历史
        session = sess_mod.Session()
        try:
            text = _run_entry(entry, model, session=session, deadline=deadline)
            rec["session_id"] = session.id
            break
        except Exception as e:  # noqa: BLE001 —— 瞬时失败有限重试；仍是只读操作
            attempt_errors.append(f"{type(e).__name__}: {e}")
            if attempt <= RUN_RETRIES:
                time.sleep(RUN_RETRY_DELAY)
    if text is None:
        return _finish_run(rec, entry, "failed",
                           error=attempt_errors[-1] if attempt_errors else "未知错误",
                           attempts=attempts, attempt_errors=attempt_errors)
    if loop.DEADLINE_MARK in text:
        return _finish_run(rec, entry, "timeout", error="整任务时间预算耗尽（轮次边界停止）",
                           attempts=attempts, attempt_errors=attempt_errors,
                           result_text=text)
    verdict = _detect_verdict(text)
    # 被轮次保护截断 / 内部步骤失败 / 解读不完整：结论不能停在"正常"
    if loop.MAX_TURNS_MARK in text or any(h in text for h in _TRUNCATED_HINTS):
        if verdict == "正常" or verdict == "未判定":
            verdict = "不完整"
    return _finish_run(rec, entry, "succeeded", verdict=verdict, attempts=attempts,
                       attempt_errors=attempt_errors, result_text=text)


def _record_synthetic_failed(entry: dict, exc: BaseException) -> dict:
    """记录/执行路径的意外异常：落一条可查询的失败记录，不影响其他任务。"""
    now = time.time()
    rec = {
        "run_id": _new_run_id(entry["id"]), "task_id": entry["id"], "prompt": "",
        "session_id": None, "pid": os.getpid(), "status": "failed", "verdict": None,
        "started": _iso(now), "started_ts": now, "finished": _iso(now),
        "finished_ts": now, "duration_s": 0, "attempts": 0,
        "attempt_errors": [], "error": core.redact(f"{type(exc).__name__}: {exc}"),
        "result_preview": "", "report_path": None,
    }
    try:
        _write_run(rec)
    except OSError as e:
        rec["unrecorded"] = True
        _safe_audit("schedule-record-error", id=entry["id"], error=str(e))
    _update_entry_state(entry["id"], rec["run_id"], "failed", None, reanchor=True)
    return rec


def _claim_due(task_id: str, now: float) -> dict | None:
    """执行前取任务：短锁内复核"仍在且启用、仍到期"，然后才推进 next_run。

    推进放在每个任务真正开始前，而不是扫描时一次性推进——否则中途被 kill 时，
    后面排队的任务 next_run 已被推到未来，既不会补跑也没有任何记录（静默漏检）。
    """
    try:
        with _locked("schedules.mutex"):
            items, meta = _load_state()
            target = next((it for it in items if it["id"] == task_id), None)
            if target is None or not target.get("enabled", True):
                return None
            if now < _run_time(target):
                return None
            target["next_run"] = _next_run(target, now)
            target["last_run"] = now
            meta["last_scan"] = now
            _save(items, meta)
            return dict(target)
    except (ScheduleError, TypeError, ValueError, OSError):
        return None


def _touch_heartbeat() -> None:
    try:
        with _locked("schedules.mutex"):
            items, meta = _load_state()
            meta["last_scan"] = time.time()
            _save(items, meta)
    except (ScheduleError, OSError):
        pass


def run_due(model: str | None = None) -> list[dict]:
    """执行所有到期任务，返回本次运行的记录列表（含 missed 记录）。

    两阶段：扫描只"认领"到期任务（算好 missed，刷心跳），执行前再短锁复核并
    推进 next_run。这样中途被 kill 时，排队中的任务仍是到期状态，下一轮会补上，
    不会像"扫描时一次性推进"那样静默漏检。
    单条任务的记录/执行异常不会中断整轮——否则一个任务的环境问题会让其余巡检停摆。
    """
    now = time.time()
    planned, disabled = [], []
    with _locked("schedules.mutex"):
        items, meta = _load_state()
        since = _meta_ts(meta, "last_scan")
        for it in items:
            if not it.get("enabled", True):
                continue
            try:
                anchor = _run_time(it)
                _validate_schedule(it)  # 时间配置损坏的条目要停用，不能留着每轮跳过
                if now < anchor:
                    continue
                missed = _missed_count(it, anchor, since, now)
            except (ScheduleError, TypeError, ValueError):
                it["enabled"] = False
                disabled.append(it["id"])
                continue
            planned.append((it["id"], missed))
        meta["last_scan"] = now
        if not _safe_save(items, meta):
            _safe_audit("schedule-degraded", reason="排期/心跳写盘失败，本轮可能重复执行")
    for task_id in disabled:
        _safe_audit("schedule-disable", id=task_id, reason="时间/预算配置损坏")

    records = []
    for task_id, missed in planned:
        entry = _claim_due(task_id, time.time())
        if entry is None:  # 扫描后被暂停、删除或改期
            _safe_audit("schedule-skip", id=task_id, reason="扫描后不再到期或已暂停")
            continue
        if missed:
            try:
                records.append(_record_missed(entry, missed))
            except (OSError, ScheduleError) as e:
                _safe_audit("schedule-record-error", id=task_id, error=str(e))
        try:
            rec = _execute_run(entry, model)
        except (OSError, ScheduleError) as e:
            _safe_audit("schedule-run-error", id=task_id, error=str(e))
            rec = _record_synthetic_failed(entry, e)
        except Exception as e:  # noqa: BLE001 —— 单任务异常不拖垮整轮巡检
            _safe_audit("schedule-run-crash", id=task_id, error=str(e))
            rec = _record_synthetic_failed(entry, e)
        _safe_audit("schedule-run", id=task_id, run_id=rec.get("run_id"),
                    status=rec["status"], verdict=rec.get("verdict"),
                    attempts=rec.get("attempts"), error=rec.get("error"),
                    result=rec.get("result_preview", ""))
        records.append(rec)
        _touch_heartbeat()  # 长任务执行期间也算"进程活着"，避免下次被误判停机
    return records


def runs_exit_code(records: list[dict]) -> int:
    """--once 的退出码：运行失败与检查发现异常必须能区分。"""
    if any(r["status"] in ("failed", "timeout", "interrupted") for r in records):
        return 1
    if any(r.get("verdict") in ("异常", "警告") for r in records):
        return 2
    if any(r["status"] == "missed" or r.get("verdict") in ("不完整", "未判定")
           for r in records):
        return 3
    return 0


def _sleep_seconds() -> float:
    now = time.time()
    items = [it for it in _load() if it.get("enabled", True)]
    values = []
    for it in items:
        try:
            values.append(_run_time(it))
        except ScheduleError:
            return 1.0  # 让下一轮 run_due 停用损坏条目。
    if not values:
        return 60.0
    # 已经过期（例如写盘失败导致 next_run 没推进）：退避 30s，不要 1 秒一次反复打端点
    return max(30.0, min(60.0, min(values) - now))


def _print_record(rec: dict, prefix: str = "") -> None:
    if rec["status"] == "missed":
        print(f"{prefix}{rec.get('task_id')} missed：{rec.get('error')}")
        return
    verdict = f"，结论 {rec['verdict']}" if rec.get("verdict") else ""
    print(f"{prefix}{rec.get('task_id')} {rec['status']}{verdict}"
          f"（耗时 {rec.get('duration_s')}s，run {rec.get('run_id')}）")
    if rec.get("result_preview"):
        print(rec["result_preview"], "\n")
    elif rec.get("error"):
        print(f"  {rec['error']}\n")


def daemon(model: str | None = None, once: bool = False) -> int:
    with _locked("schedules.lock", block=False) as acquired:
        if not acquired:
            print("[schedule] 已有调度进程在运行，本次未执行（退出码 3）")
            return 3  # 明确区分"没跑"与"跑完且正常"
        interrupted = reconcile_runs()
        if interrupted:
            print(f"[schedule] 发现 {interrupted} 次中断的执行（见 schedule runs）")
        if once:
            try:
                records = run_due(model)
            except (ScheduleError, OSError) as e:
                print(f"[schedule] 调度失败: {core.redact(str(e))}", file=sys.stderr)
                return 1
            for rec in records:
                _print_record(rec, "[schedule] ")
            if not records:
                print("[schedule] (无到期任务)")
            code = runs_exit_code(records)
            if interrupted:
                code = 1  # 本轮对账出中断 = 上轮运行失败，必须让 cron 看到
            return code
        print(f"[schedule] 守护进程启动（Ctrl-C 退出）；注册表: {_file()}，"
              f"运行记录: {_runs_dir()}")
        try:
            while True:
                try:
                    records = run_due(model)
                except (ScheduleError, OSError) as e:
                    print(f"[schedule] {e}", file=sys.stderr)
                    time.sleep(60)
                    continue
                for rec in records:
                    _print_record(rec, f"[schedule {time.strftime('%H:%M:%S')}] ")
                try:
                    delay = _sleep_seconds()
                except ScheduleError:
                    delay = 60.0
                time.sleep(delay)
        except KeyboardInterrupt:
            print("\n[schedule] 已停止")
            return 0


def cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsagent schedule",
                                description="定时巡检（无人值守只读运行，每次执行有运行记录）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出定时任务（含预算与最近一次状态）")
    a = sub.add_parser("add", help="添加定时任务")
    a.add_argument("prompt", help="给 agent 的巡检指令（写清只读要求）")
    group = a.add_mutually_exclusive_group(required=True)
    group.add_argument("--every", type=int, metavar="SECONDS",
                       help=f"每 N 秒（>= {MIN_EVERY}）")
    group.add_argument("--daily", metavar="HH:MM", help="每天本地时间")
    a.add_argument("--timeout", type=int, metavar="SECONDS",
                   help=f"整任务时间预算（默认 {DEFAULT_TASK_TIMEOUT}s，"
                        f"{MIN_TASK_TIMEOUT}-{MAX_TASK_TIMEOUT}）")
    r = sub.add_parser("remove", help="删除任务（id 可用唯一前缀）")
    r.add_argument("id")
    for action, help_text in (("pause", "暂停任务"), ("resume", "恢复任务")):
        sp = sub.add_parser(action, help=f"{help_text}（id 可用唯一前缀）")
        sp.add_argument("id")
    hist = sub.add_parser("runs", help="运行历史（含中断/错过的执行）")
    hist.add_argument("--id", help="只看某个任务（id 或唯一前缀）")
    hist.add_argument("--tail", type=int, default=20, help="条数（默认 20）")
    run = sub.add_parser("run", help="运行调度（默认守护进程；--once 适合 cron）")
    run.add_argument("--once", action="store_true", help="只跑当前到期任务后退出")
    args = p.parse_args(argv)
    try:
        if args.cmd == "list":
            print(format_list())
            return 0
        if args.cmd == "add":
            entry = add(args.prompt, every_seconds=args.every, daily=args.daily,
                        timeout_seconds=args.timeout)
            print(f"已添加 {entry['id']}：{describe(entry)}，下次 {fmt_ts(entry['next_run'])}"
                  f"（时间预算 {_task_timeout(entry)}s）")
            return 0
        if args.cmd == "remove":
            entry = remove(args.id)
            print(f"已删除 {entry['id']}：{entry['prompt'][:60]}")
            return 0
        if args.cmd in ("pause", "resume"):
            entry = pause(args.id) if args.cmd == "pause" else resume(args.id)
            state = "已暂停" if args.cmd == "pause" else "已恢复"
            try:
                when = fmt_ts(_run_time(entry))
            except ScheduleError:
                when = "时间配置损坏"  # 改状态已生效，不能因为打印失败报错退出
            print(f"{state} {entry['id']}：{describe(entry)}，下次 {when}")
            return 0
        if args.cmd == "runs":
            if args.tail < 0:
                raise ScheduleError("tail 不能为负数")
            # 查询前先对账：daemon 被 kill 后，遗留的 running 记录应显示为中断
            reconcile_runs()
            prefix = False
            if args.id:
                try:
                    args.id = resolve_id(args.id)
                except ScheduleError:
                    prefix = True  # 任务已删除：run 记录还在，按 id 前缀过滤
            print(format_runs(task_id=args.id, tail=args.tail, prefix=prefix))
            return 0
        return daemon(once=args.once)
    except (ScheduleError, OSError) as e:
        print(f"[schedule] 错误: {core.redact(str(e))}", file=sys.stderr)
        return 1
