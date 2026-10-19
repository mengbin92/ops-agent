"""会话持久化：~/.ops-agent/sessions/<id>.jsonl，格式 v1。

首行元信息 {"format": 1, ...}，之后每行一条消息（与 LLM messages 结构一致）。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

FORMAT_VERSION = 1
SESSION_KEEP = 300  # 会话文件保留上限，超出按修改时间删最旧


def _prune_sessions() -> None:
    d = core.STATE_DIR / "sessions"
    if not d.exists():
        return
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    for p in files[:-SESSION_KEEP]:
        p.unlink(missing_ok=True)


class Session:
    def __init__(self, session_id: str | None = None):
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.path = core.STATE_DIR / "sessions" / f"{self.id}.jsonl"
        self.messages: list[dict] = []
        if self.path.exists():
            self._load()
        elif session_id is None:
            _prune_sessions()  # 新会话时顺带修剪

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("format"):
                continue
            self.messages.append(rec)

    def append(self, message: dict) -> None:
        self.messages.append(message)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps(
                {"format": FORMAT_VERSION, "id": self.id,
                 "created": time.time()}, ensure_ascii=False) + "\n", encoding="utf-8")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")


def list_sessions() -> list[dict]:
    """按修改时间倒序列出会话：{id, mtime, turns, preview}。preview 取首条用户消息。"""
    d = core.STATE_DIR / "sessions"
    if not d.exists():
        return []
    out = []
    for p in d.glob("*.jsonl"):
        preview, turns = "", 0
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("format"):
                    continue
                if rec.get("role") == "user":
                    turns += 1
                    if not preview:
                        preview = str(rec.get("content", ""))[:60]
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": p.stem, "mtime": p.stat().st_mtime,
                    "turns": turns, "preview": preview})
    return sorted(out, key=lambda s: s["mtime"], reverse=True)
