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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

SSH = "ssh"
CONNECT_TIMEOUT = 10   # 连接超时秒数（目标可用 connect_timeout 覆盖）
IO_TIMEOUT = 120       # 只读命令 / 快照抓取超时
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RemoteError(Exception):
    pass


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
    targets = load_targets()
    t = targets.get(name)
    if not isinstance(t, dict):
        known = ", ".join(sorted(n for n in targets if n != "_local")) or "无"
        raise RemoteError(f"未注册的 SSH 目标: {name}（已注册: {known}；"
                          f"在 {_targets_file()} 注册后才可用）")
    return t


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
    if not isinstance(recorded, dict):
        return current  # 旧快照没有指纹：按 target 名解析，兼容历史快照
    for key in ("host", "user", "port", "identity", "options"):
        if recorded.get(key) != current.get(key):
            raise RemoteError(
                f"快照记录的 {target} 与当前注册表不一致（{key}: {recorded.get(key)!r} → "
                f"{current.get(key)!r}）：拒绝回滚，请核对 targets.json 或重新快照")
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


def _pull_file(name: str, path_str: str, d: Path, items: list) -> None:
    """远程文件拉回本地存档。取不到就 abort——没有快照不允许执行。"""
    dest = _dest(d, path_str)
    try:
        r = _ssh(name, f"cat -- {shlex.quote(path_str)}", timeout=IO_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RemoteError(f"远程取快照超时: {path_str}") from e
    if r.returncode != 0:
        raise RemoteError(f"远程取快照失败({r.returncode}): {path_str}: {_text(r)[:300]}")
    dest.write_bytes(r.stdout)
    items.append({"type": "file", "remote": name, "src": path_str,
                  "dest": str(dest.relative_to(d))})


def _capture_cmd(name: str, cmd: str, label: str, d: Path, items: list) -> None:
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
            "created": datetime.now(timezone.utc).isoformat(), "items": items}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot_file(name: str, path_str: str) -> str:
    """远程文件快照：拉回本地存档（回滚时推回目标机）。"""
    sid, d = core.begin_snapshot()
    items: list = []
    try:
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
    code, msg = core.check(cmd, target=name)
    if code != 0:
        raise RemoteError(msg)
    sid, d = core.begin_snapshot()
    items: list = []
    try:
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
                timeout: int = core.EXEC_TIMEOUT) -> tuple[int, str | None, str]:
    """远程原子执行：本机守门 → 远程快照 → ssh 执行 → 本地审计。

    返回 (exit_code, snapshot_id, output)，与 core.exec_change 同形
    （exit=2 无审批戳、3 快照失败、4 超时）。
    """
    snapshot_files = snapshot_files or []
    snapshot_cmds = snapshot_cmds or []
    get_target(name)  # 先把"打错目标名"报出来，而不是误导用户去建审批戳
    code, msg = core.check(cmd, target=name)
    if code != 0:
        print(core.redact(msg), file=sys.stderr)
        return code, None, msg
    for c, _ in snapshot_cmds:  # 快照命令本身也过守门：否则可把写操作藏进抓取参数
        code, _ = core.check(c, target=name)
        if code != 0:
            core.audit("exec-abort", cmd=cmd, target=name, reason=f"快照命令未通过守门: {c}")
            output = core.redact(f"[opsx] 快照命令未通过守门，已中止执行: {c}")
            print(output, file=sys.stderr)
            return 2, None, output
    sid = None
    if snapshot_files or snapshot_cmds:
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
            output = f"[opsx] 远程快照失败，已中止执行: {e}"
            print(output, file=sys.stderr)
            core.audit("exec-abort", cmd=cmd, target=name, reason=str(e))
            return 3, None, output
    try:
        r = _ssh(name, cmd, timeout=timeout)
        output = core.redact(_text(r))
        exit_code = r.returncode
    except subprocess.TimeoutExpired:
        output = f"[opsx] 超时：远程命令超过 {timeout}s 未结束，SSH 已终止，远端状态需确认"
        exit_code = 4
    core.audit("exec", cmd=cmd, target=name, snapshot_id=sid, exit_code=exit_code,
               output=output[:core.EXEC_AUDIT_OUTPUT])
    core.prune_snapshots()
    return exit_code, sid, output


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


def rollback(snapshot_id: str, confirm: bool = False) -> str:
    """回滚：远程快照推回目标机，本机快照交给 core.rollback。

    回滚是写操作：入口展示 rollback_plan 并由人确认后再传 confirm=True。
    """
    d, meta = core.load_snapshot(snapshot_id)
    target = meta.get("target")
    if not target:
        return core.rollback(snapshot_id, confirm=confirm)
    file_items = [item for item in meta["items"] if item["type"] == "file"]
    if file_items and not confirm:
        raise core.OpsxError("回滚会覆盖目标机文件，属于写操作：请先预览并由人确认"
                            "（CLI: opsx rollback <id>）")
    env = _snapshot_target_env(meta)
    backup_sid = None
    report = []
    try:
        if file_items:
            backup_sid = core._rollback_backup(
                snapshot_id, target, file_items,
                lambda src: _remote_read(target, src), meta_extra={"env": env})
        for item in meta["items"]:
            if item["type"] != "file":
                out = (d / item["output"]).read_text(encoding="utf-8", errors="replace")
                report.append(f"捕获状态 [{item.get('name') or item['cmd']}]:\n{out}")
                continue
            # 覆盖写保留原 inode/权限；文件已被删除时重建（权限为默认值）。
            try:
                r = _ssh(target, f"cat > {shlex.quote(item['src'])}",
                         stdin=(d / item["dest"]).read_bytes(), timeout=IO_TIMEOUT)
            except subprocess.TimeoutExpired as e:
                raise RemoteError(f"远程回滚超时: {item['src']}") from e
            if r.returncode != 0:
                raise RemoteError(f"远程回滚失败({r.returncode}): {item['src']}: {_text(r)[:300]}")
            report.append(f"已恢复远程文件: {target}:{item['src']}")
    except (core.OpsxError, RemoteError) as e:
        core.audit("rollback-failed", snapshot_id=snapshot_id, target=target,
                   backup_snapshot_id=backup_sid, error=str(e))
        if backup_sid:
            raise RemoteError(f"{e}（回滚前副本: {backup_sid}）") from e
        raise
    if backup_sid:
        report.append(f"回滚前副本: {backup_sid}"
                      f"（如需撤销本次回滚: opsx rollback {backup_sid}）")
    core.audit("rollback", snapshot_id=snapshot_id, target=target,
               backup_snapshot_id=backup_sid, files=[i["src"] for i in file_items])
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
