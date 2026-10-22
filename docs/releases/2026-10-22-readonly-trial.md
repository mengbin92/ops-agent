# 只读巡检试用版（2026-10-22）与 7 天连续试运行

本文件是 W4 的发布说明，同时是 W5「只读版通过 7 天连续试运行」的记录表。
状态：**已交付、试运行未开始**（2026-10-22 交付；试运行需要接入真实 webhook 与 LLM，
在测试主机上连续跑 7 天，当前仓库内没有真实渠道与主机，故不预先打勾）。

## 1. 版本内容

包含 W1–W4 的全部能力，定位是**只读**：能发现异常、能通知到人、能追溯证据，但不会改任何东西。

| 能力 | 入口 | 说明 |
| --- | --- | --- |
| 确定性巡检 | `opsagent inspect` | 连通性 / 资源（磁盘、内存、负载）/ 服务（systemd、docker）三组固定探针；`ok`/`warn`/`critical`/`unknown`/`error` 五态；结论"正常"要求无异常、三组全跑、无未采集项 |
| 定时任务 | `opsagent schedule` | `add/list/remove/pause/resume/runs/run`；每次执行有运行记录（run/session/pid/状态/结论/耗时/报告/通知）；整任务时间预算；只读有限重试 |
| 任务级规则检查 | `schedule add --checks` | 在调用模型之前跑规则检查，模型只解读；LLM 不可用时规则异常仍会上报 |
| 单渠道通知 | `opsagent notify` | webhook（generic / slack / feishu / dingtalk）；去重、升级、提醒、恢复；全静默与时段静默；失败可查可重试 |
| 事件时间线 | `opsagent events` | 异常/恢复/通知/运行状态变化，只记状态变化；跨轮转可查 |
| 只读安全边界 | `core.check` + `read` | R0 白名单、写操作需审批、敏感文件（含 `notify.json`、整个 `~/.ops-agent`）不读进模型 |

交付自检：`./opsx selftest` → **206/206 passed**；同日[路线图对照复审](../reviews/2026-10-22-roadmap-implementation-review.md)
修复后为 **241/241 passed**；W5 交付后为 **261/261 passed**
（[W5 复审记录](../reviews/2026-10-22-week5-change-safety-review.md)）。

W1–W5 期间连续发生"巡检摘要/调度结论/通知状态机/变更执行记录"等重大修复，按上面的口径，
**观察期须从 W5 交付日 2026-10-22 重计**，即最早 2026-10-29 才可判定；此前任何"通过"都是无效的。

## 2. 试用版怎么装

```bash
# 1) 自检（不需要网络）
./opsx selftest

# 2) 配通知渠道（URL 的 path 就是凭据，只写进 0600 的 notify.json）
./opsagent notify setup --url https://hooks.example.com/services/... \
    --format slack --quiet-hours 23:00-07:00
./opsagent notify test          # 手机/群里应收到"测试通知"

# 3) 登记每日巡检（先只查本机，确认稳定后再加 SSH 目标）
./opsagent schedule add --daily 09:00 --checks all --timeout 900 \
    "解读规则检查结果，列出需要人工确认的点与建议；不要执行任何变更"

# 4) 起调度（二选一）
./opsagent schedule run                          # 长驻守护进程
# 或 cron：*/5 * * * * cd <repo> && ./opsagent schedule run --once
```

环境变量：`OPSAGENT_API_KEY` / `OPSAGENT_BASE_URL`（LLM）。内网 webhook 需要设
`no_proxy`。状态全部在 `~/.ops-agent/`：`runs/`、`reports/`、`signals/`、
`notifications/`、`events.jsonl`、`schedules.json`、`notify.json`。

## 3. 试运行的目标与判定

| 目标 | 判定方式 | 通过线 |
| --- | --- | --- |
| 巡检确实执行 | `schedule runs` 每天应有与排期数量一致的 `succeeded`（或明确的 `failed/timeout/interrupted/missed`） | 7 天内无"未解释的漏跑" |
| 异常能送达 | 人为制造持续异常（如临时把磁盘阈值降到当前使用率以下），观察是否收到且只收到一条 | 首次告警 ≤ 1 个排期周期；同异常 6 小时内不重复 |
| 恢复能送达 | 撤销人为异常，等待一个排期周期 | 收到一条"恢复"，不重复 |
| 失败可查可重试 | 临时把 webhook 改成不可达地址，制造告警 | `notify list` 有 failed + 原因；改回后 `notify retry` 或下一轮 daemon 自动补发成功 |
| LLM 不可用仍上报 | 用错误的 `OPSAGENT_API_KEY` 跑一轮 | 运行记录 `failed`，但通知里有规则异常与探针命令 |
| 证据可追溯 | 从任意一条通知回查 | 能按 `run_id` 找到运行记录、按 `session_id` 找到会话、打开报告看到完整输出 |

时间口径：**连续 7 天**。W5 交付日 2026-10-22 为重计起点，最早可判定日为 **2026-10-29**。期间如再发生"重大修复"（改了巡检探针、通知状态机、调度推进逻辑、变更执行记录），观察期从修复当天**重计 7 天**。

当前状态（2026-10-22）：**未开始计时**——需要接入真实 webhook 与真实目标机后才能产生有意义的数据，仓库内没有真实渠道，因此下表的所有日期保持空白，不预先打勾。

## 4. 每日核对（试运行时填写）

| 日期 | 排期数 | succeeded | failed/timeout | 中断/错过 | 通知发送/失败 | 发现的误报/漏报 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10-23 |  |  |  |  |  |  | ⬜ |
| 10-24 |  |  |  |  |  |  | ⬜ |
| 10-25 |  |  |  |  |  |  | ⬜ |
| 10-26 |  |  |  |  |  |  | ⬜ |
| 10-27 |  |  |  |  |  |  | ⬜ |
| 10-28 |  |  |  |  |  |  | ⬜ |
| 10-29 |  |  |  |  |  |  | ⬜ |
| 10-30（验收） |  |  |  |  |  |  | ⬜ |

每日 5 分钟核对命令：

```bash
./opsagent schedule runs --tail 40      # 跑没跑、什么结论
./opsagent notify status                # 待重试几条、最近是否失败
./opsagent events --tail 30             # 异常/恢复/通知的状态变化
./opsagent inspect --target all --json  # 需要对照当前的规则结论时
```

记录规则：**只写实际看到的数据**；没核对就留空，不要为了表格好看打勾。误报/漏报要写清
"哪台机器、哪个检查项、当时的证据命令与输出"，否则无法改进探针阈值。

## 5. 停止与回滚

```bash
./opsagent schedule run        # Ctrl-C 停止守护进程
./opsagent schedule pause <id> # 暂停某个任务
./opsagent notify disable      # 停用通知（巡检与事件仍照常，只是不发送）
./opsagent notify unmute       # 解除静默
```

本版本不改任何系统状态，因此没有"回滚变更"的动作；把 `schedules.json` 里的任务删除、
或停掉守护进程即回到完全静止状态。运行记录、报告、事件可按目录自行归档/删除。

## 6. 已知边界（验收时要接受的前提）

- 探针只覆盖连通性、磁盘/内存/负载、systemd/Docker 服务；不覆盖 HTTP/TLS 证书、备份新鲜度、
  数据库健康。需要这些请另接外部监控。
- `unknown` 只表示"没采到"，不等于"坏了"；报告里有证据命令，需要人复核。
- 单机通知在宿主彻底宕机时发不出消息；这类保障需要把心跳检查放到另一台主机或外部服务。
- 通知发送持锁（默认 8s 超时），慢 webhook 会推迟同机的一次并发 `notify retry/status`。
- 恢复是推断：检查项被删除或目标被移出清单时也会按"恢复"处理，原因会留在事件里。
- 巡检期间若有人改写 `targets.json`，同一次报告的不同探针可能打到不同主机（报告按开始时的
  目标快照展示）。
- 结论解析仍是文本启发式：模型不按"单独一行 `结论：X`"输出时记「未判定」（`--once` 退出码 3），
  不会猜成正常。
