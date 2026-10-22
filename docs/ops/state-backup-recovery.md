# 状态备份与安全恢复

`opsx state` 使用 Python 标准库，把完整状态目录保存成私有备份目录，再恢复到一个**从未存在的新目录**。默认状态目录是 `~/.ops-agent`；可用 `OPSX_STATE_DIR` 指定。源状态不被修改，恢复不会覆盖当前活跃状态。

## 一致性前提：先停全部写入者

备份和恢复之前，停止 systemd/launchd 托管的调度进程、cron 任务、所有 Agent/REPL、MCP 服务及可能读改写该状态目录的 CLI；确认进程已经退出。根据实际安装方式使用 [托管示例](../../README.md)中的服务名，不要同时启动另一个终端里的巡检。单纯暂停某个 schedule 不会停止其他写入者。

`--quiesced` 表示操作者已经完成停写确认。工具不会停止进程，也没有覆盖整个状态目录的全局事务锁。它会核对文件与目录清单、文件大小/标识/修改时间、归档及逐文件 SHA-256，以发现常见的备份期间变化；这些检查**不能把在线写入变成全局事务快照**。发现变化或可捕获的中断时，清理本次新建的不完整备份并报错，须停止所有写入者后重做。清理前核对目录身份，目录已被替换时不会删除替代物。

原生支持 macOS/Linux。macOS 的系统别名 `/var → /private/var`、`/tmp → /private/tmp`、`/etc → /private/etc`会转换成规范路径；用户自行创建的路径软链接仍会被拒绝。

## 创建与验证

先确保备份父目录存在且由自己控制。每次选用新的备份目录名；工具拒绝覆盖已有文件或目录，也拒绝在源状态目录中建立备份。

```sh
mkdir -p "$HOME/ops-agent-backups"
chmod 700 "$HOME/ops-agent-backups"

# 前置：已停止全部状态写入者。
./opsx state backup "$HOME/ops-agent-backups/2026-10-22" --quiesced
./opsx state verify "$HOME/ops-agent-backups/2026-10-22"
```

成功备份的目录固定包含：

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 格式版本 `format: 1`、创建时间、停写声明、归档 SHA-256、完整目录/文件清单、逐文件大小与 SHA-256、原权限数值 |
| `state.tar` | 未压缩的完整状态原始字节，含快照原件和原 `meta.json` |

备份目录权限为 `0700`，两个文件均为 `0600`。源状态中的常规文件完整保留，包含 `notify.json` 中真实 webhook 凭据、配置覆盖、目标清单、会话、事件、审计、报告、发件箱、预算、用量与快照；不会脱敏破坏恢复。备份含完整秘密，按凭据材料保管。工具不提供加密；如需异地保存，使用现有加密备份设施，并保护备份副本与密钥。

校验器拒绝软链接、硬链接、设备/FIFO、路径穿越、绝对路径、重复/额外/缺失条目及目录/文件冲突；也检查归档和逐文件摘要。它不使用 `extractall`。从外部复制备份后须保持目录私有、文件不允许组/其他人访问。哈希校验提供完整性检查，**不是签名认证**；同时改写归档和 manifest 的人可以制造另一份结构合法的备份，恢复来源必须可信。

FIFO 容器文件也会立即拒绝，不会等待读者/写者；tar 扩展头读取有界，结束标记后的附加数据和截断归档拒绝。
半成品备份仅有 `state.tar` 时仍受 Agent 读取保护，不必等待 manifest 发布。SIGKILL、断电等无法执行清理的情况，需由操作者停写后检查并隔离残留，不能将其作为可用备份。

## 恢复到独立目录

保留当前状态和备份。目标目录必须不存在，其父目录必须已经存在。先恢复副本并检查，不要直接把新副本投入无人值守运行。

```sh
# 前置：已停止全部状态写入者；~/.ops-agent-restored 必须不存在。
./opsx state restore "$HOME/ops-agent-backups/2026-10-22" \
  --to "$HOME/.ops-agent-restored" --quiesced

# 检查恢复副本；显式指定路径，避免误查原状态。
OPSX_STATE_DIR="$HOME/.ops-agent-restored" ./opsx list
OPSX_STATE_DIR="$HOME/.ops-agent-restored" ./opsx executions
OPSX_STATE_DIR="$HOME/.ops-agent-restored" ./opsagent schedule list
OPSX_STATE_DIR="$HOME/.ops-agent-restored" ./opsagent notify status
```

恢复会验证整份备份后才建立新目录；写入时再逐文件校验。失败只清理本次新建的恢复目录，源状态与备份均保留。所有恢复目录为 `0700`、文件为 `0600`；不保留原 owner/group、ACL、扩展属性或文件时间。快照内容及其 `meta.json` 保持原字节，快照内记录的业务文件 mode 供之后经确认的文件回滚使用，不会给状态原件开放 `0644` 等权限。此工具只恢复 Agent 状态，**不会连接 SSH、请求 LLM、发送通知或覆盖实际业务配置**。

恢复时应用以下策略，并在新状态的 `recovery.json` 记录摘要：

恢复目录在提取原始文件前即建立 `.ops-agent-recovered` 标记。在切换 `OPSX_STATE_DIR` 前，Agent 工具拒绝读取
该副本的原始内容；切换后普通报告/记忆恢复可读，通知凭据、快照原件和状态根目录递归读取仍受保护。

| 状态 | 恢复后的行为 |
| --- | --- |
| 所有审批，包括未过期、已消费和已过期戳 | 从有效 `approvals/` 移入私有 `recovery-quarantine-<随机值>/approvals/`；有效审批目录为空。原件保留供审计，任何变更都须重新批准，禁止把隔离戳搬回授权目录 |
| `executions/*.json` 中 `running` | 改为未核实 `unknown`，清空 PID 与 resolved，留下恢复说明；同目标同命令仍被阻止重放，须核实后使用现有 resolve 流程 |
| `runs/*.json` 中 `running` | 改为 `interrupted`，清空 PID、记录结束时间与说明，避免 PID 在新进程/新机器复用造成假运行 |
| 所有 schedule | `enabled=false`；上次 running 标记变 interrupted；清除旧机器心跳元数据。人工核对目标与计划后逐个 resume |
| 长目标 `goals.json` | active 改为 paused，保留已用轮次和上限；会话恢复后不会自动续跑，须人工检查后显式 resume |
| 通知配置与发件箱 | 配置 `enabled=false`，原 URL 和发件箱保留；副本不会自动补发旧生产告警。核对渠道和待发送记录后才重新启用 |
| `targets.json`、预算、用量账本和计数 | 保持原始字节，不重置额度或消费计数；跨机器迁移须人工核对 SSH 端点、密钥及时间区设置 |

如果执行记录、调度、通知、长目标配置等安全处理所需的 JSON 编码或结构无效，恢复会失败并保留原备份，避免悄悄跳过安全处理。
恢复元数据限 UTF-8、单文件不超过 32 MiB、整数 token 不超过 4096 字符，拒绝重复键及非 JSON 数值；运行记录还须通过实际 loader 校验。
已有 `recovery.json` 会与原审批一起隔离保留，新的 `recovery.json` 记录其位置，便于再次备份恢复后的追溯。
不要直接修改唯一备份；复制出私有工作副本、保留损坏证据，修复后重新验证与恢复。`verify` 通过表示归档完整，不保证其中每种应用状态都能解析，也不保证外部服务可用。

## 切换与演练验收

1. 在新状态中检查 snapshots、executions、runs、事件和报告是否齐全，核对 `recovery.json` 与备份 manifest。
2. 核实每条 unknown 变更的真实业务结果；未核实前不重放。审批重新申请，不延长或重算旧审批 TTL。
3. 核对 SSH 目标、业务文件路径、模型配置、预算和测试通知接收端。先做只读检查；需要业务恢复时继续使用经过确认的 snapshot/scenario 回滚流程与服务后验。
4. 确定要切换时，修改托管服务/cron/启动环境中的 `OPSX_STATE_DIR` 指向新目录。保留旧目录和备份，不要用符号链接绕过工具的路径保护。
5. 保持通知停用，逐个恢复 schedule，检查下一次运行时间与运行记录；核对旧发件箱后启用正确的通知配置。使用独立测试渠道验证异常/恢复消息。
6. 在 W8 验收记录中保存备份路径、manifest 的归档摘要、恢复目标、权限核对、被隔离审批数/路径、unknown 核实结果、探针/服务后验及验收人。真实环境恢复演练与 7 天连续观察须据实记录，不能用本地自动化回归代替。

该状态备份不主动收集状态目录外的 SSH 私钥、系统服务定义或 provider 环境变量；这些内容如果已存在于状态或快照中，仍按原件保留。它不能替代数据库备份、业务卷快照或完整部署回退方案，用途是恢复运维证据与可安全启动的 Agent 状态。

## 自动化回归

```sh
python3 -m unittest discover -s tests -p 'test_w8_state_backup.py' -v
```

覆盖原始快照/秘密 roundtrip、快照 mode、原始预算与目标清单、恢复权限、既有状态拒绝覆盖、审批隔离、实际 execution loader 与 unknown 防重放、运行记录中断、通知补发禁用、并发写入检测、归档损坏、路径穿越、硬/软链接和特殊文件拒绝。
