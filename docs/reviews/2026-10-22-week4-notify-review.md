# 2026-10-22 W4 通知与事件：实现与复审记录

对照 [roadmap](../2026-10-21-personal-ops-agent-roadmap.md) W4（单渠道通知、去重/恢复/静默、
轻量事件时间线、只读试用版）完成实现，并做了两轮独立复审。基线 `./opsx selftest` 164/164，
交付后 **206/206**；没有引入第三方依赖（HTTP 用标准库 `urllib`）。

## 1. 交付内容

| 工作包 | 实现位置 | 结果 |
| --- | --- | --- |
| 单渠道通知 | `agent/notify.py` | webhook 一种渠道，`format` 支持 `generic`/`slack`/`feishu`/`dingtalk`；配置 `~/.ops-agent/notify.json`（0600），URL 只以 `scheme://host/…` 形式出现在事件、发件箱、输出里 |
| 去重 | `notify._plan` | 每条异常一个 fingerprint（`check:<检查项id>` / `task:<任务id>` / `run:<任务id>` / `missed:<任务id>`），只在**新出现、级别升级/降级、超过提醒间隔、恢复**时发；一次运行最多一条消息 |
| 恢复 | `notify._recoverable` | 只有"本次确实检查过该目标、该分组跑过、目标判定不是不完整、摘要未被截断"才判恢复；其余保持沉默 |
| 静默 | `notify._suppression_reason` | `mute_until` 全静默（连异常一起压，只记事件与发件箱）；`quiet_hours` 只压警告/恢复，异常与"不可采集"照发；静默不计为"已通知"，解除后重新提醒 |
| 失败与重试 | 发件箱 + `retry_pending` | 发送失败落 `notifications/<id>.json`（失败原因、HTTP 状态、下次重试时间），指数退避 1min 起、上限 1h、最多 5 次；已恢复的旧告警标 `superseded` 不补发；静默/停用期间不补发 |
| 事件时间线 | `agent/events.py` | `events.jsonl`（0600，5MB 轮转，`--tail` 跨轮转可读）；只记状态变化，不记每次正常巡检；异常出现/升级/降级/提醒/恢复、通知发送/失败/静默/跳过、运行失败/超时/中断/错过、规则检查错误 |
| 证据与会话关联 | `checks.save_signal` + `schedule._attach_signal` | 规则结果按会话落 `signals/<session_id>.json`（含异常项、探针命令、输出片段、目标判定）；运行记录附 `anomalies`/`signal_path`/`rules_targets`/`rules_truncated`；消息里带 `run_id`、`session_id`、报告路径 |
| LLM 不可用仍上报 | `schedule._run_preset_checks` | 任务可声明 `--checks all`（或目标名 + `--checks-group`），在**调用模型之前**跑规则检查并落 signal；模型鉴权失败/断网时规则异常照常通知 |
| 入口 | `opsagent notify ...` / `opsagent events` / `events` 只读工具 | setup / status / test / list / retry / mute / unmute / enable / disable；agent 侧只有只读 `events`，模型不能自己发通知 |

## 2. 第一轮复审发现并修复

| 优先级 | 问题与复现场景 | 修复 |
| --- | --- | --- |
| P0 | 只查部分目标会误判恢复：`inspect --target web1` 的报告 `complete=True`，把 `db1` 的旧告警清成"已恢复" | 恢复要求 `signal.targets` 含该目标；新增 `target_verdicts`，按目标判定而非全局 `complete`；只跑部分分组时没跑的组一律不判恢复 |
| P0 | 运行记录只挂前 20 条异常，第 21 条起被判"本次没出现=已恢复" | 通知优先用 `checks.load_signal(session_id)` 读完整摘要（≤100 条）；紧凑副本标 `rules_truncated`，截断时禁止判恢复 |
| P0 | `notify.json` 里的 webhook URL（即 token）可被 `read`/`cat` 读进模型上下文 | 加入 `core._SENSITIVE_FILE_PATTERNS`；整个 `~/.ops-agent` 目录加入敏感目录（递归读取会扫到它），`reports/` 子路径仍可读 |
| P0 | `http.client.HTTPException`（`BadStatusLine`/`IncompleteRead`/`InvalidURL`）不是 `OSError`，一次畸形响应打死守护进程 | `_post` 显式捕获；`_deliver` 兜底任何异常；daemon 的重试用 `_safe_retry_notifications` 包住 |
| P1 | 重试绕过静默：`notify mute --for 2h` 后旧失败告警仍会被补发 | `retry_pending` 先判 `enabled/mute_until/quiet_hours`，被压制标 `muted` |
| P1 | 恢复消息在静默期被永久丢弃（状态先清了，之后不再补发） | `_plan` 只标 `pending_recovery`，真送达（或未配置/停用）才 `_mark_recovered` |
| P1 | 静默/未配置期间每轮写一条 `anomaly-new` 事件，时间线被刷成日志洪水 | `_mark_muted` 不再清 `notified_at`，改用 `needs_notify`；`event_severity` 控制事件只记首次/级别变化 |
| P1 | 消息超过上限时先被切掉的是"报告在哪"，且先截断后脱敏可能漏半截私钥 | 先留尾部预算（报告路径、追溯），脱敏后再按整行回退；尾部始终保留 |
| P1 | 配置了非法端口/控制字符时 `notify status`/daemon 启动抛未捕获异常 | URL 校验端口范围与控制字符；`redact_endpoint` 兜底；`describe_config` 不抛 |
| P1 | 钉钉/飞书用 HTTP 200 + `errcode` 表示失败，被记成 `sent` | `_response_error` 按渠道解析；POST 不跟随重定向（301/302 会被改成 GET 返回 200） |
| P1 | `quiet_hours` 的 `8:00-08:00` 能通过校验，等于全天静默 | 按分钟比较起止时间 |
| P1 | critical 变 unknown/error 时被降级成 warning 并静默 | 新增 `unreachable` 级别（rank 高于 critical），级别变化单独发消息；5 分钟内"未升级的变化"按抖动压掉 |
| P1 | "首轮 LLM 就不可用"时规则检查根本不会跑（原实现依赖模型先调用 `inspect`） | 任务级 `--checks`：模型之前跑规则检查并落 signal |
| P2 | `_notify_run` 的异常分支不落盘，`schedule runs` 看不到"通知处理失败" | 异常分支写 `notify_status=error` + 原因 |
| P2 | 被 kill 的运行记录没有 `session_id`，无法按会话找回摘要 | 先建会话、回写 running 记录再调用模型；摘要路径也写回记录 |
| P2 | `missed:` 立刻又被判"恢复"，用户连收两条矛盾消息 | 错过是 `transient` 一次性事件，不参与恢复 |
| P2 | `setup` 会顺带启用通知并清掉静默时段；`enable/disable` 会解除静默 | 只改显式传入字段；`enable/disable` 不动 `mute_until` |
| P2 | `events.tail` 轮转后看不到 `.1` 里的历史 | 旧文件在前一起读 |

## 3. 第二轮复审发现并修复

| 优先级 | 问题与复现场景 | 修复 |
| --- | --- | --- |
| P0 | 目录敏感规则可被 `~/.ops-agent/.`、`~/.ops-agent/*`、`n*` 绕过（`grep -r` 直接读出 webhook token） | `core._path_probes`：词法归一 + 去掉 glob 段取最左目录 + realpath，文件/目录规则都按归一后的路径判；补三条变体回归 |
| P0 | `checks` 摘要自身 100 条上限无标记 → 120 条异常时 20 条从未告警、100 条被误判恢复 | 摘要带 `truncated`；`_recoverable` 对截断摘要禁止恢复 |
| P1 | 恢复消息**发送失败**时仍清状态、写"已恢复"事件，重试又被标 superseded → 恢复永久丢失 | 只有 `status == "sent"`（或未配置/停用）才清；失败保留 `pending_recovery`，下一轮补发 |
| P1 | 进程被 kill 后 `_signal_from_record` 因缺 `signal_path` 直接返回 None，规则异常丢失 | 有 `session_id` 就按会话读摘要，并用 `created_ts` 与运行开始时间交叉校验 |
| P1 | 先跑 `schedule runs`（对账）后，daemon 不再通知那些中断记录 | 运行记录持久化 `notify_status`；daemon 启动与每轮扫描"终态但未通知"的近期记录补发 |
| P1 | 任一目标不可达 → 全局 `complete=False` → 所有 `check:` 项永远无法恢复 | 按 `target_verdicts` 与 `groups` 判定单项覆盖 |
| P1 | `notify-state.json` 损坏时把所有失败通知判成 `superseded`，告警静默丢失 | 状态加载返回"是否可信"，不可信时不做 superseded 判定 |
| P1 | `--checks` 指向未注册目标在 add 时被接受，运行时静默降级（任务看起来仍"正常"） | add 时校验目标名；规则检查失败写审计 + `schedule-checks-error` 事件 |
| P1 | 预设规则检查不受任务预算约束（N 个目标 × 120s 可超过 900s） | `run_checks` 接受整体 `deadline`，预设检查取任务预算的一半（上限 300s） |
| P2 | 从没通知过的条目（未配置/停用期间发现）永久残留并每轮虚增 `recovered` | `notified_at` 为空时静默清状态并落盘；删除任务时清掉它的 `task:`/`run:`/`missed:` 指纹 |
| P2 | 静默期 `muted_at` 每轮刷新，提醒窗口永不生效且每轮 fsync | 只在真的写 muted 记录时刷新 `muted_at`；状态无变化不落盘 |
| P2 | 发件箱目录损坏时异常穿透 `process_run`，状态没落盘 → 每轮重复发送 | `_store_notification` 只记错误，不阻断状态落盘 |
| P2 | `generic` 格式的 200 + `{"ok":false}` 仍记 `sent` | 响应体校验不按 format 短路 |
| P2 | `notify retry` 把 superseded/muted 说成"仍失败: None"并退出 1 | 按状态给文案；只有 `failed` 才退出 1 |
| P3 | 降级也记成 `anomaly-escalated` | 独立 `anomaly-changed` 事件；统计分 `changed` |
| P3 | 恢复项绕过预算导致编号跳号、末行被切半行 | 恢复项也计入预算；编号用已列出序号；硬截断按整行回退 |
| P3 | `events.tail` 在只剩 `.1` 时返回空 | 任一候选文件存在即可读 |

## 4. 已知边界

- **单渠道**：只支持一个启用的 webhook。多渠道路由、IM 双向对话、Web UI 不在本期。
- **发送持锁**：`process_run` 在 `notify.mutex` 内做 HTTP（默认 8s 超时，最长 30s），
  保证"同一次状态变化只发一条"；代价是慢端点会推迟同机并发的一次 `notify retry/status`。
  真出现慢端点问题再改成"锁内计划 + 锁外发送 + 乐观回写"。
- **全静默会压掉异常**：`mute` 是用户显式要求"别吵"，异常也不发；事件与发件箱仍记录，
  `opsagent events` 可查。只压警告请用 `quiet_hours`。
- **恢复是推断**：判据是"本次检查过该目标/该分组且不完整项为空"。检查项被删除、目标被移出
  清单时也会按恢复处理（并把原因留在事件里）。
- **信号会轮转**：`signals/` 保留 300 份；摘要文件被删除后运行记录退回 20 条紧凑副本，
  此时禁止判恢复（宁可少一条恢复通知，也不误报）。
- **时间预算**：预设规则检查最多用任务预算的一半（上限 300s），剩余时间留给模型；最坏情况
  仍是"预算 + 一次 LLM 调用超时"。
- **退出码不变**：通知发送失败不改变 `run --once` 的退出码（0/1/2/3 仍表示巡检本身），
  失败通过 `notify status`、`schedule runs` 的通知字段与事件时间线暴露。

## 5. 验证

```bash
./opsx selftest                     # 206/206 passed（W4 新增 42 项）
python3 -m compileall -q ...        # 通过
git diff --check                    # 通过

# 真实 HTTP 桩（非 mock）验证发送路径与 CLI 全链路
./opsagent notify setup --url http://127.0.0.1:PORT/hook
./opsagent notify test              # 已发送（HTTP 200）
./opsagent schedule add --every 60 --checks local --checks-group resources "解读规则结果"
OPSAGENT_API_KEY= ./opsagent schedule run --once
# → 运行 failed（LLM 不可用）、规则报告已落盘、通知 sent（含报告路径与会话 id）
./opsagent schedule runs            # 显示 通知: <id> sent
./opsagent notify list              # 记录与失败原因
./opsagent events --tail 6          # run-failed / anomaly-new / notify-sent
```

覆盖的关键场景：同一异常连续三轮只发一条；`renotify_after` 到点发提醒；warn→critical 立即升级；
critical→unknown 发"不可采集"；完整且干净的运行发恢复且只发一次；静默期记录 `muted` 且解除后
重新提醒；静默期恢复不丢；发送失败可查可重试、退避后成功；已恢复的旧失败通知标 `superseded`；
状态文件损坏不误判恢复；`inspect --target` 子集不误清其他目标；摘要截断不误判恢复。
