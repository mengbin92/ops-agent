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
