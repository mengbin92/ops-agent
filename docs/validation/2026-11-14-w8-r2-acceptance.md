# 2026-11-14 W8 R2 七天连续运行收口验收记录

状态：**R2 七天连续运行门禁通过（仅对 `v0.1.0-rc.3`）**。
本文记录 2026-11-07 09:38:18 → 2026-11-14 09:38:18 的最终对账，
按 [R2 重启记录 §10](2026-11-07-w8-observation-restart.md) 的六项判定标准逐项核对。
**这不等于正式版发布门禁通过**：S3 三项与 S4 发布门禁代码化仍未完成。

## 1. 验收对象

| 项目 | 内容 |
| --- | --- |
| 观察期 | 2026-11-07 09:38:18 → 2026-11-14 09:38:18（北京时间，168.0 小时） |
| 运行版本 | `v0.1.0-rc.3`（固定部署，`current` 指向全程未变） |
| 任务 | `sch-35e3e0`，每 10800s（3h），预算 900s，local 三组规则检查 |
| 状态目录 | `~/.ops-agent-trial-r2/`（0700，与 R1 隔离） |
| 预期时点 | 服务器私有 `~/.ops-agent-trial-r2/validation/deploy-20261107.json`（0600，56 个时点） |
| 逐日表 | [7 天试运行逐日表](../releases/2026-10-22-readonly-trial.md) R2 段（已按实测回填） |
| 核对时间 | 2026-11-14 10:31 首次、2026-11-15 14:51 复核，数据一致 |

## 2. 判定结论

| 判定项 | 通过线 | 实测 | 判定 |
| --- | --- | --- | --- |
| 排期 | 56 时点全命中，0 未解释漏跑 | 实跑 56 / 漏跑 0 | ✅ |
| 结果 | 56/56 `succeeded` +「正常」，0 failed / timeout / interrupted / missed | 56/56 全满足 | ✅ |
| 漂移 | 固定网格锚定，≈ 0 | 最大 0.00 min、平均 -0.01 min | ✅ |
| 版本 | rc.3 全程未变；`current` / `targets.json` 未改；`NRestarts=0` | `targets.json` mtime 停在 11-06 08:59（观察期内未改），`NRestarts=0` | ✅ |
| 证据 | run→session→report 齐全，未轮转 | 56/56 齐全；reports 62（< 100） | ✅ |
| 异常说明 | 无未解释项 | 零异常，`notify_status` 全 `none` | ✅ |

**结论**：六项全部满足，R2 七天连续运行门禁判定通过。

## 3. 运行数据

| 指标 | 值 |
| --- | --- |
| 预期 / 实际槽位 | 56 / 56 |
| 结果 | 56 `succeeded`、56「正常」；0 failed / timeout / interrupted / missed / 漏跑 |
| 首 / 末次 | 11-07 12:38:18 / 11-14 09:38:18 |
| 最大漂移 | 0.00 min（逐次落在 `:38:18`；R1 曾累积 +40.1min） |
| 逐日 | 7 个日次各 8/8 全成功 |
| 证据体量 | runs 56、sessions 56、signals 56、reports 62（含启动前手动检查） |
| 通知 / 事件 | 56 次 `notify_status=none`；发件箱 1 条（11-09 `notify test`）；events 2 行 |
| 守护进程 | 观察期内 `opsagent-schedule.service` active、`NRestarts=0`；收口后已于 11-15 停用 |

首次抽样显示 `session 存在=False` 系按 `.json` 后缀匹配所致，实际会话文件为 `.jsonl`；
按真实格式复核 56/56 齐全，该误判与纠正已如实记录，不作为证据缺口。

## 4. 逐日回填

7 行逐日数据已于 2026-11-15 按 `runs/` 实测回填到
[逐日表 R2 段](../releases/2026-10-22-readonly-trial.md)：每日 8/8 `succeeded` /「正常」、
0 failed / 0 中断错过、通知 0/0、无误报漏报。表格不再保留空白，也未填写任何未观测项。

## 5. 边界与未满足项（诚实记录）

1. **版本范围**：本结论**只对 `v0.1.0-rc.3` 有效**。`v0.1.0-rc.4` 已于 11-09 发布，
   含通知状态机 / 调度恢复 / 探针重大修复，其[发布说明](../releases/release-v0.1.0-rc.4.md)
   要求「实际部署后重新计满七天观察」；当前未部署 rc.4，不能据本记录宣称 rc.4 通过。
   维护者 11-11 裁决走 rc.3 收口路线，不重计。
2. **观察期内通知送达仍无证据**：R2 期内零异常，56 次 `notify_status=none`，
   去重 / 升级 / 恢复 / 失败补发链路未被触发。[11-09 独立目录演练](2026-11-09-notify-drill.md)
   （7 场景通过、收件端确认 5 封实收）不构成本观察期内的送达证据。
3. **S3 三项未执行**：真实 SSH 恢复、断线 `unknown` 防重放、服务后验 `verify_failed`。
4. **S4 发布门禁代码化未完成**：`release.yml` 尚无外部验收证据阻断。
5. **观察期外运行与任务移除**：任务 `sch-35e3e0` 在观察期外额外产生 9 次只读 run
   （runs 65 / reports 71，不计入 56 个验收槽位），已于 2026-11-15 14:54 按维护者要求移除。
   `schedules.json` 与 `notify-state.json` 备份于服务器私有目录
   `~/.ops-agent-archive/remove-r2-task-20261115-145444/`，证据全部保留，本收口结论不受影响。
   移除后 `~/.ops-agent-trial-r2/schedules.json` 为 0 个任务；用户级调度服务
   `opsagent-schedule.service` 已于 2026-11-15 15:04 以 `disable --now` 停用
   （inactive / disabled、无残留 daemon 进程），可随时 `enable --now` 恢复。
   停用不影响任何已归档证据，也不改变本收口结论。
6. 本记录不改变 W8 整体与正式版发布判定。

## 6. 证据路径

服务器私有 `~/.ops-agent-trial-r2/`：`runs/`、`reports/`、`sessions/`（`.jsonl`）、
`signals/`、`notifications/`、`events.jsonl`、`schedules.json`、
`validation/deploy-20261107.json`。原始凭据、真实地址与收件人不入库。
仓库侧：[R2 重启记录](2026-11-07-w8-observation-restart.md) §10、
[逐日表](../releases/2026-10-22-readonly-trial.md) R2 段、本文。
