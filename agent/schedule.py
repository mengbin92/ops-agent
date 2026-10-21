"""定时巡检：schedules.json 注册表 + 到期执行 + 守护进程 / --once（cron）。

无人值守运行：ask_user 不可用，只读排查；写操作会被守门拒绝（安全方向）。
同一时刻只允许一个调度进程（flock）；注册表的读改写另有短锁，daemon 与 CLI
同时增删任务不会丢更新。
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

SCHEDULE_PROMPT = """你是 ops-agent 的定时巡检任务，在无人值守环境运行（没有用户可回答问题）。

## 规则
- 只读：bash 只执行 R0 命令；可用 read / skill / workflow / inspect / ops_list。
- 不要调用 ask_user（不可用，会被视为拒绝）；不要尝试写操作。
- 需要变更时：把建议写进报告（命令、风险、回滚方式），由人后续走变更单流程。
- 判断主机/服务健康时先用 inspect：规则结论由代码判定，不依赖你；unknown/error 不等于正常。
- 报告格式：先给结论（正常/异常/不完整），再列异常项与证据（命令 + 关键输出摘要），最后给建议。
- 没有异常就明确写"未发现异常"，不要编造；不确定就说不确定。默认中文。"""


class ScheduleError(Exception):
    pass


def _file() -> Path:
    return core.STATE_DIR / "schedules.json"


def _load() -> list[dict]:
    p = _file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ScheduleError(f"调度文件损坏: {p}（{e}）") from e
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ScheduleError(f"调度文件格式异常: {p}")
    if any(not isinstance(it, dict)
           or not isinstance(it.get("id"), str) or not it["id"]
           or not isinstance(it.get("prompt"), str) or not it["prompt"].strip()
           or not isinstance(it.get("enabled", True), bool) for it in items):
        raise ScheduleError(f"调度文件格式异常: {p}")
    return items


def _save(items: list[dict]) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_name(f".schedules-{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
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
    try:
        if isinstance(value, (bool, float)):
            raise ValueError
        every = int(value)
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


def describe(entry: dict) -> str:
    if entry.get("every_seconds"):
        return f"每 {entry['every_seconds']} 秒"
    return f"每天 {entry.get('daily') or '（时间配置损坏）'}"


def fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def add(prompt: str, every_seconds=None, daily=None) -> dict:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ScheduleError("prompt 不能为空")
    if (every_seconds is not None) == (daily is not None):
        raise ScheduleError("every_seconds 与 daily 必须二选一")
    now = time.time()
    entry = {"id": "sch-" + uuid.uuid4().hex[:6], "prompt": prompt, "enabled": True,
             "every_seconds": None, "daily": None, "created": now, "last_run": None}
    if every_seconds is not None:
        entry["every_seconds"] = _interval(every_seconds)
        entry["next_run"] = _next_run(entry, now)
    else:
        if not isinstance(daily, str) or not DAILY_RE.fullmatch(daily):
            raise ScheduleError("daily 格式应为 HH:MM（24 小时制）")
        entry["daily"] = daily
        entry["next_run"] = _next_daily(daily, now)
    with _locked("schedules.mutex"):
        items = _load()
        items.append(entry)
        _save(items)
    core.audit("schedule-add", id=entry["id"], prompt=prompt[:200],
               every_seconds=entry["every_seconds"], daily=entry["daily"])
    return entry


def remove(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise ScheduleError("缺少任务 id")
    with _locked("schedules.mutex"):
        items = _load()
        matches = [it for it in items if it["id"] == key] or \
                  [it for it in items if it["id"].startswith(key)]
        if not matches:
            raise ScheduleError(f"没有匹配的任务: {key}")
        if len(matches) > 1:
            raise ScheduleError("id 前缀不唯一: " + ", ".join(it["id"] for it in matches))
        target = matches[0]
        _save([it for it in items if it["id"] != target["id"]])
    core.audit("schedule-remove", id=target["id"])
    return target


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
        state = "启用" if it.get("enabled", True) else "停用"
        try:
            when = fmt_ts(_run_time(it))
        except ScheduleError:
            when = "时间配置损坏"
        lines.append(f"{it['id']}  {describe(it)}  下次 {when}  {state}  "
                     f"{it['prompt'][:60]}")
    return "\n".join(lines)


def _run_entry(entry: dict, model: str | None) -> str:
    from . import loop, tools  # 延迟导入：tools 依赖本模块
    ctx = tools.ToolContext(ask_user=None, model=model, allowed=tools.SCHEDULED_TOOLS)
    try:
        return loop.run_turn(entry["prompt"], sess_mod.Session(), ctx,
                             system=loop._default_system(SCHEDULE_PROMPT),
                             max_turns=SCHEDULE_MAX_TURNS,
                             model=model)
    except Exception as e:  # noqa: BLE001 —— 单次巡检失败不拖垮守护进程
        return f"[schedule 失败] {e}"


def run_due(model: str | None = None) -> list[tuple[dict, str]]:
    """执行所有到期任务（先推进 next_run 再跑，长任务不会在跑的过程中被重复触发）。"""
    now = time.time()
    with _locked("schedules.mutex"):
        items = _load()
        due, changed = [], False
        for it in items:
            if not it.get("enabled", True):
                continue
            try:
                if now < _run_time(it):
                    continue
                next_run = _next_run(it, now)
            except ScheduleError:  # 单条任务时间配置损坏：停用它，别卡住其他巡检
                it["enabled"] = False
                changed = True
                core.audit("schedule-disable", id=it["id"], reason="时间配置损坏")
                continue
            it["last_run"] = now
            it["next_run"] = next_run
            due.append(it)
            changed = True
        if changed:
            _save(items)
    results = []
    for entry in due:
        result = _run_entry(entry, model)
        core.audit("schedule-run", id=entry["id"], prompt=entry["prompt"][:200],
                   result=result[:2000])
        results.append((entry, result))
    return results


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
    return max(1.0, min(60.0, min(values) - now))


def daemon(model: str | None = None, once: bool = False) -> int:
    with _locked("schedules.lock", block=False) as acquired:
        if not acquired:
            print("[schedule] 已有调度进程在运行，退出")
            return 0
        if once:
            try:
                results = run_due(model)
            except ScheduleError as e:
                print(f"[schedule] {e}", file=sys.stderr)
                return 1
            for entry, result in results:
                print(f"[schedule] {entry['id']}（{describe(entry)}）已执行\n{result}\n")
            if not results:
                print("[schedule] (无到期任务)")
            return 0
        print(f"[schedule] 守护进程启动（Ctrl-C 退出）；注册表: {_file()}")
        try:
            while True:
                try:
                    results = run_due(model)
                except ScheduleError as e:
                    print(f"[schedule] {e}", file=sys.stderr)
                    time.sleep(60)
                    continue
                for entry, result in results:
                    print(f"[schedule {time.strftime('%H:%M:%S')}] "
                          f"{entry['id']}（{describe(entry)}）已执行\n{result}\n")
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
                                description="定时巡检（无人值守只读运行）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出定时任务")
    a = sub.add_parser("add", help="添加定时任务")
    a.add_argument("prompt", help="给 agent 的巡检指令（写清只读要求）")
    group = a.add_mutually_exclusive_group(required=True)
    group.add_argument("--every", type=int, metavar="SECONDS",
                       help=f"每 N 秒（>= {MIN_EVERY}）")
    group.add_argument("--daily", metavar="HH:MM", help="每天本地时间")
    r = sub.add_parser("remove", help="删除任务（id 可用唯一前缀）")
    r.add_argument("id")
    run = sub.add_parser("run", help="运行调度（默认守护进程；--once 适合 cron）")
    run.add_argument("--once", action="store_true", help="只跑当前到期任务后退出")
    args = p.parse_args(argv)
    try:
        if args.cmd == "list":
            print(format_list())
            return 0
        if args.cmd == "add":
            entry = add(args.prompt, every_seconds=args.every, daily=args.daily)
            print(f"已添加 {entry['id']}：{describe(entry)}，下次 {fmt_ts(entry['next_run'])}")
            return 0
        if args.cmd == "remove":
            entry = remove(args.id)
            print(f"已删除 {entry['id']}：{entry['prompt'][:60]}")
            return 0
        return daemon(once=args.once)
    except ScheduleError as e:
        print(f"[schedule] 错误: {e}", file=sys.stderr)
        return 1
