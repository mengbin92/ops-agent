"""单渠道通知：webhook（Slack / 飞书 / 钉钉 / 通用 JSON）+ 去重 / 恢复 / 静默 + 可重试发件箱。

W4 的设计取舍：

- **只支持一个启用渠道**。多渠道路由会把"告警到底发出去了没有"变成组合爆炸，
  个人运维阶段一个渠道足够；换渠道改配置即可（历史记录仍可查）。
- **同一持续异常不反复刷屏**。每条异常按 fingerprint（检查项 id / 任务 id）记状态，
  只在四种情况发消息：新出现、级别升级、超过 renotify_after 的提醒、恢复。
  一次运行最多一条消息（把本次所有新异常聚合成一条），避免 20 个异常刷 20 条。
- **通知失败不丢**。落 `notifications/<id>.json`（failed + 下次重试时间），daemon 每轮
  重试有限条，人也随时可以 `opsagent notify retry`。
- **凭据不外泄**。webhook URL 本身就是密钥：notify.json 0600，事件与发件箱只存脱敏后的
  endpoint（scheme://host/…），URL 明文不进模型、不进审计、不进报告。
- **不把发送塞进巡检**。发送在内置超时内同步完成（默认 8s），失败只记状态不重试等待；
  真正阻塞巡检的网络问题会被上层的时间预算兜住。
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from . import checks  # noqa: E402
from . import events  # noqa: E402

try:  # Windows 无 fcntl：退化为无锁（本项目运行在 macOS/Linux）
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

CONFIG_FIELDS = ("channel", "url", "format", "timeout_seconds", "renotify_after",
                 "quiet_hours", "mute_until", "enabled")
CHANNELS = ("webhook",)
FORMATS = ("generic", "slack", "feishu", "dingtalk")
DEFAULT_FORMAT = "generic"
DEFAULT_TIMEOUT = 8
MIN_TIMEOUT, MAX_TIMEOUT = 3, 30
DEFAULT_RENOTIFY = 6 * 3600          # 同一异常默认 6 小时提醒一次，不是每次巡检都发
MIN_RENOTIFY, MAX_RENOTIFY = 300, 7 * 86400
TEXT_LIMIT = 3500                    # 单条消息字符上限（超出按项截断，指向报告）
MAX_ATTEMPTS = 5
RETRY_BASE = 60                      # 首次重试 60s，之后指数退避
RETRY_MAX = 3600
NOTIF_KEEP = 500                     # 发件箱保留份数
STATE_KEEP_SECONDS = 30 * 86400      # 恢复/沉默超过 30 天的 fingerprint 从状态里清掉
ANOMALY_LIMIT = 50                   # 单条消息最多列出的异常项
QUIET_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$")
DURATION_RE = re.compile(r"^(\d{1,4})([smhd])$")
MAX_MUTE = 30 * 86400        # 单次静默上限 30 天：误输入的 9999d 不该等于永久静默
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2, "unreachable": 3}
SEVERITY_LABEL = {"critical": "异常", "warning": "警告", "info": "恢复",
                  "unreachable": "不可采集"}
MIN_CHANGE_GAP = 300   # 同一项在级别间抖动时，5 分钟内不为"级别变化"重复发
NOTIF_STATUSES = ("sent", "failed", "muted", "superseded")


class NotifyError(Exception):
    pass


# ------------------------------------------------------------------ 路径与锁

def _config_path() -> Path:
    return core.STATE_DIR / "notify.json"


def _state_path() -> Path:
    return core.STATE_DIR / "notify-state.json"


def _outbox_dir() -> Path:
    return core.STATE_DIR / "notifications"


@contextmanager
def _locked(name: str):
    """flock 文件锁。

    与 schedule._locked 形状相同但故意不复用：schedule 依赖本模块（run_due 里发通知），
    反向 import 会形成循环；这里只有十几行，重复比引入隐藏的依赖方向更安全。
    """
    if fcntl is None:
        yield True
        return
    path = core.STATE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield True
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass


def _now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def _finite(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value):
        return default
    return float(value)


# ------------------------------------------------------------------ 配置

def redact_endpoint(url: str) -> str:
    """只保留 scheme://host/…：webhook 的 path 往往就是凭据本身。"""
    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "[无法解析的 URL]"
    host = parts.hostname or "?"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        port = ":(非法端口)"
    tail = "/…" if (parts.path and parts.path != "/") or parts.query else ""
    return f"{parts.scheme}://{host}{port}{tail}"


def _validate_url(url) -> str:
    if not isinstance(url, str) or not url.strip():
        raise NotifyError("url 不能为空")
    url = url.strip()
    if any(ch in url for ch in " \t\r\n") or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in url):
        raise NotifyError("url 不能包含空白或控制字符")
    if len(url) > 2000:
        raise NotifyError("url 过长（上限 2000 字符）")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as e:
        raise NotifyError(f"url 无法解析: {e}") from e
    if parts.scheme not in ("https", "http"):
        raise NotifyError("url 只支持 https（本机回环可用 http）")
    if parts.username or parts.password:
        raise NotifyError("url 不能内嵌用户名/密码：凭据请放在 path 或 query 里，"
                          "或用渠道自身的 token 机制")
    host = (parts.hostname or "").lower()
    if not host:
        raise NotifyError("url 缺少主机名")
    try:
        port = parts.port  # 访问 .port 才会解析并校验范围（99999 / abc 都会抛 ValueError）
    except ValueError as e:
        raise NotifyError(f"url 端口非法: {e}") from e
    if port is not None and not 1 <= port <= 65535:
        raise NotifyError("url 端口须在 1-65535 之间")
    if parts.scheme == "http" and host not in ("127.0.0.1", "localhost", "::1"):
        raise NotifyError("非本机地址必须用 https（防止 Webhook 凭据与告警内容明文上网）")
    return url


def _validate_quiet_hours(value) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not QUIET_RE.fullmatch(value.strip()):
        raise NotifyError("quiet_hours 格式应为 HH:MM-HH:MM（如 23:00-07:00）")
    value = value.strip()
    start, end = value.split("-")
    # 按分钟比较："8:00-08:00" 与 "08:00-08:00" 等价，字符串比较会放过前者，
    # 而 in_quiet_hours 会把它当成"全天静默"。
    if _minutes(start) == _minutes(end):
        raise NotifyError("quiet_hours 的起止时间不能相同")
    return value


def _validate_config(raw) -> dict:
    if not isinstance(raw, dict):
        raise NotifyError("通知配置必须是 JSON 对象")
    unknown = [k for k in raw if k not in CONFIG_FIELDS]
    if unknown:
        raise NotifyError("未知配置字段: " + ", ".join(sorted(map(str, unknown)))
                          + f"（允许: {', '.join(CONFIG_FIELDS)}）")
    channel = raw.get("channel", "webhook")
    if channel not in CHANNELS:
        raise NotifyError(f"channel 只支持 {list(CHANNELS)}（当前 {channel!r}）")
    cfg = {
        "channel": channel,
        "url": _validate_url(raw.get("url")),
        "format": raw.get("format", DEFAULT_FORMAT),
        "timeout_seconds": raw.get("timeout_seconds", DEFAULT_TIMEOUT),
        "renotify_after": raw.get("renotify_after", DEFAULT_RENOTIFY),
        "quiet_hours": _validate_quiet_hours(raw.get("quiet_hours")),
        "mute_until": raw.get("mute_until", 0),
        "enabled": raw.get("enabled", True),
    }
    if cfg["format"] not in FORMATS:
        raise NotifyError(f"format 只支持 {list(FORMATS)}")
    for key, lo, hi in (("timeout_seconds", MIN_TIMEOUT, MAX_TIMEOUT),
                        ("renotify_after", MIN_RENOTIFY, MAX_RENOTIFY)):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise NotifyError(f"{key} 须为 {lo}-{hi} 的整数"
                              + ("（秒）" if key == "renotify_after" else ""))
    mute_until = cfg["mute_until"]
    if isinstance(mute_until, bool) or not isinstance(mute_until, (int, float)) \
            or not math.isfinite(mute_until) or mute_until < 0:
        raise NotifyError("mute_until 须为时间戳（0 表示未静默）")
    cfg["mute_until"] = float(mute_until)
    if not isinstance(cfg["enabled"], bool):
        raise NotifyError("enabled 须为布尔值")
    return cfg


def load_config(required: bool = True) -> dict:
    """读通知配置；未配置时 required=False 返回 {}，required=True 抛错并给出初始化命令。"""
    path = _config_path()
    if not path.exists():
        if required:
            raise NotifyError(f"未配置通知渠道（{path}）："
                              "先运行 opsagent notify setup --url <webhook 地址>")
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise NotifyError(f"通知配置损坏: {path}（{e}）") from e
    return _validate_config(raw)


def save_config(raw: dict) -> dict:
    """校验后原子写入（0600）：配置含 webhook 凭据，不能给同机其他用户读。"""
    cfg = _validate_config(raw)
    core._ensure_state_dir()
    path = _config_path()
    tmp = path.with_name(f".notify-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({k: cfg[k] for k in CONFIG_FIELDS}, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return cfg


def describe_config(cfg: dict) -> str:
    if not cfg:
        return "未配置（巡检仍会记录事件，只是不发消息；用 opsagent notify setup 初始化）"
    state = "启用" if cfg["enabled"] else "已停用"
    mute = ""
    if cfg["mute_until"] > time.time():
        mute = f"｜静默至 {_now_iso(cfg['mute_until'])}"
    quiet = f"｜静默时段 {cfg['quiet_hours']}" if cfg["quiet_hours"] else ""
    return (f"{state}｜渠道 {cfg['channel']}｜{redact_endpoint(cfg['url'])}"
            f"｜格式 {cfg['format']}｜超时 {cfg['timeout_seconds']}s"
            f"｜提醒间隔 {_human_duration(cfg['renotify_after'])}{quiet}{mute}")


# ------------------------------------------------------------------ 时间辅助

def _human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, rest = divmod(seconds, 3600)
        return f"{hours}h" + (f"{rest // 60}m" if rest >= 60 else "")
    days, rest = divmod(seconds, 86400)
    return f"{days}d" + (f"{rest // 3600}h" if rest >= 3600 else "")


def parse_duration(text: str) -> int:
    """`30m` / `2h` / `7d` / `900s` → 秒。"""
    m = DURATION_RE.fullmatch((text or "").strip())
    if not m:
        raise NotifyError("时长格式应为 30m / 2h / 7d / 900s")
    value, unit = int(m.group(1)), m.group(2)
    if value <= 0:
        raise NotifyError("时长必须大于 0")
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _minutes(hhmm: str) -> int:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


def in_quiet_hours(quiet: str | None, ts: float) -> bool:
    """静默时段判定；支持跨午夜（23:00-07:00）与整段相等被拒绝的配置。"""
    if not quiet:
        return False
    start, end = quiet.split("-")
    lo, hi = _minutes(start), _minutes(end)
    lt = time.localtime(ts)
    now = lt.tm_hour * 60 + lt.tm_min
    if lo < hi:
        return lo <= now < hi
    return now >= lo or now < hi


# ------------------------------------------------------------------ 状态与发件箱

def _load_state_checked() -> tuple[dict, bool]:
    """返回 (状态, 是否可信)。状态文件损坏时不可信：不能据此判断"告警已恢复"。"""
    path = _state_path()
    if not path.exists():
        return {"version": 1, "items": {}}, True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"version": 1, "items": {}}, False
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {"version": 1, "items": {}}, False
    return {"version": 1, "items": items}, True


def _load_state() -> dict:
    # 状态坏了不能挡住巡检：退回空状态（最坏结果是多提醒一次，而不是不发）
    return _load_state_checked()[0]


def _write_private(path: Path, text: str) -> None:
    """0600 原子写：先按 0600 建临时文件再 replace，避免中途有 0644 的窗口。"""
    tmp = path.with_name(f".{path.name}-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _save_state(state: dict) -> None:
    core._ensure_state_dir()
    _write_private(_state_path(), json.dumps(state, ensure_ascii=False, indent=2))


def _prune_state(state: dict, now: float) -> None:
    items = state.get("items") or {}
    for fp in list(items):
        entry = items.get(fp)
        if not isinstance(entry, dict):
            items.pop(fp, None)
            continue
        last = _finite(entry.get("last_seen"), 0) or 0
        if entry.get("severity") is None and now - last > STATE_KEEP_SECONDS:
            items.pop(fp, None)


def _new_notification_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-ntf-" + uuid.uuid4().hex[:6]


def _write_notification(notif: dict) -> None:
    directory = _outbox_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    _write_private(directory / f"{notif['id']}.json",
                   json.dumps(notif, ensure_ascii=False, indent=2))


def _prune_notifications(keep: int = NOTIF_KEEP) -> None:
    try:
        files = sorted(_outbox_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in files[:-keep]:
        try:
            path.unlink()
        except OSError:
            pass


def _read_notification(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        return None
    if data.get("status") not in NOTIF_STATUSES:
        return None
    return data


def list_notifications(status: str | None = None, tail: int = 20) -> list[dict]:
    directory = _outbox_dir()
    if not directory.is_dir():
        return []
    out = []
    for path in directory.glob("*.json"):
        rec = _read_notification(path)
        if rec is None or (status and rec.get("status") != status):
            continue
        out.append(rec)
    out.sort(key=lambda r: float(_finite(r.get("created"), 0) or 0), reverse=True)
    return out[:tail]


def format_notifications(status: str | None = None, tail: int = 20) -> str:
    records = list_notifications(status=status, tail=tail)
    if not records:
        return "(无通知记录)" if not status else f"(无 {status} 状态的通知)"
    lines = []
    for rec in records:
        extra = ""
        if rec["status"] == "failed":
            extra = (f"｜第 {rec.get('attempts')} 次失败，"
                     f"下次重试 {_now_iso(_finite(rec.get('next_attempt_ts'), 0) or 0)}"
                     f"｜{rec.get('last_error') or ''}")
        elif rec["status"] == "muted":
            extra = f"｜未发送（{rec.get('muted_reason') or '静默'}）"
        lines.append(f"{rec.get('created_iso') or '?'}  {rec['id']}  {rec['status']}  "
                     f"{rec.get('severity') or '-'}  {rec.get('title') or ''}{extra}")
    return "\n".join(lines)


def _all_notifications() -> list[dict]:
    directory = _outbox_dir()
    if not directory.is_dir():
        return []
    out = []
    for path in directory.glob("*.json"):
        rec = _read_notification(path)
        if rec is not None and isinstance(rec.get("text"), str):
            out.append(rec)
    return out


# ------------------------------------------------------------------ 载荷与发送

def build_payload(cfg: dict, notif: dict) -> dict:
    """四种主流 webhook 的文本载荷；generic 额外带上机器可读字段。"""
    text = notif.get("text") or ""
    fmt = cfg.get("format", DEFAULT_FORMAT)
    if fmt == "slack":
        return {"text": text}
    if fmt == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if fmt == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    return {
        "event": "ops-agent",
        "kind": notif.get("kind"),
        "severity": notif.get("severity"),
        "title": notif.get("title"),
        "text": text,
        "run_id": notif.get("run_id"),
        "task_id": notif.get("task_id"),
        "session_id": notif.get("session_id"),
        "report_path": notif.get("report_path"),
        "items": notif.get("items") or [],
        "recoveries": notif.get("recoveries") or [],
        "timestamp": notif.get("created"),
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """POST 不跟随重定向：301/302 会被改成 GET 并返回 200，看起来"发送成功"其实没投递。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _response_error(fmt: str, body: str) -> str | None:
    """部分渠道用 HTTP 200 + 错误体表示失败（钉钉关键词、飞书 token 等）。"""
    if not body:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if fmt == "dingtalk" and data.get("errcode") not in (None, 0, "0"):
        return f"钉钉返回 errcode={data.get('errcode')}: {data.get('errmsg') or ''}"[:200]
    if fmt == "feishu":
        code = data.get("code", data.get("StatusCode"))
        if code not in (None, 0, "0"):
            return f"飞书返回 code={code}: {data.get('msg') or ''}"[:200]
    if data.get("ok") is False or data.get("success") is False:
        return f"渠道返回失败: {data.get('error') or data.get('message') or body[:120]}"[:200]
    if fmt == "generic" and isinstance(data.get("error"), str) and data["error"]:
        return f"渠道返回 error: {data['error']}"[:200]
    return None


def _post(url: str, payload: dict, timeout: int) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "ops-agent/1 (personal ops)"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # 必须是 URLError 之前，HTTPError 是它的子类
        try:
            body = e.read(512).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 —— 读错误体失败不影响状态判定
            body = ""
        return int(e.code), body
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            http.client.HTTPException) as e:
        # BadStatusLine / IncompleteRead / InvalidURL 都不是 OSError：漏掉会让
        # 一次畸形响应把守护进程打挂。
        raise NotifyError(f"发送失败: {type(e).__name__}: {e}") from e


def _backoff(attempts: int) -> int:
    return min(RETRY_MAX, RETRY_BASE * (2 ** max(0, attempts - 1)))


def _deliver(cfg: dict, notif: dict) -> dict:
    """发送并回写结果（调用方持有 notify.mutex）。"""
    notif["attempts"] = int(notif.get("attempts") or 0) + 1
    notif["last_attempt_iso"] = _now_iso()
    try:
        status, body = _post(cfg["url"], build_payload(cfg, notif),
                             cfg.get("timeout_seconds", DEFAULT_TIMEOUT))
    except NotifyError as e:
        notif.update({"status": "failed", "last_error": core.redact(str(e))[:300],
                      "next_attempt_ts": time.time() + _backoff(notif["attempts"])})
        return notif
    except Exception as e:  # noqa: BLE001 —— 发送路径不能把异常抛给巡检
        notif.update({"status": "failed",
                      "last_error": core.redact(f"{type(e).__name__}: {e}")[:300],
                      "next_attempt_ts": time.time() + _backoff(notif["attempts"])})
        return notif
    channel_error = _response_error(cfg.get("format", DEFAULT_FORMAT), body) \
        if 200 <= status < 300 else None
    if 200 <= status < 300 and channel_error is None:
        notif.update({"status": "sent", "http_status": status, "sent_at": time.time(),
                      "sent_iso": _now_iso(), "last_error": None, "next_attempt_ts": 0})
    elif channel_error is not None:
        notif.update({"status": "failed", "http_status": status,
                      "last_error": core.redact(channel_error)[:300],
                      "next_attempt_ts": time.time() + _backoff(notif["attempts"])})
    else:
        notif.update({"status": "failed", "http_status": status,
                      "last_error": f"HTTP {status}: {core.redact(body)[:200]}",
                      "next_attempt_ts": time.time() + _backoff(notif["attempts"])})
    return notif


def retry_pending(limit: int = 5, notif_id: str | None = None) -> list[dict]:
    """重试失败通知：daemon 每轮调用（有限条），也可 `opsagent notify retry` 手动触发。"""
    if type(limit) is not int or limit < 1:
        raise NotifyError("limit 须为正整数")
    cfg = load_config()  # 未配置就没法重试：让调用方把错误暴露给人
    now = time.time()
    done = []
    with _locked("notify.mutex"):
        state, trusted = _load_state_checked()
        open_fps = {fp for fp, entry in (state.get("items") or {}).items()
                    if isinstance(entry, dict) and entry.get("severity") is not None}
        candidates = [n for n in _all_notifications() if n.get("status") == "failed"]
        if notif_id:
            candidates = [n for n in candidates if n["id"] == notif_id]
            if not candidates:
                raise NotifyError(f"没有失败状态的通知: {notif_id}")
        else:
            candidates = [n for n in candidates
                          if int(n.get("attempts") or 0) < MAX_ATTEMPTS
                          and (_finite(n.get("next_attempt_ts"), 0) or 0) <= now]
            candidates.sort(key=lambda n: _finite(n.get("created"), 0) or 0)
            candidates = candidates[:limit]
        for notif in candidates:
            fps = [f for f in (notif.get("fingerprints") or []) if isinstance(f, str)]
            if trusted and fps and not any(fp in open_fps for fp in fps):
                # 告警都已经恢复了再补发旧告警，会变成"先恢复、后又报异常"的乱序
                notif.update({"status": "superseded", "superseded_at": now,
                              "last_error": None})
                _write_notification(notif)
                events.append("notify-retry-skipped", notification=notif["id"],
                              title=notif.get("title"), reason="已恢复或被更新的通知取代")
                done.append(notif)
                continue
            reason = _suppression_reason(cfg, notif.get("severity") or "warning", now)
            if reason:
                # 用户明确静默/停用期间不补发：否则"静默"形同虚设
                notif.update({"status": "muted", "muted_reason": reason})
                _write_notification(notif)
                events.append("notify-muted", reason=reason, title=notif.get("title"),
                              notification=notif["id"])
                done.append(notif)
                continue
            _deliver(cfg, notif)
            _write_notification(notif)
            ok = notif["status"] == "sent"
            events.append("notify-retry-sent" if ok else "notify-retry-failed",
                          notification=notif["id"], title=notif.get("title"),
                          attempts=notif.get("attempts"), error=notif.get("last_error"))
            done.append(notif)
    return done


# ------------------------------------------------------------------ 状态机

def _signal_from_record(rec: dict) -> dict | None:
    """取本次运行的规则检查摘要（优先读 checks 落盘的完整版）。

    运行记录里的 anomalies 只是给人看的紧凑副本（最多 20 条）；判定"是否恢复"
    必须用完整摘要，否则第 21 条之后的异常会被当成"本次没出现 = 已恢复"。
    摘要文件已被轮转删除时退回紧凑副本，并标记 truncated，禁止据此判定恢复。
    """
    session_id = rec.get("session_id")
    anomalies = rec.get("anomalies")
    compact = {
        "session_id": session_id,
        "verdict": rec.get("rules_verdict"),
        "complete": rec.get("rules_complete"),
        "targets": rec.get("rules_targets") or [],
        "truncated": bool(rec.get("rules_truncated")),
        "anomalies": anomalies if isinstance(anomalies, list) else [],
        "report_path": rec.get("report_path"),
    }
    if not session_id:
        return compact if rec.get("signal_path") else None
    signal = checks.load_signal(session_id)
    if signal is None:
        # 摘要被轮转删除：只有当时确实落过摘要才退回紧凑副本，且按"截断"处理
        return dict(compact, truncated=True) if rec.get("signal_path") else None
    started = _finite(rec.get("started_ts"), 0) or 0
    created = _finite(signal.get("created_ts"), 0) or 0
    if started and created and created < started - 60:
        return None  # 会话里遗留的旧摘要，不能当成本次结论
    return signal


def _current_items(rec: dict, signal: dict | None) -> dict:
    """把一次运行折算成"当前存在的异常"：fingerprint -> 描述。

    以确定性检查结果（signal）为准；没有 signal 时才退回模型给的结论。
    signal 来自 checks 直接落盘的文件，不经模型转述，模型不可用时仍然有值。
    """
    task_id = str(rec.get("task_id") or "?")
    run_id = str(rec.get("run_id") or "")
    items: dict = {}
    anomalies = (signal or {}).get("anomalies") or []
    for anomaly in anomalies:
        if not isinstance(anomaly, dict):
            continue
        key = anomaly.get("id") or anomaly.get("name")
        if not key:
            continue
        # unknown/error 是"测不到"，不是"正常"：单独一级，且比异常更该提醒
        # （磁盘 98% 突然变成探针超时，比继续报 98% 更危险）。
        status = anomaly.get("status")
        severity = {"critical": "critical", "warn": "warning"}.get(status, "unreachable")
        items[f"check:{key}"] = {
            "severity": severity, "kind": "check", "task_id": task_id, "run_id": run_id,
            "target": anomaly.get("target"),
            "title": f"{anomaly.get('target') or '?'} {anomaly.get('name') or key}",
            "line": str(anomaly.get("line") or anomaly.get("name") or "")[:400],
            "command": str(anomaly.get("command") or "")[:200],
            "output": str(anomaly.get("output") or "")[:300],
        }
    verdict = rec.get("verdict")
    rules_complete = bool(signal) and signal.get("complete") is True
    # 规则已经报出异常时，模型的"异常"结论几乎总是同一批问题，再发一条就是刷屏；
    # 只有在规则什么都没发现时，模型的结论才提供额外信息，才值得单独提醒
    # （宁可多一次误报，也不要静默掉模型发现的线索）。
    if not items and (verdict in ("异常", "警告")
                      or (verdict in ("不完整", "未判定") and not rules_complete)):
        items[f"task:{task_id}"] = {
            "severity": "critical" if verdict == "异常" else "warning",
            "kind": "verdict", "task_id": task_id, "run_id": run_id,
            "title": f"任务 {task_id} 结论：{verdict}",
            "line": str(rec.get("result_preview") or f"结论：{verdict}")[:400],
            "command": "", "output": "",
        }
    status = rec.get("status")
    if status in ("failed", "timeout", "interrupted"):
        detail = {"failed": "运行失败", "timeout": "运行超时",
                  "interrupted": "运行被中断（调度进程退出）"}[status]
        items[f"run:{task_id}"] = {
            "severity": "critical", "kind": "run", "task_id": task_id, "run_id": run_id,
            "title": f"任务 {task_id} {detail}",
            "line": str(rec.get("error") or detail)[:400],
            "command": "", "output": "",
        }
    elif status == "missed":
        # 错过是一次性事件，不是"持续异常"：不去重、也不参与恢复判定。
        items[f"missed:{task_id}"] = {
            "severity": "warning", "kind": "missed", "task_id": task_id, "run_id": run_id,
            "transient": True,
            "title": f"任务 {task_id} 错过应执行时点",
            "line": str(rec.get("error") or "调度进程停机期间错过了执行")[:400],
            "command": "", "output": "",
        }
    return items


def _recoverable(fp: str, signal: dict | None, rec: dict) -> bool:
    """只有"能证明已恢复"才发恢复通知：采集不完整/被截断时宁可保持沉默。"""
    if fp.startswith("check:"):
        if not signal or signal.get("truncated"):
            return False  # 摘要被截断：没看到不等于不存在
        parts = fp[len("check:"):].split("/")
        target = parts[0]
        group = parts[1] if len(parts) > 1 else ""
        if target not in (signal.get("targets") or []):
            return False  # 本次根本没查这个目标（inspect --target web1 证明不了 db1）
        groups = signal.get("groups") or []
        if group and groups and group not in groups:
            return False  # 只跑了部分分组：没跑的组不能判恢复
        # 按目标判定：别的目标不可达不该拖住本目标的恢复
        verdict = (signal.get("target_verdicts") or {}).get(target)
        if verdict is not None:
            return verdict != "不完整"
        return signal.get("complete") is True
    if fp.startswith("run:"):
        return rec.get("status") == "succeeded"
    if fp.startswith("missed:"):
        return False
    return (rec.get("status") == "succeeded" and rec.get("verdict") == "正常"
            and (signal is None or signal.get("complete") is True))


def _plan(state: dict, current: dict, signal: dict | None, rec: dict,
          now: float, cfg: dict) -> tuple[list, list, dict]:
    """决定本次要发什么：返回 (待发异常, 恢复项, 统计)。只改内存状态，由调用方落盘。"""
    items = state.setdefault("items", {})
    renotify = cfg.get("renotify_after", DEFAULT_RENOTIFY)
    to_send, recoveries = [], []
    counts = {"suppressed": 0, "new": 0, "escalated": 0, "changed": 0,
              "reminder": 0, "recovered": 0, "cleared": 0}
    for fp, item in current.items():
        prev = items.get(fp) if isinstance(items.get(fp), dict) else None
        prev_sev = prev.get("severity") if prev else None
        last_notified = _finite(prev.get("notified_at"), None) if prev else None
        if prev is None or prev_sev is None:
            action = "new"
        elif prev_sev != item["severity"]:
            # 级别变化（含 critical → unknown 这种"测不到了"）要告诉人；
            # 但"往回变"太频繁就是抖动：5 分钟内不高于上次已通知级别就不发。
            notified_sev = prev.get("notified_severity") or prev_sev
            flapping = (last_notified is not None
                        and now - last_notified < MIN_CHANGE_GAP
                        and SEVERITY_RANK.get(item["severity"], 1)
                        <= SEVERITY_RANK.get(notified_sev, 1))
            if flapping:
                action = "suppressed"
            else:
                action = "escalated" if SEVERITY_RANK.get(item["severity"], 1) > \
                    SEVERITY_RANK.get(prev_sev, 1) else "changed"
        elif prev.get("needs_notify"):
            # 静默/停用/未配置期间没能送达：解除后要重新提醒
            action = "new"
        elif last_notified is None:
            action = "new"
        elif now - last_notified >= renotify:
            action = "reminder"
        else:
            action = "suppressed"
        if action == "suppressed":
            counts["suppressed"] += 1
            continue
        counts[action] += 1
        entry = dict(prev or {})
        reopened = prev is None or prev_sev is None
        entry.update({"severity": item["severity"], "last_seen": now,
                      "first_seen": now if reopened else
                      (_finite(entry.get("first_seen"), now) or now),
                      "task_id": item["task_id"], "title": item["title"],
                      "kind": item.get("kind")})
        entry.pop("recovered_at", None)
        entry.pop("pending_recovery", None)
        item["action"] = action
        item["fingerprint"] = fp
        to_send.append(item)
        items[fp] = entry
        if item.get("transient"):
            # 一次性事件（错过）：发过就不再保持"未恢复"状态
            entry["severity"] = None
            entry["recovered_at"] = now

    for fp, entry in list(items.items()):
        if not isinstance(entry, dict) or entry.get("severity") is None or fp in current:
            continue
        scope = fp.split(":", 1)[1] if fp.startswith(("run:", "task:")) else None
        if scope is not None and scope != rec.get("task_id"):
            continue
        if not _recoverable(fp, signal, rec):
            continue
        counts["recovered"] += 1
        if _finite(entry.get("notified_at"), None) is None:
            # 从没真正告诉过用户（未配置/停用/静默期间发现的）：静默清状态，
            # 不发"恢复"——否则会推一条"X 已恢复"，而对方根本不知道 X 曾经坏过。
            entry["severity"] = None
            entry["recovered_at"] = now
            entry.pop("needs_notify", None)
            entry.pop("event_severity", None)
            items[fp] = entry
            counts["cleared"] += 1
            continue
        # 先不把状态清成"已恢复"：只有真把恢复消息发出去（或根本没有通知渠道）才清。
        entry["pending_recovery"] = now
        entry["recovered_run"] = rec.get("run_id")
        items[fp] = entry
        recoveries.append({"fingerprint": fp, "title": entry.get("title") or fp,
                           "first_seen": _finite(entry.get("first_seen"), now) or now,
                           "severity": "info"})
    return to_send, recoveries, counts


def _compose(items: list, recoveries: list, rec: dict, now: float) -> dict:
    """把本次所有新异常与恢复聚合成**一条**消息（刷屏的主要来源是每条异常发一次）。

    先留出尾部预算（报告路径、追溯命令），再按预算装正文：异常项多时截断的是
    正文而不是"报告在哪"。最后统一脱敏，超长时按整行回退，尾部始终保留。
    """
    severity = "info"
    for item in items:
        if SEVERITY_RANK.get(item["severity"], 1) > SEVERITY_RANK.get(severity, 0):
            severity = item["severity"]
    total = len(items) + len(recoveries)
    if items and recoveries:
        title = f"巡检告警 {len(items)} 项、恢复 {len(recoveries)} 项"
    elif items:
        title = f"巡检告警 {len(items)} 项"
    else:
        title = f"巡检恢复 {len(recoveries)} 项"
    head = [
        f"[ops-agent] {title}",
        f"时间: {_now_iso(now)}｜任务 {rec.get('task_id') or '?'}"
        f"｜run {rec.get('run_id') or '?'}｜会话 {rec.get('session_id') or '（未记录）'}",
        "",
    ]
    tail = []
    if rec.get("report_path"):
        tail.append(f"报告: {rec['report_path']}（含命令与完整输出）")
    tail.append(f"追溯: opsagent schedule runs --id {rec.get('task_id') or ''}".rstrip()
                + "｜opsagent events --tail 20")
    head_text, tail_text = "\n".join(head), "\n".join(tail)
    budget = max(400, TEXT_LIMIT - len(head_text) - len(tail_text) - 120)
    body, used, listed, truncated = [], 0, 0, False
    for index, item in enumerate(items, 1):
        if listed >= ANOMALY_LIMIT:
            truncated = True
            break
        block = [f"{listed + 1}. [{SEVERITY_LABEL.get(item['severity'], '告警')}] "
                 f"{item.get('title') or item.get('fingerprint')}：{item.get('line') or ''}"]
        if item.get("command"):
            block.append(f"   命令: {item['command']}")
        if item.get("output"):
            block.append("   输出: "
                         + " / ".join(str(item["output"]).splitlines()[:3])[:240])
        block_text = "\n".join(block)
        if used + len(block_text) > budget:
            truncated = True
            break
        body.extend(block)
        used += len(block_text)
        listed += 1
    if recoveries and not truncated:
        if body:
            body.append("")
        body.append("恢复:")
        for rec_item in recoveries:
            if listed >= ANOMALY_LIMIT:
                truncated = True
                break
            lasted = _human_duration(now - (rec_item.get("first_seen") or now))
            line = (f"{listed + 1}. {rec_item['title']} 已恢复正常"
                    f"（此前持续约 {lasted}）")
            if used + len(line) > budget:
                truncated = True
                break
            body.append(line)
            used += len(line)
            listed += 1
    if truncated or listed < total:
        body.append(f"（消息已截断：本次共 {total} 项，完整清单见报告与事件时间线）")
    text = core.redact("\n".join(head + body + [""] + tail))
    if len(text) > TEXT_LIMIT:  # 脱敏可能让文本变长；按整行回退，尾部必须保住
        room = max(200, TEXT_LIMIT - len(core.redact(tail_text)) - 200)
        kept, size = [], 0
        for line in core.redact("\n".join(head + body)).splitlines():
            if size + len(line) + 1 > room:
                break
            kept.append(line)
            size += len(line) + 1
        text = "\n".join(kept) + f"\n…（正文已截断，共 {total} 项）\n" \
            + core.redact(tail_text)
    return {"severity": severity, "title": title, "text": text,
            "items": [{"fingerprint": i.get("fingerprint"), "severity": i["severity"],
                       "title": i.get("title"), "line": i.get("line"),
                       "command": i.get("command")} for i in items[:ANOMALY_LIMIT]],
            "recoveries": [{"fingerprint": r["fingerprint"], "title": r["title"]}
                           for r in recoveries[:ANOMALY_LIMIT]]}


def _suppression_reason(cfg: dict, severity: str, now: float) -> str | None:
    if not cfg.get("enabled", True):
        return "disabled"
    if (_finite(cfg.get("mute_until"), 0) or 0) > now:
        return "muted"
    # 静默时段只压 warning/info：异常与"测不到"要能把人叫起来，
    # 否则静默就等于关监控。
    if SEVERITY_RANK.get(severity, 1) < 2 and in_quiet_hours(cfg.get("quiet_hours"), now):
        return "quiet_hours"
    return None


def _should_record_muted(state: dict, fingerprints: list, now: float, cfg: dict) -> bool:
    """静默期间同一批异常只记一条 muted 记录，不然每轮巡检都写一条噪音。"""
    window = cfg.get("renotify_after", DEFAULT_RENOTIFY)
    items = state.get("items") or {}
    for fp in fingerprints:
        entry = items.get(fp)
        muted_at = _finite(entry.get("muted_at"), None) if isinstance(entry, dict) else None
        if muted_at is None or now - muted_at >= window:
            return True
    return False


def forget_task(task_id: str) -> int:
    """任务被删除：清掉它的 task:/run:/missed: 指纹，避免状态永久残留。"""
    if not isinstance(task_id, str) or not task_id:
        return 0
    removed = 0
    with _locked("notify.mutex"):
        state = _load_state()
        items = state.get("items") or {}
        for fp in list(items):
            if fp.startswith(("task:", "run:", "missed:")) \
                    and fp.split(":", 1)[1] == task_id:
                items.pop(fp, None)
                removed += 1
        if removed:
            try:
                _save_state(state)
            except OSError:
                pass
    return removed


def _store_notification(notif: dict, result: dict) -> None:
    """写发件箱；失败只记到 result，不阻断状态落盘（否则下一轮会重复发送）。"""
    try:
        _write_notification(notif)
        _prune_notifications()
    except OSError as e:
        result["error"] = result["error"] or f"发件箱写入失败: {e}"
        notif["unrecorded"] = True


def _mark_muted(state: dict, fingerprints: list, now: float, touch: bool = True) -> bool:
    """标记"被静默"，返回是否有状态变化（决定要不要落盘）。

    touch=True 才刷新 muted_at，让"静默期只记一条 muted 记录"的窗口真正生效。
    """
    items = state.get("items") or {}
    changed = False
    for fp in fingerprints:
        entry = items.get(fp)
        if isinstance(entry, dict):
            if touch and entry.get("muted_at") != now:
                entry["muted_at"] = now
                changed = True
            # needs_notify 让"解除静默后重新提醒"与"事件去重"互不干扰：
            # 不清 notified_at，就不会每轮重写一条 anomaly-new 事件。
            if not entry.get("needs_notify"):
                entry["needs_notify"] = True
                changed = True
            items[fp] = entry
    return changed


def _mark_notified(state: dict, fingerprints: list, now: float) -> None:
    items = state.get("items") or {}
    for fp in fingerprints:
        entry = items.get(fp)
        if isinstance(entry, dict):
            entry["notified_at"] = now
            entry["notified_severity"] = entry.get("severity")
            entry["needs_notify"] = False
            entry["event_severity"] = entry.get("severity")


def _mark_recovered(state: dict, fingerprints: list, now: float) -> None:
    items = state.get("items") or {}
    for fp in fingerprints:
        entry = items.get(fp)
        if isinstance(entry, dict):
            entry["severity"] = None
            entry["recovered_at"] = now
            entry["pending_recovery"] = None
            entry["needs_notify"] = False
            entry.pop("event_severity", None)  # 复发时要能再记一条 anomaly-new
            items[fp] = entry


def _config_error(message: str, now: float) -> None:
    """配置坏了是"通知本身坏了"，必须留痕；同一错误不每轮刷一条。"""
    path = core.STATE_DIR / "notify-config-error.ts"
    try:
        last = float(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        last = 0
    if now - last < DEFAULT_RENOTIFY:
        return
    try:
        _write_private(path, str(now))
    except OSError:
        pass
    events.append("notify-config-error", error=message)


def process_run(rec: dict, signal: dict | None = None) -> dict:
    """一次运行结束后调用：更新异常状态 -> 写事件 -> 必要时发一条通知。

    返回 {actions, notification, error}，便于测试与调用方审计。失败不外抛：
    通知是旁路，不能把已经跑完的巡检变成失败。
    """
    now = time.time()
    result = {"actions": {"new": 0, "escalated": 0, "reminder": 0,
                          "suppressed": 0, "recovered": 0},
              "notification": None, "error": None}
    try:
        cfg = load_config(required=False)
    except NotifyError as e:
        cfg = {}
        result["error"] = str(e)
        _config_error(str(e), now)
    signal = signal if signal is not None else _signal_from_record(rec)
    current = _current_items(rec, signal)
    with _locked("notify.mutex"):
        state = _load_state()
        to_send, recoveries, counts = _plan(state, current, signal, rec, now, cfg)
        result["actions"] = counts
        items_state = state.get("items") or {}
        # 静默清掉的"从没通知过"的条目也要落盘，否则下一轮会重复清
        dirty = counts.get("cleared", 0) > 0
        for item in to_send:
            entry = items_state.get(item["fingerprint"]) or {}
            previous_event = entry.get("event_severity")
            if item["action"] == "reminder":
                events.append("anomaly-reminder", fingerprint=item["fingerprint"],
                              severity=item["severity"], title=item.get("title"),
                              task=rec.get("task_id"), run=rec.get("run_id"))
                entry["event_severity"] = item["severity"]
                dirty = True
            elif previous_event != item["severity"]:
                # 事件只在首次出现/级别变化时记；静默期重复的"new"不再刷时间线。
                # 升级、降级分开记：时间线上把"降级"读成"升级"会误导复盘。
                kind = {"escalated": "anomaly-escalated",
                        "changed": "anomaly-changed"}.get(item["action"], "anomaly-new")
                events.append(kind, fingerprint=item["fingerprint"],
                              severity=item["severity"], title=item.get("title"),
                              line=item.get("line"), task=rec.get("task_id"),
                              run=rec.get("run_id"), previous=previous_event)
                entry["event_severity"] = item["severity"]
                dirty = True
            items_state[item["fingerprint"]] = entry
        fingerprints = [item["fingerprint"] for item in to_send] + \
                       [item["fingerprint"] for item in recoveries]
        if fingerprints:
            composed = _compose(to_send, recoveries, rec, now)
            reason = _suppression_reason(cfg, composed["severity"], now) if cfg \
                else "unconfigured"
            notif = {
                "id": _new_notification_id(), "created": now, "created_iso": _now_iso(now),
                "kind": "anomaly" if not recoveries else ("recovery" if not to_send else "mixed"),
                "severity": composed["severity"], "title": composed["title"],
                "text": composed["text"], "items": composed["items"],
                "recoveries": composed["recoveries"],
                "task_id": rec.get("task_id"), "run_id": rec.get("run_id"),
                "session_id": rec.get("session_id"), "report_path": rec.get("report_path"),
                "fingerprints": fingerprints,
                "endpoint": redact_endpoint(cfg.get("url", "")) if cfg else None,
                "status": "failed", "attempts": 0, "last_error": None,
                "next_attempt_ts": 0, "http_status": None,
            }
            if reason in (None, "unconfigured", "disabled"):
                delivered = reason != "unconfigured" and reason != "disabled"
                if reason is None:
                    _deliver(cfg, notif)
                    _store_notification(notif, result)
                    events.append("notify-sent" if notif["status"] == "sent"
                                  else "notify-failed",
                                  notification=notif["id"], severity=composed["severity"],
                                  title=composed["title"], attempts=notif["attempts"],
                                  error=notif.get("last_error"), endpoint=notif["endpoint"],
                                  task=rec.get("task_id"), run=rec.get("run_id"))
                    _mark_notified(state, fingerprints, now)
                    dirty = True
                    delivered = notif["status"] == "sent"
                else:
                    # 未配置/已停用：没有可送达的受众，恢复态直接落状态，事件留痕
                    result["notification"] = None
                if delivered:
                    # 只有真送达（或根本没有渠道/已停用，无需送达）才把状态清成"已恢复"；
                    # 发送失败时保留 pending_recovery，下一轮再补发，避免"恢复消息丢了"。
                    _mark_recovered(state, [r["fingerprint"] for r in recoveries], now)
                    for rec_item in recoveries:
                        events.append("anomaly-recovered",
                                      fingerprint=rec_item["fingerprint"],
                                      title=rec_item.get("title"), task=rec.get("task_id"),
                                      run=rec.get("run_id"))
                    dirty = dirty or bool(recoveries)
                result["notification"] = notif if reason is None else result["notification"]
            else:
                notif["status"] = "muted"
                notif["muted_reason"] = reason
                record_muted = _should_record_muted(state, fingerprints, now, cfg)
                if record_muted:
                    _store_notification(notif, result)
                    events.append("notify-muted", reason=reason, title=composed["title"],
                                  task=rec.get("task_id"), run=rec.get("run_id"),
                                  count=len(fingerprints))
                changed = _mark_muted(state, fingerprints, now, touch=record_muted)
                dirty = dirty or record_muted or changed
                # 恢复项保持 pending_recovery：解除静默后补发"已恢复"
                result["notification"] = notif
        if dirty:
            # 状态没变就不写盘：未配置/持续被去重的巡检不该每轮都 fsync 一次
            _prune_state(state, now)
            try:
                _save_state(state)
            except OSError as e:
                # 状态写不进去最坏是"下次重复提醒一次"，不能反过来让巡检失败
                result["error"] = result["error"] or f"通知状态写盘失败: {e}"
                _config_error(f"通知状态写盘失败: {e}", now)
    return result


# ------------------------------------------------------------------ CLI

def status() -> dict:
    try:
        cfg = load_config(required=False)
        error = None
    except NotifyError as e:
        cfg, error = {}, str(e)
    records = list_notifications(tail=NOTIF_KEEP)
    counts = {}
    for rec in records:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    pending = [r for r in records if r["status"] == "failed"
               and int(r.get("attempts") or 0) < MAX_ATTEMPTS]
    return {"config": cfg, "error": error, "counts": counts, "records": len(records),
            "pending": len(pending), "mute_until": cfg.get("mute_until", 0) if cfg else 0}


def _print_status() -> None:
    info = status()
    print(f"渠道: {describe_config(info['config'])}")
    if info["error"]:
        print(f"配置错误: {info['error']}")
    if info["records"]:
        detail = "、".join(f"{k} {v}" for k, v in sorted(info["counts"].items()))
        print(f"发件箱: {info['records']} 条（{detail}）")
    else:
        print("发件箱: 空")
    print(f"待重试: {info['pending']} 条（opsagent notify retry）")
    tail = list_notifications(tail=5)
    if tail:
        print("\n最近通知:")
        print(format_notifications(tail=5))


def _parse_until(text: str) -> float:
    text = (text or "").strip()
    if not text:
        raise NotifyError("--until 需要时间（如 '2026-10-23 08:00'）")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M"):
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%m-%d %H:%M":
            parsed = (time.localtime().tm_year,) + parsed[1:]
        ts = time.mktime(parsed)
        if ts <= time.time():
            raise NotifyError("--until 必须是将来时间")
        return ts
    raise NotifyError("--until 时间格式应为 'YYYY-MM-DD HH:MM'")


def cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="opsagent notify",
        description="单渠道通知：配置、测试、查询失败与重试、静默",
        epilog="退出码：0 成功；1 参数/配置错误或发送失败")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="渠道配置、发件箱统计与最近通知")
    setup = sub.add_parser("setup", help="配置 webhook（写入 ~/.ops-agent/notify.json，0600）")
    setup.add_argument("--url", required=True, help="https webhook 地址（本机回环可用 http）")
    setup.add_argument("--format", default=None, choices=list(FORMATS),
                       help=f"载荷格式（默认保留旧值，首次为 {DEFAULT_FORMAT}）")
    setup.add_argument("--timeout", type=int, default=None, metavar="SECONDS",
                       help=f"发送超时（默认保留旧值，首次 {DEFAULT_TIMEOUT}s）")
    setup.add_argument("--renotify", type=int, default=None, metavar="SECONDS",
                       help="同一异常的提醒间隔（默认保留旧值，首次 6 小时）")
    setup.add_argument("--quiet-hours", default=None, metavar="HH:MM-HH:MM",
                       help="静默时段（只压警告/恢复，异常与不可采集仍发送）")
    setup.add_argument("--no-quiet-hours", action="store_true", help="清除已有的静默时段")
    sub.add_parser("test", help="发送一条测试通知")
    listing = sub.add_parser("list", help="通知记录（含失败原因与下次重试时间）")
    listing.add_argument("--status", choices=list(NOTIF_STATUSES), default=None)
    listing.add_argument("--tail", type=int, default=20)
    retry = sub.add_parser("retry", help="重试失败通知")
    retry.add_argument("--id", help="只重试这一条")
    retry.add_argument("--limit", type=int, default=5)
    mute = sub.add_parser("mute", help="静默一段时间（巡检照常记录事件）")
    group = mute.add_mutually_exclusive_group(required=True)
    group.add_argument("--for", dest="duration", metavar="2h", help="时长：30m / 2h / 7d")
    group.add_argument("--until", metavar="'2026-10-23 08:00'", help="静默到指定时间")
    sub.add_parser("unmute", help="取消静默（未发送的异常会在下次巡检重新提醒）")
    for action, help_text in (("enable", "启用通知"), ("disable", "停用通知")):
        sub.add_parser(action, help=help_text)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "status":
            _print_status()
            return 0
        if args.cmd == "setup":
            try:  # 只改用户显式给的字段：setup 不该顺手启用通知或清掉静默时段
                old = load_config(required=False)
            except NotifyError:
                old = {}
            quiet = None if args.no_quiet_hours else (
                args.quiet_hours if args.quiet_hours is not None
                else old.get("quiet_hours"))
            raw = {
                "channel": "webhook", "url": args.url,
                "format": args.format or old.get("format") or DEFAULT_FORMAT,
                "timeout_seconds": args.timeout if args.timeout is not None
                else old.get("timeout_seconds", DEFAULT_TIMEOUT),
                "renotify_after": args.renotify if args.renotify is not None
                else old.get("renotify_after", DEFAULT_RENOTIFY),
                "quiet_hours": quiet,
                "enabled": old.get("enabled", True),
                "mute_until": old.get("mute_until", 0),
            }
            cfg = save_config(raw)
            events.append("notify-config", action="setup",
                          endpoint=redact_endpoint(cfg["url"]), format=cfg["format"],
                          renotify=cfg["renotify_after"], quiet=cfg["quiet_hours"])
            print(f"已写入 {_config_path()}（0600）")
            print(f"渠道: {describe_config(cfg)}")
            print("建议下一步: opsagent notify test")
            return 0
        if args.cmd == "test":
            cfg = load_config()
            notif = {
                "id": _new_notification_id(), "created": time.time(),
                "created_iso": _now_iso(), "kind": "test", "severity": "info",
                "title": "ops-agent 测试通知",
                "text": (f"[ops-agent] 测试通知\n时间: {_now_iso()}\n"
                         f"渠道: {redact_endpoint(cfg['url'])}｜格式: {cfg['format']}\n"
                         "收到这条说明 webhook 配置可用。"),
                "items": [], "recoveries": [], "task_id": None, "run_id": None,
                "session_id": None, "report_path": None, "fingerprints": [],
                "endpoint": redact_endpoint(cfg["url"]), "status": "failed",
                "attempts": 0, "last_error": None, "next_attempt_ts": 0,
                "http_status": None,
            }
            with _locked("notify.mutex"):
                _deliver(cfg, notif)
                _write_notification(notif)
            events.append("notify-sent" if notif["status"] == "sent" else "notify-failed",
                          notification=notif["id"], notif_kind="test",
                          attempts=notif["attempts"], error=notif.get("last_error"),
                          endpoint=notif["endpoint"])
            if notif["status"] == "sent":
                print(f"已发送（HTTP {notif.get('http_status')}）→ {notif['endpoint']}")
                reason = _suppression_reason(cfg, "critical", time.time())
                if reason:
                    label = {"muted": "静默", "disabled": "停用"}.get(reason, reason)
                    print(f"注意：通知当前处于{label}状态；手动测试不受影响。")
                return 0
            print(f"发送失败: {notif.get('last_error')}（可用 opsagent notify retry 重试）",
                  file=sys.stderr)
            return 1
        if args.cmd == "list":
            if args.tail < 0:
                raise NotifyError("tail 不能为负数")
            print(format_notifications(status=args.status, tail=args.tail))
            return 0
        if args.cmd == "retry":
            done = retry_pending(limit=args.limit, notif_id=args.id)
            if not done:
                print("(没有到期待重试的通知)")
                return 0
            for notif in done:
                status = notif["status"]
                if status == "sent":
                    text = "已发送"
                elif status == "muted":
                    text = f"已静默（{notif.get('muted_reason') or '静默'}），未发送"
                elif status == "superseded":
                    text = "已跳过（对应告警已恢复）"
                else:
                    text = f"仍失败: {notif.get('last_error')}"
                print(f"{notif['id']}  {text}")
            # 只有"仍失败"才算失败退出；静默/已恢复不是错误
            return 1 if any(n["status"] == "failed" for n in done) else 0
        if args.cmd in ("mute", "unmute", "enable", "disable"):
            cfg = load_config()
            now = time.time()
            if args.cmd == "mute":
                until = now + parse_duration(args.duration) if args.duration \
                    else _parse_until(args.until)
                if until - now > MAX_MUTE:
                    raise NotifyError("单次静默不能超过 30 天（要长期停用请用 notify disable）")
            elif args.cmd == "unmute":
                until = 0
            else:
                until = cfg["mute_until"]  # enable/disable 不该顺带解除静默
            if args.cmd in ("enable", "disable"):
                cfg["enabled"] = args.cmd == "enable"
            cfg["mute_until"] = until
            cfg = save_config(cfg)
            events.append("notify-config", action=args.cmd,
                          enabled=cfg["enabled"],
                          mute_until=_now_iso(cfg["mute_until"]) if cfg["mute_until"] else None)
            print(f"已更新: {describe_config(cfg)}")
            return 0
        raise NotifyError(f"未知子命令: {args.cmd}")
    except (NotifyError, OSError) as e:
        print(f"[notify] 错误: {core.redact(str(e))}", file=sys.stderr)
        return 1
