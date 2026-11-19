# ops-agent 全仓代码审查 · 2026-11-19

以 `develop` 的 `8dfa085` 为审查基线。按个人运维场景审查，允许本机 CLI、调度进程与 A2A 请求并发使用同一份状态目录。走查了执行守门、MCP/RPC/A2A、模型连接与会话、巡检与调度、通知、场景、工作流、目标状态、日志与预算、离线恢复及发布流程。

## 修复清单

| 编号 | 优先级 | 位置 | 问题与修复 |
| --- | --- | --- | --- |
| 01 | P0 | [core.py](../../core.py)、[checks.py](../../agent/checks.py)、[scenario.py](../../scenario.py) | 巡检探针可以凭通用审批戳放行，其他只读入口有两次判定不一致的窗口。bash、巡检、场景探针及快照命令共用一次判定，强制 R0。 |
| 02 | P0 | [core.py](../../core.py)、[remote.py](../../remote.py)、[opsx](../../opsx) | Kubernetes 预演的原始文本与实际参数可能不一致，diff 也可能继承外部执行器。按实际参数判定预演，本机和注册 SSH 执行隔离外部 diff 配置。 |
| 03 | P1 | [core.py](../../core.py)、[tools.py](../../agent/tools.py)、[scenario.py](../../scenario.py) | 快照声明只含命令时未校验，审批戳替换时可能消耗不同计划，场景审批也可能漏写计划。场景固定用户看到的定义及环境，展示和审批戳复用同一快照计划，消费审批时在锁内复核。 |
| 04 | P1 | [core.py](../../core.py)、[remote.py](../../remote.py)、[execution.py](../../execution.py) | 回滚预览、恢复目标与执行记录在部分分支不一致。拒绝敏感或非普通文件目的地，复核远程真实路径，保留仍在执行的记录，并在缺失执行阶段证据时停止变更。同步调用按线程归属关联记录。 |
| 05 | P1 | [a2a_server.py](../../agent/a2a_server.py)、[a2a_client.py](../../agent/a2a_client.py)、[requirements-a2a.txt](../../requirements-a2a.txt) | 错误格式的 Bearer 可导致鉴权异常，客户端凭据缺少发送范围约束。使用字节比较及精确公开路径，限制凭据发送到调用 URL 同源，支持显式发布地址，并升级 Starlette。 |
| 06 | P1 | [a2a_executor.py](../../agent/a2a_executor.py)、[loop.py](../../agent/loop.py) | 取消后后台线程还可执行后续工具，等待会话锁的任务长期占用槽位。加入协作取消，阻止继续调模型或执行工具，并以后台线程的实际退出时间回收槽位。会话缓存保留使用中与排队的会话。 |
| 07 | P1 | [llm.py](../../agent/llm.py)、[loop.py](../../agent/loop.py)、[repl.py](../../agent/repl.py)、[opsagent](../../opsagent)、[goal.py](../../agent/goal.py) | 模型和工具输出、分片流式文本及目标状态存在脱敏和终端展示缺口。助手消息拼接、校验和脱敏后才向前端交付，工具事件脱敏，终端控制字符可见化，目标读写和续跑走脱敏。 |
| 08 | P1 | [notify.py](../../agent/notify.py) | 合并恢复邮件可能把某项告警的恢复信息发给未收到原告警的收件人。按单项原告警的实际送达范围构造邮件，并记录实际投递结果。 |
| 09 | P2 | [schedule.py](../../agent/schedule.py) | 推进排期和创建运行记录之间的中断可导致漏跑无记录，查询对账可误刷新心跳，异常时间值可中断调度。领取时先落运行记录，区分对账与扫描，统一校验时间与间隔。 |
| 10 | P2 | [notify.py](../../agent/notify.py)、[schedule.py](../../agent/schedule.py) | 分组巡检可能无法凭同项健康证据恢复告警，配置异常可能吞掉待恢复事件，运行历史显示过期的通知状态。使用精确探针状态，保留待恢复信息，展示发件箱最新结果，并收紧邮箱校验。 |
| 11 | P2 | [core.py](../../core.py)、[remote.py](../../remote.py)、[logscan.py](../../agent/logscan.py)、[llm.py](../../agent/llm.py) | 日志、探针及模型响应可能无上限读入，聚焦正则可超出预算，HTTP 短读可被误认作完整响应。为采集、模型响应和正则匹配设边界，保留 stdout 与 stderr 的用途，显式报告超限及短读。 |
| 12 | P2 | [scenario.py](../../scenario.py)、[workflow.py](../../agent/workflow.py) | 场景参数可能被二次替换，已完成回滚可因变更后的后验定义报失败，工作流目录可能展示无效覆盖项。按原模板做一次替换，分开报告回滚与后验证据，目录复用加载校验。 |
| 13 | P2 | [a2a_executor.py](../../agent/a2a_executor.py)、[a2a_client.py](../../agent/a2a_client.py)、[a2a_cli.py](../../agent/a2a_cli.py)、[mcp_server.py](../../mcp_server.py) | 修复 A2A 立即响应被误判、多次等待输入只处理一次、轮次耗尽标成功的问题。MCP 在异常 JSON 后继续处理下一条消息。 |
| 14 | P3 | [core.py](../../core.py)、[events.py](../../agent/events.py)、[usage.py](../../usage.py)、[opsx](../../opsx) | JSONL 半行会吞掉下一条记录，旧日志中的 Unicode 分隔符也会被误拆。统一私有追加，读侧按物理换行切分，最近用量按有效记录计数。 |
| 15 | P3 | [logscan.py](../../agent/logscan.py)、[selftest.py](../../selftest.py)、[release.yml](../../.github/workflows/release.yml) | 修复时间戳覆盖率的分母不一致和时间桶粒度，退出时清理自检临时目录，SemVer 构建元数据中的连字符不再使正式版本被标为预发布。 |
| 16 | OCR 高优 | [logscan.py](../../agent/logscan.py) | Nginx 时间戳此前只捕获到小时，造成跨分钟的异常归入同一桶。各来源现在单独捕获分钟再分桶，新增 Nginx 双桶回归。 |

## 行为与验证范围

- 模型侧继续使用 SSE。REPL 和流式 RPC 在一条助手消息校验、脱敏完毕后交付，工具和确认事件仍随流程发送。
- 只读命令、巡检、快照命令与日志采集的 stdout/stderr 合计上限为 32 MiB。超限会明确失败。
- A2A 通过可选依赖运行，需要 Python 3.11 或更高版本。本机验证分别覆盖无可选依赖和已安装 A2A 依赖的环境。
- A2A 通配监听必须提供 `--public-url`，该地址用于 AgentCard。此参数也可指定反向代理对外发布的 HTTPS 地址和路径前缀。
- 本次验证使用本机回环、临时状态和模拟服务。真实 SSH、模型与通知渠道、GitHub Actions 及发布后的持续观察需要各自的环境证据。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| macOS · Python 3.9.6 完整 unittest | 423 项中 409 项通过，14 项可选 A2A 用例跳过 |
| macOS · Python 3.12.13 完整 unittest | 423 项全部通过，含 A2A 端到端和 W8 本机回环演练 |
| `opsx selftest` | Python 3.9 与 3.12 各 322/322 通过 |
| A2A 依赖一致性 | 36 个已安装包的依赖检查通过 |
| 发布版本标记 | 正式版、带构建元数据正式版、带构建元数据预发布三个本地用例通过 |
| OpenCodeReview | 高强度审查与定向补审共覆盖 30/30 个入选文件。首轮唯一高优发现已修复，修正后的日志采集文件通过复审。P0/P1 无未处理项。 |
| Git 差异检查 | `git diff --check` 通过 |
