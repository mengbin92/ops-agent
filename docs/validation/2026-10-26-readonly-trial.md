# 2026-10-26 只读试运行准备记录

状态：**真实 SSH、Linux 后台托管、规则巡检和真实模型调用通过；完整 7 天验收未开始计时**。
通知渠道尚未配置，接收端送达与每日证据归档仍待验收。
下列时间均为北京时间；目标仅以别名记录，原始地址和输出留在私有状态目录。

## 运行范围

| 项目 | 实际配置 |
| --- | --- |
| 源码基线 | `18809815d1a37003b8d3ea8fd474a388f1ae3fc4`；配置前源码工作区干净 |
| 状态目录 | Linux 的 `~/.ops-agent-trial`；Mac 原目录保留且旧任务已停用 |
| 调度端 / 目标端 | 当前为 Linux 用户级 systemd 调度，同机检查；最初接入阶段由 Mac 经 SSH 检查 |
| SSH 目标 | 用户指定的已注册目标 `localServer`；免密连接与 Docker 访问已通过 |
| 任务 | `sch-335370`，每 10800 秒，整任务预算 900 秒；当前检查目标为服务器内置 `local` |
| 固定服务基线 | `docker.service` 和当时正在运行的 10 个 Docker 容器；不会每轮按现存容器重建清单 |
| 配置切换 | 2026 年 10 月 26 日 15:08:10，从 `local` 切到 `localServer` |
| 下一轮排期 | 2026 年 10 月 26 日 18:02:42；迁移保留原 ID、频率、预算和排期，旧历史另行完整归档 |

原任务 15:02 的本机记录仍为 `succeeded / 不完整`，不能作为远程巡检通过证据。
首次 SSH 接入时，目标配置仅补充所选目标的服务清单，其他目标配置未改；调度配置仅修改该任务的 `checks`。
修改前备份位于 `~/ops-agent-backups/trial-config-20261026_150810/`，目录权限 0700，文件权限 0600；
这是两份配置文件的备份，不是完整状态备份。更新后的 `targets.json` 权限为 0600。

## 实际验证

| 检查 | 结果与证据 |
| --- | --- |
| SSH 连通与执行用户 | 实际远程 `uname -s`、`id -un` 均退出 0 |
| Docker 访问与服务状态 | 实际容器清单读取成功；Docker systemd 服务为 active/running |
| 完整确定性巡检 | 15:08:31 执行 `opsagent inspect --target localServer --json`，退出 0；15 项正常，警告、异常、未知、错误均为 0 |
| 报告 | `~/.ops-agent-trial/reports/20261026-150831-inspect-631e41.md` |
| 私有 JSON 证据 | `~/.ops-agent-trial/validation/remote-baseline-20261026_150831.json`，权限 0600 |
| 配置完整性 | 保存后核对：除指定服务清单和任务检查目标外，其他目标、任务字段与调度 meta 均保持一致 |

本次手动规则巡检未调用模型，未创建新的 schedule run，也未发送通知；远程未执行写入、重启或故障注入。
这份证据只证明当时规则检查完整，不能替代业务接口健康、定时任务下一轮或连续 7 天验收。

## Linux 托管迁移与验证

2026 年 10 月 26 日完成迁移，操作方式见[常驻巡检说明](../ops/server-scheduler.md)。
不安装 Mac 防休眠服务；调度器在 Linux 上独立运行，不依赖 Mac 电源、终端或 SSH 会话存活。

| 验证项 | 实际结果 |
| --- | --- |
| Linux 环境 | Python 3.12.3；用户级 systemd 可用，`Linger=yes` |
| 源码部署 | 固定上述 commit 到 `~/.local/share/ops-agent/releases/<commit>/`，`current` 指向此版本 |
| 安装自检 | Linux 实机 `opsx selftest`，309/309 通过 |
| systemd 单元 | 校验退出 0；`opsagent-schedule.service` 为 enabled、active/running |
| 托管权限下的规则检查 | 15:22:38 在同一用户管理器与模型环境下检查服务器 `local`；15 项全部正常，退出 0 |
| 实际模型调用 | 使用私有环境文件运行真实模型请求，退出 0，回复“托管环境验证通过。” |
| 自动拉起 | 15:23:26 验证完成；无任务执行时终止调度进程，10 秒重启配置自动恢复，`NRestarts=1` |
| 单实例与排期 | 新进程持有调度锁，只有一个调度进程；任务字段和下一轮时间未因重启改变 |
| SSH 会话独立性 | 启动会话退出后，重新连接仍为 active，心跳继续推进 |
| 凭据与依赖 | 环境文件权限 0600，service 内没有模型凭据；模型端点未直接指向 Mac 当前地址；未复制 SSH 私钥 |

迁移前确认 Mac 已无调度进程及 running 记录，完整停写备份及校验均通过：
`~/ops-agent-backups/trial-before-server-migration-20261026_152111/`。
该备份包含原始历史及必要凭据，权限私有；服务器保存同名副本，逐文件及归档校验通过。
原始归档 SHA-256：`e6e71c8dc9e28916faea362324357c9cc537c72d78eda3d67e184da9485cd2f3`。

Linux 活跃状态只保留该机器的 `_local` 检查清单，不复制其他 SSH 目标、旧通知状态或 Mac 运行 PID。
旧运行记录、会话、报告、信号和事件保留在上述完整归档，避免旧 Mac 绝对路径被误作 Linux 活跃证据。
调用用量记录随迁移保留，Linux 的 `meta.last_scan` 由新进程实际扫描生成；Mac 旧任务设为停用以防双跑。

服务器私有证据：`~/.ops-agent-trial/validation/` 下的 `systemd-rules-check.json`、
`systemd-llm-check.log`、`systemd-supervision-check.json` 和 `server-migration.json`；
Mac 保存核验索引 `~/.ops-agent-trial/validation/server-migration-verification.json`。
未实际重启服务器，也未故意断开模型网络；本次没有重启或修改任何被检查的业务服务或容器。

## 后续待执行

- [ ] 核对切换后的首轮定时执行，保存对应 run、session、signal 和完整报告；原本机运行时间不计入远程观察。
- [ ] 配置专用真实通知渠道，确认异常、去重、升级、恢复、失败补发以及 LLM 不可用时的规则异常送达。
- [x] 将调度迁到 Linux 用户级 systemd，验证登出常驻配置、实际模型环境、单实例及进程退出后自动恢复，解除 Mac 休眠依赖。
- [ ] 落实每日私有证据归档，避免报告轮转造成缺口。
- [ ] 上述前置完成后记录实际观察起点、版本和目标基线，按[逐日表](../releases/2026-10-22-readonly-trial.md)连续核对满 168 小时。

任务范围为现有机器的只读检查；可中断测试服务、允许写入目录和恢复演练授权仍未确认，不开展故障注入或变更验收。
