"""确定性检查：连通性 / 资源 / 服务三组探针 + 结构化结果（不依赖 LLM）。

W2 的四条设计约束：

1. **探针固定在代码里**：检查项由配置决定，模型不能决定"检查什么"。固定检查先跑、
   结果先落盘，LLM 只做解读（可选，失败不影响规则结论）。
2. **每条结果都能回答"怎么知道的"**：命令、退出码、采样时间、观测值、阈值、证据片段。
3. **采不到就是 unknown/error**：目标不可达、权限不足、输出无法解析都不算 healthy；
   只有全部 ok 且无未采集项，结论才可能是"正常"。
4. **探针命令同样走守门**：每条命令先过 core.check，配置写错不会变成写操作。

目标清单位于 `~/.ops-agent/targets.json`（与远程执行共用，新增字段全部可选）：

    {"web1": {"host": "10.0.0.5", "user": "deploy",
              "tags": ["prod", "web"],
              "env": {"role": "nginx", "os": "ubuntu-24.04"},
              "services": [{"type": "systemd", "name": "nginx"},
                           {"type": "docker", "name": "api", "expect": "running"}],
              "checks": {"disk_paths": ["/", "/data"], "disk_warn_pct": 85,
                         "disk_crit_pct": 95, "mem_warn_pct": 90, "mem_crit_pct": 95,
                         "load_warn_per_core": 2.0, "load_crit_per_core": 4.0,
                         "timeout": 20, "budget_seconds": 120},
              "enabled": true}}

`local` 是内置伪目标（本机），不需要注册；本机的检查项写在保留条目 `_local` 里：

    {"_local": {"services": [{"type": "docker", "name": "redis"}],
                "checks": {"disk_paths": ["/", "/Users"]}, "tags": ["workstation"]}}

`_local` 不允许写 host/user/port 等连接字段（本机不走 SSH）；注册表里出现名为 `local`
的条目会被拒绝（避免歧义）。`targets.json` 里的 `env` 是资产描述（人看的），不参与审批
绑定——审批绑定的是 host/user/port/identity/options（见 `remote.target_env`）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
import remote  # noqa: E402

GROUPS = ("connectivity", "resources", "services")
LOCAL = "local"
LOCAL_CONFIG_KEY = "_local"   # targets.json 里配置本机检查项的保留条目（不参与 SSH）
LOCAL_CONFIG_FIELDS = ("tags", "env", "services", "checks", "enabled")

OK, WARN, CRITICAL, UNKNOWN, ERROR = "ok", "warn", "critical", "unknown", "error"

DEFAULTS = {
    "disk_paths": ["/"],
    "disk_warn_pct": 85,
    "disk_crit_pct": 95,
    "mem_warn_pct": 90,
    "mem_crit_pct": 95,
    "load_warn_per_core": 2.0,
    "load_crit_per_core": 4.0,
    "timeout": 20,
    "budget_seconds": 120,
}
MAX_DISK_PATHS = 8
MAX_SERVICES = 20
MIN_TIMEOUT, MAX_TIMEOUT = 5, 300
MIN_BUDGET, MAX_BUDGET = 10, 3600
# 单目标探针次数上限：与时间预算互补的硬上界（合法配置最多约 33 项，这里留余量）。
PROBE_BUDGET = 60
EVIDENCE_LIMIT = 1200       # 单条证据保留的字符数（全量输出在报告文件里）
REPORT_KEEP = 100           # 报告文件保留份数
# 规则检查结果的机器可读摘要（按会话落盘，供调度/通知取用；模型不可用也有值）
SIGNAL_KEEP = 300           # 与 session.SESSION_KEEP 对齐：会话还在，摘要就还在
SIGNAL_ANOMALY_LIMIT = 100  # 单次摘要保留的异常条目上限
SIGNAL_OUTPUT_LIMIT = 400   # 每条异常保留的证据输出字符数
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SERVICE_KINDS = ("systemd", "docker")
SERVICE_EXPECT = ("running", "stopped")
# 目标条目允许的顶层字段：写错层级（如把 disk_paths 写在顶层）必须报错，不能静默生效。
TARGET_FIELDS = ("host", "user", "port", "identity", "options", "connect_timeout",
                 "tags", "env", "services", "checks", "enabled")

# 绝对路径：允许空格与非 ASCII（"/Volumes/My Disk" 是真实挂载点），
# 拒绝控制字符；命令侧仍用 shlex.quote，且写标记检查对原始段生效。
_PATH_RE = re.compile(r"^/[^\x00-\x1f\x7f]*$")
MAX_PATH_LEN = 500
# 服务名/容器名会拼进 shell 命令：只允许保守字符集，且不能以 '-' 开头（避免被当选项）。
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]*$")
_MOUNT_PCT_RE = re.compile(r"^(\d{1,3})%$")
_LOAD_RE = re.compile(r"load averages?:\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", re.I)
# /proc/loadavg 与 sysctl vm.loadavg 的原始形态：数字在开头（后者带花括号）。
_LOAD_RAW = re.compile(r"^\s*\{?\s*(\d+(?:\.\d+)?)[\s,]+(\d+(?:\.\d+)?)[\s,]+"
                       r"(\d+(?:\.\d+)?)")
_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_VM_LINE_RE = re.compile(r"^(.+?):\s+(\d+)\.?\s*$")
_MEMINFO_RE = re.compile(r"^(\w+):\s+(\d+)(?:\s+kB)?\s*$")


class CheckError(Exception):
    pass


# ---------------------------------------------------------------- 配置与清单


def _norm_checks(name: str, raw) -> dict:
    """校验并补全单个目标的检查配置；未知字段直接拒绝（拼错不该静默生效）。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CheckError(f"目标 {name} 的 checks 须为对象")
    extra = sorted(set(raw) - set(DEFAULTS))
    if extra:
        raise CheckError(f"目标 {name} 的 checks 含未知字段: {', '.join(extra)}")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    paths = cfg.get("disk_paths")
    if not isinstance(paths, list) or not paths or len(paths) > MAX_DISK_PATHS:
        raise CheckError(
            f"目标 {name} 的 checks.disk_paths 须为 1-{MAX_DISK_PATHS} 个绝对路径的数组")
    for p in paths:
        if (not isinstance(p, str) or not _PATH_RE.fullmatch(p) or ".." in p.split("/")
                or len(p) > MAX_PATH_LEN):
            raise CheckError(f"目标 {name} 的 checks.disk_paths 含非法路径: {p!r}"
                             f"（须为绝对路径，不含控制字符与 ..，长度 ≤ {MAX_PATH_LEN}）")
    cfg["disk_paths"] = list(paths)
    for key in ("disk_warn_pct", "disk_crit_pct", "mem_warn_pct", "mem_crit_pct"):
        val = cfg[key]
        if type(val) is not int or not 0 < val <= 100:
            raise CheckError(f"目标 {name} 的 checks.{key} 须为 1-100 的整数")
    for key in ("load_warn_per_core", "load_crit_per_core"):
        val = cfg[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0 < val <= 1000:
            raise CheckError(f"目标 {name} 的 checks.{key} 须为正数")
    for warn_key, crit_key in (("disk_warn_pct", "disk_crit_pct"),
                               ("mem_warn_pct", "mem_crit_pct"),
                               ("load_warn_per_core", "load_crit_per_core")):
        if cfg[crit_key] < cfg[warn_key]:
            raise CheckError(f"目标 {name} 的 checks.{crit_key} 不能小于 {warn_key}")
    timeout = cfg["timeout"]
    if type(timeout) is not int or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise CheckError(f"目标 {name} 的 checks.timeout 须为 {MIN_TIMEOUT}-{MAX_TIMEOUT} 的整数秒")
    budget = cfg["budget_seconds"]
    if type(budget) is not int or not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise CheckError(f"目标 {name} 的 checks.budget_seconds 须为 "
                         f"{MIN_BUDGET}-{MAX_BUDGET} 的整数秒")
    return cfg


def _norm_services(name: str, raw) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CheckError(f"目标 {name} 的 services 须为数组")
    if len(raw) > MAX_SERVICES:
        raise CheckError(f"目标 {name} 的 services 最多 {MAX_SERVICES} 项")
    out = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise CheckError(f"目标 {name} 的 services[{i}] 须为对象")
        extra = sorted(set(item) - {"type", "name", "expect"})
        if extra:
            raise CheckError(f"目标 {name} 的 services[{i}] 含未知字段: {', '.join(extra)}")
        kind, svc = item.get("type"), item.get("name")
        if kind not in SERVICE_KINDS:
            raise CheckError(f"目标 {name} 的 services[{i}].type 须为 {'/'.join(SERVICE_KINDS)}")
        if not isinstance(svc, str) or not _SERVICE_NAME_RE.fullmatch(svc):
            raise CheckError(f"目标 {name} 的 services[{i}].name 非法: {svc!r}"
                             "（只允许字母数字与 @ . _ : -）")
        expect = item.get("expect", "running")
        if expect not in SERVICE_EXPECT:
            raise CheckError(f"目标 {name} 的 services[{i}].expect 须为 {'/'.join(SERVICE_EXPECT)}")
        out.append({"type": kind, "name": svc, "expect": expect})
    return out


def _clean_text(name: str, field: str, value: str) -> str:
    """资产描述会进报告与提示词：拒绝换行/控制字符，避免把说明写成指令。"""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise CheckError(f"目标 {name} 的 {field} 含换行或控制字符: {value!r}")
    return value


def _norm_tags(name: str, raw) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 20:
        raise CheckError(f"目标 {name} 的 tags 须为不超过 20 项的字符串数组")
    for tag in raw:
        if not isinstance(tag, str) or not tag.strip() or len(tag) > 40:
            raise CheckError(f"目标 {name} 的 tags 含非法标签: {tag!r}")
        _clean_text(name, "tags", tag)
    return [t.strip() for t in raw]


def _norm_env(name: str, raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 20:
        raise CheckError(f"目标 {name} 的 env 须为不超过 20 项的 {{键: 值}} 对象")
    out = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 40:
            raise CheckError(f"目标 {name} 的 env 含非法键: {key!r}")
        _clean_text(name, "env 的键", key)
        if not isinstance(val, str) or len(val) > 200:
            raise CheckError(f"目标 {name} 的 env.{key} 须为不超过 200 字符的字符串")
        _clean_text(name, f"env.{key}", val)
        out[key.strip()] = val
    return out


def load_inventory() -> list:
    """读取检查清单：内置 local（可经 `_local` 配置）+ 注册表中的目标。

    `_local` 之外的 `_` 开头条目仍按普通目标处理（历史上就允许下划线开头的目标名）。
    """
    try:
        targets = remote.load_targets()
    except remote.RemoteError as e:
        raise CheckError(str(e)) from e
    if LOCAL in targets:
        raise CheckError(f"targets.json 不能用 {LOCAL!r} 作目标名：{LOCAL} 是本机内置目标，"
                         f"本机配置请写在 {LOCAL_CONFIG_KEY!r} 条目里；"
                         f"若要检查该主机，请改成其他目标名")
    local_raw = targets.get(LOCAL_CONFIG_KEY)
    if local_raw is not None and not isinstance(local_raw, dict):
        raise CheckError(f"{LOCAL_CONFIG_KEY} 的配置须为对象")
    local_cfg = local_raw or {}
    extra = sorted(set(local_cfg) - set(LOCAL_CONFIG_FIELDS))
    if extra:
        raise CheckError(f"{LOCAL_CONFIG_KEY} 只允许 {sorted(LOCAL_CONFIG_FIELDS)} 字段，"
                         f"非法字段: {', '.join(extra)}（本机不走 SSH，不能写 host 等连接字段）")
    local_enabled = local_cfg.get("enabled", True)
    if not isinstance(local_enabled, bool):
        raise CheckError(f"{LOCAL_CONFIG_KEY} 的 enabled 须为布尔值")
    inventory = [{
        "name": LOCAL, "kind": "local", "target": None, "host": "localhost",
        "user": "", "port": None,
        "tags": _norm_tags(LOCAL, local_cfg.get("tags")),
        "env": _norm_env(LOCAL, local_cfg.get("env")),
        "services": _norm_services(LOCAL, local_cfg.get("services")),
        "checks": _norm_checks(LOCAL, local_cfg.get("checks")),
        "enabled": local_enabled,
    }]
    for name in sorted(targets):
        if name == LOCAL_CONFIG_KEY:
            continue
        cfg = targets[name]
        if not isinstance(cfg, dict):
            raise CheckError(f"目标 {name} 的配置须为对象")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise CheckError(f"目标名非法: {name!r}")
        unknown = sorted(set(cfg) - set(TARGET_FIELDS))
        if unknown:
            raise CheckError(f"目标 {name} 含未知字段: {', '.join(unknown)}"
                             f"（允许: {', '.join(TARGET_FIELDS)}；"
                             f"检查阈值要写在 checks 里）")
        enabled = cfg.get("enabled", True)
        if not isinstance(enabled, bool):
            raise CheckError(f"目标 {name} 的 enabled 须为布尔值")
        port = cfg.get("port")
        host = str(cfg.get("host") or "")
        user = str(cfg.get("user") or "")
        _clean_text(name, "host", host)  # host/user 会进报告标题与提示词
        _clean_text(name, "user", user)
        inventory.append({
            "name": name, "kind": "remote", "target": name,
            "host": host, "user": user,
            "port": port if type(port) is int else None,
            "tags": _norm_tags(name, cfg.get("tags")),
            "env": _norm_env(name, cfg.get("env")),
            "services": _norm_services(name, cfg.get("services")),
            "checks": _norm_checks(name, cfg.get("checks")),
            "enabled": enabled,
        })
    return inventory


def _select(inventory: list, targets) -> list:
    if targets is None:
        wanted = ["all"]
    elif isinstance(targets, str):
        wanted = [targets]
    elif isinstance(targets, list):
        wanted = targets
    else:
        raise CheckError('targets 须为 "all"、目标名或目标名数组')
    if not wanted:
        wanted = ["all"]
    by_name = {t["name"]: t for t in inventory}
    out, seen, missing = [], set(), []
    for item in wanted:
        if not isinstance(item, str) or not item.strip():
            raise CheckError("targets 里的目标名须为非空字符串")
        name = item.strip()
        if name == "all":
            for entry in inventory:
                if entry["enabled"] and entry["name"] not in seen:
                    seen.add(entry["name"])
                    out.append(entry)
        elif name in by_name:
            if name not in seen:
                seen.add(name)
                out.append(by_name[name])
        else:
            missing.append(name)
    if missing:
        known = ", ".join(sorted(by_name))
        raise CheckError(f"未注册的检查目标: {', '.join(missing)}（可用: {known}）")
    if not out:
        raise CheckError("没有可检查的目标（注册表为空或全部 enabled=false）")
    return out


def _norm_groups(groups) -> tuple:
    if groups is None:
        return GROUPS
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, list) or not groups:
        raise CheckError(f"groups 须为 {list(GROUPS)} 的非空子集")
    picked = []
    for g in groups:
        if g not in GROUPS:
            raise CheckError(f"未知检查分组: {g!r}（可用: {', '.join(GROUPS)}）")
        if g not in picked:
            picked.append(g)
    return tuple(picked)


# ---------------------------------------------------------------- 探针执行


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _exec(target, cmd: str, timeout: int) -> tuple:
    """执行探针命令：target=None 为本机。

    单独一层便于自检用假主机替换这一层——守门、解析、状态判定仍走真实代码。
    """
    if target:
        try:
            return remote.run(target, cmd, timeout=timeout)
        except remote.RemoteError as e:
            return 125, f"[远程执行失败] {e}"
    try:
        r = core.run_shell(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"[超时] 命令超过 {timeout}s 未结束，已终止"
    except OSError as e:
        return 125, f"[执行失败] {e}"
    out = r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
    return r.returncode, out


def _budget_state(budget: dict) -> str:
    """探针前检查预算：返回空串表示可执行，否则返回不可执行的原因。

    两道闸：单目标总时间（配置 budget_seconds）与探针次数（PROBE_BUDGET）。
    时间闸是主约束——它把最坏耗时钉在配置值上，而不是"次数 × 单条超时"。
    """
    if budget.get("left", 0) <= 0:
        return "[预算] 单目标探针次数已达上限，未执行"
    deadline = budget.get("deadline")
    if deadline is not None and time.monotonic() > deadline:
        return "[预算] 单目标检查时间预算已用尽，未执行"
    return ""


def _probe(target, cmd: str, timeout: int, budget: dict | None = None) -> tuple:
    """探针命令先过守门：配置里写错命令不会变成写操作。

    budget：`{"left": n, "deadline": monotonic}`。超预算立即返回 non-zero，
    由调用方记为 unknown——宁可少采一项，也不能让单目标把整次巡检拖成无界。
    """
    if budget is not None:
        reason = _budget_state(budget)
        if reason:
            return 127, reason
        budget["left"] -= 1
    try:
        code, msg = core.check(cmd, target=target)
    except (core.OpsxError, OSError) as e:  # OSError：审计/状态目录写不进去
        return 126, f"[守门失败] {e}"
    if code != 0:
        return 126, f"[拒绝] {msg}"
    return _exec(target, cmd, timeout)


_FAIL_PREFIXES = ("[预算]", "[超时]", "[opsx] 超时", "[拒绝]", "[守门失败]",
                  "[远程执行失败]", "[执行失败]")


def _fail_detail(prefix: str, code: int, text: str) -> str:
    """失败原因：已经自带说明的（预算/超时/拒绝）原样给出，别包成"df 执行失败"。"""
    for marker in _FAIL_PREFIXES:
        if (text or "").startswith(marker):
            return text.strip()[:300]
    return f"{prefix}（exit={code}）"


def _item(target: str, group: str, name: str, status: str, *, value=None, display: str = "",
          unit: str = "", threshold=None, detail: str = "", command: str = "",
          exit_code=None, output: str = "", sampled_at: str = "") -> dict:
    return {
        "id": f"{target}/{group}/{name}",
        "target": target, "group": group, "name": name, "status": status,
        "value": value, "display": display, "unit": unit, "threshold": threshold,
        "detail": core.redact(detail), "sampled_at": sampled_at or _now(),
        "evidence": {"command": command, "exit": exit_code,
                     # 先脱敏再截断：私钥的 BEGIN/END 被切断会让正则匹配失败
                     "output": core.redact(output or "")[:EVIDENCE_LIMIT]},
    }


# ---- 输出解析：都是纯函数，自检直接打靶 ----

def _stdout_only(text: str) -> str:
    """去掉 remote._text 追加的 stderr 段：告警行里的 "90%" 不是容量列。"""
    return (text or "").split("\n[stderr]\n", 1)[0]


def parse_df(text: str) -> tuple:
    """`df -P -k -- <path>` → (已用百分比, 挂载点, 失败原因)。

    不依赖表头文字（locale 会翻译它）：POSIX `-P` 固定 6 列，容量列在第 5 列，
    挂载点从第 6 列开始（可能含空格）。只解析 stdout，避免 stderr 里的 "90%" 被误读。
    """
    lines = [ln for ln in _stdout_only(text).splitlines() if ln.strip()]
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or not _MOUNT_PCT_RE.fullmatch(fields[4]):
            continue
        return float(fields[4][:-1]), " ".join(fields[5:]), ""
    return None, "", f"df 输出里找不到容量列: {(lines[-1] if lines else '（空输出）')[:120]}"


def parse_meminfo(text: str) -> tuple:
    """Linux `/proc/meminfo` → (已用百分比, 失败原因)。

    用 meminfo 而不是 `free`：`free` 的表头/字段名会被 locale 翻译，
    meminfo 的键名固定是英文，解析不受环境影响。
    """
    values = {}
    for line in (text or "").splitlines():
        m = _MEMINFO_RE.match(line.strip())
        if not m:
            continue
        try:
            values[m.group(1)] = int(m.group(2))  # kB；只比较比值，单位无关
        except ValueError:  # 超长数字会触发 int() 位数上限，按无法解析处理
            return None, "meminfo 数值无法解析"
    total = values.get("MemTotal", 0)
    if total <= 0:
        return None, "缺少 MemTotal"
    available = values.get("MemAvailable")
    if available is None:  # 3.14 之前没有 MemAvailable：退回 free+buffers+cached
        available = (values.get("MemFree", 0) + values.get("Buffers", 0)
                     + values.get("Cached", 0))
        if available <= 0:
            return None, "缺少 MemAvailable 且无法用 free+buffers+cached 估算"
    return max(0.0, min(100.0, (total - available) / total * 100)), ""


def parse_vm_stat(text: str, total_bytes: int) -> tuple:
    """macOS `vm_stat` + 物理内存总量 → (已用百分比, 说明)。

    used 取 `Pages active + Pages wired down`：压缩内存本身占用 wired，
    再叠加 "Pages stored in compressor" 会重复计数（实测可算出 >100%）。
    这不是"内存压力"，只是占用率——macOS 会主动用满空闲内存做缓存，
    因此高占用本身不必然代表异常，阈值宜按机器实际基线调。
    """
    page_size, pages = 4096, {}
    for line in (text or "").splitlines():
        m = _PAGE_SIZE_RE.search(line)
        if m:
            page_size = int(m.group(1))
            continue
        m = _VM_LINE_RE.match(line.strip())
        if m:
            try:
                pages[m.group(1).strip()] = int(m.group(2))
            except ValueError:
                return None, "vm_stat 数值无法解析"
    if not isinstance(total_bytes, int) or total_bytes <= 0:
        return None, "缺少物理内存总量"
    active = pages.get("Pages active", 0)
    wired = pages.get("Pages wired down", 0)
    if active <= 0 and wired <= 0:
        return None, "vm_stat 缺少 Pages active / Pages wired down 字段"
    pct = (active + wired) * page_size / total_bytes * 100
    return max(0.0, min(100.0, pct)), f"page_size={page_size}（active + wired）"


def parse_load(text: str):
    """负载输出 → (1 分钟, 5 分钟, 15 分钟)；无法解析返回 None。

    兼容 `uptime` 的文字形态、`/proc/loadavg` 与 macOS `sysctl -n vm.loadavg`。
    """
    m = _LOAD_RE.search(text or "") or _LOAD_RAW.match(text or "")
    if not m:
        return None
    try:
        return tuple(float(m.group(i)) for i in (1, 2, 3))
    except ValueError:
        return None


def parse_systemd_show(text: str) -> dict:
    values = {}
    for line in (text or "").splitlines():
        key, sep, val = line.partition("=")
        if sep:
            values[key.strip()] = val.strip()
    return {"load": values.get("LoadState", ""), "active": values.get("ActiveState", ""),
            "sub": values.get("SubState", "")}


def parse_docker_inspect(text: str):
    """`docker inspect --format` 的 `状态,健康,重启次数`；无法解析返回 None。

    分隔符用逗号而不是竖线：`|` 是 shell 管道符，会先把命令切成多段，
    让 `docker inspect` 之外的片段落进未知命令（R2）而被守门拒绝。
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    fields = lines[0].split(",")
    if len(fields) < 3:
        return None
    try:
        restarts = int(fields[2].strip())
    except ValueError:
        restarts = -1
    return {"status": fields[0].strip(), "health": fields[1].strip(), "restarts": restarts}


def _pct_status(pct: float, warn, crit) -> str:
    if pct >= crit:
        return CRITICAL
    if pct >= warn:
        return WARN
    return OK


# ---- 分组探针 ----

def _headline(text: str, fallback: str) -> str:
    """取第一条有意义的输出行（跳过空行与 remote._text 插入的 [stderr] 分割标记）。"""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped in ("[stderr]", "[stdout]"):
            continue
        return stripped[:200]
    return fallback


def _connectivity(item: dict, timeout: int, budget: dict) -> tuple:
    code, out = _probe(item["target"], "uname -s", timeout, budget)
    # 复用 _headline：stdout 为空时不会把 "[stderr]" 当成平台名
    platform = _headline(out, "") if code == 0 else ""
    if platform.startswith("["):
        platform = ""
    if code == 0 and platform:
        status, detail = OK, f"平台 {platform}"
    else:
        status = ERROR
        detail = _headline(out, f"uname 失败（exit={code}）")
        if code == 0 and not platform:
            detail = "uname 无输出，无法确定平台"
    check = _item(item["name"], "connectivity", "连通性", status,
                  display=platform or "不可达", detail=detail,
                  command="uname -s", exit_code=code, output=out)
    return check, platform


def _disk_items(item: dict, cfg: dict, ts: str, budget: dict) -> list:
    out = []
    for path in cfg["disk_paths"]:
        cmd = f"df -P -k -- {shlex.quote(path)}"
        code, text = _probe(item["target"], cmd, cfg["timeout"], budget)
        label = f"磁盘 {path}"
        if code != 0:
            out.append(_item(item["name"], "resources", label, UNKNOWN,
                             detail=_fail_detail("df 执行失败", code, text), command=cmd,
                             exit_code=code, output=text, sampled_at=ts))
            continue
        pct, mount, why = parse_df(text)
        if pct is None:
            out.append(_item(item["name"], "resources", label, UNKNOWN,
                             detail=f"df 输出无法解析：{why}", command=cmd,
                             exit_code=code, output=text, sampled_at=ts))
            continue
        status = _pct_status(pct, cfg["disk_warn_pct"], cfg["disk_crit_pct"])
        out.append(_item(item["name"], "resources", label, status,
                         value=pct, display=f"{pct:.1f}%", unit="%",
                         threshold=f"警告 {cfg['disk_warn_pct']}% / 异常 {cfg['disk_crit_pct']}%",
                         detail=f"{mount} 已用 {pct:.1f}%", command=cmd,
                         exit_code=code, output=text, sampled_at=ts))
    return out


def _memory_item(item: dict, cfg: dict, platform: str, ts: str, budget: dict) -> dict:
    name, target, timeout = item["name"], item["target"], cfg["timeout"]
    warn, crit = cfg["mem_warn_pct"], cfg["mem_crit_pct"]
    if platform == "Darwin":
        cmd = "vm_stat"
        code, text = _probe(target, cmd, timeout, budget)
        if code != 0:
            return _item(name, "resources", "内存", UNKNOWN,
                         detail=_fail_detail("vm_stat 执行失败", code, text), command=cmd,
                         exit_code=code, output=text, sampled_at=ts)
        mem_cmd = "sysctl -n hw.memsize"
        mem_code, mem_out = _probe(target, mem_cmd, timeout, budget)
        total = None
        if mem_code == 0:
            try:
                total = int(mem_out.strip().splitlines()[0])
            except (ValueError, IndexError):
                total = None
        pct, why = parse_vm_stat(text, total)
        evidence = f"{text}\n\n$ {mem_cmd}\n{mem_out if mem_code == 0 else '（不可用）'}"
        cmd_shown = f"{cmd}; {mem_cmd}"
    else:
        # 用 /proc/meminfo 而不是 free：键名固定英文，不受目标机 locale 影响。
        cmd = "cat /proc/meminfo"
        code, text = _probe(target, cmd, timeout, budget)
        if code != 0:
            return _item(name, "resources", "内存", UNKNOWN,
                         detail=_fail_detail("读取 /proc/meminfo 失败", code, text),
                         command=cmd, exit_code=code, output=text, sampled_at=ts)
        pct, why = parse_meminfo(text)
        evidence, cmd_shown = text, cmd
    if pct is None:
        return _item(name, "resources", "内存", UNKNOWN, detail=f"内存输出无法解析：{why}",
                     command=cmd_shown, exit_code=code, output=evidence, sampled_at=ts)
    return _item(name, "resources", "内存", _pct_status(pct, warn, crit),
                 value=pct, display=f"{pct:.1f}%", unit="%",
                 threshold=f"警告 {warn}% / 异常 {crit}%",
                 detail=f"已用 {pct:.1f}%", command=cmd_shown, exit_code=code,
                 output=evidence, sampled_at=ts)


def _load_item(item: dict, cfg: dict, platform: str, ts: str, budget: dict) -> dict:
    name, target, timeout = item["name"], item["target"], cfg["timeout"]
    # 负载读原始文件/内核参数：`uptime` 的输出文字（"load average"）会被 locale 翻译。
    cmd = "sysctl -n vm.loadavg" if platform == "Darwin" else "cat /proc/loadavg"
    code, text = _probe(target, cmd, timeout, budget)
    if code != 0:
        return _item(name, "resources", "负载", UNKNOWN,
                     detail=_fail_detail("读取负载失败", code, text), command=cmd,
                     exit_code=code, output=text, sampled_at=ts)
    loads = parse_load(text)
    if loads is None:
        return _item(name, "resources", "负载", UNKNOWN, detail="负载输出无法解析",
                     command=cmd, exit_code=code, output=text, sampled_at=ts)
    cpu_cmd = "sysctl -n hw.ncpu" if platform == "Darwin" else "nproc"
    cpu_code, cpu_out = _probe(target, cpu_cmd, timeout, budget)
    cores = None
    if cpu_code == 0:
        try:
            cores = int(cpu_out.strip().splitlines()[0])
        except (ValueError, IndexError):
            cores = None
    evidence = f"{text}\n\n$ {cpu_cmd}\n{cpu_out if cpu_out.strip() else '（无输出）'}"
    if not cores or cores <= 0:
        return _item(name, "resources", "负载", UNKNOWN,
                     display=f"{loads[0]:.2f}（核数未知）",
                     detail=_fail_detail(f"无法确定 CPU 核数（{cpu_cmd}），无法与阈值比较",
                                         cpu_code, cpu_out),
                     command=f"{cmd}; {cpu_cmd}", exit_code=code, output=evidence,
                     sampled_at=ts)
    ratio = loads[0] / cores
    if ratio >= cfg["load_crit_per_core"]:
        status = CRITICAL
    elif ratio >= cfg["load_warn_per_core"]:
        status = WARN
    else:
        status = OK
    return _item(name, "resources", "负载", status,
                 value=round(ratio, 2), display=f"{loads[0]:.2f} / {cores} 核 = {ratio:.2f}×",
                 unit="×核",
                 threshold=f"警告 {cfg['load_warn_per_core']}× / 异常 {cfg['load_crit_per_core']}×",
                 detail=f"1/5/15 分钟负载 {loads[0]:.2f} / {loads[1]:.2f} / {loads[2]:.2f}",
                 command=f"{cmd}; {cpu_cmd}", exit_code=code, output=evidence, sampled_at=ts)


def _systemd_item(item: dict, cfg: dict, svc: dict, platform: str, ts: str,
                  budget: dict) -> dict:
    name, target = item["name"], item["target"]
    label = f"服务 systemd:{svc['name']}"
    if platform == "Darwin":
        return _item(name, "services", label, UNKNOWN,
                     detail="当前平台无 systemd（darwin），无法检查该服务", sampled_at=ts)
    if platform not in ("Linux", ""):
        return _item(name, "services", label, UNKNOWN,
                     detail=f"未适配的平台 {platform}，无法检查 systemd 服务", sampled_at=ts)
    cmd = f"systemctl show -p LoadState -p ActiveState -p SubState {svc['name']}"
    code, text = _probe(target, cmd, cfg["timeout"], budget)
    if code != 0:
        return _item(name, "services", label, UNKNOWN,
                     detail=_fail_detail("systemctl show 执行失败"
                                         "（权限不足或 systemd 不可用）", code, text),
                     command=cmd, exit_code=code, output=text, sampled_at=ts)
    state = parse_systemd_show(text)
    if state["load"] == "not-found":
        return _item(name, "services", label, UNKNOWN,
                     detail="单元不存在（LoadState=not-found）", command=cmd,
                     exit_code=code, output=text, sampled_at=ts)
    if not state["active"]:
        return _item(name, "services", label, UNKNOWN,
                     detail="systemctl show 输出缺少 ActiveState", command=cmd,
                     exit_code=code, output=text, sampled_at=ts)
    active = state["active"]
    display = f"{active}/{state['sub']}" if state["sub"] else active
    if svc["expect"] == "stopped":
        if active == "inactive":
            status = OK
        elif active == "failed":
            status = CRITICAL
        else:
            status = WARN
    elif active == "active":
        status = OK
    elif active in ("activating", "reloading", "deactivating"):
        status = WARN
    elif active == "failed":
        status = CRITICAL
    elif active == "inactive":
        status = CRITICAL
    else:
        status = UNKNOWN
    detail = f"期望 {svc['expect']}，实际 {display}"
    return _item(name, "services", label, status, display=display,
                 threshold=f"期望 {svc['expect']}", detail=detail, command=cmd,
                 exit_code=code, output=text, sampled_at=ts)


def _docker_item(item: dict, cfg: dict, svc: dict, ts: str, budget: dict) -> dict:
    name, target = item["name"], item["target"]
    label = f"容器 docker:{svc['name']}"
    fmt = ("{{.State.Status}},"
           "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}},"
           "{{.RestartCount}}")
    cmd = f"docker inspect --format '{fmt}' {svc['name']}"
    code, text = _probe(target, cmd, cfg["timeout"], budget)
    if code != 0:
        return _item(name, "services", label, UNKNOWN,
                     detail=_fail_detail("docker inspect 失败"
                                         "（容器不存在或 docker 不可用）", code, text),
                     command=cmd, exit_code=code, output=text, sampled_at=ts)
    info = parse_docker_inspect(text)
    if info is None:
        return _item(name, "services", label, UNKNOWN, detail="docker inspect 输出无法解析",
                     command=cmd, exit_code=code, output=text, sampled_at=ts)
    state, health = info["status"], info["health"]
    display = state + ("" if health in ("", "none") else f"（health={health}）")
    if not state:
        status = UNKNOWN
    elif svc["expect"] == "stopped":
        status = OK if state in ("exited", "created") else WARN
        if state == "dead":
            status = CRITICAL
    elif health == "unhealthy":
        status = CRITICAL
    elif state == "running":
        status = WARN if health == "starting" else OK
    elif state in ("restarting", "dead", "exited", "paused", "removing"):
        status = CRITICAL
    elif state == "created":
        status = WARN
    else:
        status = UNKNOWN
    restarts = info["restarts"]
    restarts_text = f"，重启 {restarts} 次" if restarts > 0 else ""
    return _item(name, "services", label, status, display=display,
                 threshold=f"期望 {svc['expect']}", detail=f"状态 {display}{restarts_text}",
                 command=cmd, exit_code=code, output=text, sampled_at=ts)


def _label(item: dict) -> str:
    if item["kind"] == "local":
        label = "本机"
    else:
        user = f"{item['user']}@" if item["user"] else ""
        port = f":{item['port']}" if item["port"] else ""
        label = f"{user}{item['host'] or '?'}{port}"
    extra = []
    if item["tags"]:
        extra.append("标签 " + ",".join(item["tags"]))
    if item["env"]:
        extra.append(" / ".join(f"{k}={v}" for k, v in sorted(item["env"].items())))
    return core.redact("；".join([label] + extra))


def _run_target(item: dict, groups: tuple, timeout_override, deadline=None) -> dict:
    cfg = dict(item["checks"])
    cfg["disk_paths"] = list(cfg["disk_paths"])
    if timeout_override:
        cfg["timeout"] = timeout_override
    ts = _now()
    result = {"name": item["name"], "target": item["target"], "host": item["host"],
              "kind": item["kind"], "tags": list(item["tags"]),
              "env": dict(item["env"]), "label": _label(item),
              "reachable": False, "checks": []}
    own = time.monotonic() + cfg["budget_seconds"]
    budget = {"left": PROBE_BUDGET,
              # 整体 deadline（调度任务预算）与单目标预算取小：预设检查不能吃掉模型时间
              "deadline": min(own, deadline) if deadline is not None else own}
    # 平台探测与分组无关：选探针（meminfo/free、nproc/sysctl）必须先知道是什么系统。
    check, platform = _connectivity(item, cfg["timeout"], budget)
    result["reachable"] = check["status"] == OK
    result["platform"] = platform
    if "connectivity" in groups or not result["reachable"]:
        # 没要连通性分组时，失败本身就是关键证据，也要留下。
        result["checks"].append(check)

    if not result["reachable"]:
        reason = check["detail"]
        for group in groups:
            if group == "connectivity":
                continue
            result["checks"].append(_item(
                item["name"], group, "未采集", UNKNOWN,
                detail=f"目标不可达，未执行 {group} 检查（连通性: {reason}）", sampled_at=ts))
        return result

    if "resources" in groups:
        result["checks"] += _disk_items(item, cfg, ts, budget)
        result["checks"].append(_memory_item(item, cfg, platform, ts, budget))
        result["checks"].append(_load_item(item, cfg, platform, ts, budget))
    if "services" in groups:
        if not item["services"]:
            result["checks"].append(_item(
                item["name"], "services", "服务清单", UNKNOWN,
                detail="未配置 services：该目标的业务可用性未被检查", sampled_at=ts))
        for svc in item["services"]:
            if svc["type"] == "systemd":
                result["checks"].append(_systemd_item(item, cfg, svc, platform, ts, budget))
            else:
                result["checks"].append(_docker_item(item, cfg, svc, ts, budget))
    result["probes_left"] = budget["left"]
    return result


def _safe_target(item: dict, groups: tuple, timeout_override, deadline=None) -> dict:
    """单个目标出错（解析/意外异常）不能拖垮整次巡检：降级成该目标的一条 error。"""
    try:
        return _run_target(item, groups, timeout_override, deadline)
    except Exception as e:  # noqa: BLE001 —— 其他目标的结果仍然有价值
        return {
            "name": item["name"], "target": item["target"], "host": item["host"],
            "kind": item["kind"], "tags": list(item["tags"]), "env": dict(item["env"]),
            "label": _label(item), "reachable": False, "platform": "",
            "checks": [_item(item["name"], "connectivity", "检查执行异常", ERROR,
                             detail=f"该目标的检查执行失败: {e}", sampled_at=_now())],
        }


# ---------------------------------------------------------------- 报告


def _anomaly_line(check: dict) -> str:
    name = f"{check['target']} · {check['name']}"
    body = check.get("display") or ""
    threshold = check.get("threshold")
    if body and threshold:
        body = f"{body}（阈值 {threshold}）"
    elif not body:
        body = check.get("detail") or "无详情"
    if body and check.get("detail") and body != check["detail"] and check["status"] != OK:
        body = f"{body}｜{check['detail']}"
    return f"[{check['status']}] {name}: {body}"


def _verdict(counts: dict, complete: bool) -> str:
    if counts[CRITICAL]:
        return "异常"
    if counts[WARN]:
        return "警告"
    if not complete:  # 有未采集项，或压根没检查全部三个分组
        return "不完整"
    return "正常"


def _target_verdict(checks: list, full_groups: bool) -> str:
    """目标级结论。没跑全三个分组时不能写"正常"——那是覆盖不足，不是健康。"""
    statuses = [c["status"] for c in checks]
    if CRITICAL in statuses:
        return "异常"
    if WARN in statuses:
        return "警告"
    if ERROR in statuses or UNKNOWN in statuses:
        return "不完整"
    return "正常" if full_groups else "未完整检查"


def _skipped_names(inventory: list, selected: list) -> list:
    chosen = {t["name"] for t in selected}
    return [t["name"] for t in inventory if not t["enabled"] and t["name"] not in chosen]


def run_checks(targets=None, groups=None, timeout=None, deadline=None) -> dict:
    """跑三组确定性检查，返回结构化报告（不调用任何模型）。

    deadline 是 time.monotonic() 口径的整体时间上限（调度任务预算），
    与每个目标自己的 budget_seconds 取小——超时后的探针记 unknown，不静默跳过。
    """
    started = time.monotonic()
    group_list = _norm_groups(groups)
    if timeout is not None and (type(timeout) is not int or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT):
        raise CheckError(f"timeout 须为 {MIN_TIMEOUT}-{MAX_TIMEOUT} 的整数秒")
    inventory = load_inventory()
    selected = _select(inventory, targets)
    target_reports = [_safe_target(item, group_list, timeout, deadline) for item in selected]

    counts = {OK: 0, WARN: 0, CRITICAL: 0, UNKNOWN: 0, ERROR: 0}
    anomalies = []
    full_groups = len(group_list) == len(GROUPS)
    for tr in target_reports:
        tr["verdict"] = _target_verdict(tr["checks"], full_groups)
        for check in tr["checks"]:
            counts[check["status"]] += 1
            if check["status"] != OK:
                anomalies.append({"id": check["id"], "target": check["target"],
                                  "name": check["name"], "status": check["status"],
                                  "line": _anomaly_line(check)})
    total = sum(counts.values())
    coverage = {
        "groups_requested": list(group_list),
        "groups_skipped": [g for g in GROUPS if g not in group_list],
        "targets_skipped": _skipped_names(inventory, selected),
    }
    # 完整性 = 三项都不缺：没有采不到、没有漏分组、没有漏目标。
    # 只跑一个分组时即使全 ok 也必须是"不完整"，否则单看连通性就能宣称"正常"。
    complete = (counts[UNKNOWN] == 0 and counts[ERROR] == 0
                and not coverage["groups_skipped"])
    report = {
        "report_id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "generated_at": _now(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "mode": "rules-only",
        "groups": list(group_list),
        "coverage": coverage,
        "targets": target_reports,
        "target_names": [tr["name"] for tr in target_reports],
        "summary": {"targets": len(target_reports), "checks": total, **counts},
        "verdict": _verdict(counts, complete),
        "complete": complete,
        "anomalies": anomalies,
    }
    try:
        core.audit("inspect", report_id=report["report_id"], targets=report["target_names"],
                   groups=list(group_list), verdict=report["verdict"],
                   complete=report["complete"], anomalies=len(anomalies))
    except OSError:
        pass  # 审计写不进去（磁盘满/只读 home）不能连累已经跑完的检查结果
    return report


def _cell(value) -> str:
    text = "-" if value in (None, "") else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _completeness_text(report: dict) -> str:
    if report["complete"]:
        return "完整"
    reasons = []
    summary = report["summary"]
    if summary[UNKNOWN] or summary[ERROR]:
        reasons.append("存在未采集项")
    skipped = report["coverage"]["groups_skipped"]
    if skipped:
        reasons.append("未检查分组 " + "/".join(skipped))
    return "不完整（" + ("；".join(reasons) or "覆盖不足") + "）"


def _coverage_text(report: dict) -> str:
    coverage = report["coverage"]
    text = "分组 " + "/".join(coverage["groups_requested"])
    if coverage["targets_skipped"]:
        text += "；跳过目标（enabled=false）: " + ", ".join(coverage["targets_skipped"])
    return text


def format_report(report: dict) -> str:
    """人类可读报告：先列全部异常（不允许被后文覆盖），再逐目标给完整表格与证据。"""
    summary = report["summary"]
    lines = [
        f"# 巡检报告 {report['report_id']}",
        f"- 生成时间: {report['generated_at']}（耗时 {report['duration_ms']}ms，规则检查，模型未参与）",
        f"- 目标: {summary['targets']} 个（{', '.join(report['target_names'])}）",
        f"- 覆盖: {_coverage_text(report)}",
        f"- 检查项: {summary['checks']} 项｜正常 {summary[OK]} / 警告 {summary[WARN]} / "
        f"异常 {summary[CRITICAL]} / 未知 {summary[UNKNOWN]} / 错误 {summary[ERROR]}",
        f"- 结论: **{report['verdict']}**｜完整性: {_completeness_text(report)}",
    ]
    if report["anomalies"]:
        lines.append("")
        lines.append(f"## 全部异常与未采集项（共 {len(report['anomalies'])} 项）")
        lines += [f"{i}. {a['line']}" for i, a in enumerate(report["anomalies"], 1)]
    for tr in report["targets"]:
        lines.append("")
        lines.append(f"## {tr['name']}（{tr['label']}）— {tr['verdict']}")
        lines.append("| 检查项 | 状态 | 观测值 | 阈值 | 采样时间 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for check in tr["checks"]:
            at = check["sampled_at"][11:19] if check.get("sampled_at") else "-"
            lines.append(f"| {_cell(check['group'] + '/' + check['name'])} | "
                         f"{check['status']} | {_cell(check.get('display'))} | "
                         f"{_cell(check.get('threshold'))} | {at} |")
        bad = [c for c in tr["checks"] if c["status"] != OK]
        if bad:
            lines.append("")
            lines.append("证据（仅非正常项）：")
            for check in bad:
                ev = check["evidence"]
                lines.append(f"- [{check['status']}] {check['name']}："
                             f"{check.get('detail') or check.get('display') or ''}")
                if ev.get("command"):
                    lines.append(f"  - 命令: `{ev['command']}`（exit={ev['exit']}）")
                if ev.get("output"):
                    excerpt = " / ".join(ev["output"].splitlines()[:8])[:600]
                    lines.append(f"  - 输出: {excerpt}")
    return "\n".join(lines)


def _report_dir() -> Path:
    return core.STATE_DIR / "reports"


def _prune_reports(keep: int = REPORT_KEEP) -> None:
    # shortcut: 与 outputs 的轮转同源风险——多进程并发时可能删掉另一进程刚写的文件；
    # 需要更长保留期时改按日期分目录。这里只藏掉单次失败，不影响本次报告的写入结果。
    try:
        files = sorted(_report_dir().glob("*.md"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in files[:-keep]:
        try:
            path.unlink()
        except OSError:
            pass


def save_report(slug: str, text: str) -> Path | None:
    """完整报告落盘（0600，落盘前统一脱敏）。

    返回 None 表示写不进去（磁盘满/无权限）：调用方必须降级为"直接返回报告文本"，
    不能因为保存失败把已经跑完的检查结果整体丢掉。
    """
    directory = _report_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)  # 已存在的旧目录不会因 mkdir 的 mode 收紧权限
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", slug or "report").strip("-")[:60] or "report"
        path = directory / (time.strftime("%Y%m%d-%H%M%S") + f"-{safe}-"
                            f"{uuid.uuid4().hex[:6]}.md")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(core.redact(text) + "\n")  # 落盘文件与回显同一脱敏口径
            handle.flush()
            os.fsync(handle.fileno())  # 崩溃后不留 0 字节报告（工具已对外声明该路径）
    except OSError:
        return None
    _prune_reports()
    return path


def _signal_dir() -> Path:
    return core.STATE_DIR / "signals"


def _signal_payload(report: dict, report_path: Path | str | None) -> dict:
    """报告 → 机器可读摘要：异常项带上证据命令与输出片段。

    证据必须在这里从 checks 结构里取，而不是等模型复述——模型可能改写、遗漏，
    甚至在提示注入下伪造路径；通知与运行记录只信这份确定性文件。
    """
    evidence = {}
    for target_report in report.get("targets") or []:
        for check in (target_report or {}).get("checks") or []:
            if isinstance(check, dict) and check.get("id"):
                evidence[check["id"]] = check
    anomalies = []
    for anomaly in report.get("anomalies") or []:
        if not isinstance(anomaly, dict):
            continue
        check = evidence.get(anomaly.get("id")) or {}
        ev = check.get("evidence") or {}
        anomalies.append({
            "id": anomaly.get("id"), "target": anomaly.get("target"),
            "name": anomaly.get("name"), "status": anomaly.get("status"),
            "line": anomaly.get("line"),
            "command": ev.get("command"),
            "output": (ev.get("output") or "")[:SIGNAL_OUTPUT_LIMIT],
        })
        if len(anomalies) >= SIGNAL_ANOMALY_LIMIT:
            break
    all_anomalies = report.get("anomalies") or []
    target_verdicts = {tr.get("name"): tr.get("verdict")
                       for tr in report.get("targets") or [] if isinstance(tr, dict)}
    groups = report.get("groups") or []
    target_groups = {name: list(groups) for name in report.get("target_names") or []}
    check_statuses = {key: check.get("status") for key, check in evidence.items()}
    target_complete = {
        tr.get("name"): (set(groups) == set(GROUPS)
                         and tr.get("verdict") not in ("不完整", "未完整检查")
                         and not any(c.get("status") in (UNKNOWN, ERROR)
                                     for c in tr.get("checks") or []))
        for tr in report.get("targets") or [] if isinstance(tr, dict)
    }
    for name in report.get("target_names") or []:
        target_complete.setdefault(name, report.get("complete") is True
                                   and set(groups) == set(GROUPS))
    return {
        "version": 1,
        "created_ts": time.time(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_id": report.get("report_id"),
        "verdict": report.get("verdict"),
        "complete": report.get("complete"),
        "groups": report.get("groups") or [],
        "targets": report.get("target_names") or [],
        "target_verdicts": target_verdicts,
        "target_groups": target_groups,
        "target_complete": target_complete,
        "check_statuses": check_statuses,
        "summary": report.get("summary") or {},
        "report_path": str(report_path) if report_path else None,
        "report_paths": [str(report_path)] if report_path else [],
        "anomalies": anomalies,
        # 摘要自身也可能被 100 条上限截断：此时"没看到"不等于"不存在"，禁止判恢复
        "truncated": len(all_anomalies) > len(anomalies),
    }


def _merge_signal(previous: dict, current: dict) -> dict:
    """同一会话可以分批巡检；只替换这次实际采样的目标与分组。"""
    targets, groups = set(current["targets"]), set(current["groups"])

    def sampled(key):
        parts = key.split("/", 2)
        picked = current["target_groups"].get(parts[0], groups)
        return len(parts) > 1 and parts[0] in targets and parts[1] in picked

    old_statuses = previous.get("check_statuses") or {
        a["id"]: a.get("status") for a in previous.get("anomalies") or []
        if isinstance(a, dict) and isinstance(a.get("id"), str)}
    statuses = {key: status for key, status in old_statuses.items()
                if isinstance(key, str) and status in (OK, WARN, CRITICAL, UNKNOWN, ERROR)
                and not sampled(key)}
    statuses.update(current["check_statuses"])
    anomalies = {a["id"]: a for a in previous.get("anomalies") or []
                 if isinstance(a, dict) and isinstance(a.get("id"), str)
                 and not sampled(a["id"])}
    anomalies.update({a["id"]: a for a in current["anomalies"]})
    target_groups = dict(previous.get("target_groups") or {})
    for target in previous.get("targets") or []:
        target_groups.setdefault(target, previous.get("groups") or [])
    for target, picked in current["target_groups"].items():
        target_groups[target] = list(dict.fromkeys(target_groups.get(target, []) + picked))
    current["targets"] = list(dict.fromkeys((previous.get("targets") or []) + current["targets"]))
    current["groups"] = list(dict.fromkeys((previous.get("groups") or []) + current["groups"]))
    current["target_groups"] = target_groups
    incoming_complete = current["target_complete"]
    incoming_statuses = current["check_statuses"]
    current["check_statuses"] = statuses
    current["target_complete"], current["target_verdicts"] = {}, {}
    for target in current["targets"]:
        checks = [{"status": status} for key, status in statuses.items()
                  if key.split("/", 1)[0] == target]
        full = set(target_groups.get(target, [])) == set(GROUPS)
        current["target_complete"][target] = full and not any(
            check["status"] in (UNKNOWN, ERROR) for check in checks)
        if (target in targets and not incoming_complete.get(target)
                and not any(key.split("/", 1)[0] == target for key in incoming_statuses)):
            current["target_complete"][target] = False
        current["target_verdicts"][target] = _target_verdict(checks, full)
    counts = {status: sum(value == status for value in statuses.values())
              for status in (OK, WARN, CRITICAL, UNKNOWN, ERROR)}
    current["summary"] = {"targets": len(current["targets"]), "checks": len(statuses), **counts}
    current["complete"] = all(current["target_complete"].values())
    current["verdict"] = _verdict(counts, current["complete"])
    current["anomalies"] = list(anomalies.values())[:SIGNAL_ANOMALY_LIMIT]
    current["truncated"] = (bool(previous.get("truncated")) or current["truncated"]
                            or len(anomalies) > SIGNAL_ANOMALY_LIMIT)
    old_paths = previous.get("report_paths") or [previous.get("report_path")]
    current["report_paths"] = list(dict.fromkeys(
        path for path in old_paths + current["report_paths"] if path))
    return current


def save_signal(session_id: str | None, report: dict,
                report_path: Path | str | None = None) -> Path | None:
    """把规则检查结果按会话落盘（`~/.ops-agent/signals/<session_id>.json`）。

    调度与通知用会话 id 取回这份摘要，因此"LLM 完全不可用"也不影响异常上报：
    检查块在模型之前跑完，摘要已经在这里了。写入失败只返回 None，不影响工具结果。
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    try:
        payload = _signal_payload(report, report_path)
        previous = load_signal(session_id)
        if previous is not None:
            payload = _merge_signal(previous, payload)
    except (TypeError, ValueError):
        return None
    return _save_signal_payload(session_id, payload)


def _save_signal_payload(session_id: str, payload: dict) -> Path | None:
    """保存已聚合的摘要，供同会话采样与调度重试共用。"""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    directory = _signal_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        payload = dict(payload, session_id=session_id)
        path = directory / f"{session_id}.json"
        tmp = directory / f".{session_id}-{uuid.uuid4().hex[:6]}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(core.redact_obj(payload), handle, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        return None
    _prune_signals()
    return path


def signal_path(session_id: str) -> Path | None:
    """摘要文件路径（供运行记录引用；非法会话 id 返回 None）。"""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return _signal_dir() / f"{session_id}.json"


def load_signal(session_id: str | None) -> dict | None:
    """按会话取回规则检查摘要；没有或损坏时返回 None（调用方退回结论文本）。"""
    path = signal_path(session_id or "")
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if data.get("session_id") != session_id:
        return None
    if not isinstance(data.get("anomalies"), list):
        return None
    if any(not isinstance(data.get(key), list)
           or any(not isinstance(value, str) for value in data[key])
           for key in ("targets", "groups")):
        return None
    for key in ("target_verdicts", "target_groups", "target_complete", "check_statuses"):
        if key in data and not isinstance(data[key], dict):
            return None
    if any(not isinstance(value, list) or any(group not in GROUPS for group in value)
           for value in (data.get("target_groups") or {}).values()):
        return None
    if any(type(value) is not bool for value in (data.get("target_complete") or {}).values()):
        return None
    if any(status not in (OK, WARN, CRITICAL, UNKNOWN, ERROR)
           for status in (data.get("check_statuses") or {}).values()):
        return None
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
           or item.get("status") not in (WARN, CRITICAL, UNKNOWN, ERROR)
           for item in data["anomalies"]):
        return None
    if "report_paths" in data and (not isinstance(data["report_paths"], list)
                                   or any(not isinstance(path, str)
                                          for path in data["report_paths"])):
        return None
    return data


def _prune_signals(keep: int = SIGNAL_KEEP) -> None:
    try:
        files = sorted(_signal_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in files[:-keep]:
        try:
            path.unlink()
        except OSError:
            pass


def _explain(report_text: str, model) -> str:
    """可选 LLM 解读：只读上下文、轮次受限；失败由调用方降级为"不可用"。"""
    from . import loop, session as session_mod, tools
    ctx = tools.ToolContext(ask_user=None, allowed=tools.SUBAGENT_TOOLS, model=model)
    prompt = ("下面是程序生成的确定性巡检结果。不要重新跑整套检查，也不要改写规则结论。\n"
              "请用中文回答：1) 最需要处理的 1-3 项（按影响排序）；2) 每项的可能原因与"
              "只读验证命令；3) 哪些是未采集项、需要人工补什么。规则结论为正常时只做简要确认。\n\n"
              + report_text)
    return loop.run_turn(prompt, session_mod.Session(), ctx, system=loop.SUBAGENT_PROMPT,
                         model=model, max_turns=3)


# 退出码约定（脚本/cron 可判定，不必解析文本）：
# 0 正常；2 有警告或异常；3 不完整（未采集或漏分组）；1 检查无法执行
EXIT_OK, EXIT_ERROR, EXIT_FINDING, EXIT_INCOMPLETE = 0, 1, 2, 3


def exit_code(report: dict) -> int:
    verdict = report["verdict"]
    if verdict == "正常":
        return EXIT_OK
    if verdict == "不完整":
        return EXIT_INCOMPLETE
    return EXIT_FINDING


class _Parser(argparse.ArgumentParser):
    """用法错误按 1 退出：2 已约定表示"有警告/异常"，不能被 argparse 占用。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: 错误: {message}\n")


def cli(argv=None) -> int:
    parser = _Parser(
        prog="opsagent inspect",
        description="确定性巡检：连通性/资源/服务（规则检查不依赖 LLM，--explain 才调用模型）",
        epilog="退出码：0 正常；2 有警告或异常；3 不完整（未采集/漏分组）；1 检查无法执行")
    parser.add_argument("--target", action="append", default=[],
                        help="目标名或 all（可多次）；默认 all")
    parser.add_argument("--group", action="append", default=[], choices=list(GROUPS),
                        help="只跑指定分组（可多次；只跑部分分组时结论最高为「不完整」）")
    parser.add_argument("--timeout", type=int, default=0,
                        help=f"单条探针超时秒数，覆盖目标配置（{MIN_TIMEOUT}-{MAX_TIMEOUT}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告（stdout）")
    parser.add_argument("--explain", action="store_true",
                        help="追加 LLM 解读（需要 API；失败不影响规则结果与结论）")
    parser.add_argument("--model", default=None, help="覆盖默认模型")
    args = parser.parse_args(argv)
    targets = args.target or ["all"]
    try:
        report = run_checks(targets=targets[0] if len(targets) == 1 else targets,
                            groups=args.group or None,
                            timeout=args.timeout or None)
    except CheckError as e:
        print(f"[检查错误] {core.redact(str(e))}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as e:  # noqa: BLE001 —— 未预期异常也要可读，不能甩 traceback
        print(f"[检查内部错误] {core.redact(str(e))}", file=sys.stderr)
        return EXIT_ERROR
    text = format_report(report)
    path = save_report("inspect", text)
    if path:
        note = f"[全量报告] {path}"
    elif args.json:
        note = "[报告未能落盘] 仅上面的 JSON 可用"
    else:
        note = "[报告未能落盘] 以上为完整内容"
    if args.json:
        print(json.dumps(core.redact_obj(report), ensure_ascii=False, indent=2))
        print(note, file=sys.stderr)
    else:
        print(text)
        print(f"\n{note}")
    if args.explain:
        # --json 时解读写 stderr：stdout 必须保持是合法 JSON
        stream = sys.stderr if args.json else sys.stdout
        print("\n## LLM 解读（可选，规则结论不变）\n", file=stream)
        try:
            print(_explain(text, args.model), file=stream)
        except Exception as e:  # noqa: BLE001 —— 模型/网络问题不能吞掉规则结果
            print(f"[LLM 解读不可用] {core.redact(str(e))}\n"
                  "规则检查结果与结论不受影响。", file=stream)
    return exit_code(report)
