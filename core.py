"""opsx 核心库：纯函数，CLI 与未来 MCP server 共用。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("OPSX_STATE_DIR", str(Path.home() / ".ops-agent")))
REPO_DIR = Path(__file__).resolve().parent

_RANK = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}


class OpsxError(Exception):
    pass


def load_config() -> dict:
    cfg = {"readonly_patterns": [], "risk_rules": {"R1": [], "R2": [], "R3": []}}
    _parse_config(REPO_DIR / "config.yaml", cfg)
    override = STATE_DIR / "config.override.yaml"
    if override.exists():
        _parse_config(override, cfg)
    return cfg


def _parse_config(path: Path, cfg: dict) -> None:
    """极简行解析：顶层 `key:` 开段，`- value` 为条目，# 为注释。值为原始正则。"""
    section = None
    for raw in Path(path).read_text().splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith(":") and not s.startswith("- "):
            section = s[:-1]
            continue
        if s.startswith("- ") and section:
            item = s[2:].strip()
            if section == "readonly_patterns":
                cfg["readonly_patterns"].append(item)
            elif section in ("R1", "R2", "R3"):
                cfg["risk_rules"][section].append(item)


def classify(cmd: str) -> tuple[str, str | None]:
    """复合命令分段取最高风险级。返回 (级别, 命中的最高风险分段)。"""
    config = load_config()
    level, matched = "R0", None
    for seg in re.split(r"&&|\|\||[;|]", cmd):
        seg = seg.strip()
        if not seg:
            continue
        seg_level = _classify_single(seg, config)
        if _RANK[seg_level] > _RANK[level]:
            level, matched = seg_level, seg
    return level, matched


def _classify_single(seg: str, config: dict) -> str:
    for pat in config["readonly_patterns"]:
        if re.search(pat, seg):
            return "R0"
    for pat in config["risk_rules"]["R3"]:
        if re.search(pat, seg):
            return "R3"
    for pat in config["risk_rules"]["R2"]:
        if re.search(pat, seg):
            return "R2"
    for pat in config["risk_rules"]["R1"]:
        if re.search(pat, seg):
            return "R1"
    return "R2"  # 未知命令安全方向默认


def audit(event: str, **fields) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with open(STATE_DIR / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def normalize(cmd: str) -> str:
    return " ".join(cmd.split())


def cmd_hash(cmd: str) -> str:
    return hashlib.sha256(normalize(cmd).encode()).hexdigest()


def approve(cmd: str, ttl_seconds: int = 900, force: bool = False) -> Path:
    level, _ = classify(cmd)
    if level == "R0":
        raise OpsxError("只读命令无需审批戳")
    if level == "R3" and not force:
        raise OpsxError("R3 命令需 --force 显式确认（不可逆操作）")
    stamp = {
        "cmd_hash": cmd_hash(cmd),
        "cmd": cmd,
        "created": time.time(),
        "ttl": ttl_seconds,
        "force": force,
        "level": level,
    }
    p = STATE_DIR / "approvals" / (stamp["cmd_hash"] + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding="utf-8")
    audit("approve", cmd=cmd, level=level, ttl=ttl_seconds, force=force)
    return p


def check_stamp(cmd: str) -> bool:
    p = STATE_DIR / "approvals" / (cmd_hash(cmd) + ".json")
    if not p.exists():
        return False
    s = json.loads(p.read_text(encoding="utf-8"))
    return time.time() < s["created"] + s["ttl"]


def check(cmd: str) -> tuple[int, str]:
    level, matched = classify(cmd)
    if level == "R0":
        audit("check", cmd=cmd, level=level, result="allow")
        return 0, "只读命令，放行"
    if check_stamp(cmd):
        audit("check", cmd=cmd, level=level, result="allow-stamp")
        return 0, "有效审批戳，放行"
    audit("check", cmd=cmd, level=level, result="deny")
    hit = f"（命中分段：{matched}）" if matched else ""
    return 2, (
        f"[opsx] 拒绝执行：该命令判定为 {level}{hit}。"
        "请先向用户提交变更单（风险说明+回滚计划），经确认后使用 opsx exec 执行。"
    )


def _new_snapshot_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _begin_snapshot() -> tuple[str, Path]:
    sid = _new_snapshot_id()
    d = STATE_DIR / "snapshots" / sid
    d.mkdir(parents=True, exist_ok=True)
    return sid, d


def _snapshot_file_into(d: Path, path_str: str, items: list) -> None:
    src = Path(path_str)
    if not src.is_file():
        raise OpsxError(f"快照源不是文件: {path_str}")
    dest = d / "files" / str(src.resolve()).lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    items.append({
        "type": "file",
        "src": str(src.resolve()),
        "dest": str(dest.relative_to(d)),
        "mode": stat.S_IMODE(src.stat().st_mode),
    })


def _snapshot_cmd_into(d: Path, cmd: str, name: str, items: list) -> None:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise OpsxError(f"状态抓取命令失败({r.returncode}): {cmd}\n{r.stderr[:500]}")
    fname = f"cmd-{(name or 'output').replace('/', '_')}.txt"
    (d / fname).write_text(r.stdout, encoding="utf-8")
    items.append({"type": "cmd", "cmd": cmd, "name": name, "output": fname})


def _write_meta(d: Path, sid: str, items: list) -> None:
    meta = {"id": sid, "created": datetime.now(timezone.utc).isoformat(), "items": items}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot_file(path_str: str) -> str:
    sid, d = _begin_snapshot()
    items: list = []
    _snapshot_file_into(d, path_str, items)
    _write_meta(d, sid, items)
    audit("snapshot", id=sid, kind="file", src=path_str)
    return sid


def snapshot_cmd(cmd: str, name: str = "") -> str:
    sid, d = _begin_snapshot()
    items: list = []
    _snapshot_cmd_into(d, cmd, name, items)
    _write_meta(d, sid, items)
    audit("snapshot", id=sid, kind="cmd", cmd=cmd)
    return sid


def rollback(snapshot_id: str) -> str:
    d = STATE_DIR / "snapshots" / snapshot_id
    if not d.exists():
        raise OpsxError(f"快照不存在: {snapshot_id}")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    report = []
    for item in meta["items"]:
        if item["type"] == "file":
            shutil.copy2(d / item["dest"], item["src"])
            os.chmod(item["src"], item["mode"])
            report.append(f"已恢复文件: {item['src']}")
        else:
            out = (d / item["output"]).read_text(encoding="utf-8")
            report.append(f"捕获状态 [{item.get('name') or item['cmd']}]:\n{out}")
    audit("rollback", snapshot_id=snapshot_id)
    return "\n".join(report)


def prune_snapshots(keep: int = 200) -> None:
    d = STATE_DIR / "snapshots"
    if not d.exists():
        return
    ids = sorted(p for p in d.iterdir() if p.is_dir())
    for p in ids[:-keep]:
        shutil.rmtree(p)
        audit("prune", snapshot_id=p.name)


def exec_change(
    cmd: str,
    snapshot_files: list[str] | None = None,
    snapshot_cmds: list[tuple[str, str]] | None = None,
) -> int:
    snapshot_files = snapshot_files or []
    snapshot_cmds = snapshot_cmds or []
    code, msg = check(cmd)
    if code != 0:
        print(msg, file=sys.stderr)
        return code
    sid = None
    if snapshot_files or snapshot_cmds:
        sid, d = _begin_snapshot()
        items: list = []
        try:
            for f in snapshot_files:
                _snapshot_file_into(d, f, items)
            for c, name in snapshot_cmds:
                _snapshot_cmd_into(d, c, name, items)
        except OpsxError as e:
            shutil.rmtree(d, ignore_errors=True)
            print(f"[opsx] 快照失败，已中止执行: {e}", file=sys.stderr)
            audit("exec-abort", cmd=cmd, reason=str(e))
            return 3
        _write_meta(d, sid, items)
    r = subprocess.run(cmd, shell=True)
    audit("exec", cmd=cmd, snapshot_id=sid, exit_code=r.returncode)
    prune_snapshots()
    return r.returncode
