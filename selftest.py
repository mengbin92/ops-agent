"""opsx 自检：python3 selftest.py，全部 assert，无框架。"""
import os
import tempfile

os.environ["OPSX_STATE_DIR"] = tempfile.mkdtemp(prefix="opsx-test-")

from pathlib import Path  # noqa: E402
import json  # noqa: E402

import core  # noqa: E402
import importlib
importlib.reload(core)  # 经 opsx 调用时 core 可能已导入，重载以应用 OPSX_STATE_DIR


def test_classify():
    assert core.classify("kubectl get pods -n default")[0] == "R0"
    assert core.classify("systemctl restart nginx")[0] == "R1"
    assert core.classify("kubectl apply -f deploy.yaml")[0] == "R2"
    assert core.classify("kubectl delete pod x")[0] == "R3"
    assert core.classify("docker ps && rm -rf /tmp/x")[0] == "R3"
    assert core.classify("some-unknown-write")[0] == "R2"


def test_audit():
    core.audit("test", foo=1)
    line = Path(core.STATE_DIR, "audit.jsonl").read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["event"] == "test" and rec["foo"] == 1


def test_stamp():
    core.approve("systemctl restart nginx", ttl_seconds=60)
    assert core.check_stamp("systemctl restart nginx") is True
    core.approve("systemctl restart oldsvc", ttl_seconds=60)
    p = Path(core.STATE_DIR, "approvals", core.cmd_hash("systemctl restart oldsvc") + ".json")
    s = json.loads(p.read_text())
    s["created"] -= 3600
    p.write_text(json.dumps(s))
    assert core.check_stamp("systemctl restart oldsvc") is False
    try:
        core.approve("rm -rf /tmp/x")
        raise AssertionError("R3 无 --force 应拒绝发戳")
    except core.OpsxError:
        pass


def test_check():
    assert core.check("docker ps")[0] == 0
    code, msg = core.check("systemctl restart unapproved-svc")
    assert code == 2 and "变更单" in msg


def test_snapshot_file_rollback():
    import tempfile as tf
    with tf.TemporaryDirectory() as td:
        f = Path(td) / "app.conf"
        f.write_text("v1")
        sid = core.snapshot_file(str(f))
        f.write_text("v2")
        core.rollback(sid)
        assert f.read_text() == "v1"


def test_snapshot_cmd():
    sid = core.snapshot_cmd("echo hello-opsx", name="greeting")
    report = core.rollback(sid)
    assert "hello-opsx" in report


def test_exec_aborts_on_snapshot_failure():
    core.approve("echo hi", ttl_seconds=60)
    code = core.exec_change("echo hi", snapshot_files=["/nonexistent/path/xyz"])
    assert code != 0


def test_exec_happy():
    import tempfile as tf
    with tf.TemporaryDirectory() as td:
        f = Path(td) / "a.txt"
        f.write_text("before")
        cmd = f"echo after > {f}"
        core.approve(cmd, ttl_seconds=60)
        code = core.exec_change(cmd, snapshot_files=[str(f)])
        assert code == 0 and f.read_text() == "after\n"  # echo 输出带尾换行


def test_rm_combined_flags_r3():
    assert core.classify("rm -fr /tmp/x")[0] == "R3"
    assert core.classify("rm -Rf /tmp/x")[0] == "R3"


def test_redirect_not_readonly():
    assert core.classify("cat /a > /b")[0] == "R2"
    assert core.classify("systemctl status x 2>/dev/null")[0] == "R0"


def test_find_mutation_not_readonly():
    assert core.classify("find /tmp -name x -delete")[0] == "R2"
    assert core.classify("find /tmp -name x -exec rm {} \\;")[0] == "R2"
    assert core.classify("find /tmp -name '*.log'")[0] == "R0"


def test_systemctl_stop_is_r3():
    assert core.classify("systemctl stop nginx")[0] == "R3"
    assert core.classify("systemctl disable nginx")[0] == "R3"
    assert core.classify("systemctl enable nginx")[0] == "R2"


def test_hook_malformed_json():
    import subprocess as sp
    import sys
    r = sp.run([sys.executable, str(core.REPO_DIR / "opsx"), "hook"],
               input="not-json{", capture_output=True, text=True)
    assert r.returncode == 2


def test_hook_empty_command():
    import subprocess as sp
    import sys
    r = sp.run([sys.executable, str(core.REPO_DIR / "opsx"), "hook"],
               input="{}", capture_output=True, text=True)
    assert r.returncode == 2


def test_rm_longopts_and_guarded_flags():
    assert core.classify("rm --recursive /tmp/x")[0] == "R3"
    assert core.classify("rm --recursive -f /tmp/x")[0] == "R3"
    assert core.classify("rm -I /tmp/x")[0] == "R2"  # 交互式提示，留 R2
    assert core.classify("rm -d /tmp/empty")[0] == "R2"  # 仅空目录，留 R2


def test_audit_rotation():
    core.AUDIT_ROTATE_BYTES = 10
    core.audit("big", payload="x" * 100)
    core.audit("after-rotate")
    assert (Path(core.STATE_DIR) / "audit.jsonl.1").exists()
    last = Path(core.STATE_DIR, "audit.jsonl").read_text().strip().splitlines()[-1]
    assert json.loads(last)["event"] == "after-rotate"
    core.AUDIT_ROTATE_BYTES = 10 * 1024 * 1024


def test_git_readonly_whitelist():
    assert core.classify("git status")[0] == "R0"
    assert core.classify("git log --oneline -5")[0] == "R0"
    assert core.classify("git diff HEAD~1")[0] == "R0"
    assert core.classify("git branch")[0] == "R0"
    assert core.classify("git branch -a")[0] == "R0"
    assert core.classify("git remote -v")[0] == "R0"
    assert core.classify("git stash list")[0] == "R0"
    assert core.classify("vm_stat")[0] == "R0"


def test_git_mutations_not_whitelisted():
    assert core.classify("git branch -d feature-x")[0] == "R2"
    assert core.classify("git tag v1.0")[0] == "R2"
    assert core.classify("git push origin main")[0] == "R2"
    assert core.classify("git commit -m x")[0] == "R2"


class McpClient:
    """std.io JSON-RPC 测试客户端：子进程拉起 mcp_server.py。"""

    def __init__(self):
        import subprocess as sp
        import sys
        self.proc = sp.Popen(
            [sys.executable, str(core.REPO_DIR / "mcp_server.py")],
            stdin=sp.PIPE, stdout=sp.PIPE, text=True,
        )
        self._id = 0

    def request(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def send_raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name, arguments=None):
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        assert "result" in resp, f"tools/call 无 result: {resp}"
        result = resp["result"]
        assert result["content"][0]["type"] == "text"
        text = result["content"][0]["text"]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:  # isError 内容为纯文本，非 JSON
            payload = text
        return result, payload

    def close(self):
        self.proc.terminate()


def test_mcp_handshake():
    c = McpClient()
    try:
        resp = c.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "0"},
        })
        r = resp["result"]
        assert r["serverInfo"]["name"] == "ops-guard"
        assert r["protocolVersion"] == "2025-03-26"
        assert r["capabilities"] == {"tools": {"listChanged": False}}
        c.notify("notifications/initialized")
        resp = c.request("ping")
        assert resp["result"] == {}
    finally:
        c.close()


def test_mcp_protocol_errors():
    c = McpClient()
    try:
        c.send_raw("not-json{")
        resp = json.loads(c.proc.stdout.readline())
        assert resp["error"]["code"] == -32700
        assert resp["id"] is None
        resp = c.request("no/such_method")
        assert resp["error"]["code"] == -32601
        resp = c.request("ping")  # 服务循环存活
        assert resp["result"] == {}
    finally:
        c.close()


def test_mcp_tools_list():
    c = McpClient()
    try:
        tools = c.request("tools/list")["result"]["tools"]
        assert len(tools) == 8
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == {
            "ops_check", "ops_snapshot_file", "ops_snapshot_cmd", "ops_approve",
            "ops_exec", "ops_rollback", "ops_list", "ops_audit",
        }
        assert by_name["ops_check"]["annotations"]["readOnlyHint"] is True
        assert by_name["ops_list"]["annotations"]["readOnlyHint"] is True
        assert by_name["ops_audit"]["annotations"]["readOnlyHint"] is True
        assert "annotations" not in by_name["ops_exec"]
        assert by_name["ops_exec"]["inputSchema"]["required"] == ["command"]
    finally:
        c.close()


def test_mcp_readonly_tools():
    c = McpClient()
    try:
        _, payload = c.call_tool("ops_check", {"command": "docker ps"})
        assert payload["allowed"] is True and payload["level"] == "R0"
        _, payload = c.call_tool("ops_check", {"command": "rm -rf /tmp/x"})
        assert payload["allowed"] is False and payload["level"] == "R3"
        result, _ = c.call_tool("ops_approve", {"command": "rm -rf /tmp/x"})  # R3 无 force
        assert result["isError"] is True
        result, _ = c.call_tool("ops_check", {})  # 缺参
        assert result["isError"] is True
        _, payload = c.call_tool("ops_audit", {"tail": 3})
        assert isinstance(payload["events"], list)
    finally:
        c.close()


def test_mcp_write_cycle():
    import tempfile as tf
    c = McpClient()
    try:
        with tf.TemporaryDirectory() as td:
            f = Path(td) / "mcp-demo.conf"
            f.write_text("v1")
            cmd = f"echo v2 > {f}"
            _, payload = c.call_tool("ops_approve", {"command": cmd, "ttl_seconds": 60})
            assert payload["level"] == "R2"
            _, payload = c.call_tool("ops_exec", {"command": cmd, "snapshot_files": [str(f)]})
            assert payload["exit_code"] == 0 and payload["snapshot_id"]
            assert f.read_text() == "v2\n"
            _, payload = c.call_tool("ops_rollback", {"snapshot_id": payload["snapshot_id"]})
            assert "已恢复文件" in payload["report"]
            assert f.read_text() == "v1"
            _, payload = c.call_tool("ops_list")
            assert payload["snapshots"] and payload["approvals"]
            cmd2 = f"echo v3 > {f}"
            _, payload = c.call_tool("ops_exec", {"command": cmd2})  # 无戳 → exit 2
            assert payload["exit_code"] == 2
    finally:
        c.close()


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failed = 0
    for fn in ALL:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(ALL) - failed}/{len(ALL)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
