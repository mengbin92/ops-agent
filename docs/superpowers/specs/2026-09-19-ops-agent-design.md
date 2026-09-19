# 运维 Agent 设计文档

日期：2026-09-19
状态：已评审通过，待实现

## 1. 背景与目标

一个专门负责运维的 Claude Code 子代理（ops agent），核心安全诉求：

- **默认只读**：所有操作默认为只读，写操作是例外。
- **高风险需授权**：高风险操作执行前必须经用户会话内确认，并附风险说明。
- **可回滚**：变更前自动快照，提供 `opsx rollback` 一键回滚。
- **可审计**：所有判定、审批、执行、回滚落审计日志。

运维目标环境：本机 macOS、远程 Linux 服务器（SSH 直连）、K8s 集群（kubeconfig 直连）、Docker 环境。

实现路径：方案一（Hooks + 审批戳机制），核心逻辑未来可平移为 MCP server。

## 2. 组件与目录结构

```
ops-agent/                          # 独立 git 仓库，专用运维工作区
├── .claude/
│   ├── agents/ops.md               # Agent 定义：角色、风险分级、操作流程
│   └── settings.json               # PreToolUse hook 注册（项目级，仅本目录生效）
├── opsx                            # CLI 入口（python3，纯标准库）
├── core.py                         # 核心逻辑库：分类/审批戳/快照/回滚/审计
├── config.yaml                     # 只读白名单 + 风险规则
└── README.md                       # 使用说明 + 协议 + 已知边界
```

运行时状态目录 `~/.ops-agent/`（不进 git）：

```
~/.ops-agent/
├── approvals/                      # 审批戳文件（命令哈希 + 时间戳 + TTL）
├── snapshots/<snapshot_id>/        # 快照（文件副本 + 捕获的命令输出 + meta.json）
├── audit.jsonl                     # 审计日志（追加写）
└── config.override.yaml            # 个人覆盖配置（深合并，覆盖仓库级 config.yaml）
```

## 3. 风险分级模型

| 级别 | 定义 | 例子 | 流程 |
|------|------|------|------|
| R0 | 只读查询 | `kubectl get/describe/logs/top`、`systemctl status/show`、`docker ps/logs/inspect`、日志检索（cat/grep/journalctl）、`df/du/free/ps` | 直接执行，hook 白名单放行 |
| R1 | 可逆低风险写 | 重启服务（`systemctl restart`）、`docker restart`、修改有快照的非关键配置 | 变更单 → 会话内确认 → 快照 → 执行 |
| R2 | 高风险写 | 部署/升级、扩缩容、修改关键路径配置、包安装、防火墙规则变更 | 变更单 → 会话内确认 → 快照 → 执行 → 事后报告含回滚命令 |
| R3 | 不可逆 | `rm -rf`、`kubectl delete`、`docker rm/rmi`、`systemctl stop/disable`、磁盘格式化、数据库 drop | 默认拒绝；仅用户显式输入确认后放行，必须注明"无回滚" |

### 3.1 复合命令处理

按 `;`、`&&`、`||`、`|` 分段，对每段独立分类，**取最高风险级**作为整条命令的级别。审批戳按整段规范化命令（压缩空白后的 SHA-256）计算，不允许分段授权拼装绕过。

已知边界（ponytail）：命令替换 `$(...)`、反引号、here-doc 内部内容不做递归解析，规则匹配以线性模式匹配为准，升级路径是引入真正的 shell 解析器（如 `shlex` + 逐 token 判定）——当前模型下（防误执行而非防恶意）已够。

## 4. 核心流程

### 4.1 R1/R2 变更流程（正常路径）

1. Agent 判定操作级别，向用户输出结构化**变更单**，包含：
   - 目标环境（本机/某台服务器/某集群）
   - 待执行命令（完整、可直接复制）
   - 风险说明（为什么是这个级别、影响面）
   - 回滚计划（对文件/配置类：快照恢复；对服务类：先捕获当前状态再执行逆操作）
   - 快照计划（快照什么、怎么快照）
2. Agent 用 AskUserQuestion 请求确认；用户拒绝则终止并说明原因。
3. 用户批准后，Agent 调用 `opsx exec '<命令>' [--snapshot-file <路径> ...] [--snapshot-cmd '<状态抓取命令>' ...]`，快照参数取自变更单中的快照计划，可多个。该命令原子完成：
   - 校验：白名单或有效审批戳存在
   - 快照：依次执行传入的快照参数；任一步失败即中止，不执行目标命令
   - 执行：实际运行命令
   - 审计：写入 exec 记录（含 snapshot_id、退出码）
4. 执行后 Agent 报告：结果、快照 ID、回滚命令（`opsx rollback <快照ID>`）。

### 4.2 hook 兜底路径

Agent 绕过流程直接发起 Bash 调用时：

1. PreToolUse hook 触发 `opsx check '<命令>'`。
2. 命中只读白名单或有有效审批戳 → exit 0，放行。
3. 否则 exit 2，stderr 输出拒绝原因 + 指引（"该命令属于 R2，请先提交变更单并经用户确认后使用 opsx exec 执行"）。
4. 无论放行与否，check 结果写入审计日志。

### 4.3 R3 流程

Agent 收到 R3 类请求时的默认行为是拒绝并解释风险。仅当用户在会话中显式输入确认（例如明确回复"确认执行"）后，Agent 才允许执行，且变更单中必须注明"此操作不可逆，无回滚方案"。审批戳需带 `--force` 标记才生效。

### 4.4 审批戳机制

- 内容：规范化命令的 SHA-256、原始命令、创建时间、TTL（默认 15 分钟）、force 标记、审批方式（interactive / manual）。
- 存储：`~/.ops-agent/approvals/<sha256>.json`。
- 有效期：创建时间 + TTL；过期即失效，需重新走流程。
- 执行由 `opsx exec` 完成；Agent 在获得用户确认后先 `opsx approve '<命令>'` 创建审批戳，再 `opsx exec` 执行（该流程在 ops.md 中固化）。`opsx approve --ttl 1800`（单位秒）供用户手动预授权（降级路径用）。

### 4.5 快照与回滚

- 文件/配置类：通过 `opsx exec --snapshot-file <路径>` 在执行前复制到 `snapshots/<id>/files/<原路径>`，meta.json 记录原始路径与权限位。`opsx rollback <id>` 恢复文件内容与权限。
- 服务/部署类：执行前 Agent 必须通过 `opsx exec --snapshot-cmd '<抓取当前状态命令>'` 捕获当前状态（如旧镜像 tag、`systemctl cat` 输出、当前 replica 数），保证"有快照才执行"。`opsx rollback <id>` 输出捕获的状态，Agent 据此执行逆操作（回滚本质是两阶段：恢复数据 + Agent 执行语义逆操作）。
- 单独使用（不随 exec）：`opsx snapshot --file/--cmd` 也可独立调用，供只读场景做手动备份。
- 快照保留策略：默认保留最近 200 个，超出按时间清理；清理写入审计日志。

### 4.6 降级路径

若运行环境不支持子代理内使用 AskUserQuestion：Agent 输出变更单后结束轮次，用户在主会话手动执行 `! opsx approve '<命令>'` 预授权，然后让 Agent 继续执行。README 写明两种模式。

## 5. opsx CLI 规格

| 子命令 | 职责 | 关键行为 |
|--------|------|----------|
| `opsx check <cmd>` | hook 入口 | 白名单/审批戳 → exit 0；否则 exit 2 + 原因。只写审计，不执行 |
| `opsx exec <cmd> [--snapshot-file ...] [--snapshot-cmd ...]` | 原子执行变更 | 校验 → 快照（按传入参数）→ 执行 → 审计；任何一步失败即中止并写审计 |
| `opsx approve <cmd> [--ttl 秒] [--force]` | 手动预授权 | 创建审批戳，打印风险级与有效期 |
| `opsx snapshot --file <p>` / `--cmd '<c>' [--name n]` | 创建快照 | 返回 snapshot_id，写审计 |
| `opsx rollback <snapshot_id>` | 回滚 | 恢复文件 / 输出捕获状态，写审计 |
| `opsx list [--snapshots\|--approvals]` | 列出状态 | 只读 |
| `opsx selftest` | 自检 | 内置 assert 测试集，见第 8 节 |
| `opsx audit [--tail N]` | 查看审计 | 只读 |

技术约束：python3，纯标准库（json、hashlib、shutil、subprocess、argparse、pathlib、re、datetime、uuid）。core.py 为纯函数库，CLI（opsx）为薄壳——这是 MCP 演进的接口边界。

## 6. Agent 定义要点（.claude/agents/ops.md）

- frontmatter：`name: ops`，`tools: Bash, Read, Grep, Glob, AskUserQuestion`。
- 系统提示词包含：
  - 角色与目标环境清单（本机、远程 Linux、K8s、Docker），访问方式（SSH/kubeconfig 直连）。
  - 四级风险定义 + 每级典型命令例子（与 config.yaml 的规则对应）。
  - R1/R2 强制变更单流程；R3 强制拒绝 + 显式确认要求。
  - 执行后报告义务：结果、快照 ID、回滚命令。
  - 默认中文输出；只读优先（先查状态再提变更）。
  - 明确禁止：绕开 opsx 直接执行写操作、拆分命令规避审批、未经确认的 R3。

## 7. Hook 配置（.claude/settings.json）

项目级注册 PreToolUse / Bash hook，调用仓库内 `opsx check`。

重要边界：Claude Code hook 只能按工具名匹配，不能按 Agent 匹配。因此防线生效的前提是**在 ops-agent/ 目录内启动 ops agent**（hook 项目级生效）。在目录外启动时仅有 prompt 约束一层防线，README 必须显著写明。未来若 harness 支持按 Agent 匹配 hook，再收紧。

## 8. 测试策略

`opsx selftest` 用 assert 实现（无测试框架），覆盖：

1. 白名单命令（R0 样例）被 check 放行。
2. 未审批的 R1/R2 命令被 check 拒绝（exit 2）。
3. 审批戳存在且在 TTL 内 → 放行；过期 → 拒绝。
4. 复合命令取最高风险级（`docker ps && rm -rf /tmp/x` → R3）。
5. 快照-回滚往返：建临时文件 → snapshot → 修改 → rollback → 内容一致。
6. 审计日志每条事件可被解析（JSONL 逐行 json.loads 成功）。

selftest 全部通过是仓库 CI/提交前的唯一硬性检查。

## 9. 错误处理

- 快照失败 → exec 中止，不执行目标命令，审计记录失败原因。宁可不变更，不可无快照变更。
- 执行失败（非零退出码）→ Agent 依据快照与回滚计划决定是否回滚；exec 将退出码与输出如实返回。
- 审批戳校验失败、配置文件解析失败 → 明确错误信息 + exit 非零，不写部分状态。
- hook 自身异常 → exit 2（拒绝方向），宁可误拒不可误放。

## 10. 演进路径（预留）

core.py 从第一天即为纯函数库。演进 MCP server 时：

- 新建 `mcp_server.py`，将 classify/stamp/snapshot/rollback/audit 包装为 MCP tools（元数据带 risk_level）。
- 状态目录 `~/.ops-agent/` 的布局即对外契约，保持不变。
- CLI 与 MCP 并存，共享 core.py，互不依赖。

## 11. 明确不做（YAGNI）

- 异步审批队列 / 双人复核（当前会话内确认足够）。
- 多机并发编排（脚本保持单命令粒度）。
- Web 界面 / 通知渠道。
- 真正的 shell AST 解析（见 3.1 已知边界）。
- 防恶意 Agent 的密码学级强制（审批戳由 Agent 侧创建，威胁模型为防误执行）。
