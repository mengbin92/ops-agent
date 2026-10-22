"""opsx 自检：python3 selftest.py，全部 assert，无框架。"""
import os
import tempfile

os.environ["OPSX_STATE_DIR"] = tempfile.mkdtemp(prefix="opsx-test-")

from pathlib import Path  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

import core  # noqa: E402
import importlib
importlib.reload(core)  # 经 opsx 调用时 core 可能已导入，重载以应用 OPSX_STATE_DIR
os.chdir(Path(__file__).resolve().parent)  # 审批绑定 cwd：自检固定在仓库目录运行


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
        core.rollback(sid, confirm=True)
        assert f.read_text() == "v1"


def test_snapshot_cmd():
    sid = core.snapshot_cmd("echo hello-opsx", name="greeting")
    report = core.rollback(sid)
    assert "hello-opsx" in report


def test_exec_aborts_on_snapshot_failure():
    core.approve("printf hi", ttl_seconds=60)
    code, sid, _ = core.exec_change("printf hi", snapshot_files=["/nonexistent/path/xyz"])
    assert code == 3 and sid is None


def test_exec_happy():
    import tempfile as tf
    with tf.TemporaryDirectory() as td:
        f = Path(td) / "a.txt"
        f.write_text("before")
        cmd = f"echo after > {f}"
        core.approve(cmd, ttl_seconds=60)
        code, sid, output = core.exec_change(cmd, snapshot_files=[str(f)])
        assert code == 0 and f.read_text() == "after\n"  # echo 输出带尾换行
        assert sid and (Path(core.STATE_DIR) / "snapshots" / sid).exists()


def test_exec_output_captured_to_audit():
    core.approve("printf audit-capture-check", ttl_seconds=60)
    code, sid, output = core.exec_change("printf audit-capture-check")
    assert code == 0 and "audit-capture-check" in output
    last = Path(core.STATE_DIR, "audit.jsonl").read_text().strip().splitlines()[-1]
    rec = json.loads(last)
    assert rec["event"] == "exec" and "audit-capture-check" in rec["output"]


def test_exec_timeout():
    core.approve("sleep 5", ttl_seconds=60)
    code, _, output = core.exec_change("sleep 5", timeout=1)
    assert code == 4 and "超时" in output
    last = Path(core.STATE_DIR, "audit.jsonl").read_text().strip().splitlines()[-1]
    assert json.loads(last)["exit_code"] == 4


def test_rm_combined_flags_r3():
    assert core.classify("rm -fr /tmp/x")[0] == "R3"
    assert core.classify("rm -Rf /tmp/x")[0] == "R3"


def test_redirect_not_readonly():
    assert core.classify("cat /a > /b")[0] == "R2"
    assert core.classify("systemctl status x 2>/dev/null")[0] == "R0"


def test_write_marker_bypasses_closed():
    # fd1 显式重定向是写（此前被误判为 fd 豁免）
    assert core.classify("cat /etc/passwd 1>/tmp/x")[0] == "R2"
    assert core.classify("cat /a 1>>/b")[0] == "R2"
    # &> 全量重定向
    assert core.classify("cat /a &>/tmp/x")[0] == "R2"
    # 命令替换藏写操作（此前只读白名单直接放行）
    assert core.classify("cat $(rm -f /tmp/x)")[0] != "R0"
    assert core.classify("cat `rm -f /tmp/x`")[0] != "R0"
    assert core.classify("git status $(touch /tmp/x)")[0] != "R0"
    # 合法的 stderr 重定向仍算只读
    assert core.classify("kubectl get pods 2>&1")[0] == "R0"
    assert core.classify("journalctl -u x 2>/dev/null")[0] == "R0"


def test_check_stamp_corrupted_file():
    p = Path(core.STATE_DIR, "approvals", core.cmd_hash("systemctl restart broken") + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not-json{")
    assert core.check_stamp("systemctl restart broken") is False


def test_prune_approvals():
    core.approve("systemctl restart prune-me", ttl_seconds=60)
    p = Path(core.STATE_DIR, "approvals", core.cmd_hash("systemctl restart prune-me") + ".json")
    s = json.loads(p.read_text())
    s["created"] -= 3600  # 置为过期
    p.write_text(json.dumps(s))
    core.prune_approvals()
    assert not p.exists()


def test_collect_state():
    core.approve("systemctl restart collect-me", ttl_seconds=60)
    sid = core.snapshot_cmd("echo collect-state", name="cs")
    state = core.collect_state()
    assert sid in state["snapshots"]
    assert any(a["cmd"] == "systemctl restart collect-me" for a in state["approvals"])


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


def test_tool_schema_validation_before_side_effects():
    from agent import tools as agent_tools
    import mcp_server
    cmd = "rm -rf /tmp/schema-validation-must-not-run"
    cases = [
        ("ops_approve", {"command": cmd, "force": "false"}),
        ("ops_approve", {"command": cmd, "force": True, "ttl_seconds": True}),
        ("ops_exec", {"command": "echo ok", "snapshot_cmds": [{}]}),
        ("ops_exec", {"command": "echo ok", "snapshot_cmds": "echo bad"}),
        ("ops_list", []),
    ]
    ctx = agent_tools.ToolContext(ask_user=lambda q: "确认")
    for name, args in cases:
        out = agent_tools.dispatch(name, args, ctx)
        assert "错误" in out, (name, out)
        resp = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": name, "arguments": args}})
        assert resp["result"].get("isError"), resp
    assert not core.check_stamp(cmd)
    for method in ("initialize", "tools/call"):
        resp = mcp_server.handle_message({"jsonrpc": "2.0", "id": 2,
                                          "method": method, "params": []})
        assert resp["error"]["code"] == -32602, resp
    assert mcp_server.handle_message({"id": 3, "method": "ping"})["error"]["code"] == -32600
    resp = mcp_server.handle_message({"jsonrpc": "2.0", "id": 4,
        "method": "initialize", "params": {"protocolVersion": {"unexpected": "object"}}})
    assert resp["error"]["code"] == -32602
    result = mcp_server.call_tool("ops_audit", {"tail": 0})
    assert json.loads(result["content"][0]["text"])["events"] == []


def test_ops_exec_nonzero_exit_does_not_claim_no_change():
    from agent import tools as agent_tools
    import shlex
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "changed.txt"
        cmd = f"echo changed > {shlex.quote(str(f))}; exit 3"
        core.approve(cmd)
        out = agent_tools.dispatch("ops_exec", {"command": cmd}, agent_tools.ToolContext())
        assert f.read_text() == "changed\n" and "exit=3" in out
        assert "快照失败" not in out and "已中止" not in out


def test_hook_guarded_cli_and_snapshot_command_forms():
    import shlex
    import subprocess
    import sys
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, OPSX_STATE_DIR=str(Path(td) / "state"))
        entry = str(core.REPO_DIR / "opsx")
        f = Path(td) / "hook.conf"
        f.write_text("before")
        command = f"echo after > {shlex.quote(str(f))}"
        wrappers = [
            shlex.join([entry, "approve", command]),
            shlex.join([sys.executable, entry, "exec", command]),
        ]
        def hook(text):
            return subprocess.run([sys.executable, entry, "hook"],
                input=json.dumps({"tool_input": {"command": text}}), env=env,
                capture_output=True, text=True, timeout=5).returncode
        for wrapper in wrappers:
            assert hook(wrapper) == 0, wrapper
            for suffix in (" && echo extra", " > /tmp/hook-output", "\necho extra"):
                assert hook(wrapper + suffix) == 2, wrapper + suffix
        assert hook("/tmp/opsx approve 'systemctl restart x'") == 2
        denied = subprocess.run([sys.executable, entry, "exec", command], env=env,
                                capture_output=True, text=True)
        assert denied.returncode == 2 and f.read_text() == "before"
        subprocess.run([sys.executable, entry, "approve", command], env=env,
                       check=True, capture_output=True, text=True)
        executed = subprocess.run([sys.executable, entry, "exec", command,
                                  "--snapshot-file", str(f), "--snapshot-cmd", "echo prior-state"],
                                 env=env, capture_output=True, text=True)
        assert executed.returncode == 0 and f.read_text() == "after\n", executed.stderr
        sid = re.search(r"snapshot_id: ([^（\s]+)", executed.stdout).group(1)
        restored = subprocess.run([sys.executable, entry, "rollback", sid, "--yes"], env=env,
                                 capture_output=True, text=True, check=True)
        assert f.read_text() == "before" and "prior-state" in restored.stdout


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
        assert len(tools) == 10   # W5 新增 ops_executions（只读执行记录）
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == {
            "ops_check", "ops_snapshot_file", "ops_snapshot_cmd", "ops_approve",
            "ops_revoke", "ops_exec", "ops_rollback", "ops_executions",
            "ops_list", "ops_audit",
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
            _, payload = c.call_tool("ops_revoke", {"command": "systemctl restart mcp-none"})
            assert payload["revoked"] is False
            _, payload = c.call_tool("ops_approve", {"command": cmd, "ttl_seconds": 60})
            assert payload["level"] == "R2"
            _, payload = c.call_tool("ops_exec", {"command": cmd, "snapshot_files": [str(f)]})
            assert payload["exit_code"] == 0 and payload["snapshot_id"]
            assert f.read_text() == "v2\n"
            sid = payload["snapshot_id"]
            _, payload = c.call_tool("ops_rollback", {"snapshot_id": sid})  # 未确认：只返回计划
            assert payload["needs_confirm"] is True and "回滚计划" in payload["plan"]
            assert f.read_text() == "v2\n"  # 未确认不改文件
            _, payload = c.call_tool("ops_rollback", {"snapshot_id": sid, "confirm": True})
            assert "已恢复文件" in payload["report"]
            assert f.read_text() == "v1"
            _, payload = c.call_tool("ops_list")
            assert payload["snapshots"]
            assert all(a["cmd"] != cmd for a in payload["approvals"])
            cmd2 = f"echo v3 > {f}"
            _, payload = c.call_tool("ops_exec", {"command": cmd2})  # 无戳 → exit 2
            assert payload["exit_code"] == 2
    finally:
        c.close()


def _stub_llm(script):
    """按脚本返回 message：list[dict]，每次 chat 调用弹出一条。"""
    from agent import llm
    calls = {"n": 0}
    orig = llm.chat

    def fake(messages, tools=None, model=None, timeout=300, on_delta=None):
        m = dict(script[min(calls["n"], len(script) - 1)])
        calls["n"] += 1
        if on_delta and m.get("content"):
            on_delta(m["content"])  # 桩：一次性吐出全部文本
        return m

    llm.chat = fake
    return lambda: setattr(llm, "chat", orig)


def _tool_call(cid, name, arguments):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_agent_loop_readonly():
    from agent import loop, session as agent_session, tools as agent_tools
    restore = _stub_llm([
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("c1", "bash", {"command": "uname"})]},
        {"role": "assistant", "content": "结论是：只读命令可用。"},
    ])
    try:
        s = agent_session.Session()
        text = loop.run_turn("测试一下", s, agent_tools.ToolContext())
        assert "只读命令可用" in text
        tool_msgs = [m for m in s.messages if m.get("role") == "tool"]
        assert tool_msgs and "[exit=0]" in tool_msgs[0]["content"]
    finally:
        restore()


def test_agent_loop_guard_denies_write():
    from agent import loop, session as agent_session, tools as agent_tools
    restore = _stub_llm([
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("c1", "bash", {"command": "systemctl restart nginx"})]},
        {"role": "assistant", "content": "已按流程要求先提交变更单。"},
    ])
    try:
        s = agent_session.Session()
        loop.run_turn("重启 nginx", s, agent_tools.ToolContext(ask_user=None))
        tool_msgs = [m for m in s.messages if m.get("role") == "tool"]
        assert "opsx 拒绝" in tool_msgs[0]["content"]
        assert "变更单" in tool_msgs[0]["content"]
    finally:
        restore()


def test_agent_loop_approval_flow():
    import tempfile as tf
    from agent import loop, session as agent_session, tools as agent_tools
    with tf.TemporaryDirectory() as td:
        f = Path(td) / "svc.conf"
        f.write_text("v1")
        cmd = f"echo v2 > {f}"
        restore = _stub_llm([
            {"role": "assistant", "content": "", "tool_calls": [
                _tool_call("c2", "ops_approve", {"command": cmd, "ttl_seconds": 60})]},
            {"role": "assistant", "content": "", "tool_calls": [
                _tool_call("c3", "ops_exec", {"command": cmd, "snapshot_file": str(f)})]},
            {"role": "assistant", "content": "变更完成。"},
        ])
        try:
            s = agent_session.Session()
            answers = iter(["确认执行"])
            ctx = agent_tools.ToolContext(ask_user=lambda q: next(answers))
            text = loop.run_turn("改配置", s, ctx)
            assert "变更完成" in text
            assert f.read_text() == "v2\n"
            tool_msgs = [m for m in s.messages if m.get("role") == "tool"]
            assert any("审批戳已创建" in m["content"] for m in tool_msgs)
            assert any("执行成功" in m["content"] for m in tool_msgs)
        finally:
            restore()


def test_agent_session_persistence():
    from agent import session as agent_session
    s = agent_session.Session("test-persist-0001")
    s.append({"role": "user", "content": "hi"})
    s2 = agent_session.Session("test-persist-0001")
    assert s2.messages == [{"role": "user", "content": "hi"}]
    first = s2.path.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first)["format"] == 1


def test_session_security_and_partial_write_recovery():
    from agent import session as agent_session
    saved_dir = core.STATE_DIR
    old_umask = os.umask(0)
    try:
        with tempfile.TemporaryDirectory() as td:
            core.STATE_DIR = Path(td) / "private-state"
            for bad_id in ("../outside", "/tmp/outside", "", 12):
                try:
                    agent_session.Session(bad_id)
                    raise AssertionError(f"非法会话 id 应拒绝: {bad_id}")
                except ValueError:
                    pass
            s = agent_session.Session("test-secure")
            s.append({"role": "user", "content": "secret"})
            assert s.path.stat().st_mode & 0o777 == 0o600
            assert s.path.parent.stat().st_mode & 0o777 == 0o700
            assert core.STATE_DIR.stat().st_mode & 0o777 == 0o700
            with s.path.open("ab") as f:
                f.write(b'{"role":"user","content":"\xe4')
            resumed = agent_session.Session(s.id)
            assert resumed.messages == s.messages
            resumed.append({"role": "assistant", "content": "done"})
            assert agent_session.Session(s.id).messages == resumed.messages
            # 完整 JSON 恰好缺少末尾换行也能继续追加。
            s.path.write_bytes(s.path.read_bytes().rstrip(b"\n"))
            resumed = agent_session.Session(s.id)
            resumed.append({"role": "user", "content": "next"})
            assert len(agent_session.Session(s.id).messages) == 3
            before = list(resumed.messages)
            try:
                resumed.append({"role": "user", "content": object()})
                raise AssertionError("不可序列化消息应失败")
            except TypeError:
                pass
            assert resumed.messages == before
            s.path.write_text('{"format":2,"id":"test-secure"}\n', encoding="utf-8")
            try:
                agent_session.Session(s.id)
                raise AssertionError("未知版本不能当成 v1 读取")
            except ValueError:
                pass
            assert agent_session.list_sessions() == []
    finally:
        os.umask(old_umask)
        core.STATE_DIR = saved_dir


def test_loop_invalid_arguments_and_interruption_recovery():
    from agent import loop, llm, session as agent_session, tools as agent_tools
    original_chat, original_dispatch = llm.chat, agent_tools.dispatch
    calls, received = [], []
    agent_tools.dispatch = lambda name, args, ctx: calls.append((name, args)) or "ok"
    try:
        for arguments in ('{"command":', '["uname"]', 'null'):
            script = iter([
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "bad-args", "type": "function", "function": {
                        "name": "ops_list", "arguments": arguments}}]},
                {"role": "assistant", "content": "已处理参数错误"}])
            llm.chat = lambda *a, **kw: next(script)
            s = agent_session.Session()
            loop.run_turn("检查", s, agent_tools.ToolContext())
            assert "有效 JSON 对象" in s.messages[-2]["content"]
        assert calls == [], calls
        s = agent_session.Session()
        s.append({"role": "user", "content": "原任务"})
        s.append({"role": "assistant", "content": "", "tool_calls": [
            _tool_call("done", "ops_list", {}), _tool_call("interrupted", "ops_exec", {"command": "x"})]})
        s.append({"role": "tool", "tool_call_id": "done", "name": "ops_list", "content": "ok"})

        def recovered(messages, **kwargs):
            received.extend(messages)
            return {"role": "assistant", "content": "已核实"}

        llm.chat = recovered
        assert loop.run_turn("继续", s, agent_tools.ToolContext()) == "已核实"
        missing = [m for m in received if m.get("tool_call_id") == "interrupted"]
        assert len(missing) == 1 and "勿直接重试写操作" in missing[0]["content"]
        assert calls == []
        # 工具前的计划文本不能掩盖耗尽轮次。
        llm.chat = lambda *a, **kw: {"role": "assistant", "content": "先检查",
                                    "tool_calls": [_tool_call("loop", "ops_list", {})]}
        events = []
        text = loop.run_turn("检查", agent_session.Session(), agent_tools.ToolContext(),
                             max_turns=1, on_event=lambda k, p: events.append((k, p)))
        assert "最大轮次" in text and any("最大轮次" in str(p) for _, p in events)
    finally:
        llm.chat, agent_tools.dispatch = original_chat, original_dispatch


def test_agent_ask_user_degrades_without_tty():
    from agent import tools as agent_tools
    out = agent_tools.dispatch("ask_user", {"question": "确认？"}, agent_tools.ToolContext())
    assert "视为用户拒绝" in out


def test_markdown_render_blocks():
    from agent import markdown
    out = markdown.render("# 标题\n正文 **加粗** 和 `code`\n```\nraw **not bold**\n```")
    assert "\033[" in out  # 有 ANSI 上色
    assert "加粗" in out and "not bold" in out
    # 代码块内不解析行内样式
    code_line = [l for l in out.split("\n") if "not bold" in l][0]
    assert "**" in code_line
    # 标题行带样式
    head = [l for l in out.split("\n") if "标题" in l][0]
    assert head.startswith("\033[")


def test_markdown_render_table_cjk():
    from agent import markdown
    src = "| 检查项 | 结果 |\n|---|---|\n| 进程 | 无 |\n| docker ps | 运行中 |"
    out = markdown.render(src)
    plain = markdown.re.sub(r"\033\[[0-9;]*m", "", out)
    lines = [l for l in plain.split("\n") if l.strip()]
    assert len(lines) == 4  # 表头 + 分隔 + 两行
    # 分隔行在第二行，且列对齐（各行显示宽度一致，尾空格也算宽度）
    assert "─┼─" in lines[1]
    widths = {markdown._width(l) for l in lines}
    assert len(widths) == 1, f"列未对齐: {widths}"


def test_markdown_inline_link_and_list():
    from agent import markdown
    out = markdown.render("- 见 [文档](https://example.com)\n1. 第一项")
    plain = markdown.re.sub(r"\033\[[0-9;]*m", "", out)
    assert "文档(https://example.com)" in plain
    assert "• 见" in plain and "1. 第一项" in plain


def test_copy_to_clipboard():
    """剪贴板复制：自检默认**不碰**系统剪贴板。

    pbcopy 会覆盖用户当前复制的内容，而且没有可靠的"撤销"——内容已经丢了。
    以前这里真跑 pbcopy，于是每执行一次 `./opsx selftest` 就把用户剪贴板换成
    "ops-agent 复制测试 ✓"：用户按 Cmd+V 拿到的是这行字，必须重新复制一次。

    默认路径全部用替身验证（命令选择、stdin 入参、降级与失败文案），
    真实写入只在 OPSX_SELFTEST_CLIPBOARD=1 时执行，并尽量把原内容放回去。
    """
    import subprocess as sp
    import types
    from agent import repl

    text = "ops-agent 复制测试 ✓"
    real_sub, real_shutil = repl.subprocess, repl.shutil

    if os.environ.get("OPSX_SELFTEST_CLIPBOARD") == "1":
        before = None
        if real_shutil.which("pbpaste"):
            before = sp.run(["pbpaste"], capture_output=True, text=True).stdout
        try:
            assert "已复制" in repl.copy_to_clipboard(text)
            if real_shutil.which("pbcopy"):
                pasted = sp.run(["pbpaste"], capture_output=True, text=True).stdout
                assert pasted == text
        finally:
            if before is not None:  # 尽力恢复，别让自检留下自己的痕迹
                sp.run(["pbcopy"], input=before, text=True, timeout=5, check=False)
        return

    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw.get("input")))
        return sp.CompletedProcess(cmd, 0, "", "")

    try:
        repl.subprocess = types.SimpleNamespace(run=fake_run,
                                                SubprocessError=sp.SubprocessError)
        repl.shutil = types.SimpleNamespace(which=lambda name: f"/usr/bin/{name}")
        result = repl.copy_to_clipboard(text)
        assert "已复制" in result and calls, result
        assert calls[0][1] == text, "文本必须作为 stdin 传给剪贴板工具"
        assert calls[0][0][0] in ("pbcopy", "wl-copy", "xclip"), calls[0][0]

        # 没有任何剪贴板工具：优雅降级，不抛异常
        repl.shutil = types.SimpleNamespace(which=lambda name: None)
        assert "复制失败" in repl.copy_to_clipboard(text)

        # 工具存在但执行失败：返回说明而不是抛出去
        def boom(cmd, **kw):
            raise sp.CalledProcessError(1, cmd)

        repl.subprocess = types.SimpleNamespace(run=boom,
                                                SubprocessError=sp.SubprocessError)
        repl.shutil = types.SimpleNamespace(which=lambda name: f"/usr/bin/{name}")
        assert "复制失败" in repl.copy_to_clipboard(text)
    finally:
        repl.subprocess, repl.shutil = real_sub, real_shutil


def test_repl_history_roundtrip():
    import tempfile as tf
    from agent import repl
    if repl.readline is None:
        return  # 无 readline 平台跳过
    with tf.TemporaryDirectory() as td:
        repl.HISTORY_FILE = Path(td) / "hist"
        repl.readline.clear_history()
        for item in ["uname -a", "/copy", "重启 nginx"]:
            repl.readline.add_history(item)
        repl._save_history()
        repl.readline.clear_history()
        repl._load_history()
        n = repl.readline.get_current_history_length()
        got = [repl.readline.get_history_item(i) for i in range(1, n + 1)]
        assert got[-3:] == ["uname -a", "/copy", "重启 nginx"], got
        repl.readline.clear_history()


def test_llm_sse_stream():
    import io
    import os
    from agent import llm
    os.environ.setdefault("OPSAGENT_API_KEY", "test-dummy")  # _post 发请求前强制校验 key

    sse_lines = [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"content":"结论："}}]}',
        'data: {"choices":[{"delta":{"content":"一切正常"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"bash","arguments":"{\\"command\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"uname\\"}"}}]}}]}',
        'data: [DONE]',
    ]

    class FakeResp:
        def __enter__(self):
            return io.BytesIO(("\n".join(sse_lines) + "\n").encode())

        def __exit__(self, *a):
            return False

    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=300: FakeResp()
    try:
        deltas = []
        msg = llm.chat([{"role": "user", "content": "hi"}], on_delta=deltas.append)
        assert "".join(deltas) == "结论：一切正常"
        assert msg["content"] == "结论：一切正常"
        tc = msg["tool_calls"][0]
        assert tc["id"] == "c1" and tc["function"]["name"] == "bash"
        assert json.loads(tc["function"]["arguments"]) == {"command": "uname"}
    finally:
        urllib.request.urlopen = orig


def test_llm_stream_and_response_fail_closed():
    import io
    import http.client
    from unittest.mock import patch
    from agent import llm

    tool_delta = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
                  "function": {"name": "ops_list", "arguments": "{}"}}]}}]}
    truncated_openai = "data: " + json.dumps(tool_delta) + "\n"
    truncated_anthropic = ('data: {"type":"content_block_start","index":0,'
                           '"content_block":{"type":"tool_use","id":"c1",'
                           '"name":"ops_list","input":{}}}\n')
    cases = [
        ("openai", truncated_openai),
        ("openai", truncated_openai + 'data: {"error":{"message":"busy"}}\n'),
        ("openai", 'data: {"choices":[{"delta":{"content":"部分回复"}}]}\n'
                   'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n'),
        ("openai", 'data: {broken}\ndata: [DONE]\n'),
        ("openai", 'data: {"choices":[null]}\n'),
        ("openai", 'data: {"choices":[{"delta":{"tool_calls":[{"function":12}]}}]}\n'),
        ("openai", 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                   '"function":{"name":"ops_list","arguments":"{}"}}]}}]}\ndata: [DONE]\n'),
        ("anthropic", truncated_anthropic),
        ("anthropic", 'data: {"type":"content_block_delta","delta":12}\n'),
        ("anthropic", 'data: {"type":"content_block_start","content_block":12}\n'),
        ("anthropic", truncated_anthropic + 'data: {"type":"message_delta",'
                      '"delta":{"stop_reason":"max_tokens"}}\ndata: {"type":"message_stop"}\n'),
    ]
    for provider, payload in cases:
        with patch.dict(os.environ, {"OPSAGENT_PROVIDER": provider, "OPSAGENT_API_KEY": "test-dummy"}), \
                patch("urllib.request.urlopen", side_effect=lambda *a, **kw: io.BytesIO(payload.encode())):
            try:
                llm.chat([{"role": "user", "content": "x"}], on_delta=lambda p: None)
                raise AssertionError(f"应拒绝不完整或错误的 {provider} 流")
            except llm.LLMError:
                pass
    # Anthropic 首块可以自带文本，不应丢失。
    payload = ('data: {"type":"content_block_start","index":0,'
               '"content_block":{"type":"text","text":"开头"}}\n'
               'data: {"type":"message_stop"}\n')
    with patch.dict(os.environ, {"OPSAGENT_PROVIDER": "anthropic", "OPSAGENT_API_KEY": "test-dummy"}), \
            patch("urllib.request.urlopen", side_effect=lambda *a, **kw: io.BytesIO(payload.encode())):
        deltas = []
        assert llm.chat([{"role": "user", "content": "x"}], on_delta=deltas.append)["content"] == "开头"
        assert deltas == ["开头"]

    class FailedRead(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"partial", 20)

    for provider in ("openai", "anthropic"):
        with patch.dict(os.environ, {"OPSAGENT_PROVIDER": provider, "OPSAGENT_API_KEY": "test-dummy"}), \
                patch("urllib.request.urlopen", side_effect=lambda *a, **kw: FailedRead()):
            try:
                llm.chat([{"role": "user", "content": "x"}])
                raise AssertionError("读取失败应统一为 LLMError")
            except llm.LLMError as e:
                assert "读取失败" in str(e)
        for payload in ("null", "[]", '{"content":[{"type":"text","text":12}]}',
                        '{"choices":[{"message":{"role":"assistant","content":"ok","tool_calls":{}}}]}'):
            with patch.dict(os.environ, {"OPSAGENT_PROVIDER": provider, "OPSAGENT_API_KEY": "test-dummy"}), \
                    patch("urllib.request.urlopen", side_effect=lambda *a, **kw: io.BytesIO(payload.encode())):
                try:
                    llm.chat([{"role": "user", "content": "x"}])
                    raise AssertionError("无效响应必须统一拒绝")
                except llm.LLMError:
                    pass


def test_loop_compaction():
    from agent import loop
    big = "x" * 5000
    messages = [{"role": "system", "content": "sys"}]
    # 12 条大 tool 输出 → 总量远超阈值
    for i in range(12):
        messages.append({"role": "tool", "tool_call_id": f"t{i}", "content": big})
        messages.append({"role": "assistant", "content": f"回复{i}"})
    out = loop.compact(messages)
    # 早期 tool 被裁剪，最近 COMPACT_KEEP_RECENT 条保留原样
    compacted = [m for m in out if m.get("content") == loop.COMPACT_PLACEHOLDER]
    assert compacted, "应有被裁剪的早期 tool 输出"
    tail = out[-loop.COMPACT_KEEP_RECENT:]
    assert all(m.get("content") != loop.COMPACT_PLACEHOLDER for m in tail)
    # 低于阈值不动
    small = [{"role": "user", "content": "hi"}]
    assert loop.compact(small) is small


def test_session_list_sessions():
    import time
    from agent import session as agent_session
    s1 = agent_session.Session("test-list-0001")
    s1.append({"role": "user", "content": "第一个会话的问题"})
    time.sleep(0.01)
    s2 = agent_session.Session("test-list-0002")
    s2.append({"role": "user", "content": "第二个会话"})
    sessions = {s["id"]: s for s in agent_session.list_sessions()}
    assert sessions["test-list-0001"]["preview"] == "第一个会话的问题"
    assert sessions["test-list-0001"]["turns"] == 1
    # 倒序：新的在前
    ids = [s["id"] for s in agent_session.list_sessions()]
    assert ids.index("test-list-0002") < ids.index("test-list-0001")


def test_stream_printer_repaint(capsys=None):
    import io
    from agent import repl
    printer = repl.StreamPrinter()
    buf = io.StringIO()
    buf.isatty = lambda: True
    import sys as _sys
    orig = _sys.stdout
    _sys.stdout = buf
    try:
        printer.feed("# 标题")
        printer.feed("\n正文**加粗**")
        printer.finish()
    finally:
        _sys.stdout = orig
    out = buf.getvalue()
    assert "\033[" in out  # 第二次重绘发了光标移动序列
    assert out.count("标题") >= 2  # 增量重绘：旧内容被新内容覆盖打印


def test_repl_plain_stream_and_no_color():
    import io
    from unittest.mock import patch
    from agent import markdown, repl
    text = "# 标题\n正文**加粗**"
    assert "\033[" not in markdown.render(text, color=False)
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        printer = repl.StreamPrinter()
        printer.feed("# 标题")
        printer.feed("\n正文**加粗**")
        assert printer.finish() == text
    assert buf.getvalue() == text + "\n"
    with patch.object(repl, "_USE_COLOR", False), patch("sys.stdout", io.StringIO()) as out:
        repl.ReplState(None).emit("text", text)
        assert "\033[" not in out.getvalue()


def test_repl_resume_runs_goal_and_non_tty_has_no_approval_channel():
    from unittest.mock import patch
    from agent import repl
    state = repl.ReplState(None)
    calls, contexts = [], []
    def run_goal(st, ctx):
        calls.append(st)
        contexts.append(ctx)
    with patch.object(repl, "ReplState", return_value=state), \
         patch.object(repl.goal_mod, "update"), \
         patch.object(repl, "_continue_goal", side_effect=run_goal), \
         patch.object(repl, "_load_history"), patch.object(repl, "_save_history"), \
         patch("sys.stdin.isatty", return_value=False), \
         patch("builtins.input", side_effect=["/goal resume", "/quit"]):
        assert repl.main() == 0
    assert calls == [state] and contexts[0].ask_user is None
    with patch.object(repl, "ReplState", return_value=state), \
         patch.object(repl.loop, "run_turn", side_effect=KeyboardInterrupt), \
         patch.object(repl.goal_mod, "get", side_effect=repl.goal_mod.GoalError("目标文件损坏")), \
         patch.object(repl, "_load_history"), patch.object(repl, "_save_history"), \
         patch("builtins.input", side_effect=["检查", "/quit"]):
        assert repl.main() == 0


def test_result_color_semantics():
    from agent import repl
    assert repl.result_color("[opsx 拒绝] 该命令判定为 R2") == repl.C_RED
    assert repl.result_color("[错误] 文件不存在: /x") == repl.C_RED
    assert repl.result_color("[超时] 命令超过 120s") == repl.C_RED
    assert repl.result_color("审批戳已创建（级别 R1，TTL 900s）") == repl.C_GREEN
    assert repl.result_color("[完成] 执行成功（exit=0）") == repl.C_GREEN
    assert repl.result_color("已恢复文件: /etc/a.conf") == repl.C_GREEN
    assert repl.result_color("[ask_user 不可用] 当前形态无交互终端。视为用户拒绝") == repl.C_YELLOW
    assert repl.result_color("[exit=0]\nFilesystem 1K-blocks") == repl.C_DIM


def test_ask_style_danger_detection():
    from agent import repl
    border, title = repl._ask_style("变更单：rm -rf /tmp/x（R3，不可逆，无回滚方案）")
    assert repl.C_RED in border and "不可逆" in title
    border, title = repl._ask_style("变更单：systemctl restart nginx（R1 可逆），确认？")
    assert border == repl.C_YELLOW and "请求确认" in title


def test_color_respects_no_color_and_tty():
    from agent import repl
    orig = repl._USE_COLOR
    try:
        repl._USE_COLOR = False
        assert repl._c(repl.C_RED, "文本") == "文本"
        repl._USE_COLOR = True
        assert repl._c(repl.C_RED, "文本") == f"{repl.C_RED}文本{repl.C_RESET}"
    finally:
        repl._USE_COLOR = orig


def test_make_prompt_libedit_safe():
    from agent import repl
    orig_color, orig_libedit = repl._USE_COLOR, repl._is_libedit
    try:
        repl._USE_COLOR = True
        # libedit（macOS）：无色，根治历史重绘残影
        repl._is_libedit = lambda: True
        assert repl.make_prompt("ops> ") == "ops> "
        # GNU readline：\001 \002 包裹 ANSI，不计入显示宽度
        repl._is_libedit = lambda: False
        p = repl.make_prompt("ops> ")
        assert p.startswith("\001") and "\002ops> \001" in p and p.endswith("\002")
        # 禁色时原样
        repl._USE_COLOR = False
        assert repl.make_prompt("ops> ") == "ops> "
    finally:
        repl._USE_COLOR, repl._is_libedit = orig_color, orig_libedit


def test_bash_tool_denies_write_even_with_stamp():
    """bash 工具只放行 R0：即使有审批戳，写命令也必须走 ops_exec。"""
    from agent import tools as agent_tools
    core.approve("systemctl restart stamped-svc", ttl_seconds=60)
    out = agent_tools.dispatch("bash", {"command": "systemctl restart stamped-svc"},
                               agent_tools.ToolContext())
    assert "opsx 拒绝" in out and "ops_exec" in out
    # R0 仍直接执行
    out = agent_tools.dispatch("bash", {"command": "uname"}, agent_tools.ToolContext())
    assert "[exit=0]" in out


def test_rpc_peer_request_resolve_no_deadlock():
    """反向请求由另一线程回填时不会死锁（修复前读循环与等待同线程）。"""
    import threading
    import time as _t
    from agent import rpc
    peer = rpc.RpcPeer()
    captured = {}
    peer.send = lambda obj: captured.setdefault("id", obj.get("id"))  # 不写 stdout

    def fake_client():
        for _ in range(100):
            if captured.get("id"):
                peer.resolve({"jsonrpc": "2.0", "id": captured["id"], "result": "确认执行"})
                return
            _t.sleep(0.01)
        raise AssertionError("客户端未收到反向请求")

    t = threading.Thread(target=fake_client, daemon=True)
    t.start()
    result = peer.request("ask_user", {"question": "确认？"})
    t.join(timeout=2)
    assert result == "确认执行"


def test_rpc_protocol_validation_and_disconnect():
    import io
    import sys
    from unittest.mock import patch
    from agent import rpc
    inputs = [[], None, 12, {"jsonrpc": "1.0", "id": 1, "method": "ping"},
              {"jsonrpc": "2.0", "id": {}, "method": "ping"},
              {"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"text": 12}},
              {"jsonrpc": "2.0", "id": 3, "method": "prompt", "params": {"text": "x", "stream": "false"}},
              {"jsonrpc": "2.0", "id": 4, "method": "unknown"},
              {"jsonrpc": "2.0", "id": 5, "method": "ping"},
              {"jsonrpc": "2.0", "id": "srv-unsolicited", "result": "yes"}]
    stdout = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO("\n".join(json.dumps(m) for m in inputs) + "\n")), \
            patch.object(sys, "stdout", stdout):
        rpc.serve()
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len([m for m in replies if m.get("error", {}).get("code") == -32600]) == 5
    keyed = {m["id"]: m for m in replies if m["id"] is not None}
    assert keyed[2]["error"]["code"] == keyed[3]["error"]["code"] == -32602
    assert keyed[4]["error"]["code"] == -32601 and keyed[5]["result"] == {}
    assert "srv-unsolicited" not in keyed
    # stdin EOF 时，已排队 prompt 的确认也不能等到十分钟超时。
    stdout = io.StringIO()
    request = {"jsonrpc": "2.0", "id": 6, "method": "prompt", "params": {"text": "变更"}}

    def ask_at_eof(text, session, ctx, **kwargs):
        return ctx.ask_user("确认？")

    with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), \
            patch.object(sys, "stdout", stdout), patch.object(rpc.loop, "run_turn", ask_at_eof):
        rpc.serve()
    assert "拒绝" in json.loads(stdout.getvalue().splitlines()[-1])["result"]["text"]


def test_rpc_peer_rejects_unsolicited_and_error_responses():
    import threading
    from unittest.mock import patch
    from agent import rpc
    peer = rpc.RpcPeer()
    assert not peer.resolve({"jsonrpc": "2.0", "id": "srv-unknown", "result": "yes"})
    assert peer._pending == {}
    for answer in ({"error": {"code": -1, "message": "cancelled"}}, {"result": {"answer": "yes"}},
                   {"result": None}, {"result": "yes"}):
        peer.send = lambda request: peer.resolve({"jsonrpc": "2.0", "id": request["id"], **answer})
        assert peer.request("ask_user", {}) == ("yes" if answer == {"result": "yes"} else None)
        assert peer._pending == {}
    sent = threading.Event()
    peer.send = lambda request: sent.set()
    answers = []
    worker = threading.Thread(target=lambda: answers.append(peer.request("ask_user", {})), daemon=True)
    worker.start()
    assert sent.wait(2)
    peer.close()
    worker.join(2)
    assert not worker.is_alive() and answers == [None] and peer._pending == {}
    with patch.object(rpc, "ASK_USER_TIMEOUT", 0):
        peer = rpc.RpcPeer()
        peer.send = lambda request: None
        assert peer.request("ask_user", {}) is None and peer._pending == {}


def test_session_prune():
    from agent import session as agent_session
    import time as _t
    keep = agent_session.SESSION_KEEP
    agent_session.SESSION_KEEP = 3
    try:
        for i in range(5):
            s = agent_session.Session(f"test-prune-{i:04d}")
            s.append({"role": "user", "content": str(i)})
            _t.sleep(0.01)
        agent_session.Session()  # 新会话触发修剪
        remaining = [p for p in Path(core.STATE_DIR, "sessions").glob("test-prune-*.jsonl")]
        assert len(remaining) <= 3, remaining
    finally:
        agent_session.SESSION_KEEP = keep


def _write_targets():
    p = Path(core.STATE_DIR, "targets.json")
    p.write_text(json.dumps({
        "web1": {"host": "10.0.0.11", "user": "ops", "port": 2222,
                 "identity": "~/.ssh/id_test"},
        "db1": {"host": "db.internal.example"},
    }), encoding="utf-8")
    return p


def test_remote_targets_registry_and_argv():
    import remote
    _write_targets()
    argv = remote.ssh_argv("web1", "uptime")
    assert argv[0] == "ssh" and argv[-2:] == ["10.0.0.11", "uptime"]
    assert argv[argv.index("-l") + 1] == "ops"
    assert argv[argv.index("-p") + 1] == "2222"
    assert "BatchMode=yes" in argv  # 无人值守不弹密码提示
    assert any(a.startswith("ConnectTimeout=") for a in argv)
    assert str(Path("~/.ssh/id_test").expanduser()) in argv
    # 主机只能来自注册表：未注册 / 非法名一律拒绝
    for bad in ("evil.example", "../web1", "web1 x", ""):
        try:
            remote.ssh_argv(bad, "id")
            raise AssertionError(f"非法目标应拒绝: {bad!r}")
        except remote.RemoteError:
            pass


def test_remote_target_fields_cannot_inject_options():
    """注册表手误（host 以 '-' 开头/含空白）不能被 ssh 当成选项。"""
    import remote
    p = _write_targets()
    data = json.loads(p.read_text())
    for bad_host in ("-oProxyCommand=touch /tmp/pwned", "10.0.0.11 10.0.0.12"):
        data["evil"] = {"host": bad_host}
        p.write_text(json.dumps(data))
        try:
            remote.ssh_argv("evil", "id")
            raise AssertionError(f"应拒绝: {bad_host!r}")
        except remote.RemoteError:
            pass


def _fake_resolved_response(cmd):
    if 'OPSX-RESOLVED' not in cmd:
        return None
    import shlex
    import subprocess
    path = shlex.split(cmd.splitlines()[1])[0][2:]
    return subprocess.CompletedProcess([], 0, b'OPSX-RESOLVED\0' + path.encode() + b'\0', b'')


def test_remote_exec_snapshot_and_rollback():
    import subprocess
    import remote
    _write_targets()
    calls = []

    def fake_ssh(name, cmd, stdin=None, timeout=120):
        resolved = _fake_resolved_response(cmd)
        if resolved is not None:
            return resolved
        calls.append((name, cmd, stdin))
        if "OPSX-EXISTS" in cmd:
            return subprocess.CompletedProcess([], 0, stdout=b"OPSX-EXISTS\nremote-v1\n", stderr=b"")
        if cmd.startswith("cat --"):
            return subprocess.CompletedProcess([], 0, stdout=b"remote-v1\n", stderr=b"")
        if cmd.startswith("systemctl restart"):
            return subprocess.CompletedProcess([], 0, stdout=b"restarted\n", stderr=b"")
        if ".opsx-restore." in cmd and stdin is not None:
            # 模拟恢复脚本的后验输出（size 必须等于推上去的字节数）
            return subprocess.CompletedProcess(
                [], 0, stdout=f"opsx-restored had=1 size={len(stdin)} mode=640".encode(),
                stderr=b"")
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    orig = remote._ssh
    remote._ssh = fake_ssh
    try:
        # 本机审批戳不能用于远程目标
        core.approve("systemctl restart nginx", ttl_seconds=60)
        code, sid, _ = remote.exec_change("web1", "systemctl restart nginx")
        assert code == 2 and sid is None and calls == []
        # 戳按「目标+命令」绑定：web1 的戳不能拿到 db1 用
        core.approve("systemctl restart nginx", ttl_seconds=60, target="web1")
        assert core.check_stamp("systemctl restart nginx", target="web1") is True
        assert core.check_stamp("systemctl restart nginx", target="db1") is False

        code, sid, output = remote.exec_change(
            "web1", "systemctl restart nginx", snapshot_files=["/etc/nginx/nginx.conf"])
        assert code == 0 and sid and "restarted" in output
        meta = json.loads(Path(core.STATE_DIR, "snapshots", sid, "meta.json").read_text())
        assert meta["target"] == "web1"
        assert meta["items"][0]["src"] == "/etc/nginx/nginx.conf"
        archived = Path(core.STATE_DIR, "snapshots", sid, meta["items"][0]["dest"])
        assert archived.read_text() == "remote-v1\n"  # 快照落在本机

        report = remote.rollback(sid, confirm=True)
        assert "web1:/etc/nginx/nginx.conf" in report
        assert "回滚前副本" in report  # 先存副本再覆盖
        pushed = [c for c in calls if c[2] is not None]
        assert pushed and pushed[0][2] == b"remote-v1\n"   # 回滚经 stdin 推回
        # 必须是"临时文件 + rename"的原子写：直接 cat > 目标时断线会留下半份配置
        assert ".opsx-restore." in pushed[0][1] and "mv -f" in pushed[0][1]

        # 审计带目标，输出截断入库
        recs = [json.loads(line) for line in
                Path(core.STATE_DIR, "audit.jsonl").read_text().splitlines()]
        ex = [r for r in recs if r["event"] == "exec" and r.get("target") == "web1"]
        assert ex and ex[-1]["exit_code"] == 0 and "restarted" in ex[-1]["output"]

        # 本机快照仍由 core.rollback 负责
        with tempfile.TemporaryDirectory(prefix="opsx-local-") as td:
            f = Path(td) / "local.conf"
            f.write_text("v1")
            local_sid = core.snapshot_file(str(f))
            f.write_text("v2")
            assert "已恢复文件" in remote.rollback(local_sid, confirm=True) and f.read_text() == "v1"
    finally:
        remote._ssh = orig


def test_remote_snapshot_failure_aborts():
    import subprocess
    import remote
    _write_targets()
    core.approve("systemctl restart nginx", ttl_seconds=60, target="web1")
    sdir = Path(core.STATE_DIR, "snapshots")
    before = len(list(sdir.iterdir())) if sdir.exists() else 0

    def fake_ssh(name, cmd, stdin=None, timeout=120):
        return subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"cat: no such file")

    orig = remote._ssh
    remote._ssh = fake_ssh
    try:
        code, sid, _ = remote.exec_change("web1", "systemctl restart nginx",
                                          snapshot_files=["/missing.conf"])
        assert code == 3 and sid is None
        after = len(list(sdir.iterdir())) if sdir.exists() else 0
        assert after == before  # 失败快照不留垃圾
    finally:
        remote._ssh = orig


def test_bash_tool_remote_readonly():
    import subprocess
    import remote
    from agent import tools as agent_tools
    _write_targets()

    def fake_ssh(name, cmd, stdin=None, timeout=120):
        return subprocess.CompletedProcess([], 0, stdout=b"load 0.10 0.20", stderr=b"")

    orig = remote._ssh
    remote._ssh = fake_ssh
    try:
        out = agent_tools.dispatch("bash", {"command": "uptime", "target": "web1"},
                                   agent_tools.ToolContext())
        assert "target=web1" in out and "load 0.10" in out
        # 远程写命令仍必须走 ops_exec（不能靠 bash 带戳直连）
        out = agent_tools.dispatch("bash", {"command": "systemctl restart nginx",
                                            "target": "web1"}, agent_tools.ToolContext())
        assert "opsx 拒绝" in out and "ops_exec" in out
    finally:
        remote._ssh = orig
    # 未注册目标：工具层给出可读错误，不发起 ssh（走真实 ssh_argv 校验）
    out = agent_tools.dispatch("bash", {"command": "uptime", "target": "nope"},
                               agent_tools.ToolContext())
    assert "未注册" in out


def test_ops_list_shows_targets():
    from agent import tools as agent_tools
    _write_targets()
    out = agent_tools.dispatch("ops_list", {}, agent_tools.ToolContext())
    assert "== targets ==" in out and "web1" in out and "10.0.0.11" in out


def test_opsx_cli_remote_end_to_end():
    """真实 CLI + 假 ssh 二进制：验证 --target 参数接线、快照/回滚路径。"""
    import subprocess
    import sys
    state = tempfile.mkdtemp(prefix="opsx-cli-remote-")
    bindir = Path(state, "bin")
    bindir.mkdir()
    log = Path(state, "ssh.log")
    pushed = Path(state, "pushed.bin")
    fake = bindir / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$*" in\n'
        '  *"OPSX-RESOLVED"*) printf \'OPSX-RESOLVED\\000/etc/nginx/nginx.conf\\000\' ;;\n'
        '  *"OPSX-EXISTS"*) printf \'OPSX-EXISTS\\n\'; printf \'remote-v1\\n\' ;;\n'
        "  *\"cat -- \"*) printf 'remote-v1\\n' ;;\n"
        '  *".opsx-restore."*) cat > "$OPSX_TEST_PUSHED" ; '
        'printf \'opsx-restored had=1 size=%s mode=600\\n\' "$(wc -c < "$OPSX_TEST_PUSHED" | tr -d \' \')" ;;\n'
        "  *) printf 'remote-ok\\n' ;;\n"
        'esac\n'
    )
    fake.chmod(0o755)
    Path(state, "targets.json").write_text(json.dumps({"web1": {"host": "10.0.0.11"}}))
    env = dict(os.environ, OPSX_STATE_DIR=state,
               PATH=f"{bindir}:{os.environ['PATH']}", OPSX_TEST_PUSHED=str(pushed))
    opsx = str(core.REPO_DIR / "opsx")

    def run(*a):
        return subprocess.run([sys.executable, opsx, *a], capture_output=True, text=True, env=env)

    r = run("targets")
    assert r.returncode == 0 and "web1" in r.stdout, r.stderr
    r = run("approve", "--target", "web1", "systemctl", "restart", "nginx")
    assert r.returncode == 0 and "目标 web1" in r.stdout, r.stderr
    r = run("exec", "--target", "web1", "--snapshot-file", "/etc/nginx/nginx.conf",
            "systemctl", "restart", "nginx")
    assert r.returncode == 0, r.stderr
    m = re.search(r"snapshot_id: (\S+?)（", r.stdout)
    assert m, r.stdout
    sid = m.group(1)
    r = run("rollback", sid, "--yes")
    assert r.returncode == 0 and "web1:/etc/nginx/nginx.conf" in r.stdout, r.stderr
    assert pushed.read_bytes() == b"remote-v1\n"
    log_text = log.read_text()
    assert ".opsx-restore." in log_text and "mv -f" in log_text
    assert "cat > /etc/nginx/nginx.conf" not in log_text   # 不再直接截断覆盖
    # 独立远程快照（opsx snapshot --target --cmd）
    r = run("snapshot", "--target", "web1", "--cmd", "uptime", "--name", "up")
    assert r.returncode == 0 and r.stdout.strip(), r.stderr
    sid2 = r.stdout.strip()
    meta2 = json.loads(Path(state, "snapshots", sid2, "meta.json").read_text())
    assert meta2["target"] == "web1" and meta2["items"][0]["cmd"] == "uptime"
    assert Path(state, "snapshots", sid2, "cmd-up.txt").read_text() == "remote-ok\n"
    # 未注册目标：明确指出配置缺失，且不发起 ssh
    r = run("exec", "--target", "nope", "systemctl", "restart", "nginx")
    assert r.returncode == 1 and "未注册" in r.stderr, (r.returncode, r.stderr)
    assert not log.read_text().count("nope")


def test_snapshot_cmds_also_guarded():
    """快照抓取命令同样过守门：不能把写操作藏进 exec 的 snapshot_cmds。"""
    import tempfile as tf
    with tf.TemporaryDirectory() as td:
        cap_target = f"{td}/cap-xyz"
        main = f"echo main > {td}/m.txt"
        core.approve(main, ttl_seconds=60)
        code, sid, _ = core.exec_change(main, snapshot_cmds=[(f"rm -rf {cap_target}", "")])
        assert code == 2 and sid is None
        assert not Path(cap_target).exists()
        # 即使获批，非R0命令也不能通过采集入口绕开执行协议
        core.approve("printf cap-ok", ttl_seconds=60)
        code, sid, _ = core.exec_change(main, snapshot_cmds=[("printf cap-ok", "cap")])
        assert code == 2 and sid is None
        code, sid, _ = core.exec_change(main, snapshot_cmds=[("echo captured", "cap")])
        assert code == 0 and sid
        cap = Path(core.STATE_DIR, "snapshots", sid, "cmd-cap.txt")
        assert "captured" in cap.read_text()


def test_snapshot_id_path_traversal_rejected():
    """snapshot_id 来自模型：带路径分隔符就能借 rollback 覆盖任意文件。"""
    import shutil
    import remote
    outside = Path(core.STATE_DIR).parent / "opsx-trav-check"
    victim = Path(tempfile.mkdtemp(prefix="opsx-victim-")) / "v.txt"
    victim.write_text("keep")
    outside.mkdir(exist_ok=True)
    (outside / "meta.json").write_text(json.dumps({"items": [
        {"type": "file", "dest": str(victim), "src": str(victim), "mode": 0o644}]}))
    try:
        for bad in ("../../opsx-trav-check", "/etc", "..", "a/b", ""):
            for mod in (core, remote):
                try:
                    mod.rollback(bad)
                    raise AssertionError(f"应拒绝非法快照 id: {bad!r}")
                except core.OpsxError:
                    pass
        assert victim.read_text() == "keep"
    finally:
        shutil.rmtree(outside, ignore_errors=True)
        shutil.rmtree(victim.parent, ignore_errors=True)


def test_snapshot_dirs_private():
    """快照里可能有密钥/配置：目录必须只有本人可读（远程拉回的文件同受保护）。"""
    assert core.begin_snapshot()[1].stat().st_mode & 0o077 == 0
    core.audit("perm-probe")
    assert core.STATE_DIR.stat().st_mode & 0o077 == 0


def test_remote_timeout_paths():
    import subprocess
    import remote
    _write_targets()
    core.approve("systemctl restart nginx", ttl_seconds=60, target="web1")

    def timeout_ssh(name, cmd, stdin=None, timeout=120):
        raise subprocess.TimeoutExpired(cmd, timeout)

    orig = remote._ssh
    remote._ssh = timeout_ssh
    try:
        assert remote.run("web1", "uptime")[0] == 4
        code, sid, output = remote.exec_change("web1", "systemctl restart nginx")
        assert code == 4 and sid is None and "超时" in output
    finally:
        remote._ssh = orig


def test_remote_rollback_failure_reported():
    import subprocess
    import remote
    _write_targets()
    core.approve("systemctl restart nginx", ttl_seconds=60, target="web1")

    def fake_ssh(name, cmd, stdin=None, timeout=120):
        resolved = _fake_resolved_response(cmd)
        if resolved is not None:
            return resolved
        if "OPSX-EXISTS" in cmd:
            return subprocess.CompletedProcess([], 0, stdout=b"OPSX-EXISTS\nv1", stderr=b"")
        if cmd.startswith("cat --"):
            return subprocess.CompletedProcess([], 0, stdout=b"v1", stderr=b"")
        if cmd.startswith("systemctl restart"):
            return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"denied")

    orig = remote._ssh
    remote._ssh = fake_ssh
    try:
        code, sid, _ = remote.exec_change("web1", "systemctl restart nginx",
                                          snapshot_files=["/etc/nginx/nginx.conf"])
        assert code == 0 and sid
        try:
            remote.rollback(sid, confirm=True)
            raise AssertionError("推回失败应报错")
        except remote.RemoteError as e:
            assert "回滚失败" in str(e)
    finally:
        remote._ssh = orig


def test_ops_list_survives_corrupt_registry():
    from agent import tools as agent_tools
    core.snapshot_cmd("echo corrupt-reg", name="cr")
    p = Path(core.STATE_DIR, "targets.json")
    try:
        for content in (b"{not json", b"\xff\xfe"):
            p.write_bytes(content)
            out = agent_tools.dispatch("ops_list", {}, agent_tools.ToolContext())
            assert "== snapshots ==" in out and "== targets ==" in out and "解析失败" in out
    finally:
        _write_targets()



def _fake_anthropic_response(content_blocks):
    import io

    class FakeResp:
        def __enter__(self):
            return io.BytesIO(json.dumps({"content": content_blocks}).encode())

        def __exit__(self, *a):
            return False

    return FakeResp()


def _with_urlopen(fake, fn):
    """临时替换 urlopen，捕获请求；返回 (结果, 捕获的 {url, headers, body})。"""
    import urllib.request
    captured: dict = {}

    def wrapper(req, timeout=300):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        return fake(req, timeout)

    orig = urllib.request.urlopen
    urllib.request.urlopen = wrapper
    try:
        return fn(), captured
    finally:
        urllib.request.urlopen = orig


def test_anthropic_provider_request_and_response():
    from agent import llm, llm_anthropic
    env = {"OPSAGENT_PROVIDER": "anthropic", "OPSAGENT_API_KEY": "test-dummy",
           "OPSAGENT_BASE_URL": "https://proxy.example/v1", "OPSAGENT_MODEL": "claude-test"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    def fake(req, timeout):
        return _fake_anthropic_response([
            {"type": "text", "text": "结论："},
            {"type": "tool_use", "id": "tu1", "name": "bash", "input": {"command": "uname"}}])

    def call():
        messages = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "tool_calls": [_tool_call("c1", "bash", {"command": "uname"})]},
            {"role": "tool", "tool_call_id": "c1", "name": "bash", "content": "Darwin"},
            {"role": "tool", "tool_call_id": "c2", "name": "bash", "content": "Linux"},
            {"role": "assistant", "content": "读完了"},
        ]
        tools = [{"type": "function", "function": {
            "name": "bash", "description": "d",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                           "required": ["command"]}}}]
        return llm.chat(messages, tools=tools)

    try:
        msg, cap = _with_urlopen(fake, call)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert cap["url"] == "https://proxy.example/v1/messages"
    assert cap["headers"]["x-api-key"] == "test-dummy"
    assert cap["headers"]["anthropic-version"] == llm_anthropic.API_VERSION
    body = cap["body"]
    assert body["model"] == "claude-test" and body["stream"] is False and body["max_tokens"] > 0
    assert body["system"] == "SYS"
    assert body["tools"][0]["input_schema"]["required"] == ["command"]
    assert "type" not in body["tools"][0]  # OpenAI 包装层已去除
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"], roles
    assistant = body["messages"][1]["content"]
    assert assistant[0]["type"] == "tool_use" and assistant[0]["input"] == {"command": "uname"}
    # 连续 tool 结果合并成一条 user 消息（两条 tool_result）
    results = body["messages"][2]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    assert [r["tool_use_id"] for r in results] == ["c1", "c2"]
    # 响应 → OpenAI 形状
    assert msg["content"] == "结论："
    tc = msg["tool_calls"][0]
    assert tc["id"] == "tu1" and tc["function"]["name"] == "bash"
    assert json.loads(tc["function"]["arguments"]) == {"command": "uname"}


def test_anthropic_defaults_and_unknown_provider():
    from agent import llm, llm_anthropic
    saved = {k: os.environ.get(k) for k in
             ("OPSAGENT_PROVIDER", "OPSAGENT_API_KEY", "OPSAGENT_BASE_URL", "OPSAGENT_MODEL")}
    os.environ.pop("OPSAGENT_BASE_URL", None)
    os.environ.pop("OPSAGENT_MODEL", None)
    os.environ["OPSAGENT_PROVIDER"] = "anthropic"
    os.environ["OPSAGENT_API_KEY"] = "test-dummy"

    def fake(req, timeout):
        return _fake_anthropic_response([{"type": "text", "text": "ok"}])

    try:
        msg, cap = _with_urlopen(fake, lambda: llm.chat([{"role": "user", "content": "x"}]))
        assert cap["url"] == "https://api.anthropic.com/v1/messages"
        assert cap["body"]["model"] == llm_anthropic.DEFAULT_MODEL
        assert msg["content"] == "ok"
        os.environ["OPSAGENT_PROVIDER"] = "bogus"
        try:
            llm.chat([{"role": "user", "content": "x"}])
            raise AssertionError("未知 provider 应报错")
        except llm.LLMError as e:
            assert "未知 provider" in str(e)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_anthropic_sse_stream():
    import io
    from agent import llm
    saved = {k: os.environ.get(k) for k in
             ("OPSAGENT_PROVIDER", "OPSAGENT_API_KEY", "OPSAGENT_BASE_URL")}
    os.environ.update({"OPSAGENT_PROVIDER": "anthropic", "OPSAGENT_API_KEY": "test-dummy"})
    os.environ.pop("OPSAGENT_BASE_URL", None)

    sse = "\n".join([
        'event: message_start',
        'data: {"type":"message_start","message":{"id":"m1"}}',
        'event: content_block_start',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"结论："}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"一切正常"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu1","name":"bash","input":{}}}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":"}}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"uname\\"}"}}',
        'data: {"type":"content_block_stop","index":1}',
        'event: message_stop',
        'data: {"type":"message_stop"}',
    ]) + "\n"

    class FakeResp:
        def __enter__(self):
            return io.BytesIO(sse.encode())

        def __exit__(self, *a):
            return False

    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=300: FakeResp()
    try:
        deltas: list[str] = []
        msg = llm.chat([{"role": "user", "content": "hi"}], on_delta=deltas.append)
        assert "".join(deltas) == "结论：一切正常"
        assert msg["content"] == "结论：一切正常"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"command": "uname"}

        err_sse = ('data: {"type":"error","error":{"type":"overloaded_error",'
                   '"message":"busy"}}\n')
        urllib.request.urlopen = lambda req, timeout=300: type("R", (), {
            "__enter__": lambda self: io.BytesIO(err_sse.encode()),
            "__exit__": lambda self, *a: False})()
        try:
            llm.chat([{"role": "user", "content": "hi"}], on_delta=lambda _t: None)
            raise AssertionError("流式 error 事件应抛 LLMError")
        except llm.LLMError as e:
            assert "流式错误" in str(e)
    finally:
        urllib.request.urlopen = orig
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_output_spill_and_read_pagination():
    from agent import tools as agent_tools
    ctx = agent_tools.ToolContext()
    td = tempfile.TemporaryDirectory(prefix="opsx-spill-")
    big = Path(td.name) / "big.log"
    big.write_text("\n".join(f"line {i}" for i in range(1, 20001)) + "\n", encoding="utf-8")
    out = agent_tools.dispatch("bash", {"command": f"cat {big}"}, ctx)
    assert out.startswith("[exit=0]")
    m = re.search(r"完整内容: (\S+\.txt)", out)
    assert m, out[:300]
    path = Path(m.group(1))
    assert path.exists() and path.stat().st_size > agent_tools.OUTPUT_LIMIT
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert str(agent_tools.SPILL_DIR) in str(path)
    # 预览被截断
    assert len(out) < path.stat().st_size
    # read 分段读到尾部
    head = agent_tools.dispatch("read", {"path": str(path), "offset": 1, "limit": 2}, ctx)
    assert head.startswith("line 1\nline 2") and "继续读取 offset=3" in head
    tail = agent_tools.dispatch("read", {"path": str(path), "offset": 19990, "limit": 20}, ctx)
    assert "line 20000" in tail and "继续读取" not in tail
    # 越界/非法参数
    assert "超出文件行数" in agent_tools.dispatch(
        "read", {"path": str(path), "offset": 99999}, ctx)
    assert "limit 必须是正整数" in agent_tools.dispatch(
        "read", {"path": str(path), "limit": 0}, ctx)
    assert "[错误] 文件不存在" in agent_tools.dispatch("read", {"path": "/nonexistent/x"}, ctx)
    # 超长行：截断提示与下一页 offset 必须一致（此前会谎报"第 1-2000 行"）
    longline = Path(td.name) / "longline.txt"
    longline.write_text("x" * 31000 + "\nsecond\n", encoding="utf-8")
    r = agent_tools.dispatch("read", {"path": str(longline)}, ctx)
    assert "单次读取超过" in r and "继续读取 offset=2" in r
    assert "第 1-2000 行" not in r
    td.cleanup()


def test_read_large_lines_uses_bounded_memory():
    import tracemalloc
    from agent import tools as agent_tools
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "long.log"
        with p.open("w") as f:
            for _ in range(100):
                f.write("x" * 80_000)
            f.write("\nlast line\n")
        tracemalloc.start()
        try:
            out = agent_tools.dispatch("read", {"path": str(p), "limit": 1},
                                       agent_tools.ToolContext())
            tail = agent_tools.dispatch("read", {"path": str(p), "offset": 2},
                                        agent_tools.ToolContext())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert "单次读取超过" in out and "offset=2" in out
        assert tail == "last line" and peak < 1_000_000, (tail, peak)


def test_spill_prune():
    from agent import tools as agent_tools
    orig, td = agent_tools.SPILL_DIR, tempfile.TemporaryDirectory(prefix="opsx-prune-")
    agent_tools.SPILL_DIR = Path(td.name)
    try:
        made = []
        for i in range(3):
            f = agent_tools.SPILL_DIR / f"prune-test-{i}.txt"
            f.write_text("x")
            os.utime(f, (1000 + i, 1000 + i))
            made.append(f)
        agent_tools._prune_outputs(keep=2)
        assert not made[0].exists() and made[1].exists() and made[2].exists()
    finally:
        agent_tools.SPILL_DIR = orig
        td.cleanup()


def test_revoke_stamp():
    import subprocess
    import sys as _sys
    from agent import tools as agent_tools
    cmd = "systemctl restart revoke-svc"
    assert core.revoke(cmd) is False  # 未建戳
    core.approve(cmd, ttl_seconds=60)
    assert core.check_stamp(cmd) is True
    out = agent_tools.dispatch("ops_revoke", {"command": cmd},
                               agent_tools.ToolContext())
    assert "已撤销" in out and core.check_stamp(cmd) is False
    # 目标绑定：本机 revoke 不会撤销 web1 的戳
    _write_targets()
    core.approve(cmd, ttl_seconds=60, target="web1")
    assert core.revoke(cmd) is False
    assert core.check_stamp(cmd, "web1") is True
    # CLI
    cli_cmd = "systemctl restart cli-revoke"
    core.approve(cli_cmd, ttl_seconds=60)
    r = subprocess.run([_sys.executable, "opsx", "revoke", cli_cmd], capture_output=True,
                       text=True, cwd=str(Path(__file__).resolve().parent))
    assert r.returncode == 0 and "已撤销" in r.stdout, r.stderr
    assert core.check_stamp(cli_cmd) is False
    assert core.revoke(cmd, "web1") is True  # 清理本用例留下的远程戳


def test_rpc_stream_events():
    import io
    import sys as _sys
    from agent import rpc
    restore = _stub_llm([{"role": "assistant", "content": "巡检完成"}])
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "prompt",
                    "params": {"text": "检查", "stream": True}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "prompt",
                      "params": {"text": "再查"}}) + "\n")
    stdout = io.StringIO()
    orig_in, orig_out = _sys.stdin, _sys.stdout
    _sys.stdin, _sys.stdout = stdin, stdout
    try:
        rpc.serve()
    finally:
        _sys.stdin, _sys.stdout = orig_in, orig_out
        restore()
    lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
    notes = [l for l in lines if l.get("method") == "event"]
    kinds = [n["params"]["kind"] for n in notes]
    assert "delta" in kinds and "stream_end" in kinds, kinds
    delta = [n for n in notes if n["params"]["kind"] == "delta"][0]
    assert delta["params"]["payload"] == "巡检完成"
    result = [l for l in lines if l.get("id") == 1][0]["result"]
    assert result["text"] == "巡检完成" and "events" not in result
    assert result["session_id"]
    # 不传 stream 时保持旧契约：一次性返回 events
    plain = [l for l in lines if l.get("id") == 2][0]["result"]
    assert [e["kind"] for e in plain["events"]] == ["text"], plain
    assert plain["events"][0]["payload"] == "巡检完成"




def test_skill_catalog_and_load():
    from agent import tools as agent_tools
    ctx = agent_tools.ToolContext()
    catalog = agent_tools.dispatch("skill", {}, ctx)
    for name in ("nginx-5xx", "disk-full", "safe-service-restart"):
        assert name in catalog, catalog
    body = agent_tools.dispatch("skill", {"name": "nginx-5xx"}, ctx)
    assert "systemctl status nginx" in body and "变更单" in body
    assert "systemctl status nginx" in agent_tools.dispatch(
        "skill", {"name": "nginx-5xx.md"}, ctx)  # 允许带扩展名
    # 用户目录同名覆盖仓库
    user_dir = Path(core.STATE_DIR, "skills")
    user_dir.mkdir(parents=True, exist_ok=True)
    override = user_dir / "nginx-5xx.md"
    override.write_text("---\ndescription: 覆盖版\n---\n# 覆盖正文\n", encoding="utf-8")
    try:
        assert "覆盖正文" in agent_tools.dispatch("skill", {"name": "nginx-5xx"}, ctx)
        assert "覆盖版" in agent_tools.dispatch("skill", {}, ctx)
    finally:
        override.unlink(missing_ok=True)
    # 名字形状校验与不存在
    assert "非法 skill 名" in agent_tools.dispatch("skill", {"name": "../etc/passwd"}, ctx)
    assert "未找到 skill" in agent_tools.dispatch("skill", {"name": "no-such-skill"}, ctx)


def test_skill_reads_are_bounded_and_names_valid():
    from agent import skills
    user_dir = Path(core.STATE_DIR, "skills")
    user_dir.mkdir(parents=True, exist_ok=True)
    big = user_dir / "big-skill.md"
    big.write_text("# 大文件\n" + "x" * (skills.SKILL_LIMIT + 100), encoding="utf-8")
    bad = user_dir / "bad name.md"
    bad.write_text("---\ndescription: 名字非法\n---\nbody\n", encoding="utf-8")
    try:
        body = skills.load("big-skill")
        assert "已截断" in body and len(body) < skills.SKILL_LIMIT + 200
        names = [m["name"] for m in skills.catalog()]
        assert "big-skill" in names and "bad name" not in names
    finally:
        big.unlink(missing_ok=True)
        bad.unlink(missing_ok=True)


def test_subagent_parallel_and_readonly():
    import time as _t
    from agent import loop, tools as agent_tools
    orig = loop.run_turn
    seen = []

    def fake(task, session, ctx, **kwargs):
        seen.append((task, ctx.allowed, kwargs.get("system"), kwargs.get("max_turns"),
                     kwargs.get("model")))
        _t.sleep(0.3)
        return f"结论:{task}"

    loop.run_turn = fake
    try:
        started = _t.monotonic()
        out = agent_tools.dispatch("subagent", {"tasks": ["检查 A", "检查 B"]},
                                   agent_tools.ToolContext(model="m-test"))
        elapsed = _t.monotonic() - started
    finally:
        loop.run_turn = orig
    assert "结论:检查 A" in out and "结论:检查 B" in out
    assert elapsed < 0.55, elapsed  # 并行：总耗时约等于单个任务
    assert len(seen) == 2
    assert all(ctx_allowed == agent_tools.SUBAGENT_TOOLS for _, ctx_allowed, _, _, _ in seen)
    assert all(system == loop.SUBAGENT_PROMPT for _, _, system, _, _ in seen)
    assert all(turns == agent_tools.SUBAGENT_MAX_TURNS for _, _, _, turns, _ in seen)
    assert all(model == "m-test" for _, _, _, _, model in seen)
    # 子代理上下文：写工具被拒、工具列表只剩只读
    sub_ctx = agent_tools.ToolContext(allowed=agent_tools.SUBAGENT_TOOLS)
    assert "只读上下文" in agent_tools.dispatch(
        "ops_exec", {"command": "systemctl restart x"}, sub_ctx)
    names = {s["function"]["name"] for s in agent_tools.tool_schemas(sub_ctx)}
    # events 是只读时间线查询（W4），与 bash/read 同级；写工具仍不可见。
    assert names == {"bash", "read", "skill", "events", "ops_executions", "ops_list"}
    assert "subagent" not in names  # 不能继续嵌套派生
    # 空任务 / 超量
    assert "[错误]" in agent_tools.dispatch("subagent", {"tasks": []}, agent_tools.ToolContext())
    assert "最多" in agent_tools.dispatch(
        "subagent", {"tasks": [f"t{i}" for i in range(5)]}, agent_tools.ToolContext())


def test_run_turn_max_turns_bounded():
    from agent import loop, session as agent_session, tools as agent_tools
    restore = _stub_llm([{"role": "assistant", "content": "", "tool_calls": [
        _tool_call("c1", "bash", {"command": "uname"})]}])
    try:
        s = agent_session.Session()
        text = loop.run_turn("循环", s, agent_tools.ToolContext(), max_turns=2)
        assert "最大轮次" in text
        assert len([m for m in s.messages if m.get("role") == "tool"]) == 2
    finally:
        restore()




def test_schedule_store_and_due():
    import time
    from agent import loop, schedule
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)
    entry = schedule.add("检查磁盘", every_seconds=60)
    assert entry["id"].startswith("sch-")
    assert 0 < entry["next_run"] - time.time() <= 60
    daily = schedule.add("每日巡检", daily="09:00")
    lt = time.localtime(daily["next_run"])
    assert (lt.tm_hour, lt.tm_min) == (9, 0) and daily["next_run"] > time.time()
    assert "检查磁盘" in schedule.format_list()
    # 手动把 next_run 拨到过去 → run_due 执行两条
    items = schedule.list_entries()
    for it in items:
        it["next_run"] = time.time() - 1
    schedule._save(items)
    calls = []
    orig = loop.run_turn

    def fake(task, session, ctx, **kwargs):
        calls.append((task, ctx, kwargs))
        return f"报告:{task}"

    loop.run_turn = fake
    try:
        results = schedule.run_due()
    finally:
        loop.run_turn = orig
    assert len(results) == 2
    assert {r["result_preview"] for r in results} == {"报告:检查磁盘", "报告:每日巡检"}
    assert all(r["status"] == "succeeded" for r in results)
    assert all(r["run_id"] and r["session_id"] and r["pid"] == os.getpid()
               for r in results)
    assert all(r["duration_s"] is not None and r["attempts"] == 1 for r in results)
    saved = {r["run_id"]: r for r in results}
    for rec in saved.values():
        assert (core.STATE_DIR / "runs" / f"{rec['run_id']}.json").is_file()
    # 任务条目带最后状态，schedule list 可查
    for it in schedule.list_entries():
        assert it.get("last_status") == "succeeded" and it.get("last_run_id")
    assert all(ctx.ask_user is None for _, ctx, _ in calls)
    assert all(kw["system"] == schedule.SCHEDULE_PROMPT for _, _, kw in calls)
    assert all(kw["max_turns"] == schedule.SCHEDULE_MAX_TURNS for _, _, kw in calls)
    # 执行前已推进 next_run → 立即再跑不会重复触发
    assert all(it["next_run"] > time.time() for it in schedule.list_entries())
    assert schedule.run_due() == []
    events = [json.loads(l)["event"] for l in
              (core.STATE_DIR / "audit.jsonl").read_text().strip().splitlines()[-4:]]
    assert events.count("schedule-run") == 2
    # remove 支持唯一前缀
    assert schedule.remove(entry["id"][:7])["id"] == entry["id"]
    assert schedule.remove(daily["id"])["id"] == daily["id"]
    assert schedule.list_entries() == []
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)


def test_schedule_validation_and_cli():
    import subprocess as sp
    import sys as _sys
    from agent import schedule
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)
    for bad in ({"every_seconds": 10}, {"daily": "25:00"}, {"daily": "9点"},
                {"every_seconds": 60, "daily": "09:00"}):
        try:
            schedule.add("x", **bad)
            raise AssertionError(f"应拒绝: {bad}")
        except schedule.ScheduleError:
            pass
    script = str(Path(__file__).resolve().parent / "opsagent")
    r = sp.run([_sys.executable, script, "schedule", "add", "--every", "3600", "CLI 巡检"],
               capture_output=True, text=True)
    assert r.returncode == 0 and "已添加" in r.stdout, r.stderr
    r = sp.run([_sys.executable, script, "schedule", "list"], capture_output=True, text=True)
    assert "CLI 巡检" in r.stdout, r.stderr
    entry = schedule.list_entries()[0]
    r = sp.run([_sys.executable, script, "schedule", "remove", entry["id"]],
               capture_output=True, text=True)
    assert r.returncode == 0 and "已删除" in r.stdout, r.stderr
    assert schedule.list_entries() == []
    r = sp.run([_sys.executable, script, "schedule", "remove", "nope"],
               capture_output=True, text=True)
    assert r.returncode == 1 and "没有匹配" in r.stderr, r.stderr
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)


def test_schedule_corrupt_entry_disabled():
    import time as _t
    from agent import schedule
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)
    good = schedule.add("正常巡检", every_seconds=60)
    items = schedule.list_entries()
    items[0]["next_run"] = 0
    items.append({"id": "sch-bad", "prompt": "坏任务", "enabled": True,
                  "every_seconds": None, "daily": None, "next_run": 0})
    schedule._save(items)
    orig = schedule._run_entry
    schedule._run_entry = lambda entry, model, **kw: f"报告:{entry['id']}"
    try:
        results = schedule.run_due()
    finally:
        schedule._run_entry = orig
    assert [r["task_id"] for r in results if r["status"] == "succeeded"] == [good["id"]]
    assert any(r["task_id"] == "sch-bad" and r["status"] == "failed" for r in results)
    assert schedule.runs_exit_code(results) == 1
    bad = [it for it in schedule.list_entries() if it["id"] == "sch-bad"][0]
    assert bad["enabled"] is False
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)


def test_schedule_corrupt_times_and_daily_dst():
    import time
    from agent import schedule
    p = core.STATE_DIR / "schedules.json"
    p.unlink(missing_ok=True)
    good = schedule.add("正常巡检", every_seconds=60)
    items = schedule.list_entries()
    items[0]["next_run"] = 0
    bad_values = [{"next_run": value} for value in (None, "soon", [], float("nan"),
                                                   float("inf"), 10 ** 300)]
    bad_values += [{"every_seconds": value} for value in (-60, True, 60.5, "bad")]
    bad_values += [{"every_seconds": None, "daily": "99:99"},
                   {"every_seconds": 60, "daily": "09:00"}]
    for i, values in enumerate(bad_values):
        items.append({"id": f"sch-bad-{i}", "prompt": "坏任务", "next_run": 0,
                      "every_seconds": 60, "daily": None, **values})
    schedule._save(items)
    original = schedule._run_entry
    schedule._run_entry = lambda entry, model, **kw: "报告"
    try:
        assert "时间配置损坏" in schedule.format_list()
        records = schedule.run_due()
        assert [r["task_id"] for r in records if r["status"] == "succeeded"] == [good["id"]]
        assert sum(r["status"] == "failed" for r in records) == len(bad_values)
        assert all(not it["enabled"] for it in schedule.list_entries()[1:])
        assert schedule.run_due() == []
    finally:
        schedule._run_entry = original
        p.unlink(missing_ok=True)
    malformed = json.dumps({"items": [{"id": 123, "prompt": "坏任务"}]})
    p.write_text(malformed, encoding="utf-8")
    try:
        try:
            schedule.add("不能覆盖坏文件", every_seconds=60)
            raise AssertionError("应报告格式错误，不能静默丢掉损坏条目")
        except schedule.ScheduleError:
            assert p.read_text(encoding="utf-8") == malformed
    finally:
        p.unlink(missing_ok=True)
    for value in (False, True, 60.5, float("inf"), 10 ** 300):
        try:
            schedule.add("x", every_seconds=value)
            raise AssertionError(f"应拒绝无效间隔: {value}")
        except schedule.ScheduleError:
            pass
    if hasattr(time, "tzset"):
        previous_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            for month, day in ((3, 7), (10, 31)):
                now = time.mktime((2026, month, day, 12, 0, 0, 0, 0, -1))
                next_run = schedule._next_daily("09:00", now)
                local = time.localtime(next_run)
                assert (local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min) == (
                    month if month == 3 else 11, day + 1 if month == 3 else 1, 9, 0)
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()


def test_schedule_memory_and_readonly_context():
    from agent import loop, memory, schedule, tools as agent_tools
    p = memory.path()
    p.unlink(missing_ok=True)
    memory.add("db-scheduled: OS = Linux")
    calls = []
    original = loop.run_turn

    def fake(prompt, session, ctx, **kwargs):
        calls.append((ctx, kwargs["system"]))
        return "未发现异常"

    loop.run_turn = fake
    try:
        assert schedule._run_entry({"prompt": "巡检"}, "scheduled-model") == "未发现异常"
        ctx, system = calls[0]
        assert "db-scheduled" in system and schedule.SCHEDULE_PROMPT in system
        assert ctx.ask_user is None and ctx.allowed == agent_tools.SCHEDULED_TOOLS
        assert ctx.model == "scheduled-model"
        assert ctx.allowed.isdisjoint({"remember", "goal", "schedule", "ops_exec", "ops_approve"})
    finally:
        loop.run_turn = original
        p.unlink(missing_ok=True)


def test_schedule_tool_and_lock():
    from agent import schedule, tools as agent_tools
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)
    ctx = agent_tools.ToolContext()
    out = agent_tools.dispatch("schedule", {"action": "add", "prompt": "每日磁盘巡检",
                                            "every_seconds": 3600}, ctx)
    assert "已添加" in out and "sch-" in out
    assert "每日磁盘巡检" in agent_tools.dispatch("schedule", {"action": "list"}, ctx)
    entry = schedule.list_entries()[0]
    assert "已删除" in agent_tools.dispatch("schedule", {"action": "remove", "id": entry["id"]}, ctx)
    assert agent_tools.dispatch("schedule", {"action": "list"}, ctx).startswith("(无定时任务")
    assert "二选一" in agent_tools.dispatch("schedule", {"action": "add", "prompt": "x"}, ctx)
    if schedule.fcntl is not None:  # 同一文件第二个锁拿不到（双守护/双 cron 不会并发）
        with schedule._locked("schedules.lock", block=False) as first:
            assert first is True
            with schedule._locked("schedules.lock", block=False) as second:
                assert second is False
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)


def test_ops_approve_requires_interactive_channel():
    from agent import tools as agent_tools
    cmd = "systemctl restart approve-gate"
    out = agent_tools.dispatch("ops_approve", {"command": cmd, "ttl_seconds": 60},
                               agent_tools.ToolContext())
    assert "没有可确认的用户通道" in out
    assert core.check_stamp(cmd) is False
    out = agent_tools.dispatch("ops_approve", {"command": cmd, "ttl_seconds": 60},
                               agent_tools.ToolContext(ask_user=lambda q: "确认"))
    assert "审批戳已创建" in out and core.check_stamp(cmd) is True
    core.revoke(cmd)


def test_ops_approve_requires_explicit_answer_for_command_and_target():
    from agent import tools as agent_tools
    cmd = "systemctl restart explicit-approval"
    for answer in (None, "", "no", "拒绝", "还需要检查", "false"):
        core.approve(cmd)
        out = agent_tools.dispatch("ops_approve", {"command": cmd},
            agent_tools.ToolContext(ask_user=lambda q, answer=answer: answer))
        assert "未明确批准" in out and not core.check_stamp(cmd), out
    questions = []
    def approve(question):
        questions.append(question)
        return "确认执行"
    out = agent_tools.dispatch("ops_approve", {"command": cmd},
                               agent_tools.ToolContext(ask_user=approve))
    assert "审批戳已创建" in out and core.check_stamp(cmd)
    assert cmd in questions[0] and "本机" in questions[0] and "R1" in questions[0]
    core.revoke(cmd)


def test_goal_lifecycle():
    from agent import goal, session as agent_session
    (core.STATE_DIR / "goals.json").unlink(missing_ok=True)
    sid = "goal-test-0001"
    agent_session.Session(sid).append({"role": "user", "content": "开始"})
    st = goal.create(sid, "把 nginx 升级到稳定版并验证", max_rounds=2)
    assert st["status"] == "active" and st["max_rounds"] == 2
    try:
        goal.create(sid, "另一个目标")
        raise AssertionError("已有活跃目标应拒绝")
    except goal.GoalError:
        pass
    msg = goal.continuation(sid)
    assert msg and "目标自动续跑 1/2" in msg and "nginx" in msg
    assert "2/2" in (goal.continuation(sid) or "")
    assert goal.continuation(sid) is None
    paused = goal.get(sid)
    assert paused["status"] == "paused" and "上限" in paused["reason"]
    goal.update(sid, "resume")
    assert goal.get(sid)["status"] == "active" and goal.get(sid)["rounds_used"] == 0
    try:
        goal.update(sid, "blocked")
        raise AssertionError("blocked 缺 reason 应拒绝")
    except goal.GoalError:
        pass
    goal.update(sid, "blocked", reason="等待维护窗口")
    assert goal.get(sid)["status"] == "blocked"
    goal.update(sid, "complete")
    assert goal.get(sid)["status"] == "completed"
    assert goal.continuation(sid) is None
    try:
        goal.update(sid, "resume")
        raise AssertionError("已完成目标不能 resume")
    except goal.GoalError:
        pass
    try:
        goal.update("no-such-session", "complete")
        raise AssertionError("没有目标应报错")
    except goal.GoalError:
        pass
    try:
        goal.create(sid + "-x", "   ")
        raise AssertionError("空 objective 应拒绝")
    except goal.GoalError:
        pass
    assert "目标" in goal.format_one(goal.get(sid))
    (core.STATE_DIR / "goals.json").unlink(missing_ok=True)


def test_goal_tool_and_repl_continuation():
    from agent import goal, loop, repl, tools as agent_tools
    (core.STATE_DIR / "goals.json").unlink(missing_ok=True)
    state = repl.ReplState(None)
    state.session.append({"role": "user", "content": "开始"})
    ctx = agent_tools.ToolContext(session_id=state.session.id, goal_continuation=True)
    out = agent_tools.dispatch("goal", {"action": "create",
                                        "objective": "巡检完所有节点", "max_rounds": 1}, ctx)
    assert "已创建" in out
    assert "巡检完所有节点" in agent_tools.dispatch("goal", {"action": "status"}, ctx)
    # 非 REPL 形态拒绝创建（不会续跑，免得留下死目标）
    plain = agent_tools.ToolContext(session_id=state.session.id)
    assert "不会自动续跑" in agent_tools.dispatch(
        "goal", {"action": "create", "objective": "x"}, plain)
    calls = []
    orig = loop.run_turn

    def fake(task, session, ctx, **kwargs):
        calls.append(task)
        return "继续完成"

    loop.run_turn = fake
    try:
        repl._continue_goal(state, ctx)
    finally:
        loop.run_turn = orig
    assert len(calls) == 1 and "目标自动续跑" in calls[0]
    assert goal.get(state.session.id)["status"] == "paused"
    assert "completed" in agent_tools.dispatch("goal", {"action": "complete"}, ctx)
    # 没有会话 id 时 goal 不可用
    assert "不可用" in agent_tools.dispatch("goal", {"action": "status"},
                                            agent_tools.ToolContext())
    (core.STATE_DIR / "goals.json").unlink(missing_ok=True)


def test_goal_concurrent_updates_and_corrupt_budget():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from agent import goal, session as agent_session
    p = core.STATE_DIR / "goals.json"
    p.unlink(missing_ok=True)
    sessions = [f"goal-concurrent-{i}" for i in range(12)]
    for sid in sessions:
        agent_session.Session(sid).append({"role": "user", "content": "开始"})
    barrier = threading.Barrier(len(sessions))

    def create(sid):
        barrier.wait()
        return goal.create(sid, "并发目标", max_rounds=20)

    try:
        with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
            assert len(list(pool.map(create, sessions))) == len(sessions)
        assert set(goal._load()) == set(sessions)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: goal.continuation(sessions[0]), range(30)))
        assert sum(msg is not None for msg in results) == 20
        assert goal.get(sessions[0])["status"] == "paused"
        original = goal._load()
        for bad in ({"rounds_used": -1}, {"rounds_used": "1"}, {"max_rounds": "20"},
                    {"max_rounds": False}, {"status": "unknown"}):
            data = json.loads(json.dumps(original))
            data[sessions[0]].update(bad)
            p.write_text(json.dumps(data), encoding="utf-8")
            try:
                goal.continuation(sessions[0])
                raise AssertionError(f"应拒绝损坏预算: {bad}")
            except goal.GoalError:
                pass
    finally:
        p.unlink(missing_ok=True)
        for sid in sessions:
            (core.STATE_DIR / "sessions" / f"{sid}.jsonl").unlink(missing_ok=True)


def test_workflow_run_and_validation():
    from agent import loop, tools as agent_tools, workflow
    ctx = agent_tools.ToolContext()
    assert "daily-inspection" in agent_tools.dispatch("workflow", {}, ctx)
    daily = workflow.load("daily-inspection")
    assert len(daily["steps"]) == 3
    assert daily["checks"] == {"targets": "all",
                               "groups": ["connectivity", "resources", "services"]}
    assert workflow.load("daily-inspection.json")["name"] == "daily-inspection"
    user_dir = Path(core.STATE_DIR, "workflows")
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "wf-test.json"
    p.write_text(json.dumps({"description": "测试编排", "steps": [
        {"name": "一", "prompt": "第一步"},
        {"name": "二", "prompt": "基于上一步：{prev}"}]}), encoding="utf-8")
    calls = []
    orig = loop.run_turn

    def fake(task, session, ctx, **kwargs):
        calls.append((task, ctx.allowed))
        return f"结果<{task}>"

    loop.run_turn = fake
    try:
        out = agent_tools.dispatch("workflow", {"name": "wf-test"}, ctx)
        for bad, needle in (
                (json.dumps({"steps": []}), "没有 steps"),
                (json.dumps({"steps": [{"name": "x"}]}), "缺少 prompt"),
                ("{not json", "损坏"),
                (json.dumps({"steps": [{"prompt": "x"}] * 9}), "步骤过多")):
            p.write_text(bad, encoding="utf-8")
            result = agent_tools.dispatch("workflow", {"name": "wf-test"}, ctx)
            assert needle in result, (bad, result)
    finally:
        loop.run_turn = orig
        p.unlink(missing_ok=True)
    assert "结果<第一步>" in out and "步骤 2: 二" in out
    assert len(calls) == 2
    assert all(allowed == agent_tools.SUBAGENT_TOOLS for _, allowed in calls)
    assert "结果<第一步>" in calls[1][0] and "{prev}" not in calls[1][0]
    assert "错误" in agent_tools.dispatch("workflow", {"name": "no-such"}, ctx)
    assert "非法" in agent_tools.dispatch("workflow", {"name": "../x"}, ctx)




def test_ssh_readonly_extended_command_set():
    """ssh 直连只读白名单：远程命令集与本机对齐（uptime/free/df 等），且带词边界防前缀注入。"""
    for cmd in (
        'ssh neo@10.0.0.1 "uptime"',
        'ssh neo@10.0.0.1 "free -h"',
        'ssh neo@10.0.0.1 "df -h"',
        'ssh neo@10.0.0.1 "cat /var/log/nginx/error.log"',
        'ssh neo@10.0.0.1 "uptime; uname -a; free -h; df -h"',
        'ssh user@10.0.0.1 "hostname; date; ps aux"',
        'ssh neo@10.0.0.1 "echo ok"',
        'ssh neo@10.0.0.1 "systemctl status nginx"',
        'ssh neo@10.0.0.1 "systemctl list-units --state=failed --no-pager"',
        'ssh neo@10.0.0.1 "systemctl --failed"',
        'ssh neo@10.0.0.1 "systemctl is-system-running"',
        'ssh neo@10.0.0.1 "w"',
        'ssh neo@10.0.0.1 "who"',
        "ssh neo@10.0.0.1 \"find /var/log -name '*.log'\"",
    ):
        assert core.classify(cmd)[0] == "R0", cmd
    for cmd in (
        'ssh neo@10.0.0.1 "systemctl restart nginx"',
        'ssh -o ProxyCommand=cat host "uptime"',   # 选项注入仍拒绝
        'ssh neo@10.0.0.1 "uptime > /tmp/x"',
        'ssh neo@10.0.0.1 "echo $(cat /etc/passwd)"',
        'ssh neo@10.0.0.1 "psql -c \\"DROP TABLE t\\""',  # ps 前缀注入拒绝（psql 不是 ps）
        'ssh neo@10.0.0.1 "find / -delete"',
    ):
        assert core.classify(cmd)[0] != "R0", cmd


def test_echo_is_readonly():
    for cmd in ('echo hello', 'echo "---"', 'ls /tmp; echo "done"',
                'echo 中文标记', 'w', 'who'):
        assert core.classify(cmd)[0] == "R0", cmd
    for cmd in ('echo x > /tmp/x', 'echo $(cat /etc/passwd)', 'echo x | tee /tmp/x'):
        assert core.classify(cmd)[0] != "R0", cmd




def test_stream_printer_overflow_appends_without_repaint():
    """内容超过屏幕可重绘行数后不再整段重绘（否则同一内容被反复打印）。"""
    import io
    import os
    import shutil
    from agent import repl
    orig_size = shutil.get_terminal_size
    shutil.get_terminal_size = lambda: os.terminal_size((80, 12))  # 12 行屏，可重绘 10 行
    buf = io.StringIO()
    import sys as _sys
    orig_out = _sys.stdout
    _sys.stdout = buf
    try:
        printer = repl.StreamPrinter()
        for i in range(1, 31):
            printer.feed(f"第{i}行内容\n")
        text = printer.finish()
    finally:
        _sys.stdout = orig_out
        shutil.get_terminal_size = orig_size
    out = buf.getvalue()
    # 溢出前最多重绘 ~10 次；若仍在整段重绘（bug）第 1 行会出现 30 次
    assert out.count("第1行内容") <= 11, out.count("第1行内容")
    assert out.count("第30行内容") == 1
    assert "第1行内容" in text and "第30行内容" in text


def test_memory_store_and_recall():
    from agent import memory
    p = memory.path()
    p.unlink(missing_ok=True)
    assert memory.recall() == ""
    entry = memory.add("user01-X99：Ubuntu，systemd 255，9 个 docker 容器健康")
    assert entry.startswith("- [") and "user01-X99" in entry
    assert oct(p.stat().st_mode & 0o777) == "0o600"  # 主机信息 0600
    memory.add("多行条目\n续行缩进")
    recalled = memory.recall()
    assert "user01-X99" in recalled and "\n  续行缩进" in recalled
    assert "写法约定" not in recalled  # 多行头注释不进入注入内容
    for bad, needle in (("x" * 501, "太长"), ("   ", "不能为空"),
                        ("api_key=sk-live-abcdefghijklmnop", "密钥")):
        try:
            memory.add(bad)
            raise AssertionError(f"应拒绝：{needle}")
        except memory.MemoryError as e:
            assert needle in str(e)
    p.write_text("旧" * 9000 + "\n新事实\n", encoding="utf-8")
    tail = memory.recall()
    assert "新事实" in tail and "早期记忆已省略" in tail and len(tail) < 9000
    p.unlink(missing_ok=True)


def test_memory_concurrent_first_write_and_permissions():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from agent import memory
    p = memory.path()
    p.unlink(missing_ok=True)
    barrier = threading.Barrier(12)

    def add(i):
        barrier.wait()
        return memory.add(f"host-{i}: OS = Linux")

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            entries = list(pool.map(add, range(12)))
        text = p.read_text(encoding="utf-8")
        assert text.count(memory.HEADER) == 1
        assert all(text.count(entry + "\n") == 1 for entry in entries)
        assert p.stat().st_mode & 0o777 == 0o600
        p.write_text("", encoding="utf-8")
        p.chmod(0o644)
        memory.add("复用空文件")
        assert p.read_text(encoding="utf-8").startswith(memory.HEADER)
        assert p.stat().st_mode & 0o777 == 0o600
    finally:
        p.unlink(missing_ok=True)


def test_memory_tool_and_system_injection():
    from agent import loop, tools as agent_tools
    p = core.STATE_DIR / "memory.md"
    p.unlink(missing_ok=True)
    # 无记忆时 system prompt 原样
    assert loop._default_system() == loop.SYSTEM_PROMPT
    ctx = agent_tools.ToolContext()
    out = agent_tools.dispatch("remember", {"text": "db-01 的 5432 是只读备库"}, ctx)
    assert "已记录" in out
    system = loop._default_system()
    assert "db-01" in system and "长期记忆" in system
    # 只读上下文（子代理/定时任务）不可用 remember
    ro = agent_tools.ToolContext(allowed=agent_tools.SUBAGENT_TOOLS)
    assert "只读上下文" in agent_tools.dispatch("remember", {"text": "x"}, ro)
    assert agent_tools.SCHEDULED_TOOLS.isdisjoint({"remember"})
    # 审计留痕
    line = Path(core.STATE_DIR, "audit.jsonl").read_text().strip().splitlines()[-1]
    assert json.loads(line)["event"] == "memory-add"
    # 空内容经工具返回可读错误
    assert "不能为空" in agent_tools.dispatch("remember", {"text": " "}, ctx)
    p.unlink(missing_ok=True)


def test_guard_shell_separators_and_write_options():
    for command in (
        "echo ok\nrm -rf /tmp/x", "echo ok & rm -rf /tmp/x",
        "env sh -c 'touch /tmp/x'", "env X=1 sh -c 'touch /tmp/x'",
        "brew services restart nginx", "brew services stop nginx",
        "git remote -v add surprise https://example.invalid/repo",
        "git reflog expire --expire=all", "date -s 2026-10-21",
        "hostname changed", "journalctl --rotate", "journalctl --vacuum-time=1s",
        "git diff --output=/tmp/x", "git log --output /tmp/x",
        "docker compose config -o /tmp/x", "docker compose config -o/tmp/x",
        "find /tmp -fprint /tmp/x", "find /tmp -execdir touch x +",
        "cat /dev/null 2>/tmp/x", "cat /dev/null 2>/dev/null.other",
        "cat <(touch /tmp/x)", "echo'custom-executable'", "echo\u00a0custom-executable",
        "\u00a0echo custom-executable", "echo\vcustom-executable", "echo\rcustom-executable",
        'ssh -oProxyCommand=touch /tmp/x "uptime"',
        'ssh -oProxyCommand=evil "uptime"', 'ssh\nrm "uptime"',
        'ssh host "hostname changed"', 'ssh host "brew services stop nginx"',
        'ssh host "env"', 'ssh host "uptime" extra',
    ):
        assert core.classify(command)[0] != "R0", command
    for command in ("cat /dev/null 2>/dev/null", "cat /dev/null 2>&1", "env",
                    "brew services list", "git remote -v", "hostname -f",
                    'date +"%F %T"', 'ssh host "date; hostname; systemctl --failed"'):
        assert core.classify(command)[0] == "R0", command
    for command in ("echo ok\nrm -rf /tmp/x", "rm /tmp/x -rf",
                    "systemctl --user stop nginx", "kubectl -n prod delete pod x",
                    "docker --context prod rm x"):
        assert core.classify(command)[0] == "R3", command


def test_guard_stamp_semantics_and_corrupt_shapes():
    command = "printf 'one  two'"
    core.approve(command, ttl_seconds=60)
    assert core.check_stamp(command)
    assert not core.check_stamp("printf 'one two'")
    assert core.cmd_hash("printf a\\ ") != core.cmd_hash("printf a\\")
    assert core.cmd_hash("printf a\vb") != core.cmd_hash("printf a b")
    assert core.cmd_hash("printf  a") == core.cmd_hash(" printf a ")
    destructive = "rm -rf /tmp/guard-force-test"
    p = core.approve(destructive, ttl_seconds=60, force=True)
    stamp = json.loads(p.read_text())
    for fields in ({"force": False}, {"target": "another"}, {"cmd_hash": "wrong"},
                   {"ttl": float("inf")}, {"created": 10 ** 500}):
        p.write_text(json.dumps(dict(stamp, **fields)))
        assert not core.check_stamp(destructive), fields
    for malformed in ([], None, "stamp", {"created": 0, "ttl": "forever"}):
        p.write_text(json.dumps(malformed))
        assert not core.check_stamp(destructive)
        core.collect_state()
        core.prune_approvals()
        assert not p.exists()
    for ttl, force in ((0, False), (-1, False), (True, False), (60, "false")):
        try:
            core.approve("printf invalid-stamp", ttl_seconds=ttl, force=force)
            raise AssertionError("invalid approval must fail")
        except core.OpsxError:
            pass


def test_guard_snapshot_commands_and_duplicate_outputs():
    import remote
    import subprocess
    _write_targets()
    with tempfile.TemporaryDirectory() as td:
        unwanted = Path(td, "unwanted")
        command = f"touch {unwanted}"
        for create in (lambda: core.snapshot_cmd(command),
                       lambda: remote.snapshot_cmd("web1", command)):
            try:
                create()
                raise AssertionError("unapproved capture must fail")
            except (core.OpsxError, remote.RemoteError):
                pass
        assert not unwanted.exists()
        code, sid, _ = core.exec_change("echo main", snapshot_cmds=[("echo first", ""),
                                                                      ("echo second", "")])
        assert code == 0 and sid
        d, meta = core.load_snapshot(sid)
        assert [Path(d, item["output"]).read_text().strip() for item in meta["items"]] == ["first", "second"]
        original = remote._ssh
        remote._ssh = lambda name, cmd, **kw: subprocess.CompletedProcess([], 0, cmd.encode(), b"")
        try:
            code, sid, _ = remote.exec_change("web1", "echo main", snapshot_cmds=[("echo first", "same"),
                                                                                  ("echo second", "same")])
            assert code == 0 and sid
            d, meta = core.load_snapshot(sid)
            assert [Path(d, item["output"]).read_text() for item in meta["items"]] == ["echo first", "echo second"]
            for path in ("relative.conf", "~/app.conf", "/tmp/../app.conf"):
                try:
                    remote.snapshot_file("web1", path)
                    raise AssertionError("ambiguous remote path must fail")
                except remote.RemoteError:
                    pass
        finally:
            remote._ssh = original


def test_guard_snapshot_metadata_and_failure_cleanup():
    import remote
    with tempfile.TemporaryDirectory() as td:
        source = Path(td, "source")
        source.write_text("before")
        source.chmod(0o640)
        sid = core.snapshot_file(str(source))
        d, meta = core.load_snapshot(sid)
        source.write_text("after")
        original = json.dumps(meta)
        for value in ([], {}, {"id": sid, "items": [None]},
                      {"id": sid, "items": [{"type": "file", "src": str(source),
                                              "mode": 0o640, "dest": "../../outside"}]}):
            Path(d, "meta.json").write_text(json.dumps(value))
            for restore in (core.rollback, remote.rollback):
                try:
                    restore(sid)
                    raise AssertionError("corrupt snapshot must fail")
                except core.OpsxError:
                    pass
            assert source.read_text() == "after"
        Path(d, "meta.json").write_text(original)
        other = Path(td, "other")
        other.write_text("untouched")
        source.unlink()
        source.symlink_to(other)
        core.rollback(sid, confirm=True)
        assert not source.is_symlink() and source.read_text() == "before"
        assert source.stat().st_mode & 0o777 == 0o640 and other.read_text() == "untouched"
        sdir = core.STATE_DIR / "snapshots"
        before = set(sdir.iterdir())
        try:
            core.snapshot_file(str(Path(td, "missing")))
            raise AssertionError("missing snapshot file must fail")
        except core.OpsxError:
            pass
        assert set(sdir.iterdir()) == before
        write_meta = core._write_meta
        def fail_meta(*args):
            raise OSError("test disk failure")
        core._write_meta = fail_meta
        try:
            marker = Path(td, "must-not-run")
            command = f"touch {marker}"
            core.approve(command, ttl_seconds=60)
            assert core.exec_change(command, snapshot_files=[str(source)])[0] == 3
            assert not marker.exists() and set(sdir.iterdir()) == before
        finally:
            core._write_meta = write_meta


def test_guard_process_group_timeout_stops_children():
    import time
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td, "late-write")
        try:
            core.run_shell(f"(sleep 0.3; touch {marker}) & wait", timeout=0.05)
            raise AssertionError("command must time out")
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.4)
        assert not marker.exists()


def test_guard_fresh_state_private_and_prune_zero():
    original = core.STATE_DIR
    with tempfile.TemporaryDirectory() as td:
        core.STATE_DIR = Path(td, "fresh")
        try:
            _, d = core.begin_snapshot()
            assert core.STATE_DIR.stat().st_mode & 0o077 == 0
            assert d.stat().st_mode & 0o077 == 0
            core.prune_snapshots(keep=0)
            assert not list((core.STATE_DIR / "snapshots").iterdir())
        finally:
            core.STATE_DIR = original


def test_guard_audit_parallel_rotation():
    from concurrent.futures import ThreadPoolExecutor
    original, limit = core.STATE_DIR, core.AUDIT_ROTATE_BYTES
    with tempfile.TemporaryDirectory() as td:
        core.STATE_DIR = Path(td, "state")
        try:
            for rotate_bytes in (10, 10 ** 8):
                core.AUDIT_ROTATE_BYTES = rotate_bytes
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda i: core.audit("parallel", index=i), range(100)))
                records = [json.loads(line) for line in
                           (core.STATE_DIR / "audit.jsonl").read_text().splitlines()]
                assert all(record["event"] == "parallel" for record in records)
                assert len(records) == (1 if rotate_bytes == 10 else 101)
            assert (core.STATE_DIR / "audit.jsonl.1").exists()
        finally:
            core.STATE_DIR, core.AUDIT_ROTATE_BYTES = original, limit


def test_guard_schema_and_invalid_config_fail_closed():
    schema = {"type": "object", "required": ["force"], "properties": {
        "force": {"type": "boolean"}, "ttl": {"type": "integer"},
        "captures": {"type": "array", "items": {"type": "object", "required": ["command"],
                    "properties": {"command": {"type": "string"}}}}}}
    core.validate_arguments({"force": False, "ttl": 60, "captures": [{"command": "uptime"}]}, schema)
    for value in ({"force": "false"}, {"force": False, "ttl": True},
                  {"force": True, "captures": ["uptime"]}, {"force": True, "captures": [{}]}, []):
        try:
            core.validate_arguments(value, schema)
            raise AssertionError("schema mismatch must fail")
        except core.OpsxError:
            pass
    override = core.STATE_DIR / "config.override.yaml"
    for text in ("readonly_patterns:\n  - [\n", "readonly_patterns: []\n", "unknown:\n  - foo\n"):
        override.write_text(text)
        try:
            try:
                core.classify("echo allowed")
                raise AssertionError("invalid policy must fail closed")
            except core.OpsxError:
                pass
        finally:
            override.unlink()


def test_secret_redaction_forms():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE-secret-material\n-----END RSA PRIVATE KEY-----"
    text = (f"password=hunter2\nAPI_KEY=sk-{'a' * 20}\n{pem}\n"
            "Authorization: Bearer abc.def.ghi\naws=AKIAABCDEFGHIJKLMNOP\nnormal=1")
    out = core.redact(text)
    for secret in ("hunter2", "sk-aaaa", "MIIE-secret-material", "abc.def.ghi",
                   "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in out, secret
    assert "normal=1" in out and out.count(core.REDACTED) >= 5
    assert core.redact(out) == out  # 幂等：重复脱敏不破坏占位符
    assert core.redact("systemctl restart nginx") == "systemctl restart nginx"
    assert core.redact_obj({"password": "hunter2"})["password"] == core.REDACTED
    assert core.redact_obj({"note": "hello"})["note"] == "hello"
    json_text = '{"password": "hunter2", "client_secret":"GOCSPX-abc"}'
    assert "hunter2" not in core.redact(json_text) and "GOCSPX" not in core.redact(json_text)
    code, msg = core.check('curl -H "Authorization: Bearer HUNTER2-TOKEN" https://example')  # 拒绝文案也脱敏
    assert code == 2 and "HUNTER2-TOKEN" not in msg


def test_secret_not_reaching_model_session_or_audit():
    import tempfile as tf
    from agent import llm, loop
    from agent import session as agent_session
    from agent import tools as agent_tools
    sentinel = "sk-SENTINEL0123456789abcdef"
    with tf.TemporaryDirectory() as td:
        f = Path(td, "creds.txt")
        f.write_text(f"AWS_SECRET_ACCESS_KEY={sentinel}\n")
        ctx = agent_tools.ToolContext()
        out = agent_tools.dispatch("bash", {"command": f"cat {f}"}, ctx)
        assert sentinel not in out and core.REDACTED in out
        out = agent_tools.dispatch("read", {"path": str(f)}, ctx)  # read 出口同样脱敏
        assert sentinel not in out
        assert sentinel in f.read_text()  # 源文件保持原样：快照/恢复需要原始内容
        sid = core.snapshot_file(str(f))  # 快照保留原始内容供恢复，不脱敏
        d, meta = core.load_snapshot(sid)
        assert sentinel in (d / meta["items"][0]["dest"]).read_text()
        big = Path(td, "big.log")
        big.write_text("x" * (agent_tools.OUTPUT_LIMIT + 100) + f"\ntoken={sentinel}\n")
        out = agent_tools.dispatch("bash", {"command": f"cat {big}"}, ctx)
        assert sentinel not in out
        spill = re.search(r"完整内容: ([^（\s]+)", out)
        assert spill, out[-300:]
        assert sentinel not in Path(spill.group(1)).read_text()  # 落盘全文也脱敏
        s = agent_session.Session()
        s.append({"role": "tool", "content": f"key={sentinel}"})
        assert sentinel not in s.path.read_text()
        assert sentinel not in s.messages[0]["content"]
        core.audit("redact-probe", output=f"password={sentinel}")
        last = json.loads(Path(core.STATE_DIR, "audit.jsonl").read_text().strip().splitlines()[-1])
        assert sentinel not in last["output"]
        received = []
        script = iter([
            {"role": "assistant", "content": "", "tool_calls": [
                _tool_call("s1", "bash", {"command": f"cat {f}"})]},
            {"role": "assistant", "content": "已脱敏"}])
        orig = llm.chat
        llm.chat = lambda messages, **kw: (received.extend(messages), next(script))[1]
        try:
            loop.run_turn("看凭据", agent_session.Session(), agent_tools.ToolContext())
        finally:
            llm.chat = orig
        assert sentinel not in json.dumps(received, ensure_ascii=False)  # 不进入模型请求


def test_sensitive_file_read_restrictions():
    import tempfile as tf
    from agent import tools as agent_tools
    ctx = agent_tools.ToolContext()
    with tf.TemporaryDirectory() as td:
        sshdir = Path(td, ".ssh")
        sshdir.mkdir()
        key = sshdir / "id_ed25519"
        key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nKEYMATERIAL\n-----END OPENSSH PRIVATE KEY-----\n")
        envf = Path(td) / ".env"
        envf.write_text("TOKEN=abc\n")
        log = Path(td) / "app.log"
        log.write_text("mentions id_ed25519 in a log line\n")
        for path in (key, envf):
            out = agent_tools.dispatch("read", {"path": str(path)}, ctx)
            assert "[opsx 拒绝]" in out and "KEYMATERIAL" not in out and "TOKEN=abc" not in out
        out = agent_tools.dispatch("bash", {"command": f"cat {key}"}, ctx)
        assert "[opsx 拒绝]" in out and "KEYMATERIAL" not in out
        link = Path(td) / "innocent-name"
        link.symlink_to(key)
        out = agent_tools.dispatch("bash", {"command": f"cat {link}"}, ctx)  # 符号链接同样拒绝
        assert "[opsx 拒绝]" in out and "KEYMATERIAL" not in out
        _write_targets()
        code, msg = core.check("cat id_rsa", target="web1")  # 远程无法 stat：裸文件名也拒绝
        assert code == 2 and "敏感" in msg
        for path in ("/etc/kubernetes/admin.conf", "/etc/ssh/ssh_host_ed25519_key",
                     "/etc/ssl/private/server.pem",
                     "~/.config/gcloud/application_default_credentials.json"):
            code, msg = core.check(f"cat {path}")
            assert code == 2 and "敏感" in msg, path
        code, msg = core.check('ssh web1 "cat id_rsa"')  # ssh 引号内层命令同样检查
        assert code == 2 and "敏感" in msg
        assert core.sensitive_path("/app/.env.example") is False  # 模板不是密钥
        assert core.check("cat /app/.env.example")[0] == 0
        # KUBECONFIG 前缀/--kubeconfig 是选集群，不是读文件内容
        assert core.classify("KUBECONFIG=~/.kube/config kubectl get pods")[0] == "R0"
        assert core.check("KUBECONFIG=~/.kube/config kubectl get pods")[0] == 0
        assert core.sensitive_command("kubectl --kubeconfig ~/.kube/config get pods") is None
        assert core.classify("KUBECONFIG=$(id) kubectl get pods")[0] != "R0"  # 赋值里的命令替换仍拦
        code, _ = core.check(f"sudo cat {sshdir}/*")  # 前缀命令不能绕过内容读取判断
        assert code == 2
        code, _ = core.check("cat ~/.ops-agent/snapshots/x/files/y")  # 快照原件同样不读
        assert code == 2
        for dump in ("kubectl get secret db-creds -o yaml",  # Secret 值不算只读排查
                     "kubectl get secrets -n prod -o jsonpath={.items[*].data}",
                     "kubectl get secret db-creds --template={{.data.password}}"):
            code, msg = core.check(dump)
            assert code == 2 and "敏感" in msg, dump
        assert core.check("kubectl get secret db-creds -o name")[0] == 0  # 只看名字可以
        assert core.check("kubectl get secrets -n prod")[0] == 0  # 默认表格不含值
        assert core.check("kubectl describe secret db-creds")[0] == 0
        out = agent_tools.dispatch("bash", {"command": f"ls -l {sshdir}"}, ctx)  # 只看权限可以
        assert "[exit=0]" in out and "id_ed25519" in out
        out = agent_tools.dispatch("bash", {"command": f"grep id_ed25519 {log}"}, ctx)  # 搜索词不误伤
        assert "[exit=0]" in out and "mentions" in out
        out = agent_tools.dispatch("bash", {"command": f"find {td} -name id_ed25519"}, ctx)
        assert "[exit=0]" in out and "id_ed25519" in out
        sid = core.snapshot_file(str(log))  # 快照原件不读进模型上下文
        snap = Path(core.STATE_DIR, "snapshots", sid, "files", str(log).lstrip("/"))
        out = agent_tools.dispatch("read", {"path": str(snap)}, ctx)
        assert "[opsx 拒绝]" in out and "mentions" not in out


def test_rollback_preview_confirm_and_backup():
    import tempfile as tf
    with tf.TemporaryDirectory() as td:
        f = Path(td) / "svc.conf"
        f.write_text("v1")
        sid = core.snapshot_file(str(f))
        f.write_text("v2")  # 执行后又改过：回滚不能静默覆盖
        plan = core.rollback_plan(sid)
        assert plan["files"][0]["exists"] is True
        assert "v2" in plan["files"][0]["diff"] and "v1" in plan["files"][0]["diff"]
        assert "回滚计划" in core.format_rollback_plan(plan)
        try:
            core.rollback(sid)  # 未确认：不改文件
            raise AssertionError("未确认的回滚必须拒绝")
        except core.OpsxError:
            pass
        assert f.read_text() == "v2"
        report = core.rollback(sid, confirm=True)
        assert f.read_text() == "v1" and "回滚前副本" in report
        backup = re.search(r"回滚前副本: ([^（\s]+)", report).group(1)
        meta = json.loads(Path(core.STATE_DIR, "snapshots", backup, "meta.json").read_text())
        assert meta["rollback_of"] == sid
        assert Path(core.STATE_DIR, "snapshots", backup, meta["items"][0]["dest"]).read_text() == "v2"
        assert core.rollback(backup, confirm=True) and f.read_text() == "v2"  # 副本可撤销回滚
        # 文件已删除：预览标出将新建，回滚前副本不记录不存在的文件
        f.unlink()
        plan = core.rollback_plan(sid)
        assert plan["files"][0]["exists"] is False
        report = core.rollback(sid, confirm=True)
        assert f.read_text() == "v1" and "回滚前副本" not in report  # 没有可备份的旧内容


def test_rollback_tool_requires_user_confirmation():
    import tempfile as tf
    from agent import tools as agent_tools
    with tf.TemporaryDirectory() as td:
        f = Path(td) / "app.conf"
        f.write_text("v1")
        sid = core.snapshot_file(str(f))
        f.write_text("v2")
        asked = []
        out = agent_tools.dispatch("ops_rollback", {"snapshot_id": sid},
            agent_tools.ToolContext(ask_user=lambda q: asked.append(q) or "no"))
        assert "[opsx 拒绝]" in out and f.read_text() == "v2"
        assert "回滚计划" in asked[0] and str(f) in asked[0] and "v2" in asked[0]
        out = agent_tools.dispatch("ops_rollback", {"snapshot_id": sid},
                                   agent_tools.ToolContext())  # 无确认通道
        assert "[opsx 拒绝]" in out and f.read_text() == "v2"
        out = agent_tools.dispatch("ops_rollback", {"snapshot_id": sid},
            agent_tools.ToolContext(ask_user=lambda q: "确认回滚"))
        assert "已恢复文件" in out and f.read_text() == "v1"


def test_approval_bound_to_cwd_host_and_kubeconfig():
    import tempfile as tf
    import remote
    cmd = "systemctl restart env-bound-svc"
    repo = os.getcwd()
    with tf.TemporaryDirectory() as td:
        a, b = Path(td, "a"), Path(td, "b")
        a.mkdir()
        b.mkdir()
        try:
            os.chdir(a)
            core.approve(cmd, ttl_seconds=60)
            assert core.check_stamp(cmd) is True
            os.chdir(b)
            assert core.check_stamp(cmd) is False  # cwd 变了：旧授权失效
            code, msg = core.check(cmd)
            assert code == 2 and "环境已变化" in msg  # 明确提示重新批准，而不是当成没戳
            assert "变更单" in core.check("systemctl restart no-such-stamp")[1]
        finally:
            os.chdir(repo)
        # 实际主机变了：目标注册表里的 host 被改写
        _write_targets()
        core.approve(cmd, ttl_seconds=60, target="web1")
        assert core.check_stamp(cmd, "web1") is True
        p = _write_targets()
        data = json.loads(p.read_text())
        data["web1"]["host"] = "10.0.0.99"
        p.write_text(json.dumps(data))
        try:
            assert core.check_stamp(cmd, "web1") is False
            code, _, _ = remote.exec_change("web1", cmd)
            assert code == 2  # 换主机不能用旧戳
        finally:
            _write_targets()
        # identity/options 也会改变实际落到哪台机器
        core.approve(cmd, ttl_seconds=60, target="web1")
        p = _write_targets()
        data = json.loads(p.read_text())
        data["web1"]["options"] = ["-o", "HostName=10.0.0.99"]
        p.write_text(json.dumps(data))
        try:
            assert core.check_stamp(cmd, "web1") is False
        finally:
            _write_targets()
        # KUBECONFIG 变了：不同集群不能复用授权
        local = "printf kube-bound"
        old = os.environ.get("KUBECONFIG")
        try:
            os.environ["KUBECONFIG"] = str(Path(td, "one.yaml"))
            core.approve(local, ttl_seconds=60)
            assert core.check_stamp(local) is True
            os.environ["KUBECONFIG"] = str(Path(td, "two.yaml"))
            assert core.check_stamp(local) is False
        finally:
            if old is None:
                os.environ.pop("KUBECONFIG", None)
            else:
                os.environ["KUBECONFIG"] = old


def test_kubectl_change_requires_explicit_context():
    cmd = "kubectl apply -f deploy.yaml"
    try:
        core.approve(cmd, ttl_seconds=60)
        raise AssertionError("kubectl 变更没有 --context 必须拒绝发戳")
    except core.OpsxError as e:
        assert "--context" in str(e)
    scoped = "kubectl apply --context=prod -n prod -f deploy.yaml"
    p = core.approve(scoped, ttl_seconds=60)
    stamp = json.loads(p.read_text())
    assert stamp["env"]["k8s"] == {"context": "prod", "namespace": "prod"}
    assert core.check_stamp(scoped) is True
    other = "kubectl apply --context=stage -n prod -f deploy.yaml"
    assert core.check_stamp(other) is False  # 换 context 不能用旧戳
    assert core.check("kubectl get pods --context=prod")[0] == 0  # 只读不需要审批
    for bypass in ("kubectl apply -f deploy.yaml && echo --context=prod",  # 别段参数不能冒充
                   "sudo kubectl apply -f deploy.yaml",
                   "/usr/local/bin/kubectl apply -f deploy.yaml",  # 全路径同样要认出来
                   'sh -c "kubectl apply -f deploy.yaml"',
                   "kubectl apply -f deploy.yaml # --context=prod"):  # 注释不执行
        try:
            core.approve(bypass, ttl_seconds=60)
            raise AssertionError(f"kubectl 变更缺少 --context 必须拒绝: {bypass}")
        except core.OpsxError:
            pass
    core.approve("systemctl restart nginx-check && kubectl get pods -n prod",
                 ttl_seconds=60)  # 只读 kubectl 段不强制 context
    try:
        core.approve("kubectl apply --context=dev -f a.yaml && kubectl delete --context=prod pod x",
                     ttl_seconds=60, force=True)
        raise AssertionError("同一命令混用不同 context 必须拒绝")
    except core.OpsxError as e:
        assert "不同 --context" in str(e)


def test_old_stamp_without_env_is_invalid():
    cmd = "systemctl restart legacy-stamp"
    p = core.approve(cmd, ttl_seconds=60)
    stamp = json.loads(p.read_text())
    p.write_text(json.dumps({k: v for k, v in stamp.items() if k != "env"}))
    assert core.check_stamp(cmd) is False  # 旧格式戳安全失效，需重新授权
    assert not any(a["cmd"] == cmd for a in core.collect_state()["approvals"])  # 也不显示成有效
    p.write_text(json.dumps(dict(stamp, env=dict(stamp["env"], cwd="/elsewhere"))))
    assert core.check_stamp(cmd) is False


def test_cli_and_mcp_outputs_redacted():
    import subprocess
    import sys
    from agent import tools as agent_tools
    sentinel = "sk-CLI-SENTINEL0123456789"
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "out.txt"
        f.write_text(f"TOKEN={sentinel}\n")
        # CLI 形态的 stdout 也会被模型宿主读取（Claude Code Bash）
        env = dict(os.environ, OPSX_STATE_DIR=str(Path(td) / "state"))
        r = subprocess.run([sys.executable, str(core.REPO_DIR / "opsx"), "exec", f"cat {f}"],
                           env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert sentinel not in r.stdout and core.REDACTED in r.stdout
        # MCP 工具结果同样过脱敏出口
        c = McpClient()
        try:
            _, payload = c.call_tool("ops_exec", {"command": f"cat {f}"})
            assert payload["exit_code"] == 0
            assert sentinel not in payload["output"] and core.REDACTED in payload["output"]
        finally:
            c.close()
        # 工具输出的脱敏在 dispatch 之后仍保持幂等
        out = agent_tools.dispatch("bash", {"command": f"cat {f}"}, agent_tools.ToolContext())
        assert core.redact(out) == out and sentinel not in out
    # opsx list 会把审批命令打印给宿主：展示同样脱敏（戳文件里保留原文供精确匹配）
    core.approve("setup-env TOKEN=sk-DISPLAY-SENTINEL0123456789", ttl_seconds=60)
    assert "DISPLAY-SENTINEL" not in "\n".join(core.format_state(core.collect_state()))


def test_remote_rollback_target_check_and_backup():
    import subprocess
    import remote
    _write_targets()
    core.approve("systemctl restart nginx", ttl_seconds=60, target="web1")
    pushed = []

    def fake(name, cmd, stdin=None, timeout=120):
        resolved = _fake_resolved_response(cmd)
        if resolved is not None:
            return resolved
        if "OPSX-EXISTS" in cmd:
            return subprocess.CompletedProcess([], 0, stdout=b"OPSX-EXISTS\ncurrent-v2\n", stderr=b"")
        if cmd.startswith("cat --"):
            return subprocess.CompletedProcess([], 0, stdout=b"archived-v1\n", stderr=b"")
        if ".opsx-restore." in cmd:      # 原子恢复脚本（mktemp → chmod → mv）
            pushed.append(stdin)
            size = len(stdin or b"")
            return subprocess.CompletedProcess(
                [], 0, stdout=f"opsx-restored had=1 size={size} mode=600".encode(), stderr=b"")
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    orig = remote._ssh
    remote._ssh = fake
    try:
        code, sid, _ = remote.exec_change("web1", "systemctl restart nginx",
                                          snapshot_files=["/etc/nginx/nginx.conf"])
        assert code == 0 and sid
        report = remote.rollback(sid, confirm=True)
        backup = re.search(r"回滚前副本: ([^（\s]+)", report).group(1)
        meta = json.loads(Path(core.STATE_DIR, "snapshots", backup, "meta.json").read_text())
        assert meta["target"] == "web1" and meta["env"]["host"] == "10.0.0.11"
        core.load_snapshot(backup)  # 远程副本必须可加载（缺 target/mode 曾报损坏）
        pushed.clear()
        assert "已恢复远程文件" in remote.rollback(backup, confirm=True)
        assert pushed and pushed[-1] == b"current-v2\n"  # 副本存的是回滚前的当前内容
        # 注册表把 web1 改指向另一台机器：旧快照不能写过去
        p = _write_targets()
        data = json.loads(p.read_text())
        data["web1"]["host"] = "10.0.0.99"
        p.write_text(json.dumps(data))
        for call in (lambda: remote.rollback_plan(sid),
                     lambda: remote.rollback(sid, confirm=True)):
            try:
                call()
                raise AssertionError("目标被改指向后必须拒绝回滚")
            except remote.RemoteError as e:
                assert "不一致" in str(e)
    finally:
        remote._ssh = orig
        _write_targets()


def test_rollback_partial_failure_reports_backup():
    import tempfile as tf
    with tf.TemporaryDirectory() as td:
        a, b = Path(td, "a.conf"), Path(td, "b.conf")
        a.write_text("a1")
        b.write_text("b1")
        core.approve("printf noop", ttl_seconds=60)
        code, sid, _ = core.exec_change("printf noop", snapshot_files=[str(a), str(b)])
        assert code == 0 and sid
        a.write_text("a2")
        b.write_text("b2")
        real_replace = core.os.replace

        def flaky(src, dst):
            if Path(dst).resolve() == b.resolve():
                raise OSError("simulated write failure")
            return real_replace(src, dst)

        core.os.replace = flaky
        try:
            try:
                core.rollback(sid, confirm=True)
                raise AssertionError("第二个文件写失败应报错")
            except core.OpsxError as e:
                msg = str(e)
        finally:
            core.os.replace = real_replace
        assert "simulated write failure" in msg and "回滚前副本" in msg
        backup = re.search(r"回滚前副本: ([^（\s）]+)", msg).group(1)
        meta = json.loads(Path(core.STATE_DIR, "snapshots", backup, "meta.json").read_text())
        assert {Path(i["src"]).resolve() for i in meta["items"]} == {a.resolve(), b.resolve()}
        assert a.read_text() == "a1"  # 半恢复状态可查，且知道从哪恢复


def test_ci_workflow_runs_selftest():
    workflow = Path(__file__).resolve().parent / ".github" / "workflows" / "selftest.yml"
    assert workflow.is_file(), "自检必须接入 CI"
    assert "./opsx selftest" in workflow.read_text(encoding="utf-8")


def _with_targets(payload) -> callable:
    """临时替换 targets.json，返回还原函数（自检不碰用户真实状态目录）。"""
    path = Path(core.STATE_DIR).resolve()
    # 用 raise 而不是 assert：python -O 会剥离 assert，保护就失效了。
    if (not path.name.startswith("opsx-test-")
            or path.parent != Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError(f"自检状态目录不是临时目录，拒绝改写 targets.json: {path}")
    p = Path(core.STATE_DIR, "targets.json")
    old = p.read_text(encoding="utf-8") if p.exists() else None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def restore():
        if old is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(old, encoding="utf-8")
    return restore


def test_checks_parsers():
    """探针解析：df/meminfo/vm_stat/loadavg/systemd/docker 真实格式与无法解析路径。"""
    from agent import checks
    pct, mount, why = checks.parse_df(
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/vdb 1000 970 30 97% /data\n")
    assert pct == 97.0 and mount == "/data" and why == ""
    # 表头被 locale 翻译（zh_CN 的 df）也要能解析：只看数据行里的百分比列
    pct, mount, why = checks.parse_df(
        "文件系统 1024块 已用 可用 已用% 挂载点\n/dev/vda 1000 900 100 90% /data\n")
    assert pct == 90.0 and mount == "/data" and why == ""
    # 挂载点含空格时不能把挂载点截成半截
    pct, mount, why = checks.parse_df(
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/disk4s1 1000 950 50 95% /Volumes/My Disk\n")
    assert pct == 95.0 and mount == "/Volumes/My Disk", (pct, mount)
    assert checks.parse_df("Filesystem x\n")[0] is None
    assert checks.parse_df("garbage")[0] is None
    # /proc/meminfo 的键名固定英文，不受 locale 影响
    assert checks.parse_meminfo(
        "MemTotal:       1000000 kB\nMemFree:          100000 kB\n"
        "MemAvailable:    500000 kB\n")[0] == 50.0
    assert checks.parse_meminfo("MemTotal: 1000 kB\nMemFree: 100 kB\n"
                                "Buffers: 100 kB\nCached: 300 kB\n")[0] == 50.0
    assert checks.parse_meminfo("MemTotal: 1 2\n")[0] is None
    assert checks.parse_meminfo("<html>502 Bad Gateway</html>")[0] is None
    vm = ("Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
          "Pages free: 1000.\nPages active: 2000.\nPages wired down: 1000.\n"
          "Pages stored in compressor: 3000.\n")
    pct, why = checks.parse_vm_stat(vm, 4096 * 10000)
    assert abs(pct - 30.0) < 0.01, (pct, why)   # 压缩内存已含在 wired，不重复计数
    assert checks.parse_vm_stat(vm, 0)[0] is None
    assert checks.parse_load("0.10 0.20 0.30 1/123 4567") == (0.10, 0.20, 0.30)  # /proc/loadavg
    assert checks.parse_load("{ 2.46 2.51 3.09 }")[0] == 2.46                  # vm.loadavg
    assert checks.parse_load("18:04 up 12 days, load averages: 2.46 2.51 3.09") == \
        (2.46, 2.51, 3.09)
    assert checks.parse_load("18:04 up 1 day, load average: 0.10, 0.20, 0.30")[0] == 0.10
    assert checks.parse_load("nothing here") is None
    assert checks.parse_load("Mem: 1000 400 100") is None   # 非负载文本不能误判
    assert checks.parse_systemd_show("LoadState=loaded\nActiveState=failed\n"
                                     "SubState=dead\n")["active"] == "failed"
    assert checks.parse_docker_inspect("running,healthy,3\n") == {
        "status": "running", "health": "healthy", "restarts": 3}
    assert checks.parse_docker_inspect("[stderr]\nno such object") is None


def test_checks_inventory_validation():
    """目标配置校验：拼错字段/非法值必须报错，不能静默退化成"没有这项检查"。"""
    from agent import checks
    cases = [
        ({"a": {"host": "h", "checks": {"disk_paths": []}}}, "disk_paths"),
        ({"a": {"host": "h", "checks": {"disk_paths": ["rel/path"]}}}, "非法路径"),
        ({"a": {"host": "h", "checks": {"disk_paths": ["/data/../etc"]}}}, "非法路径"),
        ({"a": {"host": "h", "checks": {"disk_warn_pct": 101}}}, "disk_warn_pct"),
        ({"a": {"host": "h", "checks": {"disk_crit_pct": 80}}}, "不能小于"),
        ({"a": {"host": "h", "checks": {"timeout": 1}}}, "timeout"),
        ({"a": {"host": "h", "checks": {"no_such": 1}}}, "未知字段"),
        ({"a": {"host": "h", "services": [{"type": "runit", "name": "x"}]}},
         "services[1].type"),
        ({"a": {"host": "h", "services": [{"type": "docker", "name": "-rf"}]}}, "非法"),
        ({"a": {"host": "h", "enabled": "yes"}}, "enabled"),
        ({"a": {"host": "h", "disk_paths": ["/data"]}}, "未知字段"),  # 阈值写错层级
        ({"local": {"host": "h"}}, "不能"),
        ({"_local": {"host": "h"}}, "不能写 host"),
    ]
    orig = checks._exec
    checks._exec = lambda target, cmd, timeout: (0, "")
    try:
        for payload, needle in cases:
            restore = _with_targets(payload)
            try:
                checks.load_inventory()
                raise AssertionError(f"应拒绝非法配置: {payload}")
            except checks.CheckError as e:
                assert needle in str(e), (payload, str(e))
            finally:
                restore()
        restore = _with_targets({"skipme": {"host": "h", "enabled": False,
                                            "checks": {}}})
        try:
            inventory = checks.load_inventory()
            assert "skipme" in [t["name"] for t in inventory]
            assert "skipme" not in [t["name"] for t in checks._select(inventory, "all")]
            assert [t["name"] for t in checks._select(inventory, "skipme")] == ["skipme"]
            try:
                checks._select(inventory, "nope")
                raise AssertionError("未注册目标应报错")
            except checks.CheckError as e:
                assert "未注册的检查目标" in str(e)
        finally:
            restore()
    finally:
        checks._exec = orig


def test_checks_three_hosts_report():
    """W2 验收：一台正常、一台磁盘超阈值、一台不可达——报告保留全部异常且不谎报正常。"""
    from agent import checks
    restore = _with_targets({
        "web1": {"host": "10.0.0.1", "user": "deploy", "tags": ["prod", "web"],
                 "env": {"role": "nginx"},
                 "services": [{"type": "systemd", "name": "nginx"},
                              {"type": "docker", "name": "api"}]},
        "web2": {"host": "10.0.0.2", "user": "deploy",
                 "services": [{"type": "systemd", "name": "nginx"}],
                 "checks": {"disk_paths": ["/", "/data"]}},
        "db1": {"host": "10.0.0.3", "user": "root",
                "services": [{"type": "systemd", "name": "postgresql"}]},
    })
    orig = checks._exec

    def fake(target, cmd, timeout):
        if target == "db1":
            return 255, "\n[stderr]\nssh: connect to host 10.0.0.3 port 22: Operation timed out"
        if cmd == "uname -s":
            return 0, "Linux\n"
        if cmd.startswith("df "):
            if target == "web2" and "/data" in cmd:
                return 0, ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                           "/dev/vdb 100000 97000 3000 97% /data\n")
            return 0, ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                       "/dev/vda 100000 40000 60000 40% /\n")
        if cmd == "cat /proc/meminfo":
            return 0, ("MemTotal:       1000000 kB\nMemFree:          100000 kB\n"
                       "MemAvailable:    500000 kB\n")
        if cmd == "cat /proc/loadavg":
            return 0, "0.10 0.20 0.30 1/123 4567\n"
        if cmd == "nproc":
            return 0, "4\n"
        if cmd.startswith("systemctl show"):
            return 0, "LoadState=loaded\nActiveState=active\nSubState=running\n"
        if cmd.startswith("docker inspect"):
            return 0, "running,healthy,0\n"
        return 127, f"unexpected command: {cmd}"

    checks._exec = fake
    try:
        report = checks.run_checks(targets=["web1", "web2", "db1"])
        text = checks.format_report(report)
    finally:
        checks._exec = orig
        restore()

    assert report["verdict"] == "异常"
    assert report["complete"] is False
    summary = report["summary"]
    assert summary["checks"] == sum(summary[k] for k in
                                    ("ok", "warn", "critical", "unknown", "error"))
    by_name = {t["name"]: t for t in report["targets"]}
    assert by_name["web1"]["verdict"] == "正常"
    assert by_name["web2"]["verdict"] == "异常"
    assert by_name["db1"]["verdict"] == "不完整"
    lines = [a["line"] for a in report["anomalies"]]
    assert any("97.0%" in line for line in lines)
    assert any("[error]" in line and "db1" in line for line in lines)
    assert any("[unknown]" in line and "db1" in line for line in lines)
    for line in lines:  # 全部异常都必须出现在报告正文（不许被后文覆盖）
        assert line in text, line
    assert "97.0%" in text and "Operation timed out" in text
    assert "不完整（存在未采集项）" in text
    assert "## 全部异常与未采集项" in text


def test_checks_unknown_never_healthy():
    """采不到就标 unknown：解析失败/单元不存在/核数未知都不能被汇总成正常。"""
    from agent import checks
    restore = _with_targets({"web1": {"host": "10.0.0.1", "user": "deploy",
                                      "services": [{"type": "systemd", "name": "nginx"}]}})
    orig = checks._exec

    def fake(target, cmd, timeout):
        if cmd == "uname -s":
            return 0, "Linux\n"
        if cmd.startswith("df "):
            return 0, ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                       "/dev/vda 100 1 99 1% /\n")
        if cmd == "cat /proc/meminfo":
            return 0, "<html>502 Bad Gateway</html>\n"
        if cmd == "cat /proc/loadavg":
            return 0, "0.10 0.20 0.30 1/123 4567\n"
        if cmd == "nproc":
            return 3, "nproc: not found\n"
        if cmd.startswith("systemctl show"):
            return 0, "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        return 127, "unexpected"

    checks._exec = fake
    try:
        report = checks.run_checks(targets=["web1"])
        text = checks.format_report(report)
    finally:
        checks._exec = orig
        restore()
    assert report["verdict"] == "不完整"
    assert report["complete"] is False
    status = {c["name"]: c["status"] for c in report["targets"][0]["checks"]}
    assert status["内存"] == "unknown"
    assert status["负载"] == "unknown"
    assert status["服务 systemd:nginx"] == "unknown"
    assert "无法解析" in text and "单元不存在" in text


def test_inspect_tool_and_readonly_context():
    """inspect 工具：全部 ok 才报正常，报告落盘 0600；无人值守可用、子代理仍不放开写。"""
    from agent import checks, tools as agent_tools
    restore = _with_targets({"_local": {"services": [{"type": "docker", "name": "api"}]}})
    orig = checks._exec

    def fake(target, cmd, timeout):
        if cmd == "uname -s":
            return 0, "Linux\n"
        if cmd.startswith("df "):
            return 0, ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                       "/dev/vda 100 50 50 50% /\n")
        if cmd == "cat /proc/meminfo":
            return 0, ("MemTotal: 1000 kB\nMemAvailable: 600 kB\n")
        if cmd == "cat /proc/loadavg":
            return 0, "0.5 0.5 0.5 1/100 1000\n"
        if cmd == "nproc":
            return 0, "2\n"
        if cmd.startswith("docker inspect"):
            return 0, "running,none,0\n"
        return 127, "unexpected"

    checks._exec = fake
    try:
        out = agent_tools.dispatch("inspect", {}, agent_tools.ToolContext())
        bad = agent_tools.dispatch("inspect", {"groups": ["nope"]},
                                   agent_tools.ToolContext())
    finally:
        checks._exec = orig
        restore()
    assert "巡检报告" in out and "结论: **正常**" in out
    path = Path(re.search(r"\[全量报告\] (\S+)", out).group(1))
    assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
    assert "结论: **正常**" in path.read_text(encoding="utf-8")
    assert "须为" in bad                      # 非法分组被 schema 拒绝
    assert "inspect" in agent_tools.SCHEDULED_TOOLS
    assert "inspect" not in agent_tools.SUBAGENT_TOOLS
    for name in ("remember", "ops_approve", "ops_exec", "ops_rollback"):
        assert name not in agent_tools.SCHEDULED_TOOLS


def test_workflow_checks_first_and_llm_down():
    """API 完全不可用时仍产出规则检查结果，且规则异常逐项列出。"""
    from agent import checks, loop, tools as agent_tools
    restore = _with_targets({"web1": {"host": "10.0.0.1", "user": "deploy",
                                      "services": [{"type": "systemd", "name": "nginx"}],
                                      "checks": {"disk_paths": ["/data"]}}})
    user_dir = Path(core.STATE_DIR, "workflows")
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "wf-checks.json"
    p.write_text(json.dumps({
        "description": "规则先行",
        "checks": {"targets": ["web1"],
                   "groups": ["connectivity", "resources", "services"]},
        "steps": [{"name": "解读", "prompt": "规则结果：{checks}"},
                  {"name": "建议", "prompt": "前面：{prev}"}]}), encoding="utf-8")
    orig_exec, orig_turn = checks._exec, loop.run_turn

    def fake_exec(target, cmd, timeout):
        if cmd == "uname -s":
            return 0, "Linux\n"
        if cmd.startswith("df "):
            return 0, ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                       "/dev/vdb 100 98 2 98% /data\n")
        if cmd == "cat /proc/meminfo":
            return 0, ("MemTotal: 1000 kB\nMemAvailable: 600 kB\n")
        if cmd == "cat /proc/loadavg":
            return 0, "0.5 0.5 0.5 1/100 1000\n"
        if cmd == "nproc":
            return 0, "2\n"
        if cmd.startswith("systemctl show"):
            return 0, "LoadState=loaded\nActiveState=active\nSubState=running\n"
        return 127, "unexpected"

    def broken_llm(*args, **kwargs):
        raise RuntimeError("API 401 unauthorized")

    checks._exec, loop.run_turn = fake_exec, broken_llm
    try:
        out = agent_tools.dispatch("workflow", {"name": "wf-checks"},
                                   agent_tools.ToolContext())
    finally:
        checks._exec, loop.run_turn = orig_exec, orig_turn
        p.unlink(missing_ok=True)
        restore()
    assert "## 规则检查（确定性探针，模型未参与）" in out
    assert "98.0%" in out and "结论 **异常**" in out
    assert "全部列出" in out
    assert "[失败] API 401 unauthorized" in out
    assert "LLM 解读：0/2 步成功" in out and "解读不完整" in out
    assert "不得汇总为正常" in out
    saved = Path(re.search(r"\[全量报告\] (\S+)", out).group(1)).read_text(encoding="utf-8")
    assert "98.0%" in saved and "LLM 解读：0/2 步成功" in saved


def test_workflow_summary_keeps_all_steps():
    """汇总保留每步结果与状态；早期异常不被最后一步覆盖；占位符只替换一次。"""
    from agent import loop, tools as agent_tools
    user_dir = Path(core.STATE_DIR, "workflows")
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "wf-agg.json"
    p.write_text(json.dumps({"steps": [
        {"name": "一", "prompt": "第一步"},
        {"name": "二", "prompt": "上一步：{prev}"},
        {"name": "三", "prompt": "全部步骤：{steps}"}]}), encoding="utf-8")
    calls = []
    orig = loop.run_turn

    def fake(task, session, ctx, **kwargs):
        calls.append(task)
        if task == "第一步":
            return "早期异常A：磁盘 96%"
        if "上一步" in task:
            return "含 {prev} 字面量"   # 替换一次后不再二次展开
        return "正常"

    loop.run_turn = fake
    try:
        out = agent_tools.dispatch("workflow", {"name": "wf-agg"},
                                   agent_tools.ToolContext())
    finally:
        loop.run_turn = orig
        p.unlink(missing_ok=True)
    assert "早期异常A：磁盘 96%" in out
    assert out.count("## 步骤") == 3
    assert "步骤状态：1「一」完成｜2「二」完成｜3「三」完成" in out
    assert "早期异常A：磁盘 96%" in calls[2]     # {steps} 索引带到第三步
    assert "上一步：早期异常A：磁盘 96%" in calls[1]   # {prev} 拿到上一步结论
    assert "含 {prev} 字面量" in calls[2]              # 替入内容不会被二次展开
    assert "本 workflow 未配置 checks 块" in out


def test_workflow_checks_config_validation():
    """checks 块写错要报错：静默退化成"没有检查"会让报告谎报正常。"""
    from agent import loop, tools as agent_tools
    user_dir = Path(core.STATE_DIR, "workflows")
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "wf-bad.json"
    steps = [{"name": "x", "prompt": "y"}]
    cases = [
        ({"checks": [], "steps": steps}, "checks 须为对象"),
        ({"checks": {"targets": []}, "steps": steps}, "checks.targets"),
        ({"checks": {"groups": ["disk"]}, "steps": steps}, "checks.groups"),
        ({"checks": {"nope": 1}, "steps": steps}, "未知字段"),
    ]
    orig = loop.run_turn
    loop.run_turn = lambda *a, **k: "ok"
    try:
        for payload, needle in cases:
            p.write_text(json.dumps(payload), encoding="utf-8")
            result = agent_tools.dispatch("workflow", {"name": "wf-bad"},
                                          agent_tools.ToolContext())
            assert needle in result, (payload, result)
        p.write_text(json.dumps({"checks": {"targets": ["ghost"]}, "steps": steps}),
                     encoding="utf-8")
        result = agent_tools.dispatch("workflow", {"name": "wf-bad"},
                                      agent_tools.ToolContext())
        assert "未注册的检查目标" in result      # 目标名写错必须显式暴露
        assert "规则检查：未执行" in result
    finally:
        loop.run_turn = orig
        p.unlink(missing_ok=True)


def test_readonly_whitelist_nproc_sysctl():
    """资源探针新增的只读取值命令进白名单；sysctl 写形态仍不是 R0。"""
    assert core.classify("nproc")[0] == "R0"
    assert core.classify("sysctl -n hw.memsize")[0] == "R0"
    assert core.classify("sysctl -n hw.memsize hw.ncpu")[0] == "R0"
    assert core.classify("sysctl -a")[0] == "R0"
    for cmd in ("sysctl -w kern.maxfiles=1", "sysctl kern.maxfiles=1",
                "sysctl -n hw.memsize; sysctl -w kern.maxfiles=1"):
        assert core.classify(cmd)[0] != "R0", cmd


def _linux_fake(extra=None):
    """构造一个只覆盖 Linux 探针的假执行器，避免每个用例重复同一堆分支。"""
    routes = {
        "uname -s": (0, "Linux\n"),
        "cat /proc/meminfo": (0, "MemTotal: 1000 kB\nMemAvailable: 600 kB\n"),
        "cat /proc/loadavg": (0, "0.1 0.1 0.1 1/100 1000\n"),
        "nproc": (0, "4\n"),
    }
    routes.update(extra or {})

    def fake(target, cmd, timeout):
        if cmd in routes:
            return routes[cmd]
        if cmd.startswith("df "):
            return 0, ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                       "/dev/vda 100 40 60 40% /\n")
        if cmd.startswith("systemctl show"):
            return 0, "LoadState=loaded\nActiveState=active\nSubState=running\n"
        if cmd.startswith("docker inspect"):
            return 0, "running,none,0\n"
        return 127, f"unexpected command: {cmd}"
    return fake


def test_checks_partial_groups_cannot_claim_ok():
    """只跑部分分组时结论最高"不完整"：单看连通性不能宣称主机正常。"""
    from agent import checks
    restore = _with_targets({"web1": {"host": "10.0.0.1"}})
    orig = checks._exec
    checks._exec = _linux_fake()
    try:
        partial = checks.run_checks(targets=["web1"], groups=["connectivity"])
        full = checks.run_checks(targets=["web1"], groups=["connectivity", "resources"])
    finally:
        checks._exec = orig
        restore()
    assert partial["coverage"]["groups_skipped"] == ["resources", "services"]
    assert partial["complete"] is False
    assert partial["verdict"] == "不完整"
    # 目标级也不能写"正常"：只跑了连通性不等于这台主机健康
    assert partial["targets"][0]["verdict"] == "未完整检查"
    assert "未检查分组 resources/services" in checks.format_report(partial)
    # 漏了 services 分组：即使已跑的项全 ok，也不能说正常
    assert full["summary"][checks.CRITICAL] == 0
    assert full["verdict"] == "不完整"


def test_checks_skipped_target_is_visible():
    """enabled=false 的目标在 all 里跳过，但必须在报告里点名，不能凭空消失。"""
    from agent import checks
    restore = _with_targets({"offline": {"host": "10.0.0.2", "enabled": False},
                             "web1": {"host": "10.0.0.1"}})
    orig = checks._exec
    checks._exec = _linux_fake()
    try:
        report = checks.run_checks()
    finally:
        checks._exec = orig
        restore()
    assert report["coverage"]["targets_skipped"] == ["offline"]
    assert "offline" in checks.format_report(report)
    assert "offline" not in report["target_names"]


def test_checks_probe_budget_bounds_work():
    """单目标探针有界：时间预算与次数上限各自生效，超预算标 unknown 而不是漏项。"""
    from agent import checks
    import time as _t
    assert checks._budget_state({"left": 5, "deadline": _t.monotonic() + 30}) == ""
    assert "时间预算" in checks._budget_state({"left": 5, "deadline": _t.monotonic() - 1})
    assert "次数" in checks._budget_state({"left": 0, "deadline": _t.monotonic() + 30})
    services = [{"type": "docker", "name": f"c{i}"} for i in range(5)]
    restore = _with_targets({"web1": {"host": "10.0.0.1", "services": services}})
    orig_exec, orig_budget = checks._exec, checks.PROBE_BUDGET
    checks._exec, checks.PROBE_BUDGET = _linux_fake(), 4
    try:
        report = checks.run_checks(targets=["web1"])
    finally:
        checks._exec, checks.PROBE_BUDGET = orig_exec, orig_budget
        restore()
    details = [c["detail"] for c in report["targets"][0]["checks"]]
    assert any("预算" in d for d in details), details
    assert report["verdict"] == "不完整"
    # 每个非 ok 项都在异常清单里，没有被"预算"吃掉
    assert len(report["anomalies"]) >= 5


def test_checks_underscore_target_still_usable():
    """除 _local 外，下划线开头的目标名保持原样（历史配置不能因新增保留条目失效）。"""
    from agent import checks
    import remote
    restore = _with_targets({"_db": {"host": "10.0.0.9", "user": "root"}})
    orig = checks._exec
    checks._exec = _linux_fake()
    try:
        names = [t["name"] for t in checks.load_inventory()]
        assert "_db" in names
        assert remote.get_target("_db")["host"] == "10.0.0.9"
        try:
            remote.get_target("_local")
            raise AssertionError("_local 不能当 SSH 目标")
        except remote.RemoteError as e:
            assert "本机检查配置" in str(e)
        assert "_db" in "\n".join(remote.describe())
    finally:
        checks._exec = orig
        restore()


def test_report_save_failure_degrades():
    """报告落盘失败不能让已跑完的检查结果丢失。"""
    from agent import checks, tools as agent_tools
    restore = _with_targets({"_local": {"services": [{"type": "docker", "name": "api"}]}})
    orig_exec, orig_dir = checks._exec, checks._report_dir
    checks._exec = _linux_fake()
    checks._report_dir = lambda: Path("/dev/null/reports")  # 落盘必然失败
    try:
        assert checks.save_report("x", "body") is None
        out = agent_tools.dispatch("inspect", {}, agent_tools.ToolContext())
    finally:
        checks._exec, checks._report_dir = orig_exec, orig_dir
        restore()
    assert "巡检报告" in out and "结论: **正常**" in out
    assert "报告未能落盘" in out


def test_inspect_exit_codes():
    """退出码可被脚本判定：0 正常 / 2 有异常 / 3 不完整。"""
    from agent import checks
    assert checks.exit_code({"verdict": "正常"}) == 0
    assert checks.exit_code({"verdict": "警告"}) == 2
    assert checks.exit_code({"verdict": "异常"}) == 2
    assert checks.exit_code({"verdict": "不完整"}) == 3
    restore = _with_targets({"_local": {"services": [{"type": "docker", "name": "api"}]}})
    orig = checks._exec
    checks._exec = _linux_fake()
    try:
        ok_code = checks.cli(["--target", "local", "--json"])
    finally:
        checks._exec = orig
        restore()
    assert ok_code == 0


def test_checks_parsers_ignore_stderr_and_paths_with_spaces():
    """df 的 stderr 告警不能被当成容量列；带空格/中文的挂载点要能配置。"""
    from agent import checks
    pct, mount, why = checks.parse_df(
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/vda 100 90 10 90% /data\n"
        "[stderr]\ndf: warning: 90% of inodes used\n")
    assert pct == 90.0 and mount == "/data", (pct, mount)
    assert checks.parse_df("[stderr]\ndf: warning: 90% used\n")[0] is None
    # 路径允许空格与非 ASCII（shlex.quote 负责安全传递）
    cfg = checks._norm_checks("web1", {"disk_paths": ["/Volumes/My Disk", "/数据/盘"]})
    assert cfg["disk_paths"] == ["/Volumes/My Disk", "/数据/盘"]
    for bad in ("/data/../etc", "relative/path", "/tmp\nx"):
        try:
            checks._norm_checks("web1", {"disk_paths": [bad]})
            raise AssertionError(f"应拒绝非法路径: {bad!r}")
        except checks.CheckError:
            pass


def test_workflow_keeps_footer_when_truncated():
    """输出超长被截断时，末尾「汇总完整性」必须仍在可见范围内。"""
    from agent import loop, tools as agent_tools
    user_dir = Path(core.STATE_DIR, "workflows")
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "wf-big.json"
    p.write_text(json.dumps({"steps": [
        {"name": "一", "prompt": "第一步"},
        {"name": "二", "prompt": "第二步"},
        {"name": "三", "prompt": "第三步"}]}), encoding="utf-8")
    orig = loop.run_turn
    loop.run_turn = lambda task, *a, **k: "X" * 12000
    try:
        out = agent_tools.dispatch("workflow", {"name": "wf-big"},
                                   agent_tools.ToolContext())
    finally:
        loop.run_turn = orig
        p.unlink(missing_ok=True)
    assert len(out) <= agent_tools.OUTPUT_LIMIT + 2000, len(out)
    assert "汇总完整性" in out          # 尾部没被截掉
    assert "LLM 解读：3/3 步成功" in out
    assert "中段已截断" in out
    saved = Path(re.search(r"\[全量报告\] (\S+)", out).group(1)).read_text(encoding="utf-8")
    assert saved.count("X") == 36000    # 报告文件保留完整步骤原文


def test_workflow_step_not_done_is_not_success():
    """空回复与超轮次保护都不算"解读完成"，不能在汇总里写全成功。"""
    from agent import loop, tools as agent_tools
    user_dir = Path(core.STATE_DIR, "workflows")
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "wf-partial.json"
    p.write_text(json.dumps({"steps": [
        {"name": "一", "prompt": "第一步"},
        {"name": "二", "prompt": "第二步"},
        {"name": "三", "prompt": "第三步"}]}), encoding="utf-8")
    results = ["正常结论", "", "分析中\n[已达最大轮次保护，终止]"]
    orig = loop.run_turn
    loop.run_turn = lambda task, *a, **k: results.pop(0)
    try:
        out = agent_tools.dispatch("workflow", {"name": "wf-partial"},
                                   agent_tools.ToolContext())
    finally:
        loop.run_turn = orig
        p.unlink(missing_ok=True)
    assert "1/3 步成功" in out and "解读不完整" in out
    assert "2「二」失败（空输出）" in out
    assert "3「三」失败（超轮次保护）" in out


def test_inspect_cli_json_stays_valid_with_explain():
    """--json 时解读写 stderr，stdout 必须仍是可解析的 JSON。"""
    from agent import checks
    restore = _with_targets({"_local": {"services": [{"type": "docker", "name": "api"}]}})
    orig_exec, orig_explain = checks._exec, checks._explain
    checks._exec = _linux_fake()
    checks._explain = lambda text, model: "解读正文"
    stdout, stderr = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        code = checks.cli(["--target", "local", "--json", "--explain"])
    finally:
        sys.stdout, sys.stderr = saved
        checks._exec, checks._explain = orig_exec, orig_explain
        restore()
    assert code == 0
    payload = json.loads(stdout.getvalue())      # stdout 只能是 JSON
    assert payload["verdict"] == "正常"
    assert "解读正文" in stderr.getvalue()


def test_inspect_cli_usage_error_is_not_finding():
    """用法错误退出 1，不能与"发现异常"的 2 混淆。"""
    from agent import checks
    saved = sys.stderr
    sys.stderr = io.StringIO()
    try:
        try:
            checks.cli(["--group", "nope"])
            raise AssertionError("非法分组应被 argparse 拒绝")
        except SystemExit as e:
            code = e.code
        text = sys.stderr.getvalue()
    finally:
        sys.stderr = saved
    assert code == 1, code
    assert "错误" in text


def _clean_schedules():
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)
    import shutil
    shutil.rmtree(core.STATE_DIR / "runs", ignore_errors=True)


def test_schedule_run_records_and_query():
    """每次执行都有运行记录：run/session/pid/耗时/结论可查询，且预览脱敏。"""
    import shutil
    from agent import schedule, tools as agent_tools
    _clean_schedules()
    entry = schedule.add("巡检", every_seconds=60)
    items = schedule.list_entries()
    items[0]["next_run"] = time.time() - 1
    schedule._save(items)
    orig = schedule._run_entry
    schedule._run_entry = lambda e, m, **kw: ("发现正常\n结论：正常\n"
                                              "key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    try:
        records = schedule.run_due()
    finally:
        schedule._run_entry = orig
    rec = records[0]
    assert rec["status"] == "succeeded" and rec["verdict"] == "正常"
    assert rec["session_id"] and rec["pid"] == os.getpid()
    assert rec["attempts"] == 1 and rec["duration_s"] is not None
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in rec["result_preview"]  # 预览也脱敏
    saved = json.loads((core.STATE_DIR / "runs" / f"{rec['run_id']}.json").read_text())
    assert saved["status"] == "succeeded" and saved["run_id"] == rec["run_id"]
    # 历史查询：全部 / 按任务过滤 / 文本形态
    listed = schedule.list_runs()
    assert listed[0]["run_id"] == rec["run_id"]
    assert schedule.list_runs(task_id=entry["id"])[0]["run_id"] == rec["run_id"]
    assert schedule.list_runs(task_id="sch-nope") == []
    text = schedule.format_runs()
    assert rec["run_id"] in text and "succeeded 正常" in text
    # 工具侧可查
    out = agent_tools.dispatch("schedule", {"action": "runs"}, agent_tools.ToolContext())
    assert rec["run_id"] in out
    _clean_schedules()


def test_schedule_pause_resume():
    """暂停后到期不跑；恢复从当前时间重新起算（暂停期不补跑、不记 missed）。"""
    from agent import schedule, tools as agent_tools
    _clean_schedules()
    entry = schedule.add("巡检", every_seconds=3600)
    ctx = agent_tools.ToolContext()
    out = agent_tools.dispatch("schedule", {"action": "pause", "id": entry["id"]}, ctx)
    assert "已暂停" in out
    items = schedule.list_entries()
    items[0]["next_run"] = time.time() - 100     # 暂停期间已过期
    schedule._save(items)
    assert schedule.run_due() == []               # 暂停的不执行
    assert "暂停" in schedule.format_list()
    out = agent_tools.dispatch("schedule", {"action": "resume", "id": entry["id"]}, ctx)
    assert "已恢复" in out and "下次" in out
    item = schedule.list_entries()[0]
    assert item["enabled"] is True
    assert item["next_run"] > time.time()         # 不补跑暂停期
    assert schedule.run_due() == []               # 恢复后未到期，不立即跑
    _clean_schedules()


def test_schedule_interrupted_vs_missed():
    """daemon 中断后可辨别：遗留 running → interrupted；停机错过的时点 → missed。"""
    from agent import schedule
    _clean_schedules()
    entry = schedule.add("巡检", every_seconds=60)
    # 伪造一条"进程已死但状态还是 running"的记录（比如 daemon 被 kill -9）
    dead = schedule._begin_run(entry, None)
    dead["pid"] = 99999999                        # 不存在的 pid
    schedule._write_run(dead)
    assert schedule.reconcile_runs() == 1
    saved = json.loads((core.STATE_DIR / "runs" / f"{dead['run_id']}.json").read_text())
    assert saved["status"] == "interrupted" and "中断" in saved["error"]
    # 活着的进程不误标
    alive = schedule._begin_run(entry, None)
    assert schedule.reconcile_runs() == 0
    saved = json.loads((core.STATE_DIR / "runs" / f"{alive['run_id']}.json").read_text())
    assert saved["status"] == "running"
    # 停机错过：心跳停在 5 分钟前（daemon 那时还在），next_run 也是 5 分钟前
    items = schedule.list_entries()
    anchor = time.time() - 300
    items[0]["next_run"] = anchor
    schedule._save(items, {"last_scan": anchor - 1})
    orig = schedule._run_entry
    schedule._run_entry = lambda e, m, **kw: "结论：正常"
    try:
        records = schedule.run_due()
    finally:
        schedule._run_entry = orig
    assert [r["status"] for r in records] == ["missed", "succeeded"]
    runs = schedule.list_runs()
    kinds = {r["status"] for r in runs}
    assert "missed" in kinds and "interrupted" in kinds and "succeeded" in kinds
    missed = [r for r in runs if r["status"] == "missed"][0]
    assert missed["missed_count"] == 5, missed["missed_count"]
    assert f"错过 {missed['missed_count']} 个" in missed["error"]
    # 立即重跑（未到期）不重复执行
    schedule._run_entry = lambda e, m, **kw: "结论：正常"
    try:
        assert schedule.run_due() == []
    finally:
        schedule._run_entry = orig
    # 心跳已刷新：把 next_run 再拨回过去也只是"迟到了"，不该重复记 missed
    items = schedule.list_entries()
    items[0]["next_run"] = time.time() - 300
    schedule._save(items)
    schedule._run_entry = lambda e, m, **kw: "结论：正常"
    try:
        assert [r["status"] for r in schedule.run_due()] == ["succeeded"]
    finally:
        schedule._run_entry = orig
    assert sum(1 for r in schedule.list_runs() if r["status"] == "missed") == 1
    _clean_schedules()


def test_schedule_timeout_budget_and_retry():
    """整任务超时记 timeout（退出码 1）；瞬时失败有限重试后记 attempts。"""
    from agent import schedule
    _clean_schedules()
    schedule.add("巡检", every_seconds=60, timeout_seconds=60)
    real_sleep = time.sleep
    time.sleep = lambda s: None
    orig = schedule._run_entry
    try:
        # 重试后成功：每次尝试独立会话，失败信息保留在 attempt_errors
        state = {"n": 0}
        sessions, deadlines = [], []

        def flaky(entry, model, **kw):
            state["n"] += 1
            sessions.append(kw["session"].id)
            deadlines.append(kw.get("deadline"))
            if state["n"] == 1:
                raise ConnectionError("ssh reset")
            return "结论：正常"

        items = schedule.list_entries()
        items[0]["next_run"] = time.time() - 1
        schedule._save(items)
        schedule._run_entry = flaky
        rec = schedule.run_due()[0]
        assert rec["status"] == "succeeded" and rec["attempts"] == 2
        assert rec["error"] is None
        assert rec["attempt_errors"] and "ConnectionError" in rec["attempt_errors"][0]
        assert len(set(sessions)) == 2                 # 不复用半截会话
        assert all(d and d > time.monotonic() for d in deadlines)  # deadline 真的传下去了

        # 始终失败 → failed
        items = schedule.list_entries()
        items[0]["next_run"] = time.time() - 1
        schedule._save(items)
        schedule._run_entry = lambda e, m, **kw: (_ for _ in ()).throw(RuntimeError("down"))
        rec = schedule.run_due()[0]
        assert rec["status"] == "failed" and rec["attempts"] == 2
        assert "RuntimeError" in rec["error"]

        # 超预算 → timeout
        items = schedule.list_entries()
        items[0]["next_run"] = time.time() - 1
        schedule._save(items)
        schedule._run_entry = lambda e, m, **kw: "[任务超时] 已终止：任务时间预算耗尽"
        rec = schedule.run_due()[0]
        assert rec["status"] == "timeout" and "时间预算" in rec["error"]
    finally:
        time.sleep = real_sleep
        schedule._run_entry = orig
        _clean_schedules()
    # 退出码：运行失败 > 异常发现 > 不完整 > 正常
    assert schedule.runs_exit_code([{"status": "succeeded", "verdict": "正常"}]) == 0
    assert schedule.runs_exit_code([{"status": "succeeded", "verdict": "异常"}]) == 2
    assert schedule.runs_exit_code([{"status": "succeeded", "verdict": "未判定"}]) == 3
    assert schedule.runs_exit_code([{"status": "succeeded", "verdict": "异常"},
                                    {"status": "timeout", "verdict": None}]) == 1


def test_schedule_once_exit_codes():
    """--once 的退出码区分运行失败与检查发现异常。"""
    from agent import schedule
    _clean_schedules()
    schedule.add("巡检", every_seconds=60, timeout_seconds=60)

    def due_now():
        items = schedule.list_entries()
        items[0]["next_run"] = time.time() - 1
        schedule._save(items)

    real_sleep = time.sleep
    time.sleep = lambda s: None
    orig = schedule._run_entry
    try:
        schedule._run_entry = lambda e, m, **kw: "结论：正常"
        due_now()
        assert schedule.daemon(once=True) == 0
        schedule._run_entry = lambda e, m, **kw: "磁盘 97%\n结论：异常"
        due_now()
        assert schedule.daemon(once=True) == 2
        schedule._run_entry = lambda e, m, **kw: "看起来都还行"
        due_now()
        assert schedule.daemon(once=True) == 3   # 无可解析结论 = 未判定，不是正常
        schedule._run_entry = lambda e, m, **kw: (_ for _ in ()).throw(RuntimeError("x"))
        due_now()
        assert schedule.daemon(once=True) == 1
    finally:
        time.sleep = real_sleep
        schedule._run_entry = orig
        _clean_schedules()


def test_run_turn_deadline_stops_at_turn_boundary():
    """任务时间预算在轮次边界生效：到点不再调用 LLM，结果带超时标记。"""
    from agent import loop, session as agent_session, tools as agent_tools
    calls = []

    def fake_chat(*a, **k):
        calls.append(1)
        return {"role": "assistant", "content": "结论：正常"}

    orig = loop.llm.chat
    loop.llm.chat = fake_chat
    try:
        ctx = agent_tools.ToolContext(deadline=time.monotonic() - 1)
        text = loop.run_turn("巡检", agent_session.Session(), ctx)
        assert loop.DEADLINE_MARK in text and "时间预算" in text
        assert not calls                       # 预算已尽：一次 LLM 都不该调
        ctx = agent_tools.ToolContext(deadline=time.monotonic() + 60)
        text = loop.run_turn("巡检", agent_session.Session(), ctx)
        assert "结论：正常" in text and len(calls) == 1
    finally:
        loop.llm.chat = orig


def test_workflow_deadline_marks_remaining_steps():
    """workflow 在预算耗尽后：剩余步骤记失败（时间预算耗尽），不静默跳过。"""
    from agent import loop, tools as agent_tools
    user_dir = Path(core.STATE_DIR, "workflows")
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "wf-deadline.json"
    p.write_text(json.dumps({"steps": [
        {"name": "一", "prompt": "第一步"},
        {"name": "二", "prompt": "第二步"}]}), encoding="utf-8")
    orig = loop.run_turn
    loop.run_turn = lambda *a, **k: "结论：正常"
    try:
        ctx = agent_tools.ToolContext(deadline=time.monotonic() - 1)
        out = agent_tools.dispatch("workflow", {"name": "wf-deadline"}, ctx)
    finally:
        loop.run_turn = orig
        p.unlink(missing_ok=True)
    assert "1「一」失败（时间预算耗尽）" in out
    assert "2「二」失败（时间预算耗尽）" in out      # 剩余步骤显式记失败，不静默跳过
    assert "LLM 解读：0/2 步成功" in out and "解读不完整" in out


def test_workflow_step_cut_by_deadline_is_not_success():
    """单步执行到一半被时间预算截断：这一步记失败（不是"完成"）。"""
    from agent import loop, tools as agent_tools
    user_dir = Path(core.STATE_DIR, "workflows")
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "wf-cut.json"
    p.write_text(json.dumps({"steps": [
        {"name": "一", "prompt": "第一步"},
        {"name": "二", "prompt": "第二步"}]}), encoding="utf-8")
    results = ["部分结论\n[任务超时] 已终止：任务时间预算耗尽", "结论：正常"]
    orig = loop.run_turn
    loop.run_turn = lambda *a, **k: results.pop(0)
    try:
        out = agent_tools.dispatch("workflow", {"name": "wf-cut"},
                                   agent_tools.ToolContext())
    finally:
        loop.run_turn = orig
        p.unlink(missing_ok=True)
    assert "1「一」失败（时间预算耗尽）" in out
    assert "2「二」完成" in out
    assert "LLM 解读：1/2 步成功" in out


def test_schedule_verdict_parsing_is_conservative():
    """结论只认"单独一行"或"报告头"，日志/引用里的结论不能翻转最终判定。"""
    from agent import schedule
    assert schedule._detect_verdict("巡检完成\n结论：正常") == "正常"
    assert schedule._detect_verdict("结论: **异常**") == "异常"
    assert schedule._detect_verdict("结论：异常项 3 个") == "未判定"   # 正文不是结论行
    assert schedule._detect_verdict("日志原文：结论：正常\n实际检查失败\n结论：异常") == "异常"
    assert schedule._detect_verdict(
        "- 结论: **异常**｜完整性: 不完整（存在未采集项）") == "异常"
    assert schedule._detect_verdict("本次不得汇总为正常") == "未判定"
    assert schedule._detect_verdict("") == "未判定"
    # 轮次/预算截断与"解读不完整"不能停在正常
    from agent import loop
    assert loop.MAX_TURNS_MARK == "[已达最大轮次保护"
    assert loop.DEADLINE_MARK == "[任务超时]"


def test_schedule_truncated_run_is_not_normal():
    """模型被轮次截断但正文写了"结论：正常" → 记 succeeded 但结论降级为不完整（exit 3）。"""
    from agent import loop, schedule
    _clean_schedules()
    schedule.add("巡检", every_seconds=60)
    items = schedule.list_entries()
    items[0]["next_run"] = time.time() - 1
    schedule._save(items)
    orig = schedule._run_entry
    schedule._run_entry = lambda e, m, **kw: f"结论：正常\n{loop.MAX_TURNS_MARK}，终止]"
    try:
        rec = schedule.run_due()[0]
    finally:
        schedule._run_entry = orig
        _clean_schedules()
    assert rec["verdict"] == "不完整"
    assert schedule.runs_exit_code([rec]) == 3


def test_schedule_record_failure_does_not_stop_other_tasks():
    """运行记录写不进去时仍然执行并返回结果，且不影响后续任务。"""
    from agent import schedule
    _clean_schedules()
    first = schedule.add("巡检A", every_seconds=60)
    second = schedule.add("巡检B", every_seconds=60)
    items = schedule.list_entries()
    for it in items:
        it["next_run"] = time.time() - 1
    schedule._save(items)
    orig_run, orig_write = schedule._run_entry, schedule._write_run

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    schedule._run_entry = lambda e, m, **kw: "结论：正常"
    schedule._write_run = boom
    try:
        records = schedule.run_due()
    finally:
        schedule._run_entry, schedule._write_run = orig_run, orig_write
        _clean_schedules()
    assert [r["task_id"] for r in records] == [first["id"], second["id"]]
    assert all(r["status"] == "succeeded" and r.get("unrecorded") for r in records)


def test_schedule_corrupt_records_and_settings_are_tolerated():
    """损坏的运行记录/手工改过的字段不能让查询或整轮调度崩溃。"""
    from agent import schedule
    _clean_schedules()
    runs = core.STATE_DIR / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "x.json").write_text(json.dumps({"run_id": "x", "status": "succeeded"}))
    (runs / "y.json").write_text(json.dumps({
        "run_id": "y", "status": "running", "pid": 10 ** 30,
        "started": "2026-10-21 10:00:00", "started_ts": 1}))
    assert schedule.list_runs() == []          # 字段不全/pid 越界都跳过
    assert schedule.reconcile_runs() == 0      # 不因坏记录崩溃
    assert schedule.format_runs() == "(无运行记录)"
    # 正向对照：一条合法记录必须仍然可见（否则"全跳过"的断言可能是空转）
    schedule.add("巡检", every_seconds=60)
    good_entry = schedule.list_entries()[0]
    good = schedule._begin_run(good_entry, None)
    schedule._write_run(dict(good, status="succeeded", finished="2026-10-21 10:00:01",
                             finished_ts=time.time(), duration_s=1.0, verdict="正常"))
    assert [r["run_id"] for r in schedule.list_runs()] == [good["run_id"]]
    assert good["run_id"] in schedule.format_runs()
    # every_seconds 是数字字符串（手工编辑/旧文件）：归一化后照常执行，不抛 TypeError
    schedule.add("巡检", every_seconds=60)
    items = schedule.list_entries()
    items[0]["every_seconds"] = "60"
    items[0]["next_run"] = time.time() - 1
    schedule._save(items, {"last_scan": time.time() - 1})
    orig = schedule._run_entry
    schedule._run_entry = lambda e, m, **kw: "结论：正常"
    try:
        records = schedule.run_due()
    finally:
        schedule._run_entry = orig
        _clean_schedules()
    assert [r["status"] for r in records] == ["succeeded"]


def test_schedule_missed_ignores_own_slowness():
    """心跳新鲜时不算停机：长任务/排队不会被记成 missed；无心跳或 next_run 损坏不推断。"""
    from agent import schedule
    now = time.time()
    every = {"every_seconds": 60}
    # daemon 10 秒前刚心跳过，任务自己跑了 1 小时 → 错过 0
    assert schedule._missed_count(every, now - 3600, now - 10, now) == 0
    # 心跳停在 5 分钟前 → 错过 5 次
    assert schedule._missed_count(every, now - 300, now - 300, now) == 5
    # 首次运行没有心跳、或 next_run 缺失/为 0：不做推断
    assert schedule._missed_count(every, now - 300, 0, now) == 0
    assert schedule._missed_count(every, 0, now - 300, now) == 0
    assert schedule._missed_count({"daily": "09:00"}, now - 3 * 86400, now - 3 * 86400, now) >= 2


def test_schedule_resume_keeps_enabled_schedule_and_pause_cancels():
    """对已启用任务重复 resume 不动 next_run；暂停后排队中的执行被取消。"""
    from agent import schedule
    _clean_schedules()
    entry = schedule.add("巡检", every_seconds=3600)
    planned = schedule.list_entries()[0]["next_run"]
    schedule.resume(entry["id"])                  # 本来就启用：不改排期
    assert schedule.list_entries()[0]["next_run"] == planned
    schedule.pause(entry["id"])
    assert schedule._claim_due(entry["id"], time.time()) is None  # 暂停后不再被认领
    schedule.resume(entry["id"])
    items = schedule.list_entries()
    items[0]["next_run"] = time.time() - 1       # 恢复后到期：可被认领
    schedule._save(items)
    assert schedule._claim_due(entry["id"], time.time()) is not None
    _clean_schedules()


def test_schedule_report_path_must_stay_in_reports_dir():
    """report_path 取自模型文本，只能接受状态目录下 reports/ 里的真实路径。"""
    from agent import schedule
    _clean_schedules()
    entry = schedule.add("巡检", every_seconds=60)
    reports = core.STATE_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    real = reports / "20261021-test.md"
    real.write_text("x", encoding="utf-8")
    try:
        rec = schedule._finish_run(schedule._begin_run(entry, None), entry, "succeeded",
                                   verdict="正常", result_text=f"[全量报告] {real}")
        assert rec["report_path"] == str(real.resolve())
        spoof = schedule._finish_run(schedule._begin_run(entry, None), entry, "succeeded",
                                     verdict="正常", result_text="[全量报告] /etc/passwd")
        assert spoof["report_path"] is None
    finally:
        _clean_schedules()


def test_schedule_lock_conflict_reports_not_run():
    """已有调度进程时 --once 返回 3：cron 不能把它当成"跑完且正常"。"""
    from agent import schedule
    _clean_schedules()
    try:
        with schedule._locked("schedules.lock", block=False) as held:
            assert held is True
            assert schedule.daemon(once=True) == 3
    finally:
        _clean_schedules()


def test_schedule_queue_survives_kill_and_failed_record_persists():
    """两阶段认领：执行中被打断时，排队任务仍是到期状态（下轮补上）；兜底失败记录可查。"""
    from agent import schedule
    _clean_schedules()
    try:
        a = schedule.add("巡检A", every_seconds=60)
        b = schedule.add("巡检B", every_seconds=60)
        items = schedule.list_entries()
        for it in items:
            it["next_run"] = time.time() - 1
        schedule._save(items)
        state = {"n": 0}

        def killed_after_first(entry, model, **kw):
            state["n"] += 1
            if entry["id"] == a["id"]:
                raise KeyboardInterrupt
            return "结论：正常"

        orig = schedule._run_entry
        schedule._run_entry = killed_after_first
        try:
            try:
                schedule.run_due()
                raise AssertionError("KeyboardInterrupt 应向上传播（模拟进程被杀）")
            except KeyboardInterrupt:
                pass
            left = {it["id"]: it for it in schedule.list_entries()}
            assert left[a["id"]]["next_run"] > time.time()   # A 已认领，不会重复
            assert left[b["id"]]["next_run"] <= time.time()  # B 未认领，仍是到期状态
            records = schedule.run_due()                     # 下一轮补上 B
            assert [r["task_id"] for r in records] == [b["id"]]
        finally:
            schedule._run_entry = orig

        # 兜底失败记录：_execute_run 抛非 OSError 异常也要落盘可查
        items = schedule.list_entries()
        for it in items:
            it["next_run"] = time.time() - 1
        schedule._save(items)
        orig_exec = schedule._execute_run
        schedule._execute_run = lambda entry, model: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            records = schedule.run_due()
        finally:
            schedule._execute_run = orig_exec
        assert all(r["status"] == "failed" for r in records)
        saved = {r["run_id"] for r in schedule.list_runs()}
        assert {r["run_id"] for r in records} <= saved
        assert any("boom" in (r.get("error") or "") for r in schedule.list_runs())
    finally:
        _clean_schedules()


def test_schedule_runs_prefix_after_removal_and_sleep_backoff():
    """任务删除后仍能按 id 前缀查历史；到期未执行时守护进程退避而不是 1 秒空转。"""
    from agent import schedule
    _clean_schedules()
    try:
        entry = schedule.add("巡检", every_seconds=60)
        items = schedule.list_entries()
        items[0]["next_run"] = time.time() - 1
        schedule._save(items)
        orig = schedule._run_entry
        schedule._run_entry = lambda e, m, **kw: "结论：正常"
        try:
            rec = schedule.run_due()[-1]
        finally:
            schedule._run_entry = orig
        schedule.remove(entry["id"])
        assert schedule.format_runs(task_id=entry["id"], prefix=True).count(rec["run_id"]) == 1
        # 到期未推进（模拟写盘失败）：睡眠退避 ≥ 30s
        items = schedule.list_entries()
        if not items:
            schedule.add("巡检2", every_seconds=60)
            items = schedule.list_entries()
        items[0]["next_run"] = time.time() - 100
        schedule._save(items)
        assert schedule._sleep_seconds() >= 30
    finally:
        _clean_schedules()


def test_schedule_report_path_requires_existing_file():
    """报告路径必须是 reports/ 下真实存在的文件（含空格的路径也要能识别）。"""
    from agent import schedule
    _clean_schedules()
    entry = schedule.add("巡检", every_seconds=60)
    reports = core.STATE_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    with_space = reports / "a b.md"
    with_space.write_text("x", encoding="utf-8")
    try:
        rec = schedule._finish_run(schedule._begin_run(entry, None), entry, "succeeded",
                                   verdict="正常", result_text=f"[全量报告] {with_space}")
        assert rec["report_path"] == str(with_space.resolve())  # 空格不被截断
        missing = schedule._finish_run(
            schedule._begin_run(entry, None), entry, "succeeded", verdict="正常",
            result_text=f"[全量报告] {reports}/does-not-exist.md")
        assert missing["report_path"] is None                   # 不存在的路径不采信
    finally:
        _clean_schedules()


def test_schedule_verdict_ignores_quoted_old_reports():
    """代码围栏/引用块里粘贴的旧结论不能翻转本轮判定；整行加粗仍可识别。"""
    from agent import schedule
    assert schedule._detect_verdict("**结论：正常**") == "正常"
    assert schedule._detect_verdict("  结论：异常") == "异常"
    text = ("检查完成\n结论：异常\n\n上次报告记录：\n```\n结论: **正常**｜完整性: 完整\n```")
    assert schedule._detect_verdict(text) == "异常"
    quoted = "结论：异常\n> 旧报告：\n> 结论：正常"
    assert schedule._detect_verdict(quoted) == "异常"


def test_schedule_interval_accepts_integral_float():
    """60.0/"60" 可归一化；60.5/59/bool 仍拒绝（不因小类型问题永久停用任务）。"""
    from agent import schedule
    assert schedule._interval(60) == 60
    assert schedule._interval("60") == 60
    assert schedule._interval(60.0) == 60
    for bad in (60.5, 59, True, "60s", None):
        try:
            schedule._interval(bad)
            raise AssertionError(f"应拒绝: {bad!r}")
        except schedule.ScheduleError:
            pass


def _clean_notify():
    """清掉通知/信号/事件状态：通知测试与调度测试互不污染。

    事件也要清：否则"有没有写事件"的断言可能被前一个用例残留的事件蒙对。
    """
    import shutil
    for name in ("notify.json", "notify-state.json", "notify-config-error.ts",
                 "events.jsonl", "events.jsonl.1"):
        (core.STATE_DIR / name).unlink(missing_ok=True)
    for sub in ("notifications", "signals"):
        shutil.rmtree(core.STATE_DIR / sub, ignore_errors=True)


def _notify_setup(**over):
    from agent import notify
    payload = {"channel": "webhook",
               "url": "https://hooks.example.com/services/T/B/SECRET",
               "format": "generic"}
    payload.update(over)
    return notify.save_config(payload)


class _FakePost:
    """替身 _post：记录载荷，可按需失败。不发真实请求（自检不联网）。"""

    def __init__(self, status=200, body="ok", error=None):
        self.calls, self.status, self.body, self.error = [], status, body, error

    def __call__(self, url, payload, timeout):
        if self.error:
            from agent import notify
            raise notify.NotifyError(self.error)
        self.calls.append(payload)
        return self.status, self.body


def _payload_text(payload: dict) -> str:
    """四种 webhook 载荷里取纯文本（钉钉的 text 是对象，不是字符串）。"""
    text = payload.get("text")
    if isinstance(text, str):
        return text
    if isinstance(text, dict):
        return text.get("content") or ""
    return (payload.get("content") or {}).get("text") or ""


def _anomaly(**over):
    item = {"id": "web1/resources/磁盘 /data", "target": "web1", "name": "磁盘 /data",
            "status": "critical", "line": "[critical] 磁盘 /data：/data 已用 96.0%",
            "command": "df -P -k -- /data", "output": "/dev/vdb 1000 960 40 96% /data"}
    item.update(over)
    return item


def _report_with(anomalies, complete=True, verdict=None):
    if verdict is None:
        verdict = ("异常" if any(a["status"] == "critical" for a in anomalies)
                   else ("警告" if anomalies else "正常"))
    targets = {}
    for a in anomalies:
        targets.setdefault(a["target"], {"name": a["target"], "label": a["target"],
                                         "checks": [], "verdict": "异常",
                                         "reachable": True, "platform": "Linux"})
        targets[a["target"]]["checks"].append({
            "id": a["id"], "target": a["target"], "group": "resources",
            "name": a["name"], "status": a["status"], "display": a["status"],
            "value": 96.0, "unit": "%", "threshold": "警告 85% / 异常 95%",
            "detail": a["line"], "sampled_at": "2026-10-22 10:00:00",
            "evidence": {"command": a.get("command"), "exit": 0, "output": a.get("output")}})
    # 与 checks.run_checks 的真实报告同形：format_report 会读这些字段
    return {"report_id": "R-test", "generated_at": "2026-10-22 10:00:00",
            "duration_ms": 12, "mode": "rules-only",
            "verdict": verdict, "complete": complete,
            "groups": ["connectivity", "resources", "services"],
            "coverage": {"groups_skipped": [], "targets_skipped": [],
                         "groups_requested": ["connectivity", "resources", "services"],
                         "targets_requested": ["all"]},
            "target_names": sorted(targets) or ["web1"],
            "summary": {"targets": len(targets) or 1, "checks": 6,
                        "ok": max(0, 6 - len(anomalies)),
                        "warn": 0, "critical": len(anomalies), "unknown": 0, "error": 0},
            "targets": list(targets.values()), "anomalies": anomalies}


def _rec_with_signal(report, status="succeeded", verdict=None, session="sess-sig-1"):
    """构造一条与真实运行记录同形的记录（含 checks 信号落盘）。"""
    from agent import checks
    path = checks.save_signal(session, report, None)
    return {"run_id": "run-1", "task_id": "sch-t1", "session_id": session,
            "status": status, "verdict": verdict if verdict is not None else report["verdict"],
            "signal_path": str(path) if path else None,
            "rules_verdict": report.get("verdict"),
            "rules_complete": report.get("complete"),
            "anomalies": [{"id": a["id"], "target": a["target"], "name": a["name"],
                           "status": a["status"], "line": a["line"],
                           "command": a.get("command"), "output": a.get("output")}
                          for a in report.get("anomalies") or []]}


def test_notify_config_validation():
    """配置是凭据入口：只允许 https（本机回环可 http）、拒绝内嵌凭据与未知字段。"""
    from agent import notify
    _clean_notify()
    cases = [
        ({"url": "http://example.com/hook"}, "https"),
        ({"url": "ftp://example.com/x"}, "https"),
        ({"url": "https://u:p@example.com/x"}, "用户名"),
        ({"url": "https://example.com/a b"}, "空白"),
        ({"url": ""}, "url"),
        ({"format": "telegram"}, "format"),
        ({"timeout_seconds": 60}, "timeout_seconds"),
        ({"renotify_after": 10}, "renotify_after"),
        ({"quiet_hours": "25:00-07:00"}, "quiet_hours"),
        ({"quiet_hours": "08:00-08:00"}, "不能相同"),
        ({"enabled": "yes"}, "enabled"),
        ({"webhook": "x"}, "未知配置字段"),
        ({"channel": "email"}, "channel"),
    ]
    for over, needle in cases:
        payload = {"channel": "webhook", "url": "https://hooks.example.com/x"}
        payload.update(over)
        try:
            notify.save_config(payload)
            raise AssertionError(f"应拒绝: {over}")
        except notify.NotifyError as e:
            assert needle in str(e), (over, e)
    cfg = _notify_setup()
    path = core.STATE_DIR / "notify.json"
    assert oct(path.stat().st_mode)[-3:] == "600"
    shown = notify.describe_config(cfg)
    assert "SECRET" not in shown and "hooks.example.com" in shown
    assert notify.redact_endpoint("https://h.example.com:8443/a/b?x=1") == \
        "https://h.example.com:8443/…"
    assert notify.describe_config({}).startswith("未配置")
    _clean_notify()


def test_notify_payload_formats():
    """四种 webhook 载荷形状 + 文本里必须带证据命令。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        for fmt, check in (
                ("slack", lambda p: set(p) == {"text"}),
                ("feishu", lambda p: p["msg_type"] == "text" and "text" in p["content"]),
                ("dingtalk", lambda p: p["msgtype"] == "text" and "content" in p["text"]),
                ("generic", lambda p: p["event"] == "ops-agent" and p["run_id"] == "run-1"
                                      and p["items"] and p["severity"] == "critical")):
            _clean_notify()
            _notify_setup(format=fmt)
            out = notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
            assert out["notification"]["status"] == "sent", (fmt, out)
            payload = sent.calls[-1]
            assert check(payload), (fmt, payload)
            text = _payload_text(payload)
            assert "[异常]" in text and "df -P -k -- /data" in text, (fmt, text)
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_dedup_and_reminder():
    """同一持续异常只提醒一次；到提醒间隔才再发一条（不刷屏也不静默）。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup(renotify_after=300)
        rec = _rec_with_signal(_report_with([_anomaly()]))
        first = notify.process_run(rec)
        assert first["actions"]["new"] == 1 and len(sent.calls) == 1
        again = notify.process_run(rec)
        assert again["actions"]["suppressed"] == 1 and again["notification"] is None
        assert len(sent.calls) == 1, "同一异常不应重复发送"
        state = notify._load_state()
        for entry in state["items"].values():
            entry["notified_at"] = time.time() - 400
        notify._save_state(state)
        third = notify.process_run(rec)
        assert third["actions"]["reminder"] == 1 and len(sent.calls) == 2
        assert third["notification"]["title"].startswith("巡检")
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_escalation_is_sent():
    """级别升级（警告 → 异常）必须立即发，不能被去重窗口吞掉。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        warn = _anomaly(status="warn", line="[warn] 磁盘 /data：已用 88%")
        assert notify.process_run(_rec_with_signal(_report_with([warn])))["actions"]["new"] == 1
        assert len(sent.calls) == 1
        crit = _anomaly(status="critical")
        out = notify.process_run(_rec_with_signal(_report_with([crit])))
        assert out["actions"]["escalated"] == 1 and len(sent.calls) == 2
        assert "[异常]" in sent.calls[-1]["text"]
        assert out["notification"]["severity"] == "critical"
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_recovery_only_when_complete():
    """恢复通知只在"采集完整且该项不再异常"时发；不完整不能当作已恢复。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        assert len(sent.calls) == 1
        # 采集不完整：即使这次没看到异常项，也不能断言恢复。
        # （"解读不完整"本身会作为一条任务级警告提醒一次——这是有意的降级信号。）
        incomplete = _rec_with_signal(_report_with([], complete=False, verdict="不完整"),
                                      verdict="不完整")
        out_incomplete = notify.process_run(incomplete)
        assert out_incomplete["actions"]["recovered"] == 0
        items = (out_incomplete["notification"] or {}).get("items") or []
        assert not any(i["fingerprint"].startswith("check:") for i in items), items
        state = notify._load_state()
        assert state["items"]["check:web1/resources/磁盘 /data"]["severity"] == "critical"
        sent_before = len(sent.calls)
        # 完整且干净：发恢复通知
        ok = _rec_with_signal(_report_with([], complete=True, verdict="正常"), verdict="正常")
        out = notify.process_run(ok)
        # 两条历史项都该恢复：规则检查项 + 上一步"解读不完整"的任务级项
        recovered = {r["fingerprint"] for r in out["notification"]["recoveries"]}
        assert recovered == {"check:web1/resources/磁盘 /data", "task:sch-t1"}, recovered
        assert out["notification"]["severity"] == "info"
        assert "恢复" in sent.calls[-1]["text"] and len(sent.calls) == sent_before + 1
        # 再跑一次不应重复发恢复
        assert notify.process_run(ok)["notification"] is None
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_mute_suppresses_then_rearms():
    """静默期内不发也不丢状态；解除静默后下一次巡检会重新提醒。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup(mute_until=time.time() + 3600)
        rec = _rec_with_signal(_report_with([_anomaly()]))
        out = notify.process_run(rec)
        assert out["notification"]["status"] == "muted"
        assert out["notification"]["muted_reason"] == "muted"
        assert sent.calls == [], "静默期不能真的发出去"
        assert len(notify.list_notifications(status="muted")) == 1
        # 同一批异常静默期内只记一条 muted 记录
        notify.process_run(rec)
        assert len(notify.list_notifications(status="muted")) == 1
        cfg = notify.load_config()
        cfg["mute_until"] = 0
        notify.save_config({k: cfg[k] for k in notify.CONFIG_FIELDS})
        again = notify.process_run(rec)
        assert again["notification"]["status"] == "sent" and len(sent.calls) == 1
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_quiet_hours_holds_warning_not_critical():
    """静默时段只压警告；异常仍要能把人叫起来（否则静默等于关监控）。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    lt = time.localtime()
    start, end = (lt.tm_hour - 1) % 24, (lt.tm_hour + 1) % 24
    quiet = f"{start:02d}:00-{end:02d}:00"
    try:
        _clean_notify()
        _notify_setup(quiet_hours=quiet)
        warn = _anomaly(status="warn")
        out = notify.process_run(_rec_with_signal(_report_with([warn])))
        assert out["notification"]["status"] == "muted"
        assert out["notification"]["muted_reason"] == "quiet_hours"
        assert sent.calls == []
        crit = _anomaly(id="web1/resources/内存", name="内存")
        out2 = notify.process_run(_rec_with_signal(_report_with([crit])))
        assert out2["notification"]["status"] == "sent" and len(sent.calls) == 1
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_failure_recorded_and_retried():
    """发送失败要落库可查、按退避重试，重试成功后状态变 sent。"""
    from agent import notify
    orig = notify._post
    notify._post = _FakePost(error="Connection refused")
    try:
        _clean_notify()
        _notify_setup()
        rec = _rec_with_signal(_report_with([_anomaly()]))
        out = notify.process_run(rec)
        notif = out["notification"]
        assert notif["status"] == "failed" and notif["attempts"] == 1
        assert "Connection refused" in notif["last_error"]
        assert notif["next_attempt_ts"] > time.time() + 30, "失败后要退避，不能立即重试风暴"
        assert notify.retry_pending() == [], "未到退避时间不应重试"
        assert "failed" in notify.format_notifications()
        assert notify.status()["pending"] == 1
        pending = notify.list_notifications(status="failed")[0]
        pending["next_attempt_ts"] = 0
        notify._write_notification(pending)
        notify._post = _FakePost()
        done = notify.retry_pending(limit=5)
        assert [d["status"] for d in done] == ["sent"] and done[0]["attempts"] == 2
        assert notify.list_notifications(status="sent")
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_one_message_per_run():
    """一次运行多个新异常 → 一条消息（刷屏的主要来源是每条异常发一次）。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        anomalies = [_anomaly(id=f"web{i}/resources/磁盘 /data", target=f"web{i}",
                              name=f"磁盘 /data{i}") for i in range(3)]
        out = notify.process_run(_rec_with_signal(_report_with(anomalies)))
        assert len(sent.calls) == 1
        text = sent.calls[0]["text"]
        assert text.count("[异常]") == 3 and "共 3 项" not in text
        assert out["notification"]["severity"] == "critical"
        assert len(out["notification"]["items"]) == 3
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_unconfigured_keeps_events():
    """没配渠道也要留事件：通知是出口，不是唯一的记录。"""
    from agent import events, notify
    _clean_notify()
    out = notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
    assert out["notification"] is None and out["actions"]["new"] == 1
    assert notify.list_notifications() == []
    assert any(r["kind"] == "anomaly-new" for r in events.tail(10))
    _clean_notify()


def test_notify_endpoint_not_leaked():
    """webhook 的 path 就是凭据：事件与发件箱只能有脱敏后的 endpoint。"""
    from agent import events, notify
    orig = notify._post
    notify._post = _FakePost()
    try:
        _clean_notify()
        _notify_setup()
        notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        outbox = "\n".join(p.read_text(encoding="utf-8")
                           for p in (core.STATE_DIR / "notifications").glob("*.json"))
        timeline = (core.STATE_DIR / "events.jsonl").read_text(encoding="utf-8")
        assert "SECRET" not in outbox and "SECRET" not in timeline
        assert notify.list_notifications()[0]["endpoint"] == "https://hooks.example.com/…"
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_cli_setup_status_and_errors():
    """CLI 输出不能回显凭据；配置错误按 1 退出；test 子命令要真的走发送路径。"""
    from agent import notify
    _clean_notify()
    orig = notify._post
    notify._post = _FakePost()
    saved_out, saved_err = sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    try:
        sys.stdout, sys.stderr = out, err
        codes = [
            notify.cli(["setup", "--url", "https://hooks.example.com/services/T/B/SECRET",
                        "--format", "slack"]),
            notify.cli(["status"]),
            notify.cli(["test"]),
            notify.cli(["mute", "--for", "2h"]),
            notify.cli(["list"]),
        ]
        bad = notify.cli(["setup", "--url", "http://example.com/hook"])
        too_long = notify.cli(["mute", "--for", "9999d"])
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        notify._post = orig
    text = out.getvalue()
    assert codes == [0, 0, 0, 0, 0], codes
    assert "SECRET" not in text and "hooks.example.com" in text
    assert "已发送" in text and "静默至" in text
    assert bad == 1 and "https" in err.getvalue()
    assert too_long == 1, "静默时长要有上限"
    assert notify.list_notifications(status="sent"), "test 通知应落发件箱"
    _clean_notify()


def test_events_timeline_append_tail_and_rotate():
    """时间线：倒序 tail、按类型过滤、坏行跳过、0600、按大小轮转。"""
    from agent import events
    for name in ("events.jsonl", "events.jsonl.1"):
        (core.STATE_DIR / name).unlink(missing_ok=True)
    assert events.append("anomaly-new", title="磁盘 96%", task="sch-1")
    events.append("notify-sent", title="已发送")
    assert [r["kind"] for r in events.tail(2)] == ["notify-sent", "anomaly-new"]
    assert len(events.tail(10, kind="anomaly-new")) == 1
    assert "磁盘 96%" in events.format_tail(2)
    with open(core.STATE_DIR / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    assert len(events.tail(5)) == 2          # 坏行被跳过，不影响已有事件
    assert oct((core.STATE_DIR / "events.jsonl").stat().st_mode)[-3:] == "600"
    orig = events.MAX_BYTES
    events.MAX_BYTES = 1
    try:
        events.append("anomaly-recovered", title="恢复")
    finally:
        events.MAX_BYTES = orig
    assert (core.STATE_DIR / "events.jsonl.1").is_file()
    try:
        events.append("Bad Kind!", title="x")
        raise AssertionError("非法事件类型应拒绝")
    except ValueError:
        pass


def test_notify_state_pruned_but_open_kept():
    """恢复/沉默超过 30 天的 fingerprint 清掉；仍异常的无论多久都保留。"""
    from agent import notify
    now = time.time()
    state = {"version": 1, "items": {
        "check:old-recovered": {"severity": None, "last_seen": now - 40 * 86400},
        "check:still-bad": {"severity": "critical", "last_seen": now - 40 * 86400},
        "check:fresh": {"severity": None, "last_seen": now},
        "check:garbage": "not-a-dict",
    }}
    notify._prune_state(state, now)
    assert set(state["items"]) == {"check:still-bad", "check:fresh"}


def test_notify_duration_and_quiet_hours():
    from agent import notify
    assert notify.parse_duration("30m") == 1800
    assert notify.parse_duration("2h") == 7200
    assert notify.parse_duration("7d") == 604800
    assert notify.parse_duration("900s") == 900
    for bad in ("0m", "2h30m", "", "-1h", None):
        try:
            notify.parse_duration(bad)
            raise AssertionError(f"应拒绝: {bad!r}")
        except notify.NotifyError:
            pass
    assert notify.in_quiet_hours(None, time.time()) is False
    lt = time.localtime()
    night = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 30, 0, 0, 0, -1))
    noon = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))
    assert notify.in_quiet_hours("23:00-07:00", night) is True
    assert notify.in_quiet_hours("23:00-07:00", noon) is False
    assert notify.in_quiet_hours("09:00-18:00", noon) is True


def test_checks_signal_roundtrip_and_validation():
    """信号按会话落盘（0600），会话/版本不匹配一律拒绝。"""
    from agent import checks
    _clean_notify()
    assert checks.save_signal("bad id!", _report_with([])) is None
    assert checks.save_signal(None, _report_with([])) is None
    assert checks.load_signal(None) is None
    path = checks.save_signal("sess-sig-1", _report_with([_anomaly()]), "/tmp/report.md")
    assert path and oct(path.stat().st_mode)[-3:] == "600"
    signal = checks.load_signal("sess-sig-1")
    assert signal["complete"] is True and signal["report_path"] == "/tmp/report.md"
    assert signal["anomalies"][0]["command"] == "df -P -k -- /data"
    assert "96%" in signal["anomalies"][0]["output"]
    path.write_text(json.dumps(dict(signal, session_id="other")), encoding="utf-8")
    assert checks.load_signal("sess-sig-1") is None
    path.write_text(json.dumps(dict(signal, version=2)), encoding="utf-8")
    assert checks.load_signal("sess-sig-1") is None
    path.write_text("{broken", encoding="utf-8")
    assert checks.load_signal("sess-sig-1") is None
    _clean_notify()


def test_notify_llm_down_reports_rule_anomalies():
    """验收核心：LLM 从第一轮就不可用，规则检查（先于模型运行）的异常仍能发出去。

    这里刻意让 _run_entry 在任何工具调用之前就抛错——不依赖模型先成功调用 inspect。
    """
    from agent import checks, notify, schedule
    _clean_notify()
    _clean_schedules()
    sent = _FakePost()
    report = _report_with([_anomaly()])
    orig = (notify._post, schedule._run_entry, checks.run_checks)

    def fake_checks(targets=None, groups=None, timeout=None, deadline=None):
        return report

    def fake_entry(entry, model, session=None, deadline=None, extra=""):
        assert "规则检查" in extra, "模型之前必须先跑规则检查"
        raise ConnectionError("LLM 401 invalid api key: sk-live-ABCDEFGHIJKLMNOPQRST")

    notify._post = sent
    schedule._run_entry = fake_entry
    checks.run_checks = fake_checks
    try:
        _notify_setup()
        schedule.add("每日巡检", every_seconds=60, checks_target="all")
        items = schedule.list_entries()
        items[0]["next_run"] = time.time() - 1
        schedule._save(items)
        records = schedule.run_due()
    finally:
        notify._post, schedule._run_entry, checks.run_checks = orig
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "failed", rec["status"]
    assert len(rec["anomalies"]) == 1 and rec["signal_path"]
    assert rec["notify_status"] == "sent" and rec["notification_id"]
    text = sent.calls[0]["text"]
    assert "df -P -k -- /data" in text and "web1 磁盘 /data" in text
    assert "sk-live" not in text and "[已脱敏]" in text
    assert rec["session_id"] in text
    assert "通知:" in schedule.format_runs()
    assert "规则检查 all" in schedule.format_list()
    # 即使运行记录被截断/进程被杀，也要能按会话取回完整摘要
    signal = notify._signal_from_record(rec)
    assert signal and signal["anomalies"], signal
    _clean_schedules()
    _clean_notify()


def test_notify_missed_and_interrupted_are_notified():
    """错过与中断是"没人在看"的盲区，必须能推到人。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        missed = notify.process_run({"run_id": "r-m", "task_id": "sch-m", "status": "missed",
                                     "error": "遗忘 5 个应执行时点"})
        assert missed["notification"]["severity"] == "warning"
        assert "错过" in sent.calls[-1]["text"]
        interrupted = notify.process_run({"run_id": "r-i", "task_id": "sch-i",
                                          "status": "interrupted", "error": "进程退出"})
        assert interrupted["notification"]["severity"] == "critical"
        assert "中断" in sent.calls[-1]["text"]
    finally:
        notify._post = orig
        _clean_notify()


def test_events_tool_is_readonly_and_bounded():
    """events 是只读工具（无人值守可见）；模型没有发通知的工具，避免被告警刷屏。"""
    from agent import tools as agent_tools
    ctx = agent_tools.ToolContext(allowed=agent_tools.SCHEDULED_TOOLS)
    out = agent_tools.dispatch("events", {"tail": 3}, ctx)
    assert isinstance(out, str) and out
    assert "[错误]" in agent_tools.dispatch("events", {"tail": -1}, ctx)
    assert "[错误]" in agent_tools.dispatch("events", {"kind": "Bad Kind"}, ctx)
    names = {s["function"]["name"] for s in agent_tools.tool_schemas(ctx)}
    assert "events" in names and "notify" not in names


def test_notify_subset_target_cannot_recover_other_target():
    """只查 web1 的报告不能证明 db1 恢复：否则会误报"已恢复"，比漏报更危险。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        db = _anomaly(id="db1/resources/磁盘 /data", target="db1", name="磁盘 /data")
        notify.process_run(_rec_with_signal(_report_with([db])))
        assert len(sent.calls) == 1
        # 另一个目标（web1）检查完整且干净，但根本没查 db1
        web_ok = _report_with([], complete=True, verdict="正常")
        web_ok["target_names"] = ["web1"]
        web_ok["targets"] = [{"name": "web1", "label": "web1", "checks": [],
                              "verdict": "正常", "reachable": True, "platform": "Linux"}]
        out = notify.process_run(_rec_with_signal(web_ok, session="sess-web1",
                                                  verdict="正常"))
        assert out["actions"]["recovered"] == 0, out["actions"]
        assert out["notification"] is None
        state = notify._load_state()
        assert state["items"]["check:db1/resources/磁盘 /data"]["severity"] == "critical"
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_truncated_anomalies_never_recover():
    """>20 条异常时运行记录只留前 20 条，但恢复判定必须用完整摘要；
    摘要文件没了（truncated）就禁止判定恢复。"""
    from agent import checks, schedule, notify
    _clean_notify()
    anomalies = [_anomaly(id=f"web{i}/resources/磁盘 /x{i}", target=f"web{i}",
                          name=f"磁盘 /x{i}") for i in range(25)]
    report = _report_with(anomalies)
    session = "sess-many-1"
    path = checks.save_signal(session, report, None)
    assert path
    rec = {"run_id": "run-many", "task_id": "sch-many", "session_id": session,
           "status": "succeeded", "verdict": "异常", "signal_path": str(path),
           "rules_verdict": "异常", "rules_complete": True,
           "rules_targets": report["target_names"]}
    schedule._attach_signal(rec, checks.load_signal(session))
    assert len(rec["anomalies"]) == 20 and rec["rules_truncated"] is True
    # 第 25 条必须仍在 current 里（判定用完整 signal，不用紧凑副本）
    current = notify._current_items(rec, notify._signal_from_record(rec))
    assert f"check:{anomalies[-1]['id']}" in current
    # 摘要文件缺失 → truncated，且不能据此判恢复
    session2 = "sess-many-2"
    path2 = checks.save_signal(session2, report, None)
    rec2 = dict(rec, session_id=session2, signal_path=str(path2))
    schedule._attach_signal(rec2, checks.load_signal(session2))
    path2.unlink()
    signal = notify._signal_from_record(rec2)
    assert signal["truncated"] is True
    assert notify._recoverable("check:web1/resources/磁盘 /x1", signal, rec2) is False
    _clean_notify()


def test_notify_retry_respects_mute_and_skips_recovered():
    """静默期不补发旧告警；已恢复的旧告警标记 superseded，避免"先恢复又告警"。"""
    from agent import notify
    orig = notify._post
    notify._post = _FakePost(error="Connection refused")
    try:
        _clean_notify()
        _notify_setup()
        rec = _rec_with_signal(_report_with([_anomaly()]))
        notify.process_run(rec)
        failed = notify.list_notifications(status="failed")[0]
        failed["next_attempt_ts"] = 0
        notify._write_notification(failed)
        # 静默期：不补发
        cfg = notify.load_config()
        cfg["mute_until"] = time.time() + 600
        notify.save_config({k: cfg[k] for k in notify.CONFIG_FIELDS})
        done = notify.retry_pending(limit=5)
        assert [d["status"] for d in done] == ["muted"], done
        # 解除静默后，一条"所属异常已恢复"的失败通知不该再补发（会变成先恢复又告警）。
        cfg["mute_until"] = 0
        notify.save_config({k: cfg[k] for k in notify.CONFIG_FIELDS})
        stale = {
            "id": "20261022-000000-ntf-stale1", "created": time.time() - 60,
            "created_iso": "2026-10-22 10:00:00", "kind": "anomaly",
            "severity": "critical", "title": "巡检告警 1 项",
            "text": "[ops-agent] 巡检告警 1 项\n旧消息", "items": [], "recoveries": [],
            "task_id": "sch-t1", "run_id": "run-old", "session_id": "sess-old",
            "report_path": None, "fingerprints": ["check:web1/resources/内存"],
            "endpoint": "https://hooks.example.com/…", "status": "failed",
            "attempts": 1, "last_error": "Connection refused", "next_attempt_ts": 0,
            "http_status": None,
        }
        notify._write_notification(stale)
        state = notify._load_state()
        state["items"]["check:web1/resources/内存"] = {"severity": None,
                                                      "last_seen": time.time()}
        notify._save_state(state)
        notify._post = _FakePost()
        done = notify.retry_pending(limit=5)
        assert [d["status"] for d in done] == ["superseded"], done
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_2xx_error_body_is_failure():
    """钉钉/飞书用 200 + errcode 表示失败：不能记成 sent。"""
    from agent import notify
    orig = notify._post
    try:
        _clean_notify()
        _notify_setup(format="dingtalk")
        notify._post = lambda url, payload, timeout: (
            200, '{"errcode": 310000, "errmsg": "keywords not in content"}')
        out = notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        assert out["notification"]["status"] == "failed"
        assert "errcode=310000" in out["notification"]["last_error"]
        _clean_notify()
        _notify_setup(format="feishu")
        notify._post = lambda url, payload, timeout: (200, '{"code": 19001, "msg": "bad token"}')
        out = notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        assert out["notification"]["status"] == "failed"
        assert "code=19001" in out["notification"]["last_error"]
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_url_rejects_bad_port_and_control_chars():
    from agent import notify
    _clean_notify()
    for url, needle in (
            ("https://h.example.com:99999/x", "端口"),
            ("https://h.example.com:abc/x", "端口"),
            ("https://h.example.com/x\x01y", "控制字符"),
            ("https://h.example.com/x\nY", "控制字符")):
        try:
            notify.save_config({"channel": "webhook", "url": url})
            raise AssertionError(f"应拒绝: {url!r}")
        except notify.NotifyError as e:
            assert needle in str(e), (url, e)
    # 坏端口的历史配置不能让 status/describe 崩溃
    assert "非法端口" in notify.redact_endpoint("https://h.example.com:99999/x")
    _clean_notify()


def test_notify_quiet_hours_equal_minutes_rejected():
    """8:00-08:00 与 08:00-08:00 等价，都必须拒绝（否则等于全天静默）。"""
    from agent import notify
    _clean_notify()
    for quiet in ("08:00-08:00", "8:00-08:00", "23:00-23:00"):
        try:
            notify.save_config({"channel": "webhook",
                                "url": "https://hooks.example.com/x",
                                "quiet_hours": quiet})
            raise AssertionError(f"应拒绝: {quiet}")
        except notify.NotifyError as e:
            assert "不能相同" in str(e), (quiet, e)
    _clean_notify()


def test_notify_recovery_held_while_muted_then_sent():
    """静默期不丢恢复：解除静默后补发一次"已恢复"。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        assert len(sent.calls) == 1
        cfg = notify.load_config()
        cfg["mute_until"] = time.time() + 600
        notify.save_config({k: cfg[k] for k in notify.CONFIG_FIELDS})
        ok = _rec_with_signal(_report_with([], complete=True, verdict="正常"), verdict="正常")
        muted = notify.process_run(ok)
        assert muted["notification"]["status"] == "muted"
        assert len(sent.calls) == 1
        assert notify._load_state()["items"]["check:web1/resources/磁盘 /data"]["severity"]             == "critical", "静默期不能先把状态清成已恢复"
        cfg["mute_until"] = 0
        notify.save_config({k: cfg[k] for k in notify.CONFIG_FIELDS})
        out = notify.process_run(ok)
        assert out["actions"]["recovered"] == 1 and out["notification"]["severity"] == "info"
        assert len(sent.calls) == 2 and "恢复" in sent.calls[-1]["text"]
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_missed_is_transient_not_recovered():
    """错过是一次性事件：发过即结束，不会紧接着再发一条"恢复"。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        notify.process_run({"run_id": "r-m", "task_id": "sch-m", "status": "missed",
                            "error": "遗忘 3 个应执行时点"})
        assert "错过" in sent.calls[-1]["text"]
        assert notify._load_state()["items"]["missed:sch-m"]["severity"] is None
        ok = {"run_id": "r-ok", "task_id": "sch-m", "status": "succeeded",
              "verdict": "正常"}
        out = notify.process_run(ok)
        assert out["actions"]["recovered"] == 0 and out["notification"] is None
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_setup_preserves_existing_settings():
    """setup 只改显式传入的字段；enable/disable 不该顺带解除静默。"""
    from agent import notify
    _clean_notify()
    notify.save_config({"channel": "webhook", "url": "https://hooks.example.com/a",
                        "format": "slack", "renotify_after": 1800,
                        "quiet_hours": "23:00-07:00", "enabled": False,
                        "mute_until": time.time() + 600})
    saved = io.StringIO()
    old = sys.stdout
    sys.stdout = saved
    try:
        assert notify.cli(["setup", "--url", "https://hooks.example.com/b"]) == 0
        assert notify.cli(["disable"]) == 0
    finally:
        sys.stdout = old
    cfg = notify.load_config()
    assert cfg["format"] == "slack" and cfg["renotify_after"] == 1800
    assert cfg["quiet_hours"] == "23:00-07:00"
    assert cfg["enabled"] is False
    assert cfg["mute_until"] > time.time(), "disable 不该解除静默"
    out = saved.getvalue()
    assert "hooks.example.com" in out and "notify.json" in out
    sys.stdout = old
    saved2 = io.StringIO()
    sys.stdout = saved2
    try:
        assert notify.cli(["setup", "--url", "https://hooks.example.com/b",
                           "--no-quiet-hours"]) == 0
    finally:
        sys.stdout = old
    assert notify.load_config()["quiet_hours"] is None
    _clean_notify()


def test_schedule_running_record_has_session_before_model():
    """运行记录在调用模型前就带 session_id：进程被 kill 后仍能找回规则摘要。"""
    from agent import checks, schedule
    _clean_notify()
    _clean_schedules()
    seen = {}
    orig = schedule._run_entry

    def fake(entry, model, session=None, deadline=None, extra=""):
        recs = list((core.STATE_DIR / "runs").glob("*.json"))
        seen["records"] = [json.loads(p.read_text(encoding="utf-8")) for p in recs]
        seen["session"] = session.id
        raise ConnectionError("boom")

    schedule._run_entry = fake
    try:
        entry = schedule.add("巡检", every_seconds=60)
        schedule._execute_run(entry, None)
    finally:
        schedule._run_entry = orig
    assert seen["records"], "调用模型前必须已写 running 记录"
    assert any(r.get("session_id") == seen["session"] and r["status"] == "running"
               for r in seen["records"]), seen["records"]
    _clean_schedules()
    _clean_notify()


def test_events_tail_reads_rotated_file():
    """轮转后仍能查到 .1 里的历史，否则"最近发生了什么"会突然断档。"""
    from agent import events
    for name in ("events.jsonl", "events.jsonl.1"):
        (core.STATE_DIR / name).unlink(missing_ok=True)
    events.append("anomaly-new", title="旧事件")
    orig = events.MAX_BYTES
    events.MAX_BYTES = 1
    try:
        events.append("anomaly-new", title="新事件")
    finally:
        events.MAX_BYTES = orig
    assert (core.STATE_DIR / "events.jsonl.1").is_file()
    titles = [r.get("title") for r in events.tail(5)]
    assert "旧事件" in titles and "新事件" in titles, titles


def test_notify_unconfigured_events_do_not_flood():
    """没配渠道时持续异常也只记一次事件，不能每轮巡检刷一条。"""
    from agent import events, notify
    _clean_notify()
    for name in ("events.jsonl", "events.jsonl.1"):
        (core.STATE_DIR / name).unlink(missing_ok=True)
    rec = _rec_with_signal(_report_with([_anomaly()]))
    for _ in range(3):
        notify.process_run(rec)
    rows = [r for r in events.tail(50) if r["kind"] == "anomaly-new"]
    assert len(rows) == 1, rows
    _clean_notify()


def test_notify_muted_events_and_records_deduped():
    """静默期同样不重复写 anomaly-new，也不重复写 muted 记录。"""
    from agent import events, notify
    orig = notify._post
    notify._post = _FakePost()
    try:
        _clean_notify()
        for name in ("events.jsonl", "events.jsonl.1"):
            (core.STATE_DIR / name).unlink(missing_ok=True)
        _notify_setup(mute_until=time.time() + 3600)
        rec = _rec_with_signal(_report_with([_anomaly()]))
        for _ in range(3):
            notify.process_run(rec)
        assert len([r for r in events.tail(50) if r["kind"] == "anomaly-new"]) == 1
        assert len(notify.list_notifications(status="muted")) == 1
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_critical_to_unreachable_is_an_alert():
    """磁盘 98% → 探针超时：这是"测不到了"，必须提醒，不能被去重压掉。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        assert len(sent.calls) == 1
        unknown = _anomaly(status="unknown", line="[unknown] 磁盘 /data：df 执行失败（exit=255）")
        out = notify.process_run(_rec_with_signal(_report_with([unknown])))
        assert out["notification"]["status"] == "sent"
        assert "不可采集" in sent.calls[-1]["text"]
        assert len(sent.calls) == 2
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_message_keeps_report_path_when_truncated():
    """正文被截断时，报告路径与追溯命令必须仍在消息里（否则"完整清单见报告"无处可寻）。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        anomalies = [_anomaly(id=f"web{i}/resources/磁盘 /data{i}", target=f"web{i}",
                              name=f"磁盘 /data{i}",
                              line="x" * 400) for i in range(20)]
        rec = _rec_with_signal(_report_with(anomalies))
        rec["report_path"] = "/Users/example/.ops-agent/reports/20261022-inspect-abc.md"
        out = notify.process_run(rec)
        text = out["notification"]["text"]
        assert len(text) <= notify.TEXT_LIMIT + 200, len(text)
        assert "报告: /Users/example/.ops-agent/reports/20261022-inspect-abc.md" in text
        assert "追溯:" in text
        assert "已截断" in text, "20 条 × 400 字符必然超预算，应显式说明被截断"
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_signal_found_without_signal_path():
    """进程被 kill 时运行记录只有 session_id：仍要能按会话找回规则摘要。"""
    from agent import checks, notify
    _clean_notify()
    report = _report_with([_anomaly()])
    session = "sess-killed-1"
    path = checks.save_signal(session, report, None)
    assert path
    rec = {"run_id": "run-killed", "task_id": "sch-k", "session_id": session,
           "status": "interrupted", "started_ts": time.time(), "verdict": None}
    items = notify._current_items(rec, notify._signal_from_record(rec))
    assert "check:web1/resources/磁盘 /data" in items, items
    assert "run:sch-k" in items
    # 旧摘要（早于本次运行）不能被当成本次结论
    old = dict(rec, started_ts=time.time() + 3600)
    assert notify._signal_from_record(old) is None
    _clean_notify()


def test_schedule_pending_notify_survives_runs_query():
    """先 `schedule runs` 对账、后 daemon 启动：中断记录仍要能推给用户。"""
    from agent import notify, schedule
    _clean_notify()
    _clean_schedules()
    sent = _FakePost()
    orig = notify._post
    notify._post = sent
    try:
        _notify_setup()
        now = time.time()
        rec = {"run_id": "20261022-100000-sch-dead-abcdef", "task_id": "sch-dead",
               "prompt": "x", "session_id": None, "pid": 99999999, "status": "running",
               "verdict": None, "started": "2026-10-22 10:00:00", "started_ts": now,
               "finished": None, "finished_ts": None, "duration_s": None,
               "attempts": 0, "error": None, "result_preview": "",
               "report_path": None}
        (core.STATE_DIR / "runs").mkdir(parents=True, exist_ok=True)
        (core.STATE_DIR / "runs" / f"{rec['run_id']}.json").write_text(
            json.dumps(rec), encoding="utf-8")
        # 诊断时最常见的动作：先查 runs（会把 running 标成 interrupted）
        assert schedule.reconcile_runs() == 1
        assert schedule.list_runs()[0]["status"] == "interrupted"
        pending = schedule._pending_notify_runs()
        assert [r["run_id"] for r in pending] == [rec["run_id"]], pending
        schedule._notify_pending_runs()
        assert len(sent.calls) == 1 and "中断" in sent.calls[-1]["text"]
        # 已通知过的不再重复补发
        assert schedule._pending_notify_runs() == []
    finally:
        notify._post = orig
        _clean_schedules()
        _clean_notify()


def test_notify_state_corrupt_does_not_supersede():
    """状态文件损坏时不能把失败通知判成"已恢复"，否则告警被静默丢掉。"""
    from agent import notify
    orig = notify._post
    notify._post = _FakePost(error="Connection refused")
    try:
        _clean_notify()
        _notify_setup()
        notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        failed = notify.list_notifications(status="failed")[0]
        failed["next_attempt_ts"] = 0
        notify._write_notification(failed)
        notify._state_path().write_text("{broken", encoding="utf-8")
        notify._post = _FakePost()
        done = notify.retry_pending(limit=5)
        assert [d["status"] for d in done] == ["sent"], done
    finally:
        notify._post = orig
        _clean_notify()


def test_sensitive_state_dir_bypass_variants():
    """`~/.ops-agent/.`、`/*`、`n*` 都是同一目录：只按字符串前缀匹配会被绕过。"""
    for cmd in ("grep -r SECRET ~/.ops-agent/.",
                "grep -r SECRET ~/.ops-agent/*",
                "cat ~/.ops-agent/n*",
                "grep -r hook ~/.ops-agent"):
        assert core.sensitive_command(cmd), cmd
        assert core.check(cmd)[0] != 0, cmd
    # 报告与普通列目录仍可用（巡检报告必须能读）
    assert core.sensitive_command("cat ~/.ops-agent/reports/x.md") is None
    assert core.sensitive_command("ls -l ~/.ops-agent") is None
    assert core.sensitive_command("cat ~/.ops-agent/notify.json")


def test_schedule_checks_target_validated():
    """--checks 写错目标名要在 add 时拒绝，不能留下"看起来跑过规则检查"的任务。"""
    from agent import schedule
    _clean_schedules()
    try:
        schedule.add("巡检", every_seconds=60, checks_target="typo")
        raise AssertionError("未注册目标应被拒绝")
    except schedule.ScheduleError as e:
        assert "未注册" in str(e)
    try:
        schedule.add("巡检", every_seconds=60, checks_groups=["resources"])
        raise AssertionError("只给分组应被拒绝")
    except schedule.ScheduleError as e:
        assert "配合" in str(e)
    try:
        schedule.add("巡检", every_seconds=60, checks_target="local",
                     checks_groups=["nope"])
        raise AssertionError("未知分组应被拒绝")
    except schedule.ScheduleError as e:
        assert "未知检查分组" in str(e)
    entry = schedule.add("巡检", every_seconds=60, checks_target="local",
                         checks_groups=["resources"])
    assert entry["checks"] == {"targets": "local", "groups": ["resources"]}
    _clean_schedules()


def test_notify_recovery_retried_after_failure():
    """恢复消息发送失败时不能先清状态：下一轮要能补发，否则"恢复"永久丢失。"""
    from agent import notify
    orig = notify._post
    try:
        _clean_notify()
        _notify_setup()
        notify._post = _FakePost()
        notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        notify._post = _FakePost(error="Connection refused")
        ok = _rec_with_signal(_report_with([], complete=True, verdict="正常"), verdict="正常")
        failed = notify.process_run(ok)
        assert failed["notification"]["status"] == "failed"
        state = notify._load_state()
        assert state["items"]["check:web1/resources/磁盘 /data"]["severity"] == "critical", \
            "恢复消息没发出去就不能清成已恢复"
        assert state["items"]["check:web1/resources/磁盘 /data"].get("pending_recovery")
        notify._post = _FakePost()
        retried = notify.process_run(ok)
        assert retried["actions"]["recovered"] == 1
        assert retried["notification"]["severity"] == "info"
        assert notify._load_state()["items"]["check:web1/resources/磁盘 /data"][
            "severity"] is None
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_other_target_unreachable_does_not_block_recovery():
    """别的目标不可达不该拖住本目标的恢复（全局 complete 早就为假）。"""
    from agent import notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        web = _anomaly(id="web1/resources/磁盘 /data", target="web1", name="磁盘 /data")
        notify.process_run(_rec_with_signal(_report_with([web])))
        assert len(sent.calls) == 1
        # 本次：web1 干净且完整，db1 不可达 → 全局 complete=False
        report = _report_with([], complete=False, verdict="不完整")
        report["target_names"] = ["web1", "db1"]
        report["targets"] = [
            {"name": "web1", "label": "web1", "checks": [], "verdict": "正常",
             "reachable": True, "platform": "Linux"},
            {"name": "db1", "label": "db1", "verdict": "不完整", "reachable": False,
             "platform": "",
             "checks": [{"id": "db1/connectivity/连通性", "target": "db1",
                         "group": "connectivity", "name": "连通性", "status": "error",
                         "display": "不可达", "threshold": "", "detail": "ssh 超时",
                         "sampled_at": "2026-10-22 10:00:00",
                         "evidence": {"command": "uname -s", "exit": 255,
                                      "output": "ssh: connect timed out"}}]}]
        out = notify.process_run(_rec_with_signal(report, session="sess-web1", verdict="不完整"))
        assert out["actions"]["recovered"] == 1, out["actions"]
        assert any("磁盘 /data" in r["title"]
                   for r in out["notification"]["recoveries"])
    finally:
        notify._post = orig
        _clean_notify()


def test_notify_recurrence_after_recovery_logs_new_event():
    """恢复后再次异常要重新记一条 anomaly-new，时间线不能看不出复发。"""
    from agent import events, notify
    orig = notify._post
    sent = _FakePost()
    notify._post = sent
    try:
        _clean_notify()
        _notify_setup()
        notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        ok = _rec_with_signal(_report_with([], complete=True, verdict="正常"), verdict="正常")
        notify.process_run(ok)
        notify.process_run(_rec_with_signal(_report_with([_anomaly()]),
                                            session="sess-again-1"))
        kinds = [r["kind"] for r in events.tail(20)]
        assert kinds.count("anomaly-new") == 2, kinds
        assert "anomaly-recovered" in kinds
        assert len(sent.calls) == 3
    finally:
        notify._post = orig
        _clean_notify()


# Review regressions: notifications and events
def test_review_events_reads_rotated_file_without_current():
    from agent import events
    _clean_notify()
    try:
        events.append('anomaly-new', title='旧事件')
        events._path().replace(events._path().with_name('events.jsonl.1'))
        assert events.tail(1)[0]['title'] == '旧事件'
    finally:
        _clean_notify()


def test_review_notify_redacts_endpoint_in_delivery_errors():
    from agent import notify
    original = notify._post
    try:
        for status, body, error in (
                (200, 'ok', 'invalid URL https://hooks.example.com/services/T/B/SECRET'),
                (400, 'invalid hook SECRET and querycredential', None),
                (200, '{"ok":false,"error":"bad hook SECRET"}', None)):
            _clean_notify()
            _notify_setup(url='https://hooks.example.com/services/T/B/SECRET?k=querycredential')
            notify._post = _FakePost(status=status, body=body, error=error)
            result = notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
            assert result['notification']['status'] == 'failed'
            for path in [core.STATE_DIR / 'events.jsonl',
                         *list((core.STATE_DIR / 'notifications').glob('*.json'))]:
                text = path.read_text()
                assert 'SECRET' not in text and 'querycredential' not in text
    finally:
        notify._post = original
        _clean_notify()


def test_review_notify_failed_alert_does_not_create_false_recovery():
    from agent import notify
    original = notify._post
    try:
        _clean_notify()
        _notify_setup()
        notify._post = _FakePost(error='offline')
        failed = notify.process_run(_rec_with_signal(_report_with([_anomaly()])))['notification']
        sent = _FakePost()
        notify._post = sent
        normal = _rec_with_signal(_report_with([], complete=True, verdict='正常'))
        assert notify.process_run(normal)['notification'] is None
        assert notify.retry_pending(notif_id=failed['id'])[0]['status'] == 'superseded'
        assert not sent.calls
    finally:
        notify._post = original
        _clean_notify()


def test_review_notify_recovery_retry_clears_state_once():
    from agent import notify
    original = notify._post
    try:
        _clean_notify()
        _notify_setup()
        notify._post = _FakePost()
        notify.process_run(_rec_with_signal(_report_with([_anomaly()])))
        notify._post = _FakePost(error='offline')
        normal = _rec_with_signal(_report_with([], complete=True, verdict='正常'))
        failed = notify.process_run(normal)['notification']
        sent = _FakePost()
        notify._post = sent
        assert notify.retry_pending(notif_id=failed['id'])[0]['status'] == 'sent'
        assert notify._load_state()['items']['check:web1/resources/磁盘 /data']['severity'] is None
        assert notify.process_run(normal)['notification'] is None
        assert len(sent.calls) == 1
    finally:
        notify._post = original
        _clean_notify()


def test_review_notify_discards_stale_recovery_after_recurrence():
    from agent import notify
    original = notify._post
    try:
        _clean_notify()
        _notify_setup()
        notify._post = _FakePost()
        alert = _rec_with_signal(_report_with([_anomaly()]))
        notify.process_run(alert)
        notify._post = _FakePost(error='offline')
        normal = _rec_with_signal(_report_with([], complete=True, verdict='正常'))
        recovery = notify.process_run(normal)['notification']
        sent = _FakePost()
        notify._post = sent
        # 同一告警复发仍可能处于去重窗口；也必须取消待发的恢复。
        alert = _rec_with_signal(_report_with([_anomaly()]))
        notify.process_run(alert)
        assert notify.retry_pending(notif_id=recovery['id'])[0]['status'] == 'superseded'
        assert not sent.calls
        assert not notify._load_state()['items']['check:web1/resources/磁盘 /data'].get('pending_recovery')
    finally:
        notify._post = original
        _clean_notify()


def test_review_notify_refreshes_partial_batch_and_mute_rearms():
    from agent import notify
    original = notify._post
    try:
        _clean_notify()
        _notify_setup()
        notify._post = _FakePost(error='offline')
        a, b = _anomaly(), _anomaly(id='web1/resources/内存', name='内存',
                                    line='内存已用 96%', command='free -m', output='96%')
        failed = notify.process_run(_rec_with_signal(_report_with([a, b])))['notification']
        only_b = _rec_with_signal(_report_with([b]))
        notify.process_run(only_b)
        cfg = notify.load_config()
        cfg['mute_until'] = time.time() + 3600
        notify.save_config(cfg)
        assert notify.retry_pending(notif_id=failed['id'])[0]['status'] == 'muted'
        cfg['mute_until'] = 0
        notify.save_config(cfg)
        sent = _FakePost()
        notify._post = sent
        out = notify.process_run(only_b)
        assert out['notification']['status'] == 'sent'
        assert len(out['notification']['items']) == 1
        assert out['notification']['items'][0]['title'].endswith('内存')
        assert '磁盘 /data' not in sent.calls[0]['text']
    finally:
        notify._post = original
        _clean_notify()


def test_review_notify_retry_filters_recovered_items_and_retains_transient():
    from agent import notify
    original = notify._post
    try:
        _clean_notify()
        _notify_setup()
        notify._post = _FakePost(error='offline')
        a, b = _anomaly(), _anomaly(id='web1/resources/内存', name='内存',
                                    line='内存已用 96%', command='free -m', output='96%')
        failed = notify.process_run(_rec_with_signal(_report_with([a, b])))['notification']
        notify.process_run(_rec_with_signal(_report_with([b])))
        sent = _FakePost()
        notify._post = sent
        retried = notify.retry_pending(notif_id=failed['id'])[0]
        assert retried['status'] == 'sent'
        assert len(retried['items']) == 1 and '磁盘 /data' not in sent.calls[0]['text']
        # missed 是一次性事件，状态不会保持 open，但失败后仍必须可重试。
        notify._post = _FakePost(error='offline')
        missed = notify.process_run({'task_id': 'missed-task', 'run_id': 'missed-run',
                                     'status': 'missed', 'error': 'missed one'})['notification']
        notify._post = sent
        assert notify.retry_pending(notif_id=missed['id'])[0]['status'] == 'sent'
    finally:
        notify._post = original
        _clean_notify()


def test_review_notify_updates_last_seen_and_skips_corrupt_outbox():
    from agent import notify
    original = notify._post
    try:
        _clean_notify()
        _notify_setup()
        notify._post = _FakePost(error='offline')
        rec = _rec_with_signal(_report_with([_anomaly()]))
        failed = notify.process_run(rec)['notification']
        state = notify._load_state()
        state['items']['check:web1/resources/磁盘 /data']['last_seen'] = 1
        notify._save_state(state)
        notify.process_run(rec)
        assert notify._load_state()['items']['check:web1/resources/磁盘 /data']['last_seen'] > 1
        for over in ({'attempts': 'oops'}, {'items': [{'fingerprint': []}]},
                     {'items': [{'fingerprint': 'x', 'severity': []}]},
                     {'report_paths': [{}]}):
            notify._write_notification(dict(failed, id='broken', **over))
            assert 'broken' not in [n['id'] for n in notify.list_notifications()]
        assert notify.retry_pending() == []
    finally:
        notify._post = original
        _clean_notify()


# Review regressions: security
# Review regressions: security

def test_review_custom_state_sensitive_paths():
    from agent import tools as agent_tools
    ctx = agent_tools.ToolContext()
    state = core.STATE_DIR
    for path in (state / "notify.json", state / "snapshots" / "s1" / "files" / "config"):
        assert core.sensitive_command(f"cat {path}"), path
        out = agent_tools.dispatch("read", {"path": str(path)}, ctx)
        assert "拒绝" in out, (path, out)
    for suffix in ("", "/.", "/*", "/n*"):
        assert core.sensitive_command(f"grep -r hook {state}{suffix}"), suffix
    assert core.sensitive_command(f"ls -l {state}") is None
    assert core.sensitive_command(f"cat {state}/reports/health.md") is None


def test_review_read_symlink_sensitive_directory():
    from agent import tools as agent_tools
    with tempfile.TemporaryDirectory() as td:
        secret_dir = Path(td) / ".config" / "gcloud"
        secret_dir.mkdir(parents=True)
        secret = secret_dir / "application_default_credentials.json"
        secret.write_text('{"refresh_token":"directory-alias-sentinel"}')
        link = Path(td) / "ordinary.json"
        link.symlink_to(secret)
        out = agent_tools.dispatch("read", {"path": str(link)}, agent_tools.ToolContext())
        assert "拒绝" in out and "directory-alias-sentinel" not in out, out


def test_review_kubectl_credential_dump_flags():
    for flag in ("-oyaml", "-ojson", "-o=json", "-ojsonpath={.data}",
                 "--output=yaml"):
        cmd = f"kubectl get secret database {flag}"
        assert core.sensitive_command(cmd), cmd
        assert core.check(cmd)[0] == 2, cmd
    for flag in ("-o name", "-oname", "-o=name"):
        assert core.sensitive_command(f"kubectl get secret database {flag}") is None, flag
    for flag in ("--raw", "--raw=true"):
        assert core.sensitive_command(f"kubectl config view {flag}"), flag
    assert core.sensitive_command("kubectl config view") is None


def test_review_kubectl_literal_scope_and_display():
    for flags in ("--context=''", '--context=""', '--context="$OPS_TEST_CONTEXT"',
                  '--context=prod --context=stage', '--context=prod -n "$OPS_TEST_NS"'):
        cmd = f"kubectl apply -f deployment.yaml {flags}"
        try:
            core.approve(cmd, ttl_seconds=60)
        except core.OpsxError:
            pass
        else:
            raise AssertionError(f"empty/dynamic/ambiguous scope was approved: {cmd}")
    cmd = ("kubectl get pods --context=dev && "
           "kubectl apply -f deployment.yaml --context=prod -n production")
    p = core.approve(cmd, ttl_seconds=60)
    stamp = json.loads(p.read_text())
    assert stamp["env"]["k8s"] == {"context": "prod", "namespace": "production"}, stamp
    assert core.check_stamp(cmd)
    # A previously issued stamp must also fail after scope validation tightens.
    old = 'kubectl apply -f deployment.yaml --context="$OPS_TEST_CONTEXT"'
    stamp.update(cmd=old, cmd_hash=core.cmd_hash(old), env=core.resolve_env(old))
    (p.parent / (core.cmd_hash(old) + ".json")).write_text(json.dumps(stamp))
    assert not core.check_stamp(old)


def test_review_rollback_redacts_before_diff_and_clip():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "config.txt"
        body = [f"private-key-body-sentinel-{i}" for i in range(12)]
        original = ("-----BEGIN PRIVATE KEY-----\n" + "\n".join(body)
                    + "\n-----END PRIVATE KEY-----\n")
        f.write_text(original)
        sid = core.snapshot_file(str(f))
        f.write_text(original.replace(body[6], "changed-private-key-line"))
        plan = core.rollback_plan(sid)
        assert "private-key-body-sentinel" not in json.dumps(plan), plan
        archived, meta = core.load_snapshot(sid)
        assert (archived / meta["items"][0]["dest"]).read_text() == original
        # A long PEM would lose its end marker if clipping happened before redaction.
        f.write_text("-----BEGIN PRIVATE KEY-----\n" + "long-pem-sentinel\n" * 500
                     + "-----END PRIVATE KEY-----\n")
        cmd_sid = core.snapshot_cmd(f"cat {f}")
        output = core.rollback_plan(cmd_sid)["commands"][0]["output"]
        assert "long-pem-sentinel" not in output and core.REDACTED in output, output


def test_review_exec_rechecks_approval_after_snapshot():
    import remote
    from unittest import mock
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "config.txt"
        f.write_text("before")
        cmd = f"printf changed > {f}"
        core.approve(cmd, ttl_seconds=60)
        original = core._snapshot_file_into

        def copy_then_revoke(d, src, items):
            original(d, src, items)
            core.revoke(cmd)

        with mock.patch.object(core, "_snapshot_file_into", copy_then_revoke):
            code, sid, _ = core.exec_change(cmd, snapshot_files=[str(f)])
        assert code == 2 and sid and f.read_text() == "before"
        _write_targets()
        remote_cmd = "systemctl restart snapshot-bound"
        core.approve(remote_cmd, ttl_seconds=60, target="web1")

        def pull_then_revoke(name, src, d, items):
            dest = d / "file.txt"
            dest.write_text("before")
            items.append({"type": "file", "src": src, "dest": "file.txt"})
            core.revoke(remote_cmd, target=name)

        with mock.patch.object(remote, "_pull_file", pull_then_revoke), \
                mock.patch.object(remote, "_ssh") as ssh:
            code, sid, _ = remote.exec_change("web1", remote_cmd,
                                               snapshot_files=["/etc/app.conf"])
        assert code == 2 and sid
        # 快照阶段会探测机器指纹（多一次只读 ssh），但变更命令一定没下发
        sent = [call.args[1] if len(call.args) > 1 else "" for call in ssh.call_args_list]
        assert not any(remote_cmd in cmd for cmd in sent), sent


def test_review_sensitive_input_redirection():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        try:
            os.chdir(td)
            Path('.env').write_text('arbitrary-config-sentinel')
            for cmd in ('cat <.env', 'cat<.env', 'head -c 10 <.env'):
                assert core.sensitive_command(cmd), cmd
                assert core.check(cmd)[0] == 2, cmd
        finally:
            os.chdir(cwd)


# Review regressions: checks
def test_review_signal_keeps_other_targets_and_unsampled_groups():
    """分批检查保留其他目标与未采样分组；同项新采样才替换旧状态。"""
    from agent import checks
    restore = _with_targets({
        "web1": {"host": "h1", "services": [{"type": "docker", "name": "api"}]},
        "db1": {"host": "h2", "services": [{"type": "docker", "name": "db"}]}})
    original = checks._exec
    sid = "review-checks-merge"
    path = checks.signal_path(sid)
    path.unlink(missing_ok=True)
    unhealthy = _linux_fake({"cat /proc/meminfo": (0, "MemTotal: 1000 kB\nMemAvailable: 1 kB\n")})
    checks._exec = unhealthy
    try:
        web = checks.run_checks(targets="web1")
        checks.save_signal(sid, web, "/tmp/web-evidence.md")
        checks._exec = _linux_fake()
        db = checks.run_checks(targets="db1")
        checks.save_signal(sid, db, "/tmp/db-evidence.md")
        signal = checks.load_signal(sid)
        assert signal["verdict"] == "异常"
        assert signal["targets"] == ["web1", "db1"]
        assert signal["anomalies"][0]["id"] == "web1/resources/内存"
        assert signal["report_paths"] == ["/tmp/web-evidence.md", "/tmp/db-evidence.md"]
        assert signal["target_complete"] == {"web1": True, "db1": True}
        # 只重查服务，不能把此前内存告警丢掉。
        checks.save_signal(sid, checks.run_checks(targets="web1", groups=["services"]))
        assert checks.load_signal(sid)["anomalies"][0]["id"] == "web1/resources/内存"
        checks.save_signal(sid, checks.run_checks(targets="web1", groups=["resources"]))
        recovered = checks.load_signal(sid)
        assert recovered["verdict"] == "正常" and not recovered["anomalies"]
        assert recovered["summary"]["checks"] == 10
    finally:
        checks._exec = original
        path.unlink(missing_ok=True)
        restore()


def test_review_signal_target_completeness_does_not_follow_verdict():
    """同一目标既有 critical 又有 unknown 时，结论异常不代表采集完整。"""
    from agent import checks
    restore = _with_targets({"web1": {"host": "h", "services": [{"type": "docker", "name": "api"}]}})
    original = checks._exec
    checks._exec = _linux_fake({"cat /proc/meminfo": (0, "unparseable"),
                               "docker inspect --format '{{.State.Status}},{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}},{{.RestartCount}}' api":
                               (0, "exited,none,0\n")})
    try:
        report = checks.run_checks(targets="web1")
        payload = checks._signal_payload(report, None)
        assert payload["verdict"] == "异常"
        assert payload["target_verdicts"]["web1"] == "异常"
        assert payload["target_complete"]["web1"] is False
    finally:
        checks._exec = original
        restore()


def test_review_inspect_workflow_share_deadline():
    """工具里的确定性检查必须收到调度传下来的整任务截止时间。"""
    from agent import checks, tools as agent_tools
    original = checks.run_checks
    called = []
    def fake(**kwargs):
        called.append(kwargs.get("deadline"))
        raise checks.CheckError("test stop")
    checks.run_checks = fake
    ctx = agent_tools.ToolContext(deadline=time.monotonic() - 1)
    try:
        agent_tools.dispatch("inspect", {}, ctx)
        agent_tools.dispatch("workflow", {"name": "daily-inspection"}, ctx)
        assert called == [ctx.deadline, ctx.deadline]
    finally:
        checks.run_checks = original


def test_review_inspect_preview_preserves_entire_anomaly_list():
    """输出截断不能静默丢掉第 30K 字符之后的异常，落盘失败时也如此。"""
    from agent import checks, tools as agent_tools
    original = checks.run_checks, checks.save_report
    report = _report_with([_anomaly(id=f"web1/resources/item-{i}", name=f"item-{i}")
                           for i in range(180)])
    for item in report["anomalies"]:
        item["line"] += "X" * 200
    checks.run_checks = lambda **kwargs: report
    checks.save_report = lambda *args: None
    try:
        result = agent_tools.dispatch("inspect", {}, agent_tools.ToolContext())
        assert len(result) > agent_tools.OUTPUT_LIMIT
        assert all(item["line"] in result for item in report["anomalies"])
        assert "已截断" in result
    finally:
        checks.run_checks, checks.save_report = original


def test_review_disk_path_rejects_del_control_character():
    from agent import checks
    try:
        checks._norm_checks("web1", {"disk_paths": ["/data\x7f"]})
        raise AssertionError("DEL 控制字符不应进入探针路径")
    except checks.CheckError:
        pass

def test_review_signal_merge_uses_each_targets_sampled_groups():
    """跨重试会话聚合时，不把别的目标采过的分组当成本目标已采样。"""
    from agent import checks
    restore = _with_targets({
        "web1": {"host": "h1", "services": [{"type": "docker", "name": "api"}]},
        "db1": {"host": "h2", "services": [{"type": "docker", "name": "db"}]}})
    original = checks._exec
    try:
        checks._exec = _linux_fake({"cat /proc/meminfo": (0, "MemTotal: 1000 kB\nMemAvailable: 1 kB\n")})
        first = checks._signal_payload(checks.run_checks(targets="web1"), None)
        checks._exec = _linux_fake()
        second = checks._merge_signal(
            checks._signal_payload(checks.run_checks(targets="web1", groups=["connectivity"]), None),
            checks._signal_payload(checks.run_checks(targets="db1", groups=["resources"]), None))
        merged = checks._merge_signal(first, second)
        assert [a["id"] for a in merged["anomalies"]] == ["web1/resources/内存"]
        assert merged["target_complete"]["db1"] is False
        assert merged["verdict"] == "异常"
    finally:
        checks._exec = original
        restore()


def test_review_signal_rejects_corrupt_merge_metadata():
    from agent import checks
    sid = "review-corrupt-metadata"
    path = checks.signal_path(sid)
    original = checks._signal_payload(_report_with([_anomaly()]), None)
    original["session_id"] = sid
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        for over in ({"targets": {}}, {"target_groups": {"web1": 1}},
                     {"target_complete": {"web1": "true"}},
                     {"check_statuses": {"web1/resources/x": []}},
                     {"anomalies": ["not an item"]}):
            path.write_text(json.dumps(dict(original, **over)), encoding="utf-8")
            assert checks.load_signal(sid) is None, over
    finally:
        path.unlink(missing_ok=True)



def test_review_notify_severity_changes_compare_last_notification():
    from agent import notify
    cfg = {'renotify_after': 21600}
    critical = {'severity': 'critical', 'task_id': 'task1', 'title': 'disk', 'kind': 'check'}
    warning = dict(critical, severity='warning')
    fp = 'check:web1/resources/disk'
    # 首次发送失败，没有人见过告警时，降级不能因冷却而把全部通知丢掉。
    state = {'items': {}}
    notify._plan(state, {fp: dict(critical)}, None, {}, 1000, cfg)
    notify._mark_notified(state, [fp], 1000, 'failed1', delivered=False)
    items, _, _ = notify._plan(state, {fp: dict(warning)}, None, {}, 1060, cfg)
    assert len(items) == 1
    # 已送达 critical 后的稳定 warning 应在抖动窗口结束后通知。
    state = {'items': {}}
    notify._plan(state, {fp: dict(critical)}, None, {}, 1000, cfg)
    notify._mark_notified(state, [fp], 1000, 'sent1', delivered=True)
    items, _, _ = notify._plan(state, {fp: dict(warning)}, None, {}, 1060, cfg)
    assert not items
    items, _, _ = notify._plan(state, {fp: dict(warning)}, None, {}, 1400, cfg)
    assert len(items) == 1 and items[0]['severity'] == 'warning'


def test_review_notify_structured_data_is_redacted_at_exits():
    from agent import notify
    original = notify._post
    try:
        _clean_notify()
        _notify_setup()
        sent = _FakePost()
        notify._post = sent
        secret = 'STRUCTURED-SECRET-SENTINEL'
        notify.process_run({'task_id': 'task1', 'run_id': 'run1', 'status': 'failed',
                            'error': 'password=' + secret})
        texts = [json.dumps(sent.calls), (core.STATE_DIR / 'notify-state.json').read_text()]
        texts.extend(p.read_text() for p in (core.STATE_DIR / 'notifications').glob('*.json'))
        assert all(secret not in text for text in texts)
    finally:
        notify._post = original
        _clean_notify()


def test_schedule_rules_override_model_normal():
    """结构化异常/未知不能被模型的“正常”覆盖，退出码与记录保持规则结论。"""
    from unittest.mock import patch
    from agent import checks, schedule
    _clean_schedules()
    _clean_notify()
    try:
        entry = schedule.add("巡检", every_seconds=60, checks_target="all")
        for expected, report in (
                ("异常", _report_with([_anomaly()])),
                ("警告", _report_with([_anomaly(status="warn")], verdict="警告")),
                ("不完整", _report_with([], complete=False, verdict="不完整"))):
            with patch.object(checks, "run_checks", return_value=report), \
                    patch.object(schedule, "_run_entry", return_value="结论：正常"):
                rec = schedule._execute_run(entry, None)
            assert rec["status"] == "succeeded" and rec["verdict"] == expected, rec
            assert schedule.list_entries()[0]["last_verdict"] == expected
            assert schedule.runs_exit_code([rec]) == (3 if expected == "不完整" else 2)
            assert schedule.list_runs()[0]["verdict"] == expected
    finally:
        _clean_schedules()
        _clean_notify()


def test_schedule_preset_check_errors_fail_even_if_model_normal():
    """规则无法执行或摘要写失败是运行失败，提示词不能代替失败状态。"""
    from unittest.mock import patch
    from agent import checks, schedule
    _clean_schedules()
    _clean_notify()
    try:
        entry = schedule.add("巡检", every_seconds=60, checks_target="all")
        with patch.object(checks, "run_checks", side_effect=checks.CheckError("bad target")), \
                patch.object(schedule, "_run_entry", return_value="结论：正常"):
            rec = schedule._execute_run(entry, None)
        assert rec["status"] == "failed" and "规则检查未能执行" in rec["error"]
        assert schedule.runs_exit_code([rec]) == 1
        with patch.object(checks, "run_checks", return_value=_report_with([])), \
                patch.object(checks, "save_signal", return_value=None), \
                patch.object(schedule, "_run_entry", return_value="结论：正常"):
            rec = schedule._execute_run(entry, None)
        assert rec["status"] == "failed" and "摘要写入失败" in rec["error"]
    finally:
        _clean_schedules()
        _clean_notify()


def test_schedule_claim_failure_is_a_visible_failed_run():
    """推进 next_run 写失败时不能冒充“无到期任务”并 exit 0。"""
    from unittest.mock import patch
    from agent import schedule
    _clean_schedules()
    _clean_notify()
    try:
        entry = schedule.add("巡检", every_seconds=60)
        entries = schedule.list_entries()
        entries[0]["next_run"] = time.time() - 1
        schedule._save(entries)
        save, calls = schedule._save, []

        def fail_claim(*args, **kwargs):
            calls.append(None)
            if len(calls) == 2:
                raise OSError("claim write failed")
            return save(*args, **kwargs)

        with patch.object(schedule, "_save", side_effect=fail_claim), \
                patch.object(schedule, "_run_entry") as runner:
            records = schedule.run_due()
        runner.assert_not_called()
        assert len(records) == 1 and records[0]["status"] == "failed", records
        assert "claim write failed" in records[0]["error"]
        assert schedule.runs_exit_code(records) == 1
        assert schedule.list_runs(task_id=entry["id"])[0]["status"] == "failed"
    finally:
        _clean_schedules()
        _clean_notify()


def test_schedule_successful_runs_have_chronological_pending_notifications():
    """成功巡检写完结果、发送前崩溃，也必须按发生顺序补发。"""
    from unittest.mock import patch
    from agent import schedule
    _clean_schedules()
    _clean_notify()
    try:
        entry = schedule.add("巡检", every_seconds=60)
        first = schedule._finish_run(schedule._begin_run(entry, None), entry,
                                     "succeeded", verdict="异常")
        second = schedule._finish_run(schedule._begin_run(entry, None), entry,
                                      "succeeded", verdict="正常")
        pending = schedule._pending_notify_runs()
        assert [rec["run_id"] for rec in pending] == [first["run_id"], second["run_id"]]
        seen = []
        with patch.object(schedule, "_notify_run", side_effect=lambda rec: seen.append(rec["run_id"])):
            schedule._notify_pending_runs()
        assert seen == [first["run_id"], second["run_id"]]
    finally:
        _clean_schedules()
        _clean_notify()


def test_schedule_retry_keeps_rules_from_other_attempts():
    """重试会话只查另一目标，不能丢首轮规则异常或其报告路径。"""
    from unittest.mock import patch
    from agent import checks, notify, schedule
    _clean_schedules()
    _clean_notify()
    try:
        entry = schedule.add("巡检", every_seconds=60, checks_target="all")
        seen = []

        def retry(entry, model, session=None, **kwargs):
            seen.append(session.id)
            if len(seen) == 1:
                raise ConnectionError("temporary failure")
            report = _report_with([_anomaly(id="db1/resources/磁盘 /db", target="db1")])
            checks.save_signal(session.id, report, "/tmp/retry-db-report.md")
            return "结论：正常"

        with patch.object(checks, "run_checks", return_value=_report_with([_anomaly()])), \
                patch.object(schedule, "_run_entry", side_effect=retry), \
                patch.object(schedule.time, "sleep"):
            rec = schedule._execute_run(entry, None)
        assert rec["attempts"] == 2 and rec["verdict"] == "异常", rec
        assert {a["target"] for a in rec["anomalies"]} == {"web1", "db1"}
        assert len(rec["rules_report_paths"]) == 2
        saved = notify._signal_from_record(rec)
        assert {a["target"] for a in saved["anomalies"]} == {"web1", "db1"}
        assert rec["session_id"] == seen[-1]
        _clean_schedules()
        _clean_notify()
        entry = schedule.add("巡检", every_seconds=60, checks_target="all")
        seen.clear()
        original_save = checks._save_signal_payload
        written = []

        def fail_aggregate(session_id, payload):
            written.append(None)
            return None if len(written) == 3 else original_save(session_id, payload)

        with patch.object(checks, "run_checks", return_value=_report_with([_anomaly()])), \
                patch.object(checks, "_save_signal_payload", side_effect=fail_aggregate), \
                patch.object(schedule, "_run_entry", side_effect=retry), \
                patch.object(schedule.time, "sleep"):
            rec = schedule._execute_run(entry, None)
        assert rec["status"] == "failed" and "摘要写入失败" in rec["error"]
        assert all(saved["status"] != "running" for saved in schedule.list_runs())
    finally:
        _clean_schedules()
        _clean_notify()


def test_schedule_prompt_secret_redacted_before_truncation_and_storage():
    """任务提示也是常规记录，长私钥必须先脱敏再进入审计/运行摘要。"""
    from agent import schedule
    _clean_schedules()
    private = "SCHEDULE-PRIVATE-SENTINEL" * 40
    prompt = f"排查\n-----BEGIN PRIVATE KEY-----\n{private}\n-----END PRIVATE KEY-----"
    try:
        entry = schedule.add(prompt, every_seconds=60)
        run = schedule._begin_run(entry, None)
        text = schedule._file().read_text() + (schedule._runs_dir() / f"{run['run_id']}.json").read_text()
        text += (core.STATE_DIR / "audit.jsonl").read_text()
        assert "SCHEDULE-PRIVATE-SENTINEL" not in text
        assert core.REDACTED in text
        assert schedule._file().stat().st_mode & 0o777 == 0o600
        success = schedule._finish_run(run, entry, "succeeded", verdict="正常")
        previous = schedule.list_entries()[0]["last_success"]
        schedule._finish_run(schedule._begin_run(entry, None), entry, "failed", error="failed")
        assert schedule.list_entries()[0]["last_success"] == previous
    finally:
        _clean_schedules()


def test_run_turn_deadline_stops_remaining_calls_in_same_reply():
    """同一次模型回复里的工具也受预算约束，未执行调用要有明确协议记录。"""
    from unittest.mock import patch
    from agent import loop, session as sessions, tools as agent_tools
    session = sessions.Session()
    ctx = agent_tools.ToolContext(deadline=time.monotonic() + 100)
    calls = [{"id": f"call-{i}", "type": "function",
              "function": {"name": "bash", "arguments": '{"command":"uname"}'}}
             for i in range(3)]
    msg = {"role": "assistant", "content": "检查中", "tool_calls": calls}
    seen = []

    def dispatch(name, args, context):
        seen.append(name)
        context.deadline = time.monotonic() - 1
        return "[exit=0] Linux"

    with patch.object(loop.llm, "chat", return_value=msg), \
            patch.object(agent_tools, "dispatch", side_effect=dispatch):
        text = loop.run_turn("检查", session, ctx, max_turns=1)
    assert seen == ["bash"] and loop.DEADLINE_MARK in text
    results = [m for m in session.messages if m["role"] == "tool"]
    assert len(results) == 3 and all("工具未执行" in m["content"] for m in results[1:])


def test_rpc_and_print_mode_errors_redact_credentials():
    """LLM 异常体可能回显凭据，RPC 与单发错误出口同样脱敏。"""
    import runpy
    from unittest.mock import patch
    from agent import loop, rpc
    secret = "sk-ERRORSENTINEL0123456789abcdef"
    request = {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"text": "检查"}}
    stdout = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), \
            patch.object(sys, "stdout", stdout), \
            patch.object(loop, "run_turn", side_effect=loop.llm.LLMError(f"api_key={secret}")):
        rpc.serve()
    assert secret not in stdout.getvalue() and core.REDACTED in stdout.getvalue()
    script = runpy.run_path(str(Path(__file__).resolve().parent / "opsagent"))
    stderr = io.StringIO()
    with patch.object(sys, "stderr", stderr), \
            patch.object(loop, "run_turn", side_effect=loop.llm.LLMError(f"api_key={secret}")):
        assert script["_print_mode"]("检查", None) == 1
    assert secret not in stderr.getvalue() and core.REDACTED in stderr.getvalue()


def test_schedule_processing_error_is_retried_after_config_is_fixed():
    """通知配置/状态处理失败尚无发件箱，修复后仍要补处理原运行。"""
    from unittest.mock import patch
    from agent import notify, schedule
    _clean_schedules()
    _clean_notify()
    try:
        entry = schedule.add("巡检", every_seconds=60)
        rec = schedule._finish_run(schedule._begin_run(entry, None), entry,
                                   "succeeded", verdict="异常")
        with patch.object(notify, "process_run", return_value={"error": "bad config"}):
            schedule._notify_run(rec)
        assert rec["status"] == "succeeded" and rec["notify_status"] == "error"
        assert schedule._pending_notify_runs() == []  # 失败旁路有退避，不能每秒处理。
        future = time.time() + 61
        with patch.object(schedule.time, "time", return_value=future), \
                patch.object(notify, "process_run", return_value={"configured": False}) as process:
            schedule._notify_pending_runs()
        assert process.call_count == 1
        saved = schedule.list_runs()[0]
        assert saved["status"] == "succeeded" and saved["notify_status"] == "none"
        assert not saved.get("notify_error") and schedule._pending_notify_runs() == []
    finally:
        _clean_schedules()
        _clean_notify()


def test_review_kubectl_auth_reconcile_requires_context():
    assert core._kubectl_read_only('kubectl auth can-i get pods')
    assert core._kubectl_read_only('kubectl auth whoami')
    assert not core._kubectl_read_only('kubectl auth reconcile -f roles.yaml')
    try:
        core.approve('kubectl auth reconcile -f roles.yaml')
        raise AssertionError('RBAC 写操作必须显式绑定 context')
    except core.OpsxError:
        pass


def test_review_notify_old_runs_cannot_overwrite_new_observations():
    from agent import checks, notify
    original = notify._post
    try:
        _clean_notify()
        _notify_setup()
        sent = _FakePost()
        notify._post = sent
        now = time.time()
        healthy = checks._signal_payload(_report_with([], verdict='正常'), None)
        unhealthy = checks._signal_payload(_report_with([_anomaly()]), None)
        new = {'task_id': 'task-new', 'run_id': 'new', 'finished_ts': now,
               'status': 'succeeded', 'verdict': '正常'}
        old = {'task_id': 'task-old', 'run_id': 'old', 'finished_ts': now - 60,
               'status': 'succeeded', 'verdict': '异常'}
        # 健康时没有告警指纹，仍要记住已采样，挡住其他任务迟到的旧告警。
        assert notify.process_run(new, signal=healthy)['notification'] is None
        assert notify.process_run(old, signal=unhealthy)['notification'] is None
        assert not sent.calls
        # 新异常不能被旧健康报告恢复；新恢复后也不能由旧异常重新打开。
        new.update(finished_ts=now + 1, verdict='异常')
        notify.process_run(new, signal=unhealthy)
        assert len(sent.calls) == 1
        old['verdict'] = '正常'
        assert notify.process_run(old, signal=healthy)['notification'] is None
        new.update(finished_ts=now + 2, verdict='正常')
        notify.process_run(new, signal=healthy)
        assert len(sent.calls) == 2
        old['verdict'] = '异常'
        assert notify.process_run(old, signal=unhealthy)['notification'] is None
        assert len(sent.calls) == 2
    finally:
        notify._post = original
        _clean_notify()


def test_execution_records_state_machine():
    """每次变更都落一条执行记录：成功/失败都带退出码、耗时与证据入口。"""
    import execution
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "a.txt"
        ok = f"echo ok > {f}"
        core.approve(ok, ttl_seconds=60)
        code, sid, _ = core.exec_change(ok)
        assert code == 0
        rec = execution.find_latest(ok)
        assert rec and rec["status"] == "succeeded" and rec["exit_code"] == 0
        assert rec["finished"] and rec["duration_s"] is not None
        assert rec["cmd_hash"] == core.cmd_hash(ok, None)
        assert rec["pid"] == os.getpid()
        # 记录文件 0600：里面有命令原文与输出片段，等同审计材料
        path = Path(core.STATE_DIR, "executions", rec["id"] + ".json")
        assert stat_mode(path) == 0o600, oct(stat_mode(path))

        fail = "rm /nonexistent-opsx-w5-file"
        core.approve(fail, ttl_seconds=60)
        code, _, _ = core.exec_change(fail)
        assert code == 1
        rec = execution.find_latest(fail)
        assert rec["status"] == "failed" and rec["exit_code"] == 1
        # 展示时脱敏（记录里存原文，供人核对）
        assert rec["id"] in execution.format_list([rec])


def stat_mode(path) -> int:
    import stat as stat_mod
    return stat_mod.S_IMODE(Path(path).stat().st_mode)


def test_target_lock_rejects_concurrent_change():
    """同一目标不允许两个变更重叠：抢不到锁就明确失败，不排队也不静默执行。"""
    import execution
    cmd = "rm /nonexistent-opsx-w5-lock"
    core.approve(cmd, ttl_seconds=60)
    with core.target_lock(None):
        code, sid, output = core.exec_change(cmd)
    assert code == core.EXEC_ABORT_LOCK and sid is None
    assert "正在被另一次变更占用" in output
    rec = execution.find_latest(cmd)
    assert rec and rec["status"] == "aborted" and rec["phase"] == "lock"
    # 被挡下的执行不能消耗审批次数：锁释放后同一条命令仍可执行
    code, _, _ = core.exec_change(cmd)
    assert code == 1                      # rm 失败，但确实执行了
    assert execution.find_latest(cmd)["status"] == "failed"


def test_target_lock_across_processes():
    """跨进程验证：另一个会话持锁期间，本次变更被拒（不靠进程内状态）。"""
    import subprocess
    import sys as sys_mod
    script = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(core.REPO_DIR)!r})\n"
        "import core\n"
        "with core.target_lock('web1'):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(8)\n"
    )
    proc = subprocess.Popen([sys_mod.executable, "-c", script], env=dict(os.environ),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        try:
            with core.target_lock("web1"):
                raise AssertionError("另一个进程持锁时不该拿到锁")
        except core.TargetBusy as e:
            assert "web1" in str(e)
        # 不同目标不受影响
        with core.target_lock("db1"):
            pass
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_remote_timeout_and_ssh255_become_unknown():
    """断线/超时 → unknown（不是 failed），并且挡住同目标同命令的重放。"""
    import subprocess
    import execution
    import remote
    _write_targets()
    cmd = "systemctl restart tricky-svc"
    core.approve(cmd, ttl_seconds=60, target="web1")
    orig = remote._ssh

    def timeout_ssh(name, c, stdin=None, timeout=120):
        raise subprocess.TimeoutExpired(c, timeout)

    remote._ssh = timeout_ssh
    try:
        code, sid, out = remote.exec_change("web1", cmd, timeout=5)
    finally:
        remote._ssh = orig
    assert code == 4 and "远端状态需确认" in out
    rec = execution.find_latest(cmd, "web1")
    assert rec and rec["status"] == "unknown"
    assert "超时" in (rec["error"] or "")

    # 不自动重放：再执行被拦，且一次 ssh 都没发
    sent = []

    def record_ssh(name, c, stdin=None, timeout=120):
        sent.append(c)
        return subprocess.CompletedProcess([], 0, b"", b"")

    remote._ssh = record_ssh
    try:
        core.approve(cmd, ttl_seconds=60, target="web1")
        code2, _, msg = remote.exec_change("web1", cmd)
    finally:
        remote._ssh = orig
    assert code2 == core.EXEC_ABORT_UNKNOWN, code2
    assert not sent, sent
    assert "未核实" in msg and rec["id"] in msg

    # 人核实"未生效"之后才允许重新执行
    execution.resolve(rec["id"], "observed-failed", "确认服务未重启")
    core.approve(cmd, ttl_seconds=60, target="web1")
    remote._ssh = record_ssh
    try:
        code3, _, _ = remote.exec_change("web1", cmd)
    finally:
        remote._ssh = orig
    assert code3 == 0
    assert execution.find_latest(cmd, "web1")["status"] == "succeeded"

    # SSH 255：同样无法确认，记 unknown 而不是 failed
    cmd2 = "systemctl restart ssh255-svc"
    core.approve(cmd2, ttl_seconds=60, target="web1")
    remote._ssh = lambda *a, **k: subprocess.CompletedProcess(
        [], 255, b"", b"Connection reset by peer")
    try:
        code4, _, out4 = remote.exec_change("web1", cmd2)
    finally:
        remote._ssh = orig
    assert code4 == 255 and "无法确认" in out4
    rec2 = execution.find_latest(cmd2, "web1")
    assert rec2["status"] == "unknown"
    execution.resolve(rec2["id"], "observed-ok", "确认已重启")


def test_execution_reconcile_resolve_and_prune():
    """进程被杀 → unknown；只有 unknown 能核实；未核实的 unknown 永不被清理。"""
    import execution
    dead = execution.begin("exec", "rm /nonexistent-opsx-w5-dead", None, core.local_env())
    dead["pid"] = 99999999
    with execution._locked():
        execution._write(dead)
    moved = execution.reconcile()
    assert any(r["id"] == dead["id"] for r in moved)
    got = execution.get(dead["id"])
    assert got["status"] == "unknown" and "无法确认" in got["error"]

    alive = execution.begin("exec", "rm /nonexistent-opsx-w5-alive", None, core.local_env(), phase="lock")
    assert execution.reconcile() == []          # 活着的记录不动
    assert execution.get(alive["id"])["status"] == "running"

    # 迟到收尾不能把 reconcile 的 unknown 改成 succeeded
    execution.finish(dead["id"], "succeeded", exit_code=0)
    assert execution.get(dead["id"])["status"] == "unknown"

    try:
        execution.resolve(alive["id"], "observed-ok")
        raise AssertionError("非 unknown 不该能核实")
    except core.OpsxError as e:
        assert "只有 unknown" in str(e)
    try:
        execution.resolve(dead["id"], "not-a-real-outcome")
        raise AssertionError("非法核实结论应被拒绝")
    except core.OpsxError as e:
        assert "observed-ok" in str(e)

    execution.finish(alive["id"], "failed", exit_code=1)
    assert execution.pending_unknown(None, core.cmd_hash("rm /nonexistent-opsx-w5-dead")) \
        is not None
    execution.resolve(dead["id"], "observed-failed", "确认没执行")
    assert execution.pending_unknown(
        None, core.cmd_hash("rm /nonexistent-opsx-w5-dead")) is None

    # prune 只清已核实/终态记录，未核实的 unknown 必须保留
    open_rec = execution.begin("exec", "rm /nonexistent-opsx-w5-open", None, core.local_env())
    with execution._locked():
        rec = execution._read(Path(core.STATE_DIR, "executions", open_rec["id"] + ".json"))
        rec.update({"status": "unknown", "error": "测试：待核实"})
        execution._write(rec)
    for _ in range(4):
        execution.begin("exec", "rm /nonexistent-opsx-w5-filler", None, core.local_env())
    execution.prune(keep=2)
    assert execution.get(open_rec["id"]) is not None, "未核实的 unknown 不能被清理"
    execution.resolve(open_rec["id"], "observed-failed")


def test_approval_uses_are_one_shot_and_atomic():
    """一次批准只生效一次；并发消耗不会双花（超出的返回 None）。"""
    import threading
    cmd = "rm /nonexistent-opsx-w5-uses"
    core.approve(cmd, ttl_seconds=60)
    assert core.consume_stamp(cmd) == 0           # 最后一次机会
    assert core.consume_stamp(cmd) is None
    assert core.check(cmd)[0] == 2                 # 已用尽 → 拒绝
    assert core.stamp_exhausted(cmd) is True
    audit_text = Path(core.STATE_DIR, "audit.jsonl").read_text(encoding="utf-8")
    assert "approve-consume" in audit_text

    # uses=5 时并发 9 个消耗：恰好 5 个成功，剩余值互不重复
    cmd2 = "rm /nonexistent-opsx-w5-uses2"
    core.approve(cmd2, ttl_seconds=60, uses=5)
    results = []
    lock = threading.Lock()

    def consume():
        value = core.consume_stamp(cmd2)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=consume) for _ in range(9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = sorted(v for v in results if v is not None)
    assert ok == [0, 1, 2, 3, 4], ok           # 5 次成功，剩余次数不重复
    assert results.count(None) == 4

    try:
        core.approve(cmd2, ttl_seconds=60, uses=0)
        raise AssertionError("uses 必须 >= 1")
    except core.OpsxError as e:
        assert "1-" in str(e)


def test_remote_restore_atomic_keeps_mode_and_rebuilds():
    """恢复脚本实测：原子替换、按快照权限还原、失败不留半份文件与临时文件。"""
    import shlex
    import stat as stat_mod
    import subprocess
    import remote
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "app.conf"
        target.write_text("old-content\n")
        os.chmod(target, 0o640)
        payload = b"new-content\n"
        script = remote._render_restore(str(target), "640", len(payload))
        r = subprocess.run(["/bin/sh", "-c", script], input=payload,
                           capture_output=True)
        assert r.returncode == 0, r.stderr
        assert target.read_bytes() == payload, target.read_bytes()
        assert stat_mod.S_IMODE(target.stat().st_mode) == 0o640, \
            oct(stat_mod.S_IMODE(target.stat().st_mode))
        had, size = remote._parse_restore(r.stdout)
        assert had is True and size == len(payload), (had, size, r.stdout)
        assert not list(Path(td).glob(".opsx-restore.*")), "临时文件必须清理"

        # chmod 失败 → 原文件一个字节都没变，且不留临时文件
        bad = script.replace('chmod "640"', 'chmod "9999"')
        r2 = subprocess.run(["/bin/sh", "-c", bad], input=b"should-not-land",
                            capture_output=True)
        assert r2.returncode != 0
        assert target.read_bytes() == payload, "恢复失败不能留下半份内容"
        assert not list(Path(td).glob(".opsx-restore.*"))

        # 文件此前被删除：按快照权限重建（不是默认 umask）
        target.unlink()
        r3 = subprocess.run(["/bin/sh", "-c", script], input=payload,
                            capture_output=True)
        assert r3.returncode == 0, r3.stderr
        had3, _ = remote._parse_restore(r3.stdout)
        assert had3 is False and target.read_bytes() == payload, (had3, r3.stdout)
        assert stat_mod.S_IMODE(target.stat().st_mode) == 0o640, \
            oct(stat_mod.S_IMODE(target.stat().st_mode))

    # 快照没记录权限：覆盖已有文件时沿用目标当前权限，重建时用 600（不是统一降权到 600）
    assert remote._restore_mode({"mode": 0o600}) == ("600", "")
    mode, note = remote._restore_mode({})
    assert mode == "" and "未记录权限" in note and remote.FALLBACK_MODE == "600"


def test_opsx_executions_cli():
    """CLI：执行记录可查、待核实项显眼、非 unknown 不能核实。"""
    import subprocess
    import sys as sys_mod
    state = tempfile.mkdtemp(prefix="opsx-exe-")
    env = dict(os.environ, OPSX_STATE_DIR=state)
    opsx = str(core.REPO_DIR / "opsx")

    def run(*a):
        return subprocess.run([sys_mod.executable, opsx, *a], capture_output=True,
                              text=True, env=env)

    cmd = "rm /nonexistent-opsx-cli-w5"
    assert run("approve", cmd).returncode == 0
    r = run("exec", cmd)
    assert r.returncode == 1, (r.stdout, r.stderr)      # rm 失败
    m = re.search(r"execution_id: (exe-\S+?)（", r.stdout)
    assert m, r.stdout
    eid = m.group(1)
    r = run("executions", "show", eid)
    assert r.returncode == 0 and "failed" in r.stdout and eid in r.stdout
    r = run("executions")
    assert r.returncode == 0 and eid in r.stdout
    r = run("executions", "resolve", eid, "--observed-ok")
    assert r.returncode == 1 and "只有 unknown" in r.stderr
    r = run("executions", "resolve", eid)
    assert r.returncode == 1 and "observed-ok" in r.stderr


def stat_mode(path) -> int:
    import stat as stat_mod
    return stat_mod.S_IMODE(Path(path).stat().st_mode)


def test_remote_kubectl_approval_env_matches_execution():
    """远程 kubectl：审批与执行必须用同一份 env（含 k8s 作用域），否则永远判环境变化。"""
    import remote
    _write_targets()
    cmd = "kubectl -n prod rollout restart deploy/api --context prod-ctx"
    core.approve(cmd, ttl_seconds=60, target="web1")
    env = core.resolve_env(cmd, "web1")
    assert "k8s" in env
    assert core.check(cmd, "web1", env)[0] == 0        # 同源 → 放行
    # target_env 缺 k8s 键：这正是修复前的对照（会被判"执行环境已变化"）
    assert core.check(cmd, "web1", remote.target_env("web1"))[0] == 2


def test_dead_process_running_blocks_replay_without_reconcile():
    """进程被 kill 后记录仍是 running：门禁要直接拦住，不能等有人先查一次列表。"""
    import execution
    cmd = "rm /nonexistent-opsx-w5-killed"
    core.approve(cmd, ttl_seconds=60)
    rec = execution.begin("exec", cmd, None, core.local_env(), phase="exec")
    rec["pid"] = 99999999
    with execution._locked():
        execution._write(rec)
    code, msg = core._prepare_exec(cmd)[:2]
    assert code == core.EXEC_ABORT_UNKNOWN and "未核实" in msg
    # 同一条记录如果还在拿锁/做快照（phase=lock），命令一定没下发 → 明确中止、不阻断
    other = "rm /nonexistent-opsx-w5-killed2"
    core.approve(other, ttl_seconds=60)
    rec2 = execution.begin("exec", other, None, core.local_env(), phase="lock")
    rec2["pid"] = 99999999
    with execution._locked():
        execution._write(rec2)
    assert core._prepare_exec(other)[0] == 0
    shown = [r for r in execution.list_records(status="aborted") if r["id"] == rec2["id"]]
    assert shown and shown[0]["error"].startswith("执行进程在命令下发前退出")
    execution.resolve(rec["id"], "observed-failed", "确认未执行")


def test_bad_lock_wait_does_not_create_unknown_record():
    """参数写错不该制造"待核实"记录：校验必须在建记录之前。"""
    import execution
    cmd = "rm /nonexistent-opsx-w5-args"
    core.approve(cmd, ttl_seconds=60)
    before = len(execution.list_records(tail=-1))
    try:
        core.exec_change(cmd, lock_wait=-1)
        raise AssertionError("负的锁等待必须拒绝")
    except core.OpsxError as e:
        assert "非负" in str(e)
    assert len(execution.list_records(tail=-1)) == before, "不该留下执行记录"
    assert core._prepare_exec(cmd)[0] == 0, "审批次数也不该被消耗"


def test_command_exit_code_not_confused_with_gate():
    """命令自身的非零退出码不能被当成"没执行"（agent 曾按 5/6 误判）。"""
    import execution
    for target_code in (5, 6):
        cmd = f'sh -c "exit {target_code}"'
        core.approve(cmd, ttl_seconds=60)
        code, _, _ = core.exec_change(cmd)
        assert code == target_code, (cmd, code)
        rec = execution.find_latest(cmd)
        assert rec["status"] == "failed" and rec["exit_code"] == target_code
    # 门禁码与常见命令码不重叠（120/121 在运维工具里罕见）
    assert core.EXEC_ABORT_UNKNOWN not in (1, 2, 3, 4, 5, 6, 126, 127)
    assert core.EXEC_ABORT_LOCK not in (1, 2, 3, 4, 5, 6, 126, 127)


def test_restore_follows_chained_symlinks():
    """a → b → c 的多层软链：要改真正的目标文件，且不能把中间软链换成普通文件。"""
    import subprocess
    import remote
    with tempfile.TemporaryDirectory() as td:
        real = Path(td, "real.conf")
        real.write_text("old\n")
        link2 = Path(td, "link2.conf")
        link2.symlink_to(real)
        link1 = Path(td, "link1.conf")
        link1.symlink_to(link2)
        payload = b"new\n"
        script = remote._render_restore(str(link1), "644", len(payload))
        r = subprocess.run(["/bin/sh", "-c", script], input=payload, capture_output=True)
        assert r.returncode == 0, r.stderr
        assert real.read_bytes() == payload, real.read_bytes()
        assert link1.is_symlink() and link2.is_symlink(), "中间软链不能被替换成普通文件"


def test_restore_verifies_size_before_replacing():
    """提前 EOF（只传到一半）必须在 rename 之前被拒绝，原文件一个字节都不动。"""
    import subprocess
    import remote
    with tempfile.TemporaryDirectory() as td:
        target = Path(td, "app.conf")
        target.write_text("old-content\n")
        payload = b"0123456789"
        script = remote._render_restore(str(target), "600", len(payload))
        r = subprocess.run(["/bin/sh", "-c", script], input=b"abc", capture_output=True)
        assert r.returncode != 0
        assert target.read_text() == "old-content\n", "截断内容不能覆盖目标"
        assert not list(Path(td).glob(".opsx-restore.*"))


def test_restore_without_mode_keeps_existing_permissions():
    """旧快照没有 mode：覆盖已有文件要沿用目标当前权限（不能把 0755 降成 0600）。"""
    import stat as stat_mod
    import subprocess
    import remote
    with tempfile.TemporaryDirectory() as td:
        target = Path(td, "run.sh")
        target.write_text("old\n")
        os.chmod(target, 0o755)
        payload = b"new\n"
        script = remote._render_restore(str(target), "", len(payload))
        r = subprocess.run(["/bin/sh", "-c", script], input=payload, capture_output=True)
        assert r.returncode == 0, r.stderr
        assert stat_mod.S_IMODE(target.stat().st_mode) == 0o755
        # 文件此前不存在（重建）→ 保守 600
        target.unlink()
        r2 = subprocess.run(["/bin/sh", "-c", script], input=payload, capture_output=True)
        assert r2.returncode == 0, r2.stderr
        assert stat_mod.S_IMODE(target.stat().st_mode) == 0o600


def test_exhausted_stamp_not_listed_and_r3_limited():
    """已用尽的戳不再显示为可用授权；R3 不可逆操作只能用一次。"""
    cmd = "rm /nonexistent-opsx-w5-exhausted"
    core.approve(cmd, ttl_seconds=60)
    assert any(a["cmd"] == cmd for a in core.collect_state()["approvals"])
    assert core.consume_stamp(cmd) == 0
    assert not any(a["cmd"] == cmd for a in core.collect_state()["approvals"]), \
        "用尽的戳不能被列成可用授权"
    assert core.stamp_exhausted(cmd) is True   # 但仍能给出"已用尽"的明确提示
    try:
        core.approve("kubectl delete pod r3-once", uses=2, force=True)
        raise AssertionError("R3 不允许一次批准多次执行")
    except core.OpsxError as e:
        assert "R3" in str(e)


def test_rollback_script_fully_rendered_and_verified():
    """走真实 rollback 链路：下发的脚本必须已渲染（无 @@ 占位符），且后验字节数对上。"""
    import subprocess
    import remote
    _write_targets()
    real_run = subprocess.run

    def fake_ssh(name, cmd, stdin=None, timeout=120):
        if cmd.startswith("systemctl restart"):
            return subprocess.CompletedProcess([], 0, b"restarted\n", b"")
        # 其余（快照拉取/stat/恢复脚本）真正执行：残留的 @@ 占位符会在这里炸出来
        assert "@@" not in cmd, cmd[:400]
        r = real_run(["/bin/sh", "-c", cmd], input=stdin or b"", capture_output=True)
        return subprocess.CompletedProcess([], r.returncode, r.stdout, r.stderr)

    orig = remote._ssh
    remote._ssh = fake_ssh
    try:
        core.approve("systemctl restart render-check", ttl_seconds=60, target="web1")
        with tempfile.TemporaryDirectory(prefix="opsx-render-") as td:
            f = Path(td) / "app.conf"
            f.write_text("remote-v1\n")
            os.chmod(f, 0o640)
            code, sid, _ = remote.exec_change("web1", "systemctl restart render-check",
                                              snapshot_files=[str(f)])
            assert code == 0 and sid
            meta = json.loads(Path(core.STATE_DIR, "snapshots", sid, "meta.json").read_text())
            assert meta["items"][0]["mode"] == 0o640      # 快照记录了权限
            f.write_text("changed\n")
            report = remote.rollback(sid, confirm=True)
            assert "已恢复远程文件" in report and "权限 640" in report, report
            assert f.read_text() == "remote-v1\n"        # 内容确实被写回
    finally:
        remote._ssh = orig


def test_rollback_without_backcast_is_uncertain():
    """脚本退出 0 但没带回后验 → 记 unknown，不能宣称校验通过。"""
    import subprocess
    import execution
    import remote
    _write_targets()
    core.approve("systemctl restart no-backcast", ttl_seconds=60, target="web1")

    def fake_ssh(name, cmd, stdin=None, timeout=120):
        resolved = _fake_resolved_response(cmd)
        if resolved is not None:
            return resolved
        if cmd.startswith("systemctl restart"):
            return subprocess.CompletedProcess([], 0, b"restarted\n", b"")
        if ".opsx-restore." in cmd:
            return subprocess.CompletedProcess([], 0, b"", b"")   # 后验被吞掉
        if "OPSX-EXISTS" in cmd:
            # 目标机当前内容（回滚前副本要读它）
            return subprocess.CompletedProcess([], 0, b"OPSX-EXISTS\nv2\n", b"")
        return subprocess.CompletedProcess([], 0, b"ok\n", b"")

    orig = remote._ssh
    remote._ssh = fake_ssh
    try:
        with tempfile.TemporaryDirectory(prefix="opsx-nobc-") as td:
            f = Path(td) / "app.conf"
            f.write_text("v1\n")
            code, sid, _ = remote.exec_change("web1", "systemctl restart no-backcast",
                                              snapshot_files=[str(f)])
            assert code == 0 and sid
            try:
                remote.rollback(sid, confirm=True)
                raise AssertionError("缺少后验时不应宣告成功")
            except remote.RemoteError as e:
                assert "无法确认" in str(e)
            rec = [r for r in execution.list_records(tail=10)
                   if r["kind"] == "rollback" and r["snapshot_id"] == sid]
            assert rec and rec[0]["status"] == "unknown", rec
    finally:
        remote._ssh = orig


def test_execution_authorize_phase_is_not_unknown():
    """快照已完成但授权还没消耗时进程被杀：命令没下发，应记 aborted 而不是 unknown。"""
    import execution
    cmd = "rm /nonexistent-opsx-w5-authorize"
    core.approve(cmd, ttl_seconds=60)
    rec = execution.begin("exec", cmd, None, core.local_env(), phase="authorize")
    rec["pid"] = 99999999
    with execution._locked():
        execution._write(rec)
    shown = execution.get(rec["id"])
    assert shown["status"] == "aborted" and "命令未执行" in shown["error"]
    assert core._prepare_exec(cmd)[0] == 0, "没下发的命令不该挡住重试"


def test_agent_exec_reports_only_current_run():
    """agent 侧：被门禁挡下时不能把上一条成功记录的状态贴到本次结果上。"""
    from agent import tools as agent_tools
    ctx = agent_tools.ToolContext()
    cmd = "rm /nonexistent-opsx-agent-since"
    core.approve(cmd, ttl_seconds=60)
    out = agent_tools.dispatch("ops_exec", {"command": cmd}, ctx)
    assert "[失败]" in out and "exe-" in out          # 第一次：真的执行了（失败）
    out2 = agent_tools.dispatch("ops_exec", {"command": cmd}, ctx)
    assert "[opsx 拒绝]" in out2, out2                 # 第二次：戳用尽，被挡
    assert "exe-" not in out2, f"不该出现上一次的 execution_id: {out2}"


# W5 review regressions: core
def test_w5_review_corrupt_approval_counts_fail_closed():
    from unittest.mock import patch
    cmd = 'printf w5-count-sentinel'
    core.approve(cmd, ttl_seconds=60)
    path = core.STATE_DIR / 'approvals' / (core.cmd_hash(cmd) + '.json')
    original = json.loads(path.read_text())
    for fields in ({'used': -1}, {'used': '1'}, {'used': True}, {'uses': 0},
                   {'uses': False}, {'uses': 101}, {'cmd': 1}):
        path.write_text(json.dumps(dict(original, **fields)))
        assert not core.check_stamp(cmd), fields
        assert core.consume_stamp(cmd) is None, fields
    # 老格式缺计数字段只兼容一次，不能因损坏计数重新获得额度。
    legacy = dict(original)
    legacy.pop('used'); legacy.pop('uses')
    path.write_text(json.dumps(legacy))
    assert core.consume_stamp(cmd) == 0 and core.consume_stamp(cmd) is None
    irreversible = 'rm -rf /nonexistent-w5-review-r3'
    core.approve(irreversible, force=True)
    path = core.STATE_DIR / 'approvals' / (core.cmd_hash(irreversible) + '.json')
    record = json.loads(path.read_text()); record['uses'] = 2
    path.write_text(json.dumps(record))
    assert not core.check_stamp(irreversible)
    assert core.consume_stamp(irreversible) is None


def test_w5_review_approval_rechecks_environment_after_snapshot():
    from unittest.mock import patch
    import execution
    with tempfile.TemporaryDirectory() as td:
        target = Path(td, 'app.conf'); target.write_text('before')
        command = f'echo after > {target}'
        core.approve(command)
        original = core._snapshot_file_into
        def change_context(*args):
            original(*args)
            os.environ['KUBECONFIG'] = str(Path(td, 'other-context'))
        with patch.dict(os.environ), patch.object(core, '_snapshot_file_into', side_effect=change_context):
            code, sid, output = core.exec_change(command, snapshot_files=[str(target)])
        assert code == 2 and sid and target.read_text() == 'before', (code, output)
        assert execution.find_latest(command)['status'] == 'aborted'
        assert core.check_stamp(command), '没有下发，审批次数应保留'


def test_w5_review_target_aliases_share_lock():
    restore = _with_targets({'w5-a': {'host': 'same.example.', 'user': 'ops'},
                             'w5-b': {'host': 'SAME.EXAMPLE', 'user': 'deploy', 'port': 22},
                             'w5-c': {'host': 'SAME.EXAMPLE', 'port': 2222}})
    try:
        assert core._lock_path('w5-a') == core._lock_path('w5-b')
        assert core._lock_path('w5-a') != core._lock_path('w5-c')
        with core.target_lock('w5-a'):
            try:
                with core.target_lock('w5-b'):
                    raise AssertionError('相同host+port的不同名称不能并发写')
            except core.TargetBusy:
                pass
            with core.target_lock('w5-c'):
                pass
    finally:
        restore()


def test_w5_review_target_lock_released_on_setup_interrupt():
    from unittest.mock import patch
    # 在yield以前被中断也要解锁，否则REPL继续存活时该目标永久卡住。
    with patch.object(core, '_safe_audit', side_effect=KeyboardInterrupt):
        try:
            with core.target_lock(None):
                raise AssertionError('不应进入调用体')
        except KeyboardInterrupt:
            pass
    with core.target_lock(None):
        pass
    # 等待flock时中断，同样需要关闭已打开的fd。
    opened = []
    real_open = os.open
    def capture(*args, **kwargs):
        fd = real_open(*args, **kwargs); opened.append(fd); return fd
    with patch.object(core.os, 'open', side_effect=capture), \
            patch.object(core.fcntl, 'flock', side_effect=BlockingIOError), \
            patch.object(core.time, 'sleep', side_effect=KeyboardInterrupt):
        try:
            with core.target_lock(None, wait=1):
                raise AssertionError('不应进入调用体')
        except KeyboardInterrupt:
            pass
    assert opened
    try:
        os.fstat(opened[-1])
        raise AssertionError('等待中断后fd泄漏')
    except OSError:
        pass


def test_w5_review_prune_cannot_delete_concurrent_new_approval():
    import threading
    from unittest.mock import patch
    command = 'printf w5-prune-race'
    core.approve(command)
    path = core.STATE_DIR / 'approvals' / (core.cmd_hash(command) + '.json')
    old = json.loads(path.read_text()); old['created'] -= 3600; path.write_text(json.dumps(old))
    reading, release, attempting, finished = [threading.Event() for _ in range(4)]
    errors = []
    live = core._stamp_live
    def slow_live(stamp):
        if isinstance(stamp, dict) and stamp.get('cmd') == command and stamp['created'] == old['created']:
            reading.set()
            if not release.wait(3):
                raise AssertionError('test release timeout')
        return live(stamp)
    def prune():
        try: core.prune_approvals()
        except BaseException as error: errors.append(error)
    def approve():
        attempting.set()
        try: core.approve(command)
        except BaseException as error: errors.append(error)
        finally: finished.set()
    with patch.object(core, '_stamp_live', side_effect=slow_live):
        cleaner = threading.Thread(target=prune); writer = threading.Thread(target=approve)
        cleaner.start()
        try:
            assert reading.wait(3)
            writer.start(); assert attempting.wait(3)
            assert not finished.wait(0.1), '清理未与批准互斥，会删除新授权'
        finally:
            release.set(); cleaner.join(3)
            if writer.ident is not None: writer.join(3)
    assert not errors, errors
    assert not cleaner.is_alive() and not writer.is_alive()
    assert core.check_stamp(command), '并发新批准不能被旧清理删除'


def test_w5_review_snapshot_commands_cannot_bypass_execution_protocol():
    from unittest.mock import patch
    import remote
    with tempfile.TemporaryDirectory() as td:
        victim = Path(td, 'must-not-write')
        command = f'echo changed > {victim}'
        core.approve(command)
        try:
            core.snapshot_cmd(command)
            raise AssertionError('已批准的写命令也不能作为采集执行')
        except core.OpsxError as error:
            assert 'R0' in str(error)
        assert not victim.exists() and core.check_stamp(command)
        restore = _with_targets({'w5-snapshot': {'host': 'fake.example'}})
        try:
            core.approve(command, target='w5-snapshot')
            with patch.object(remote, '_ssh') as ssh:
                try:
                    remote.snapshot_cmd('w5-snapshot', command)
                    raise AssertionError('远程写采集也必须拒绝')
                except remote.RemoteError as error:
                    assert 'R0' in str(error)
                ssh.assert_not_called()
        finally:
            restore()


def test_w5_review_local_rollback_permissions_and_lock_record():
    import execution
    with tempfile.TemporaryDirectory() as td:
        target = Path(td, 'app.conf'); target.write_text('before'); target.chmod(0o755)
        sid = core.snapshot_file(str(target)); target.write_text('after'); target.chmod(0o751)
        metadata = core.STATE_DIR / 'snapshots' / sid / 'meta.json'
        record = json.loads(metadata.read_text()); record['items'][0].pop('mode')
        metadata.write_text(json.dumps(record))
        core.rollback(sid, confirm=True)
        assert target.read_text() == 'before' and stat_mode(target) == 0o751
        target.unlink(); core.rollback(sid, confirm=True)
        assert stat_mode(target) == 0o600
        record['items'][0]['mode'] = 0; metadata.write_text(json.dumps(record))
        core.rollback(sid, confirm=True)
        assert stat_mode(target) == 0
        target.chmod(0o600); target.write_text('unchanged')
        with core.target_lock(None):
            try:
                core.rollback(sid, confirm=True)
                raise AssertionError('回滚不能并发修改本机')
            except core.TargetBusy:
                pass
        latest = execution.find_latest(f'rollback {sid}', kind='rollback')
        assert latest['status'] == 'aborted' and target.read_text() == 'unchanged'


def test_w5_review_unknown_retains_snapshots_and_rollback_backup():
    import execution
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, patch.object(core, 'STATE_DIR', Path(td)):
        target = Path(td, 'app'); target.write_text('old')
        sid = core.snapshot_file(str(target))
        target.write_text('current')
        _, meta = core.load_snapshot(sid)
        backup = core._rollback_backup(sid, None, meta['items'], core._local_read_state)
        record = execution.begin('exec', 'printf uncertain', phase='exec', snapshot_id=sid)
        execution.finish(record['id'], 'unknown')
        core.prune_snapshots(keep=0)
        assert core.snapshot_dir(sid).is_dir() and core.snapshot_dir(backup).is_dir()
        execution.resolve(record['id'], 'observed-failed')
        core.prune_snapshots(keep=0)
        assert not core.snapshot_dir(sid).exists() and not core.snapshot_dir(backup).exists()




# W5 review regressions: execution
def test_w5_review_execution_records_redact_before_storage_and_clipping():
    import execution
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, patch.object(core, 'STATE_DIR', Path(td)):
        secret = 'EXECUTION-SECRET-SENTINEL'
        cmd = 'printf password=' + secret
        rec = execution.begin('exec', cmd, env={'host': 'test', 'auth': secret})
        path = execution._path(rec['id'])
        assert secret not in path.read_text()
        assert execution.get(rec['id'])['cmd_hash'] == core.cmd_hash(cmd)
        pem = '-----BEGIN PRIVATE KEY-----\n' + 'PRIVATE-BODY-SENTINEL\n' * 100 + '-----END PRIVATE KEY-----'
        execution.finish(rec['id'], 'failed', output=pem, error='password=' + secret)
        assert 'PRIVATE-BODY-SENTINEL' not in path.read_text()
        assert secret not in path.read_text()
        # 历史明文记录也不能通过新查询出口读进模型。
        rec.update(status='failed', error='password=' + secret)
        path.write_text(json.dumps(rec))
        assert secret not in json.dumps(execution.get(rec['id']))
        assert secret not in json.dumps(execution.list_records())
        # 同 pid 遗留临时文件权限不应被 rename 带到正式记录。
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        tmp.write_text('old')
        tmp.chmod(0o644)
        with execution._locked():
            execution._write(rec)
        assert path.stat().st_mode & 0o777 == 0o600



def test_w5_review_corrupt_execution_records_block_replay_and_survive_pruning():
    import execution
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, patch.object(core, 'STATE_DIR', Path(td)):
        cmd = 'systemctl restart protected-test-service'
        rec = execution.begin('exec', cmd, env=core.local_env())
        execution.finish(rec['id'], 'unknown')
        rec = execution.get(rec['id'])
        path = execution._path(rec['id'])
        corrupt = ['{broken', json.dumps(dict(rec, id='exe-other-record')),
                   json.dumps(dict(rec, started_ts='soon')),
                   json.dumps(dict(rec, started_ts=float('nan'))),
                   json.dumps(dict(rec, started_ts=10 ** 1000)),
                   json.dumps(dict(rec, cmd_hash=None)), json.dumps(dict(rec, env=[]))]
        for text in corrupt:
            path.write_text(text)
            os.utime(path, (1, 1))
            assert execution.list_records(tail=-1) == []
            try:
                execution.pending_unknown(None, core.cmd_hash(cmd))
                raise AssertionError('坏记录不能当作没有未核实变更')
            except core.OpsxError as e:
                assert str(path) in str(e) and '拒绝执行' in str(e)
            execution.prune(keep=0)
            assert path.is_file(), '不能随时间清除不确定性，自动放开重放'
        path.write_text(json.dumps(rec))
        assert execution.pending_unknown(None, core.cmd_hash(cmd))['id'] == rec['id']



def test_w5_review_invalid_resolution_does_not_clear_unknown():
    import execution
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, patch.object(core, 'STATE_DIR', Path(td)):
        cmd = 'systemctl restart unresolved-test-service'
        rec = execution.begin('exec', cmd, env=core.local_env())
        execution.finish(rec['id'], 'unknown')
        path = execution._path(rec['id'])
        original = json.loads(path.read_text())
        for invalid in (True, 'yes', [True], {'outcome': 'observed-ok'},
                        {'outcome': 'anything', 'at': '2026-10-22 10:00:00'}):
            path.write_text(json.dumps(dict(original, resolved=invalid)))
            assert execution.pending_unknown(None, core.cmd_hash(cmd))['id'] == rec['id']
        execution.resolve(rec['id'], 'observed-failed', '确认未生效')
        assert execution.pending_unknown(None, core.cmd_hash(cmd)) is None



def test_w5_review_unknown_blocks_same_host_aliases_and_legacy_secret_commands():
    import execution
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, patch.object(core, 'STATE_DIR', Path(td)):
        cmd = 'systemctl restart app password=ALIAS-SECRET-SENTINEL'
        env = {'scope': 'remote', 'host': 'Host.Example.', 'port': 22, 'user': 'ops'}
        rec = execution.begin('exec', cmd, 'first', env)
        execution.finish(rec['id'], 'unknown')
        alias_env = dict(env, host='host.example', port='22', user='admin')
        def pending(candidate=alias_env, command=cmd):
            return execution.pending_unknown('alias', core.cmd_hash(command, 'alias'),
                                             env=candidate, command=command)
        assert pending()['id'] == rec['id']
        assert pending(dict(alias_env, host='other.example')) is None
        assert pending(dict(alias_env, port=2222)) is None
        assert pending(command='systemctl restart other') is None
        # 旧记录不含 independent hash；即使 cmd 已脱敏，也应按本次原文匹配旧 target hash。
        legacy = execution.get(rec['id'])
        legacy.pop('command_hash')
        with execution._locked():
            execution._write(legacy)
        assert 'ALIAS-SECRET-SENTINEL' not in execution._path(rec['id']).read_text()
        assert pending()['id'] == rec['id']
        # target=None 是本机，不是“所有远程目标”。
        assert execution.pending_unknown(None) is None



def test_w5_review_find_latest_filters_process_kind_and_invocation_start():
    import execution
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, patch.object(core, 'STATE_DIR', Path(td)):
        cmd = 'systemctl restart correlated-test-service'
        mine = execution.begin('exec', cmd, env=core.local_env())
        other = execution.begin('exec', cmd, env=core.local_env())
        rollback = execution.begin('rollback', cmd, env=core.local_env())
        for rec, offset in ((mine, 0), (other, 1), (rollback, 2)):
            rec.update(started_ts=mine['started_ts'] + offset, status='aborted')
            if rec is other:
                rec['pid'] = os.getpid() + 100000
            with execution._locked():
                execution._write(rec)
        assert execution.find_latest(cmd)['id'] == rollback['id']
        filtered = execution.find_latest(cmd, since=mine['started_ts'],
                                         pid=os.getpid(), kind='exec')
        assert filtered['id'] == mine['id']
        assert execution.find_latest(cmd, since=mine['started_ts'] + .5,
                                     pid=os.getpid(), kind='exec') is None





# W5 review regressions: interfaces
def test_w5_review_mcp_rejected_exec_never_reuses_old_execution():
    import mcp_server
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix='opsx-w5-mcp-current-') as td, \
            patch.object(core, 'STATE_DIR', Path(td)):
        cmd = 'printf w5-mcp-current'
        core.approve(cmd)
        first = json.loads(mcp_server.call_tool('ops_exec', {'command': cmd})['content'][0]['text'])
        assert first['exit_code'] == 0 and first['execution_status'] == 'succeeded'
        second = json.loads(mcp_server.call_tool('ops_exec', {'command': cmd})['content'][0]['text'])
        assert second['exit_code'] == 2
        assert 'execution_id' not in second and 'execution_status' not in second



def test_w5_review_execution_interfaces_ignore_another_process_record():
    import execution
    import mcp_server
    import runpy
    from unittest.mock import patch
    from agent import tools as agent_tools
    cli = runpy.run_path(str(core.REPO_DIR / 'opsx'))
    own, other = [], []

    def fake_change(cmd, *args, **kwargs):
        current = execution.begin('exec', cmd, env=core.local_env())
        execution.finish(current['id'], 'succeeded', exit_code=0)
        foreign = execution.begin('exec', cmd, env=core.local_env(), phase='lock')
        foreign['pid'] = os.getpid() + 100000
        with execution._locked():
            execution._write(foreign)
        execution.finish(foreign['id'], 'aborted', error='another process could not acquire lock')
        own.append(current['id'])
        other.append(foreign['id'])
        return 0, None, 'done'

    with tempfile.TemporaryDirectory(prefix='opsx-w5-own-record-') as td, \
            patch.object(core, 'STATE_DIR', Path(td)), \
            patch.object(core, 'exec_change', side_effect=fake_change):
        cmd = 'systemctl restart w5-own-record'
        agent = agent_tools.dispatch('ops_exec', {'command': cmd}, agent_tools.ToolContext())
        assert own[-1] in agent and other[-1] not in agent and '[完成]' in agent
        payload = json.loads(mcp_server.call_tool('ops_exec', {'command': cmd})['content'][0]['text'])
        assert payload['execution_id'] == own[-1] and payload['execution_status'] == 'succeeded'
        stdout = io.StringIO()
        with patch.object(sys, 'stdout', stdout):
            assert cli['main'](['exec', cmd]) == 0
        assert own[-1] in stdout.getvalue() and other[-1] not in stdout.getvalue()



def test_w5_review_execution_queries_reject_zero_and_negative_tail():
    import mcp_server
    from agent import tools as agent_tools
    for tail in (0, -1):
        text = agent_tools.dispatch('ops_executions', {'tail': tail}, agent_tools.ToolContext())
        assert 'tail 须为正整数' in text
        result = mcp_server.handle_message({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                           'params': {'name': 'ops_executions',
                                                      'arguments': {'tail': tail}}})['result']
        assert result['isError'] and 'tail 须为正整数' in result['content'][0]['text']



def test_w5_review_agent_unknown_and_aborted_results_are_distinct():
    import execution
    from unittest.mock import patch
    from agent import tools as agent_tools
    created = []

    def fake_change(cmd, *args, **kwargs):
        unknown = 'unknown' in cmd
        rec = execution.begin('exec', cmd, env=core.local_env(), phase='exec' if unknown else 'authorize')
        execution.finish(rec['id'], 'unknown' if unknown else 'aborted', error='not confirmed')
        created.append(rec['id'])
        return (255 if unknown else 2), (None if unknown else 'snapshot-preserved'), 'not confirmed'

    with tempfile.TemporaryDirectory(prefix='opsx-w5-result-label-') as td, \
            patch.object(core, 'STATE_DIR', Path(td)), \
            patch.object(core, 'exec_change', side_effect=fake_change):
        unknown = agent_tools.dispatch('ops_exec', {'command': 'systemctl restart unknown'},
                                       agent_tools.ToolContext())
        assert unknown.startswith('[结果未知]') and '[失败]' not in unknown
        assert created[-1] in unknown
        aborted = agent_tools.dispatch('ops_exec', {'command': 'systemctl restart aborted'},
                                       agent_tools.ToolContext())
        assert '本次未执行' in aborted and created[-1] in aborted
        assert 'snapshot-preserved' in aborted



def test_w5_review_rollback_interfaces_report_current_execution_on_failure():
    import execution
    import mcp_server
    import remote
    import runpy
    from unittest.mock import patch
    from agent import tools as agent_tools
    cli = runpy.run_path(str(core.REPO_DIR / 'opsx'))
    sid = 'snapshot-w5-interface'
    ids, fail = [], [False]

    def fake_rollback(snapshot_id, confirm=False):
        assert confirm
        rec = execution.begin('rollback', f'rollback {snapshot_id}', env=core.local_env(),
                              snapshot_id=snapshot_id, phase='restore')
        ids.append(rec['id'])
        execution.finish(rec['id'], 'unknown' if fail[0] else 'succeeded')
        if fail[0]:
            raise remote.RemoteError('写入结果无法确认')
        return '已恢复'

    plan = {'target': None, 'snapshot_id': sid, 'files': [], 'commands': []}
    with tempfile.TemporaryDirectory(prefix='opsx-w5-rollback-reference-') as td, \
            patch.object(core, 'STATE_DIR', Path(td)), \
            patch.object(remote, 'rollback_plan', return_value=plan), \
            patch.object(remote, 'rollback', side_effect=fake_rollback):
        for failure in (False, True):
            fail[0] = failure
            text = agent_tools.dispatch('ops_rollback', {'snapshot_id': sid},
                                        agent_tools.ToolContext())
            assert ids[-1] in text and ('unknown' if failure else 'succeeded') in text
            result = mcp_server.call_tool('ops_rollback', {'snapshot_id': sid, 'confirm': True})
            if failure:
                assert result['isError'] and ids[-1] in result['content'][0]['text']
            else:
                payload = json.loads(result['content'][0]['text'])
                assert payload['execution_id'] == ids[-1] and payload['execution_status'] == 'succeeded'
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr):
                assert cli['main'](['rollback', sid, '--yes']) == (1 if failure else 0)
            assert ids[-1] in stdout.getvalue() and ('unknown' if failure else 'succeeded') in stdout.getvalue()



def test_w5_review_hook_requires_guarded_exec_even_with_approval():
    import shlex
    import subprocess
    with tempfile.TemporaryDirectory(prefix='opsx-w5-hook-') as td:
        target = Path(td) / 'app.conf'
        target.write_text('before')
        state = Path(td) / 'state'
        env = dict(os.environ, OPSX_STATE_DIR=str(state))
        entry = str(core.REPO_DIR / 'opsx')
        command = f'printf after > {shlex.quote(str(target))}'
        approved = subprocess.run([sys.executable, entry, 'approve', command], env=env,
                                   capture_output=True, text=True)
        assert approved.returncode == 0, approved.stderr
        stamp = next((state / 'approvals').glob('*.json'))
        for _ in range(2):
            denied = subprocess.run([sys.executable, entry, 'hook'], env=env,
                                    input=json.dumps({'tool_input': {'command': command}}),
                                    capture_output=True, text=True)
            assert denied.returncode == 2 and '必须通过 opsx exec' in denied.stderr
            assert json.loads(stamp.read_text())['used'] == 0
        wrapper = shlex.join([sys.executable, entry, 'exec', command])
        allowed = subprocess.run([sys.executable, entry, 'hook'], env=env,
                                 input=json.dumps({'tool_input': {'command': wrapper}}),
                                 capture_output=True, text=True)
        assert allowed.returncode == 0 and json.loads(stamp.read_text())['used'] == 0
        readonly = subprocess.run([sys.executable, entry, 'hook'], env=env,
                                  input=json.dumps({'tool_input': {'command': 'uname -s'}}),
                                  capture_output=True, text=True)
        assert readonly.returncode == 0 and target.read_text() == 'before'
        executed = subprocess.run([sys.executable, entry, 'exec', command], env=env,
                                  capture_output=True, text=True)
        assert executed.returncode == 0 and target.read_text() == 'after'
        assert json.loads(stamp.read_text())['used'] == 1





# W5 review regressions: remote
def test_w5_review_remote_symlink_snapshots_keep_real_file_mode():
    import subprocess
    from unittest.mock import patch
    import remote
    _write_targets()
    def local_ssh(name, cmd, stdin=None, timeout=120):
        return subprocess.run(['/bin/sh', '-c', cmd], input=stdin or b'', capture_output=True)
    with tempfile.TemporaryDirectory() as td, patch.object(remote, '_ssh', side_effect=local_ssh):
        real = Path(td, 'secret.conf')
        real.write_bytes(b'original')
        real.chmod(0o600)
        link = Path(td, 'link.conf')
        link.symlink_to(real)
        sid = remote.snapshot_file('web1', str(link))
        _, meta = core.load_snapshot(sid)
        assert meta['items'][0]['mode'] == 0o600
        real.write_bytes(b'changed')
        report = remote.rollback(sid, confirm=True)
        assert real.read_bytes() == b'original' and link.is_symlink()
        assert real.stat().st_mode & 0o7777 == 0o600
        backup = re.search(r'回滚前副本: ([^（\s]+)', report).group(1)
        _, backup_meta = core.load_snapshot(backup)
        assert backup_meta['machine'] == meta['machine']



def test_w5_review_remote_transport_stays_on_approved_target():
    import subprocess
    from unittest.mock import patch
    import execution
    import remote
    for change in (False, True):
        registry = _write_targets()
        hosts = []
        def ssh(name, cmd, stdin=None, timeout=120):
            host = remote.ssh_argv(name, cmd)[-2]
            hosts.append(host)
            if 'OPSX-RESOLVED' in cmd:
                data = b'OPSX-RESOLVED\x00/etc/app.conf\x00'
            elif cmd.startswith('cat --'):
                targets = json.loads(registry.read_text())
                targets[name]['host'] = '10.0.0.99'
                registry.write_text(json.dumps(targets))
                data = b'original'
            elif cmd.startswith('stat '):
                data = b'600 ops:ops\n'
            elif 'OPSX-HOST ' in cmd:
                data = f'OPSX-HOST {host}\nOPSX-MACHINE-ID abcdef0123456789\n'.encode()
            else:
                data = b'changed'
            return subprocess.CompletedProcess([], 0, data, b'')
        cmd = 'systemctl restart fixed-target-review'
        with patch.object(remote, '_ssh', side_effect=ssh):
            if change:
                core.approve(cmd, ttl_seconds=60, target='web1')
                code, sid, _ = remote.exec_change('web1', cmd, snapshot_files=['/etc/app.conf'])
                assert code == 0
                assert execution.find_latest(cmd, 'web1')['env']['host'] == '10.0.0.11'
            else:
                sid = remote.snapshot_file('web1', '/etc/app.conf')
        assert hosts and set(hosts) == {'10.0.0.11'}, hosts
        assert core.load_snapshot(sid)[1]['env']['host'] == '10.0.0.11'
        assert remote.get_target('web1')['host'] == '10.0.0.99'
    _write_targets()



def test_w5_review_remote_machine_probe_must_match_and_parse_markers():
    import subprocess
    from unittest.mock import patch
    import remote
    _write_targets()
    sid, directory = core.begin_snapshot()
    data = directory / 'data'
    data.write_bytes(b'archived')
    meta = {'id': sid, 'target': 'web1', 'env': remote.target_env('web1'), 'machine': 'node/id',
            'items': [{'type': 'file', 'src': '/etc/app.conf', 'dest': 'data', 'mode': 0o600}]}
    (directory / 'meta.json').write_text(json.dumps(meta))
    with patch.object(remote, '_target_machine', return_value=''), patch.object(remote, '_ssh') as ssh:
        try:
            remote.rollback(sid, confirm=True)
            raise AssertionError('已记录指纹但当前无法采集时必须拒绝回滚')
        except remote.RemoteError as error:
            assert '无法确认' in str(error)
        ssh.assert_not_called()
    response = subprocess.CompletedProcess([], 0,
        b'identical login banner\nidentical footer\nOPSX-HOST node-a\nOPSX-MACHINE-ID abcdef0123456789\n', b'')
    with patch.object(remote, '_ssh', return_value=response):
        assert remote._target_machine('web1') == 'node-a/abcdef012345'
    response = subprocess.CompletedProcess([], 0, b'888 impossible-mode\n', b'')
    with patch.object(remote, '_ssh', return_value=response):
        assert remote._target_stat('web1', '/etc/app.conf') == (None, '')



def test_w5_review_restore_rejects_deep_links_and_preserves_zero_mode():
    import subprocess
    import remote
    with tempfile.TemporaryDirectory() as td:
        real = Path(td, 'real')
        real.write_bytes(b'old')
        current = real
        for index in range(10):
            link = Path(td, f'link-{index}')
            link.symlink_to(current)
            current = link
        response = subprocess.run(['/bin/sh', '-c', remote._render_restore(str(current), '600', 3)],
                                  input=b'new', capture_output=True)
        assert response.returncode != 0 and real.read_bytes() == b'old'
        assert all(Path(td, f'link-{i}').is_symlink() for i in range(10))
        response = subprocess.run(['/bin/sh', '-c', remote._render_restore(td, '600', 3)],
                                  input=b'new', capture_output=True)
        assert response.returncode != 0 and not list(Path(td).glob('.opsx-restore.*'))
        response = subprocess.run(['/bin/sh', '-c', remote._render_restore(str(real), '0', 3)],
                                  input=b'new', capture_output=True)
        assert response.returncode == 0 and real.stat().st_mode & 0o7777 == 0
        assert remote._parse_restore_info(response.stdout)['mode'] == '0'
        real.chmod(0o600)



def test_w5_review_restore_owner_failure_keeps_original_file():
    import subprocess
    import remote
    with tempfile.TemporaryDirectory() as td:
        real = Path(td, 'app.conf')
        real.write_bytes(b'old')
        bindir = Path(td, 'bin')
        bindir.mkdir()
        chown = bindir / 'chown'
        chown.write_text('#!/bin/sh\nexit 1\n')
        chown.chmod(0o755)
        environment = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        response = subprocess.run(['/bin/sh', '-c', remote._render_restore(str(real), '600', 3)],
                                  input=b'new', capture_output=True, env=environment)
        assert response.returncode != 0
        assert real.read_bytes() == b'old' and not list(Path(td).glob('.opsx-restore.*'))



def test_w5_review_remote_rollback_disconnection_is_unknown():
    import subprocess
    from unittest.mock import patch
    import execution
    import remote
    _write_targets()
    def local_ssh(name, cmd, stdin=None, timeout=120):
        return subprocess.run(['/bin/sh', '-c', cmd], input=stdin or b'', capture_output=True)
    with tempfile.TemporaryDirectory() as td:
        source = Path(td, 'app.conf')
        source.write_bytes(b'archived')
        with patch.object(remote, '_ssh', side_effect=local_ssh):
            sid = remote.snapshot_file('web1', str(source))
        def disconnect(name, cmd, stdin=None, timeout=120):
            if '.opsx-restore.' in cmd:
                return subprocess.CompletedProcess([], 255, b'', b'connection reset')
            return local_ssh(name, cmd, stdin, timeout)
        with patch.object(remote, '_ssh', side_effect=disconnect):
            try:
                remote.rollback(sid, confirm=True)
                raise AssertionError('断线后必须要求核实恢复结果')
            except remote._UncertainChange:
                pass
        records = [r for r in execution.list_records(tail=500)
                   if r['kind'] == 'rollback' and r.get('snapshot_id') == sid]
        assert records[0]['status'] == 'unknown'
    with patch.object(remote, '_ssh', return_value=subprocess.CompletedProcess([], -15, b'', b'')):
        assert remote._run_remote_change('web1', 'true', 1)[1] == 'unknown'



def test_w5_review_remote_restore_checks_permission_postconditions():
    import subprocess
    from unittest.mock import patch
    import execution
    import remote
    _write_targets()
    def local_ssh(name, cmd, stdin=None, timeout=120):
        return subprocess.run(['/bin/sh', '-c', cmd], input=stdin or b'', capture_output=True)
    with tempfile.TemporaryDirectory() as td:
        source = Path(td, 'app.conf')
        source.write_bytes(b'archived')
        source.chmod(0o600)
        with patch.object(remote, '_ssh', side_effect=local_ssh):
            sid = remote.snapshot_file('web1', str(source))
        for suffix in ('had=1 size=8', 'had=1 size=8 mode=644', 'size=8 mode=600',
                       'had=1 size=7 mode=600'):
            def invalid_postcondition(name, cmd, stdin=None, timeout=120):
                if '.opsx-restore.' in cmd:
                    return subprocess.CompletedProcess([], 0, ('opsx-restored ' + suffix).encode(), b'')
                return local_ssh(name, cmd, stdin, timeout)
            with patch.object(remote, '_ssh', side_effect=invalid_postcondition):
                try:
                    remote.rollback(sid, confirm=True)
                    raise AssertionError('后验不完整或权限不匹配不能宣告成功')
                except remote._UncertainChange:
                    pass
            records = [r for r in execution.list_records(tail=500)
                       if r['kind'] == 'rollback' and r.get('snapshot_id') == sid]
            assert records[0]['status'] == 'unknown'



def test_w5_review_remote_backup_interruption_is_aborted():
    import subprocess
    from unittest.mock import patch
    import execution
    import remote
    _write_targets()
    def local_ssh(name, cmd, stdin=None, timeout=120):
        return subprocess.run(['/bin/sh', '-c', cmd], input=stdin or b'', capture_output=True)
    with tempfile.TemporaryDirectory() as td, patch.object(remote, '_ssh', side_effect=local_ssh):
        source = Path(td, 'app.conf')
        source.write_bytes(b'archived')
        sid = remote.snapshot_file('web1', str(source))
        with patch.object(core, '_rollback_backup', side_effect=KeyboardInterrupt):
            try:
                remote.rollback(sid, confirm=True)
                raise AssertionError('用户中断应传给调用方')
            except KeyboardInterrupt:
                pass
        records = [r for r in execution.list_records(tail=500)
                   if r['kind'] == 'rollback' and r.get('snapshot_id') == sid]
        assert records[0]['status'] == 'aborted'





def test_w5_review_failed_terminal_write_blocks_live_process_replay():
    import execution
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, patch.object(core, 'STATE_DIR', Path(td)):
        cmd = 'systemctl restart terminal-write-test'
        core.approve(cmd, uses=2)
        write = execution._write
        sent = []
        def fail_finish(record):
            if record['status'] != 'running':
                raise OSError('disk full')
            return write(record)
        def fake_backend(command, timeout):
            sent.append(command)
            current = execution.find_latest(command)
            assert current['status'] == 'running', '正常持锁的执行不能误报unknown'
            try:
                execution.resolve(current['id'], 'observed-failed')
                raise AssertionError('活动执行不能提前核实')
            except core.OpsxError:
                pass
            return 0, 'succeeded', 'done', None
        with patch.object(core, '_run_change', side_effect=fake_backend), \
                patch.object(execution, '_write', side_effect=fail_finish):
            try:
                core.exec_change(cmd)
                raise AssertionError('终态写盘失败应显式报错')
            except OSError:
                pass
        assert execution.find_latest(cmd)['status'] == 'unknown'
        with patch.object(core, '_run_change', side_effect=fake_backend):
            assert core.exec_change(cmd)[0] == core.EXEC_ABORT_UNKNOWN
        assert sent == [cmd], '同一活pid下的遗留running不能重放'
        # 即使另一条新调用已经持有同一目标锁，也不能把它当成旧执行仍然活动。
        with core.target_lock(None, execution_id='exe-different-owner'):
            assert execution.find_latest(cmd)['status'] == 'unknown'



def test_w5_review_execution_lock_probe_is_read_only_and_supports_legacy():
    import execution
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as td, patch.object(core, 'STATE_DIR', Path(td)):
        env = {'scope': 'remote', 'host': 'original.example', 'port': 22}
        rec = execution.begin('exec', 'systemctl restart legacy-active', 'old-name', env)
        assert execution.get(rec['id'])['status'] == 'unknown'
        assert not (core.STATE_DIR / 'locks').exists(), '只读查询不能创建锁文件'
        legacy = core.STATE_DIR / 'locks' / 'old-name.lock'
        legacy.parent.mkdir()
        fd = os.open(legacy, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            execution.fcntl.flock(fd, execution.fcntl.LOCK_EX)
            os.write(fd, json.dumps({'pid': os.getpid(), 'execution_id': rec['id']}).encode())
            assert execution.get(rec['id'])['status'] == 'running'
            # 旧锁没有execution_id时仍认pid，兼容已运行的旧版本。
            os.ftruncate(fd, 0); os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps({'pid': os.getpid()}).encode())
            assert execution.get(rec['id'])['status'] == 'running'
        finally:
            os.close(fd)
        assert execution.get(rec['id'])['status'] == 'unknown'
        execution.resolve(rec['id'], 'observed-failed')
        assert execution.get(rec['id'])['resolved']['outcome'] == 'observed-failed'



def test_w5_review_remote_rollback_rejects_retargeted_file_and_parent_links():
    import subprocess
    from unittest.mock import patch
    import remote
    _write_targets()
    for kind in ('file', 'directory', 'late'):
        with tempfile.TemporaryDirectory() as td:
            directory_a, directory_b = Path(td, 'a'), Path(td, 'b')
            directory_a.mkdir()
            directory_b.mkdir()
            file_a, file_b = directory_a / 'app.conf', directory_b / 'app.conf'
            file_a.write_bytes(b'archived A')
            file_b.write_bytes(b'unrelated B')
            link = Path(td, 'current')
            link.symlink_to(directory_a if kind == 'directory' else file_a)
            source = link / 'app.conf' if kind == 'directory' else link
            def retarget():
                link.unlink()
                link.symlink_to(directory_b if kind == 'directory' else file_b)
            def local_ssh(name, cmd, stdin=None, timeout=120):
                if kind == 'late' and '.opsx-restore.' in cmd:
                    retarget()
                return subprocess.run(['/bin/sh', '-c', cmd], input=stdin or b'', capture_output=True)
            with patch.object(remote, '_ssh', side_effect=local_ssh):
                sid = remote.snapshot_file('web1', str(source))
                assert core.load_snapshot(sid)[1]['items'][0]['resolved_path'] == str(file_a.resolve())
                file_a.write_bytes(b'current A')
                if kind != 'late':
                    retarget()
                try:
                    remote.rollback(sid, confirm=True)
                    raise AssertionError('软链改向必须拒绝，不能把 A 的旧内容写进 B')
                except remote.RemoteError as error:
                    assert '真实路径已改变' in str(error) or 'opsx-restore-path-changed' in str(error)
            assert file_a.read_bytes() == b'current A'
            assert file_b.read_bytes() == b'unrelated B'
            assert link.is_symlink()



def test_w5_review_legacy_alias_lock_and_owner_write_failure():
    from unittest.mock import patch
    import execution
    import fcntl
    restore = _with_targets({'w5-old-alias': {'host': 'same.example'},
                             'w5-new-alias': {'host': 'same.example'}})
    try:
        directory = core.STATE_DIR / 'locks'; directory.mkdir(exist_ok=True)
        old = os.open(directory / 'w5-old-alias.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with core.target_lock('w5-new-alias'):
                    raise AssertionError('新版规范锁不能绕过旧版同机别名锁')
            except core.TargetBusy:
                pass
        finally:
            os.close(old)
        with core.target_lock('w5-new-alias'):
            pass
    finally:
        restore()
    command = 'printf w5-owner-short-write'
    core.approve(command)
    with patch.object(core.os, 'write', return_value=0), patch.object(core, '_run_change') as backend:
        try:
            core.exec_change(command)
            raise AssertionError('锁owner写不完整不得启动变更')
        except core.OpsxError:
            pass
        backend.assert_not_called()
    assert execution.find_latest(command)['status'] == 'aborted'
    assert core.check_stamp(command), '命令未下发，不消耗授权'
    with core.target_lock(None):
        pass


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failed = 0
    for fn in ALL:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except (Exception, SystemExit) as e:  # noqa: BLE001 —— SystemExit 也不能终止整轮自检
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(ALL) - failed}/{len(ALL)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
