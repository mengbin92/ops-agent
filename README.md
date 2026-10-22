# ops-agent

默认只读、高风险操作需会话内授权并附风险说明与快照回滚的运维智能体。
既可**独立运行**（自带 agent loop 与 LLM 接入），也可作为 Claude Code 子代理/MCP server 嵌入。

## 独立 agent 形态（opsagent）

自带 agent 内核（`agent/`：loop + provider（OpenAI 兼容 / Anthropic 原生）+ 工具注册表 + 会话持久化），
守门核心 core.py 作为内置安全层复用。架构见
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

# 变更执行记录（每次 exec/rollback 一条，断线后靠它确认结果）
./opsx executions                      # 最近 20 条 + 待核实项
./opsx executions show <execution_id>
./opsx executions resolve <id> --observed-ok|--observed-failed

# 确定性巡检（规则检查不依赖模型，退出码 0/2/3/1）
./opsagent inspect
./opsagent inspect --target web1 --json
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
- 远程快照：执行前用 `cat` 拉回 `~/.ops-agent/snapshots/<id>/files/...` 存档（快照路径用绝对路径），
  同时记录真实路径、权限与属主；快照还记录目标机指纹（hostname + machine-id 前 12 位）。
  目标机不需要安装任何东西。
- 远程回滚是**原子写**：目标目录内 `mktemp` → 写入 → 校验字节数 → `chmod` → `rename`。
  写一半断线不会留下半份配置（要么旧内容、要么完整新内容）；软链会被解析到真实文件
  且保留链接本身。权限按快照记录还原；快照没记权限时，覆盖已有文件沿用目标当前权限、
  重建用 0600。覆盖已有文件时保留当前属主/属组，无法保留就中止；
  文件此前被删除时按快照重建，属主/属组**不自动还原**（需要 root），
  报告里会写明，需要时人工 `chown`。
- 目标被改指向（`targets.json` 换了 host，或指纹对不上）时拒绝回滚——
  拿 A 机的快照覆盖 B 机是灾难。
- 新快照还会拒绝文件或父目录软链接改向；旧快照没有真实路径记录时会明确提示校验限制。
  同次变更固定 SSH 配置，注册表中途改变不会把后续命令改发另一台主机。
- 认证：`BatchMode=yes`，要求密钥/ssh-agent 免密——agent 无人值守，不能停在密码提示上。
- 目标字段（host/user/port/identity）含空白或前导 `-` 一律拒绝，避免配置值被 ssh 当成选项。
- 同一份注册表也是**巡检清单**：可选字段 `tags` / `env` / `services` / `checks` / `enabled`
  用于描述资产与检查阈值（见「确定性巡检」）。本机检查项写在保留条目 `_local` 里，
  它不是 SSH 目标（`_local` 不能用于 `--target` 执行命令）。
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
./opsagent schedule add --daily 09:00 --checks all "解读规则检查结果并给出处理建议"
./opsagent schedule add --every 1800 --timeout 600 "检查 nginx 错误日志"
./opsagent schedule list              # 含预算与最近一次状态（成功/失败/超时/中断）
./opsagent schedule pause <id>        # 暂停：到期不跑；恢复后从当前时间重新起算，不补跑
./opsagent schedule resume <id>
./opsagent schedule runs --tail 20    # 运行历史（可按 --id 过滤，含中断与错过的执行）
./opsagent schedule remove <id>       # id 可用唯一前缀
./opsagent schedule run               # 守护进程（同一时刻只允许一个，flock 防双开）
./opsagent schedule run --once        # 适合 cron：*/5 * * * * ... schedule run --once
```

`--checks all`（或 `--checks web1 --checks-group resources`）声明**任务级规则检查**：每轮先在
调用模型之前跑确定性探针，结果直接落 signal 与报告，模型只负责解读。这样即使 LLM 从第一轮
就不可用（鉴权失败/断网），规则异常仍然会上报、仍然会通知。

agent 里也可以直接说"每天九点巡检一次"，模型用 `schedule` 工具登记（list / runs / add /
pause / resume / remove）。每次执行写审计（`schedule-run`，含状态与结论摘要），注册表的
增删与 daemon 的推进各有锁，不会丢更新。每次巡检的会话也落在 `~/.ops-agent/sessions/`
（计入 300 上限），可在 `/resume` 回看。cron 场景记得在环境里带上 `OPSAGENT_API_KEY` /
`OPSAGENT_BASE_URL`。

### 运行记录与可追踪性（W3）

每次执行在 `~/.ops-agent/runs/<run_id>.json` 留一条记录（0600，保留最近 200 条）：

| 字段 | 说明 |
| --- | --- |
| `run_id` / `task_id` / `session_id` | 定位一次执行；会话 id 可直接 `/resume` 回看 |
| `status` | `running` / `succeeded` / `failed` / `timeout` / `interrupted` / `missed` |
| `verdict` | 从结果里解析的结论：正常 / 警告 / 异常 / 不完整 / 未判定 |
| `started` / `finished` / `duration_s` | 起止与耗时 |
| `attempts` / `attempt_errors` | 只读巡检的有限重试（1 次）与失败原因 |
| `error` / `result_preview` / `report_path` | 失败原因、结论摘要（已脱敏）、关联的巡检报告 |

- **整任务时间预算**：`--timeout`（默认 900s，60–86400）。在轮次与工具调用前检查预算，
  结果标 `timeout`；workflow 里剩余步骤会显式记为「时间预算耗尽」，不会静默跳过。
- **只读重试**：执行抛异常时重试 1 次（间隔 3s），每次尝试用新会话（不重复追加 prompt）。
- **中断 vs 错过**：daemon 被 kill 后遗留的 `running` 记录，在下次启动或
  `schedule runs` 查询时对账为 `interrupted`；停机期间错过的执行时点记为 `missed`
  （含错过次数）。判定依据是心跳（`schedules.json` 的 `meta.last_scan`，每次扫描与每个
  任务结束后刷新），所以**任务自己跑得久或排队不会**被误记为停机错过。
- **退出码**（`run --once`）：`0` 全部正常；`1` 有任务运行失败/超时，或启动时对账出中断；
  `2` 检查发现警告/异常；`3` 不完整/未判定/有错过，或本次因已有调度进程而未执行。
- 执行前会在短锁内复核"仍在且启用、仍到期"再推进 `next_run`：暂停能取消已排队的执行；
  中途被 kill 时，排队中的任务仍是到期状态，下一轮会补上（不会被静默跳过）。
- 单条任务的记录/执行异常不会中断整轮：兜底失败记录同样落盘可查。
- 已删除任务的 run 记录仍在，`schedule runs --id <前缀>` 可按前缀查到。

### 体检、心跳与预算（W7）

```bash
./opsagent doctor                       # 体检：LLM/状态目录/SSH/心跳/通知逐项诊断
./opsagent schedule status              # 调度心跳与任务总览（心跳过期退出 1）
./opsagent budget status                # 今日调用/上限（按本机自然日滚动）
./opsagent budget set --calls 500 --tokens 500000
./opsagent budget recent --tail 10      # 最近调用（含 token 与"（估）"标记）
```

- **doctor**：每项检查给 ✓/⚠/✗ + 证据 + 修复建议——LLM 分"key 缺失 / 鉴权失败(401/403) /
  限流(429) / 网络不通"；SSH 逐台探活；心跳在扫描和每次执行尝试前刷新，过期阈值按启用任务的
  最大超时 + 300s 计算；通知渠道显示脱敏后的 endpoint 与失败待发数。
- **provider 临时错误退避**：429/408/5xx/网络瞬断在 `llm.chat` 内重试 2 次
  （指数退避 3s/6s + 抖动，封顶 30s），401/400 等永久错误绝不重试；
  退避与单次超时都裁剪到任务 deadline 内，累计等待不突破任务预算；流式已输出内容后不重试
  （防止重复推送文本）。
- **usage/调用预算**：每次请求尝试先在锁内占用调用次数，失败和重试也计入，完成后记账
  （usage.jsonl + 按日聚合）；超限立即拒绝新的 LLM 调用
  （`BudgetExhausted`，跨天自动恢复），**确定性规则检查（inspect/场景/通知）不受影响**；
  token 优先采用 provider usage，缺失时估算并计入工具定义和调用参数；token 上限在完成记账后
  阻断后续请求，不能预知并发在途请求的实际用量。状态文件损坏时备份 `.corrupt-*` 并阻断本次
  调用、写事件，随后重建计数；这是告警后恢复策略，不是永久阻断。
- **心跳过期告警**：调度 daemon 启动/`--once` 运行时检查上次心跳；过期经通知状态机告警
  （自动去重），恢复时自动发"已恢复"。
- **常驻部署**：`docs/ops/` 提供 systemd service（+ timer 替代方案）与 launchd plist
  示例，含加固指令与环境变量文件说明。

### 恢复准备与演练（W8）

技能库新增 K8s CrashLoop、Docker 重启循环、DNS/TLS、备份新鲜度与恢复演练，
每份包含证据清单、停止条件、审批和可重复验收。

```bash
# 先停止全部调度、Agent、MCP/RPC 与其他状态写入者，再由人执行离线备份。
./opsx state backup /secure/new-backup --quiesced
./opsx state verify /secure/new-backup
./opsx state restore /secure/new-backup --to /isolated/new-state --quiesced

# 可重复的本机隔离演练：实际进程/文件/HTTP，LLM 和通知使用本地桩。
python3 scripts/w8_acceptance.py --output /tmp/w8-local-evidence.json
```

备份含完整原始秘密与快照，使用私有目录、清单和逐文件 SHA-256。恢复目标必须全新，
旧审批隔离，running 执行转 unknown、调度记录转 interrupted，排期与通知停用，active 长目标暂停并保留轮次。
`--quiesced` 是停写声明，不提供在线全局事务锁；详见[状态恢复操作文档](docs/ops/state-backup-recovery.md)。

本地演练退出 0 仅代表本地通过，JSON 同时列出尚未通过的真实 SSH/LLM/通知和 7 天观察门禁；
`--require-external` 在这些门禁未完成时退出 2。当前[本地证据](docs/validation/2026-10-23-w8-local-evidence.json)
为 10/10 通过，整体仍待外部验收。见[测试环境验收步骤](docs/ops/w8-real-environment.md)和
[受控变更候选版](docs/releases/2026-10-22-controlled-change-candidate.md)。

2026-10-23 W8 独立复审修复中断残留、归档资源边界、恢复状态有效性与演练隔离/证据完整性；
既有自检 309/309、独立回归 168/168（本轮新增 64 项），详见[复审记录](docs/reviews/2026-10-23-week8-followup-review.md)。

### 变更场景（W6）：预检 → 快照 → 执行 → 后验 → 回滚建议

`opsx exec` 只回答"命令退出码是多少"，回答不了"变更之后服务真的健康吗"。场景把
"执行后的确定性验证"变成必经步骤：**命令 exit=0 但后验不健康时，报告的不是"成功"
而是"验证失败"，并给出回滚建议。**

```bash
./opsx scenario list                              # 可用场景
./opsx scenario run systemd-restart --param service=nginx
./opsx scenario run nginx-reload                  # 改完 nginx 配置后应用
./opsx scenario run docker-restart --param container=api
./opsx scenario rollback <execution_id>           # 先展示回滚计划（exit 2）
./opsx scenario rollback <execution_id> --yes     # 确认后恢复并重跑后验作证据
```

内置三个场景（`scenarios/*.json`，自定义放 `~/.ops-agent/scenarios/` 同名覆盖）：

| 场景 | 预检 | 执行 | 后验 |
| --- | --- | --- | --- |
| `systemd-restart` | 单元存在（LoadState=loaded） | `systemctl restart` | ActiveState=active 且 SubState≠failed |
| `docker-restart` | 容器 running | `docker restart` | running 且健康（有 healthcheck 时） |
| `nginx-reload` | `nginx -t` 语法（**坏配置在 reload 前就被拦下**） | `systemctl reload`（含配置快照） | active + 语法仍 ok |

要点：

- **预检失败不写入任何东西**——不建快照、不消耗审批、不产生执行记录（exit 3）。
- 执行命令复用 `opsx exec` 的全部守门（审批/锁/快照/执行记录），场景不复制安全逻辑；
  预检/后验探针强制只读（R0）。
- 结论分清六种"没执行"：需审批（2）、守门拒绝（2）、预检未过（3）、快照未建成（4）、
  目标被占（121）、结果未知（120）——都能直接进脚本。
- 后验不通过或自定义场景未配置后验均退出 7；Docker healthcheck 的 `starting` 不算验证通过。
  预检/后验使用完整输出判断，报告保留脱敏证据；远程场景与回滚后验固定在同一 SSH 端点。
- agent 侧用 `scenario_run` 工具：先展示完整命令与全部探针请求确认，批准后执行；
  无人值守（定时巡检/子代理）不可用。
- 回滚后验证**只作证据不判定**：配置已恢复旧版时，变更后验失败可能正是回滚生效的表现。
  恢复本身失败时不运行后验或写“已回滚”事件；CLI 对失败/未知分别退出 1/120。

### 变更执行记录与并发控制（W5）

变更不再只有"退出码"这一个结果。每次 `opsx exec` / `opsx rollback` 都落一条记录
（`~/.ops-agent/executions/<id>.json`，0600，保留 500 条），带 id、状态、证据：

```
running ──exit=0──────────────────────▶ succeeded
   │
   ├──明确失败（非零退出 / 本机超时且已终止）──▶ failed
   ├──执行前中止（守门 / 快照 / 审批 / 锁冲突）──▶ aborted
   └──无法确认（SSH 断线 255 / 远程超时 / 进程被杀）──▶ unknown
```

**unknown 是唯一需要人介入的终态。** 它表示"命令可能已经生效一部分"——断线、远程超时、
执行进程被 kill 都属于这种。这时系统不会自动重放：同一目标同一命令的新执行会被拒绝
（exit 120），直到有人核实：

```bash
./opsx executions show <id>                          # 看命令、目标、快照、错误
./opsx executions resolve <id> --observed-ok         # 核实：已生效（不要再跑一次）
./opsx executions resolve <id> --observed-failed     # 核实：未生效（可重新批准后执行）
```

核实只记录事实，不提供任何自动动作：`--observed-ok` 之后再执行一次往往是错的
（重复生效），`--observed-failed` 才是"重试"的前提。

| 机制 | 行为 |
|---|---|
| 目标级写锁 | 同一注册 host/port 的名称共用锁，覆盖"快照 + 执行"整段；兼容旧名称锁。抢不到锁 exit 121，并记一条 `aborted` |
| 审批次数 | 一次批准只对应一次执行（`--uses N` 可显式放宽，R3 不可逆操作只能是 1）。次数在**确定要执行**的那一刻才消耗：预检、`opsx check`、hook 都不会用掉授权 |
| 断线判定 | SSH exit 255 / 远程超时 → `unknown`（远端可能仍在跑）；本机超时 → `failed`（进程组已被 kill） |
| 执行前中止 | 参数错误、锁冲突、快照失败、审批用尽 → `aborted`，明确"本次没执行"，不制造待核实项 |
| 部分恢复 | 回滚中途失败会写明"已恢复 N/M 个文件"，不会只说"失败"让人以为文件没动 |

`opsx exec` 的退出码：`0` 成功、`1` 执行失败、`2` 无授权/守门拒绝、`3` 快照失败、
`4` 超时、`120` 有未核实的 unknown 挡着、`121` 目标正被另一次变更占用。
120/121 只描述"本次没执行"，不描述结果——命令自身的退出码可能是 5/6，
所以判断"有没有执行"要看执行记录，不要只看数字。

执行阶段同时检查 pid 与目标锁持有者：进程仍活着但操作已失去自己的锁，也按 unknown 处理，
避免终态写盘失败后重放。损坏执行记录会阻断变更，需要人工核实修复，不会定时删除后自动放行。
未核实 unknown 及仍运行的操作所引用的快照和回滚前副本不会被轮转清理。

目标别名归一依据注册表 host/port，不解析 DNS 或 SSH 配置映射；请保持同一主机的登记一致。
`unknown` 只说明"无法确认"，不代表一定生效，需要按证据人工核实。

### 通知与事件（W4）

一条命令配好出口，之后异常由程序自动推；模型不参与"要不要通知"的判断。

```bash
./opsagent notify setup --url https://hooks.slack.com/services/T0/B0/xxxx \
    --format slack --quiet-hours 23:00-07:00   # 也支持 feishu / dingtalk / generic
./opsagent notify test                 # 发一条测试消息（不受静默影响）
./opsagent notify status               # 渠道、发件箱统计、待重试条数（URL 只显示到主机）
./opsagent notify list --status failed # 失败通知与下次重试时间
./opsagent notify retry                # 立即重试（daemon 每轮也会自动重试）
./opsagent notify mute --for 2h        # 全静默（异常也不发，仍记事件）
./opsagent notify unmute
./opsagent events --tail 30            # 事件时间线
./opsagent events --kind notify-failed
```

**去重、恢复与静默**：每条异常按 fingerprint（检查项 id / 任务 id）记状态，只在四种情况发
消息——新出现、级别升级、超过提醒间隔（`--renotify`，默认 6 小时）、恢复。一次运行最多
**一条**消息，把本次所有新异常聚合成一条；同一持续异常不会每轮刷屏。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `renotify_after` | 6 小时 | 同一异常再次提醒的最小间隔 |
| `quiet_hours` | 无 | 静默时段（只压警告/恢复；**异常与"不可采集"仍然发送**） |
| `mute_until` | 0 | 全静默：**连异常一起压**，只写事件与发件箱，可用 `notify mute` 设置 |
| `enabled` | true | 总开关；停用等于不发送（事件照记） |
| `timeout_seconds` | 8 | 单次 webhook 超时（3–30） |

- **恢复通知**只在"本次确实检查过该目标、且采集完整"时才发：只查 `web1` 的报告不会把
  `db1` 的旧告警清成已恢复；采集不完整就保持沉默，不猜。
- 同一任务分批检查、失败重试会合并已采样的目标和分组，运行记录列出全部报告路径。
  规则异常或采集不完整不会被模型的"正常"结论覆盖；迟到的旧记录不会覆盖较新的观测。
- **失败可查可重试**：发送失败落 `~/.ops-agent/notifications/<id>.json`（失败原因、HTTP 状态、
  下次重试时间），daemon 每轮最多重试 5 条，指数退避（1min 起，上限 1h，最多 5 次）。已恢复
  的旧告警标记 `superseded` 不再补发，避免"先收到恢复、又收到告警"。
- **事件时间线**：`~/.ops-agent/events.jsonl`（0600，5MB 轮转一次，轮转后仍可 `--tail` 回看）。
  只记状态变化：异常出现/升级/提醒/恢复、通知发送/失败/静默、运行失败/中断/错过，不记每次
  正常巡检——否则时间线会变成第二个日志洪水。
- **关联证据与会话**：消息里带 `run_id`、`session_id`（可 `/resume` 回看）、报告路径，以及每条
  异常的探针命令与输出片段；`schedule runs` 也会显示该次运行的通知状态。
- **凭据**：webhook URL 的 path 往往就是 token。`notify.json` 0600，事件/发件箱/审计只存
  `scheme://host/…`；`read` 与 `bash` 读取 `notify.json` 或整个状态目录会被拒绝。走系统代理
  （`http_proxy`/`https_proxy`），内网 webhook 需要设 `no_proxy`。

**长目标（goal）**：在 REPL 里让 agent 推进多步目标（"排查到定位为止""升级并验证完"）。
每轮结束后按目标状态自动续跑，默认最多 6 轮（`max_rounds` 可调，上限 20），完成后自动停。
续跑中的写操作仍然逐次需要用户确认；`/goal` 查看状态，`/goal stop` 暂停，`/goal resume`
恢复并重置轮次预算。`-p` / RPC / 定时任务不会自动续跑（单发语义），`goal` 工具在这些
形态下拒绝创建，避免留下永远不推进的目标。

**巡检工作流（workflow）**：把固定巡检步骤写成 JSON——仓库 [workflows/](workflows/) 自带
`daily-inspection`，`~/.ops-agent/workflows/*.json` 同名覆盖。`checks` 块先跑确定性规则检查
（不调用模型），再由只读子代理逐步解读：

```json
{"description": "每日巡检",
 "checks": {"targets": "all", "groups": ["connectivity", "resources", "services"]},
 "steps": [
  {"name": "解读", "prompt": "规则结果：{checks}"},
  {"name": "取证", "prompt": "针对异常做只读取证：{prev}"},
  {"name": "汇总", "prompt": "规则结果：{checks}\n前序步骤：{steps}"}
]}
```

占位符单次替换：`{checks}` 规则报告、`{prev}` 上一步结论、`{steps}` 前序步骤索引。
末尾「汇总完整性」由程序生成——列出规则检查的全部异常与每步状态，模型不能改写；
输出过长被截断时也会保留这一段，避免「早期异常被最后一步的总结覆盖」。

agent 里先 `workflow`（不带 name）列目录，再 `workflow {"name": "daily-inspection"}` 运行；
定时任务的 prompt 也可以写"运行 workflow daily-inspection 并报告异常"。
`opsagent schedule add --daily 09:00 "运行 workflow daily-inspection 并报告异常"` 就是一套
完整的每日巡检。

## 确定性巡检（W2）

检查项固定写在代码里，模型只做解读：`opsagent inspect` 不需要 API Key 也能出结论，
API 完全不可用时 workflow 的规则结果照常产出。

```bash
./opsagent inspect                                   # 本机 + 全部已注册目标，三组全跑
./opsagent inspect --target web1                     # 单个目标
./opsagent inspect --group resources                 # 只跑资源组（结论最高为「不完整」）
./opsagent inspect --json                            # 结构化报告到 stdout
./opsagent inspect --explain                         # 追加 LLM 解读（可选，失败不影响结论）
```

三组检查（默认全跑，缺任一组都不算完整）：

| 分组 | 采集内容 | 判定依据 |
| --- | --- | --- |
| `connectivity` | `uname -s`（同时确定探针平台） | 命令失败即 `error`；输出解析不出平台记 `unknown` |
| `resources` | `df -P -k`（磁盘）、`/proc/meminfo` 或 `vm_stat`（内存）、`/proc/loadavg` 或 `sysctl -n vm.loadavg`（负载，除以核数） | 与阈值比较：≥ 警告为 `warn`、≥ 异常为 `critical` |
| `services` | `systemctl show`（systemd）、`docker inspect --format`（容器） | 期望运行的单元 `failed/inactive` 为 `critical`；单元/容器不存在、命令不可用为 `unknown` |

状态只有五种：`ok` / `warn` / `critical` / `unknown` / `error`。**采不到不等于健康**——
目标不可达、权限不足、输出无法解析都记 `unknown`/`error`，报告结论只有
「无异常 + 三组全跑 + 无未采集项」才可能是「正常」，否则分别是「警告」「异常」「不完整」。

配置写在 `~/.ops-agent/targets.json`（与 SSH 目标同一份，新字段全部可选）：

```json
{
  "_local": {"tags": ["workstation"],
             "services": [{"type": "docker", "name": "redis"}],
             "checks": {"disk_paths": ["/", "/Users"]}},
  "web1": {"host": "10.0.0.11", "user": "ops", "tags": ["prod", "web"],
           "env": {"role": "nginx", "os": "ubuntu-24.04"},
           "services": [{"type": "systemd", "name": "nginx"},
                        {"type": "docker", "name": "api", "expect": "running"}],
           "checks": {"disk_paths": ["/", "/data"], "disk_warn_pct": 85,
                      "disk_crit_pct": 95, "mem_warn_pct": 90, "mem_crit_pct": 95,
                      "load_warn_per_core": 2.0, "load_crit_per_core": 4.0,
                      "timeout": 20, "budget_seconds": 120}},
  "cold-standby": {"host": "10.0.0.30", "enabled": false}
}
```

- `services[].type` 只能是 `systemd` / `docker`；名称只允许字母数字与 `@ . _ : -`。
- `checks` 未知字段、非法阈值、非法目标顶层字段一律报错（拼错不该静默生效）。
- `disk_paths` 允许空格与非 ASCII（如 `/Volumes/My Disk`），拒绝控制字符与 `..`。
- `budget_seconds` 是单目标的检查时间预算（默认 120s），另有 60 次探针的硬上界；
  超预算的检查项记 `unknown`，不会静默漏项，也不会让一台卡死的主机拖住整轮巡检。
- `enabled: false` 的目标在 `--target all` 时跳过，并在报告的「覆盖」行点名。
- 完整性口径是**已启用目标**：被显式停用的目标不计入「不完整」，但会列在覆盖行里。
- 报告全文落盘 `~/.ops-agent/reports/*.md`（0600，保留最近 100 份，落盘前已脱敏）；
  工具/终端只回显摘要，异常清单无论多少条都会完整列出。

退出码（脚本/cron 可判定）：`0` 正常、`2` 有警告或异常、`3` 不完整（未采集/漏分组）、
`1` 检查无法执行（配置错误、参数错误）。

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
`.aws/credentials`、`.docker/config.json`、`.config/gcloud/`、`/etc/shadow`、
`~/.ops-agent/notify.json`（webhook URL 即凭据，0600 也挡不住同 uid 的模型进程）
与 `.ssh`/`.aws`/`.kube`/`.gnupg` 及整个 `~/.ops-agent` 目录（递归读取会顺带扫到
`notify.json`；`~/.ops-agent/reports/` 下的报告仍可读）；命令入口（`opsx check`、hook、bash 工具、`opsx exec`）
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
② PreToolUse hook 只放行 R0 只读命令或本仓库受守门的 CLI；裸写命令即使有审批戳也拒绝。

`opsx exec --snapshot-cmd`、独立的 `opsx snapshot --cmd` 与 MCP `ops_snapshot_cmd`
仅允许 R0 只读抓取，已批准的非 R0 命令也不能通过采集入口执行。
写命令统一走 `opsx exec`，才能消耗审批次数并使用写锁与执行记录。
状态目录与快照目录按 `0700` 创建，会话与输出文件为 `0600`——
远程拉回的配置/密钥只有本人可读。
本机文件回滚先写临时文件再原子替换，恢复内容与权限但会改变 inode；
远程文件回滚也原子替换，会改变 inode，并保留已有文件的属主/属组。

hook 允许直接调用本仓库的 `opsx`（或 Python + 本仓库脚本），由入口内部校验审批与快照；
入口外拼接 shell 命令仍逐条守门。审批戳保留引号、转义和换行里的空白，避免不同命令复用授权；
升级前为这类命令创建的旧戳可能需要重新授权。

审批戳可随时 `opsx revoke`（agent 内为 `ops_revoke`）撤销，不必等 TTL 过期；
远程戳按目标绑定，撤销本机同名命令不会波及目标机。

已知边界：审批戳由 Agent 侧在获得用户确认后创建，防"误执行"不防"恶意 Agent"；
命令分类为线性正则匹配，不解析 shell AST（复合命令按 `;` `&&` `||` `|`、换行与后台 `&`
分段取最高级）。命令超时会停止本地进程组；远程超时只保证本地 SSH 已停止，
目标机上的进程是否退出须另行检查。

巡检侧的已知边界：探针只覆盖连通性、磁盘/内存/负载与 systemd/Docker 服务，不替代外部监控；
`unknown` 只说明"没采到"，不说明"坏了"，需要人按报告里的证据命令复核；巡检期间若有人改写
`targets.json`，同一次报告的不同探针可能打到不同主机（报告按开始时的目标快照展示）。

调度侧的已知边界：任务串行执行，一个长任务会推迟后面的任务（不会并发）；时间预算在轮次
与工具调用前检查，已启动的调用仍受自身超时约束；`interrupted` 判定依赖 pid 存活，pid 被
复用（重启后同号进程）时可能误判为仍在运行；`missed` 依据心跳推断，写盘失败时会保守地少报；
W4 已支持单向 webhook、事件去重与恢复通知；双向 IM 对话尚未接入。

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

已按八周排期交付：W1 安全边界 → W2 确定性巡检 → W3 可追踪运行 → W4 通知与事件 →
W5 变更执行记录与并发控制 → W6 变更场景 → W7 体检/心跳/预算/退避。
状态目录新增 `runs/`（运行记录）、`reports/`（巡检与场景报告）、`signals/`（规则结果摘要）、
`notifications/`（发件箱）、`events.jsonl`（事件时间线）、`notify.json`（通知渠道，0600）、
`executions/`（变更执行记录）、`locks/`（目标级写锁）、`usage.jsonl`/`usage-state.json`/`budget.json`（调用记账与预算，0600）。这些文件都是本地事实来源，不需要数据库。
调度、运行、通知与执行记录 JSON 用临时文件替换；JSONL 查询跳过损坏行；配置损坏会报错；
重要状态仍应备份。

2026-10-22 对照路线图复审并修复 W1–W4（`241/241`），随后交付 W5（`261/261`）、
W6（`300/300`）、W7（`309/309`）：详细问题、修复与验证见
[W1–W4 复审记录](docs/reviews/2026-10-22-roadmap-implementation-review.md)、
[W5 复审记录](docs/reviews/2026-10-22-week5-change-safety-review.md) 与
[W6 复审记录](docs/reviews/2026-10-22-week6-scenario-review.md) 与
[W7 复审记录](docs/reviews/2026-10-22-week7-reliability-review.md)。

本次对当前 W1–W7 再次复审后，既有自检 **309/309**、新增回归 **60/60** 通过；
问题与兼容行为调整见[路线图后续复审记录](docs/reviews/2026-10-22-roadmap-followup-review.md)。

W5 再次复审修复后：`./opsx selftest` **292/292 passed**，新增 31 项回归，
详见[二次审查记录](docs/reviews/2026-10-22-week5-followup-review.md)。升级前请结束在途变更；
旧名称锁已桥接，但升级期间不应同时调整目标别名、SSH 配置或状态目录。
