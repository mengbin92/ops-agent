# ops-agent v0.1.0-rc.2 发布：全项目审查根因修复与 A2A 协议形态

> 2026-11-01 · 上一版：[v0.1.0-rc.1](release-v0.1.0-rc.1.md) · [GitHub Release](https://github.com/mengbin92/ops-agent/releases/tag/v0.1.0-rc.2)

v0.1.0-rc.2 是第二个**候选发布版本（prerelease）**：对整个项目做了一次完整 codex review
（core 守门核心逐行 + 六路模块审计 + 实机探针验证），20 项发现全部按根因方案修复，并新增 41 项回归测试。
同时带入 rc.1 之后合入的 A2A 协议形态、变更单快照声明与 logscan 确定性日志粗筛。

真实环境连续 7 天只读观察进行中（2026-10-28 16:57 起，现至 day5），按发版纪律本版仍为候选版；
本地 321/321 自检与全量回归通过不替代外部验收。运行时零第三方依赖（A2A 形态可选），Python 3.9+。

## 发布内容

### 1. 敏感文件绕过快照通道封堵（守门，最高优先级）

**根因**：`cat ~/.ssh/id_rsa` 被守门拒绝，但 `snapshot_file()` 复制文件时不做敏感路径判定——
MCP `ops_snapshot_file` 甚至无需审批即可触发。凭据经快照落盘、被 `state_backup` 打进 `state.tar`，
且回滚预览的 diff 只做模式脱敏（`/etc/shadow` 哈希形态不在覆盖内），内容可回流确认框与模型上下文。

**修复**：新增 `core.sensitive_snapshot_path()`（与命令读取同一套判定，含等价写法与 realpath），
在 `_snapshot_file_into` 与 `remote._pull_file` 两个咽喉点拒绝；MCP、`opsx snapshot --file`、
`ops_exec` 快照声明、远程拉取四个入口一并收紧。确需变更凭据文件请人工备份。

**影响入口**：`opsx snapshot/exec`、Agent `ops_exec`、MCP 全部快照面、远程快照。

### 2. 守门骨架统一与远程回滚可用性

**根因**：`core.exec_change` 与 `remote.exec_change` 是 110 行近似拷贝且已漂移（host 字段、
剩余次数处理各一套），守门修复需落两次；`_remote_read` 要求标记位于 stdout 行首，
登录 rc 打印任何内容（Ubuntu 非交互 ssh 会 source `.bashrc`）就让远程回滚预览与回滚前副本全部失败。

**修复**：远程 110 行删除，改为 `_ExecTransport` 注入 `core.exec_change` 单一骨架
（守门→锁→重验→快照→消耗审批→执行→记录状态）；`_remote_read` 改尾标记协议
（与 `_target_resolved_path` 同思路），字节级往返有测试；快照拉取/抓取接受执行超时传参，
大文件慢链路不再被 IO_TIMEOUT=120s 一刀切。

**影响入口**：本机/远程全部变更执行、远程快照回滚、`opsx exec --target`。

### 3. 排期漂移根治与调度可靠性

**根因**：every 任务 `next_run = now + interval` 滑动排期，且取任务与完成时**双重锚定**
（claim 推进后被 finish 的 reanchor 覆盖）——每周期漂移 = 认领延迟 + 整段执行时长，无上界累积
（观察实测 day5 已漂 ~20 分钟）；daemon 启动对账路径无 OSError 捕获，磁盘问题直接裸崩。

**修复**：取任务时按槽位网格锚定（`anchor+k*every`），finish 不再重锚 every 任务（daily 保留 DST 重锚）；
漂移降为常数 ≤60s。daemon 对账逐条写盘与整体双层容忍 OSError（`_safe_audit` 留痕后继续）。
睡眠下限 30s→1s（到期任务不再恒定晚 30s）。

**影响入口**：`opsagent schedule` 全部形态、7 天观察的定时巡检。

### 4. 场景后验占位符与参数安全

**根因**：precheck/exec 的占位符在执行前替换，postcheck 只在变更**已执行后**替换——
自定义场景后验命令里的拼写错误（`{contaner}`）导致"变更已生效、验证从未运行"，
无结论无报告无回滚提示；参数名 `end/else/if` 会打碎 docker `{{end}}` 模板。

**修复**：load() 全量校验所有命令字段的占位符完备性（错误精确到字段与占位符名），
postcheck 在 exec 前再做一次兜底替换；保留字参数名拒绝。

**影响入口**：`opsx scenario run`、`scenario_run` 工具、全部自定义场景。

### 5. agent 凭据与审批-执行参数一致性

**根因**：bash 工具全量继承环境，R0 白名单里的 `env`/`echo $VAR` 原样回显
`OPSAGENT_API_KEY`（自定义网关的非 sk- 形态 key 不在脱敏模式内）；
`ops_approve` 收 `snapshot_files` 数组而 `ops_exec` 只收单数 `snapshot_file`——
按 skill 指引声明多文件快照的变更单"批准后必被拒"。

**修复**：子进程环境无条件剔除 `OPSAGENT_API_KEY`/`OPSAGENT_A2A_TOKEN`，dispatch 再按值清洗
（双保险）；`ops_exec` 支持 `snapshot_files` 数组（旧参数并集兼容），与 `ops_approve`
共用同一份 `_normalize_snapshot_args`，结构上不可能再漂移。

**影响入口**：Agent bash 工具、`ops_approve`/`ops_exec`（agent 与 MCP 面）。

### 6. 其余审查修复

- **会话**：修剪跳过 24h 内有活动的会话（子代理/工作流高频建会话不再误删活跃空闲会话）；
  `list_sessions` 改纯文本流式读取（列表操作不再写文件）。
- **通知/巡检**：checks 状态→severity 单一映射（未知状态不再静默通知为最高级别"不可采集"）；
  `process_run` 抽出 `_apply_delivery`；logscan 聚类组上限 5000 并如实标注截断。
- **CLI/退出码**：`opsx rollback` 对结果未知的远程回滚返回 120（与 exec/scenario 契约一致）；
  ssh `options` 缺 `-` 前缀提前拒绝；MCP `ops_audit` 断行容忍；`ops_executions`/schedule/MCP
  三面 tail 一致收口 1-200。
- **usage**：账本轮转移入状态锁（daemon+CLI 并发不再提前清掉归档代），补并发轮转测试。
- **A2A**：`opsagent a2a serve --read-only`（写操作在无确认通道时自动降级拒绝）；
  "一次答复只批准一个变更"不变量测试（变异验证：改坏守卫时测试必失败）。
- **内核小项**：rpc SIGINT 干净退出；`/resume` 损坏会话不崩 REPL；流式渲染去重复；
  compact 保留窗口不再计入系统消息；Anthropic 默认模型名单一来源；
  流式输出后不重试的守卫测试（变异验证）。
- **CI**：新增独立 A2A job（装 `requirements-a2a.txt` 跑单测+e2e——此前唯一面向网络的
  入口在 CI 里全部静默跳过）；W8 演练不再每腿双跑；删除无调用死代码约 22 行。

### 7. rc.1 后合入的能力（本版一并发布）

- **A2A 协议形态**（`opsagent a2a serve|card|call|doctor`）：AgentCard + JSON-RPC，
  只读直通 / 写操作 `TASK_STATE_INPUT_REQUIRED` 二次确认，Bearer 校验与回环默认。
- **变更单结构化**：ops_approve 必填影响面 `impact` 与快照声明，审批戳与执行参数强一致。
- **logscan** 确定性日志粗筛、kubectl diff/--dry-run 预演进 R0 白名单。

## 兼容性说明

- 状态目录/审计/快照格式无破坏性变更；`remote.exec_change` 对外签名与返回值不变。
- agent `ops_exec` 新增 `snapshot_files` 数组参数；旧 `snapshot_file`（单数）继续可用（取并集）。
- `ops_executions` 类查询的 `tail` 上限统一为 200（agent/MCP/schedule 三面一致，超限报错而非截断）。
- 敏感路径（凭据文件、`.ssh`、状态目录等）不能再作为快照源——这是有意的收紧；
  确需变更凭据文件请人工备份后走变更流程。
- bash 子进程环境不再包含 `OPSAGENT_API_KEY`/`OPSAGENT_A2A_TOKEN`（远程命令本就不经本地环境）。

## 安装与升级步骤

```bash
git fetch --tags
git switch v0.1.0-rc.2   # 或下载 GitHub Release 的源码包与 SHA256SUMS
./opsx selftest           # 321 项全过为基线
./opsagent doctor         # 环境体检
```

A2A 形态（可选）：`python3 -m pip install -r requirements-a2a.txt`（需 Python ≥ 3.10）。
在跑 7 天观察的实例：正常 `git pull` + 重启 systemd 单元即可，排期状态文件格式未变。

## 验证

- 本地（macOS, Python 3.9.6）：`python -m compileall` 全量通过；`python -m unittest discover`
  全过（A2A SDK 用例按设计跳过，由 CI 独立 job 覆盖）；`./opsx selftest` **321/321**。
- 守门探针 12 组绕过尝试全部按预期拦截；新增敏感快照门、多文件变更单端到端实测通过。
- CI（push 后）：Ubuntu/macOS × Python 3.9/3.12 矩阵 + 独立 A2A job（真实 SDK 依赖 + e2e）。

## 完整变更日志

见 [CHANGELOG](../../CHANGELOG.md) 的 `[0.1.0-rc.2]` 段。
对应提交：`fix: 全项目审查修复——守门绕过、排期漂移、场景后验与内核可靠性 20 项根因修复`（8f33af3）。
