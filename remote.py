"""远程执行通道：SSH 目标注册表 + 远程快照/回滚。

守门仍在本机：同一份 config.yaml 规则、同一套审批戳（按「目标+命令」绑定）、本地审计；
执行经 ssh 下发。目标机不装任何东西——快照用 `cat` 拉回本地存档，回滚用 stdin 推回。
远程快照路径请用绝对路径（相对路径在抓取与回写时的基准目录可能不同）。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

SSH = "ssh"
CONNECT_TIMEOUT = 10   # 连接超时秒数（目标可用 connect_timeout 覆盖）
IO_TIMEOUT = 120       # 只读命令 / 快照抓取超时
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FIXED_TARGET = ContextVar("opsx_remote_target", default=None)


class RemoteError(Exception):
    pass


class _UncertainChange(RemoteError):
    """变更可能已经生效，但我们无法确认结果（超时 / 缺少后验输出）。

    单独一类是为了让调用方把执行记录标成 unknown 而不是 failed：后者会让人以为
    "没生效"，从而重跑一条可能已经生效的命令。
    """


def _targets_file() -> Path:
    return core.STATE_DIR / "targets.json"


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise RemoteError(f"非法的目标名: {name!r}（只允许字母数字._-）")
    return name


def load_targets() -> dict[str, dict]:
    """读目标注册表；文件不存在视为无目标。"""
    f = _targets_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RemoteError(f"目标注册表解析失败 {f}: {e}") from e
    if not isinstance(data, dict):
        raise RemoteError(f"目标注册表须为 {{名称: 配置}} 对象: {f}")
    return data


def get_target(name: str) -> dict:
    _check_name(name)
    if name == "_local":
        # 只保留这一个名字：它是本机检查配置（agent/checks.py），不是 SSH 目标。
        # 其他下划线开头的名字历史上就允许，保持可用。
        raise RemoteError("'_local' 是本机检查配置条目，不能作为 SSH 目标")
    fixed = _FIXED_TARGET.get()
    if fixed is not None and fixed[0] == name:
        return fixed[1]
    targets = load_targets()
    t = targets.get(name)
    if not isinstance(t, dict):
        known = ", ".join(sorted(n for n in targets if n != "_local")) or "无"
        raise RemoteError(f"未注册的 SSH 目标: {name}（已注册: {known}；"
                          f"在 {_targets_file()} 注册后才可用）")
    return t


@contextmanager
def _fixed_target(name: str):
    # 同一次变更的快照、审批和下发必须用同一目标，不能中途重读后发往另一台主机。
    token = _FIXED_TARGET.set((name, get_target(name)))
    try:
        yield
    finally:
        _FIXED_TARGET.reset(token)


def target_env(name: str) -> dict:
    """目标的实际主机指纹（审批绑定用）：host/user/port/identity/options 任一改动旧戳即失效。"""
    t = get_target(name)
    ssh_argv(name)  # 复用字段校验：非法主机/端口/选项在发戳时就拒绝，而不是执行时
    return {"scope": "remote", "target": name, "host": str(t.get("host") or ""),
            "user": str(t.get("user") or ""), "port": t.get("port") or 22,
            "identity": str(t.get("identity") or ""),
            "options": list(t.get("options") or []),
            "kubeconfig": os.environ.get("KUBECONFIG", "")}


def _snapshot_target_env(meta: dict) -> dict:
    """快照记录的目标指纹 vs 当前注册表：不一致就拒绝回滚（避免写到另一台机器）。"""
    target = meta["target"]
    current = target_env(target)
    recorded = meta.get("env")
    if isinstance(recorded, dict):
        for key in ("host", "user", "port", "identity", "options"):
            if recorded.get(key) != current.get(key):
                raise RemoteError(
                    f"快照记录的 {target} 与当前注册表不一致（{key}: {recorded.get(key)!r} → "
                    f"{current.get(key)!r}）：拒绝回滚，请核对 targets.json 或重新快照")
    # 旧快照没有机器指纹：按 target 名解析（兼容历史），但不再静默信任"同名即同机"
    recorded_machine = meta.get("machine")
    if isinstance(recorded_machine, str) and recorded_machine:
        now_machine = _target_machine(target)
        if not now_machine:
            raise RemoteError("无法确认当前目标机指纹，拒绝回滚；请先恢复目标连通性后重试")
        if now_machine != recorded_machine:
            raise RemoteError(
                f"快照记录的主机指纹 {recorded_machine} 与当前目标机 {now_machine} 不一致："
                "同名目标很可能已被改指向另一台机器，拒绝回滚。请核对 targets.json 后重试")
    return current


def _str_field(t: dict, key: str) -> str:
    """目标字段只作为独立 argv 传入（不经 shell）；空白或前导 '-' 会让 ssh
    把配置值当成选项（如 -oProxyCommand=…），直接拒绝。"""
    v = t.get(key)
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or v[0] == "-" or "\x00" in v or re.search(r"\s", v):
        raise RemoteError(f"目标字段 {key} 非法: {v!r}")
    return v


def ssh_argv(name: str, remote_cmd: str | None = None) -> list[str]:
    """目标名 → ssh 命令行。主机只能来自注册表，模型无法自行指定主机。"""
    t = get_target(name)
    host = _str_field(t, "host")
    if not host:
        raise RemoteError(f"目标 {name} 缺 host 字段")
    ct = t.get("connect_timeout", CONNECT_TIMEOUT)
    if type(ct) is not int or ct <= 0:
        raise RemoteError(f"目标 {name} 的 connect_timeout 非法: {ct!r}")
    # BatchMode：不弹密码提示（agent 无人值守会挂死）；要求密钥/agent 免密。
    argv = [SSH, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={ct}"]
    port = t.get("port")
    if port is not None:
        if type(port) is not int or not 0 < port < 65536:
            raise RemoteError(f"目标 {name} 的 port 非法: {port!r}")
        argv += ["-p", str(port)]
    user = _str_field(t, "user")
    if user:
        argv += ["-l", user]
    ident = _str_field(t, "identity")
    if ident:
        argv += ["-i", str(Path(ident).expanduser())]
    opts = t.get("options", [])
    if not isinstance(opts, list) or not all(isinstance(o, str) and "\x00" not in o for o in opts):
        raise RemoteError(f"目标 {name} 的 options 须为字符串数组")
    argv += opts
    argv.append(host)
    if remote_cmd is not None:
        argv.append(remote_cmd)
    return argv


def _ssh(name: str, remote_cmd: str, stdin: bytes | None = None,
         timeout: int = IO_TIMEOUT) -> subprocess.CompletedProcess:
    argv = ssh_argv(name, remote_cmd)
    kw = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, **kw)
    except FileNotFoundError as e:
        raise RemoteError(f"找不到 ssh 客户端: {e}") from e
    except PermissionError as e:
        raise RemoteError(f"ssh 不可执行: {argv[0]}: {e}") from e
    except (OSError, ValueError) as e:
        raise RemoteError(f"ssh 执行失败: {e}") from e


def _text(r: subprocess.CompletedProcess) -> str:
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    return out + (("\n[stderr]\n" + err) if err.strip() else "")


def run(name: str, cmd: str, timeout: int = IO_TIMEOUT) -> tuple[int, str]:
    """执行远程命令，返回 (exit_code, 合并输出)。不做守门判断——调用方负责。"""
    try:
        r = _ssh(name, cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 4, f"[opsx] 超时：远程命令超过 {timeout}s 未结束，SSH 已终止，远端状态需确认"
    return r.returncode, _text(r)


def _dest(d: Path, path_str: str) -> Path:
    if not isinstance(path_str, str) or not path_str.startswith("/") or "\x00" in path_str:
        raise RemoteError(f"远程快照须用绝对路径: {path_str!r}")
    parts = [p for p in str(path_str).split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise RemoteError(f"快照路径非法: {path_str}")
    dest = d / "files" / "/".join(parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _target_stat(name: str, path_str: str) -> tuple[int | None, str]:
    """一次 SSH 同时取权限（八进制）与属主，返回 (mode|None, "user:group")。

    为什么要一次取：快照是在目标锁内做的，每个文件多两次往返会把锁的持有时间
    成倍拉长（N 个文件从 N 次变成 3N+1 次 SSH）。
    取不到权限返回 None：回滚时"覆盖已有文件沿用目标当前权限、重建用 600"，
    绝不用目标机默认 umask 猜——那可能把 0600 的配置恢复成 0644。
    """
    q = shlex.quote(path_str)
    cmd = (f"stat -L -c '%a %U:%G' -- {q} 2>/dev/null "
           f"|| stat -L -f '%Lp %Su:%Sg' -- {q} 2>/dev/null || echo '?'")
    try:
        r = _ssh(name, cmd, timeout=IO_TIMEOUT)
    except (subprocess.TimeoutExpired, RemoteError):
        return None, ""
    if r.returncode != 0:
        return None, ""
    lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
    for line in reversed(lines):
        fields = line.split()
        if fields and re.fullmatch(r"[0-7]{1,4}", fields[0]):
            owner = fields[1] if len(fields) > 1 else ""
            return int(fields[0], 8), (owner if owner and owner != "?" else "")
    return None, ""


def _target_mode(name: str, path_str: str) -> int | None:
    return _target_stat(name, path_str)[0]


def _target_owner(name: str, path_str: str) -> str:
    """属主/属组名（只用于报告与核对，不用于恢复：改属主通常需要 root）。"""
    return _target_stat(name, path_str)[1] or "?"


def _target_machine(name: str) -> str:
    """目标机指纹：hostname + machine-id 的前 12 位。

    用途不是安全边界（SSH 认证才是），而是识别"targets.json 里同名目标被改指向
    另一台机器"这种运维事故：快照来自 A 机，回滚打到 B 机就是灾难。
    """
    cmd = ("printf 'OPSX-HOST '; { hostname 2>/dev/null || uname -n 2>/dev/null || echo '?'; }; "
           "printf 'OPSX-MACHINE-ID '; cat /etc/machine-id 2>/dev/null || true; printf '\\n'")
    try:
        r = _ssh(name, cmd, timeout=IO_TIMEOUT)
    except (subprocess.TimeoutExpired, RemoteError):
        return ""
    if r.returncode != 0:
        return ""
    fields = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        for marker in ("OPSX-HOST ", "OPSX-MACHINE-ID "):
            if line.startswith(marker):
                fields[marker] = line[len(marker):].strip()
    host, machine_id = fields.get("OPSX-HOST ", ""), fields.get("OPSX-MACHINE-ID ", "")
    if not host or host == "?":
        return ""
    return f"{host}/{machine_id[:12]}" if machine_id else host


def _target_resolved_path(name: str, path_str: str) -> str:
    if (not isinstance(path_str, str) or not path_str.startswith("/")
            or any(ord(character) < 32 or ord(character) == 127 for character in path_str)):
        raise RemoteError(f"远程文件路径须为绝对路径且不含控制字符: {path_str!r}")
    script = ("set -eu\n" + _RESOLVE_TEMPLATE.replace("@@PATH@@", shlex.quote(path_str))
              + "\nprintf 'OPSX-RESOLVED\\000%s\\000' \"$p\"\n")
    try:
        response = _ssh(name, script, timeout=IO_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise RemoteError(f"解析远程文件路径超时: {path_str}") from error
    marker = b"OPSX-RESOLVED\x00"
    if response.returncode == 0 and marker in response.stdout:
        raw, terminator, _ = response.stdout.rsplit(marker, 1)[1].partition(b"\x00")
        try:
            resolved = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise RemoteError(f"远程文件路径无法解析: {path_str}") from error
        if terminator and resolved.startswith("/"):
            return resolved
    raise RemoteError(f"无法确认远程文件真实路径: {path_str}")


def _pull_file(name: str, path_str: str, d: Path, items: list) -> None:
    """远程文件拉回本地存档。取不到就 abort——没有快照不允许执行。

    同时记录权限与属主：回滚要把权限一起还原，否则"内容对了、权限松了"
    等于把 0600 的密钥文件恢复成 0644。
    """
    dest = _dest(d, path_str)
    resolved = _target_resolved_path(name, path_str)
    try:
        r = _ssh(name, f"cat -- {shlex.quote(resolved)}", timeout=IO_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RemoteError(f"远程取快照超时: {path_str}") from e
    if r.returncode != 0:
        raise RemoteError(f"远程取快照失败({r.returncode}): {path_str}: {_text(r)[:300]}")
    dest.write_bytes(r.stdout)
    item = {"type": "file", "remote": name, "src": path_str,
            "dest": str(dest.relative_to(d)), "resolved_path": resolved}
    mode, owner = _target_stat(name, resolved)
    if mode is not None:
        item["mode"] = mode
    if owner:
        item["owner"] = owner
    items.append(item)


def _capture_cmd(name: str, cmd: str, label: str, d: Path, items: list) -> None:
    code, msg = core.check_snapshot_cmd(cmd, target=name)
    if code != 0:
        raise RemoteError(msg)
    try:
        r = _ssh(name, cmd, timeout=IO_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RemoteError(f"远程抓取状态超时: {cmd}") from e
    if r.returncode != 0:
        raise RemoteError(f"远程抓取状态失败({r.returncode}): {cmd}: {_text(r)[:300]}")
    fname = core._snapshot_output_name(label, items)
    (d / fname).write_bytes(r.stdout)
    items.append({"type": "cmd", "remote": name, "cmd": cmd, "name": label, "output": fname})


def _write_meta(d: Path, sid: str, name: str, items: list) -> None:
    meta = {"id": sid, "target": name, "env": target_env(name),
            # 机器指纹：回滚前比对，防"同名目标被改指向另一台机器"时把 A 机配置写到 B 机
            "machine": _target_machine(name),
            "created": datetime.now(timezone.utc).isoformat(), "items": items}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot_file(name: str, path_str: str) -> str:
    """远程文件快照：拉回本地存档（回滚时推回目标机）。"""
    sid, d = core.begin_snapshot()
    items: list = []
    try:
        with _fixed_target(name):
            _pull_file(name, path_str, d, items)
            _write_meta(d, sid, name, items)
    except (RemoteError, OSError):
        shutil.rmtree(d, ignore_errors=True)
        raise
    core.audit("snapshot", id=sid, kind="file", target=name, src=path_str)
    core.prune_snapshots()
    return sid


def snapshot_cmd(name: str, cmd: str, label: str = "") -> str:
    get_target(name)
    code, msg = core.check_snapshot_cmd(cmd, target=name)
    if code != 0:
        raise RemoteError(msg)
    sid, d = core.begin_snapshot()
    items: list = []
    try:
        with _fixed_target(name):
            _capture_cmd(name, cmd, label, d, items)
            _write_meta(d, sid, name, items)
    except (RemoteError, OSError):
        shutil.rmtree(d, ignore_errors=True)
        raise
    core.audit("snapshot", id=sid, kind="cmd", target=name, cmd=cmd)
    core.prune_snapshots()
    return sid


def exec_change(name: str, cmd: str, snapshot_files: list[str] | None = None,
                snapshot_cmds: list[tuple[str, str]] | None = None,
                timeout: int = core.EXEC_TIMEOUT, actor: str = "cli",
                lock_wait: float = core.DEFAULT_LOCK_WAIT) -> tuple[int, str | None, str]:
    """远程原子执行：守门→锁→重验→远程快照→消耗审批→ssh 执行→记录状态。

    返回 (exit_code, snapshot_id, output)，与 core.exec_change 同形
    （exit=2 无授权、3 快照失败、4 超时、120 有未核实执行、121 目标被占用）。

    远程超时与 SSH 255 都记 unknown 而不是 failed：这两种情况下远端可能已经
    执行了一半，标 failed 会让人以为"没生效"而重跑，正是最危险的误判。
    """
    import execution
    snapshot_files = snapshot_files or []
    snapshot_cmds = snapshot_cmds or []
    get_target(name)  # 先把"打错目标名"报出来，而不是误导用户去建审批戳
    code, msg = core._prepare_exec(cmd, name)
    if code != 0:
        return code, None, msg
    for c, _ in snapshot_cmds:  # 快照命令本身也过守门：否则可把写操作藏进抓取参数
        code, _ = core.check_snapshot_cmd(c, target=name)
        if code != 0:
            core._safe_audit("exec-abort", cmd=cmd, target=name, reason=f"快照命令未通过守门: {c}")
            output = core.redact(f"[opsx] 快照命令未通过守门，已中止执行: {c}")
            print(output, file=sys.stderr)
            return 2, None, output

    core._validate_lock_args(name, lock_wait)   # 参数错误要在建记录之前就失败
    # env 必须与审批时同源（core.resolve_env = target_env + k8s 作用域）：
    # 用 target_env 会漏掉 kubectl 命令的 k8s 键，导致远程 kubectl 变更永远判定
    # "执行环境已变化"，重新批准也不通。
    env = core.resolve_env(cmd, name)
    rec = execution.begin("exec", cmd, name, env, phase="lock")
    sid = None
    dispatched = False   # 命令是否真的下发过：决定中断时记 unknown 还是 aborted
    try:
        with _fixed_target(name), core.target_lock(
                name, wait=lock_wait, actor=actor, execution_id=rec["id"]):
            env = core.resolve_env(cmd, name)
            execution.update(rec["id"], env=env)
            code, msg = core.check(cmd, name, env)  # 锁内重验：等锁期间审批可能过期
            if code != 0:
                core._safe_audit("exec-abort", cmd=cmd, target=name, reason="等锁后审批失效")
                execution.finish(rec["id"], "aborted", error=msg, phase="lock")
                return code, None, msg
            code, msg = core._prepare_exec(cmd, name)
            if code != 0:
                execution.finish(rec["id"], "aborted", error=msg, phase="lock")
                return code, None, msg
            if snapshot_files or snapshot_cmds:
                execution.update(rec["id"], phase="snapshot")
                sid, d = core.begin_snapshot()
                items: list = []
                try:
                    for f in snapshot_files:
                        _pull_file(name, f, d, items)
                    for c, label in snapshot_cmds:
                        _capture_cmd(name, c, label, d, items)
                    _write_meta(d, sid, name, items)
                except (RemoteError, OSError, subprocess.SubprocessError) as e:
                    shutil.rmtree(d, ignore_errors=True)
                    output = core.redact(f"[opsx] 远程快照失败，已中止执行: {e}")
                    print(output, file=sys.stderr)
                    core._safe_audit("exec-abort", cmd=cmd, target=name, reason=str(e))
                    execution.finish(rec["id"], "aborted", error=output, phase="snapshot")
                    return 3, None, output
                execution.update(rec["id"], snapshot_id=sid, phase="authorize")
            if core.classify(cmd)[0] != "R0":
                # 只有需要审批的变更才消耗次数：R0 只读命令没有戳，也不该被要求有
                remaining = core.consume_stamp(cmd, name, env)
                if remaining is None:
                    output = core.redact(
                        "[opsx] 审批戳已失效或已用尽，已中止执行（未做任何变更）")
                    core._safe_audit("exec-abort", cmd=cmd, target=name, snapshot_id=sid,
                               reason="审批戳已失效")
                    execution.finish(rec["id"], "aborted", error=output, phase="exec")
                    return 2, sid, output
            execution.update(rec["id"], phase="exec")
            dispatched = True   # 从这一刻起远端可能已经生效
            exit_code, status, output, err = _run_remote_change(name, cmd, timeout)
            execution.finish(rec["id"], status, exit_code=exit_code, error=err,
                             output=output)
    except core.TargetBusy as e:
        core._safe_audit("exec-abort", cmd=cmd, target=name, reason=str(e))
        execution.finish(rec["id"], "aborted", error=str(e), phase="lock")
        return core.EXEC_ABORT_LOCK, None, str(e)
    except BaseException as e:  # 含 Ctrl-C：远端命令可能仍在跑，不能当成"没执行"
        reason = (f"{type(e).__name__}: {e}" if not isinstance(e, KeyboardInterrupt)
                  else "用户中断（Ctrl-C），无法确认远端命令是否完成")
        core._safe_audit("exec-abort", cmd=cmd, target=name, snapshot_id=sid,
                         reason=reason)
        # 命令没下发（还在拿锁/做快照/校验）就是明确的中止；下发过才谈得上"结果未知"
        execution.finish(rec["id"], "unknown" if dispatched else "aborted", error=reason)
        if isinstance(e, (core.OpsxError, KeyboardInterrupt)):
            raise core.OpsxError(core.redact(
                f"[opsx] 变更未完成：{reason}；执行记录 {rec['id']} "
                + ("需核实" if dispatched else "命令未执行"))) from e
        raise
    core._safe_audit("exec", cmd=cmd, target=name, snapshot_id=sid, exit_code=exit_code,
               execution_id=rec["id"], output=output[:core.EXEC_AUDIT_OUTPUT])
    core.prune_snapshots()
    execution.prune()
    return exit_code, sid, output


def _run_remote_change(name: str, cmd: str, timeout: int) -> tuple[int, str, str, str | None]:
    """执行并归类结果，返回 (exit_code, 状态, 输出, 错误说明)。"""
    try:
        r = _ssh(name, cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (4, "unknown",
                f"[opsx] 超时：远程命令超过 {timeout}s 未结束，SSH 已终止，远端状态需确认",
                f"远程执行超时 {timeout}s，远端可能仍在执行")
    except RemoteError as e:
        # ssh 客户端本身起不来：命令一定没下发，可以判 failed
        return 1, "failed", core.redact(f"[opsx] 远程执行失败: {e}"), str(e)
    output = core.redact(_text(r))
    if r.returncode == 255 or r.returncode < 0:
        return (r.returncode, "unknown",
                output + f"\n[opsx] SSH 会话异常（exit {r.returncode}）：无法确认命令是否执行完成。"
                "若已确认未生效，用 opsx executions resolve <id> --observed-failed 关闭后重试",
                f"SSH 会话异常（exit {r.returncode}），远端执行状态未知")
    return (r.returncode, "succeeded" if r.returncode == 0 else "failed", output,
            None if r.returncode == 0 else f"命令退出码 {r.returncode}")


def _remote_read(name: str, path_str: str) -> tuple[bool, bytes]:
    """读取目标机当前文件内容（回滚预览/回滚前副本用）。返回 (是否存在, 内容)。"""
    q = shlex.quote(path_str)
    remote_cmd = (f"if [ -e {q} ]; then printf 'OPSX-EXISTS\\n'; cat -- {q}; "
                  "else printf 'OPSX-MISSING\\n'; fi")
    try:
        r = _ssh(name, remote_cmd, timeout=IO_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RemoteError(f"读取远程文件超时: {path_str}") from e
    if r.returncode != 0:
        raise RemoteError(f"读取远程文件失败({r.returncode}): {path_str}: {_text(r)[:300]}")
    if r.stdout.startswith(b"OPSX-EXISTS\n"):
        return True, r.stdout[len(b"OPSX-EXISTS\n"):]
    if r.stdout.startswith(b"OPSX-MISSING\n"):
        return False, b""
    raise RemoteError(f"远程文件状态无法判定: {path_str}")


def rollback_plan(snapshot_id: str) -> dict:
    """远程快照的回滚预览：经 SSH 读取目标机当前内容再比对。"""
    d, meta = core.load_snapshot(snapshot_id)
    target = meta.get("target")
    if not target:
        return core.rollback_plan(snapshot_id)
    _snapshot_target_env(meta)  # 目标被改指向时不能拿旧快照覆盖新机器
    return core.rollback_plan(
        snapshot_id, read_state=lambda src: _remote_read(target, src))


# 原子恢复脚本：同目录临时文件 → chmod → rename。
# 为什么不能 `cat > 目标`：写到一半断线会留下半份配置，这比"没回滚"更糟——
# 服务可能起不来，而且文件"看起来是在的"。rename 在同一文件系统内是原子的，
# 所以要么是旧内容，要么是完整的新内容。
_RESOLVE_TEMPLATE = """\
p=@@PATH@@
n=0
while [ -L "$p" ] && [ "$n" -lt 8 ]; do
  t=$(readlink "$p")
  case "$t" in
    /*) p="$t" ;;
    *) p="$(dirname -- "$p")/$t" ;;
  esac
  n=$((n + 1))
done
if [ -L "$p" ]; then
  printf 'opsx-restore-symlink-depth-exceeded\n' >&2
  exit 3
fi
d=$(cd -P -- "$(dirname -- "$p")" && pwd -P)
p="$d/$(basename -- "$p")"
"""

_RESTORE_TEMPLATE = "set -eu\n" + _RESOLVE_TEMPLATE + """\
expected=@@RESOLVED@@
if [ -n "$expected" ] && [ "$p" != "$expected" ]; then
  printf 'opsx-restore-path-changed\n' >&2
  exit 3
fi
had=0
if [ -e "$p" ]; then had=1; fi
if [ "$had" = "1" ] && [ ! -f "$p" ]; then
  printf 'opsx-restore-not-regular-file\n' >&2
  exit 3
fi
owner=
if [ "$had" = "1" ]; then
  owner=$(stat -c '%u:%g' -- "$p" 2>/dev/null || stat -f '%u:%g' -- "$p" 2>/dev/null)
fi
d=$(dirname -- "$p")
tmp=$(mktemp "$d/.opsx-restore.XXXXXX")
trap 'if [ -n "${tmp:-}" ]; then rm -f "$tmp"; fi' EXIT HUP INT TERM
cat > "$tmp"
got=$(wc -c < "$tmp" | tr -d ' ')
if [ "$got" != "@@SIZE@@" ]; then
  printf 'opsx-restore-incomplete got=%s want=@@SIZE@@\n' "$got" >&2
  exit 3
fi
if [ -n "$owner" ]; then chown "$owner" "$tmp"; fi
if [ -n "@@MODE@@" ]; then
  chmod "@@MODE@@" "$tmp"
elif [ "$had" = "1" ]; then
  keep=$(stat -c %a -- "$p" 2>/dev/null || stat -f %Lp -- "$p" 2>/dev/null || echo "")
  case "$keep" in
    ''|*[!0-7]*) printf 'opsx-restore-mode-unavailable\n' >&2; exit 3 ;;
    *) chmod "$keep" "$tmp" ;;
  esac
else
  chmod 600 "$tmp"
fi
mv -f "$tmp" "$p"
tmp=
printf 'opsx-restored had=%s size=%s mode=%s\n' "$had" "$got" "$(stat -c %a -- "$p" 2>/dev/null || stat -f %Lp -- "$p" 2>/dev/null || echo ?)"
"""

FALLBACK_MODE = "600"   # 重建文件且快照没记权限时的保守值
_RENDER_RE = re.compile(r"@@(PATH|MODE|SIZE|RESOLVED)@@")


def _restore_mode(item: dict) -> tuple[str, str]:
    """返回 (chmod 参数, 需要说明的补充文本)。空参数表示"沿用目标机当前权限"。"""
    mode = item.get("mode")
    if type(mode) is int and 0 <= mode <= 0o7777:
        return format(mode, "o"), ""
    return "", ("（快照未记录权限：覆盖已有文件时沿用目标机当前权限，"
                f"重建时用 {FALLBACK_MODE}，请人工核对）")


def _render_restore(path_str: str, mode_token: str, size: int,
                    resolved_path: str | None = None) -> str:
    """单遍渲染：路径里若含 `@@MODE@@` 之类字面量，也不会被后续替换改坏。"""
    values = {"PATH": shlex.quote(path_str), "MODE": mode_token, "SIZE": str(size),
              "RESOLVED": shlex.quote(resolved_path or "")}
    return _RENDER_RE.sub(lambda m: values[m.group(1)], _RESTORE_TEMPLATE)


def _parse_restore_info(stdout: bytes) -> dict:
    """解析恢复脚本的后验行：`opsx-restored had=1 size=1234 mode=640`。

    取**最后一条**匹配：登录 shell 的 MOTD/rc 文件里若恰好有同样前缀的输出，
    第一条会把真实后验顶掉。
    """
    found = None
    for line in stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("opsx-restored "):
            continue
        # 不能按空白 split：BSD `wc` 会输出 "      10"，`size=      10` 会被切成
        # "size=" 与 "10" 两段，于是 size 解析成空、校验被静默跳过。
        fields = {}
        for part in re.split(r"\s+(?=[a-z]+=)", line):
            if "=" in part:
                key, _, value = part.partition("=")
                fields[key.strip()] = value.strip()
        had = fields.get("had")
        # BSD/macOS 的 wc 会对数字左填充空格（`wc -c` → "      12"）：不 strip 就会
        # 把后验结果当成"没有大小"，于是校验被静默跳过。脚本侧也做了 tr -d ' '。
        size = (fields.get("size") or "").strip()
        mode = (fields.get("mode") or "").strip()
        found = {"had": had == "1" if had in ("0", "1") else None,
                 "size": int(size) if size.isdigit() else None,
                 "mode": mode if re.fullmatch(r"[0-7]{1,4}", mode) else None}
    return found or {"had": None, "size": None, "mode": None}


def _parse_restore(stdout: bytes) -> tuple[bool | None, int | None]:
    info = _parse_restore_info(stdout)
    return info["had"], info["size"]


def rollback(snapshot_id: str, confirm: bool = False) -> str:
    """回滚：远程快照推回目标机，本机快照交给 core.rollback。

    回滚是写操作：入口展示 rollback_plan 并由人确认后再传 confirm=True。
    每一步都做后验（写入字节数必须等于快照字节数），失败时报告"恢复了几个文件"。
    """
    import execution
    d, meta = core.load_snapshot(snapshot_id)
    target = meta.get("target")
    if not target:
        return core.rollback(snapshot_id, confirm=confirm)
    file_items = [item for item in meta["items"] if item["type"] == "file"]
    if file_items and not confirm:
        raise core.OpsxError("回滚会覆盖目标机文件，属于写操作：请先预览并由人确认"
                            "（CLI: opsx rollback <id>）")
    env = _snapshot_target_env(meta)  # 目标校验先做：校验失败不必占锁
    if file_items:
        code, message = core._check_pending_unknown(f"rollback {snapshot_id}", target, env)
        if code:
            raise core.OpsxError(message)
    rec = execution.begin("rollback", f"rollback {snapshot_id}", target, env,
                          snapshot_id=snapshot_id, phase="backup")
    backup_sid = None
    report: list[str] = []
    restored = 0
    dispatched = False
    if not meta.get("machine"):
        # 快照时探测失败/旧快照：只能靠 targets.json 的环境比对，机器身份没校验过
        core._safe_audit("rollback-no-machine", snapshot_id=snapshot_id, target=target)
        report.append("注意：该快照未记录主机指纹，本次回滚跳过机器一致性校验"
                      "（请自行确认目标仍是同一台机器）")
    if any(not item.get("resolved_path") for item in file_items):
        report.append("注意：旧快照未记录文件真实路径，无法校验软链是否改向，请人工核对")
    try:
        with _fixed_target(target), core.target_lock(
                target, actor="rollback", execution_id=rec["id"]):
            env = _snapshot_target_env(meta)  # 等锁或确认期间注册表/实际机器可能已经变化。
            execution.update(rec["id"], env=env, host=env.get("host"))
            if file_items:
                code, message = core._check_pending_unknown(f"rollback {snapshot_id}", target, env)
                if code:
                    raise core.OpsxError(message)
            if file_items:
                resolved_paths = {item["src"]: _target_resolved_path(target, item["src"])
                                  for item in file_items}
                for item in file_items:
                    if item.get("resolved_path") and item["resolved_path"] != resolved_paths[item["src"]]:
                        raise RemoteError(f"远程文件真实路径已改变: {item['src']}，拒绝回滚")
                backup_sid = core._rollback_backup(
                    snapshot_id, target, file_items,
                    lambda src: _remote_read(target, resolved_paths[src]),
                    # 副本也记权限：之后回滚这份副本时要能还原同样的权限
                    mode_of=lambda src: _target_mode(target, resolved_paths[src]),
                    meta_extra={"env": env, "machine": meta.get("machine") or _target_machine(target)},
                    resolved_of=resolved_paths.__getitem__)
                execution.update(rec["id"], phase="restore")
            for item in meta["items"]:
                if item["type"] != "file":
                    out = (d / item["output"]).read_text(encoding="utf-8", errors="replace")
                    report.append(f"捕获状态 [{item.get('name') or item['cmd']}]:\n{out}")
                    continue
                payload = (d / item["dest"]).read_bytes()
                mode_arg, mode_note = _restore_mode(item)
                script = _render_restore(item["src"], mode_arg, len(payload), item.get("resolved_path"))
                try:
                    dispatched = True
                    r = _ssh(target, script, stdin=payload, timeout=IO_TIMEOUT)
                except subprocess.TimeoutExpired as e:
                    # mv 可能已经完成：超时同样"无法确认"，不能报成明确的失败
                    raise _UncertainChange(f"远程回滚超时: {item['src']}") from e
                if r.returncode == 255 or r.returncode < 0:
                    raise _UncertainChange(
                        f"远程回滚 SSH 会话异常({r.returncode}): {item['src']}，写入结果需核实")
                if r.returncode != 0:
                    raise RemoteError(
                        f"远程回滚失败({r.returncode}): {item['src']}: {_text(r)[:300]}")
                info = _parse_restore_info(r.stdout)
                if info["size"] is None or info["had"] is None or info["mode"] is None:
                    # 脚本退出 0 但没带回后验（输出被吞/MOTD 干扰/连接抖过）：
                    # 当成"无法确认"，绝不能宣告校验通过——那是静默丢内容的入口。
                    raise _UncertainChange(
                        f"远程回滚无法确认写入结果: {item['src']}（缺少后验输出）")
                if info["size"] != len(payload):
                    raise _UncertainChange(
                        f"远程回滚内容校验失败: {item['src']} 期望 {len(payload)} 字节，"
                        f"实际 {info['size']} 字节")
                if mode_arg and int(info["mode"], 8) != int(mode_arg, 8):
                    raise _UncertainChange(
                        f"远程回滚权限校验失败: {item['src']}，期望 {mode_arg}，"
                        f"实际 {info['mode']}，文件可能已经写入")
                perm = info["mode"] or "?"
                line = f"已恢复远程文件: {target}:{item['src']}（权限 {perm}）"
                if mode_note:
                    line += mode_note
                if info["had"] is False:
                    # 已删除文件的恢复策略：按快照内容与权限重建；属主/属组需要 root，
                    # 不假装能还原，明确告诉人去看一眼
                    line += "；原文件此前已被删除，本次为重建"
                    if item.get("owner"):
                        line += (f"，属主/属组未还原（快照为 {item['owner']}，"
                                 "当前为 SSH 登录用户，需要时人工 chown）")
                report.append(line)
                restored += 1
            if backup_sid:
                report.append(f"回滚前副本: {backup_sid}"
                              f"（如需撤销本次回滚: opsx rollback {backup_sid}）")
            execution.finish(rec["id"], "succeeded", output="\n".join(report))
    except core.TargetBusy as e:
        core._safe_audit("rollback-abort", snapshot_id=snapshot_id, target=target,
                   reason=str(e))
        execution.finish(rec["id"], "aborted", error=str(e), phase="lock")
        raise
    except _UncertainChange as e:
        core._safe_audit("rollback-unknown", snapshot_id=snapshot_id, target=target,
                         backup_snapshot_id=backup_sid, error=str(e),
                         execution_id=rec["id"])
        error = f"{e}（已恢复 {restored}/{len(file_items)} 个文件，结果需核实）"
        execution.finish(rec["id"], "unknown", error=error)
        if backup_sid:
            raise _UncertainChange(f"{error}（回滚前副本: {backup_sid}）") from e
        raise
    except (core.OpsxError, RemoteError) as e:
        core._safe_audit("rollback-failed", snapshot_id=snapshot_id, target=target,
                         backup_snapshot_id=backup_sid, error=str(e), execution_id=rec["id"])
        error = f"{e}（已恢复 {restored}/{len(file_items)} 个文件）"
        execution.finish(rec["id"], "failed" if dispatched else "aborted", error=error)
        if backup_sid:
            raise RemoteError(f"{error}（回滚前副本: {backup_sid}）") from e
        raise
    except BaseException:
        execution.finish(
            rec["id"], "unknown" if dispatched else "aborted",
            error=f"回滚被中断，已恢复 {restored}/{len(file_items)} 个文件，需核实")
        raise
    core._safe_audit("rollback", snapshot_id=snapshot_id, target=target,
               backup_snapshot_id=backup_sid, execution_id=rec["id"],
               files=[i["src"] for i in file_items])
    execution.prune()
    return "\n".join(report)


def describe() -> list[str]:
    """注册表的人类可读摘要（opsx targets / ops_list 共用）。"""
    try:
        targets = load_targets()
    except RemoteError as e:
        return [f"[opsx 错误] {e}"]  # 注册表坏了，不应连带隐藏快照/审批戳
    out = []
    for name in sorted(targets):
        if name == "_local":
            continue  # 本机检查配置，不是 SSH 目标
        t = targets[name] if isinstance(targets[name], dict) else {}
        user = f"{t['user']}@" if t.get("user") else ""
        port = f":{t['port']}" if t.get("port") else ""
        line = f"{name}  {user}{t.get('host', '?')}{port}"
        tags = t.get("tags")
        if isinstance(tags, list) and tags:
            line += "  [" + ",".join(str(x) for x in tags) + "]"
        services = t.get("services")
        if isinstance(services, list) and services:
            line += f"  服务 {len(services)} 项"
        if t.get("enabled") is False:
            line += "  （enabled=false，巡检跳过）"
        out.append(line)
    return out
