"""Offline, private state backups; stdlib only. Never use on live writers.

The archive preserves raw bytes, including credentials and snapshot originals.
Restores create a new directory and invalidate authorizations and live-process state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import time
import uuid

import core

FORMAT_VERSION = 1
MANIFEST_LIMIT = 32 * 1024 * 1024
MAX_ENTRIES = 100000
CHUNK = 1024 * 1024
TAR_READ_LIMIT = 64 * 1024
QUIESCENCE = "须先停止全部调度、Agent、MCP/CLI 及其他状态写入者，再显式传入 --quiesced；不是在线事务快照"


class StateBackupError(core.OpsxError):
    pass


def _absolute(path) -> Path:
    # macOS exposes /var, /tmp and /etc through fixed /private aliases.
    # Normalize only these OS-owned aliases, never arbitrary user symlinks.
    result = Path(os.fspath(path))
    if not result.is_absolute():
        result = Path.cwd() / result
    if sys.platform == "darwin":
        for alias in ("/var", "/tmp", "/etc"):
            base = Path(alias)
            canonical = Path("/private") / base.name
            if (result == base or base in result.parents) and base.is_symlink() \
                    and base.resolve() == canonical:
                result = canonical / result.relative_to(base)
                break
    # Check the original ancestors before abspath removes '..'. Otherwise an
    # alias/../name input silently bypasses our refusal of user-created links.
    _no_symlink_path(result)
    return Path(os.path.abspath(result))


def _no_symlink_path(path: Path) -> None:
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise StateBackupError(f"拒绝软链接路径: {part}")
        if part != path and not part.is_dir():
            raise StateBackupError(f"父目录不存在或不是目录: {part}")


def _root(path, *, private=False) -> Path:
    path = _absolute(path)
    _no_symlink_path(path)
    if not path.is_dir():
        raise StateBackupError(f"目录不存在: {path}")
    if private and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise StateBackupError(f"备份目录须私有（chmod 700）: {path}")
    return path


def _new_root(path) -> Path:
    path = _absolute(path)
    _no_symlink_path(path)
    if not path.parent.is_dir():
        raise StateBackupError(f"父目录不存在: {path.parent}")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise StateBackupError(f"拒绝覆盖已有目录或文件: {path}") from exc
    try:
        os.chmod(path, 0o700)
    except BaseException:
        # mkdir succeeded but initialization did not. Remove only the empty
        # directory we just created, without following a replacement link.
        try:
            path.rmdir()
        except OSError:
            pass
        raise
    return path


def _remove_created_root(path: Path, identity: tuple) -> None:
    """Clean an incomplete output only while it is still our created directory."""
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity:
        shutil.rmtree(path)


def _relative(name) -> str:
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/"))
            or str(PurePosixPath(name)) != name):
        raise StateBackupError("归档包含不安全路径")
    return name


def _signature(st) -> tuple:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _inventory(root: Path) -> dict:
    found = {}
    discovered = 0
    def visit(directory, prefix=""):
        nonlocal discovered
        children = []
        for child in directory.iterdir():
            discovered += 1
            if discovered > MAX_ENTRIES:
                raise StateBackupError("状态条目过多")
            children.append(child)
        for child in sorted(children):
            name = _relative(prefix + child.name)
            st = child.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise StateBackupError(f"状态含软链接，拒绝备份: {name}")
            kind = "directory" if stat.S_ISDIR(st.st_mode) else "file"
            if kind == "file" and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1):
                raise StateBackupError(f"拒绝特殊文件或硬链接: {name}")
            found[name] = {"type": kind, "mode": stat.S_IMODE(st.st_mode),
                           "signature": _signature(st)}
            if kind == "directory":
                visit(child, name + "/")
    visit(root)
    return found


def _open_file(root: Path, name: str):
    """Resolve every ancestor using O_NOFOLLOW; do not follow swapped directory links."""
    parts = _relative(name).split("/")
    # A damaged/untrusted manifest or archive may itself be a FIFO. Opening it
    # must not block before fstat can reject its type.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        result = os.open(parts[-1], flags, dir_fd=fd)
    except OSError as exc:
        raise StateBackupError(f"文件无法安全打开（拒绝链接或路径异常）: {name}") from exc
    finally:
        os.close(fd)
    try:
        st = os.fstat(result)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise StateBackupError(f"拒绝特殊文件或硬链接: {name}")
        return os.fdopen(result, "rb")
    except BaseException:
        os.close(result)
        raise


def _write_bytes(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _json_bytes(value) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        # A parsed JSON escape can contain an unpaired surrogate. Do not expose
        # its surrounding (possibly secret) value in a codec traceback.
        raise StateBackupError("状态 JSON 含无法编码的 Unicode 字符") from exc


def _hash_file(handle) -> str:
    digest = hashlib.sha256()
    while data := handle.read(CHUNK):
        digest.update(data)
    return digest.hexdigest()


def backup_state(source, destination, *, quiesced=False) -> dict:
    """Create a new 0700 directory with state.tar + manifest.json (0600).

    quiesced is the operator's explicit assertion, not an acquired global lock.
    We detect ordinary concurrent mutations, but cannot make live writers atomic.
    """
    if quiesced is not True:
        raise StateBackupError(QUIESCENCE)
    source = _root(source)
    destination = _absolute(destination)
    if destination == source or source in destination.parents:
        raise StateBackupError("备份目录不能位于源状态目录内")
    before = _inventory(source)
    output = _new_root(destination)
    created = output.stat()
    identity = (created.st_dev, created.st_ino)
    try:
        entries = []
        fd = os.open(output / "state.tar", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw:
            os.fchmod(raw.fileno(), 0o600)
            with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, item in before.items():
                    info = tarfile.TarInfo(name)
                    info.mode = 0o700 if item["type"] == "directory" else 0o600
                    info.mtime = 0
                    entry = {"path": name, "type": item["type"], "mode": item["mode"]}
                    if item["type"] == "directory":
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    else:
                        with _open_file(source, name) as handle:
                            if _signature(os.fstat(handle.fileno())) != item["signature"]:
                                raise StateBackupError("备份期间状态变化；停止全部写入者后重试")
                            entry["size"] = info.size = os.fstat(handle.fileno()).st_size
                            entry["sha256"] = _hash_file(handle)
                            handle.seek(0)
                            archive.addfile(info, handle)
                            if _signature(os.fstat(handle.fileno())) != item["signature"]:
                                raise StateBackupError("备份期间状态变化；停止全部写入者后重试")
                    entries.append(entry)
            raw.flush()
            os.fsync(raw.fileno())
        if _inventory(source) != before:
            raise StateBackupError("备份期间状态变化；停止全部写入者后重试")
        with _open_file(output, "state.tar") as handle:
            archive_hash = _hash_file(handle)
        manifest = {"format": FORMAT_VERSION, "created_unix": time.time(),
                    "consistency": "offline-quiesced-operator-assertion",
                    "archive_sha256": archive_hash, "entries": entries}
        _write_bytes(output / "manifest.json", _json_bytes(manifest))
        verified = verify_backup(output)
        return {"backup": str(output), **verified}
    except BaseException:
        _remove_created_root(output, identity)
        raise


def _manifest_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateBackupError("manifest 存在重复 JSON 字段")
        result[key] = value
    return result


def _manifest_int(value):
    # Python 3.9 has no built-in integer conversion limit. Bound attacker-chosen
    # numeric tokens before conversion, independently of the overall byte cap.
    if len(value) > 4096:
        raise StateBackupError("manifest 数值过大")
    return int(value)


def _manifest(root: Path) -> dict:
    expected = {"manifest.json", "state.tar"}
    found = set()
    for path in root.iterdir():
        if path.name not in expected:
            raise StateBackupError("备份目录须只含 manifest.json 与 state.tar")
        found.add(path.name)
    if found != expected:
        raise StateBackupError("备份目录须只含 manifest.json 与 state.tar")
    with _open_file(root, "manifest.json") as handle:
        if os.fstat(handle.fileno()).st_mode & 0o077:
            raise StateBackupError("备份 manifest 须为 0600")
        data = handle.read(MANIFEST_LIMIT + 1)
        if len(data) > MANIFEST_LIMIT:
            raise StateBackupError("manifest 过大")
    try:
        manifest = json.loads(data, object_pairs_hook=_manifest_object, parse_int=_manifest_int)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise StateBackupError("备份 manifest 损坏") from exc
    if (not isinstance(manifest, dict) or type(manifest.get("format")) is not int
            or manifest["format"] != FORMAT_VERSION
            or not isinstance(manifest.get("entries"), list)
            or len(manifest["entries"]) > MAX_ENTRIES
            or not isinstance(manifest.get("archive_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["archive_sha256"])):
        raise StateBackupError("不支持或损坏的备份 manifest")
    names = {}
    for item in manifest["entries"]:
        if not isinstance(item, dict):
            raise StateBackupError("manifest 条目格式错误")
        name = _relative(item.get("path"))
        if (name in names or item.get("type") not in ("file", "directory")
                or type(item.get("mode")) is not int or not 0 <= item["mode"] <= 0o7777):
            raise StateBackupError("manifest 存在重复条目或无效类型/权限")
        if item["type"] == "file" and (type(item.get("size")) is not int
                or item["size"] < 0 or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise StateBackupError("manifest 文件大小或摘要无效")
        names[name] = item
    for name in names:
        # Every entry is checked, so checking each immediate parent also checks
        # the complete tree without quadratic work for deeply nested paths.
        parent = str(PurePosixPath(name).parent)
        if parent != "." and names.get(parent, {}).get("type") != "directory":
            raise StateBackupError("manifest 缺少父目录或文件目录冲突")
    return manifest


class _TarReader:
    """Bound tar metadata reads, which tarfile otherwise sizes from raw headers."""
    def __init__(self, raw):
        self.raw = raw

    def read(self, size):
        if size < 0 or size > TAR_READ_LIMIT:
            raise StateBackupError("归档扩展头过大或读取大小无效")
        return self.raw.read(size)

    def seek(self, *args):
        return self.raw.seek(*args)

    def tell(self):
        return self.raw.tell()


def _walk_archive(root: Path, manifest: dict, *, destination=None, skip_write=frozenset()) -> None:
    expected = {item["path"]: item for item in manifest["entries"]}
    seen = set()
    with _open_file(root, "state.tar") as raw:
        if os.fstat(raw.fileno()).st_mode & 0o077:
            raise StateBackupError("备份归档须为 0600")
        if _hash_file(raw) != manifest["archive_sha256"]:
            raise StateBackupError("state.tar SHA-256 校验失败")
        raw.seek(0)
        try:
            with tarfile.open(fileobj=_TarReader(raw), mode="r:") as archive:
                for member in archive:
                    # tarfile removes the trailing slash from ordinary directory names.
                    name = _relative(member.name)
                    item = expected.get(name)
                    if (name in seen or item is None or member.linkname
                            or not (member.isdir() or member.isreg())
                            or member.pax_headers.get("linkpath") is not None
                            or ("directory" if member.isdir() else "file") != item["type"]):
                        raise StateBackupError("归档含重复、额外、链接或特殊条目")
                    seen.add(name)
                    if member.isdir():
                        if member.size != 0:
                            raise StateBackupError("归档目录携带数据")
                        if destination:
                            (destination / name).mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    if member.size != item["size"] or member.sparse is not None:
                        raise StateBackupError("归档文件大小不符或包含稀疏文件")
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise StateBackupError("归档缺少文件内容")
                    writer = None
                    try:
                        if destination and name not in skip_write:
                            path = destination / name
                            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                            writer = os.fdopen(fd, "wb")
                            os.fchmod(writer.fileno(), 0o600)
                        digest, count = hashlib.sha256(), 0
                        while data := handle.read(min(CHUNK, TAR_READ_LIMIT)):
                            count += len(data)
                            digest.update(data)
                            if writer:
                                writer.write(data)
                        if count != item["size"] or digest.hexdigest() != item["sha256"]:
                            raise StateBackupError(f"归档文件 SHA-256 校验失败: {name}")
                    finally:
                        handle.close()
                        if writer:
                            writer.close()
                # tarfile normally stops at the first zero header, hiding any
                # concatenated archive or payload. Our format permits only the
                # standard zero trailer after the final declared member.
                raw.seek(archive.offset)
                trailer_size = 0
                while data := raw.read(CHUNK):
                    trailer_size += len(data)
                    if any(data):
                        raise StateBackupError("归档结束标记后包含额外数据")
                if trailer_size < 1024 or trailer_size % 512:
                    raise StateBackupError("无效或截断的 tar 结束标记")
        except (tarfile.TarError, EOFError, OverflowError, ValueError, RecursionError) as exc:
            raise StateBackupError("无效或截断的 tar 归档") from exc
    if seen != set(expected):
        raise StateBackupError("归档缺少 manifest 中的条目")


def verify_backup(archive) -> dict:
    """Verify hashes and safe paths/types without writing a restored state."""
    root = _root(archive, private=True)
    manifest = _manifest(root)
    _walk_archive(root, manifest)
    return {"format": FORMAT_VERSION, "entries": len(manifest["entries"]),
            "files": sum(e["type"] == "file" for e in manifest["entries"]),
            "archive_sha256": manifest["archive_sha256"], "verified": True}


RECOVERY_JSON_LIMIT = 32 * 1024 * 1024


def _load_json(path: Path):
    # The runtime must not interpret ambiguous keys or non-JSON numbers after
    # recovery has rewritten a safety field. Keep the raw archive for repair.
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def finite_float(value):
        import math
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    def bounded_int(value):
        # Python 3.9 has no automatic integer conversion limit. Match the
        # manifest's bound before converting an attacker-chosen numeric token.
        if len(value) > 4096:
            raise ValueError("JSON integer token too large")
        return int(value)

    def invalid_constant(_value):
        raise ValueError("non-JSON constant")

    try:
        # Bound bytes before UTF-8 decoding or allocating the JSON object graph.
        # This cap applies only to metadata needed for recovery safety; raw
        # snapshots, audit logs and every original archive byte are preserved.
        with path.open("rb") as handle:
            data = handle.read(RECOVERY_JSON_LIMIT + 1)
        if len(data) > RECOVERY_JSON_LIMIT:
            raise ValueError("recovery JSON too large")
        return json.loads(data.decode("utf-8"), object_pairs_hook=object_pairs,
                          parse_int=bounded_int, parse_float=finite_float,
                          parse_constant=invalid_constant)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise StateBackupError(f"恢复安全处理所需的 JSON 无效: {path.name}") from exc


def _replace_json(path: Path, value) -> None:
    # Only called inside a newly created private state directory.
    temporary = path.with_name(f".{path.name}.recovery-{uuid.uuid4().hex}")
    try:
        _write_bytes(temporary, _json_bytes(value))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_recovery(root: Path, archive_hash: str) -> dict:
    # These readers do not write or use STATE_DIR. Reuse their record checks so
    # restored running records cannot disappear from execution/run queries.
    import execution
    from agent import goal, notify, schedule

    now = time.time()
    iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    result = {"restored_unix": now, "archive_sha256": archive_hash,
              "approval_policy": "all-quarantined-require-new-approval",
              "running_executions": 0, "running_schedules": 0, "paused_schedules": 0,
              "paused_goals": 0, "notification_disabled": False,
              "approval_quarantine": None, "previous_recovery": None}
    for name in ("approvals", "executions", "runs", "notifications", "snapshots", "sessions"):
        directory = root / name
        if directory.exists() and not directory.is_dir():
            raise StateBackupError(f"{name} 须为目录")
    quarantine = None
    def quarantine_dir():
        nonlocal quarantine
        if quarantine is None:
            quarantine = root / ("recovery-quarantine-" + uuid.uuid4().hex)
            quarantine.mkdir(mode=0o700)
        return quarantine

    approvals = root / "approvals"
    if approvals.exists():
        quarantine = quarantine_dir()
        approvals.rename(quarantine / "approvals")
        approvals.mkdir(mode=0o700)
        result["approval_quarantine"] = quarantine.name + "/approvals"
    for directory, status, key in (("executions", "unknown", "running_executions"),
                                   ("runs", "interrupted", "running_schedules")):
        for path in sorted((root / directory).glob("*.json")):
            record = _load_json(path)
            reader = execution._read if directory == "executions" else schedule._read_run
            try:
                valid = isinstance(record, dict) and reader(path) is not None
            except (ValueError, TypeError, OverflowError):
                valid = False
            if not valid:
                raise StateBackupError(f"{directory} 记录无法被运行时安全加载: {path.name}")
            if record.get("status") == "running":
                record.update(status=status, pid=None, finished=iso, finished_ts=now,
                              error="从离线备份恢复：原执行结果不可确认，须人工核实")
                if directory == "executions":
                    record["resolved"] = None
                _replace_json(path, record)
                result[key] += 1
    path = root / "schedules.json"
    if path.exists():
        schedules = _load_json(path)
        if (not isinstance(schedules, dict) or not isinstance(schedules.get("items"), list)
                or any(not isinstance(item, dict)
                       or not isinstance(item.get("id"), str)
                       or not re.fullmatch(r"[A-Za-z0-9._-]+", item["id"])
                       or not isinstance(item.get("prompt"), str) or not item["prompt"].strip()
                       or not isinstance(item.get("enabled", True), bool)
                       for item in schedules["items"])):
            raise StateBackupError("schedules.json 格式无效")
        ids = [item["id"] for item in schedules["items"]]
        if len(set(ids)) != len(ids):
            raise StateBackupError("schedules.json 存在重复任务 id")
        for item in schedules["items"]:
            if item.get("enabled", True):
                result["paused_schedules"] += 1
            item["enabled"] = False
            if item.get("last_status") == "running":
                item["last_status"] = "interrupted"
        # Old host's heartbeat must not make a recovered daemon appear healthy.
        schedules["meta"] = {"recovered_unix": now}
        _replace_json(path, schedules)
    path = root / "notify.json"
    if path.exists():
        config = _load_json(path)
        try:
            notify._validate_config(config)
        except (notify.NotifyError, ValueError, TypeError, OverflowError) as exc:
            # Validation errors can contain raw configuration values. Do not
            # include them in the public restore failure message.
            raise StateBackupError("notify.json 格式无效") from exc
        config["enabled"] = False
        _replace_json(path, config)
        result["notification_disabled"] = True
    path = root / "goals.json"
    if path.exists():
        goals = _load_json(path)
        if (not isinstance(goals, dict) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str) or not item["id"]
                or not isinstance(item.get("objective"), str) or not item["objective"].strip()
                or item.get("status") not in ("active", "paused", "completed", "blocked")
                or type(item.get("max_rounds")) is not int
                or not 1 <= item["max_rounds"] <= goal.MAX_ROUNDS
                or type(item.get("rounds_used")) is not int
                or not 0 <= item["rounds_used"] <= item["max_rounds"]
                for item in goals.values())):
            raise StateBackupError("goals.json 格式无效")
        for item in goals.values():
            if item["status"] == "active":
                item.update(status="paused", updated=now,
                            reason="从离线备份恢复：须人工核对后显式恢复目标")
                result["paused_goals"] += 1
        if result["paused_goals"]:
            _replace_json(path, goals)
    marker = root / "recovery.json"
    if marker.exists():
        if not marker.is_file():
            raise StateBackupError("recovery.json 须为文件")
        previous = quarantine_dir() / "recovery.json"
        marker.rename(previous)
        result["previous_recovery"] = str(previous.relative_to(root))
    _write_bytes(marker, _json_bytes(result))
    # Keep the marker used since before extraction. Recovered directories can be
    # backed up and restored again, so its original archive bytes are not reused.
    with (root / ".ops-agent-recovered").open("wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(b"ops-agent recovered state v1\n")
    return result


def restore_state(archive, destination, *, quiesced=False) -> dict:
    """Restore into an absent directory, requiring review before activation.

    Snapshot bytes and meta modes are unchanged. All filesystem directories/files
    are 0700/0600. Approval stamps are quarantined, schedules/notifications paused,
    and running process records cannot be revived by PID reuse.
    """
    if quiesced is not True:
        raise StateBackupError(QUIESCENCE)
    root = _root(archive, private=True)
    destination = _absolute(destination)
    if destination == root or root in destination.parents:
        raise StateBackupError("恢复目录不能位于备份目录内")
    manifest = _manifest(root)
    _walk_archive(root, manifest)
    output = _new_root(destination)
    identity = (output.stat().st_dev, output.stat().st_ino)
    try:
        # Protect interrupted extraction too, including a crash or SIGKILL before
        # safety rewrites complete. Archive marker bytes are verified, not written.
        _write_bytes(output / ".ops-agent-recovered", b"ops-agent recovered state v1\n")
        _walk_archive(root, manifest, destination=output,
                      skip_write=frozenset({".ops-agent-recovered"}))
        result = _safe_recovery(output, manifest["archive_sha256"])
        for directory, subdirs, files in os.walk(output):
            os.chmod(directory, 0o700)
            for filename in files:
                os.chmod(Path(directory) / filename, 0o600)
        return {"destination": str(output), **result}
    except BaseException:
        _remove_created_root(output, identity)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线状态备份与安全恢复（含未脱敏秘密）")
    sub = parser.add_subparsers(dest="action", required=True)
    backup = sub.add_parser("backup", help="停写后备份到不存在的新目录")
    backup.add_argument("--source", default=str(core.STATE_DIR))
    backup.add_argument("--destination", required=True)
    backup.add_argument("--quiesced", action="store_true", help=QUIESCENCE)
    verify = sub.add_parser("verify", help="校验备份清单、哈希和安全路径")
    verify.add_argument("archive")
    restore = sub.add_parser("restore", help="恢复到不存在的新目录；隔离审批并暂停调度/通知")
    restore.add_argument("archive")
    restore.add_argument("--destination", required=True)
    restore.add_argument("--quiesced", action="store_true", help=QUIESCENCE)
    args = parser.parse_args(argv)
    try:
        if args.action == "backup":
            result = backup_state(args.source, args.destination, quiesced=args.quiesced)
        elif args.action == "restore":
            result = restore_state(args.archive, args.destination, quiesced=args.quiesced)
        else:
            result = verify_backup(args.archive)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (StateBackupError, OSError, ValueError) as exc:
        print(f"状态备份/恢复失败: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
