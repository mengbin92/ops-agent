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
