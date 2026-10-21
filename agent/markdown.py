"""终端 Markdown 渲染：纯 stdlib，输出 ANSI 上色文本。

覆盖 REPL 常见元素：标题、加粗/斜体/行内代码、围栏代码块、列表、
引用、分割线、链接、GFM 表格（按显示宽度对齐，兼容 CJK）。
不追求完整 CommonMark——终端里好看即可。
"""
from __future__ import annotations

import re
import unicodedata

# ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
UNDERLINE = "\033[4m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
MAGENTA = "\033[35m"
BG_CODE = "\033[48;5;236m"

_HEAD_COLOR = {1: MAGENTA + BOLD, 2: CYAN + BOLD, 3: GREEN + BOLD}


def _width(text: str) -> int:
    """显示宽度：CJK 全角计 2，ANSI 转义序列不计。"""
    text = re.sub(r"\033\[[0-9;]*m", "", text)
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _inline(text: str) -> str:
    """行内样式：代码 > 加粗 > 斜体 > 链接。先占位代码避免嵌套误伤。"""
    codes: list[str] = []

    def _stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: f"{CYAN}{UNDERLINE}{m.group(1)}{RESET}{DIM}({m.group(2)}){RESET}", text)
    text = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"{BOLD}{m.group(1)}{RESET}", text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", lambda m: f"{ITALIC}{m.group(1)}{RESET}", text)
    for i, code in enumerate(codes):
        text = text.replace(f"\x00{i}\x00", f"{YELLOW}{code}{RESET}")
    return text


def _render_table(lines: list[str]) -> list[str]:
    rows = [[_inline(c.strip()) for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            for line in lines if not re.match(r"^\s*\|?[\s:|-]+\|?\s*$", line)]
    if not rows:
        return lines
    cols = max(len(r) for r in rows)
    for r in rows:
        r += [""] * (cols - len(r))
    widths = [max(_width(r[c]) for r in rows) for c in range(cols)]
    out = []
    for i, row in enumerate(rows):
        cells = " │ ".join(_pad(cell, widths[c]) for c, cell in enumerate(row))
        out.append(f"  {cells}")
        if i == 0:
            out.append("  " + "─┼─".join("─" * w for w in widths))
    return out


def render(text: str, color: bool = True) -> str:
    out: list[str] = []
    lines = text.split("\n")
    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(DIM + ("  ┄┄ 代码 ┄┄" if in_code else "  ┄┄┄┄┄┄") + RESET)
            i += 1
            continue
        if in_code:
            out.append(f"{BG_CODE} {line} {RESET}")
            i += 1
            continue
        # 表格块：连续含 | 的行
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            j = i
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                j += 1
            out.extend(_render_table(lines[i:j]))
            i = j
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = min(len(m.group(1)), 3)
            heading_color = _HEAD_COLOR[level]
            out.append(f"{heading_color}{'▍' if level == 1 else ''}{_inline(m.group(2))}{RESET}")
        elif re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line):
            out.append(DIM + "  " + "─" * 40 + RESET)
        elif re.match(r"^\s*>", line):
            quote = re.sub(r"^\s*> ?", "", line)
            out.append(f"{DIM}▎{RESET}{ITALIC}{_inline(quote)}{RESET}")
        elif re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            m2 = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
            bullet = m2.group(2) if m2.group(2)[0].isdigit() else "•"
            out.append(f"{m2.group(1)}{CYAN}{bullet}{RESET} {_inline(m2.group(3))}")
        else:
            out.append(_inline(line))
        i += 1
    result = "\n".join(out)
    return result if color else re.sub(r"\033\[[0-9;]*m", "", result)
