"""确定性日志粗筛：级别/关键词收口 + 模式聚类 + 时间分布。

定位：模型读语义之前的规则第一道。海量日志（journalctl / docker logs / 文件）
先在这里按规则收口——模型只看聚类报告和少量样本，不逐行扫原始日志。
输出即证据：每条结论附来源命令，部分扫描明确标注，无命中不等于没问题。
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

DEFAULT_LINES = 200_000     # 默认只扫最近 N 行：故障定位通常有时间窗
DEFAULT_TOP = 10
SAMPLE_LEN = 200            # 每条聚类保留的样本行长度上限
TIME_BUDGET = 30.0          # 单次扫描秒数上限；超时按部分扫描如实标注

# 级别映射：error 优先于 warn（一行多命中取高级别）
_LEVEL_RULES = [
    ("error", re.compile(
        r"\b(error|err|fatal|crit(?:ical)?|panic|emerg(?:ency)?|alert|"
        r"segfault|core dumped)\b", re.I)),
    ("warn", re.compile(r"\b(warn(?:ing)?)\b", re.I)),
]

# 关键词收口：级别词之外的常见故障信号（作用于原始行，先于归一化）
_KEYWORDS = re.compile(
    r"exception|traceback|failed|failure|timeout|timed out|refused|denied|"
    r"\boom\b|out of memory|unreachable|reset by peer|no such file|"
    r"corrupt|invalid|mismatch|cannot|unable to|"
    r"\bexit(?:ed)?\s+(?:code\s+)?[1-9]\d*\b|"
    r"(?<=[ \"'])5\d\d(?=[ \"'])",          # nginx access 里的 5xx 状态码
    re.I)

# 行首时间戳：能解析出时间就做时间桶分布，解析不了退化为行号区间
_TS_PATTERNS = [
    re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})"),          # ISO / journalctl / docker
    re.compile(r"^\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2})"),            # nginx [27/Oct/2026:14:21
    re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2})"),      # syslog Oct 27 14:21
]
_BUCKET_MINUTES = 10

# 聚类归一化：时间戳前缀、IP、UUID、十六进制、数字——同一故障的重复行聚成一团
_NORM_RULES = [
    (re.compile(r"^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s\]]*\]?\s*"), ""),
    (re.compile(r"^\[\d{2}/[A-Za-z]{3}/\d{4}:[^\]]*\]\s*"), ""),
    (re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+"), ""),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
]

_INTERESTING_LEVELS = ("error", "warn")


def _level_of(line: str) -> str | None:
    for name, pat in _LEVEL_RULES:
        if pat.search(line):
            return name
    return None


def _timestamp_bucket(line: str) -> str | None:
    for pat in _TS_PATTERNS:
        m = pat.match(line)
        if m:
            return m.group(1)
    return None


def _skeleton(line: str) -> str:
    out = line.strip()
    for pat, repl in _NORM_RULES:
        out = pat.sub(repl, out)
    return " ".join(out.split())[:SAMPLE_LEN]


def scan(lines, *, top: int = DEFAULT_TOP, pattern: str | None = None,
         time_budget: float = TIME_BUDGET) -> dict:
    """规则粗筛核心（纯函数，便于测试）。lines 为可迭代行序列。

    返回 {source 无关的} 统计、时间桶、可疑聚类；partial=True 表示时间预算内
    未扫完（结果有偏，必须如实报告）。
    """
    focus = None
    if pattern:
        focus = re.compile(pattern, re.I)

    level_counts: Counter = Counter()
    bucket_counts: dict[str, Counter] = {}
    groups: dict[tuple, dict] = {}
    total = 0
    scanned = 0
    partial = False
    ts_hits = 0
    start = time.monotonic()

    for raw in lines:
        if total & 0xFFF == 0 and time.monotonic() - start > time_budget:
            partial = True
            break
        total += 1
        line = raw.rstrip("\r\n")
        if not line:
            continue
        scanned += 1
        if focus and not focus.search(line):
            continue
        level = _level_of(line)
        if level:
            level_counts[level] += 1
        interesting = level in _INTERESTING_LEVELS or bool(_KEYWORDS.search(line))
        bucket = _timestamp_bucket(line)
        if bucket:
            ts_hits += 1
        if not interesting:
            continue
        bucket_counts.setdefault(bucket or f"lines~{total // 50000 * 50000}",
                                 Counter())[level or "kw"] += 1
        key = (level or "kw", _skeleton(line))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"level": level or "kw", "skeleton": key[1],
                               "count": 0, "first_line": total, "last_line": total,
                               "first_ts": bucket, "last_ts": bucket, "samples": []}
        g["count"] += 1
        g["last_line"] = total
        g["last_ts"] = bucket
        if len(g["samples"]) < 2 and line not in g["samples"]:
            g["samples"].append(line[:SAMPLE_LEN])

    ranked = sorted(groups.values(),
                    key=lambda g: (-g["count"], g["level"] != "error", g["first_line"]))
    return {
        "total": total, "scanned": scanned, "partial": partial,
        "time_parse_rate": (ts_hits / scanned) if scanned else 0.0,
        "level_counts": dict(level_counts),
        "buckets": {b: dict(c) for b, c in sorted(bucket_counts.items())},
        "groups": ranked[:top],
        "group_total": len(groups),
    }


# ---- 采集器：只读、固定 argv、不拼 shell（远程用 shlex.join 后走 remote.run）----

class ScanSourceError(Exception):
    pass


def _run_local(argv: list[str], timeout: int = 60) -> list[str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise ScanSourceError(f"本机没有 {argv[0]}，无法用该来源扫描")
    except subprocess.TimeoutExpired:
        raise ScanSourceError(f"{argv[0]} 采集超时（{timeout}s），未产出日志")
    if r.returncode != 0 and not r.stdout:
        raise ScanSourceError(f"{shlex.join(argv)} 退出码 {r.returncode}: "
                              f"{r.stderr.strip()[:200]}")
    return r.stdout.splitlines()


def _run_remote(target: str, argv: list[str]) -> list[str]:
    import remote  # 延迟导入：remote 依赖 core，模块级导入会成环
    code, out = remote.run(target, shlex.join(argv), timeout=60)
    if code != 0 and not out.strip():
        raise ScanSourceError(f"目标 {target} 上 {argv[0]} 采集失败（exit={code}）")
    return out.splitlines()


def from_journal(unit: str, *, since: str = "", until: str = "",
                 lines: int = DEFAULT_LINES, target: str = "") -> tuple[str, list]:
    argv = ["journalctl", "--quiet", "--no-pager", "-u", unit, "-n", str(lines)]
    if since:
        argv += ["--since", since]
    if until:
        argv += ["--until", until]
    desc = shlex.join(argv) + (f"  （目标 {target}）" if target else "（本机）")
    return desc, (_run_remote(target, argv) if target else _run_local(argv))


def from_docker(container: str, *, since: str = "",
                lines: int = DEFAULT_LINES, target: str = "") -> tuple[str, list]:
    argv = ["docker", "logs", "--timestamps", "--tail", str(lines)]
    if since:
        argv += ["--since", since]
    argv.append(container)
    desc = shlex.join(argv) + (f"  （目标 {target}）" if target else "（本机）")
    return desc, (_run_remote(target, argv) if target else _run_local(argv))


def from_file(path_str: str, *, lines: int = DEFAULT_LINES) -> tuple[str, list]:
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise ScanSourceError(f"不是常规文件: {path_str}")
    # 大文件全量流转，只保留最近 lines 行（故障定位通常有时间窗，尾部优先）
    from collections import deque
    buf: deque[str] = deque(maxlen=lines)
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                buf.append(line)
    except OSError as e:
        raise ScanSourceError(f"读取日志失败 {path_str}: {e}")
    return f"tail -n {lines} {shlex.quote(str(path))}（本机文件）", list(buf)


def detect_source(path: str = "", unit: str = "", container: str = "",
                  since: str = "", until: str = "", lines: int = DEFAULT_LINES,
                  target: str = "") -> tuple[str, list]:
    chosen = [s for s in (path, unit, container) if s]
    if len(chosen) != 1:
        raise ScanSourceError("来源三选一：path（本机文件）/ unit（journalctl）/ container（docker logs）")
    if target and path:
        raise ScanSourceError("远程扫描暂只支持 unit/container；远程文件请先在目标侧收成 journalctl 或用 bash 只读命令")
    if not shutil.which("docker") and container and not target:
        raise ScanSourceError("本机没有 docker，无法用 docker logs 来源")
    if unit:
        return from_journal(unit, since=since, until=until, lines=lines, target=target)
    if container:
        return from_docker(container, since=since, lines=lines, target=target)
    return from_file(path, lines=lines)


def format_report(desc: str, report: dict) -> str:
    """聚类报告文本：结论在前、样本在后、附跟进指针；输出本身就有界（Top N × 2 样本）。"""
    import core  # 延迟导入：样本脱敏与核心库同出口
    out = [f"== logscan: {desc} =="]
    head = (f"扫描 {report['scanned']}/{report['total']} 行"
            f"（时间戳可解析 {report['time_parse_rate']:.0%}）")
    if report["partial"]:
        head = "[部分扫描：时间预算内未扫完，结果有偏，勿当全貌] " + head
    out.append(head)
    levels = report["level_counts"]
    out.append("级别分布: " + (", ".join(f"{k}={levels[k]}" for k in ("error", "warn") if levels.get(k))
                               or "error=0, warn=0"))
    if report["buckets"]:
        out.append("== 时间分布（10 分钟桶，仅可疑行）==")
        peak = max(sum(c.values()) for c in report["buckets"].values()) or 1
        for bucket, counts in report["buckets"].items():
            n = sum(counts.values())
            bar = "■" * max(1, round(n / peak * 20))
            detail = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            out.append(f"  {bucket:<18} {bar} {detail}")
    if report["groups"]:
        out.append(f"== 可疑聚类 Top {len(report['groups'])}（共 {report['group_total']} 组，按次数排序）==")
        for i, g in enumerate(report["groups"], 1):
            span = f"{g['first_ts'] or '?'} → {g['last_ts'] or '?'}"
            loc = f"L{g['first_line']}→L{g['last_line']}"
            out.append(f"#{i} [{g['level']}] ×{g['count']}  {span}  {loc}")
            out.append(f"   模式: {g['skeleton']}")
            for sample in g["samples"]:
                out.append(f"   样本: {core.redact(sample)}")
    else:
        out.append("未发现错误级别/关键词命中。注意：粗筛只覆盖级别与关键词规则，"
                   "规则外的语义异常仍需针对性复核。")
    return "\n".join(out)
