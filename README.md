# ops-agent

默认只读、高风险操作需会话内授权并附风险说明与快照回滚的运维智能体。
既可**独立运行**（自带 agent loop 与 LLM 接入），也可作为 Claude Code 子代理/MCP server 嵌入。

## 独立 agent 形态（opsagent）

自带 agent 内核（`agent/`：loop + OpenAI 兼容 provider + 工具注册表 + 会话持久化），
守门核心 core.py 一行不改地成为其内置安全层。架构见
`docs/superpowers/specs/2026-10-19-standalone-agent-design.md`。

```bash
# 配置（OpenAI 兼容协议，不绑定厂商：BASE_URL 指哪家用哪家）
export OPSAGENT_API_KEY=sk-...
# export OPSAGENT_BASE_URL=https://api.deepseek.com   # 默认 DeepSeek，可换任意兼容端点
# export OPSAGENT_MODEL=deepseek-chat                 # 模型名随端点服务而定

./opsagent                                # 交互式 REPL（/new 新会话，/quit 退出）
./opsagent -p "看一下本机磁盘和内存"        # 单发非交互，可进脚本/CI
./opsagent rpc                            # stdio JSON-RPC 长驻进程（供 Web UI/IM 嵌入）
```

REPL 特性：**SSE 流式输出**，回复逐字到达并实时做 **Markdown 终端渲染**
（标题/表格/代码块/列表上色对齐，纯 stdlib ANSI）；**语义配色**——确认框
黄色醒目（R3/不可逆升级红色警示）、工具结果按内容定色（拒绝/错误红、
成功绿、降级黄、常规暗），非 TTY 或设 `NO_COLOR` 自动禁色（macOS libedit
下 `input()` 提示符自动无色，避免历史重绘残影；GNU readline 保留彩色）；
`/resume` 恢复历史会话；
`/copy` 复制上条回复、`/copy all` 复制全部回复到剪贴板
（pbcopy/wl-copy/xclip 自动探测）；`↑`/`↓` 翻历史输入（readline，
历史持久化于 `~/.ops-agent/repl_history`）。
上下文超长时自动 basic compaction：裁剪早期工具输出（仅裁剪发送内容，
会话文件保留完整记录）。

四种形态共享同一内核与状态目录：REPL、单发、RPC、MCP（见下）。
独立形态下守门在内核之内：bash 工具直接调 `core.check()`，写命令须
变更单 → ask_user 确认 → ops_approve 建戳 → ops_exec 快照执行。
非 TTY 环境 ask_user 不可用，写操作安全方向降级为拒绝。

## 组成（Claude Code 插件形态）

- `.claude/agents/ops.md` — Agent 定义（R0-R3 风险分级与流程纪律）
- `.claude/settings.json` — PreToolUse hook，对所有 Bash 命令执行 `opsx hook`
- `opsx` / `core.py` — 纯标准库 CLI/核心库（分类、审批戳、快照、回滚、审计）
- `config.yaml` — 只读白名单与风险规则；`~/.ops-agent/config.override.yaml` 追加覆盖

## 使用

必须在 `ops-agent/` 目录内启动（hook 为项目级配置，目录外只有 prompt 约束一层防线）。

```bash
# 自检
./opsx selftest

# 手动预授权（AskUserQuestion 不可用时的降级路径）
./opsx approve 'systemctl restart nginx' --ttl 900  # （`!` 前缀 = 你本人直接执行，即授权动作本身）

# 查看审计 / 快照 / 审批戳
./opsx audit --tail 20
./opsx list
./opsx rollback <snapshot_id>
```

## 防线说明

两层防线：① Agent 流程纪律（变更单 → AskUserQuestion → opsx exec）；
② PreToolUse hook 硬拦截（无白名单命中且无有效审批戳 → exit 2 拒绝）。

已知边界：审批戳由 Agent 侧在获得用户确认后创建，防"误执行"不防"恶意 Agent"；
命令分类为线性正则匹配，不解析 shell AST（复合命令按 `;` `&&` `||` `|` 分段取最高级）。

## MCP server 形态

`mcp_server.py` 把同一组能力以 MCP server 暴露（stdio JSON-RPC，纯 stdlib，与 CLI 共享 core.py 与状态目录）。客户端注册（把 `<本仓库路径>` 替换为本仓库绝对路径）：

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

## 演进

core.py 为纯函数库；演进 MCP server / agent 内核时直接包装同一组函数，状态目录布局即对外契约。
独立化演进路线（流式输出、compaction、skills、子代理、远程通道）见设计文档第 5 节。
