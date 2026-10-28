# 2026-10-28 7 天观察部署记录（obsServer）

状态：**只读托管已启动并开始计时（2026-10-28 16:57 北京时间）；真实模型调用已验证；
通知渠道未配置；首轮定时执行（19:57）待核对**。
目标以别名 `obsServer` 记录，原始地址与凭据不写进仓库；证据索引见服务器私有状态目录。

## 运行范围

| 项目 | 实际配置 |
| --- | --- |
| 源码基线 | `cc941d78b33d77c71a7a18a5f4ab515b8f2d691f`（develop，git archive 固定，工作树干净） |
| 状态目录 | `~/.ops-agent-trial/`（0700），与旧试用目录互不复用 |
| 调度端 / 目标端 | 同一台 Linux 主机，检查内置 `local`；无需在服务器保存 SSH 私钥 |
| 任务 | `sch-200a62`，每 10800 秒（3 小时），整任务预算 900 秒，规则检查 local 三组全跑 |
| 模型环境 | `~/.ops-agent-trial/.env`（0600；键 OPSAGENT_API_KEY / BASE_URL / MODEL），由 service 的 `EnvironmentFile=-` 加载 |
| 固定服务基线 | `docker.service` + 19 个容器（初始快照 `validation/targets-initial-20261028.json`）；不按每轮现存容器重建 |
| 未纳入基线 | `ss-rust-paynex`、`ss-rust-phone`（代理类，避免重启噪声造成误报；需要监控时改 targets.json 即可） |

## 实际验证（2026-10-28，均为北京时间）

| 检查 | 结果与证据 |
| --- | --- |
| 实机自检 | `opsx selftest` **321/321**（两次：LC_ALL=C.UTF-8 与干净环境各一次） |
| 编码边界 | 服务器未安装 `zh_CN.UTF-8`；SSH 会话转发该 locale 会导致 1 个 repl history 用例失败（环境编码，非代码缺陷）。service 已显式 `LANG=C.UTF-8` |
| 完整规则巡检 | `inspect --target local --json`：24/24 ok，warn/critical/unknown/error 全 0，complete=true，退出 0 |
| 真实模型调用 | 同一 .env 下 `opsagent -p` 真实请求退出 0，模型回复"正常。" |
| 日志粗筛（logscan） | journald `docker.service` 实扫 6821 行/149 组/1.1s 无超时；`docker logs mysql` 正常；存量噪声：dockerd 日志已有 warn/error（非观察期引入） |
| systemd 托管 | root 用户级 `opsagent-schedule.service`，enabled + active，`Linger=yes`，`Restart=always` |
| 自动拉起 | kill 主进程后 `NRestarts=1` 恢复，任务频率/预算/下次执行时间不变 |
| 证据索引 | 服务器 `~/.ops-agent-trial/validation/deploy-20261028.json`（0600） |

## 观察期参数

- **开始**：2026-10-28 16:57（托管 active、配置完整）；**预期结束**：2026-11-04 16:57，连续满 168 小时。
- 每 3 小时一次，**预期 56 次执行**（首次 19:57）；报告保留 100 份 / 运行记录保留 200 份，56 次不触发轮转，7 天内无缺档风险。
- 每日核对回填[逐日记录表](../releases/2026-10-22-readonly-trial.md)；只写实际看到的数据。

## 边界与未完成项（诚实记录）

- **通知渠道未配置**：S2 的"异常/恢复通知送达"验收项保持未完成，补配 webhook 后单独补验。
- **首轮定时执行未核对**：19:57 首轮的 run/session/report/signal 待保存核对后，才能算 S2"首轮定时执行"通过。
- **严格只读**：该服务器运行 mysql/billing 等真实负载；故障注入、受控变更、恢复演练（S3）未获授权，不在本轮范围。
- 服务器断电/网络中断造成的漏跑按运行记录如实核对；未实际重启服务器验收启动恢复。
