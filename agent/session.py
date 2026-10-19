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


class Session:
    def __init__(self, session_id: str | None = None):
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.path = core.STATE_DIR / "sessions" / f"{self.id}.jsonl"
        self.messages: list[dict] = []
        if self.path.exists():
            self._load()

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


def latest_session_id() -> str | None:
    d = core.STATE_DIR / "sessions"
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1].stem if files else None
