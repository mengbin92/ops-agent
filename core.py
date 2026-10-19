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


def _has_write_marker(seg: str) -> bool:
    """白名单形状但藏写行为时不按只读放行。

    覆盖：重定向（仅 `2>`/`2>>` 豁免，fd1 显式重定向 `1>` 算写）、`&>`全量重定向、
    tee、find -exec/-delete、命令替换（`$(...)`/反引号，可在只读命令里藏任意写操作）。
    """
    if re.search(r"\btee\b|-exec\b|-delete\b", seg):
        return True
    if "$(" in seg or "`" in seg:
        return True
    if re.search(r"&>{1,2}", seg):
        return True
    for m in re.finditer(r"(\d*)>{1,2}", seg):
        if m.group(1) != "2":  # 空（裸 >）、1>、3> 等都是写
            return True
    return False


def _classify_single(seg: str, config: dict) -> str:
    for pat in config["readonly_patterns"]:
        if re.search(pat, seg):
            if _has_write_marker(seg):
                break  # 带写标记：跳过只读放行，落入风险规则
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


AUDIT_ROTATE_BYTES = 10 * 1024 * 1024  # ponytail: 单备份轮转 audit.jsonl.1，需要多代次再加


def audit(event: str, **fields) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(STATE_DIR, 0o700)  # 审计/会话/快照里可能有敏感输出，只留本人可读
    except OSError:
        pass
    path = STATE_DIR / "audit.jsonl"
    if path.exists() and path.stat().st_size > AUDIT_ROTATE_BYTES:
        os.replace(path, STATE_DIR / "audit.jsonl.1")
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def normalize(cmd: str) -> str:
    return " ".join(cmd.split())


def cmd_hash(cmd: str, target: str | None = None) -> str:
    """审批戳键。远程目标参与哈希：在 A 机器批准的命令不能拿到 B 机器执行。"""
    key = f"{target}\n{normalize(cmd)}" if target else normalize(cmd)
    return hashlib.sha256(key.encode()).hexdigest()


def approve(cmd: str, ttl_seconds: int = 900, force: bool = False,
            target: str | None = None) -> Path:
    level, _ = classify(cmd)
    if level == "R0":
        raise OpsxError("只读命令无需审批戳")
    if level == "R3" and not force:
        raise OpsxError("R3 命令需 --force 显式确认（不可逆操作）")
    stamp = {
        "cmd_hash": cmd_hash(cmd, target),
        "cmd": cmd,
        "target": target,
        "created": time.time(),
        "ttl": ttl_seconds,
        "force": force,
        "level": level,
        "method": "manual",
    }
    p = STATE_DIR / "approvals" / (stamp["cmd_hash"] + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding="utf-8")
    prune_approvals()  # 顺带清理过期戳
    audit("approve", cmd=cmd, level=level, ttl=ttl_seconds, force=force, target=target)
    return p


def check_stamp(cmd: str, target: str | None = None) -> bool:
    p = STATE_DIR / "approvals" / (cmd_hash(cmd, target) + ".json")
    if not p.exists():
        return False
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        return time.time() < s["created"] + s["ttl"]
    except (json.JSONDecodeError, KeyError, TypeError, OSError):
        return False  # 损坏的戳按无效处理（安全方向）


def prune_approvals() -> None:
    """清理已过期的审批戳。"""
    adir = STATE_DIR / "approvals"
    if not adir.exists():
        return
    now = time.time()
    for p in adir.glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            if now >= s["created"] + s["ttl"]:
                p.unlink()
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            p.unlink(missing_ok=True)  # 损坏文件一并清掉


def revoke(cmd: str, target: str | None = None) -> bool:
    """撤销审批戳（用户取消变更/提前失效）。返回是否确有戳被删除。"""
    p = STATE_DIR / "approvals" / (cmd_hash(cmd, target) + ".json")
    existed = p.exists()
    p.unlink(missing_ok=True)
    audit("revoke", cmd=cmd, target=target, existed=existed)
    return existed


def collect_state() -> dict:
    """快照与审批戳的统一读取，opsx list / MCP / agent 工具共用。"""
    sdir = STATE_DIR / "snapshots"
    snapshots = sorted(p.name for p in sdir.iterdir() if p.is_dir()) if sdir.exists() else []
    approvals = []
    adir = STATE_DIR / "approvals"
    if adir.exists():
        for p in sorted(adir.glob("*.json")):
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
                approvals.append({"cmd": st["cmd"], "level": st["level"], "ttl": st["ttl"],
                                  "target": st.get("target")})
            except (json.JSONDecodeError, KeyError, OSError):
                continue
    return {"snapshots": snapshots, "approvals": approvals}


def format_state(state: dict) -> list[str]:
    """快照/审批戳摘要（opsx list 与 agent ops_list 共用，避免两处格式漂移）。"""
    lines = []
    if state["snapshots"]:
        lines += ["== snapshots =="] + state["snapshots"]
    if state["approvals"]:
        lines.append("== approvals ==")
        lines += [f"{a['cmd'][:80]}  (level={a['level']}, ttl={a['ttl']}s, "
                  f"{'target=' + a['target'] if a.get('target') else '本机'})"
                  for a in state["approvals"]]
    return lines


def check(cmd: str, target: str | None = None) -> tuple[int, str]:
    level, matched = classify(cmd)
    if level == "R0":
        audit("check", cmd=cmd, level=level, result="allow", target=target)
        return 0, "只读命令，放行"
    if check_stamp(cmd, target):
        audit("check", cmd=cmd, level=level, result="allow-stamp", target=target)
        return 0, "有效审批戳，放行"
    audit("check", cmd=cmd, level=level, result="deny", target=target)
    hit = f"（命中分段：{matched}）" if matched else ""
    return 2, (
        f"[opsx] 拒绝执行：该命令判定为 {level}{hit}。"
        "请先向用户提交变更单（风险说明+回滚计划），经确认后使用 opsx exec 执行。"
    )


def _new_snapshot_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def begin_snapshot() -> tuple[str, Path]:
    sid = _new_snapshot_id()
    d = STATE_DIR / "snapshots" / sid
    d.mkdir(parents=True, exist_ok=True, mode=0o700)  # 快照可能含密钥/配置，仅本人可读
    return sid, d


def _snapshot_file_into(d: Path, path_str: str, items: list) -> None:
    src = Path(path_str)
    if not src.is_file():
        raise OpsxError(f"快照源不是文件: {path_str}")
    dest = d / "files" / str(src.resolve()).lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    items.append({
        "type": "file",
        "src": str(src.resolve()),
        "dest": str(dest.relative_to(d)),
        "mode": stat.S_IMODE(src.stat().st_mode),
    })


def _snapshot_cmd_into(d: Path, cmd: str, name: str, items: list) -> None:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise OpsxError(f"状态抓取命令失败({r.returncode}): {cmd}\n{r.stderr[:500]}")
    fname = f"cmd-{(name or 'output').replace('/', '_')}.txt"
    (d / fname).write_text(r.stdout, encoding="utf-8")
    items.append({"type": "cmd", "cmd": cmd, "name": name, "output": fname})


def _write_meta(d: Path, sid: str, items: list) -> None:
    meta = {"id": sid, "created": datetime.now(timezone.utc).isoformat(), "items": items}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot_file(path_str: str) -> str:
    sid, d = begin_snapshot()
    items: list = []
    _snapshot_file_into(d, path_str, items)
    _write_meta(d, sid, items)
    audit("snapshot", id=sid, kind="file", src=path_str)
    return sid


def snapshot_cmd(cmd: str, name: str = "") -> str:
    sid, d = begin_snapshot()
    items: list = []
    _snapshot_cmd_into(d, cmd, name, items)
    _write_meta(d, sid, items)
    audit("snapshot", id=sid, kind="cmd", cmd=cmd)
    return sid


_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def snapshot_dir(snapshot_id: str) -> Path:
    """快照目录。id 来自模型/用户：允许路径分隔符就能让 rollback 读写任意目录。"""
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise OpsxError(f"非法快照 id: {snapshot_id!r}")
    return STATE_DIR / "snapshots" / snapshot_id


def rollback(snapshot_id: str) -> str:
    d = snapshot_dir(snapshot_id)
    if not d.exists():
        raise OpsxError(f"快照不存在: {snapshot_id}")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    report = []
    for item in meta["items"]:
        if item["type"] == "file":
            shutil.copy2(d / item["dest"], item["src"])
            os.chmod(item["src"], item["mode"])
            report.append(f"已恢复文件: {item['src']}")
        else:
            out = (d / item["output"]).read_text(encoding="utf-8")
            report.append(f"捕获状态 [{item.get('name') or item['cmd']}]:\n{out}")
    audit("rollback", snapshot_id=snapshot_id)
    return "\n".join(report)


def prune_snapshots(keep: int = 200) -> None:
    d = STATE_DIR / "snapshots"
    if not d.exists():
        return
    ids = sorted(p for p in d.iterdir() if p.is_dir())
    for p in ids[:-keep]:
        shutil.rmtree(p)
        audit("prune", snapshot_id=p.name)


EXEC_TIMEOUT = 3600          # 变更命令默认最长 1 小时
EXEC_AUDIT_OUTPUT = 2000     # 审计里保留的输出字符数


def exec_change(
    cmd: str,
    snapshot_files: list[str] | None = None,
    snapshot_cmds: list[tuple[str, str]] | None = None,
    timeout: int = EXEC_TIMEOUT,
) -> tuple[int, str | None, str]:
    """原子执行：校验→快照→执行→审计。返回 (exit_code, snapshot_id, output)。

    输出一律 capture（调用方自行展示）：避免经 MCP 嵌入时子进程输出污染
    stdio 协议帧；同时写入审计（截断）。exit=4 表示超时终止。
    """
    snapshot_files = snapshot_files or []
    snapshot_cmds = snapshot_cmds or []
    code, msg = check(cmd)
    if code != 0:
        print(msg, file=sys.stderr)
        return code, None, ""
    for c, _ in snapshot_cmds:  # 快照命令本身也过守门：否则可把写操作藏进抓取参数
        code, _ = check(c)
        if code != 0:
            audit("exec-abort", cmd=cmd, reason=f"快照命令未通过守门: {c}")
            print(f"[opsx] 快照命令未通过守门，已中止执行: {c}", file=sys.stderr)
            return 2, None, ""
    sid = None
    if snapshot_files or snapshot_cmds:
        sid, d = begin_snapshot()
        items: list = []
        try:
            for f in snapshot_files:
                _snapshot_file_into(d, f, items)
            for c, name in snapshot_cmds:
                _snapshot_cmd_into(d, c, name, items)
        except (OpsxError, subprocess.SubprocessError, OSError) as e:
            shutil.rmtree(d, ignore_errors=True)
            print(f"[opsx] 快照失败，已中止执行: {e}", file=sys.stderr)
            audit("exec-abort", cmd=cmd, reason=str(e))
            return 3, None, ""
        _write_meta(d, sid, items)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        output = r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
        exit_code = r.returncode
    except subprocess.TimeoutExpired:
        output = f"[opsx] 超时：命令超过 {timeout}s 未结束，已终止"
        exit_code = 4
    audit("exec", cmd=cmd, snapshot_id=sid, exit_code=exit_code,
          output=output[:EXEC_AUDIT_OUTPUT])
    prune_snapshots()
    return exit_code, sid, output
