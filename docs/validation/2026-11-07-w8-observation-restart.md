# 2026-11-07 W8 七天观察重启记录（R2）

状态：**R2 连续 7 天只读观察已启动，观察进行中**（2026-11-07 09:38:18 北京时间起，
预期 2026-11-14 09:38:18 满 168 小时）。**首轮 12:38:18 尚未执行，逐日数据全部空白、未预先打勾。**
本轮只做只读巡检，不执行任何受控变更；W8 整体仍未通过（S3 三项外部证据仍缺）。

## 1. 重启依据

R1（2026-10-28 16:57 – 11-04 17:37，56/56 成功、0 漏跑）的运行版本是 `cc941d78` / `v0.1.0-rc.1`，
含「每轮按领取时刻 + 间隔推进」的滑动排期漂移。`v0.1.0-rc.2` 将其改为固定槽位网格锚定，
属于运行行为重大修复。依据：

- [下一阶段规划](../plans/2026-10-25-next-stage.md) §2：「影响运行行为的重大修复后重新计时」
  「探针、通知状态机、调度推进或执行记录重大修复后重计 7 天」；
- [R1 验收记录](2026-11-05-w8-acceptance.md) §5.3：「按规则修正后要重新计时 7 天」；
- [只读试运行记录](../releases/2026-10-22-readonly-trial.md) §3：「观察期须在真实环境启动修复版本后连续计满 7 天」。

2026-11-07 由维护者裁决：**不采纳 R1 结论作为当前门禁证据，在修复版本上重计**。
R1 记录与证据原样保留、归档为历史轮次，不删除、不改写其观测数据。

## 2. 切换前现场核对（只读）

| 项目 | 实际值 |
| --- | --- |
| 观察节点 | 别名 `obsServer`（真实地址与凭据不入库，见[服务部署说明](../ops/server-scheduler.md)） |
| 已部署版本 | `~/.local/share/ops-agent/current` → `releases/v0.1.0-rc.3`，`VERSION=0.1.0-rc.3` |
| 版本对应 tag | `8d1bf02258c9f638cfff2b5848f9b6f10d9e3efc`；11-06 部署时已按发布记录做本机 SHA256SUMS 校验，**本次未重新校验制品摘要**（发布目录内无 SHA256SUMS 文件） |
| 旧任务 | `sch-200a62`（3h 观察）与 `sch-8a8d81`（120s 渠道验收）**均已暂停**；守护进程 active 但无到期任务 |
| 旧证据体量 | `runs/` 81、`reports/` **87**、`sessions/` 82、`signals/` 79 |
| 通知配置 | `notify.json` 存在（0600），SMTP 渠道已启用；`.env`、`targets.json` 均为 0600 |
| 磁盘 | 剩余 27G |

> **2026-11-07 10:09 补记**：上表两条旧任务已按维护者要求从 R1 `schedules.json` 移除（仅任务条目）。
> 原文件备份于服务器 `~/.ops-agent-archive/remove-tasks-20261107-100923/`（SHA-256 校验一致），
> `runs/` 81、`reports/` 87、`sessions/` 82、`signals/` 79、`notifications/` 9、`validation/` 19 份均保持不变。
> 上表记录的是**切换前**的现场状态，保留不改；移除动作与复核见 §8。

**为什么要独立状态目录**：默认报告保留 100 份、运行记录 200 份、会话 300 份。
R1 已有 87 份报告，若在原目录再跑 56 次 → 143 份，会触发轮转并冲掉本轮首日证据，
违反 [S1 留存要求](../plans/2026-10-25-next-stage.md)「验收首日记录必须能留到末日复核」。
因此新建 `~/.ops-agent-trial-r2/`，R1 目录冻结只读。

## 3. 实际执行步骤

1. 新建 `~/.ops-agent-trial-r2/`（**0700**）及 `runs/reports/sessions/signals/notifications/validation` 子目录；
   从 R1 复制 `.env`、`notify.json`、`targets.json`，全部保持 **0600**（复制过程不读取、不输出其内容）。
2. 备份 unit 为 `~/.config/systemd/user/opsagent-schedule.service.bak-20261107-r2`；
   仅改写 `Environment=OPSX_STATE_DIR` 与 `EnvironmentFile` 的路径指向新目录，
   其它行（含 3 个 `Environment=` 中的其余配置）不动。
3. `systemctl --user daemon-reload && systemctl --user restart opsagent-schedule.service`；
   切换后 `active` / `enabled`，且新目录由守护进程自动创建 `schedules.json`（`items=0`），
   证明服务确实指向 R2 状态目录；R1 `reports/` 仍为 87 份。
4. 在新状态目录创建任务：

   ```bash
   OPSX_STATE_DIR="$HOME/.ops-agent-trial-r2" \
     python3 "$HOME/.local/share/ops-agent/current/opsagent" schedule add \
     --every 10800 --timeout 900 --checks local \
     "对服务器做只读巡检：先运行确定性规则检查（本机 local，connectivity/resources/services 三组全跑），再解读结论，列出需要人工确认的点与建议。全程只读，不执行任何变更；异常时如实报告未采集项，不要把未采集说成健康。"
   ```

   返回 `sch-35e3e0`，下次 11-07 12:38。
5. 生成 56 个预期时点索引 `~/.ops-agent-trial-r2/validation/deploy-20261107.json`（0600，原始证据不入库）。

## 4. 观察期参数

| 项目 | 值 |
| --- | --- |
| 轮次 | R2（重计，R1 归档） |
| 起点 | **2026-11-07 09:38:18**（北京时间，任务创建、托管已 active 且配置完整） |
| 首轮执行 | 2026-11-07 12:38:18 |
| 预期结束 | **2026-11-14 09:38:18**（连续 168.0 小时） |
| 任务 | `sch-35e3e0`，每 10800 秒（3h），整任务预算 900s |
| 预期执行 | 56 次（`created + k×3h`，k=1..56），固定网格锚定，不再滑动 |
| 检查范围 | 内置 `local` 的 connectivity / resources / services 三组，服务基线沿用 R1 固定清单 |
| 运行版本 | `v0.1.0-rc.3`（`git archive` 固定部署，`current` 符号链接；**观察期内冻结指向与 targets.json**） |
| 状态目录 | `~/.ops-agent-trial-r2/`（0700），R1 目录冻结 |
| 通知渠道 | 沿用已验证的 SMTP 邮箱渠道（配置复制自 R1，0600）；R2 期间送达按逐日表核对 |
| 证据留存 | 启动前手动 `inspect` 另产生 4 份报告（不计入 56 次观察槽位）；合计约 60 份报告 < 100、56 份运行记录 < 200、56 份会话 < 300，本轮不触发轮转 |

## 5. 启动前门禁验证（2026-11-07 09:38–09:42，北京时间）

| 检查 | 实测结果 |
| --- | --- |
| 实机自检 | `./opsx selftest` **322/322 passed**（`LANG=C.UTF-8`，服务器无本机沙箱干扰） |
| 本机基线复核 | 维护者在普通终端复跑：`./opsx selftest` **322/322 passed**、`python3 -m unittest discover -s tests` **319 tests OK（skipped=14）**；确认此前 WorkBuddy 沙箱内 320/322 + 13 errors 为 shim 对临时目录 `mkdir(exist_ok=True)` 的误判，**非代码回归** |
| 完整规则巡检 | `inspect --target local --json`：`verdict=正常`、`complete=true`、`anomalies=0`；三组全部请求且 `groups_skipped=[]`、`targets_skipped=[]` |
| 通知渠道 | `notify status`：email 渠道启用、发件箱空、待重试 0 条（**只读确认，未主动发信**） |
| 调度心跳 | `schedule status`：心跳在运行，**1 启用 / 1 总计**（唯一调度） |
| 权限 | 状态目录 0700；`.env` / `notify.json` / `targets.json` / `deploy-20261107.json` 均 0600 |

## 6. 证据路径（服务器私有，不入库）

| 证据 | 路径 |
| --- | --- |
| 预期时点清单 | `~/.ops-agent-trial-r2/validation/deploy-20261107.json`（56 个北京时间时点 + 起止 + 任务 id） |
| 逐次运行 | `~/.ops-agent-trial-r2/runs/` |
| 完整报告 | `~/.ops-agent-trial-r2/reports/` |
| 会话 / 信号 | `~/.ops-agent-trial-r2/sessions/`、`signals/` |
| 通知发件箱 | `~/.ops-agent-trial-r2/notifications/`、`events.jsonl` |
| 调度状态 | `~/.ops-agent-trial-r2/schedules.json` |
| R1 归档 | `~/.ops-agent-trial/`（冻结，只读回看） |
| 公开汇总 | 本文件、[R2 逐日表](../releases/2026-10-22-readonly-trial.md)、[R1 验收记录](2026-11-05-w8-acceptance.md) |

每日核对命令（在服务器上执行，注意状态目录）：

```bash
export OPSX_STATE_DIR="$HOME/.ops-agent-trial-r2"
CLI="$HOME/.local/share/ops-agent/current/opsagent"
python3 "$CLI" schedule runs --tail 40
python3 "$CLI" schedule status
python3 "$CLI" notify status
python3 "$CLI" events --tail 30
```

逐日结果回填 [R2 逐日表](../releases/2026-10-22-readonly-trial.md)；**只写实际看到的数据，没核对就留空**。

## 7. 未完成项与边界（诚实记录）

1. **首轮 12:38 尚未执行**，56 次 `succeeded`、0 漏跑、最大漂移等**均未在 R2 得到验证**；
   逐日表 7 行全部空白。本记录只证明「修复版本上的观察已按规则启动」。
2. **S3 三项外部门禁仍未执行**：真实 SSH 恢复、断线 `unknown` 防重放、
   「命令 exit=0 但服务不健康 → `verify_failed`」服务后验；见[实机清单](../ops/w8-real-environment.md)第 4–8 步。
   本轮仍为只读，不授权故障注入与受控变更。
3. 通知渠道全链路证据来自 [2026-11-06 验收](2026-11-06-w8-notify-channel.md)（不同任务、不同状态目录）；
   **R2 观察期内的送达、去重、恢复与失败补发仍须按逐日记录核对**，不能直接沿用。
4. 未实际重启服务器验收断电后调度恢复；`Restart=always` 只覆盖进程退出。
5. 通用模板 [w8-real-environment.md](../ops/w8-real-environment.md) 的门禁表按既有纪律**保持原样**，
   当前进度以本文件与逐日表为准，不改模板伪造完成状态。
6. 本次不改产品代码、不升版本（`VERSION` 仍为 `0.1.0-rc.3`）、不发布新 tag；
   仓库内变更仅为文档状态回填。

## 8. 停止与回退

```bash
# 暂停 R2（保留全部证据）
ssh obsServer 'OPSX_STATE_DIR="$HOME/.ops-agent-trial-r2" python3 "$HOME/.local/share/ops-agent/current/opsagent" schedule pause sch-35e3e0'

# 需要完全回到 R1 托管时：恢复 unit 备份并重启服务（不删除任何目录）
ssh obsServer 'cp -p "$HOME/.config/systemd/user/opsagent-schedule.service.bak-20261107-r2" "$HOME/.config/systemd/user/opsagent-schedule.service" && systemctl --user daemon-reload && systemctl --user restart opsagent-schedule.service'
```

回退不会删除 R2 证据；R1 的两个旧任务保持暂停，避免双调度。观察期间不要在 R1 目录
`add` / `resume` 任务。

## 9. 声明

- 本记录依据 2026-11-07 对 `obsServer` 的实际操作与只读核对整理；执行的是观察环境切换
  （新建目录、复制配置、改 unit 路径、重启用户级巡检服务、新增只读任务），**未改动业务服务或生产数据**。
- 凭据、真实地址、收件人与上游供应商名称不写入仓库；原始证据保存在服务器私有状态目录。
- 观察期内如再发生探针、通知状态机、调度推进或执行记录的重大修复，按 §1 规则再次重计 7 天。
