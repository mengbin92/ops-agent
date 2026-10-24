"""长期记忆：跨会话持久化的运维事实（~/.ops-agent/memory.md）。

remember 工具每条追加一行（带日期，多行缩进为续行）；run_turn 每轮把文件尾部
MEMORY_LIMIT 字符注入 system prompt——确认过的主机事实/用户偏好下次会话直接可用，
不重复排查。纯文本追加：召回量超过注入上限前不需要向量检索（升级路径：按主机拆
memory/<host>.md 或 grep 召回）。
晋升路径：反复出现的排查模式由人确认后手动写进 skills/*.md，memory 保持流水。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from .schedule import _locked  # noqa: E402

MEMORY_LIMIT = 8000   # 注入 system prompt 的最大字符（取文件尾部，新事实优先）
ENTRY_LIMIT = 500     # 单条记忆长度上限：只记结论，不贴输出
HEADER = ("# ops-agent 长期记忆（remember 工具追加；过时条目手动删除）\n"
          "# 写法约定：主机: 属性 = 值；服务 X depends_on db-01；同类事实用同一词汇\n")


class MemoryError(Exception):
    pass


def path() -> Path:
    return core.STATE_DIR / "memory.md"


def add(text: str) -> str:
    """追加一条记忆，返回写入的条目文本。"""
    text = (text or "").strip()
    if not text:
        raise MemoryError("记忆内容不能为空")
    if len(text) > ENTRY_LIMIT:
        raise MemoryError(f"单条记忆太长（上限 {ENTRY_LIMIT} 字符）：只记结论，不贴输出")
    if core.redact(text) != text:
        raise MemoryError("记忆内容包含密钥/凭据，已拒绝写入：只记结论，不记秘密")
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = f"- [{time.strftime('%Y-%m-%d')}] " + "\n  ".join(text.splitlines())
    with _locked("memory.mutex"):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)  # 写入前收紧权限，也覆盖用户预建的文件。
            prefix = HEADER if os.fstat(f.fileno()).st_size == 0 else ""
            f.write(prefix + entry + "\n")
    core.audit("memory-add", chars=len(entry))
    return entry


def recall(limit: int = MEMORY_LIMIT) -> str:
    """返回注入 system prompt 的记忆正文；没有记忆时返回空串。"""
    p = path()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # 旧文件、离线恢复或手工编辑未经过 add 的拒绝逻辑；读取出口同样脱敏。
    # 必须先处理全文再截取尾部，否则 PEM 包围标记可能被截掉而泄露中间内容。
    text = core.redact(text)
    body = text[len(HEADER):] if text.startswith(HEADER) else text
    body = body.strip()
    if not body:
        return ""
    if len(body) > limit:
        cut = body[-limit:]
        cut = cut.split("\n", 1)[-1] if "\n" in cut else cut  # 丢弃截断的半行：半句话可能语义反转
        body = f"…[早期记忆已省略；全文在 {p}，可用 read 分段查看]\n" + cut
    return body
