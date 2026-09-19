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
