"""opsx 自检：python3 selftest.py，全部 assert，无框架。"""
import os
import tempfile

os.environ["OPSX_STATE_DIR"] = tempfile.mkdtemp(prefix="opsx-test-")

from pathlib import Path  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

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
    code, sid, _ = core.exec_change("echo hi", snapshot_files=["/nonexistent/path/xyz"])
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
    core.approve("echo audit-capture-check", ttl_seconds=60)
    code, sid, output = core.exec_change("echo audit-capture-check")
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
        assert len(tools) == 9
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == {
            "ops_check", "ops_snapshot_file", "ops_snapshot_cmd", "ops_approve",
            "ops_revoke", "ops_exec", "ops_rollback", "ops_list", "ops_audit",
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
                _tool_call("c1", "ask_user", {"question": f"变更单：执行 {cmd}，确认？"})]},
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
    import shutil
    import subprocess as sp
    from agent import repl
    text = "ops-agent 复制测试 ✓"
    result = repl.copy_to_clipboard(text)
    if shutil.which("pbcopy"):
        pasted = sp.run(["pbpaste"], capture_output=True, text=True).stdout
        assert pasted == text
        assert "已复制" in result
    else:  # 无剪贴板工具的环境：应优雅降级而非异常
        assert "复制失败" in result or "已复制" in result


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


def test_remote_exec_snapshot_and_rollback():
    import subprocess
    import remote
    _write_targets()
    calls = []

    def fake_ssh(name, cmd, stdin=None, timeout=120):
        calls.append((name, cmd, stdin))
        if cmd.startswith("cat --"):
            return subprocess.CompletedProcess([], 0, stdout=b"remote-v1\n", stderr=b"")
        if cmd.startswith("systemctl restart"):
            return subprocess.CompletedProcess([], 0, stdout=b"restarted\n", stderr=b"")
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

        report = remote.rollback(sid)
        assert "web1:/etc/nginx/nginx.conf" in report
        pushed = [c for c in calls if c[1].startswith("cat >")]
        assert pushed and pushed[0][2] == b"remote-v1\n"  # 回滚经 stdin 推回

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
            assert "已恢复文件" in remote.rollback(local_sid) and f.read_text() == "v1"
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
        "  *\"cat -- \"*) printf 'remote-v1\\n' ;;\n"
        '  *"cat > "*) cat > "$OPSX_TEST_PUSHED" ;;\n'
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
    r = run("rollback", sid)
    assert r.returncode == 0 and "web1:/etc/nginx/nginx.conf" in r.stdout, r.stderr
    assert pushed.read_bytes() == b"remote-v1\n"
    assert "cat > /etc/nginx/nginx.conf" in log.read_text()
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
        # 已单独审批过的抓取命令可以使用
        core.approve("echo cap-ok", ttl_seconds=60)
        code, sid, _ = core.exec_change(main, snapshot_cmds=[("echo cap-ok", "cap")])
        assert code == 0 and sid
        cap = Path(core.STATE_DIR, "snapshots", sid, "cmd-cap.txt")
        assert "cap-ok" in cap.read_text()


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
            remote.rollback(sid)
            raise AssertionError("推回失败应报错")
        except remote.RemoteError as e:
            assert "回滚失败" in str(e)
    finally:
        remote._ssh = orig


def test_ops_list_survives_corrupt_registry():
    from agent import tools as agent_tools
    core.snapshot_cmd("echo corrupt-reg", name="cr")
    p = Path(core.STATE_DIR, "targets.json")
    p.write_text("{not json", encoding="utf-8")
    try:
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
    assert names == {"bash", "read", "skill", "ops_list"}
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
    assert {result for _, result in results} == {"报告:检查磁盘", "报告:每日巡检"}
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
    schedule._run_entry = lambda entry, model: f"报告:{entry['id']}"
    try:
        results = schedule.run_due()
    finally:
        schedule._run_entry = orig
    assert [entry["id"] for entry, _ in results] == [good["id"]]
    bad = [it for it in schedule.list_entries() if it["id"] == "sch-bad"][0]
    assert bad["enabled"] is False
    (core.STATE_DIR / "schedules.json").unlink(missing_ok=True)


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


def test_workflow_run_and_validation():
    from agent import loop, tools as agent_tools, workflow
    ctx = agent_tools.ToolContext()
    assert "daily-inspection" in agent_tools.dispatch("workflow", {}, ctx)
    assert len(workflow.load("daily-inspection")["steps"]) == 4
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
