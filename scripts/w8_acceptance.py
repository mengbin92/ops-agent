#!/usr/bin/env python3
"""Repeatable W8 local recovery drill; all operational writes stay in a temporary sandbox.

Run: python3 scripts/w8_acceptance.py --output /path/to/evidence.json
Exit 0: local drills passed (external gates may still be blocked).
Exit 1: a local drill failed. Exit 2: --require-external and external gates are blocked.
The optional exported evidence file is the only persistent write. No real SSH,
provider, notification recipient, or production service is contacted.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import signal
from socketserver import TCPServer
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
SCHEMA_VERSION = 1
EXIT_FAILED = 1
EXIT_BLOCKED = 2
WORKER_TIMEOUT = 90
MAX_WORKER_BYTES = 2 * 1024 * 1024
CHECK_IDS = (
    "denied_authorization_no_write", "approved_configuration_change",
    "exit_zero_unhealthy_application", "rollback_requires_confirmation",
    "confirmed_config_permission_and_service_recovery", "local_timeout_stops_child_writes",
    "unknown_execution_blocks_replay", "llm_http_failure_and_recovery",
    "notification_failure_retry_and_recovery", "loopback_server_request_evidence",
)


def _source_manifest() -> dict[str, str]:
    """Bind evidence to the code and policy actually present during this run."""
    paths = [REPO / name for name in ("core.py", "execution.py", "scenario.py", "usage.py",
                                      "remote.py", "config.yaml")]
    paths.extend(sorted((REPO / "agent").glob("*.py")))
    result = {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in paths}
    result["drill"] = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    return result


def _validate_worker_report(data, source: dict) -> list[dict]:
    if not isinstance(data, dict) or data.get("source_sha256") != source:
        raise ValueError("worker source manifest is missing or changed")
    return _validate_checks(data.get("checks"))


def _validate_checks(checks) -> list[dict]:
    if not isinstance(checks, list) or len(checks) != len(CHECK_IDS):
        raise ValueError("worker check set is incomplete")
    ids = []
    for row in checks:
        if (not isinstance(row, dict) or set(row) != {"id", "status", "evidence"}
                or not isinstance(row["id"], str)
                or row["status"] not in ("passed", "failed")
                or not isinstance(row["evidence"], dict) or not row["evidence"]):
            raise ValueError("worker check evidence is malformed")
        ids.append(row["id"])
    if len(set(ids)) != len(ids) or set(ids) != set(CHECK_IDS):
        raise ValueError("worker check identities are incomplete or duplicated")
    return checks


def _validate_session(sandbox: Path) -> None:
    """Internal helpers only run inside the private session created by the parent."""
    marker = sandbox / ".w8-session"
    token = os.environ.get("OPSX_W8_SESSION", "")
    if (not token or os.environ.get("OPSX_W8_SANDBOX") != str(sandbox)
            or not sandbox.is_dir() or sandbox.is_symlink()
            or stat.S_IMODE(sandbox.stat().st_mode) != 0o700
            or not marker.is_file() or marker.is_symlink()
            or stat.S_IMODE(marker.stat().st_mode) != 0o600
            or marker.read_text(encoding="ascii") != token):
        raise ValueError("internal helper requires a private W8 parent session")


def _stop_worker(process: subprocess.Popen) -> None:
    # Give Python's finally blocks (including core.run_shell's session cleanup)
    # a chance to run before the unconditional process-group cleanup.
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def external_gates() -> list[dict]:
    return [{"id": key, "status": "blocked", "evidence": {"executed": False,
             "reason": reason}} for key, reason in (
        ("real_ssh_recovery", "未提供真实测试 SSH 主机；本次未执行 SSH 或远程断线恢复。"),
        ("real_llm_provider", "LLM 仅使用回环 HTTP 桩；未连接真实模型服务。"),
        ("real_notification_channel", "通知仅发送到回环 HTTP 桩；未向外部接收人发送。"),
        ("seven_day_readonly_trial", "本次短时演练不能替代修复后连续 7 天观察。"),
    )]


def exit_code(report: dict, require_external: bool = False) -> int:
    if not isinstance(report, dict) or report.get("local_status") != "passed":
        return EXIT_FAILED
    try:
        if any(row["status"] != "passed" for row in _validate_checks(report.get("checks"))):
            return EXIT_FAILED
    except ValueError:
        return EXIT_FAILED
    if require_external:
        gates = report.get("external_gates")
        expected = {row["id"] for row in external_gates()}
        if (not isinstance(gates, list) or len(gates) != len(expected)
                or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                       or row.get("status") != "passed"
                       or not isinstance(row.get("evidence"), dict)
                       or row["evidence"].get("executed") is not True for row in gates)
                or {row["id"] for row in gates} != expected):
            return EXIT_BLOCKED
    return 0


def write_report(path: Path, report: dict) -> None:
    """Atomic export with private permissions, including when replacing a public file."""
    path = path.expanduser().absolute()
    destination = path.parent.resolve() / path.name
    states = {Path.home().joinpath(".ops-agent").resolve()}
    if os.environ.get("OPSX_STATE_DIR") is not None:
        states.add(Path(os.environ["OPSX_STATE_DIR"]).expanduser().resolve())
    for state in states:
        if destination == state or state in destination.parents:
            raise ValueError("evidence output must be outside operational state")
    if destination == REPO or REPO in destination.parents:
        evidence_dir = REPO / "docs" / "validation"
        if evidence_dir not in destination.parents or destination.suffix != ".json":
            raise ValueError("repository evidence output must be JSON under docs/validation")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".w8-evidence-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _isolated_env(sandbox: Path) -> dict:
    # A denylist misses shell startup hooks, Python import hooks, dynamic loader
    # injection, PATH shims and unrelated provider credentials. Start empty.
    env = {"PATH": os.defpath, "HOME": str(sandbox / "home"),
           "TMPDIR": str(sandbox), "TMP": str(sandbox), "TEMP": str(sandbox),
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    env.update(OPSX_STATE_DIR=str(sandbox / "state"),
               OPSAGENT_PROVIDER="openai", OPSAGENT_API_KEY="w8-local-dummy-key",
               OPSAGENT_MODEL="w8-http-stub", NO_PROXY="127.0.0.1,localhost",
               no_proxy="127.0.0.1,localhost", PYTHONUNBUFFERED="1",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def run_acceptance() -> dict:
    started = utc_now()
    source = _source_manifest()
    with tempfile.TemporaryDirectory(prefix="opsx-w8-") as temporary:
        sandbox = Path(temporary).resolve()
        token = secrets.token_hex(32)
        marker = sandbox / ".w8-session"
        marker.write_text(token, encoding="ascii")
        marker.chmod(0o600)
        (sandbox / "home").mkdir(mode=0o700)
        env = _isolated_env(sandbox)
        env.update(OPSX_W8_SESSION=token, OPSX_W8_SANDBOX=str(sandbox))
        # A file avoids unbounded pipe capture and pipes held by an orphan. Its
        # contents stay inside the temporary session and are size-limited below.
        with (sandbox / "worker-result.json").open("w+b") as result_file, subprocess.Popen(
                [sys.executable, "-I", "-B", str(SCRIPT), "--_worker", str(sandbox)],
                cwd=sandbox, env=env, stdin=subprocess.DEVNULL,
                stdout=result_file, stderr=subprocess.DEVNULL, start_new_session=True) as process:
            timed_out = False
            output_exceeded = False
            try:
                deadline = time.monotonic() + WORKER_TIMEOUT
                while process.poll() is None:
                    if os.fstat(result_file.fileno()).st_size > MAX_WORKER_BYTES:
                        output_exceeded = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                _stop_worker(process)
            worker_exit = process.returncode
            try:
                result_file.seek(0, os.SEEK_END)
                output_exceeded = output_exceeded or result_file.tell() > MAX_WORKER_BYTES
                if timed_out or output_exceeded or worker_exit:
                    raise ValueError("worker did not complete within its limits")
                result_file.seek(0)
                data = json.load(result_file)
                checks = _validate_worker_report(data, source)
                if _source_manifest() != source:
                    raise ValueError("source changed during the drill")
            except (ValueError, UnicodeError, RecursionError):
                # Never re-export arbitrary worker output or JSON decoder snippets.
                checks = [{"id": "isolated_worker", "status": "failed",
                           "evidence": {"error": "worker completion/evidence validation failed",
                                        "exit_code": worker_exit, "timed_out": timed_out,
                                        "output_limit_exceeded": output_exceeded}}]
    local_status = "passed" if all(item["status"] == "passed" for item in checks) else "failed"
    return {"schema_version": SCHEMA_VERSION, "started_at_utc": started,
            "finished_at_utc": utc_now(), "local_status": local_status,
            "overall_status": "blocked" if local_status == "passed" else "failed",
            "environment": {"python": platform.python_version(), "platform": platform.platform(),
                            "drill_source_sha256": source["drill"], "source_sha256": source,
                            "transport": "local subprocesses and HTTP on 127.0.0.1 only",
                            "state_isolation": "TemporaryDirectory / OPSX_STATE_DIR",
                            "sandbox_removed": True, "production_writes": False,
                            "real_ssh_executed": False},
            "checks": checks, "external_gates": external_gates(),
            "evidence_retention": "配置、状态、服务进程均已清理；结构化证据保留在本报告。"}


def _control(sandbox: Path, **fields) -> None:
    path = sandbox / "server-control.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing.update(fields)
    staged = sandbox / "server-control.tmp"
    staged.write_text(json.dumps(existing), encoding="utf-8")
    staged.replace(path)


class _LoopbackHTTPServer(HTTPServer):
    def server_bind(self):
        # HTTPServer resolves even a numeric bind address with getfqdn(). This
        # local drill must not wait for the runner's reverse DNS resolver.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def _llm_failure_probe(llm) -> str:
    # Allow a cold HTTP request time to finish, while keeping the retry delay
    # outside that budget so exactly one failed attempt is charged. Only this
    # isolated worker's synthetic failure uses this backoff configuration.
    retry_base = llm.TRANSIENT_BASE
    try:
        llm.TRANSIENT_BASE = 10.0
        llm.chat([{"role": "user", "content": "W8 local synthetic probe"}],
                 deadline=time.monotonic() + 10.0)
    except llm.LLMError as error:
        failure = str(error)
    else:
        raise AssertionError("LLM 503 was reported successful")
    finally:
        llm.TRANSIENT_BASE = retry_base
    _require("HTTP 503" in failure, "LLM failure did not receive the synthetic HTTP 503")
    return failure


def _serve(sandbox: Path) -> int:
    """A real HTTP service whose application health follows the sandbox config."""
    _validate_session(sandbox)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status_code, body):
            encoded = json.dumps(body).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            with (sandbox / "http-evidence.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at_utc": utc_now(), "method": self.command,
                                         "path": self.path, "http_status": status_code}) + "\n")

        def do_GET(self):
            if self.path != "/health":
                self.respond(404, {"error": "unknown route"})
                return
            healthy = (sandbox / "service.conf").read_text().strip() == "healthy"
            self.respond(200 if healthy else 503, {"healthy": healthy})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            cfg = json.loads((sandbox / "server-control.json").read_text())
            if self.path == "/v1/chat/completions":
                code = cfg.get("llm_status", 200)
                self.respond(code, {"error": "synthetic unavailable"} if code != 200 else {
                    "choices": [{"message": {"role": "assistant", "content": "local-stub-ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
            elif self.path == "/hook":
                self.respond(cfg.get("notify_status", 200), {"ok": True})
            else:
                self.respond(404, {"error": "unknown route"})

    with _LoopbackHTTPServer(("127.0.0.1", 0), Handler) as server:
        fd, temporary = tempfile.mkstemp(prefix=".w8-port-", dir=sandbox)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(str(server.server_port))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, sandbox / "http-port")
        finally:
            Path(temporary).unlink(missing_ok=True)
        server.serve_forever(poll_interval=0.05)
    return 0


@contextmanager
def _server(sandbox: Path):
    _validate_session(sandbox)
    process = subprocess.Popen([sys.executable, "-I", "-B", str(SCRIPT), "--_server", str(sandbox)],
                               cwd=sandbox, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not (sandbox / "http-port").exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("local HTTP service failed to start")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{int((sandbox / 'http-port').read_text())}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _probe(url: str) -> int:
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.path != "/health"
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.port is None):
        raise ValueError("probe accepts only the local drill health endpoint")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(url, timeout=3) as response:
            data = json.loads(response.read())
            print(json.dumps(data))
            return 0 if response.status == 200 and data.get("healthy") is True else 1
    except urllib.error.HTTPError as error:
        print(error.read().decode())
        return 1


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _worker(sandbox: Path) -> dict:
    # Only the parent that created the sandbox may select operational state.
    _validate_session(sandbox)
    source = _source_manifest()
    expected_state = sandbox / "state"
    if Path(os.environ.get("OPSX_STATE_DIR", "")).resolve() != expected_state:
        raise RuntimeError("isolated OPSX_STATE_DIR is required")
    sys.path.insert(0, str(REPO))
    import core
    import execution
    import scenario
    import usage
    from agent import llm, notify

    expected_state.mkdir(mode=0o700)
    config = sandbox / "service.conf"
    config.write_text("healthy\n")
    config.chmod(0o640)
    original_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    _control(sandbox, llm_status=200, notify_status=200)
    checks = []
    context = {}

    def check(name, callback):
        try:
            evidence = callback()
            checks.append({"id": name, "status": "passed", "evidence": evidence})
        except Exception as error:
            checks.append({"id": name, "status": "failed",
                           "evidence": {"error": core.redact(str(error)),
                                        "error_type": type(error).__name__}})

    with _server(sandbox) as base:
        os.environ["OPSAGENT_BASE_URL"] = base + "/v1"
        probe = shlex.join([sys.executable, "-I", "-B", str(SCRIPT), "--_probe", base + "/health"])
        # This command alone is trusted for read-only health checks in this sandbox.
        (expected_state / "config.override.yaml").write_text(
            "readonly_patterns:\n  - ^" + re.escape(probe) + "$\n")
        command = "printf 'unhealthy\\n' > " + shlex.quote(str(config))
        scen = {"description": "W8 isolated application health recovery drill", "params": {},
                "precheck": [{"cmd": probe, "expect_contains": '"healthy": true'}],
                "exec": {"cmd": command, "snapshot_files": [str(config)]},
                "postcheck": [{"cmd": probe, "expect_contains": '"healthy": true'}]}
        (expected_state / "scenarios").mkdir()
        (expected_state / "scenarios" / "w8-local.json").write_text(json.dumps(scen))

        def denied():
            result = scenario.run("w8-local")
            _require(result["verdict"] == "approval_required", "missing approval was not rejected")
            _require(hashlib.sha256(config.read_bytes()).hexdigest() == original_hash,
                     "configuration changed without approval")
            _require(not execution.list_records(), "denied execution created an execution record")
            return {"verdict": result["verdict"], "config_sha256": original_hash,
                    "mode": oct(stat.S_IMODE(config.stat().st_mode)),
                    "execution_records": 0, "precheck": result["precheck"]}
        check("denied_authorization_no_write", denied)

        def approved():
            stamp = core.approve(command)
            _require(stat.S_IMODE(stamp.stat().st_mode) == 0o600, "approval file is not private")
            result = scenario.run("w8-local")
            context["scenario"] = result
            record = execution.get(result["execution_id"])
            _require(config.read_text() == "unhealthy\n", "approved command did not modify config")
            _require(record["status"] == "succeeded" and record["exit_code"] == 0,
                     "configuration command failed")
            _require(not core.check_stamp(command), "one-shot approval remained reusable")
            return {"execution_id": record["id"], "execution_status": record["status"],
                    "exit_code": record["exit_code"], "snapshot_id": result["snapshot_id"],
                    "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                    "approval_reusable": False}
        check("approved_configuration_change", approved)

        def unhealthy():
            result = context["scenario"]
            _require(result["verdict"] == "verify_failed", "exit 0 masked unhealthy service")
            _require(result["postcheck"] and not result["postcheck"][0]["passed"],
                     "missing failed HTTP application health evidence")
            return {"verdict": result["verdict"], "postcheck": result["postcheck"],
                    "probe_allowlist": "exact command in isolated config.override.yaml"}
        check("exit_zero_unhealthy_application", unhealthy)

        def rollback_confirmation():
            sid = context["scenario"]["snapshot_id"]
            before = config.read_bytes()
            try:
                core.rollback(sid, confirm=False)
            except core.OpsxError as error:
                refusal = str(error)
            else:
                raise AssertionError("rollback overwrote config without confirmation")
            _require(config.read_bytes() == before, "rollback refusal changed config")
            return {"snapshot_id": sid, "refusal": refusal, "config_unchanged": True}
        check("rollback_requires_confirmation", rollback_confirmation)

        def rollback():
            sid = context["scenario"]["snapshot_id"]
            config.chmod(0o600)  # Deliberately lose the original service-readable permissions.
            output = core.rollback(sid, confirm=True)
            record = execution.find_latest("rollback " + sid)
            health = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--_probe", base + "/health"],
                                    capture_output=True, text=True, timeout=5)
            _require(hashlib.sha256(config.read_bytes()).hexdigest() == original_hash,
                     "rollback did not restore original config bytes")
            _require(stat.S_IMODE(config.stat().st_mode) == 0o640, "permissions were not restored")
            _require(health.returncode == 0, "application health did not recover")
            _require(record["status"] == "succeeded", "rollback record did not succeed")
            return {"snapshot_id": sid, "execution_id": record["id"],
                    "config_sha256": original_hash, "mode_before": "0o600", "mode_after": "0o640",
                    "health_exit_code": health.returncode, "health_body": health.stdout.strip(),
                    "rollback_evidence": output}
        check("confirmed_config_permission_and_service_recovery", rollback)

        def timeout():
            marker = sandbox / "late-write"
            cmd = "sleep 0.4; printf late > " + shlex.quote(str(marker))
            core.approve(cmd)
            code, _, output = core.exec_change(cmd, timeout=0.05)
            record = execution.find_latest(cmd)
            time.sleep(0.5)  # Beyond the child's scheduled write; detects orphaned process groups.
            _require(code == 4 and record["status"] == "failed", "local timeout was not classified failed")
            _require(not marker.exists(), "child wrote after the timeout terminated its process group")
            return {"exit_code": code, "execution_id": record["id"],
                    "execution_status": record["status"], "delayed_child_write": False,
                    "output": output, "scope": "local timeout; SSH disconnect remains external"}
        check("local_timeout_stops_child_writes", timeout)

        def unknown():
            attempts = sandbox / "attempts"
            cmd = "printf 'attempt\\n' >> " + shlex.quote(str(attempts)) + "; kill -TERM $$"
            core.approve(cmd)
            code, _, _ = core.exec_change(cmd)
            record = execution.find_latest(cmd)
            _require(code < 0 and record["status"] == "unknown", "signal termination did not produce unknown")
            first = attempts.read_bytes()
            _require(first == b"attempt\n", "initial signal command did not write exactly once")
            core.approve(cmd)
            replay_code, _, replay_output = core.exec_change(cmd)
            _require(replay_code == core.EXEC_ABORT_UNKNOWN and attempts.read_bytes() == first,
                     "unknown command was replayed")
            return {"execution_id": record["id"], "first_exit_code": code,
                    "execution_status": record["status"], "replay_exit_code": replay_code,
                    "write_count": 1, "replay_output": replay_output,
                    "fault": "actual local shell SIGTERM; no SSH transport was simulated"}
        check("unknown_execution_blocks_replay", unknown)

        def llm_failure_recovery():
            usage.save_budget({"enabled": True, "daily_calls": 10, "daily_tokens": None})
            _control(sandbox, llm_status=503)
            failure = _llm_failure_probe(llm)
            failed_usage = usage.today()["calls"]
            _require(failed_usage == 1, "failed request was not budgeted exactly once")
            _control(sandbox, llm_status=200)
            response = llm.chat([{"role": "user", "content": "W8 local recovery probe"}], timeout=3)
            _require(response["content"] == "local-stub-ok", "LLM did not recover")
            _require(usage.today()["calls"] == 2, "recovery call was not budgeted")
            rows = usage.recent()
            _require(sum(bool(row.get("failed")) for row in rows) == 1,
                     "failure usage evidence missing")
            return {"transport": "real urllib to loopback HTTP stub", "failure": failure,
                    "failure_http_status": 503, "recovery_http_status": 200,
                    "failed_calls": failed_usage, "total_calls": usage.today()["calls"],
                    "response": response, "real_provider_executed": False}
        check("llm_http_failure_and_recovery", llm_failure_recovery)

        def notification_failure_recovery():
            notify.save_config({"url": base + "/hook", "format": "generic", "timeout_seconds": 3})
            _control(sandbox, notify_status=503)
            now = time.time()
            rec = {"task_id": "w8-local", "run_id": "w8-failed", "status": "succeeded",
                   "started_ts": now, "finished_ts": now, "verdict": "异常"}
            first = notify.process_run(rec)["notification"]
            _require(first and first["status"] == "failed", "notification 503 was not retained")
            _control(sandbox, notify_status=200)
            retried = notify.retry_pending(notif_id=first["id"])
            _require(len(retried) == 1 and retried[0]["status"] == "sent",
                     "failed notification did not recover through explicit retry")
            recovery_ts = time.time()
            recovered = notify.process_run(dict(rec, run_id="w8-recovered", verdict="正常",
                                               started_ts=recovery_ts,
                                               finished_ts=recovery_ts))["notification"]
            _require(recovered and recovered["kind"] == "recovery" and recovered["status"] == "sent",
                     "application recovery notification was lost")
            repeated = notify.process_run(dict(rec, run_id="w8-still-healthy", verdict="正常",
                                              started_ts=time.time(), finished_ts=time.time()))
            _require(repeated["notification"] is None, "healthy state sent duplicate recovery")
            return {"transport": "real urllib to loopback webhook stub", "failure": first,
                    "retry": retried[0], "recovery": recovered,
                    "duplicate_recovery_sent": False, "external_recipient_contacted": False}
        check("notification_failure_retry_and_recovery", notification_failure_recovery)

        def http_evidence():
            rows = [json.loads(line) for line in (sandbox / "http-evidence.jsonl").read_text().splitlines()]
            expected = {("/health", 200), ("/health", 503),
                        ("/v1/chat/completions", 503), ("/v1/chat/completions", 200),
                        ("/hook", 503), ("/hook", 200)}
            _require(expected <= {(row["path"], row["http_status"]) for row in rows},
                     "server-side request evidence is incomplete")
            return {"server_pid_is_separate": True, "bind_address": "127.0.0.1", "requests": rows}
        check("loopback_server_request_evidence", http_evidence)
    return {"checks": checks, "source_sha256": source}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="export JSON evidence atomically with mode 0600")
    parser.add_argument("--require-external", action="store_true",
                        help="exit 2 when real SSH/provider/channel/7-day gates remain blocked")
    parser.add_argument("--_worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_server", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_probe", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    def interrupted(_signum, _frame):
        raise SystemExit(128 + signal.SIGTERM)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        internal_modes = sum(value is not None for value in (args._worker, args._server, args._probe))
        if internal_modes:
            if internal_modes != 1 or args.output or args.require_external:
                raise ValueError("internal helper arguments cannot be combined")
            sandbox = Path(os.environ.get("OPSX_W8_SANDBOX", "")).resolve()
            _validate_session(sandbox)
            if args._worker:
                if args._worker.resolve() != sandbox:
                    raise ValueError("worker sandbox does not match parent session")
                print(json.dumps(_worker(sandbox), ensure_ascii=False))
                return 0
            if args._server:
                if args._server.resolve() != sandbox:
                    raise ValueError("server sandbox does not match parent session")
                return _serve(sandbox)
            return _probe(args._probe)
        report = run_acceptance()
        if args.output:
            write_report(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return exit_code(report, args.require_external)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("W8 local drill failed: " + type(error).__name__, file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
