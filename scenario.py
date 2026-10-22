#!/usr/bin/env python3
"""变更场景编排：预检 → 快照 → 执行 → 后验 → 回滚建议，全程关联 execution_id。

为什么需要这一层：`opsx exec` 只回答"命令退出码是多少"，回答不了"变更之后服务
真的健康吗"。`systemctl reload` 完全可能 exit=0 而服务已经不在了——退出码是命令
的，不是系统的。场景把"执行后的确定性验证"变成必经步骤：后验不通过就明确报
"验证失败"并给出回滚建议，而不是让人对着 exit=0 误以为一切正常。

场景定义在 scenarios/*.json（仓库内置）或 ~/.ops-agent/scenarios/*.json（同名覆盖）。
预检/后验探针强制 R0（探针不允许借参数升级成写操作）；执行命令照常走 core/remote
的守门与审批——**没有人工审批时场景不会写入任何东西**，这是验收硬门槛。

已知边界：后验只能验证"场景里写了的检查项"；没写的健康维度（日志错误、下游依赖）
仍然只有人能发现。后验通过不等于"本次变更正确"，只等于"检查项当前健康"。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import remote  # noqa: E402

PROBE_TIMEOUT = 30            # 单条预检/后验探针超时
REPORT_KEEP = 100             # 场景报告保留份数
EVIDENCE_LIMIT = 1200         # 单条证据保留字符数（完整输出在报告文件里）
MAX_CHECKS = 12               # 每段（预检/后验）最多检查项
MAX_PARAMS = 12
MAX_SNAPSHOT_FILES = 20

# 场景结论。verify_failed 是本模块存在的意义：命令 exit=0 但后验不通过。
VERDICTS = ("ok", "verify_failed", "exec_failed", "precheck_failed",
            "approval_required", "gate_denied", "busy", "uncertain")
VERDICT_LABEL = {
    "ok": "通过（后验全部健康）",
    "verify_failed": "验证失败（命令退出码为 0，但后验不健康）",
    "exec_failed": "执行失败",
    "precheck_failed": "预检未通过（未执行任何变更）",
    "approval_required": "需要人工审批（未执行任何变更）",
    "snapshot_failed": "快照未建成（命令未执行）",
    "gate_denied": "被守门拒绝（未执行任何变更）",
    "busy": "目标被占用（另一变更进行中，未执行）",
    "uncertain": "结果未知（执行记录待核实）",
}


class ScenarioError(core.OpsxError):
    """场景配置或参数错误（写错即报错，不静默忽略）。

    继承 OpsxError：opsx 主分发层 / agent 工具的 except 链都按 OpsxError 收口，
    单独开新基类会让配置错误以 traceback 形式漏到用户面前。
    """


# ---------------------------------------------------------------- 加载与校验

_DIR_CACHE: list[Path] | None = None


def dirs() -> list[Path]:
    """场景目录：用户状态目录覆盖仓库内置（与 workflows 同规则）。"""
    global _DIR_CACHE
    if _DIR_CACHE is None:
        _DIR_CACHE = [core.STATE_DIR / "scenarios", core.REPO_DIR / "scenarios"]
    return _DIR_CACHE


_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
# 替换后残留的单大括号占位符（{{...}} 模板大括号不会命中，见 substitute）
_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-z][a-z0-9_]{0,30})\}(?!\})")
_DEFAULT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,63}$"
_PATH_PATTERN = r"^/[A-Za-z0-9._@+/-]{1,200}$"
EXPECT_KEYS = ("expect", "expect_contains", "expect_not", "expect_not_contains",
               "expect_regex")


def catalog() -> list[dict]:
    """可用场景列表（损坏的定义也列出来，标 broken，不静默消失）。"""
    seen, out = set(), []
    for base in reversed(dirs()):  # 内置先扫，用户目录后扫并覆盖
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.json")):
            name = path.stem
            try:
                loaded = load(name)
            except ScenarioError as e:
                if name in seen:
                    continue
                seen.add(name)
                out.append({"name": name, "description": f"[定义损坏] {e}",
                            "params": [], "broken": True})
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "description": loaded["description"],
                        "params": sorted(loaded["params"]), "broken": False})
    return out


def load(name: str) -> dict:
    """加载并校验场景定义；任何字段写错都报 ScenarioError，不静默忽略。"""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        raise ScenarioError(f"场景名非法: {name!r}")
    path = None
    for base in dirs():
        candidate = base / f"{name}.json"
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        known = ", ".join(sorted(e["name"] for e in catalog() if not e["broken"])) or "无"
        raise ScenarioError(f"没有这个场景: {name!r}（可用: {known}；"
                            f"自定义放 {dirs()[0]}/*.json）")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ScenarioError(f"场景 {name} 无法读取: {e}") from e
    if not isinstance(raw, dict):
        raise ScenarioError(f"场景 {name} 的定义须为 JSON 对象")
    allowed = {"description", "params", "target", "precheck", "exec", "postcheck"}
    extra = sorted(set(raw) - allowed)
    if extra:
        raise ScenarioError(f"场景 {name} 有未知字段: {', '.join(extra)}"
                            f"（允许 {sorted(allowed)}）")

    description = raw.get("description") or name
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise ScenarioError(f"场景 {name} 的 description 须为不超过 300 字的字符串")

    params_raw = raw.get("params") or {}
    if not isinstance(params_raw, dict) or len(params_raw) > MAX_PARAMS:
        raise ScenarioError(f"场景 {name} 的 params 须为不超过 {MAX_PARAMS} 项的对象")
    params = {}
    for key, spec in params_raw.items():
        if not _PARAM_NAME_RE.fullmatch(str(key)):
            raise ScenarioError(f"场景 {name} 的参数名非法: {key!r}")
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ScenarioError(f"场景 {name} 的参数 {key} 的定义须为对象")
        extra_spec = sorted(set(spec) - {"required", "default", "pattern", "description"})
        if extra_spec:
            raise ScenarioError(f"场景 {name} 的参数 {key} 有未知字段: "
                                f"{', '.join(extra_spec)}")
        pattern = spec.get("pattern", _DEFAULT_PATTERN)
        if not isinstance(pattern, str) or len(pattern) > 200:
            raise ScenarioError(f"场景 {name} 的参数 {key} 的 pattern 须为不超过 "
                                "200 字符的正则")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ScenarioError(f"场景 {name} 的参数 {key} 的 pattern 编译失败: {e}") from e
        params[key] = {"required": bool(spec.get("required", False)),
                       "default": spec.get("default"), "pattern": pattern}

    target = raw.get("target")
    if target is not None:
        if not isinstance(target, str) or not target.strip():
            raise ScenarioError(f"场景 {name} 的 target 须为 'local'、已注册目标名或省略")
        target = target.strip()

    exec_raw = raw.get("exec")
    if not isinstance(exec_raw, dict) or not isinstance(exec_raw.get("cmd"), str) \
            or not exec_raw["cmd"].strip():
        raise ScenarioError(f"场景 {name} 缺少 exec.cmd（要执行什么变更）")
    extra_exec = sorted(set(exec_raw) - {"cmd", "snapshot_files", "snapshot_cmds"})
    if extra_exec:
        raise ScenarioError(f"场景 {name} 的 exec 有未知字段: {', '.join(extra_exec)}")
    snap_files = exec_raw.get("snapshot_files") or []
    if not isinstance(snap_files, list) or len(snap_files) > MAX_SNAPSHOT_FILES \
            or any(not isinstance(p, str) or not p.strip() for p in snap_files):
        raise ScenarioError(f"场景 {name} 的 exec.snapshot_files 须为不超过 "
                            f"{MAX_SNAPSHOT_FILES} 个非空字符串")
    snap_cmds = exec_raw.get("snapshot_cmds") or []
    if not isinstance(snap_cmds, list) or len(snap_cmds) > MAX_CHECKS:
        raise ScenarioError(f"场景 {name} 的 exec.snapshot_cmds 须为不超过 "
                            f"{MAX_CHECKS} 项的数组")
    for spec in snap_cmds:
        if not isinstance(spec, dict) or not isinstance(spec.get("command"), str) \
                or not spec["command"].strip():
            raise ScenarioError(f"场景 {name} 的 exec.snapshot_cmds 每项须含 command")

    return {
        "name": name,
        "description": description,
        "params": params,
        "target": target,
        "precheck": _norm_checks(name, raw.get("precheck"), "precheck"),
        "exec": {"cmd": exec_raw["cmd"], "snapshot_files": snap_files,
                 "snapshot_cmds": [{"command": spec["command"],
                                    "name": str(spec.get("name") or "")}
                                   for spec in snap_cmds]},
        "postcheck": _norm_checks(name, raw.get("postcheck"), "postcheck"),
    }


def _norm_checks(name: str, raw, what: str) -> list[dict]:
    """校验预检/后验检查项：cmd 必填，期望字段可编译。"""
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ScenarioError(f"场景 {name} 的 {what} 须为非空数组（没有就省略字段）")
    if len(raw) > MAX_CHECKS:
        raise ScenarioError(f"场景 {name} 的 {what} 最多 {MAX_CHECKS} 项")
    out = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not isinstance(item.get("cmd"), str) \
                or not item["cmd"].strip():
            raise ScenarioError(f"场景 {name} 的 {what}[{i}] 须为含 cmd 的对象")
        extra = sorted(set(item) - {"cmd", *EXPECT_KEYS})
        if extra:
            raise ScenarioError(f"场景 {name} 的 {what}[{i}] 有未知字段: "
                                f"{', '.join(extra)}（期望字段: {EXPECT_KEYS}）")
        check = {"cmd": item["cmd"]}
        for key in EXPECT_KEYS:
            value = item.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value or len(value) > 300:
                raise ScenarioError(f"场景 {name} 的 {what}[{i}].{key} 须为不超过 "
                                    "300 字符的字符串")
            if key == "expect_regex":
                try:
                    re.compile(value)
                except re.error as e:
                    raise ScenarioError(f"场景 {name} 的 {what}[{i}].expect_regex "
                                        f"编译失败: {e}") from e
            check[key] = value
        out.append(check)
    return out


def substitute(text: str, params: dict) -> str:
    """单遍替换 {param} 占位符。

    不用 str.format：docker 模板里合法地有大括号（{{.State.Status}}），format 会把
    它们当占位符炸掉；逐 token 替换只认参数名。替换后若还剩 {xxx} 形态的 token，
    说明场景里写了未定义的占位符——当配置错误报出来，而不是让带 "{typo}" 的命令
    去撞守门。
    """
    out = text
    for key in sorted(params, key=len, reverse=True):
        out = out.replace("{" + key + "}", str(params[key]))
    leftover = sorted({m.group(1) for m in _PLACEHOLDER_RE.finditer(out)})
    if leftover:
        raise ScenarioError("存在未定义的占位符: "
                            + ", ".join("{" + t + "}" for t in leftover))
    return out


def resolve_params(scen: dict, overrides: dict) -> dict:
    """合并默认值与显式参数，校验必填与 pattern。未知参数直接报错（防手滑打错）。"""
    if not isinstance(overrides, dict):
        raise ScenarioError("params 须为对象")
    unknown = sorted(set(overrides) - set(scen["params"]))
    if unknown:
        raise ScenarioError(f"未知参数: {', '.join(map(str, unknown))}"
                            f"（该场景的参数: {sorted(scen['params']) or '无'}）")
    out, missing = {}, []
    for key, spec in scen["params"].items():
        value = overrides.get(key, spec.get("default"))
        if value is None:
            if spec.get("required"):
                missing.append(key)
            continue
        value = str(value)
        pattern = spec.get("pattern", _DEFAULT_PATTERN)
        if not re.fullmatch(pattern, value):
            raise ScenarioError(f"参数 {key} 不符合约束 {pattern}: {value!r}")
        out[key] = value
    if missing:
        raise ScenarioError(f"缺少必填参数: {', '.join(missing)}"
                            f"（例: --param {missing[0]}=值）")
    return out


# ---------------------------------------------------------------- 探针

def _evaluate(check: dict, output: str) -> tuple[bool, str]:
    """按期望字段评估探针输出（多字段 AND；无期望字段 = 只要求 exit 0）。"""
    text = (output or "").strip()
    if "expect" in check and text != check["expect"]:
        return False, f"期望精确匹配 {check['expect']!r}，实际 {text[:120]!r}"
    if "expect_contains" in check and check["expect_contains"] not in output:
        return False, f"期望包含 {check['expect_contains']!r}，实际 {text[:120]!r}"
    if "expect_not" in check and text == check["expect_not"]:
        return False, f"期望不等于 {check['expect_not']!r}，但实际就是"
    if "expect_not_contains" in check and check["expect_not_contains"] in output:
        return False, f"期望不包含 {check['expect_not_contains']!r}，但出现了"
    if "expect_regex" in check and not re.search(check["expect_regex"], output):
        return False, f"期望匹配 /{check['expect_regex']}/，实际 {text[:120]!r}"
    return True, "符合预期"


def _probe(target: str | None, cmd: str, timeout: int = PROBE_TIMEOUT) -> tuple[int, str]:
    """跑一条只读探针。先过守门且必须 R0：探针命令借参数升级成写操作时，
    这里明确报"场景错误"而不是放行。"""
    level, _ = core.classify(cmd)
    if level != "R0":
        return 126, f"[场景错误] 预检/后验命令不是只读（{level}）: {core.redact(cmd)}"
    code, msg = core.check(cmd, target)
    if code != 0:
        return 126, f"[守门拒绝] {core.redact(str(msg))}"
    if target:
        try:
            r = remote._ssh(target, cmd, timeout=timeout)
        except (subprocess.TimeoutExpired, remote.RemoteError) as e:
            return 124, core.redact(f"[探针失败] {type(e).__name__}: {e}")
        output = r.stdout.decode("utf-8", "replace")
        if r.stderr.strip():
            output += "\n[stderr]\n" + r.stderr.decode("utf-8", "replace")
        return r.returncode, core.redact(output)[:EVIDENCE_LIMIT]
    try:
        r = core.run_shell(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"[探针超时] {timeout}s 未结束"
    except OSError as e:
        return 1, core.redact(f"[探针失败] {e}")
    output = r.stdout or ""
    if (r.stderr or "").strip():
        output += "\n[stderr]\n" + r.stderr
    return r.returncode, core.redact(output)[:EVIDENCE_LIMIT]


def _run_checks(checks: list[dict], params: dict, target: str | None) -> tuple[bool, list[dict]]:
    """跑一段检查（预检或后验），返回 (是否全部通过, 结构化结果)。"""
    results, all_ok = [], True
    for i, check in enumerate(checks, 1):
        cmd = substitute(check["cmd"], params)
        code, output = _probe(target, cmd)
        if code != 0:
            passed, detail = False, f"exit={code}: {output[:200]}"
        else:
            passed, detail = _evaluate(check, output)
        results.append({"n": i, "cmd": core.redact(cmd), "exit": code,
                        "passed": passed, "detail": detail, "output": output})
        if not passed:
            all_ok = False
    return all_ok, results


def _render_checks(results: list[dict]) -> list[str]:
    lines = []
    for r in results:
        mark = "✓" if r["passed"] else "✗"
        lines.append(f"- {mark} [{r['n']}] exit={r['exit']} `{r['cmd']}`"
                     + (f" — {r['detail']}" if r["detail"] else ""))
        if not r["passed"] and r.get("output"):
            excerpt = " / ".join(r["output"].splitlines()[:4])[:400]
            lines.append(f"  输出: {excerpt}")
    return lines


def _compact(results: list[dict]) -> list[dict]:
    """执行记录里保存的紧凑探针结果（给人看；完整输出在场景报告里）。"""
    return [{"cmd": r["cmd"], "exit": r["exit"], "passed": r["passed"],
             "detail": r["detail"][:200]} for r in results[:MAX_CHECKS]]


# ---------------------------------------------------------------- 报告

def _report_dir() -> Path:
    d = core.STATE_DIR / "reports"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def save_report(slug: str, text: str) -> Path | None:
    """场景报告落盘（0600，保留 100 份）；失败返回 None，不影响结论。"""
    try:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", slug or "scenario").strip("-")[:60] \
            or "scenario"
        path = _report_dir() / (time.strftime("%Y%m%d-%H%M%S") + f"-{safe}-"
                                f"{uuid.uuid4().hex[:6]}.md")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _prune_reports()
        return path
    except OSError:
        return None


def _prune_reports(keep: int = REPORT_KEEP) -> None:
    try:
        files = sorted(_report_dir().glob("*.md"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in files[:-keep]:
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 运行

def _exec_target(scen: dict, target_override: str | None) -> str | None:
    """合并场景声明的 target 与调用方 --target：显式传入优先，但固定目标不许改指向
    （防止把 A 机的场景打到 B 机）。"""
    target = target_override or scen["target"]
    if target in (None, "", "local"):
        return None
    if target_override and scen["target"] not in (None, "", "local") \
            and target_override != scen["target"]:
        raise ScenarioError(f"场景 {scen['name']} 固定在目标 {scen['target']}，"
                            f"不能再用 --target {target_override} 改指向")
    remote.get_target(target)  # 未注册目标名先报出来，别等到探针才炸
    return target


def run(name: str, params: dict | None = None, target: str | None = None,
        actor: str = "cli", lock_wait: float | None = None) -> dict:
    """跑一个场景：预检 → 快照 → 执行 → 后验 → 结论与回滚建议。

    守门/审批/锁/快照/执行记录全部由 core/remote.exec_change 负责，这里不复制
    任何安全逻辑。返回结构化结果；报告落 ~/.ops-agent/reports/。
    """
    from agent import events  # 延迟导入：事件时间线在 agent 包内
    scen = load(name)
    resolved = resolve_params(scen, params or {})
    tgt = _exec_target(scen, target)
    result = {
        "scenario": name, "description": scen["description"], "target": tgt or "local",
        "params": resolved, "verdict": None, "execution_id": None,
        "snapshot_id": None, "precheck": [], "postcheck": [], "report_path": None,
        "suggestion": "", "output": "",
    }

    # ---- 预检：失败即中止。不建快照、不碰审批、不产生执行记录——什么都没写。
    if scen["precheck"]:
        precheck_ok, result["precheck"] = _run_checks(scen["precheck"], resolved, tgt)
        if not precheck_ok:
            result["verdict"] = "precheck_failed"
            result["suggestion"] = ("预检未通过，未执行任何变更。先解决预检项"
                                    "（不要绕过预检直接跑命令）。")
            report = save_report(f"scenario-{name}", format_result(result, True))
            result["report_path"] = str(report) if report else None
            events.append("scenario-precheck-failed", scenario=name,
                          target=result["target"],
                          failed=[r["cmd"] for r in result["precheck"]
                                  if not r["passed"]])
            return result

    exec_cmd = substitute(scen["exec"]["cmd"], resolved)
    snap_files = [substitute(p, resolved) for p in scen["exec"]["snapshot_files"]]
    snap_cmds = [(substitute(spec["command"], resolved), spec["name"])
                 for spec in scen["exec"]["snapshot_cmds"]]

    # ---- 执行（含快照与守门/审批）
    exec_started = time.time()
    kwargs = {} if lock_wait is None else {"lock_wait": lock_wait}
    if tgt:
        code, sid, output = remote.exec_change(tgt, exec_cmd, snap_files, snap_cmds,
                                               actor=actor, **kwargs)
    else:
        code, sid, output = core.exec_change(exec_cmd, snap_files, snap_cmds,
                                             actor=actor, **kwargs)
    result["snapshot_id"] = sid
    result["output"] = core.redact(output or "")[:EVIDENCE_LIMIT]

    import execution
    record = execution.find_latest(exec_cmd, tgt, since=exec_started,
                                   pid=os.getpid(), kind="exec")
    exec_id = record["id"] if record is not None else None
    result["execution_id"] = exec_id
    if exec_id is not None:
        # 无论结论如何，把场景关联与预检证据先落到执行记录（失败不影响流程）
        fields = {"scenario": name, "scenario_params": resolved}
        if scen["precheck"]:
            fields["precheck"] = "passed"
        _attach(exec_id, **fields)

    status = record["status"] if record is not None else None

    # ---- 门禁分流：120/121/快照门 2 各有明确语义，不能都当"执行失败/需审批"
    if code == core.EXEC_ABORT_UNKNOWN and status in (None, "aborted"):
        result["verdict"] = "uncertain"
        result["suggestion"] = (
            "同目标同命令存在未核实的执行记录，系统不会自动重放（exit 120）。\n"
            + core.redact((output or "")[:400])
            + "\n先核实（opsx executions），不要重跑本场景。")
        _finalize(result, [], verify=None)
        return result
    if code == core.EXEC_ABORT_LOCK and status in (None, "aborted"):
        result["verdict"] = "busy"
        result["suggestion"] = ("目标正被另一次变更占用（exit 121），本次未执行、未写入。"
                                "等它结束再跑，或用 --wait-lock 等待。\n"
                                + core.redact((output or "")[:300]))
        _finalize(result, [], verify=None)
        return result
    if code == 2 and status in (None, "aborted"):
        # 区分"缺审批"与"守门永久拒绝"。判定信号用 check 本身的结果：
        # - check 放行（戳有效）但 exec_change 仍返回 2 → 是快照命令守门等其他拦截，
        #   重新批准解决不了（不能看"放行"话术里的"审批"二字——那会把快照门误判成缺审批）；
        # - check 拒绝且话术是"提交变更单/审批"类 → 批准可解；
        # - check 拒绝但话术不是审批类（敏感凭据拦截）→ 批准同样无解。
        gate_code, gate_msg = core.check(exec_cmd, tgt)
        approval_shaped = (gate_code != 0 and any(
            w in str(gate_msg) for w in ("提交变更单", "审批", "批准", "授权")))
        if approval_shaped:
            result["verdict"] = "approval_required"
            import shlex as _shlex
            level, _ = core.classify(exec_cmd)
            force_hint = ""
            if level == "R3":
                # R3 不可逆操作：approve 必须 --force，否则用户照做会撞第二个错误
                force_hint = " --force（不可逆操作，必须显式确认）"
            result["suggestion"] = (
                "变更需要人工审批，本次未执行、未写入。\n"
                + (f"守门原因: {core.redact(str(gate_msg))[:200]}\n" if gate_msg else "")
                + f"核对无误后执行: opsx approve {_shlex.quote(exec_cmd)}"
                + force_hint
                + (f" --target {tgt}" if tgt else "")
                + "\n然后重新运行本场景。")
        else:
            result["verdict"] = "gate_denied"
            result["suggestion"] = (
                "被守门拒绝（未执行、未写入；这不是重新批准能解决的问题，"
                "请核对场景里的快照命令或命令本身）。\n"
                + core.redact((output or str(gate_msg) or "")[:400]))
        return result

    if status == "unknown":
        # 后验照跑：只读证据帮助人核实，但结论必须以执行记录为准
        post_ok, result["postcheck"] = _run_checks(scen["postcheck"], resolved, tgt) \
            if scen["postcheck"] else (True, [])
        result["verdict"] = "uncertain"
        result["suggestion"] = (
            f"执行结果未知，后验仅作参考证据。先核实: opsx executions show {exec_id}"
            + ("\n" + _rollback_hint(sid) if sid else ""))
        _finalize(result, result["postcheck"] if scen["postcheck"] else [],
                  verify=None)
        return result

    if code == 3 and status in (None, "aborted"):
        # 快照没建成，命令从未下发——不能标"执行失败"，那意味着命令跑过且败了
        result["verdict"] = "snapshot_failed"
        result["suggestion"] = ("快照阶段失败，命令未执行，目标系统无改动。\n"
                                + core.redact((output or "")[:300]))
        _finalize(result, [], verify=None)
        return result
    if status != "succeeded":
        result["verdict"] = "exec_failed"
        result["suggestion"] = _rollback_hint(sid) if sid else \
            "命令执行失败，未产生需要回滚的快照。"
        _finalize(result, [], verify=None)
        return result

    # ---- 后验：命令 exit=0 也要验证系统真的健康（本模块存在的意义）
    post_ok, result["postcheck"] = _run_checks(scen["postcheck"], resolved, tgt) \
        if scen["postcheck"] else (True, [])
    if not post_ok:
        result["verdict"] = "verify_failed"
        failed = [r["cmd"] for r in result["postcheck"] if not r["passed"]]
        result["suggestion"] = (
            "命令退出码为 0，但后验不健康——不要据此认为变更成功。\n"
            + _rollback_hint(sid)
            + "\n失败的后验: " + ", ".join(failed)
            + "\n证据（命令 + 输出）已保留在报告与执行记录里，供人工判断。")
        _finalize(result, result["postcheck"], verify="failed")
        events.append("scenario-verify-failed", scenario=name, execution_id=exec_id,
                      target=result["target"], failed=failed)
        return result

    result["verdict"] = "ok"
    _finalize(result, result["postcheck"], verify="passed")
    return result


def _attach(execution_id: str, **fields) -> None:
    """把场景关联/探针结果回写执行记录；失败只说明记录缺装饰，不影响结论。"""
    import execution
    try:
        execution.update(execution_id, **fields)
    except Exception:  # noqa: BLE001 —— 装饰字段不能拖垮变更结论
        pass


def _finalize(result: dict, post_results: list[dict], verify: str | None) -> None:
    """后验结果回写执行记录 + 场景报告落盘。"""
    if result["execution_id"] is not None and post_results:
        _attach(result["execution_id"],
                postcheck=verify or ("passed" if all(r["passed"] for r in post_results)
                                     else "failed"))
    report = save_report(f"scenario-{result['scenario']}", format_result(result, True))
    result["report_path"] = str(report) if report else None


def _rollback_hint(sid: str | None) -> str:
    if not sid:
        return "（本次没有文件/状态快照，无自动回滚点；请按命令语义人工恢复。）"
    return (f"如需恢复到变更前: 先 opsx rollback {sid} 预览差异，"
            f"确认后 opsx rollback {sid} --yes。")


# ---------------------------------------------------------------- 回滚并验证

def rollback(execution_id: str, confirm: bool = False) -> dict:
    """按执行记录回滚：确认后恢复，再重跑后验作为"已恢复"的证据。

    人工确认由调用方负责（CLI --yes / agent ask_user）；回滚本身走
    core/remote.rollback 的锁、执行记录与原子恢复。
    """
    import execution
    from agent import events
    record = execution.get(execution_id)
    if record is None:
        raise ScenarioError(f"没有这条执行记录: {execution_id!r}")
    sid = record.get("snapshot_id")
    if not sid:
        raise ScenarioError(f"执行记录 {execution_id} 没有关联快照，没有可回滚的内容")
    target = record.get("target") or None
    scen = None
    if record.get("scenario"):
        try:
            scen = load(record["scenario"])
        except ScenarioError:
            scen = None  # 场景定义被删/改坏：回滚照做，只是没有后验可重跑

    rb_started = time.time()
    result_text = remote.rollback(sid, confirm=confirm) if target \
        else core.rollback(sid, confirm=confirm)
    rollback_record = execution.find_latest(f"rollback {sid}", target,
                                            since=rb_started, pid=os.getpid(),
                                            kind="rollback")
    result = {
        "scenario": record.get("scenario"), "execution_id": execution_id,
        "rollback_execution_id": rollback_record["id"] if rollback_record else None,
        "snapshot_id": sid, "target": target or "local", "verify": [],
        "verify_note": "", "report_path": None, "text": core.redact(result_text or ""),
    }

    # ---- 回滚后验证：重跑场景后验。默认只作证据不判定——回滚把配置恢复成旧版，
    # 变更后验（如"新配置语法 ok"）在回滚后失败可能正是生效的表现，自动判失败会误导。
    if scen is not None and scen["postcheck"]:
        raw_params = record.get("scenario_params")
        if raw_params is None:
            # 旧记录没记参数（或场景定义被改坏）：替换不了占位符，只能请人看回滚输出
            result["verify_note"] = ("（执行记录里没有场景参数，无法重跑后验，"
                                     "请按回滚输出人工确认）")
        else:
            try:
                resolved = resolve_params(scen, raw_params)  # {} 合法：场景可以没有参数
            except ScenarioError:
                resolved = None
            if resolved is None:
                result["verify_note"] = ("（场景参数与当前定义不匹配，无法重跑后验，"
                                         "请按回滚输出人工确认）")
            else:
                _, result["verify"] = _run_checks(scen["postcheck"], resolved, target)
                result["verify_note"] = ("（重跑的是变更后验，仅作回滚后状态的证据，不判定；"
                                         "失败可能正是回滚生效的表现）")
                if rollback_record is not None:
                    _attach(rollback_record["id"], postcheck="evidence")

    events.append("scenario-rolled-back", scenario=record.get("scenario"),
                  execution_id=execution_id,
                  rollback_execution_id=result["rollback_execution_id"],
                  snapshot_id=sid, target=result["target"])
    report = save_report(f"scenario-rollback-{sid}", _format_rollback(result))
    result["report_path"] = str(report) if report else None
    return result


# ---------------------------------------------------------------- 渲染与 CLI

def format_result(result: dict, with_suggestion: bool = False) -> str:
    label = VERDICT_LABEL.get(result["verdict"], result["verdict"] or "?")
    where = f"目标 {result['target']}" if result["target"] != "local" else "本机"
    lines = [
        f"# 场景 {result['scenario']} — {label}",
        f"- {where}｜参数: {result['params'] or '无'}",
        f"- 执行记录: {result['execution_id'] or '（未创建）'}"
        f"｜快照: {result['snapshot_id'] or '无'}",
    ]
    if result["precheck"]:
        lines.append("## 预检")
        lines += _render_checks(result["precheck"])
    if result["output"]:
        lines.append("## 执行输出（截断，完整见审计）")
        lines.append("```")
        lines.append(result["output"][:800])
        lines.append("```")
    if result["postcheck"]:
        lines.append("## 后验（命令退出后的健康验证）")
        lines += _render_checks(result["postcheck"])
    if with_suggestion and result.get("suggestion"):
        lines.append("## 建议")
        lines.append(result["suggestion"])
    if result.get("report_path"):
        lines.append(f"\n[完整报告] {result['report_path']}")
    return "\n".join(lines)


def _format_rollback(result: dict) -> str:
    lines = [
        f"# 场景回滚 {result['snapshot_id']}",
        f"- 场景: {result['scenario'] or '?'}｜原执行: {result['execution_id']}"
        f"｜回滚记录: {result['rollback_execution_id'] or '?'}",
        "## 回滚输出",
        "```",
        (result["text"] or "")[:1500],
        "```",
    ]
    if result["verify"]:
        lines.append("## 回滚后验证（证据，不判定）")
        lines += _render_checks(result["verify"])
    if result["verify_note"]:
        lines.append(result["verify_note"])
    return "\n".join(lines)
