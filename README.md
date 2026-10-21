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
./opsagent schedule list                  # 定时巡检任务（M5，详见下文）
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
变更单 → ops_approve 向用户确认完整命令与目标、明确批准后建戳 → ops_exec 快照执行。
`ask_user` 仍可用于澄清；它的回答不代替 `ops_approve` 对具体命令的确认。
非 TTY 环境 ask_user 不可用：`ops_approve` 直接拒绝（没有确认通道，不能自己给自己发戳），
写操作只能靠人工预先 `./opsx approve` 授权。

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
./opsx rollback <snapshot_id>          # 先展示目标/影响文件/差异，确认后才改写
./opsx rollback <snapshot_id> --yes    # 已核对计划（非交互环境必须显式加）
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
./opsx rollback <snapshot_id>                             # 预览+确认后远程快照推回目标机
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

## 定时巡检、长目标与工作流（M5）

**定时巡检（schedule）**：任务存在 `~/.ops-agent/schedules.json`，到期后以无人值守模式跑一轮
agent——没有可回答问题的人，因此只读排查，写操作会被守门拒绝，只输出建议。

```bash
./opsagent schedule add --daily 09:00 "只读巡检磁盘/内存/服务，异常给建议"
./opsagent schedule add --every 1800 "检查 nginx 错误日志"
./opsagent schedule list
./opsagent schedule remove <id>       # id 可用唯一前缀
./opsagent schedule run               # 守护进程（同一时刻只允许一个，flock 防双开）
./opsagent schedule run --once        # 适合 cron：*/5 * * * * ... schedule run --once
```

agent 里也可以直接说"每天九点巡检一次"，模型用 `schedule` 工具登记（list / add / remove）。
每次执行写审计（`schedule-run`，含结论摘要）；注册表的增删与 daemon 的推进各有锁，
不会丢更新。每次巡检的会话也落在 `~/.ops-agent/sessions/`（计入 300 上限），可在
`/resume` 回看。cron 场景记得在环境里带上 `OPSAGENT_API_KEY` / `OPSAGENT_BASE_URL`。

**长目标（goal）**：在 REPL 里让 agent 推进多步目标（"排查到定位为止""升级并验证完"）。
每轮结束后按目标状态自动续跑，默认最多 6 轮（`max_rounds` 可调，上限 20），完成后自动停。
续跑中的写操作仍然逐次需要用户确认；`/goal` 查看状态，`/goal stop` 暂停，`/goal resume`
恢复并重置轮次预算。`-p` / RPC / 定时任务不会自动续跑（单发语义），`goal` 工具在这些
形态下拒绝创建，避免留下永远不推进的目标。

**巡检工作流（workflow）**：把固定巡检步骤写成 JSON——仓库 [workflows/](workflows/) 自带
`daily-inspection`，`~/.ops-agent/workflows/*.json` 同名覆盖。每步起一个只读子代理顺序执行，
后一步用 `{prev}` 引用前一步结论：

```json
{"description": "每日巡检", "steps": [
  {"name": "资源", "prompt": "只读检查磁盘/内存/负载"},
  {"name": "汇总", "prompt": "基于前序结论写报告：{prev}"}
]}
```

agent 里先 `workflow`（不带 name）列目录，再 `workflow {"name": "daily-inspection"}` 运行；
定时任务的 prompt 也可以写"运行 workflow daily-inspection 并报告异常"。
`opsagent schedule add --daily 09:00 "运行 workflow daily-inspection 并报告异常"` 就是一套
完整的每日巡检。

**长期记忆（remember）**：排查中确认的主机事实（OS/服务清单/已排除的告警项）、用户偏好、
环境约定，agent 用 `remember` 工具记入 `~/.ops-agent/memory.md`（0600，单条 ≤500 字符），
之后每轮自动加载到 system prompt——下次会话不再重复排查已知事实。只记结论，一次性状态
不记；反复出现的排查模式建议晋升进 `skills/*.md`（沉淀为 playbook，memory 保持流水）。
记录时遵循写法约定——`主机: 属性 = 值`（如 `db-01: OS = Ubuntu 24.04`），依赖关系记
`服务 X depends_on db-01`，同类事实用同一词汇：召回与将来按主机结构化拆分都靠这份一致性。
过时条目直接编辑该文件删除即可；记忆与现状冲突时 agent 以现状为准并记录更正。
定时巡检等只读形态不可用 `remember`（无人值守不积累噪声），但能读到已有记忆。

## 安全边界（W1）

**秘密输出保护**：命令/工具输出在进入模型、写入会话、写入审计、落盘长输出之前统一脱敏
（`core.redact`：PEM 私钥、`password=/token=/api_key=` 等键值、`Authorization: Bearer …`、
常见 token 形状）。快照保留原始内容供恢复，不脱敏；目录 `0700`，`read` 工具不允许把快照原件
读进模型上下文。工具结果（agent / CLI / MCP）、会话落盘、审计、长输出落盘和审批列表展示
都走同一套脱敏；用户消息与 `remember` 写入同样处理（含密钥的记忆条目直接拒绝）。
`selftest` 用哨兵密钥验证各出口都不出现明文。

**敏感文件读取限制**：`read` 直接拒绝私钥（`id_rsa`/`id_ed25519`/`*.key`/`*.p12` 等）、
`.env`（`.example`/`.sample`/`.template` 除外）、`.netrc`、`.kube/config`、
`/etc/kubernetes/*.conf`、`/etc/ssh/ssh_host_*_key`、`/etc/ssl/private/*.pem`、
`.aws/credentials`、`.docker/config.json`、`.config/gcloud/`、`/etc/shadow`
与 `.ssh`/`.aws`/`.kube`/`.gnupg` 目录；命令入口（`opsx check`、hook、bash 工具、`opsx exec`）
对引用这些文件或读取敏感目录内容的命令一律拒绝（`cat ~/.ssh/id_rsa`、经符号链接指向的私钥、
`ssh host "cat id_rsa"` 的内层命令、远程命令里的裸 `id_rsa` 都覆盖）；`kubectl get secret
-o yaml/json/jsonpath`（会把 Secret 值输出成明文）同样拒绝，`-o name` 与默认表格、`describe`
不受影响。`ls -l ~/.ssh`、`grep id_rsa app.log`、`find -name id_rsa`、
`KUBECONFIG=~/.kube/config kubectl get pods` 不受影响。
边界：正则识别不保证覆盖所有秘密格式（加密内容、二进制、自定义字段会漏报），
偏保守地拒绝，可能挡住合法的密钥操作（改密钥请由人直接执行）。

**审批绑定实际执行环境**：审批戳除命令与目标名外，还绑定实际主机/用户/端口、
`identity`/`options`、工作目录、`KUBECONFIG` 以及 kubectl 的 `--context`/`--namespace`。
批准后换个目录、目标注册表里改了 host 或 SSH 选项、切了 kubeconfig 或 context，旧授权立即
失效，需要重新批准（`opsx list` 不再把旧格式戳显示成有效）。kubectl 变更必须在命令里显式写
`--context`（否则拒绝发戳，全路径、`sh -c`、注释里的假 `--context` 都不算），同一命令混用
不同 context 直接拒绝，不会用当前默认 context 悄悄执行；只读的 `kubectl get` 段不强制。

**回滚确认与回滚前副本**：回滚是写操作。入口先展示实际目标（含远程主机名）、影响文件、
当前内容与快照的差异，由人确认后才改写：CLI 交互确认（非交互需 `--yes`）、agent
`ops_rollback` 再问一次、MCP `ops_rollback` 返回预览且只有 `confirm=true` 才执行。执行前
自动把当前内容另存为「回滚前副本」快照并在报告里给出 id，后续修改不会被静默覆盖，且可再
回滚副本撤销（远程快照记录采集时的主机/用户/端口/identity/options，`targets.json` 改指向
后拒绝回滚，避免写到另一台机器）。没有旧内容可备份（文件原本不存在）时不创建空副本、也不
承诺可撤销。用户拒绝时文件不发生任何改动；多文件回滚中途失败会在错误里带上副本 id。
边界：本机回滚原子替换会改变 inode、符号链接路径会变成普通文件；快照只能恢复文件，
不能替代数据库/卷/部署回退。

**CI**：`.github/workflows/selftest.yml` 在 push/PR 上跑 `python -m compileall` 与
`./opsx selftest`（Ubuntu/macOS × Python 3.9/3.12）。

迁移提示：W1 之前的审批戳不含执行环境信息，按安全方向一律失效，需要重新授权；
旧会话/审计里已存在的明文密钥不会自动回溯清理。

## 防线说明

两层防线：① Agent 流程纪律（变更单 → AskUserQuestion → opsx exec）；
② PreToolUse hook 硬拦截（无白名单命中且无有效审批戳 → exit 2 拒绝）。

`opsx exec` 的 `--snapshot-cmd` 抓取命令同样过守门（R0 免戳，其余需它自己的审批戳），
独立的 `opsx snapshot --cmd` / MCP `ops_snapshot_cmd` 也遵守此规则，
防止把写操作藏进快照参数。状态目录与快照目录按 `0700` 创建，会话与输出文件为 `0600`——
远程拉回的配置/密钥只有本人可读。
本机文件回滚先写临时文件再原子替换，恢复内容与权限但会改变 inode；
远程文件回滚仍通过覆盖写保留已有 inode。

hook 允许直接调用本仓库的 `opsx`（或 Python + 本仓库脚本），由入口内部校验审批与快照；
入口外拼接 shell 命令仍逐条守门。审批戳保留引号、转义和换行里的空白，避免不同命令复用授权；
升级前为这类命令创建的旧戳可能需要重新授权。

审批戳可随时 `opsx revoke`（agent 内为 `ops_revoke`）撤销，不必等 TTL 过期；
远程戳按目标绑定，撤销本机同名命令不会波及目标机。

已知边界：审批戳由 Agent 侧在获得用户确认后创建，防"误执行"不防"恶意 Agent"；
命令分类为线性正则匹配，不解析 shell AST（复合命令按 `;` `&&` `||` `|`、换行与后台 `&`
分段取最高级）。命令超时会停止本地进程组；远程超时只保证本地 SSH 已停止，
目标机上的进程是否退出须另行检查。

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
`ops_rollback` 先返回目标/文件/差异预览，核对后带 `confirm=true` 重试才真正回滚（执行前自动保存回滚前副本）。

## 演进

core.py 为纯函数库；演进 MCP server / agent 内核时直接包装同一组函数，状态目录布局即对外契约。
独立化演进路线（流式输出、compaction、skills、子代理、远程通道）见设计文档第 5 节。
