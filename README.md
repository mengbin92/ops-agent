# ops-agent

默认只读、高风险操作需会话内授权并附风险说明与快照回滚的运维智能体。
既可**独立运行**（自带 agent loop 与 LLM 接入），也可作为 Claude Code 子代理/MCP server 嵌入。

## 独立 agent 形态（opsagent）

自带 agent 内核（`agent/`：loop + provider（OpenAI 兼容 / Anthropic 原生）+ 工具注册表 + 会话持久化），
守门核心 core.py 一行不改地成为其内置安全层。架构见
`docs/superpowers/specs/2026-10-19-standalone-agent-design.md`。

```bash
# 配置（OpenAI 兼容协议，不绑定厂商：BASE_URL 指哪家用哪家）
export OPSAGENT_API_KEY=sk-...
# export OPSAGENT_BASE_URL=https://api.deepseek.com   # 默认 DeepSeek，可换任意兼容端点
# export OPSAGENT_MODEL=deepseek-chat                 # 模型名随端点服务而定

# 或改用 Anthropic 原生 Messages 协议（工具/流式同样支持）
# export OPSAGENT_PROVIDER=anthropic
# export OPSAGENT_API_KEY=sk-ant-...
# export OPSAGENT_BASE_URL=https://api.anthropic.com  # 默认值；代理可给到 .../v1
# export OPSAGENT_MODEL=claude-sonnet-4-5            # 默认值

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
会话文件保留完整记录）。命令输出（`bash` / `ops_exec`）超过 3 万字符时完整内容
落盘到 `~/.ops-agent/outputs/`（0600，保留 200 份），回复里给预览 + 文件路径，
`read` 工具带 `offset`/`limit` 分段读取（大文件同理）。

RPC 形态（stdio JSON-RPC，一行一条）：`prompt {text, stream?}` / `reset` / `ping`。
`stream=true` 时边跑边推通知 `{"method":"event","params":{"kind":"delta","payload":"..."}}`
（`delta` 文本增量、`tool` 工具调用/结果、`stream_end` 文本段收尾），最终结果不再重复
`events`；不传 `stream` 则维持一次性返回 `{"text","events","session_id"}`。
`ask_user` 用 `{"method":"ask_user","id":"srv-..."}` 反向请求，客户端把回答作为响应回传。

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

# 用户取消变更时立即撤销审批戳（不必等 TTL 过期）
./opsx revoke 'systemctl restart nginx'

# 查看审计 / 快照 / 审批戳
./opsx audit --tail 20
./opsx list
./opsx rollback <snapshot_id>
```

## 远程执行（SSH 目标）

写操作可以下发到远程机，守门仍在本机跑：同一份 `config.yaml`、同一套审批戳、本地审计。
先在 `~/.ops-agent/targets.json` 注册目标——**只允许操作注册过的目标**，模型无法自行指定主机：

```json
{
  "web1": {"host": "10.0.0.11", "user": "ops", "port": 22,
           "identity": "~/.ssh/id_ed25519",
           "options": ["-o", "StrictHostKeyChecking=accept-new"]}
}
```

```bash
./opsx targets                                            # 列出已注册目标
./opsx approve --target web1 'systemctl restart nginx'    # 戳按「目标+命令」绑定
./opsx exec --target web1 --snapshot-file /etc/nginx/nginx.conf 'systemctl restart nginx'
./opsx rollback <snapshot_id>                             # 远程快照自动推回目标机
```

- agent 内：只读排查用 `bash` 带 `target`（仍要求 R0）；写操作 `ops_approve` 与 `ops_exec`
  必须带同一个 `target`——本机戳不能用于远程目标，A 机器的戳也不能拿到 B 机器用。
- 远程快照：执行前用 `cat` 拉回 `~/.ops-agent/snapshots/<id>/files/...` 存档（快照路径用绝对路径）；
  回滚用 stdin 覆盖写回（保留原 inode/权限；文件已被删则重建，权限取默认值）。
  目标机不需要安装任何东西。
- 认证：`BatchMode=yes`，要求密钥/ssh-agent 免密——agent 无人值守，不能停在密码提示上。
- 目标字段（host/user/port/identity）含空白或前导 `-` 一律拒绝，避免配置值被 ssh 当成选项。
- 边界：`ssh` 只是传输通道，目标机上的命令不受目标机本地 opsx 约束；
  MCP 形态仍只支持本机执行（MCP 宿主可直接调 `opsx exec --target` 获得同样能力）。

## 技能库与子代理（M3）

**技能库（skills）**：把标准运维场景写成按需加载的 playbook，模型先查目录、用时才载入正文
（不常驻上下文）。仓库 [skills/](skills/) 自带示例，`~/.ops-agent/skills/*.md` 同名覆盖
（个人/团队自定义）。文件名即 skill 名，frontmatter 只认 `description`：

```markdown
---
description: nginx 返回 5xx 时的只读排查顺序
---
# ...
```

agent 需要时先 `skill`（不带 name）列出目录，再 `skill {"name": "nginx-5xx"}` 载入；
playbook 里的只读命令直接用，写操作仍走变更单流程——skills 只提供步骤，不绕过守门。

**子代理（subagent）**：一次下发 2–4 个独立只读排查任务并行执行（多主机、多命名空间、
日志+配置+资源等），各自返回结论与证据，主代理汇总。子代理只能用 `bash`（仅 R0）/
`read` / `skill` / `ops_list`，不能写、也不能继续派生；每个任务最多 8 轮、沿用父代理的
`--model`。子代理会话同样落在 `~/.ops-agent/sessions/`（计入 300 个会话保留上限），
可在 `/resume` 里回看排查过程。

## 防线说明

两层防线：① Agent 流程纪律（变更单 → AskUserQuestion → opsx exec）；
② PreToolUse hook 硬拦截（无白名单命中且无有效审批戳 → exit 2 拒绝）。

`opsx exec` 的 `--snapshot-cmd` 抓取命令同样过守门（R0 免戳，其余需它自己的审批戳），
防止把写操作藏进快照参数。状态目录与快照目录按 `0700` 创建——远程拉回的配置/密钥只有本人可读。

审批戳可随时 `opsx revoke`（agent 内为 `ops_revoke`）撤销，不必等 TTL 过期；
远程戳按目标绑定，撤销本机同名命令不会波及目标机。

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

只读工具（`ops_check` / `ops_list` / `ops_audit`）建议 allowlist 免弹窗；写工具（`ops_approve` / `ops_revoke` / `ops_exec` / `ops_rollback` / `ops_snapshot_*`）保持每次弹窗（双保险），审批戳仍是硬闸门。Claude Code 的权限配置片段：

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
