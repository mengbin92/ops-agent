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
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = f"- [{time.strftime('%Y-%m-%d')}] " + "\n  ".join(text.splitlines())
    new_file = not p.exists()
    with open(p, "a", encoding="utf-8") as f:  # 单次 write：条目整体落盘
        if new_file:
            f.write(HEADER)
        f.write(entry + "\n")
    os.chmod(p, 0o600)  # 主机信息属内部资产，与 outputs 落盘同口径（即使用户预建过文件）
    core.audit("memory-add", chars=len(entry))
    return entry


def recall(limit: int = MEMORY_LIMIT) -> str:
    """返回注入 system prompt 的记忆正文；没有记忆时返回空串。"""
    p = path()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    body = text[len(HEADER):] if text.startswith(HEADER) else text
    body = body.strip()
    if not body:
        return ""
    if len(body) > limit:
        cut = body[-limit:]
        cut = cut.split("\n", 1)[-1] if "\n" in cut else cut  # 丢弃截断的半行：半句话可能语义反转
        body = f"…[早期记忆已省略；全文在 {p}，可用 read 分段查看]\n" + cut
    return body
