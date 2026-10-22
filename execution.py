"""变更执行记录：把"一次变更"变成可查询、可收尾的状态对象。

没有这层时，一次 exec 的全部痕迹是一条 audit 行加一个退出码；断线之后没人能回答
"命令到底执行完了没有"。这里给每次 exec/rollback 一个 execution_id 和明确的状态机：

    running ──exit=0──────────────────────▶ succeeded
       │
       ├──明确失败（非零退出 / 本机超时且已终止）──▶ failed
       ├──执行前中止（守门 / 快照 / 审批 / 锁冲突）──▶ aborted
       └──无法确认（SSH 断线 255 / 远程超时 / 进程被杀）──▶ unknown

unknown 是**唯一需要人类介入的终态**：命令可能执行了一半。未核实（resolved）之前，
同一目标同一命令的再次执行会被拒绝——"断线不自动重放"就落在这个门禁上，而不是
靠"我们记得不要重试"这种口头约定。

已知边界：进程存活判断用 pid（与 agent/schedule.py 的 reconcile 同源）。pid 被复用
时的方向是**少拦**（新进程占着旧 pid → 看起来还在跑 → 门禁放行），不是多拦。
个人单机场景概率低；要更严可拿 /proc/<pid> 启动时间与记录里的 started_ts 比对。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import core

try:  # POSIX 上一定有；缺失时降级为"无进程间互斥"，不阻断变更流程
    import fcntl
except ImportError:  # pragma: no cover - 仅非 POSIX 平台
    class _NoFcntl:
        LOCK_EX = LOCK_UN = 0

        @staticmethod
        def flock(fd, op):  # noqa: ARG004
            return None

    fcntl = _NoFcntl()  # type: ignore[assignment]

STATUSES = ("running", "succeeded", "failed", "unknown", "aborted")
KINDS = ("exec", "rollback")
# 需要人核实才能关闭的状态；只有 unknown 一个
OPEN_STATUSES = ("unknown",)
# 这些 phase 表示"命令一定还没下发"（拿锁 / 做快照 / 等授权 / 做回滚前副本）。
# 进程在这些阶段消失，结论是"没执行"，不该制造一条要人核实的 unknown。
BEFORE_EXEC_PHASES = ("lock", "snapshot", "authorize", "backup")
OUTCOMES = ("observed-ok", "observed-failed")
PREVIEW_LIMIT = 1500          # 记录里保留的输出字符数（完整输出在 audit / 报告文件）
KEEP = 500                    # 记录保留份数；未核实的 unknown 永不清理
_ID_RE = re.compile(r"^exe-[A-Za-z0-9-]{6,48}$")


def _dir() -> Path:
    return core.STATE_DIR / "executions"


def _path(execution_id: str) -> Path:
    if not isinstance(execution_id, str) or not _ID_RE.fullmatch(execution_id):
        raise core.OpsxError(f"非法 execution_id: {execution_id!r}")
    return _dir() / f"{execution_id}.json"


@contextmanager
def _locked():
    """写记录的互斥：CLI 与 agent 可能同时收尾不同记录。"""
    core._ensure_state_dir()
    d = _dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(d / ".mutex", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
def new_id() -> str:
    return "exe-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _write(rec: dict) -> None:
    """0600 原子写：记录里有命令原文与输出片段，等同审计材料。"""
    path = _path(rec["id"])
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(rec, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    """损坏/形状不对的记录直接跳过：一条坏记录不该让 opsx executions 整体失败。"""
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("id"), str):
        return None
    if rec.get("status") not in STATUSES or rec.get("kind") not in KINDS:
        return None
    return rec


def _pid_alive(pid) -> bool:
    if pid is None:
        return False
    if type(pid) is not int or not 0 < pid < 2 ** 31:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于我们：视为存活（安全方向）
    except (OverflowError, ValueError):
        return False
    return True


def begin(kind: str, cmd: str, target: str | None = None, env: dict | None = None,
          snapshot_id: str | None = None, phase: str = "exec") -> dict:
    """开一条执行记录。phase 标记卡在哪一步（snapshot/exec/restore），断线时有用。"""
    if kind not in KINDS:
        raise core.OpsxError(f"未知的变更类型: {kind!r}")
    now = time.time()
    rec = {
        "id": new_id(), "kind": kind, "status": "running", "phase": phase,
        "cmd": cmd, "cmd_hash": core.cmd_hash(cmd, target),
        "target": target, "host": (env or {}).get("host"),
        "env": env or {}, "snapshot_id": snapshot_id,
        "pid": os.getpid(), "started": core_iso(now), "started_ts": now,
        "finished": None, "finished_ts": None, "duration_s": None,
        "exit_code": None, "error": None, "output_preview": "",
        "resolved": None,
    }
    with _locked():
        _write(rec)
    core._safe_audit("execution-begin", execution_id=rec["id"], kind=kind, target=target,
               snapshot_id=snapshot_id, phase=phase)
    return rec


def core_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def get(execution_id: str, view: bool = True) -> dict | None:
    """按 id 取记录。默认返回**视图**（与 list 一致）：否则 show 显示 running、
    list 显示 unknown，同一条记录给出两个结论。resolve 走内部 `_read`。"""
    path = _path(execution_id)
    if not path.is_file():
        return None
    rec = _read(path)
    return _view(rec) if (rec is not None and view) else rec


def update(execution_id: str, **fields) -> dict | None:
    """就地更新字段（不改状态以外的语义）。记录不存在返回 None。"""
    with _locked():
        path = _path(execution_id)
        rec = _read(path) if path.is_file() else None
        if rec is None:
            return None
        allowed = {"phase", "snapshot_id", "host", "env"}
        for key, value in fields.items():
            if key in allowed:
                rec[key] = value
        _write(rec)
    return rec


def finish(execution_id: str, status: str, *, exit_code=None, error=None,
           output: str = "", phase: str | None = None) -> dict | None:
    """收尾。已经是终态的记录不会被覆盖——reconcile 判定 unknown 之后，
    迟到的原进程不能再把结论改回 succeeded（那正好把"无法确认"抹掉）。"""
    if status not in STATUSES or status == "running":
        raise core.OpsxError(f"收尾状态非法: {status!r}")
    with _locked():
        path = _path(execution_id)
        rec = _read(path) if path.is_file() else None
        if rec is None:
            return None
        if rec.get("status") != "running":
            return rec
        now = time.time()
        rec.update({
            "status": status, "exit_code": exit_code,
            "error": core.redact(str(error))[:500] if error else None,
            "output_preview": core.redact((output or "")[:PREVIEW_LIMIT]),
            "finished": core_iso(now), "finished_ts": now,
            "duration_s": round(now - float(rec.get("started_ts") or now), 1),
        })
        if phase:
            rec["phase"] = phase
        _write(rec)
    core._safe_audit("execution-finish", execution_id=execution_id, status=status,
               exit_code=exit_code, error=rec.get("error"))
    return rec


def reconcile() -> list[dict]:
    """running 但进程已不在 → unknown。

    进程被杀时我们确实不知道命令执行到哪了，所以不能标 failed（会让人以为没事），
    更不能重放。标 unknown 并进入待核实队列，是唯一诚实的结论。
    """
    d = _dir()
    if not d.is_dir():
        return []
    moved = []
    with _locked():
        for path in sorted(d.glob("*.json")):
            rec = _read(path)
            if rec is None or rec.get("status") != "running":
                continue
            if _pid_alive(rec.get("pid")):
                continue
            fresh = _read(path)
            if fresh is None or fresh.get("status") != "running":
                continue
            if fresh.get("started_ts") != rec.get("started_ts"):
                continue
            now = time.time()
            # 命令没下发（还在拿锁/做快照）就没什么可核实的，记 aborted 而不是 unknown，
            # 否则一次参数写错就会制造一条"待核实"并锁住该目标该命令。
            before_exec = fresh.get("phase") in BEFORE_EXEC_PHASES
            fresh.update({
                "status": "aborted" if before_exec else "unknown",
                "finished": core_iso(now), "finished_ts": now,
                "duration_s": round(now - float(fresh.get("started_ts") or now), 1),
                "error": ("执行进程在命令下发前退出（命令未执行）" if before_exec else
                          "执行进程已退出，无法确认命令是否完成（需人工核实）"),
            })
            _write(fresh)
            moved.append(fresh)
    for rec in moved:
        core._safe_audit("execution-unknown" if rec["status"] == "unknown"
                         else "execution-aborted",
                         execution_id=rec["id"], target=rec.get("target"),
                         reason="执行进程消失")
    return moved


def _view(rec: dict) -> dict:
    """读取时的状态视图：进程已死但记录仍是 running，按实际含义呈现。

    不在这里写盘——`ops_executions` 这类工具声明为只读，落盘对账交给
    `reconcile()`（CLI / daemon）。但门禁必须看到真实风险，所以视图里：
    - phase 已进入 exec（命令可能已下发）→ unknown（需要人核实）
    - phase 还在 lock/snapshot（命令一定没下发）→ aborted（明确"没执行"）
    """
    if rec.get("status") != "running" or _pid_alive(rec.get("pid")):
        return rec
    shown = dict(rec)
    if rec.get("phase") in BEFORE_EXEC_PHASES:
        shown["status"] = "aborted"
        shown["error"] = rec.get("error") or "执行进程在命令下发前退出（命令未执行）"
    else:
        shown["status"] = "unknown"
        shown["error"] = rec.get("error") or \
            "执行进程已退出，无法确认命令是否完成（需人工核实）"
    shown["stale"] = True   # 文件里仍是 running，reconcile 才会落盘
    return shown


def list_records(tail: int = 20, target: str | None = None,
                 status: str | None = None) -> list[dict]:
    d = _dir()
    if not d.is_dir():
        return []
    out = []
    for path in d.glob("*.json"):
        rec = _read(path)
        if rec is None:
            continue
        rec = _view(rec)
        if target and rec.get("target") != target:
            continue
        if status and rec.get("status") != status:
            continue
        out.append(rec)
    out.sort(key=lambda r: float(r.get("started_ts") or 0), reverse=True)
    return out[:tail] if tail >= 0 else out


def find_latest(cmd: str, target: str | None = None) -> dict | None:
    """按「命令 + 目标」找最近一条执行记录（CLI 展示 execution_id 用）。

    不缓存"最后一次 id"：并发时模块级变量会串台。同目标同命令本来就被目标锁
    串行化，所以按 cmd_hash 找最近一条是准确的。
    """
    digest = core.cmd_hash(cmd, target)
    for rec in list_records(tail=-1, target=target):
        if rec.get("cmd_hash") == digest:
            return rec
    return None


def pending_unknown(target: str | None, cmd_hash: str | None = None) -> dict | None:
    """同一目标上未核实的 unknown 执行（可按命令哈希收窄）。

    这是"不自动重放"的执行点：只要还有一条没核实的记录，就不再放行新执行。
    收窄到命令哈希是为了不因为一次失败的变更把整个目标永久锁死——同一目标上
    另一条命令仍然可以走（拿的是同一把目标锁，串行但不阻塞）。
    """
    for rec in list_records(tail=-1, target=target, status="unknown"):
        if rec.get("resolved"):
            continue
        if cmd_hash is not None and rec.get("cmd_hash") != cmd_hash:
            continue
        return rec
    return None


def resolve(execution_id: str, outcome: str, note: str = "") -> dict:
    """人工核实一条 unknown：观察到已生效（observed-ok）或未生效（observed-failed）。

    核实不解锁"重放"——它只是把记录从待核实队列里移出。是否再执行一次由人决定：
    observed-failed 时人知道前一次没生效，可重新批准执行；observed-ok 时再执行一次
    往往是错的（重复生效），所以这里只记录事实，不提供任何自动动作。
    """
    if outcome not in OUTCOMES:
        raise core.OpsxError(f"核实结论须为 {OUTCOMES} 之一")
    with _locked():
        path = _path(execution_id)
        rec = _read(path) if path.is_file() else None
        if rec is None:
            raise core.OpsxError(f"没有这条执行记录: {execution_id}")
        if rec.get("status") == "running":
            # 进程已死但还没对账：人可以直接核实，不必先跑一次 list 触发 reconcile
            shown = _view(rec)
            shown.pop("stale", None)
            if shown.get("status") == "unknown":
                now = time.time()
                shown["finished"] = shown.get("finished") or core_iso(now)
                shown["finished_ts"] = shown.get("finished_ts") or now
                _write(shown)
                core._safe_audit("execution-unknown", execution_id=execution_id,
                                 target=shown.get("target"), reason="核实时发现进程已退出")
            rec = shown
        if rec.get("status") != "unknown":
            raise core.OpsxError(
                f"{execution_id} 当前状态是 {rec.get('status')}，只有 unknown 需要核实")
        rec["resolved"] = {"outcome": outcome, "note": core.redact(str(note))[:300],
                           "at": core_iso(time.time())}
        _write(rec)
    core._safe_audit("execution-resolve", execution_id=execution_id, outcome=outcome,
               note=note, target=rec.get("target"))
    return rec


def prune(keep: int = KEEP) -> None:
    """清理旧记录，但永不清理未核实的 unknown（那等于把待办删了）。"""
    d = _dir()
    if not d.is_dir():
        return
    cutoff = time.time() - 7 * 86400
    for path in d.glob("*.json"):
        # 损坏/半写的记录读不出来，也就永远排不进 keep 的统计：按 mtime 超期清理
        if _read(path) is not None:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
    records = list_records(tail=-1)
    if len(records) <= keep:
        return
    for rec in records[keep:]:
        if rec.get("status") == "running":
            continue  # 正在跑的记录被删掉，收尾时就找不到它，结果永久丢失
        if rec.get("status") in OPEN_STATUSES and not rec.get("resolved"):
            continue
        try:
            _path(rec["id"]).unlink()
        except OSError:
            pass


def format_record(rec: dict) -> str:
    where = f"目标 {rec['target']}" if rec.get("target") else "本机"
    line = (f"{rec.get('id') or '?'}  {rec.get('kind') or '?'}  "
            f"{rec.get('status') or '?'}  {where}  {rec.get('started') or '?'}")
    if rec.get("duration_s") is not None:
        line += f"（{rec['duration_s']}s）"
    if rec.get("exit_code") is not None:
        line += f"  exit={rec['exit_code']}"
    if rec.get("snapshot_id"):
        line += f"  快照 {rec['snapshot_id']}"
    lines = [line]
    if rec.get("cmd"):
        lines.append(f"  命令: {core.redact(rec['cmd'])[:200]}")
    if rec.get("error"):
        lines.append(f"  错误: {rec['error']}")
    resolved = rec.get("resolved")
    if isinstance(resolved, dict):
        lines.append(f"  已核实: {resolved.get('outcome')}（{resolved.get('at')}）"
                     + (f" {resolved.get('note')}" if resolved.get("note") else ""))
    elif rec.get("status") in OPEN_STATUSES:
        lines.append("  待核实：opsx executions resolve "
                     f"{rec.get('id')} --observed-ok|--observed-failed")
    if rec.get("stale"):
        lines.append("  （记录文件里仍是 running：执行进程已不在，对账会落盘为上面这个状态）")
    return "\n".join(lines)


def format_list(records: list[dict]) -> str:
    if not records:
        return "(无执行记录)"
    return "\n".join(format_record(rec) for rec in records)


def summary(target: str | None = None) -> dict:
    """状态汇总（opsx executions / agent 工具共用）。"""
    records = list_records(tail=-1, target=target)
    counts = {status: 0 for status in STATUSES}
    for rec in records:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    pending = [r for r in records
               if r.get("status") in OPEN_STATUSES and not r.get("resolved")]
    return {"total": len(records), "counts": counts,
            "pending": len(pending),
            "pending_ids": [r["id"] for r in pending[:10]]}
