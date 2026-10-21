"""会话持久化：~/.ops-agent/sessions/<id>.jsonl，格式 v1。

首行元信息 {"format": 1, ...}，之后每行一条消息（与 LLM messages 结构一致）。
"""
from __future__ import annotations

import json
import fcntl
import os
import re
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
    try:
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:  # 并发子代理同时建会话/修剪时 stat 可能失败，跳过本次
        return
    for p in files[:-SESSION_KEEP]:
        try:
            p.unlink()
        except OSError:
            pass


class Session:
    def __init__(self, session_id: str | None = None):
        if session_id is not None and (not isinstance(session_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)):
            raise ValueError("无效会话 id")
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.path = core.STATE_DIR / "sessions" / f"{self.id}.jsonl"
        self.messages: list[dict] = []
        if self.path.exists():
            self._load()
        elif session_id is None:
            _prune_sessions()  # 新会话时顺带修剪

    def _load(self) -> None:
        with self.path.open("r+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            os.fchmod(f.fileno(), 0o600)
            lines = f.readlines()
            offset = 0
            for i, line in enumerate(lines):
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    if i > 0 and i == len(lines) - 1 and not line.endswith(b"\n"):
                        f.truncate(offset)  # 崩溃留下的未完整写入尾行，保留之前的完整记录。
                        break
                    raise ValueError(f"会话文件损坏（第 {i + 1} 行）") from e
                if not isinstance(rec, dict):
                    raise ValueError(f"会话记录不是对象（第 {i + 1} 行）")
                if i == 0:
                    if type(rec.get("format")) is not int or rec["format"] != FORMAT_VERSION \
                            or rec.get("id") != self.id:
                        raise ValueError("不支持的会话格式或会话 id 不匹配")
                elif rec.get("role") not in {"user", "assistant", "tool", "system"}:
                    raise ValueError(f"会话消息缺少有效 role（第 {i + 1} 行）")
                else:
                    self.messages.append(rec)
                offset += len(line)
            else:
                if lines and not lines[-1].endswith(b"\n"):
                    f.seek(0, os.SEEK_END)
                    f.write(b"\n")
            if not lines:
                raise ValueError("会话文件为空")

    def append(self, message: dict) -> None:
        message = core.redact_obj(message)  # 会话落盘与后续模型请求都不带明文密钥
        record = json.dumps(message, ensure_ascii=False) + "\n"
        core.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(core.STATE_DIR, 0o700)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            os.fchmod(f.fileno(), 0o600)
            new_file = os.fstat(f.fileno()).st_size == 0
            header = json.dumps({"format": FORMAT_VERSION, "id": self.id,
                                 "created": time.time()}, ensure_ascii=False) + "\n"
            f.write((header if new_file else "") + record)
        self.messages.append(message)
        if new_file:
            _prune_sessions()


def list_sessions() -> list[dict]:
    """按修改时间倒序列出会话：{id, mtime, turns, preview}。preview 取首条用户消息。"""
    d = core.STATE_DIR / "sessions"
    if not d.exists():
        return []
    out = []
    for p in d.glob("*.jsonl"):
        preview, turns = "", 0
        try:
            for rec in Session(p.stem).messages:
                if rec.get("role") == "user":
                    turns += 1
                    if not preview:
                        preview = str(rec.get("content", ""))[:60]
        except (OSError, ValueError):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:  # 列目录与修剪并发：文件刚被删就跳过
            continue
        out.append({"id": p.stem, "mtime": mtime, "turns": turns, "preview": preview})
    return sorted(out, key=lambda s: s["mtime"], reverse=True)
