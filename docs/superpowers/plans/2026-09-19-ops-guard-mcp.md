# ops-guard MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以纯 stdlib 手写 MCP stdio server，把 core.py 的 8 个能力透出给任意 MCP 客户端，确认流为双弹窗最大安全。

**Architecture:** `mcp_server.py` 是 core.py 之上的第二个薄壳（第一个是 CLI `opsx`）：stdio 换行分隔 JSON-RPC 2.0，单消息循环，只实现 initialize/notifications.initialized/tools/list/tools/call/ping。core.py 契约冻结不动；`ops_exec` 的 snapshot_id 从审计日志末条 exec 事件读取。CLI 与 MCP 并存共享 core.py 与 `~/.ops-agent/` 状态。

**Tech Stack:** python3 纯标准库（json/sys/subprocess/pathlib/traceback），系统 python 3.9.6 可运行；无第三方依赖。

**Spec:** `docs/superpowers/specs/2026-09-19-ops-guard-mcp-design.md`

## Global Constraints

- python3 纯标准库；禁止引入任何第三方包；禁止 3.10+ 运行时特性（`from __future__ import annotations` 已在需要处使用模式可参考）。
- core.py 契约冻结：本计划不修改 core.py、config.yaml、opsx 中任何一个。
- 状态目录 `~/.ops-agent/` 布局即对外契约，保持不变。
- 恰好 8 个工具，名称固定：ops_check / ops_snapshot_file / ops_snapshot_cmd / ops_approve / ops_exec / ops_rollback / ops_list / ops_audit。
- 仅 ops_check / ops_list / ops_audit 带 `annotations.readOnlyHint: true`，其余无 annotations 字段。
- 错误映射：core.OpsxError → tools/call result `isError: true`；帧解析失败 → JSON-RPC -32700（id: null）；未知方法 → -32601；工具内部未预期异常 → -32603；单条消息异常不得崩服务循环。
- server 名 `ops-guard`，版本 `1.0.0`；initialize 回显客户端 protocolVersion。
- 唯一硬性质量门：`python3 selftest.py` 全绿（终点 23/23）。
- 提交信息不含 Claude/AI 署名。
- 测试经子进程拉起 mcp_server.py，继承测试进程的临时 `OPSX_STATE_DIR`。

---

### Task 1: mcp_server.py 骨架 + 协议循环（握手/通知/帧错误/未知方法）

**Files:**
- Create: `mcp_server.py`
- Modify: `selftest.py`

**Interfaces:**
- Produces:
  - `mcp_server.py` 可执行入口（`python3 mcp_server.py` 启动 stdio 服务循环）
  - `handle_message(msg: dict) -> dict | None`（None = 通知无需响应）
  - `selftest.py` 模块级 `McpClient` 类：`request(method, params=None) -> dict`、`notify(method, params=None)`、`send_raw(line)`、`call_tool(name, arguments=None) -> (result, payload)`、`close()`
  - 测试 `test_mcp_handshake`、`test_mcp_protocol_errors`

- [ ] **Step 1: 写失败测试（selftest.py 追加）**

在 `ALL = [...]` 行之前插入 `McpClient` 类与两个测试（`selftest.py` 顶部已有 `import json`、`from pathlib import Path`、`import core`）：

```python
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
        return result, json.loads(result["content"][0]["text"])

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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 selftest.py`
Expected: 两个新测试 FAIL（`mcp_server.py` 不存在，Popen 报 FileNotFoundError 或类似）

- [ ] **Step 3: 写 mcp_server.py 骨架**

```python
#!/usr/bin/env python3
"""ops-guard MCP server —— core.py 的 MCP 薄壳（stdio JSON-RPC，纯 stdlib）。

协议子集：initialize / notifications.initialized / tools/list / tools/call / ping。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

SERVER_NAME = "ops-guard"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION_DEFAULT = "2024-11-05"


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _send(resp):
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_message(msg):
    """返回响应 dict；通知（无 id）返回 None。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "请求必须是 JSON 对象")
    method = msg.get("method")
    msg_id = msg.get("id")
    if "id" not in msg:
        return None  # JSON-RPC 通知，一律不响应
    if method == "initialize":
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION_DEFAULT),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method is None:
        return _err(msg_id, -32600, "缺少 method 字段")
    return _err(msg_id, -32601, f"未知方法: {method}")


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send(_err(None, -32700, "Parse error"))
            continue
        try:
            resp = handle_message(msg)
        except Exception as e:  # noqa: BLE001 —— 单条消息异常不崩循环
            traceback.print_exc(file=sys.stderr)
            resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, str(e))
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    serve()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 selftest.py`
Expected: 两个新测试 PASS，全量 20/20（18 既有 + 2 新）

- [ ] **Step 5: 提交**

```bash
git add mcp_server.py selftest.py
git commit -m "feat: ops-guard MCP server 骨架——协议循环与握手/帧错误处理"
```

---

### Task 2: 工具面（tools/list + 全部 8 个 tools/call 分发）

**Files:**
- Modify: `mcp_server.py`（整文件替换为完整版）
- Modify: `selftest.py`（追加 3 个测试）

**Interfaces:**
- Consumes: core 公开函数 `classify/check/snapshot_file/snapshot_cmd/approve/exec_change/rollback`、常量 `STATE_DIR/REPO_DIR`、异常 `OpsxError`；Task 1 的 `McpClient`。
- Produces:
  - `mcp_server.py` 模块级 `TOOLS`（8 个工具定义）、`call_tool(name, args) -> dict`
  - `handle_message` 增加 `tools/list`、`tools/call` 分支
  - 测试 `test_mcp_tools_list`、`test_mcp_readonly_tools`、`test_mcp_write_cycle`

- [ ] **Step 1: 写失败测试（selftest.py 在 `ALL = [...]` 行之前追加）**

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 selftest.py`
Expected: 三个新测试 FAIL（tools/list 返回 -32601 未知方法，或 tools/call 无 result）

- [ ] **Step 3: 整文件替换 mcp_server.py 为完整版**

保留 Task 1 的全部内容，新增 `TOOLS`、`call_tool`、`_result`、`_tool_error`、`_require`、`_last_exec_snapshot_id`，并在 `handle_message` 的 `ping` 分支后插入 `tools/list` 与 `tools/call` 分支。完整文件：

```python
#!/usr/bin/env python3
"""ops-guard MCP server —— core.py 的 MCP 薄壳（stdio JSON-RPC，纯 stdlib）。

协议子集：initialize / notifications.initialized / tools/list / tools/call / ping。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

SERVER_NAME = "ops-guard"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION_DEFAULT = "2024-11-05"


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _send(resp):
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _tool_error(message):
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _tool(name, description, schema, read_only=False):
    t = {"name": name, "description": description, "inputSchema": schema}
    if read_only:
        t["annotations"] = {"readOnlyHint": True}
    return t


TOOLS = [
    _tool("ops_check", "判定命令风险级别与放行状态（只读）", {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }, read_only=True),
    _tool("ops_snapshot_file", "对文件做执行前快照", {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }),
    _tool("ops_snapshot_cmd", "执行命令并捕获其输出作为状态快照", {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["command"],
    }),
    _tool("ops_approve", "为命令创建审批戳（R3 需 force=true）", {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "ttl_seconds": {"type": "integer", "default": 900},
            "force": {"type": "boolean", "default": False},
        },
        "required": ["command"],
    }),
    _tool("ops_exec", "原子执行：校验审批戳→快照→执行→审计", {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "snapshot_files": {"type": "array", "items": {"type": "string"}},
            "snapshot_cmds": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["command"],
                },
            },
        },
        "required": ["command"],
    }),
    _tool("ops_rollback", "按快照回滚", {
        "type": "object",
        "properties": {"snapshot_id": {"type": "string"}},
        "required": ["snapshot_id"],
    }),
    _tool("ops_list", "列出快照与审批戳（只读）", {
        "type": "object", "properties": {},
    }, read_only=True),
    _tool("ops_audit", "查看审计日志（只读）", {
        "type": "object",
        "properties": {"tail": {"type": "integer", "default": 20}},
    }, read_only=True),
]


def _require(args, key):
    if not isinstance(args, dict) or key not in args or args[key] in (None, ""):
        raise core.OpsxError(f"缺少参数: {key}")
    return args[key]


def _last_exec_snapshot_id():
    path = core.STATE_DIR / "audit.jsonl"
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").strip().splitlines()):
        rec = json.loads(line)
        if rec.get("event") == "exec":
            return rec.get("snapshot_id")
    return None


def call_tool(name, args):
    if name == "ops_check":
        cmd = _require(args, "command")
        level, _ = core.classify(cmd)
        code, message = core.check(cmd)
        return _result({"level": level, "allowed": code == 0, "message": message})
    if name == "ops_snapshot_file":
        sid = core.snapshot_file(_require(args, "path"))
        return _result({"snapshot_id": sid})
    if name == "ops_snapshot_cmd":
        sid = core.snapshot_cmd(_require(args, "command"), args.get("name", ""))
        return _result({"snapshot_id": sid})
    if name == "ops_approve":
        path = core.approve(
            _require(args, "command"),
            ttl_seconds=int(args.get("ttl_seconds", 900)),
            force=bool(args.get("force", False)),
        )
        stamp = json.loads(path.read_text(encoding="utf-8"))
        return _result({"stamp_file": path.name, "level": stamp["level"], "ttl_seconds": stamp["ttl"]})
    if name == "ops_exec":
        snap_cmds = [(c["command"], c.get("name", "")) for c in args.get("snapshot_cmds", [])]
        code = core.exec_change(
            _require(args, "command"),
            args.get("snapshot_files", []),
            snap_cmds,
        )
        payload = {"exit_code": code}
        if code == 0:
            payload["snapshot_id"] = _last_exec_snapshot_id()
        return _result(payload)
    if name == "ops_rollback":
        report = core.rollback(_require(args, "snapshot_id"))
        return _result({"report": report})
    if name == "ops_list":
        sdir = core.STATE_DIR / "snapshots"
        snapshots = sorted(p.name for p in sdir.iterdir()) if sdir.exists() else []
        approvals = []
        adir = core.STATE_DIR / "approvals"
        if adir.exists():
            for p in sorted(adir.glob("*.json")):
                st = json.loads(p.read_text(encoding="utf-8"))
                approvals.append({"cmd": st["cmd"], "level": st["level"], "ttl": st["ttl"]})
        return _result({"snapshots": snapshots, "approvals": approvals})
    if name == "ops_audit":
        tail = int(args.get("tail", 20))
        path = core.STATE_DIR / "audit.jsonl"
        events = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").strip().splitlines()[-tail:]:
                events.append(json.loads(line))
        return _result({"events": events})
    raise core.OpsxError(f"未知工具: {name}")


def handle_message(msg):
    """返回响应 dict；通知（无 id）返回 None。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "请求必须是 JSON 对象")
    method = msg.get("method")
    msg_id = msg.get("id")
    if "id" not in msg:
        return None  # JSON-RPC 通知，一律不响应
    if method == "initialize":
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION_DEFAULT),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        try:
            result = call_tool(tool_name, tool_args)
        except core.OpsxError as e:
            result = _tool_error(str(e))
        except Exception as e:  # noqa: BLE001 —— 未预期异常走 JSON-RPC 层
            traceback.print_exc(file=sys.stderr)
            return _err(msg_id, -32603, f"工具内部错误: {e}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method is None:
        return _err(msg_id, -32600, "缺少 method 字段")
    return _err(msg_id, -32601, f"未知方法: {method}")


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send(_err(None, -32700, "Parse error"))
            continue
        try:
            resp = handle_message(msg)
        except Exception as e:  # noqa: BLE001 —— 单条消息异常不崩循环
            traceback.print_exc(file=sys.stderr)
            resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, str(e))
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    serve()
```

- [ ] **Step 4: 运行全量 selftest 确认通过**

Run: `python3 selftest.py`
Expected: `23/23 passed`

- [ ] **Step 5: 提交**

```bash
git add mcp_server.py selftest.py
git commit -m "feat: MCP 工具面——tools/list 与 8 个 tools/call 分发"
```

---

### Task 3: README MCP 章节

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1-2 的 server 行为（工具名、确认流）。
- Produces: 可照抄的客户端注册与权限配置片段。

- [ ] **Step 1: 在 README「演进」小节前插入新章节**

````markdown
## MCP server 形态

`mcp_server.py` 把同一组能力以 MCP server 暴露（stdio JSON-RPC，纯 stdlib，与 CLI 共享 core.py 与状态目录）。客户端注册：

```json
{
  "mcpServers": {
    "ops-guard": {
      "command": "python3",
      "args": ["<本仓库路径>/mcp_server.py"]
    }
  }
}
```

只读工具（`ops_check` / `ops_list` / `ops_audit`）建议 allowlist 免弹窗；写工具（`ops_approve` / `ops_exec` / `ops_rollback` / `ops_snapshot_*`）保持每次弹窗（双保险），审批戳仍是硬闸门。Claude Code 的权限配置片段：

```json
{
  "permissions": {
    "allow": [
      "mcp__ops-guard__ops_check",
      "mcp__ops-guard__ops_list",
      "mcp__ops-guard__ops_audit"
    ]
  }
}
```

确认流与 CLI 形态一致：变更单 → `ops_approve`（弹窗=授权，建戳）→ `ops_exec`（再弹窗=双保险，校验戳）→ 报告 snapshot_id 与回滚。R3 仍需 `force=true` + 会话内显式确认。
````

- [ ] **Step 2: 冒烟验证**

Run:
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}' | timeout 2 python3 mcp_server.py 2>/dev/null; echo "exit=$?"
```
Expected: 输出一行 initialize 响应（含 `"name":"ops-guard"`）；stdin 关闭后服务退出。

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs: README 补 MCP server 注册与确认流说明"
```

---

## Self-Review 记录

- 规格覆盖：协议子集五方法（T1/T2）、8 工具与注解（T2）、双弹窗确认流（README T3 + 工具注解）、错误映射五情形（T1 帧/未知方法，T2 OpsxError/缺参/未预期）、snapshot_id 取审计日志（T2 `_last_exec_snapshot_id`）、e2e 测试策略十条（T1 两条 + T2 三条覆盖全部十步）、交付物四件（T1-T3）、YAGNI 清单（无对应任务，即未做）。
- 无占位符；接口名跨任务一致（McpClient.request/notify/send_raw/call_tool/close；call_tool 返回 (result, payload) 二元组）。
- 与 spec 的一处显式取舍：spec §6 说「总测试数 18 → 19」，实际拆为 5 个测试函数（18 → 23），覆盖条目一一对应，更利于定位失败。
