"""opsx 核心库：纯函数，CLI 与未来 MCP server 共用。"""
from __future__ import annotations

import difflib
import hashlib
import fcntl
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("OPSX_STATE_DIR", str(Path.home() / ".ops-agent")))
REPO_DIR = Path(__file__).resolve().parent

_RANK = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
MAX_APPROVAL_USES = 100      # 单个审批戳最多可用次数（防止"一次批准、长期免检"）


class OpsxError(Exception):
    pass


class TargetBusy(OpsxError):
    """目标正被另一次变更占用。单独一类是为了让调用方区分"并发冲突"与其它错误：
    前者要用自己的退出码/提示（不是"你的命令有问题"）。"""


# ---- 秘密脱敏与敏感文件：模型请求、会话、审计、输出落盘共用同一出口 ----
# 只是降低外泄概率的启发式：不宣称能识别所有秘密（加密内容、二进制、自定义格式会漏）。

REDACTED = "[已脱敏]"
_PEM_KEY = re.compile(r"-----BEGIN[ A-Z0-9]*PRIVATE KEY-----.*?-----END[ A-Z0-9]*PRIVATE KEY-----",
                      re.S)
_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic|token)\s+)\S+")
_SECRET_NAME = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|credential|auth)")
_SECRET_KV = re.compile(
    r"(?i)([\"']?)\b([A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|private[_-]?key|credential|auth)[A-Za-z0-9_.-]*)\1"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})\b")

# 命中即视为凭据文件（read 直接拒绝；命令里出现即拒绝，避免内容进模型或落盘）。
_SENSITIVE_FILE_PATTERNS = [
    r"(?i)(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$",
    r"(?i)(?:^|/)\.env(?:\.(?!example$|sample$|template$)[^/\s]+)?$",
    r"(?i)(?:^|/)\.(?:netrc|pgpass|git-credentials|npmrc|pypirc|my\.cnf)$",
    r"(?i)(?:^|/)[^/\s]+\.(?:key|p12|pfx|jks|keystore)$",
    r"(?i)(?:^|/)[^/\s]*(?:priv|secret)[^/\s]*\.pem$",
    r"(?:^|/)\.ssh/(?:id_[^/\s]+|identity)$",
    r"(?i)(?:^|/)\.kube/config$",
    r"(?i)(?:^|/)\.aws/credentials$",
    r"(?i)(?:^|/)\.docker/config\.json$",
    r"(?:^|/)\.config/gh/hosts\.yml$",
    r"(?i)(?:^|/)ssh_host_[^/\s]*_key$",
    r"(?:^|/)etc/kubernetes/[^/\s]*\.conf$",
    r"(?i)(?:^|/)private/[^/\s]+\.pem$",
    r"(?:^|/)(?:shadow|gshadow)$",
    # 通知配置里的 webhook URL 本身就是凭据（Slack/钉钉的 path 即 token）：
    # 0600 挡不住同 uid 的模型进程，必须按敏感文件拦截读取。
    r"(?:^|/)\.ops-agent/notify\.json$",
]
# 敏感目录：read 拒绝；命令仅在"会打印内容"时拒绝（ls -l ~/.ssh 仍可用于确认权限）。
_SENSITIVE_DIR_PATTERNS = [
    r"(?i)(?:^|/)\.ssh(?:/|$)",
    r"(?i)(?:^|/)\.aws(?:/|$)",
    r"(?i)(?:^|/)\.gnupg(?:/|$)",
    r"(?i)(?:^|/)\.kube(?:/|$)",
    r"(?i)(?:^|/)\.config/gcloud(?:/|$)",
    r"(?:^|/)\.ops-agent/snapshots(?:/|$)",  # 默认状态目录：快照原件供恢复，不读进模型
    # 整个状态目录的递归读取会顺带扫到 notify.json（webhook 凭据）。只匹配目录本身，
    # ~/.ops-agent/reports/*.md 这类子路径不受影响（巡检报告仍要能读）。
    r"(?:^|/)\.ops-agent/?$",
]
# k8s Secret 值：默认表格/describe 不输出值，-o yaml/json/jsonpath 等会输出明文（base64）。
_KUBECTL_GET_SECRET = re.compile(r"\bkubectl\b.*?\bget\b.*?\bsecrets?\b", re.I)
_KUBECTL_VALUE_OUTPUT = re.compile(
    r"\s-o(?:=(?!name\b)\S+|\s+(?!name\b)\S+|(?![=\s]|name\b)\S+)"
    r"|\s--output(?:=|\s+)(?!name\b)\S+"
    r"|\s--template(?:=|\s+)\S+"
    r"|\s--go-template(?:-file)?(?:=|\s+)\S+", re.I)
_CONTENT_READERS = frozenset({
    "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "strings", "xxd",
    "od", "hexdump", "base64", "base32", "awk", "sed", "cut", "tr", "dd", "cp", "mv",
    "scp", "rsync", "tar", "zip", "gzip", "gunzip", "openssl", "ssh-keygen", "python",
    "python3", "perl", "ruby", "node", "bash", "sh", "zsh", "source", "curl", "wget",
    "vi", "vim", "nano",
})


def redact(text: str) -> str:
    """把常见凭据/私钥换成占位符；幂等，可重复用于同一段文本。"""
    if not isinstance(text, str) or not text:
        return text
    text = _PEM_KEY.sub(REDACTED + " 私钥", text)
    text = _AUTH_HEADER.sub(r"\1" + REDACTED, text)
    text = _SECRET_KV.sub(
        lambda m: m.group(1) + m.group(2) + m.group(1) + m.group(3) + REDACTED, text)
    return _TOKEN.sub(REDACTED, text)


def redact_obj(value):
    """会话记录脱敏：字符串脱敏，dict/list 递归复制（不改原对象）。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(v, str) and v and _SECRET_NAME.search(str(k))
                    else redact_obj(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value


def sensitive_path(path: str) -> bool:
    if not isinstance(path, str):
        return False
    real = os.path.realpath(os.path.expanduser(path))
    return (real == os.path.realpath(STATE_DIR / "notify.json") or
            any(re.search(p, path) for p in _SENSITIVE_FILE_PATTERNS))


def sensitive_dir(path: str) -> bool:
    if not isinstance(path, str):
        return False
    real = os.path.realpath(os.path.expanduser(path))
    snapshots = os.path.realpath(STATE_DIR / "snapshots")
    return (real == os.path.realpath(STATE_DIR) or real == snapshots or
            real.startswith(snapshots + os.sep) or
            _protected_state_copy(real) or
            any(re.search(p, path) for p in _SENSITIVE_DIR_PATTERNS))


def _protected_state_copy(path: str) -> bool:
    """备份和恢复副本含未脱敏原件，尚未切换状态目录时也保护读取出口。"""
    candidate = Path(path)
    for directory in (candidate, *candidate.parents):
        try:
            # manifest is published last; an interrupted backup still contains raw
            # credentials in state.tar and needs the same read protection.
            if (directory / "state.tar").is_file():
                return True
            if ((directory / ".ops-agent-recovered").is_file()
                    and directory != Path(os.path.realpath(STATE_DIR))):
                return True
        except OSError:
            continue
    return False


def _path_probes(cand: str) -> list[str]:
    """候选路径的等价写法：原样、词法归一、去掉 glob 段后的目录。

    `grep -r x ~/.ops-agent/.`、`~/.ops-agent/*`、`~/.ops-agent/n*` 指向同一目录，
    只按原始字符串匹配会被绕过；这里把候选归一后再判。
    """
    out = []
    for value in (cand, os.path.expandvars(cand)):
        if not value:
            continue
        norm = os.path.normpath(value)
        for probe in (value, norm):
            if probe not in out:
                out.append(probe)
        parts = norm.split("/")
        while parts and any(ch in parts[-1] for ch in "*?["):
            parts.pop()
        trimmed = "/".join(parts) or "/"
        if trimmed not in out:
            out.append(trimmed)
    return out


def sensitive_command(cmd: str, strict: bool = False) -> str | None:
    """命令入口的敏感文件限制：返回命中的路径，命中则整条命令拒绝。

    保守策略：引用凭据文件一律拒绝；敏感目录只有会打印内容的命令才拒绝。
    strict=True 用于远程命令：本机无法 stat 目标机文件，裸文件名也按敏感处理。
    """
    if not isinstance(cmd, str):
        return None
    for seg in _segments(cmd):
        if _KUBECTL_GET_SECRET.search(seg) and _KUBECTL_VALUE_OUTPUT.search(seg):
            return "kubectl get secret -o yaml/json（禁止输出 Secret 值）"
        if re.search(r"\bkubectl\b.*\bconfig\s+view\b.*\s--raw(?:=true)?(?:\s|$)", seg):
            return "kubectl config view --raw（禁止输出集群凭据）"
        try:
            lexer = shlex.shlex(seg, posix=True, punctuation_chars="<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            words = list(lexer)
        except ValueError:
            words = seg.split()
        if not words:
            continue
        if not strict and Path(words[0]).name == "ssh" and len(words) > 1:
            inner = sensitive_command(words[-1], strict=True)  # ssh host "cat id_rsa"
            if inner:
                return inner
        readers = any(Path(w).name in _CONTENT_READERS for w in words)
        skip_next = False
        for w in words:
            if skip_next:  # find -name id_rsa / --kubeconfig path：参数不是被读取的文件
                skip_next = False
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
                continue  # 环境变量赋值不是读取文件（KUBECONFIG=... kubectl get）
            if w.startswith(("--kubeconfig=", "-kubeconfig=")):
                continue  # kubectl 读 kubeconfig 输出的是集群信息，不是文件内容
            if w in ("-name", "-iname", "--name", "--kubeconfig", "-kubeconfig"):
                skip_next = True
                continue
            cand = w.split("=", 1)[1] if w.startswith("-") and "=" in w else w
            cand = cand.strip("'\"")
            if not cand or cand.startswith("-"):
                continue
            # 裸文件名可能是搜索词（grep id_rsa app.log）：只有确实指向当前目录里的
            # 敏感文件时才拒绝；带路径分隔符/~/点前缀的一律按路径处理。
            if "/" not in cand and "~" not in cand and not os.path.isfile(
                    os.path.expanduser(cand)) and not strict:
                continue
            for probe in _path_probes(cand):
                if sensitive_path(probe):
                    return cand
                expanded = os.path.expanduser(probe)
                real = os.path.realpath(expanded)
                if real != probe and sensitive_path(real):
                    return cand  # 符号链接/相对路径最终指向凭据文件
                if readers and (sensitive_dir(probe) or sensitive_dir(real)):
                    return cand
                    # 目录规则也要看 realpath：cd 到软链目录再 cat 同样不该放行
    return None


def validate_arguments(value, schema: dict, path: str = "参数") -> None:
    types = {"string": str, "integer": int, "boolean": bool,
             "object": dict, "array": list}
    kind = schema.get("type")
    if kind in types and (not isinstance(value, types[kind]) or
                          (kind == "integer" and isinstance(value, bool))):
        raise OpsxError(f"{path} 须为 {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise OpsxError(f"{path} 须为 {schema['enum']} 之一")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise OpsxError(f"缺少参数: {path}.{key}")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                validate_arguments(value[key], child, f"{path}.{key}")
    elif kind == "array":
        for i, child in enumerate(value):
            validate_arguments(child, schema.get("items", {}), f"{path}[{i}]")


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
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as e:
        raise OpsxError(f"配置读取失败 {path}: {e}") from e
    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip()
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith(":") and not s.startswith("- "):
            section = s[:-1]
            if section not in ("readonly_patterns", "risk_rules", "R1", "R2", "R3"):
                raise OpsxError(f"配置段非法 {path}:{lineno}: {section}")
            continue
        if s.startswith("- ") and section:
            item = s[2:].strip()
            try:
                re.compile(item)
            except re.error as e:
                raise OpsxError(f"配置正则非法 {path}:{lineno}: {e}") from e
            if section == "readonly_patterns":
                cfg["readonly_patterns"].append(item)
            elif section in ("R1", "R2", "R3"):
                cfg["risk_rules"][section].append(item)
            else:
                raise OpsxError(f"配置条目位置非法 {path}:{lineno}")
        else:
            raise OpsxError(f"配置语法非法 {path}:{lineno}: {s}")


def classify(cmd: str) -> tuple[str, str | None]:
    """复合命令分段取最高风险级。返回 (级别, 命中的最高风险分段)。"""
    if not isinstance(cmd, str) or not cmd.strip(" \t\n"):
        raise OpsxError("命令须为非空字符串")
    config = load_config()
    ssh_level = _classify_ssh(cmd.strip(" \t\n"))
    if ssh_level is not None:
        return ssh_level, cmd if ssh_level != "R0" else None
    level, matched = "R0", None
    for seg in _segments(cmd):
        seg_level = _classify_single(seg, config)
        if _RANK[seg_level] > _RANK[level]:
            level, matched = seg_level, seg
    return level, matched


def _classify_ssh(cmd: str) -> str | None:
    m = re.fullmatch(r'ssh[ \t]+([A-Za-z0-9_@.\[\]:][A-Za-z0-9_@.\[\]:-]*)[ \t]+"([^"\\]+)"[ \t]*', cmd)
    if not m:
        return None
    inner = m[2]
    if re.search(r"(?:^|[;&|\n])\s*env\b", inner):
        return "R2"  # 远程环境可能带生产密钥，不进入只读白名单
    return classify(inner)[0]


def _has_write_marker(seg: str) -> bool:
    """白名单形状但藏写行为时不按只读放行。

    覆盖：重定向（仅 stderr 到 /dev/null 或 fd 复制豁免）、`&>`全量重定向、
    tee、find -exec/-delete、命令替换（`$(...)`/反引号，可在只读命令里藏任意写操作）。
    """
    if re.search(r"\btee\b|-(?:exec|execdir|ok|okdir|delete|fprint|fprint0|fprintf|fls)\b", seg):
        return True
    if "$(" in seg or "`" in seg or "<(" in seg or ">(" in seg:
        return True
    if re.search(r"&>{1,2}", seg):
        return True
    if re.search(r"--(?:output|open-files-in-pager|rotate|vacuum-\S+|flush|sync|relinquish-var|smart-relinquish-var|setup-keys|update-catalog)(?:\s|=|$)", seg):
        return True
    if re.search(r"^docker\s+compose\s+config\b.*\s-o(?:\s|\S)", seg):
        return True
    for m in re.finditer(r"(\d*)>{1,2}\s*(&[0-9]+|/dev/null(?=\s|$))?", seg):
        if m.group(1) != "2" or m.group(2) is None:
            return True
    return False


def _classify_single(seg: str, config: dict) -> str:
    ssh_level = _classify_ssh(seg)
    if ssh_level is not None:
        return ssh_level
    # KUBECONFIG=... 只改变选中的集群，不改变执行的二进制：覆盖默认 kubeconfig 的只读排查照常放行。
    # 写标记仍看原始段：赋值里的 $(...) 不能在剥离后被忘掉。
    raw = seg
    seg = re.sub(r"^KUBECONFIG=[^\s]+[ \t]+", "", seg)
    for pat in config["readonly_patterns"]:
        if re.search(pat, seg):
            if _has_write_marker(raw):
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


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)


def audit(event: str, **fields) -> None:
    _ensure_state_dir()
    path = STATE_DIR / "audit.jsonl"
    # 常规记录同样脱敏：审计留证据，不长期保存明文密钥/私钥。
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event,
           **{k: redact_obj(v) for k, v in fields.items()}}
    with open(STATE_DIR / "audit.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and path.stat().st_size > AUDIT_ROTATE_BYTES:
            os.replace(path, STATE_DIR / "audit.jsonl.1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _safe_audit(event: str, **fields) -> None:
    """变更路径的审计写入：失败不打断已完成的变更，但必须让人看见。

    为什么不能直接用 audit：一次 exec 已经执行完，此时 audit 因磁盘满抛 OSError，
    会把"执行完成"变成"调用方收到异常、看不到输出"，进而可能被当成"没执行"重跑。
    """
    try:
        audit(event, **fields)
    except OSError as e:
        print(f"[opsx] 警告：审计写入失败（{type(e).__name__}: {e}）——"
              "本次变更状态以 executions 记录为准，但缺审计留痕", file=sys.stderr)


def normalize(cmd: str) -> str:
    if re.search(r"['\"\\\n\r]", cmd):
        return cmd  # 引号、转义及换行中的空白会改变 shell 语义
    return re.sub(r"[ \t]+", " ", cmd.strip(" \t"))


# ---- 审批绑定的实际执行环境 ----
# 同一命令在不同主机 / 目录 / k8s context / KUBECONFIG 下语义不同：批准后环境变化即失效。

_SEGMENT_RE = re.compile(r"&&|\|\||[;|\n]|(?<!>)&(?!>)")
_KUBECTL_SEG_RE = re.compile(r"(?:^|[\s/\"'])kubectl(?=[\s\"']|$)")


def _segments(cmd: str) -> list[str]:
    """复合命令分段（只按 shell 分隔符切，不解析 AST）。"""
    return [seg.strip(" \t") for seg in _SEGMENT_RE.split(cmd) if seg.strip(" \t")]


def _strip_comment(seg: str) -> str:
    """去掉 shell 注释：`kubectl apply … # --context=prod` 里的 context 不会被执行。"""
    return re.sub(r"(?:^|\s)#[^\n]*$", "", seg).strip(" \t")


def _kubectl_segments(cmd: str) -> list[str]:
    return [seg for seg in (_strip_comment(s) for s in _segments(cmd or ""))
            if seg and _KUBECTL_SEG_RE.search(seg)]


_KUBECTL_FLAGS_WITH_VALUE = frozenset({
    "-n", "--namespace", "--context", "--kubeconfig", "-o", "--output", "--cluster",
    "--user", "--server", "--token", "--as", "--as-group", "--request-timeout", "-s",
    "--certificate-authority", "--client-certificate", "--client-key", "--cache-dir",
    "-f", "--filename", "-k", "--kustomize", "-l", "--selector", "--field-selector",
    "--field-manager", "--dry-run", "--timeout", "--grace-period", "--replicas",
    "--revision", "--image", "--type", "-p", "--patch", "--patch-file",
})
_KUBECTL_READ_VERBS = frozenset({"get", "describe", "logs", "top", "api-resources",
                                 "explain", "version", "cluster-info"})


def _kubectl_read_only(seg: str) -> bool:
    """kubectl 段的动词是否只读（用于 --context 要求与审核展示）。"""
    try:
        words = shlex.split(seg)
    except ValueError:
        words = seg.split()
    for i, w in enumerate(words):
        if Path(w).name != "kubectl":
            continue
        j = i + 1
        while j < len(words):
            w = words[j]
            if w in _KUBECTL_FLAGS_WITH_VALUE:
                j += 2
                continue
            if w.startswith("-"):
                j += 1
                continue
            if w == "auth":
                return j + 1 < len(words) and words[j + 1] in ("can-i", "whoami")
            return w in _KUBECTL_READ_VERBS
    return False


def _kubectl_scope_values(seg: str) -> tuple[list[str], list[str]]:
    """只采集 kubectl 的实际 argv 参数，字符串内容不能冒充作用域选项。"""
    try:
        words = shlex.split(seg, comments=True)
    except ValueError:
        return [], []
    start = next((i + 1 for i, word in enumerate(words)
                  if Path(word).name == "kubectl"), None)
    if start is None:
        # sh -c 的内层脚本不属于已解析的 argv，不能据此声称绑定了实际 context。
        return [], []
    contexts, namespaces = [], []
    i = start
    while i < len(words):
        word = words[i]
        if word == "--":
            break
        if word in ("--context", "--namespace", "-n"):
            values = contexts if word == "--context" else namespaces
            values.append(words[i + 1] if i + 1 < len(words) else "")
            i += 2
            continue
        if word.startswith("--context="):
            contexts.append(word.partition("=")[2])
        elif word.startswith("--namespace="):
            namespaces.append(word.partition("=")[2])
        elif word.startswith("-n") and len(word) > 2:
            namespaces.append(word[2:].removeprefix("="))
        elif word in _KUBECTL_FLAGS_WITH_VALUE:
            i += 2
            continue
        i += 1
    return contexts, namespaces


def k8s_scope(cmd: str) -> dict:
    segments = _kubectl_segments(cmd)
    changes = [seg for seg in segments if not _kubectl_read_only(seg)]
    for seg in changes or segments:
        ctx, ns = _kubectl_scope_values(seg)
        return {"context": ctx[-1] if ctx else "",
                "namespace": ns[-1] if ns else ""}
    return {"context": "", "namespace": ""}


def local_env() -> dict:
    return {"scope": "local", "host": socket.gethostname() or "localhost",
            "cwd": os.path.realpath(os.getcwd()),
            "kubeconfig": os.environ.get("KUBECONFIG", "")}


def resolve_env(cmd: str, target: str | None = None) -> dict:
    if target:
        import remote  # 延迟导入：remote 依赖 core，模块级导入会成环
        env = remote.target_env(target)
    else:
        env = local_env()
    if _kubectl_segments(cmd or ""):
        env["k8s"] = k8s_scope(cmd)
    return env


def format_env(env: dict) -> str:
    if env.get("scope") == "remote":
        user = f"{env['user']}@" if env.get("user") else ""
        line = f"目标 {env.get('target')}（{user}{env.get('host')}:{env.get('port')}）"
    else:
        line = f"本机 {env.get('host')}，工作目录 {env.get('cwd')}"
    k8s = env.get("k8s") or {}
    if k8s.get("context"):
        line += f"，k8s context={k8s['context']}"
        if k8s.get("namespace"):
            line += f" namespace={k8s['namespace']}"
    return line


def cmd_hash(cmd: str, target: str | None = None) -> str:
    """审批戳文件名键。远程目标参与哈希：在 A 机器批准的命令不能拿到 B 机器执行。"""
    key = f"{target}\n{normalize(cmd)}" if target else normalize(cmd)
    return hashlib.sha256(key.encode()).hexdigest()


def _validate_k8s_change_scope(cmd: str) -> None:
    kubectl = [seg for seg in _kubectl_segments(cmd) if not _kubectl_read_only(seg)]
    scopes = [_kubectl_scope_values(seg) for seg in kubectl]
    contexts = [context for context, _ in scopes]
    if any(not values or any(not value or any(ch in value for ch in "$`\\")
                             for value in values) for values in contexts):
        raise OpsxError("kubectl 变更须在命令中显式指定非空、无变量展开的 --context："
                        "审批绑定实际 context，不能用当前默认 context 执行")
    if len({value for values in contexts for value in values}) > 1:
        raise OpsxError("同一命令里的 kubectl 段使用了不同 --context：请拆开分别审批执行")
    if any(not value or any(ch in value for ch in "$`\\")
           for _, namespaces in scopes for value in namespaces):
        raise OpsxError("kubectl 变更的 namespace 须为非空、无变量展开的固定值")


# ---- 目标级写锁：同一目标同一时刻只允许一个变更在执行 ----
# 为什么需要：快照→执行→(下一步)验证这条链路不是原子的，两个会话交错执行时，
# 后一个的快照可能已经包含前一个的改动，"回滚到快照"就回到一个中间态。
# 锁按目标划分（本机算一个目标）：不同目标的变更互不阻塞，同一目标必须串行。
_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
LOCAL_TARGET = "local"
LOCAL_LOCK_FILE = "@local"
DEFAULT_LOCK_WAIT = 0.0      # 默认不等待：并发写入立即失败，比两边互等更可诊断


def _lock_name(target: str | None, *, env: dict | None = None) -> str:
    """锁文件名。本机用 `@local` 而不是 `local`：`local` 是合法目标名，
    否则"本机"与"名为 local 的远程目标"会抢同一把锁，互相误判占用。"""
    if target is None:
        return LOCAL_LOCK_FILE
    if not isinstance(target, str) or not _TARGET_RE.fullmatch(target):
        raise OpsxError(f"非法的锁目标名: {target!r}（1-64 位字母数字._-）")
    config = env
    if config is None:
        import remote
        try:
            config = remote.get_target(target)
        except remote.RemoteError:
            return target  # 未注册名称仍可供本地锁诊断使用。
    endpoint = json.dumps([str(config.get("host") or target).lower().rstrip("."),
                           str(config.get("port") or 22)])
    return "ssh-" + hashlib.sha256(endpoint.encode()).hexdigest()


def _lock_path(target: str | None, *, env: dict | None = None) -> Path:
    return STATE_DIR / "locks" / f"{_lock_name(target, env=env)}.lock"


def _validate_lock_args(target: str | None, wait) -> None:
    """锁相关参数的校验，单独暴露给 exec_change 提前调用。

    否则 `--wait-lock -1` 这类参数错误会发生在记录已经建好、锁还没拿到的时候，
    被记成一条"结果未知"，并要求人去核实一条根本没下发的命令。
    """
    _lock_name(target)
    if type(wait) not in (int, float) or not math.isfinite(wait) or wait < 0:
        raise OpsxError("锁等待秒数须为非负数值")


def _read_lock_owner(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096).decode("utf-8", "replace").strip()
        info = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        return ""
    if not isinstance(info, dict):
        return ""
    bits = []
    if info.get("execution_id"):
        bits.append(f"execution {info['execution_id']}")
    if info.get("pid"):
        bits.append(f"pid {info['pid']}")
    if info.get("actor"):
        bits.append(f"来源 {info['actor']}")
    return "，".join(bits)


@contextmanager
def target_lock(target: str | None, wait: float = DEFAULT_LOCK_WAIT,
                actor: str = "", execution_id: str | None = None):
    """目标级排他锁。拿不到就抛 OpsxError（不做静默等待）。

    wait：最多等待秒数。默认 0 = 立即失败，错误里带持有者信息，让人能判断是
    "另一个会话正在改"还是"上次崩了留下的锁"（flock 随进程退出自动释放，
    所以后者不会发生，残留文件不代表被锁）。
    """
    _validate_lock_args(target, wait)
    path = _lock_path(target)
    _ensure_state_dir()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = [path]
    if target is not None and path.name != f"{target}.lock":
        # 同时持有旧版按名称创建的锁，升级期间不能绕过仍在执行的旧进程。
        import remote
        aliases = {target}
        for alias in remote.load_targets():
            if _TARGET_RE.fullmatch(alias) and _lock_name(alias) == path.stem:
                aliases.add(alias)
        paths += [path.with_name(f"{alias}.lock") for alias in sorted(aliases)]
    name = target or LOCAL_TARGET
    deadline = time.monotonic() + wait
    handles = []
    try:
        for candidate in paths:
            fd = os.open(candidate, os.O_RDWR | os.O_CREAT, 0o600)
            held = [fd, False]
            handles.append(held)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held[1] = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
            if not held[1]:
                owner = _read_lock_owner(fd)
                detail = f"（当前持有者：{owner}）" if owner else ""
                raise TargetBusy(
                    f"目标 {name} 正在被另一次变更占用{detail}：拒绝并发写入。"
                    "等它结束后重试；flock 随进程退出自动释放，残留的锁文件本身不代表被占用。")
        payload = json.dumps({"pid": os.getpid(), "target": name, "actor": actor,
                              "execution_id": execution_id,
                              "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False)
        for fd, _ in handles:
            try:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                data = payload.encode("utf-8")
                if os.write(fd, data) != len(data):
                    raise OSError("目标锁持有者信息写入不完整")
            except OSError as e:
                raise OpsxError(f"无法记录目标锁持有者，已中止变更: {e}") from e
        _safe_audit("target-lock", target=name, actor=actor, execution_id=execution_id)
        yield
    finally:
        for fd, acquired in reversed(handles):
            try:
                if acquired:
                    try:
                        os.ftruncate(fd, 0)
                    except OSError:
                        pass
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        if any(acquired for _, acquired in handles):
            _safe_audit("target-unlock", target=name, actor=actor, execution_id=execution_id)


def approve(cmd: str, ttl_seconds: int = 900, force: bool = False,
            target: str | None = None, env: dict | None = None,
            uses: int = 1) -> Path:
    if type(ttl_seconds) is not int or ttl_seconds <= 0:
        raise OpsxError("审批 TTL 须为正整数秒")
    if type(force) is not bool:
        raise OpsxError("force 须为布尔值")
    if type(uses) is not int or not 1 <= uses <= MAX_APPROVAL_USES:
        raise OpsxError(f"审批可用次数须为 1-{MAX_APPROVAL_USES} 的整数（默认 1：一次一变）")
    level, _ = classify(cmd)
    if level == "R0":
        raise OpsxError("只读命令无需审批戳")
    if level == "R3" and not force:
        raise OpsxError("R3 命令需 --force 显式确认（不可逆操作）")
    if level == "R3" and uses != 1:
        # 不可逆操作不接受"一次批准、多次执行"：重复生效的代价无法回滚
        raise OpsxError("R3 不可逆操作的审批只能使用 1 次（uses 必须为 1）")
    env = env or resolve_env(cmd, target)
    _validate_k8s_change_scope(cmd)
    stamp = {
        "cmd_hash": cmd_hash(cmd, target),
        "cmd": cmd,
        "target": target,
        "env": env,
        "created": time.time(),
        "ttl": ttl_seconds,
        "force": force,
        "level": level,
        "method": "manual",
        # 次数受控：默认一次一变。uses>1 是给"必须重试同一命令"的场景（如幂等部署脚本），
        # 每次都必须在执行时消耗，不能在预检时消耗。
        "uses": uses,
        "used": 0,
    }
    p = STATE_DIR / "approvals" / (stamp["cmd_hash"] + ".json")
    _ensure_state_dir()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 与 consume_stamp 共用 approvals 的 flock 并原子替换：否则并发的"消耗"会用旧内容
    # 覆盖刚写入的新授权（新 uses 丢失、used 回退），或让 reader 读到半截 JSON。
    with open(p.parent / ".mutex", "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(stamp, out, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    prune_approvals()  # 顺带清理过期戳
    _safe_audit("approve", cmd=cmd, level=level, ttl=ttl_seconds, force=force, target=target,
          env=env, uses=uses)
    return p


def _stamp_uses(stamp: dict) -> tuple[int, int]:
    """(允许次数, 已用次数)。旧戳没有这两个字段：按"还剩一次未用"处理。"""
    uses = stamp.get("uses", 1)
    used = stamp.get("used", 0)
    if type(uses) is not int or not 1 <= uses <= MAX_APPROVAL_USES:
        return 0, 0
    if type(used) is not int or used < 0:
        return 0, 0
    return uses, used


def _stamp_valid(stamp, cmd: str, target: str | None, env: dict | None) -> bool:
    """审批戳是否仍然可用（只读判断，不消耗次数）。

    check_stamp 与 consume_stamp 共用同一份判定：分成两套逻辑时，
    迟早会出现"预检通过、执行时却按另一套规则拒绝/放行"的不一致。
    """
    if not isinstance(stamp, dict) or not isinstance(stamp.get("cmd"), str):
        return False
    uses, used = _stamp_uses(stamp)
    return (_stamp_live(stamp) and used < uses and
            stamp.get("cmd_hash") == cmd_hash(cmd, target) and
            normalize(stamp.get("cmd") or "") == normalize(cmd) and
            stamp.get("target") == target and stamp.get("env") == env and
            stamp.get("level") == classify(cmd)[0] and
            (stamp.get("level") != "R3" or (stamp.get("force") is True and uses == 1)))


def check_stamp(cmd: str, target: str | None = None, env: dict | None = None) -> bool:
    p = STATE_DIR / "approvals" / (cmd_hash(cmd, target) + ".json")
    if not p.exists():
        return False
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        _validate_k8s_change_scope(cmd)
        if env is None:
            env = resolve_env(cmd, target)
        return _stamp_valid(s, cmd, target, env)
    except Exception:  # noqa: BLE001 —— 环境解析或戳内容任何异常都按无效处理（安全方向）
        return False


def consume_stamp(cmd: str, target: str | None = None,
                  env: dict | None = None) -> int | None:
    """原子地消耗一次审批，返回剩余次数；戳无效/已用完返回 None。

    必须由真正执行变更的路径调用（exec_change / remote.exec_change），且要在
    "确定要执行"之后：放在 check/hook 预检里会把戳提前用掉，用户什么都没改成，
    授权却没了。整个过程在 approvals 的 flock 内完成，两个进程同时消耗不会双花。
    """
    p = STATE_DIR / "approvals" / (cmd_hash(cmd, target) + ".json")
    _ensure_state_dir()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    remaining = None
    with open(p.parent / ".mutex", "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            if not p.exists():
                return None
            try:
                stamp = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            try:
                _validate_k8s_change_scope(cmd)
                current_env = resolve_env(cmd, target)
            except Exception:  # noqa: BLE001 —— 含 RemoteError：解析不出环境就不放行
                return None
            if env is not None and env != current_env:
                return None
            env = current_env
            if not _stamp_valid(stamp, cmd, target, env):
                return None
            uses, used = _stamp_uses(stamp)
            stamp["used"] = used + 1
            tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(stamp, out, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            remaining = uses - stamp["used"]
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    _safe_audit("approve-consume", cmd=cmd, target=target, remaining=remaining)
    return remaining


def stamp_exhausted(cmd: str, target: str | None = None) -> bool:
    """戳还在有效期内，但可用次数已用完：给"请重新批准"而不是"没有审批"。"""
    p = STATE_DIR / "approvals" / (cmd_hash(cmd, target) + ".json")
    if not p.exists():
        return False
    try:
        stamp = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(stamp, dict) or not _stamp_live(stamp):
            return False
        uses, used = _stamp_uses(stamp)
        return used >= uses
    except (OSError, ValueError):
        return False


def _stamp_live(stamp) -> bool:
    if not isinstance(stamp, dict):
        return False
    created, ttl = stamp.get("created"), stamp.get("ttl")
    return (type(created) in (int, float) and type(ttl) in (int, float) and
            math.isfinite(created) and math.isfinite(ttl) and ttl > 0 and
            created <= time.time() < created + ttl)


def prune_approvals() -> None:
    """清理已过期的审批戳。"""
    adir = STATE_DIR / "approvals"
    if not adir.exists():
        return
    with open(adir / ".mutex", "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        for p in adir.glob("*.json"):
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
                if not _stamp_live(s):
                    p.unlink()
            except (json.JSONDecodeError, KeyError, TypeError, OSError, UnicodeError, OverflowError):
                p.unlink(missing_ok=True)  # 损坏文件一并清掉


def revoke(cmd: str, target: str | None = None) -> bool:
    """撤销审批戳（用户取消变更/提前失效）。返回是否确有戳被删除。"""
    p = STATE_DIR / "approvals" / (cmd_hash(cmd, target) + ".json")
    _ensure_state_dir()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 与 approve/consume 共用同一把锁：否则"消耗"在锁内已读到戳，撤销删掉后它又把
    # 文件写回去（used+1），撤销被静默推翻。
    with open(p.parent / ".mutex", "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            existed = p.exists()
            p.unlink(missing_ok=True)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    _safe_audit("revoke", cmd=cmd, target=target, existed=existed)
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
                if not isinstance(st, dict):
                    continue
                uses, used = _stamp_uses(st)
                if (not _stamp_live(st) or not isinstance(st.get("cmd"), str)
                        or not isinstance(st.get("env"), dict) or used >= uses):
                    continue  # 已用尽的戳不是"可用授权"，列出来会让人误判还有额度
                if st.get("level") not in _RANK or st.get("target") is not None and not isinstance(st["target"], str):
                    continue
                approvals.append({"cmd": st["cmd"], "level": st["level"], "ttl": st["ttl"],
                                  "target": st.get("target")})
            except (json.JSONDecodeError, KeyError, TypeError, OSError, UnicodeError, OverflowError):
                continue
    return {"snapshots": snapshots, "approvals": approvals}


def format_state(state: dict) -> list[str]:
    """快照/审批戳摘要（opsx list 与 agent ops_list 共用，避免两处格式漂移）。"""
    lines = []
    if state["snapshots"]:
        lines += ["== snapshots =="] + state["snapshots"]
    if state["approvals"]:
        lines.append("== approvals ==")
        lines += [f"{redact(a['cmd'])[:80]}  (level={a['level']}, ttl={a['ttl']}s, "
                  f"{'target=' + a['target'] if a.get('target') else '本机'})"
                  for a in state["approvals"]]
    return lines


def stamp_env_mismatch(cmd: str, target: str | None = None,
                       env: dict | None = None) -> bool:
    """戳存在但执行环境不同：给用户明确的“重新批准”提示，而不是当成没有戳。"""
    p = STATE_DIR / "approvals" / (cmd_hash(cmd, target) + ".json")
    if not p.exists():
        return False
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(s, dict) or not isinstance(s.get("env"), dict):
            return False
        return s["env"] != (env if env is not None else resolve_env(cmd, target))
    except Exception:  # noqa: BLE001 —— 解析失败按无提示处理
        return False


def check(cmd: str, target: str | None = None, env: dict | None = None) -> tuple[int, str]:
    level, matched = classify(cmd)
    hit = sensitive_command(cmd, strict=bool(target))
    if hit:
        audit("check", cmd=cmd, level=level, result="deny-sensitive", target=target, path=hit)
        return 2, redact(f"[opsx] 拒绝执行：命令涉及敏感凭据/Secret（{hit}），"
                         "密钥/私钥/Secret 值不读取、不写入。确认文件存在与权限请用 ls -l；"
                         "确需读取请由人直接操作。")
    if level == "R0":
        audit("check", cmd=cmd, level=level, result="allow", target=target)
        return 0, "只读命令，放行"
    if check_stamp(cmd, target, env):
        audit("check", cmd=cmd, level=level, result="allow-stamp", target=target)
        return 0, "有效审批戳，放行"
    if stamp_exhausted(cmd, target):
        audit("check", cmd=cmd, level=level, result="deny-exhausted", target=target)
        return 2, ("[opsx] 审批戳已用尽（该授权只允许有限次执行）：请重新批准本次变更。"
                   "一次批准对应一次执行，避免旧授权被反复复用。")
    if stamp_env_mismatch(cmd, target, env):
        audit("check", cmd=cmd, level=level, result="deny-env", target=target)
        return 2, ("[opsx] 审批戳的执行环境已变化（主机/工作目录/KUBECONFIG/k8s context 不同），"
                   "请重新批准后再执行。")
    audit("check", cmd=cmd, level=level, result="deny", target=target)
    hit = f"（命中分段：{matched}）" if matched else ""
    return 2, redact(
        f"[opsx] 拒绝执行：该命令判定为 {level}{hit}。"
        "请先向用户提交变更单（风险说明+回滚计划），经确认后使用 opsx exec 执行。"
    )


def check_snapshot_cmd(cmd: str, target: str | None = None) -> tuple[int, str]:
    if classify(cmd)[0] != "R0":
        _safe_audit("snapshot-deny", cmd=cmd, target=target, reason="抓取命令不是只读")
        return 2, "[opsx] 快照抓取仅允许 R0 只读命令；变更请使用 opsx exec（审批、写锁与执行记录）。"
    return check(cmd, target)


def _new_snapshot_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def begin_snapshot() -> tuple[str, Path]:
    _ensure_state_dir()
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


def run_shell(cmd: str, timeout: int) -> subprocess.CompletedProcess:
    """超时终止整个进程组，避免 shell 退出后子进程继续变更。"""
    with subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace",
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _snapshot_output_name(name: str, items: list) -> str:
    base = f"cmd-{(name or 'output').replace('/', '_')}"
    fname, number = base + ".txt", 2
    while any(item.get("output") == fname for item in items):
        fname = f"{base}-{number}.txt"
        number += 1
    return fname


def _snapshot_cmd_into(d: Path, cmd: str, name: str, items: list) -> None:
    code, msg = check_snapshot_cmd(cmd)
    if code != 0:
        raise OpsxError(msg)
    r = run_shell(cmd, timeout=60)
    if r.returncode != 0:
        raise OpsxError(f"状态抓取命令失败({r.returncode}): {cmd}\n{r.stderr[:500]}")
    fname = _snapshot_output_name(name, items)
    (d / fname).write_text(r.stdout, encoding="utf-8")
    items.append({"type": "cmd", "cmd": cmd, "name": name, "output": fname})


def _write_meta(d: Path, sid: str, items: list, **extra) -> None:
    meta = {"id": sid, "created": datetime.now(timezone.utc).isoformat(), "items": items,
            **extra}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot_file(path_str: str) -> str:
    sid, d = begin_snapshot()
    items: list = []
    try:
        _snapshot_file_into(d, path_str, items)
        _write_meta(d, sid, items)
    except (OpsxError, OSError) as e:
        shutil.rmtree(d, ignore_errors=True)
        raise OpsxError(f"文件快照失败: {e}") from e
    audit("snapshot", id=sid, kind="file", src=path_str)
    prune_snapshots()
    return sid


def snapshot_cmd(cmd: str, name: str = "") -> str:
    code, msg = check_snapshot_cmd(cmd)
    if code != 0:
        raise OpsxError(msg)
    sid, d = begin_snapshot()
    items: list = []
    try:
        _snapshot_cmd_into(d, cmd, name, items)
        _write_meta(d, sid, items)
    except (OpsxError, OSError, subprocess.SubprocessError) as e:
        shutil.rmtree(d, ignore_errors=True)
        raise OpsxError(f"命令快照失败: {e}") from e
    audit("snapshot", id=sid, kind="cmd", cmd=cmd)
    prune_snapshots()
    return sid


_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def snapshot_dir(snapshot_id: str) -> Path:
    """快照目录。id 来自模型/用户：允许路径分隔符就能让 rollback 读写任意目录。"""
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise OpsxError(f"非法快照 id: {snapshot_id!r}")
    d = STATE_DIR / "snapshots" / snapshot_id
    if d.is_symlink():
        raise OpsxError(f"快照目录不能为符号链接: {snapshot_id}")
    return d


def load_snapshot(snapshot_id: str) -> tuple[Path, dict]:
    d = snapshot_dir(snapshot_id)
    if not d.is_dir():
        raise OpsxError(f"快照不存在: {snapshot_id}")
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or meta.get("id") != snapshot_id or not isinstance(meta.get("items"), list):
            raise ValueError("元数据形状非法")
        if meta.get("target") is not None and not isinstance(meta["target"], str):
            raise ValueError("快照目标非法")
        for item in meta["items"]:
            if not isinstance(item, dict) or item.get("type") not in ("file", "cmd"):
                raise ValueError("快照条目非法")
            key = "dest" if item["type"] == "file" else "output"
            archived = Path(item[key])
            if archived.is_absolute() or not (d / archived).is_file():
                raise ValueError("快照文件不存在或路径非法")
            (d / archived).resolve().relative_to(d.resolve())
            if item["type"] == "file":
                if not isinstance(item["src"], str) or not item["src"].startswith("/") or "\x00" in item["src"]:
                    raise ValueError("源文件路径须为绝对路径")
                if item.get("mode") is not None and (type(item["mode"]) is not int
                                                     or not 0 <= item["mode"] <= 0o7777):
                    raise ValueError("文件权限非法")
                if item.get("owner") is not None and not isinstance(item["owner"], str):
                    raise ValueError("属主记录非法")
                if "resolved_path" in item and (not isinstance(item["resolved_path"], str)
                        or not item["resolved_path"].startswith("/") or "\x00" in item["resolved_path"]):
                    raise ValueError("真实文件路径须为绝对路径")
            elif not isinstance(item.get("cmd"), str):
                raise ValueError("快照命令非法")
        return d, meta
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise OpsxError(f"快照元数据损坏: {snapshot_id}: {e}") from e


DIFF_LIMIT = 4000  # 回滚预览里的差异展示上限（按字符）


def _local_read_state(src: str) -> tuple[bool, bytes]:
    p = Path(src)
    try:
        if not p.is_file():
            return False, b""
        return True, p.read_bytes()
    except OSError as e:
        raise OpsxError(f"回滚前读取当前文件失败 {src}: {e}") from e


def _diff_text(current: bytes, archived: bytes, src: str, snapshot_id: str) -> str:
    try:
        cur = redact(current.decode("utf-8"))
        old = redact(archived.decode("utf-8"))
    except UnicodeDecodeError:
        return "（二进制文件，差异从略）"
    if "\x00" in cur or "\x00" in old:
        return "（二进制文件，差异从略）"
    diff = "\n".join(difflib.unified_diff(
        cur.splitlines(), old.splitlines(), fromfile=f"当前 {src}",
        tofile=f"快照 {snapshot_id}", lineterm="", n=2))
    if not diff:
        return "（内容与快照一致）" if current == archived else "（内容有变化，脱敏后的文本预览无差异）"
    return diff if len(diff) <= DIFF_LIMIT else diff[:DIFF_LIMIT] + "\n...[差异已截断]"


def rollback_plan(snapshot_id: str, read_state=None) -> dict:
    """回滚预览：实际目标、将覆盖的文件、当前内容与快照内容的差异。

    read_state(src) -> (exists, bytes)：远程快照由 remote.rollback_plan 注入 SSH 读取。
    """
    d, meta = load_snapshot(snapshot_id)
    read = read_state or _local_read_state
    plan = {"snapshot_id": snapshot_id, "target": meta.get("target"),
            "host": (meta.get("env") or {}).get("host"), "files": [], "commands": []}
    for item in meta["items"]:
        if item["type"] == "file":
            try:
                archived = (d / item["dest"]).read_bytes()
            except OSError as e:
                raise OpsxError(f"回滚预览读取快照失败 {item['src']}: {e}") from e
            try:
                exists, current = read(item["src"])
            except (OpsxError, OSError) as e:
                raise OpsxError(f"回滚预览读取当前文件失败 {item['src']}: {e}") from e
            plan["files"].append({
                "src": item["src"], "exists": exists, "mode": item.get("mode"),
                "diff": _diff_text(current, archived, item["src"], snapshot_id),
            })
        else:
            out = (d / item["output"]).read_text(encoding="utf-8", errors="replace")
            plan["commands"].append({"name": item.get("name") or item["cmd"],
                                     "output": redact(out)[:DIFF_LIMIT]})
    return plan


def format_rollback_plan(plan: dict) -> str:
    where = f"目标 {plan['target']}" if plan.get("target") else "本机"
    if plan.get("target") and plan.get("host"):
        where += f"（{plan['host']}）"
    lines = [f"回滚计划：快照 {plan['snapshot_id']}（{where}）"]
    if plan["files"]:
        lines.append(f"将恢复 {len(plan['files'])} 个文件（执行前自动保存回滚前副本）：")
        for i, f in enumerate(plan["files"], 1):
            if not f["exists"]:
                state = "当前不存在，回滚将新建"
            elif f["mode"]:
                state = f"当前存在，权限 {f['mode']:04o}"
            else:
                state = "当前存在"
            lines.append(f"{i}. {f['src']}（{state}）")
            lines.extend("   " + line for line in f["diff"].splitlines())
    if plan["commands"]:
        lines.append("捕获状态（只读展示）：")
        lines += [f"[{c['name']}]\n{c['output']}" for c in plan["commands"]]
    return "\n".join(lines)


def _rollback_backup(original_sid: str, target: str | None, file_items: list,
                     read_state, mode_of=None, meta_extra: dict | None = None,
                     resolved_of=None) -> str | None:
    """回滚前副本：把即将被覆盖的当前内容另存为快照，后续修改不会静默丢失。

    没有任何文件被覆盖（当前都不存在）时返回 None，不创建空快照。
    """
    sid, d = begin_snapshot()
    items: list = []
    try:
        for item in file_items:
            exists, data = read_state(item["src"])
            if not exists:
                continue  # 当前不存在：回滚是新建，没有可覆盖的内容
            dest = d / "files" / str(item["src"]).lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            arch = {"type": "file", "src": item["src"],
                    "dest": str(dest.relative_to(d))}
            if mode_of is not None:
                arch["mode"] = mode_of(item["src"])
            if resolved_of is not None:
                arch["resolved_path"] = resolved_of(item["src"])
            items.append(arch)
        if not items:
            shutil.rmtree(d, ignore_errors=True)
            return None
        _write_meta(d, sid, items, rollback_of=original_sid, target=target,
                    **(meta_extra or {}))
    except OSError as e:
        shutil.rmtree(d, ignore_errors=True)
        raise OpsxError(f"回滚前副本保存失败: {e}") from e
    except BaseException:  # 含 Ctrl-C：清掉半成品目录后原样抛出
        shutil.rmtree(d, ignore_errors=True)
        raise
    _safe_audit("rollback-backup", id=sid, rollback_of=original_sid, target=target,
          files=len(items))
    return sid


def rollback(snapshot_id: str, confirm: bool = False) -> str:
    """恢复本机快照。confirm=False 时只做只读的事（拒绝文件写入，返回捕获状态除外）。

    回滚本身是写操作：入口（CLI/agent/MCP）展示 rollback_plan 并由人确认后再传 confirm。
    回滚也走目标锁与执行记录：并发回滚会互相覆盖，失败/中断必须留下"恢复了几个文件"。
    """
    import execution
    d, meta = load_snapshot(snapshot_id)
    if meta.get("target"):
        raise OpsxError("远程快照须通过 remote.rollback 恢复")
    file_items = [item for item in meta["items"] if item["type"] == "file"]
    if file_items and not confirm:
        raise OpsxError("回滚会覆盖当前文件，属于写操作：请先预览并由人确认"
                        "（CLI: opsx rollback <id>；agent: ops_rollback 会展示差异并确认）")
    if file_items:
        code, message = _check_pending_unknown(f"rollback {snapshot_id}", None)
        if code:
            raise OpsxError(message)
    rec = execution.begin("rollback", f"rollback {snapshot_id}", None,
                          local_env(), snapshot_id=snapshot_id, phase="lock")
    try:
        with target_lock(None, actor="rollback", execution_id=rec["id"]):
            if file_items:
                code, message = _check_pending_unknown(f"rollback {snapshot_id}", None)
                if code:
                    execution.finish(rec["id"], "aborted", error=message, phase="lock")
                    raise OpsxError(message)
            execution.update(rec["id"], phase="backup")
            backup_sid = None
            report = []
            restored = 0
            restoring = False
            try:
                if file_items:
                    backup_sid = _rollback_backup(
                        snapshot_id, None, file_items, _local_read_state,
                        lambda src: stat.S_IMODE(Path(src).stat().st_mode))
                for item in meta["items"]:
                    if item["type"] == "file":
                        staged = None
                        try:
                            try:
                                current_stat = Path(item["src"]).stat()
                            except FileNotFoundError:
                                current_stat = None
                            with tempfile.NamedTemporaryFile(dir=Path(item["src"]).parent,
                                                             delete=False) as f:
                                staged = Path(f.name)
                            shutil.copy2(d / item["dest"], staged)
                            if current_stat is not None:
                                staged_stat = staged.stat()
                                if (staged_stat.st_uid, staged_stat.st_gid) != (
                                        current_stat.st_uid, current_stat.st_gid):
                                    # 原子 rename 会换 inode；须先还原已有文件的属主/组，
                                    # 不能把服务账户的配置静默变成执行 opsx 的账户所有。
                                    os.chown(staged, current_stat.st_uid, current_stat.st_gid)
                            mode = item.get("mode")
                            if mode is None:
                                mode = (stat.S_IMODE(current_stat.st_mode)
                                        if current_stat is not None else 0o600)
                            os.chmod(staged, mode)
                            execution.update(rec["id"], phase="restore")
                            restoring = True
                            os.replace(staged, item["src"])
                        except OSError as e:
                            raise OpsxError(f"文件回滚失败 {item['src']}: {e}") from e
                        finally:
                            if staged is not None:
                                staged.unlink(missing_ok=True)
                        report.append(f"已恢复文件: {item['src']}")
                        restored += 1
                    else:
                        out = (d / item["output"]).read_text(encoding="utf-8", errors="replace")
                        report.append(f"捕获状态 [{item.get('name') or item['cmd']}]:\n{out}")
            except OpsxError as e:
                _safe_audit("rollback-failed", snapshot_id=snapshot_id,
                      backup_snapshot_id=backup_sid, error=str(e), execution_id=rec["id"])
                # 部分恢复必须说清楚：只说"失败"会让人以为文件没动
                error = f"{e}（已恢复 {restored}/{len(file_items)} 个文件）"
                execution.finish(rec["id"], "failed" if restoring else "aborted", error=error)
                if backup_sid:
                    raise OpsxError(f"{error}（回滚前副本: {backup_sid}）") from e
                raise
            except BaseException:
                execution.finish(
                    rec["id"], "unknown" if restoring else "aborted",
                    error=f"回滚被中断，已恢复 {restored}/{len(file_items)} 个文件"
                          + ("，需核实" if restoring else "，命令未执行"))
                raise
            if backup_sid:
                report.append(f"回滚前副本: {backup_sid}"
                              f"（如需撤销本次回滚: opsx rollback {backup_sid}）")
            execution.finish(rec["id"], "succeeded", output="\n".join(report))
    except TargetBusy as e:
        execution.finish(rec["id"], "aborted", error=str(e), phase="lock")
        raise
    _safe_audit("rollback", snapshot_id=snapshot_id, backup_snapshot_id=backup_sid,
          execution_id=rec["id"], files=[item["src"] for item in file_items])
    execution.prune()
    return "\n".join(report)


def prune_snapshots(keep: int = 200) -> None:
    if type(keep) is not int or keep < 0:
        raise OpsxError("快照保留数须为非负整数")
    d = STATE_DIR / "snapshots"
    if not d.exists():
        return
    import execution
    protected = set()
    for path in (STATE_DIR / "executions").glob("*.json"):
        rec = execution._read(path)
        if rec is None:
            return  # 损坏的执行记录可能引用唯一恢复证据，先人工核实再清理。
        if rec["status"] == "running" or (rec["status"] == "unknown" and not rec.get("resolved")):
            if isinstance(rec.get("snapshot_id"), str):
                protected.add(rec["snapshot_id"])
    ids = sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink())
    backups = {}
    for path in ids:
        try:
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and isinstance(meta.get("rollback_of"), str):
            backups[path.name] = meta["rollback_of"]
    while True:
        linked = {sid for sid, original in backups.items() if original in protected} - protected
        if not linked:
            break
        protected.update(linked)
    for p in ids[:-keep] if keep else ids:
        if p.name in protected:
            continue
        shutil.rmtree(p)
        audit("prune", snapshot_id=p.name)


EXEC_TIMEOUT = 3600          # 变更命令默认最长 1 小时
EXEC_AUDIT_OUTPUT = 2000     # 审计里保留的输出字符数
# 退出码：0 成功 / 1 执行失败 / 2 无授权或守门拒绝 / 3 快照失败 /
# 4 超时（本机已终止；远程是否仍在跑见执行记录）/ 120 有未核实的 unknown 挡着 /
# 121 目标正被另一次变更占用。120/121 只描述"本次没执行"，不描述结果。
# 不使用 5/6：命令自身的退出码也可能是 5 或 6（rsync/curl/systemctl/自定义脚本都有），
# 那样调用方会把"命令真的失败了"误读成"没执行"。120/121 在运维工具里罕见。
EXEC_ABORT_UNKNOWN = 120
EXEC_ABORT_LOCK = 121


def _check_pending_unknown(cmd: str, target: str | None = None,
                           env: dict | None = None) -> tuple[int, str]:
    """所有写入口共用的防重放门禁；回滚以 snapshot_id 标识恢复动作。"""
    import execution
    blocked = execution.pending_unknown(target, cmd_hash(cmd, target),
                                        env=env or resolve_env(cmd, target), command=cmd)
    if blocked is None:
        return 0, ""
    where = f"目标 {target}" if target else "本机"
    return EXEC_ABORT_UNKNOWN, (
        f"[opsx] 拒绝执行：{where} 上有一条未核实的执行 {blocked['id']}"
        f"（{blocked.get('error') or '无法确认命令是否完成'}）。\n"
        "命令可能已经生效一部分，自动重放会造成重复生效。请先核实实际状态，再决定：\n"
        f"  opsx executions show {blocked['id']}\n"
        f"  opsx executions resolve {blocked['id']} --observed-ok|--observed-failed")


def _prepare_exec(cmd: str, target: str | None = None) -> tuple[int, str]:
    """执行前的统一门禁：守门 + 同目标同命令是否有未核实的 unknown。

    "不自动重放"落在这里：只要上一条执行还是 unknown（可能改了一半），就不放行
    同一条命令再跑一次。核实由人做（opsx executions resolve），不提供自动动作。
    """
    code, msg = check(cmd, target)
    if code != 0:
        return code, msg
    return _check_pending_unknown(cmd, target)


def exec_change(
    cmd: str,
    snapshot_files: list[str] | None = None,
    snapshot_cmds: list[tuple[str, str]] | None = None,
    timeout: int = EXEC_TIMEOUT,
    actor: str = "cli",
    lock_wait: float = DEFAULT_LOCK_WAIT,
) -> tuple[int, str | None, str]:
    """原子执行：守门→锁→重验→快照→消耗审批→执行→记录状态。

    返回 (exit_code, snapshot_id, output)。输出一律 capture（调用方自行展示），
    同时写审计（截断）并落一条 execution 记录（含状态与证据）。

    锁覆盖"快照 + 执行"整段而不是只锁执行：两个会话交错时，后一个的快照可能
    已经包含前一个的改动，"回滚到快照"就回到一个中间态。
    """
    import execution
    snapshot_files = snapshot_files or []
    snapshot_cmds = snapshot_cmds or []
    code, msg = _prepare_exec(cmd, None)
    if code != 0:
        # 不在这里打印：调用方（CLI / agent 工具 / MCP）都会展示返回的 msg，
        # 再打一遍就等于同一个错误出现两次。
        return code, None, msg
    for c, _ in snapshot_cmds:  # 快照命令本身也过守门：否则可把写操作藏进抓取参数
        code, _ = check_snapshot_cmd(c)
        if code != 0:
            _safe_audit("exec-abort", cmd=cmd, reason=f"快照命令未通过守门: {c}")
            output = redact(f"[opsx] 快照命令未通过守门，已中止执行: {c}")
            print(output, file=sys.stderr)
            return 2, None, output

    _validate_lock_args(None, lock_wait)   # 参数错误要在建记录之前就失败
    rec = execution.begin("exec", cmd, None, resolve_env(cmd, None), phase="lock")
    sid = None
    dispatched = False   # 命令是否真的下发过：决定中断时记 unknown 还是 aborted
    try:
        with target_lock(None, wait=lock_wait, actor=actor, execution_id=rec["id"]):
            env = resolve_env(cmd, None)
            execution.update(rec["id"], env=env, host=env.get("host"))
            code, msg = check(cmd, None, env)  # 锁内重验：等锁期间审批可能过期/被撤销
            if code != 0:
                _safe_audit("exec-abort", cmd=cmd, reason="等锁后审批失效")
                execution.finish(rec["id"], "aborted", error=msg, phase="lock")
                return code, None, msg
            code, msg = _prepare_exec(cmd, None)  # 锁内再查一次 unknown：等锁期间可能新增
            if code != 0:
                execution.finish(rec["id"], "aborted", error=msg, phase="lock")
                return code, None, msg
            if snapshot_files or snapshot_cmds:
                execution.update(rec["id"], phase="snapshot")
                sid, d = begin_snapshot()
                items: list = []
                try:
                    for f in snapshot_files:
                        _snapshot_file_into(d, f, items)
                    for c, name in snapshot_cmds:
                        _snapshot_cmd_into(d, c, name, items)
                    _write_meta(d, sid, items)
                except (OpsxError, subprocess.SubprocessError, OSError) as e:
                    shutil.rmtree(d, ignore_errors=True)
                    output = redact(f"[opsx] 快照失败，已中止执行: {e}")
                    print(output, file=sys.stderr)
                    _safe_audit("exec-abort", cmd=cmd, reason=str(e))
                    execution.finish(rec["id"], "aborted", error=output, phase="snapshot")
                    return 3, None, output
                execution.update(rec["id"], snapshot_id=sid, phase="authorize")
            remaining = None
            if classify(cmd)[0] != "R0":
                # 只有需要审批的变更才消耗次数：R0 只读命令没有戳，也不该被要求有
                remaining = consume_stamp(cmd, None, env)
                if remaining is None:
                    # 审批在"确定要执行"的这一刻才消耗；已经失效就不该再跑
                    output = redact("[opsx] 审批戳已失效或已用尽，已中止执行（未做任何变更）")
                    _safe_audit("exec-abort", cmd=cmd, snapshot_id=sid, reason="审批戳已失效")
                    execution.finish(rec["id"], "aborted", error=output, phase="exec")
                    return 2, sid, output
            del remaining  # 剩余次数只对审批路径有意义，已在 consume_stamp 审计里留痕
            execution.update(rec["id"], phase="exec")
            dispatched = True   # 从这一刻起命令可能已经生效
            exit_code, status, output, err = _run_change(cmd, timeout)
            execution.finish(rec["id"], status, exit_code=exit_code, error=err,
                             output=output)
    except TargetBusy as e:
        _safe_audit("exec-abort", cmd=cmd, reason=str(e))
        execution.finish(rec["id"], "aborted", error=str(e), phase="lock")
        return EXEC_ABORT_LOCK, None, str(e)
    except BaseException as e:  # 含 Ctrl-C：命令可能改了一半，不能当成"没执行"
        reason = (f"{type(e).__name__}: {e}" if not isinstance(e, KeyboardInterrupt)
                  else "用户中断（Ctrl-C），无法确认命令是否完成")
        _safe_audit("exec-abort", cmd=cmd, snapshot_id=sid, reason=reason)
        # 命令没下发（还在拿锁/做快照/校验）就是明确的中止；下发过才谈得上"结果未知"
        execution.finish(rec["id"], "unknown" if dispatched else "aborted", error=reason)
        if isinstance(e, (OpsxError, KeyboardInterrupt)):
            raise OpsxError(redact(f"[opsx] 变更未完成：{reason}；"
                                   f"执行记录 {rec['id']} "
                                   + ("需核实" if dispatched else "命令未执行"))) from e
        raise
    _safe_audit("exec", cmd=cmd, snapshot_id=sid, exit_code=exit_code,
          execution_id=rec["id"], output=output[:EXEC_AUDIT_OUTPUT])
    prune_snapshots()
    execution.prune()
    return exit_code, sid, output


def _run_change(cmd: str, timeout: int) -> tuple[int, str, str, str | None]:
    """执行并归类结果，返回 (exit_code, 状态, 输出, 错误说明)。

    本机超时能 killpg 终止进程组，所以状态是 failed（明确失败）；远程超时不能
    确认远端是否还在跑，由 remote 那条路径判 unknown。
    """
    try:
        r = run_shell(cmd, timeout=timeout)
        output = redact(r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else ""))
        if r.returncode < 0:
            return (r.returncode, "unknown",
                    output + f"\n[opsx] 执行进程被信号终止（exit {r.returncode}），"
                    "无法确认子进程是否完成，需先核实再决定是否重试",
                    f"执行进程被信号终止（exit {r.returncode}），执行状态未知")
        return r.returncode, "succeeded" if r.returncode == 0 else "failed", output, None
    except subprocess.TimeoutExpired:
        return (4, "failed",
                f"[opsx] 超时：命令超过 {timeout}s 未结束，已终止进程组",
                f"超时 {timeout}s，进程组已终止")
    except OSError as e:
        return 1, "failed", redact(f"[opsx] 执行失败: {e}"), f"{type(e).__name__}: {e}"
