# ops-guard MCP Server 设计文档

日期：2026-09-19
状态：已评审通过，待实现
前置：2026-09-19-ops-agent-design.md（spec §10 的演进预留，本文将其升级为正式规格）

## 1. 背景与目标

把 opsx 的核心能力以 MCP server 形态暴露，使任意 MCP 客户端（Claude Code、Claude Desktop 等）都能调用同一组受控运维能力。已完成的关键决策：

- **实现方式**：纯 stdlib 手写 MCP stdio 协议（系统 python 3.9 直接运行，零第三方依赖；协议子集小，风险可控）。
- **确认流**：双弹窗最大安全。客户端对写工具每次弹窗确认 + core 层审批戳硬闸门双重把关。

目标：

1. `core.py` 公开能力原样透出，不新增语义、不改状态契约。
2. CLI（`opsx`）与 MCP 并存，共享 `core.py`，互不依赖。
3. `~/.ops-agent/` 布局即对外契约，保持不变。
4. 协议正确性有端到端测试兜底（stdlib 模拟客户端）。

## 2. 协议子集

stdio 传输，UTF-8，换行分隔的 JSON-RPC 2.0 消息（MCP stdio 标准帧格式）。只实现 tools 子集必需的五个方法：

| 方法 | 行为 |
|------|------|
| `initialize` | 回显客户端 `protocolVersion`；返回 `capabilities: {"tools": {"listChanged": false}}`、`serverInfo: {"name": "ops-guard", "version": "1.0.0"}` |
| `notifications/initialized` | 空操作（不回复） |
| `tools/list` | 返回 8 个工具（见 §3） |
| `tools/call` | 分发到 core.py 函数（见 §3、§5） |
| `ping` | 返回 `{}` |

不实现：resources、prompts、sampling、roots、HTTP/SSE 传输。`notifications/cancelled` 收到即忽略（不支持取消，执行中的工具调用会跑到完成——快照+执行均为短操作，可接受）。

## 3. 工具面

8 个工具，全部为 core.py 公开函数的薄包装：

| 工具 | inputSchema（摘要） | 返回（text content 内嵌 JSON） | annotations |
|------|------|------|------|
| `ops_check` | `command: string` | `{level, allowed, message}` | `readOnlyHint: true` |
| `ops_snapshot_file` | `path: string` | `{snapshot_id}` | 无 |
| `ops_snapshot_cmd` | `command: string, name?: string` | `{snapshot_id}` | 无 |
| `ops_approve` | `command: string, ttl_seconds?: int(900), force?: bool` | `{stamp_file, level, ttl_seconds}` | 无 |
| `ops_exec` | `command, snapshot_files?: string[], snapshot_cmds?: [{command, name?}]` | `{exit_code, snapshot_id\|null}` | 无 |

`ops_exec` 返回中的 `snapshot_id` 取自执行后审计日志的最后一条 `exec` 事件——core.py 契约冻结，不为其改返回值，审计日志本就是系统的权威记录（server 单消息循环，无并发读取风险）。
| `ops_rollback` | `snapshot_id: string` | `{report}` | 无 |
| `ops_list` | （无参） | `{snapshots: [...], approvals: [...]}` | `readOnlyHint: true` |
| `ops_audit` | `tail?: int(20)` | `{events: [...]}` | `readOnlyHint: true` |

说明：

- `tools/call` 的 result 统一为 `{content: [{type: "text", text: "<JSON 字符串>"}]}`；调用方 `json.loads(text)` 取结构化结果。选 JSON 内嵌而非多 content block，是最简且最互操作的形式。
- `ops_exec.snapshot_cmds` 用对象数组（`{command, name}`），修正 CLI 的 `name::command` 字符串编码——MCP 有结构化参数，不再编码。
- annotations 是给客户端的提示；旧客户端忽略未知字段，无害。
- `ops_snapshot_*`、`ops_approve`、`ops_exec`、`ops_rollback` 不加 `readOnlyHint`，客户端保守弹窗（双弹窗设计的一部分）。

## 4. 确认流（双弹窗）

R1/R2 标准流程（与 ops.md 协议一致，仅执行通道换成 MCP 工具）：

1. Agent 输出变更单（目标/命令/风险/回滚/快照计划）。
2. `ops_approve` → 客户端弹窗 = 用户授权；core 校验（R0 拒发、R3 需 `force=true`）后写审批戳。
3. `ops_exec` → 客户端再弹窗 = 双保险；core 校验戳（无戳/过期即拒，exit 语义同 CLI）。
4. Agent 报告结果 + snapshot_id + 回滚工具。

R3：仍需 `force=true` 且会话内显式确认。

客户端配置（README 提供片段）：只读三工具（`ops_check`/`ops_list`/`ops_audit`）经 allowlist 免弹窗；写工具永不 allowlist。Claude Code 形态：

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

MCP server 注册（stdio）：

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

## 5. 错误处理

| 情形 | 响应 |
|------|------|
| `core.OpsxError`（业务错误：快照源不存在、R3 无 force、戳过期等） | `tools/call` result：`{isError: true, content: [{type: "text", text: 错误消息}]}`（JSON-RPC 层保持成功） |
| 帧不是合法 JSON | JSON-RPC error，`code: -32700`，`id: null` |
| 未知方法 | JSON-RPC error，`code: -32601` |
| 参数缺失/类型错误（schema 校验） | `tools/call` result `isError: true`，消息指明缺哪个参数 |
| 工具内部未预期异常 | JSON-RPC error，`code: -32603`，消息含异常摘要；服务循环不中断 |
| 单条消息处理异常 | try/except 兜底：尽力返回错误响应，绝不让服务循环崩溃 |

服务循环：逐行读 stdin → 解析 → 分发 → `flush` 写 stdout。stderr 可用于诊断日志。

## 6. 测试策略

`selftest.py` 新增一个端到端协议测试 `test_mcp_server_e2e`（stdlib 实现，无框架）：

1. `subprocess.Popen` 拉起 `mcp_server.py`（继承测试的临时 `OPSX_STATE_DIR`），管道写入/读取。
2. `initialize` → 响应含 `serverInfo.name == "ops-guard"`。
3. `notifications/initialized` → 无响应、无报错。
4. `tools/list` → 恰 8 个工具，只读三工具有 `annotations.readOnlyHint == true`。
5. `ops_check {"command": "docker ps"}` → `allowed: true, level: "R0"`。
6. `ops_check {"command": "rm -rf /tmp/x"}` → `allowed: false, level: "R3"`。
7. `ops_approve` + `ops_exec`（带 `snapshot_files` 指向临时文件，命令改写文件内容）→ `exit_code: 0`，文件内容已变。
8. `ops_rollback` → `report` 含原内容；文件复原。
9. 写入一行 `not-json{` → 收到 `code: -32700` 的 error 响应；服务继续可用（再发一条 ping 验证）。
10. 未知方法 → `code: -32601`。

每一对请求/响应用 `readline()` 同步读取（该 server 不主动推送通知，同步模型成立）。

## 7. 交付物

- `mcp_server.py`（新建，235 行）
- `selftest.py`（+1 个 e2e 测试函数，总测试数 18 → 19）
- `README.md`（MCP 注册配置、确认流说明、与 CLI 的关系）
- 本文档

## 8. 明确不做（YAGNI）

- HTTP/SSE 传输、远程部署（把运维闸门暴露到网络是安全倒退）。
- resources/prompts/sampling 等其他 MCP 能力。
- 请求取消、并发（单消息循环，工具调用串行）。
- 鉴权/多用户（本机单用户场景，与 CLI 一致）。
- 对 `core.py` 的任何修改（契约冻结）。
