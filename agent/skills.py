"""skills 目录：按需加载的运维 playbook（仿 dsh-skill-filesystem）。

位置：仓库 `skills/*.md` + `~/.ops-agent/skills/*.md`（同名后者覆盖）。
文件名即 skill 名（形状校验，防路径穿越）；正文首部可选 frontmatter：

    ---
    description: 一句话说明何时用
    ---

catalog() 给模型列目录，load() 才把正文送进上下文——不占常驻 context。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

SKILL_LIMIT = 20_000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SEARCH_DIRS = [core.REPO_DIR / "skills", core.STATE_DIR / "skills"]


def _split_frontmatter(text: str) -> tuple[str, str]:
    """返回 (description, 正文)；无 frontmatter 时 description 为空。"""
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end == -1:
        return "", text
    desc = ""
    for line in text[4:end].splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "description":
            desc = value.strip()
            break
    return desc, text[end + 4:].lstrip("\n")


def _parse(path: Path) -> dict:
    """文件名 + frontmatter 的 description；没有 frontmatter 就取首个非标题行。

    只读文件头：目录里的文件可能很大（误放的日志等），列目录不该整读。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        head = f.read(4096)
    desc, body = _split_frontmatter(head)
    if not desc:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                desc = line[:120]
                break
    return {"name": path.stem, "description": desc, "file": path}


def catalog() -> list[dict]:
    """按名字排序的 skill 目录；用户目录同名覆盖仓库。"""
    found: dict[str, dict] = {}
    for d in SEARCH_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                meta = _parse(p)
            except OSError:
                continue
            if not _NAME_RE.fullmatch(meta["name"]):
                continue  # 文件名含空格/非 ASCII 等，工具无法按名载入
            found[meta["name"]] = meta  # SEARCH_DIRS 后者优先
    return [found[name] for name in sorted(found)]


def load(name: str) -> str:
    """载入 skill 正文；名字/文件不存在时返回可读的错误说明。"""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return f"[错误] 非法 skill 名: {name!r}（只允许字母数字与 ._-）"
    for d in reversed(SEARCH_DIRS):  # 用户目录优先
        p = d / f"{name}.md"
        if not p.is_file():
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read(SKILL_LIMIT + 1)  # 只读上限+1：大文件不进内存
        except OSError as e:
            return f"[错误] 读取 skill 失败: {e}"
        if len(text) > SKILL_LIMIT:
            return text[:SKILL_LIMIT] + f"\n...[skill 超过 {SKILL_LIMIT} 字符，已截断]"
        return text
    names = ", ".join(m["name"] for m in catalog()) or "（无）"
    return f"[错误] 未找到 skill: {name}（可用: {names}）"
