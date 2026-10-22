#!/usr/bin/env python3
"""doctor：运维系统自身的体检（读为主，LLM API 与 SSH 各做一次最小探活）。

定位：opsx/opsagent 管的是"你的机器"，doctor 管的是"ops-agent 自己还能不能干活"。
每项检查给 ok / warn / fail + 证据 + 修复建议，按影响排序输出；任一 fail 时
退出码 1（可进 cron 或排障脚本）。

检查项：
1. LLM 配置：API key 存在（只显示尾 4 位）、provider/model/base_url。
2. LLM API 探活：GET models 端点（8s 超时）——区分鉴权失败/限流/网络不通/正常。
3. 调用预算：启用状态、今日用量与上限、是否已耗尽。
4. 状态目录：可写探针、磁盘余量、关键文件体积。
5. SSH 目标：逐台 ssh 探活（15s 上限，内部 ConnectTimeout 由 remote 控制），失败给出 stderr 摘要。
6. 调度心跳：daemon 是否在跑（过期即 fail——巡检可能已经停了）。
7. 通知渠道：配置状态与发件箱里的失败条数（不主动发送；发送测试用 notify test）。

已知边界：LLM 探活只验证"端点+密钥可用"，不验证模型名是否存在于该端点
（模型列表解析各家不一，误报比漏报更糟）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import remote  # noqa: E402

API_PROBE_TIMEOUT = 8
SSH_PROBE_TIMEOUT = 15   # 必须大于 remote 的 ConnectTimeout（默认 10s）
DISK_WARN_FREE_MB = 500

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "⚠", FAIL: "✗"}


def _item(name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": core.redact(detail), "fix": fix}


def _masked(key: str) -> str:
    if not key:
        return "未设置"
    return f"已设置（尾 4 位 …{key[-4:]}）" if len(key) > 4 else "已设置（过短，异常）"


# ---------------------------------------------------------------- 检查项

def _check_llm_config() -> list[dict]:
    key = os.environ.get("OPSAGENT_API_KEY", "")
    provider = os.environ.get("OPSAGENT_PROVIDER", "openai").strip().lower() or "openai"
    model = os.environ.get("OPSAGENT_MODEL", "") or \
        ("claude-sonnet-4-5" if provider == "anthropic" else "deepseek-chat")
    base = os.environ.get("OPSAGENT_BASE_URL", "") or \
        ("https://api.anthropic.com" if provider == "anthropic"
         else "https://api.deepseek.com")
    if not key:
        return [_item("llm-config", FAIL,
                      f"provider={provider} model={model} base={base}；API key 未设置",
                      "设置 OPSAGENT_API_KEY（定时巡检走 cron/daemon 时要在服务环境里带上）")]
    return [_item("llm-config", OK,
                  f"provider={provider} model={model} base={base}；key {_masked(key)}")]


def _probe_llm_api() -> list[dict]:
    key = os.environ.get("OPSAGENT_API_KEY", "")
    if not key:
        return [_item("llm-api", FAIL, "无 API key，跳过探活")]
    provider = os.environ.get("OPSAGENT_PROVIDER", "openai").strip().lower() or "openai"
    if provider == "anthropic":
        base = os.environ.get("OPSAGENT_BASE_URL", "https://api.anthropic.com").rstrip("/")
        url = base + ("/models" if base.endswith("/v1") else "/v1/models")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        base = os.environ.get("OPSAGENT_BASE_URL", "https://api.deepseek.com").rstrip("/")
        url = base + "/models"
        headers = {"Authorization": f"Bearer {key}"}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=API_PROBE_TIMEOUT) as resp:
            return [_item("llm-api", OK, f"GET {base}… 返回 {resp.status}")]
    except urllib.error.HTTPError as e:
        code = e.code
        if code in (401, 403):
            return [_item("llm-api", FAIL, f"鉴权失败（HTTP {code}）：key 无效或无权限",
                          "核对 OPSAGENT_API_KEY 与 provider 是否匹配；key 过期就换新")]
        if code == 429:
            return [_item("llm-api", WARN, f"限流中（HTTP 429）：端点可用但当前被拒",
                          "临时性：provider 会自动退避重试；持续限流考虑降低巡检频率或预算")]
        return [_item("llm-api", WARN, f"HTTP {code}：端点可达但返回异常",
                      "偶发可忽略；持续出现核对 base_url 是否正确")]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return [_item("llm-api", FAIL, f"网络不通: {type(e).__name__}: {e}",
                      "检查网络/代理（走 http_proxy/https_proxy；内网端点要设 no_proxy）"
                      "与 base_url")]


def _check_budget() -> list[dict]:
    try:
        import usage
        cfg = usage.load_budget()
        used = usage.today()
        reason = usage.check_budget()
        if not cfg["enabled"]:
            return [_item("budget", OK, f"预算已停用；今日 {used['calls']} 次调用")]
        calls_limit = cfg.get("daily_calls")
        tokens_limit = cfg.get("daily_tokens")
        detail = (f"今日 {used['calls']}/{calls_limit or '不限'} 次、"
                  f"{used['tokens']}/{tokens_limit or '不限'} token")
        if reason:
            return [_item("budget", FAIL, f"预算已耗尽：{detail}",
                          "调整上限: opsagent budget set --calls/--tokens；"
                          "LLM 解读暂停，规则检查照常")]
        return [_item("budget", OK, detail)]
    except core.OpsxError as e:
        return [_item("budget", FAIL, f"预算配置损坏: {e}",
                      "修 ~/.ops-agent/budget.json 或删除后 opsagent budget set 重建")]


def _check_state_dir() -> list[dict]:
    out = []
    d = core.STATE_DIR
    if not d.is_dir():
        return [_item("state-dir", FAIL, f"{d} 不存在", "任意 opsx 命令会自动创建")]
    probe = d / ".doctor-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as e:
        out.append(_item("state-dir", FAIL, f"{d} 不可写: {e}",
                         "检查目录权限（应 0700 且属当前用户）"))
        return out
    try:
        probe.unlink()
        out.append(_item("state-dir", OK, f"{d} 可写"))
    except OSError as e:
        out.append(_item("state-dir", WARN, f"{d} 可写但清理探针失败: {e}"))
    try:
        import subprocess
        r = subprocess.run(["df", "-P", "-k", str(d)], capture_output=True,
                           text=True, timeout=10)
        lines = r.stdout.strip().splitlines()
        free_kb = None
        for line in reversed(lines):
            fields = line.split()
            if len(fields) >= 4 and fields[3].isdigit():
                free_kb = int(fields[3])
                break
        if free_kb is not None and free_kb < DISK_WARN_FREE_MB * 1024:
            out.append(_item("disk", WARN,
                             f"状态目录所在磁盘余量 {free_kb // 1024}MB"
                             f"（低于 {DISK_WARN_FREE_MB}MB）",
                             "清理 ~/.ops-agent/reports 与 snapshots，或扩容"))
        elif free_kb is not None:
            out.append(_item("disk", OK, f"磁盘余量 {free_kb // 1024}MB"))
    except Exception:  # noqa: BLE001 —— df 不可用时跳过，不算故障
        pass
    return out


def _check_ssh_targets() -> list[dict]:
    try:
        targets = remote.load_targets()
    except remote.RemoteError as e:
        return [_item("ssh-targets", FAIL, f"targets.json 损坏: {e}")]
    names = sorted(n for n in targets if not n.startswith("_"))
    if not names:
        return [_item("ssh-targets", OK, "未注册远程目标")]
    out = []
    for name in names:
        # 探活超时要大于 remote 的 ConnectTimeout（默认 10s，目标可自定义更大），
        # 否则干净的"连接失败"会先以 TimeoutExpired 冒出来
        import subprocess
        try:
            r = remote._ssh(name, "true", timeout=SSH_PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            out.append(_item(f"ssh:{name}", FAIL,
                             f"连接超时（{SSH_PROBE_TIMEOUT}s 未响应）",
                             "目标可能宕机或网络不通；核对 targets.json 与防火墙"))
            continue
        except Exception as e:  # noqa: BLE001 —— 单台失败不拖垮其他台的诊断
            out.append(_item(f"ssh:{name}", FAIL,
                             f"探活异常: {type(e).__name__}: {e}",
                             "核对 targets.json 的 host/user/port 与本机密钥/agent"))
            continue
        if r.returncode == 0:
            out.append(_item(f"ssh:{name}", OK, "可连接"))
        else:
            stderr = r.stderr.decode("utf-8", "replace").strip().splitlines()
            out.append(_item(f"ssh:{name}", FAIL,
                             f"连接失败（exit {r.returncode}）: "
                             f"{(stderr[0] if stderr else '')[:160]}",
                             "检查目标在线、密钥、known_hosts、防火墙"))
    return out


def _check_heartbeat() -> list[dict]:
    try:
        from agent import schedule
        health = schedule.daemon_health()
    except Exception as e:  # noqa: BLE001
        return [_item("schedule-heartbeat", WARN,
                      f"无法读取心跳: {type(e).__name__}: {e}")]
    state = health["state"]
    if state == "fresh":
        return [_item("schedule-heartbeat", OK,
                      f"守护进程心跳正常（{health['age_seconds']}s 前）")]
    if state == "never":
        return [_item("schedule-heartbeat", WARN, "调度从未运行",
                      "需要定时巡检时: opsagent schedule run（或配 systemd/launchd/cron）")]
    if state == "stale":
        return [_item("schedule-heartbeat", FAIL,
                      f"心跳过期（{health['age_seconds']}s 未更新，"
                      f"阈值 {health['stale_after']}s）：定时巡检已停止",
                      "重启: opsagent schedule run；查 systemd/launchd 服务状态；"
                      "主机休眠后 cron 可能停摆")]
    return [_item("schedule-heartbeat", FAIL,
                  f"心跳无法判定: {health.get('error')}",
                  "修 ~/.ops-agent/schedules.json 后重启调度")]


def _check_notify() -> list[dict]:
    try:
        from agent import notify
    except Exception as e:  # noqa: BLE001
        return [_item("notify", WARN, f"通知模块不可用: {e}")]
    try:
        cfg = notify.load_config(required=False)
    except Exception as e:  # noqa: BLE001
        return [_item("notify", FAIL, f"通知配置损坏: {e}",
                      "修 ~/.ops-agent/notify.json 或 opsagent notify setup 重建")]
    if not cfg:
        return [_item("notify", WARN, "未配置通知渠道",
                      "opsagent notify setup --url <webhook>；不配则异常只在本地记录")]
    failed = notify.list_notifications(status="failed", tail=50)
    endpoint = notify.redact_endpoint(cfg.get("url") or "")
    detail = f"渠道 {cfg.get('format')} → {endpoint}"
    if not cfg.get("enabled", True):
        detail += "（已停用）"
    if failed:
        return [_item("notify", WARN, f"{detail}；发件箱 {len(failed)} 条失败待重试",
                      "opsagent notify retry；持续失败核对 webhook 是否失效")]
    return [_item("notify", OK, detail)]


# ---------------------------------------------------------------- 汇总

SECTIONS = (
    ("LLM", lambda: _check_llm_config() + _probe_llm_api() + _check_budget()),
    ("本机", _check_state_dir),
    ("远程", _check_ssh_targets),
    ("调度", _check_heartbeat),
    ("通知", _check_notify),
)


def run() -> tuple[list[tuple[str, list[dict]]], dict]:
    """跑全部检查，返回 ([(分组名, 结果列表)], 统计)。单个分组挂掉不影响其余。"""
    sections: list[tuple[str, list[dict]]] = []
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for title, fn in SECTIONS:
        try:
            items = fn()
        except Exception as e:  # noqa: BLE001 —— 一个分组挂了也要给出其余诊断
            items = [_item("internal", WARN,
                           f"检查项内部错误: {type(e).__name__}: {e}")]
        for item in items:
            counts[item["status"]] += 1
        sections.append((title, items))
    return sections, counts


def format_report(sections: list[tuple[str, list[dict]]], counts: dict) -> str:
    lines = ["# ops-agent doctor",
             f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
             f"｜状态目录: {core.STATE_DIR}",
             f"- 结论: 正常 {counts[OK]}｜警告 {counts[WARN]}｜异常 {counts[FAIL]}",
             ""]
    for title, items in sections:
        lines.append(f"## {title}")
        for item in items:
            line = f"- {_MARK[item['status']]} **{item['name']}**：{item['detail']}"
            if item.get("fix"):
                line += f"\n  修复: {item['fix']}"
            lines.append(line)
    fails = [item for _, items in sections for item in items if item["status"] == FAIL]
    if fails:
        lines.append("")
        lines.append("## 需要处理（按影响排序）")
        for item in fails:
            lines.append(f"- {item['name']}：{item['detail']}"
                         + (f"（{item['fix']}）" if item.get("fix") else ""))
    return "\n".join(lines)


def cli(argv: list[str] | None = None) -> int:
    sections, counts = run()
    print(format_report(sections, counts))
    return 1 if counts[FAIL] else 0
