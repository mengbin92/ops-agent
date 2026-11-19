"""确定性日志粗筛：级别/关键词收口 + 模式聚类 + 时间分布。

定位：模型读语义之前的规则第一道。海量日志（journalctl / docker logs / 文件）
先在这里按规则收口——模型只看聚类报告和少量样本，不逐行扫原始日志。
输出即证据：每条结论附来源命令，部分扫描明确标注，无命中不等于没问题。
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

DEFAULT_LINES = 200_000     # 默认只扫最近 N 行：故障定位通常有时间窗
DEFAULT_TOP = 10
SAMPLE_LEN = 200            # 每条聚类保留的样本行长度上限
TIME_BUDGET = 30.0          # 单次扫描秒数上限；超时按部分扫描如实标注
COLLECTION_TIMEOUT = 60.0
GROUP_LIMIT = 5000          # distinct 聚类上限（镜像 checks.SIGNAL_ANOMALY_LIMIT）：
                            # 全唯一行输入也不能无界增长，超限后如实标注截断

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
    re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:)([0-5]\d)"),          # ISO / journalctl / docker
    re.compile(r"^\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:)([0-5]\d)"),            # nginx [27/Oct/2026:14:21
    re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:)([0-5]\d)"),      # syslog Oct 27 14:21
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
            prefix, minute = m.groups()
            return f"{prefix}{int(minute) // _BUCKET_MINUTES * _BUCKET_MINUTES:02}"
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
    groups_capped = False
    buckets_capped = False
    ts_hits = 0
    start = time.monotonic()

    for raw in lines:
        if time.monotonic() - start > time_budget:
            partial = True
            break
        total += 1
        line = raw.rstrip("\r\n")
        if not line:
            continue
        scanned += 1
        bucket = _timestamp_bucket(line)
        if bucket:
            ts_hits += 1
        if focus and not focus.search(line):
            continue
        level = _level_of(line)
        if level:
            level_counts[level] += 1
        interesting = level in _INTERESTING_LEVELS or bool(_KEYWORDS.search(line))
        if not interesting:
            continue
        bucket_key = bucket or f"lines~{total // 50000 * 50000}"
        if bucket_key in bucket_counts or len(bucket_counts) < GROUP_LIMIT:
            bucket_counts.setdefault(bucket_key, Counter())[level or "kw"] += 1
        else:
            buckets_capped = True
        key = (level or "kw", _skeleton(line))
        g = groups.get(key)
        if g is None and len(groups) >= GROUP_LIMIT:
            # 全唯一行输入：不再新建聚类（已有聚类继续累计），防止 groups 无界
            # 增长；只置截断标记，模式谱系不完整由结果如实标注。
            groups_capped = True
            continue
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
        "groups_truncated": groups_capped,
        "buckets_truncated": buckets_capped,
        "time_parse_rate": (ts_hits / scanned) if scanned else 0.0,
        "level_counts": dict(level_counts),
        "buckets": {b: dict(c) for b, c in sorted(bucket_counts.items())},
        "groups": ranked[:top],
        "group_total": len(groups),
    }


# ---- 采集器：只读、固定 argv、不拼 shell（远程用 shlex.join 后走 remote.run）----

class ScanSourceError(Exception):
    pass


def _run_local(argv: list[str], timeout: float = COLLECTION_TIMEOUT) -> list[str]:
    """本机采集：两路输出合并。

    不能分开捕获只取 stdout：`docker logs` 会把容器内的 stderr（应用错误日志本来
    就写在这里）放到本进程的 stderr，成功退出时那些行会被静默丢弃。远程路径经
    remote.run() 合并输出的，两处行为必须一致。
    """
    import core
    try:
        r = core.capture_read_output(argv, timeout, core.READ_OUTPUT_LIMIT)
    except FileNotFoundError:
        raise ScanSourceError(f"本机没有 {argv[0]}，无法用该来源扫描")
    except subprocess.TimeoutExpired:
        raise ScanSourceError(f"{argv[0]} 采集超时（{timeout:g}s），未产出日志")
    except (OSError, core.OpsxError) as e:
        raise ScanSourceError(str(e)) from e
    output = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        # 输出已合并：非零退出就是采集失败，不能把错误文本当成"正常的日志内容"扫描。
        raise ScanSourceError(f"{shlex.join(argv)} 退出码 {r.returncode}: "
                              f"{core.redact(output.strip())[:200]}")
    return output.splitlines()


def _run_remote(target: str, argv: list[str], timeout: float) -> list[str]:
    import core
    import remote  # 延迟导入：remote 依赖 core，模块级导入会成环
    try:
        code, out = remote.run(target, shlex.join(argv), timeout=timeout)
    except (core.OpsxError, remote.RemoteError) as e:
        raise ScanSourceError(str(e)) from e
    if code != 0:
        raise ScanSourceError(f"目标 {target} 上 {argv[0]} 采集失败（exit={code}）: "
                              f"{core.redact(out.strip())[:200]}")
    return out.splitlines()


def from_journal(unit: str, *, since: str = "", until: str = "",
                 lines: int = DEFAULT_LINES, target: str = "",
                 timeout: float = COLLECTION_TIMEOUT) -> tuple[str, list]:
    argv = ["journalctl", "--quiet", "--no-pager", "-u", unit, "-n", str(lines)]
    if since:
        argv += ["--since", since]
    if until:
        argv += ["--until", until]
    desc = shlex.join(argv) + (f"  （目标 {target}）" if target else "（本机）")
    return desc, (_run_remote(target, argv, timeout) if target else _run_local(argv, timeout))


def from_docker(container: str, *, since: str = "",
                lines: int = DEFAULT_LINES, target: str = "",
                timeout: float = COLLECTION_TIMEOUT) -> tuple[str, list]:
    argv = ["docker", "logs", "--timestamps", "--tail", str(lines)]
    if since:
        argv += ["--since", since]
    argv += ["--", container]
    desc = shlex.join(argv) + (f"  （目标 {target}）" if target else "（本机）")
    return desc, (_run_remote(target, argv, timeout) if target else _run_local(argv, timeout))


def from_file(path_str: str, *, lines: int = DEFAULT_LINES,
              timeout: float = COLLECTION_TIMEOUT) -> tuple[str, list]:
    path = Path(path_str).expanduser().absolute()
    if not path.is_file():
        raise ScanSourceError(f"不是常规文件: {path_str}")
    argv = ["tail", "-n", str(lines), str(path)]
    return f"{shlex.join(argv)}（本机文件）", _run_local(argv, timeout)


def detect_source(path: str = "", unit: str = "", container: str = "",
                  since: str = "", until: str = "", lines: int = DEFAULT_LINES,
                  target: str = "", timeout: float = COLLECTION_TIMEOUT) -> tuple[str, list]:
    chosen = [s for s in (path, unit, container) if s]
    if len(chosen) != 1:
        raise ScanSourceError("来源三选一：path（本机文件）/ unit（journalctl）/ container（docker logs）")
    if target and path:
        raise ScanSourceError("远程扫描暂只支持 unit/container；远程文件请先在目标侧收成 journalctl 或用 bash 只读命令")
    if not shutil.which("docker") and container and not target:
        raise ScanSourceError("本机没有 docker，无法用 docker logs 来源")
    if unit:
        return from_journal(unit, since=since, until=until, lines=lines, target=target, timeout=timeout)
    if container:
        return from_docker(container, since=since, lines=lines, target=target, timeout=timeout)
    return from_file(path, lines=lines, timeout=timeout)


def scan_with_timeout(lines: list[str], *, top: int, pattern: str | None,
                      timeout: float) -> dict:
    if not pattern:
        return scan(lines, top=top, time_budget=timeout)
    re.compile(pattern, re.I)
    # stdlib re has no per-match timeout. Keep user patterns outside the agent process.
    deadline = time.monotonic() + timeout
    request = json.dumps({"lines": lines, "top": top, "pattern": pattern},
                         ensure_ascii=False).encode("utf-8", "backslashreplace")
    try:
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve())],
                                input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as e:
        raise ScanSourceError("日志正则扫描超时；请缩小日志范围或简化 pattern") from e
    if result.returncode != 0:
        raise ScanSourceError("日志正则扫描未完成")
    return json.loads(result.stdout)


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
        if report.get("buckets_truncated"):
            out.append(f"  [时间桶达到 {GROUP_LIMIT} 个上限，分布不完整]")
    if report["groups"]:
        cap_note = (f"；已达 {GROUP_LIMIT} 组上限，其后新出现的模式未再收录"
                    if report.get("groups_truncated") else "")
        out.append(f"== 可疑聚类 Top {len(report['groups'])}（共 {report['group_total']} 组"
                   f"{cap_note}，按次数排序）==")
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


if __name__ == "__main__":
    print(json.dumps(scan(**json.load(sys.stdin.buffer))))
