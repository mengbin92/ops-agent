# 2026-10-21 W3 调度可追踪运行：实现与复审记录

对照 [roadmap](../2026-10-21-personal-ops-agent-roadmap.md) W3（run/session 记录、状态与历史查询、
整任务超时、只读重试、暂停/恢复、调度失败退出码）完成实现，并做了两轮独立复审。
基线 `./opsx selftest` 143/143，交付后 **164/164**；没有引入第三方依赖。

## 1. 交付内容

| 工作包 | 实现位置 | 结果 |
| --- | --- | --- |
| 运行记录 | `agent/schedule.py` 的 `_begin_run` / `_finish_run` / `_write_run` | 每次执行一份 `~/.ops-agent/runs/<run_id>.json`（0600，保留 200 条）：run_id、task_id、session_id、pid、status、verdict、started/finished/duration、attempts/attempt_errors、error、result_preview、report_path |
| 状态与历史查询 | `list_runs` / `format_runs` / `opsagent schedule runs` / `schedule` 工具 `action=runs` | 倒序列出，可按 `--id`（含前缀）过滤；每条显示状态、结论、耗时、会话、报告路径；删任务后仍可按前缀查历史 |
| 中断与错过 | `reconcile_runs` / `_missed_count` / `_record_missed` | daemon 被 kill 后遗留的 `running` 在下一次启动或查询时对账为 `interrupted`；停机期间错过的时点记为 `missed`（含次数）。判定基于心跳 `meta.last_scan`，任务自己跑得久/排队不会被误判 |
| 整任务时间预算 | `timeout_seconds` → `ToolContext.deadline` → `loop.run_turn` | 默认 900s（60–86400）；在轮次边界停止，结果标 `timeout`；workflow 剩余步骤显式记「时间预算耗尽」，子代理入口也检查 |
| 只读有限重试 | `_execute_run` | 异常重试 1 次（间隔 3s），每次新会话（不重复追加 prompt），失败原因进 `attempt_errors` |
| 暂停/恢复 | `pause` / `resume` + 工具 action | 暂停后到期不跑、已排队的执行在执行前被拦下；恢复从当前时间重算，不补跑暂停期 |
| 失败退出码 | `runs_exit_code` / `daemon` | `--once`：0 正常、1 运行失败/超时/对账出中断、2 发现警告或异常、3 不完整/未判定/有错过/因已有调度进程未执行 |
| 稳定性 | `_safe_audit` / `_safe_save` / `_claim_due` / `_record_synthetic_failed` | 审计与注册表写失败不中断巡检；单任务异常产出一条可查询的失败记录；执行前短锁复核并推进 `next_run`，排队任务不会因进程被杀而静默漏检 |

## 2. 复审发现并修复的问题（第一轮）

| 优先级 | 问题与复现场景 | 修复 |
| --- | --- | --- |
| P0 | 结论可被后文翻转：`_detect_verdict` 全文取最后一个"结论"，日志/引用里的 `结论：正常` 会覆盖真实结论；`max_turns` 截断仍记 `succeeded/正常/exit 0` | 只认"单独一行的结论"或"报告头加粗结论"，单独行优先；剥离代码围栏与引用块；轮次截断/解读不完整一律降级为「不完整」 |
| P0 | 运行记录写失败（磁盘满/只读 home）会中断整轮 `run_due`，而 `next_run` 已推进 → 该周期被静默跳过 | `_begin_run`/`_finish_run` 捕获 `OSError` 并标 `unrecorded`；`run_due` 单任务异常用兜底记录隔离；写失败时心跳/排期降级退避 |
| P1 | `every_seconds` 为数字字符串时 `_missed_count` 抛 `TypeError`，每次到期都崩 | 归一化走 `_interval`，`run_due` 捕获 `TypeError/ValueError`；新增时间配置校验函数，损坏条目按原有语义停用 |
| P1 | 一条损坏的运行记录让 `schedule runs`/对账崩溃（缺 `started`、超大 pid 触发 `OverflowError`） | `_read_run` 校验展示字段与时间戳，`_pid_alive` 捕获 `OverflowError`，`format_runs` 全部走 `.get` |
| P1 | 对账读-判-写竞态会把刚成功的运行覆盖成 `interrupted`；工具路径不对账 | 对账在 `runs.mutex` 内"读-重读-确认仍 running-写"；工具 `runs` 也先对账；对账同步任务 `last_status` |
| P1 | 结论正则的 `\b` 对中文过严（`结论：异常项 3 个` 变成未判定） | 改为行锚定 + `(?![\u4e00-\u9fff])`，允许整行加粗与前导空白 |
| P1 | `report_path` 从模型文本提取，可被伪造（`/etc/passwd`） | 只接受 `STATE_DIR/reports/` 下、且真实存在的文件 |
| P1 | 重试复用同一会话：prompt 被重复追加，首次失败信息丢失 | 每次尝试新建会话，失败原因记入 `attempt_errors` |
| P2 | `timeout_seconds` 类型稍不符（`600.0`）就永久停用任务 | 可解析的数值归一化；不可解析退回默认 900s；`schedule list` 显示生效预算 |
| P2 | `resume` 对已启用任务也重拨 `next_run`（把 30 秒后的执行推后一个周期） | 只对"原本暂停"的任务重算；重复 resume 不动排期 |
| P2 | 暂停发生在扫描之后时，已排队的任务仍会执行 | 执行前 `_claim_due` 短锁复核"仍在且启用、仍到期" |
| P2 | `next_run` 从扫描时间推进，长任务会背靠背执行 | 任务结束后按完成时间重排（`_finish_run(reanchor=True)`） |
| P2 | 退出码在"没真正检查"时返回 0（锁冲突、对账出中断、有错过） | 锁冲突 = 3；本轮对账出 `interrupted` = 1；`missed` 计入 3 |
| P3 | 远端超时原因被错误信息前缀吞掉；核数探测失败时证据丢失 | 前缀表补 `[opsx] 超时`；保留原始输出 |

## 3. 复审发现并修复的问题（第二轮）

第二轮专门复审"修复本身"，又发现：

| 优先级 | 问题与复现场景 | 修复 |
| --- | --- | --- |
| P0 | 扫描时一次性推进**所有**到期任务的 `next_run`，第一个任务执行期间进程被杀 → 后面排队的任务既不补跑也无任何记录（daily 会静默少检一天） | 改为两阶段：扫描只认领（算 missed、刷心跳），执行前 `_claim_due` 在短锁内复核并推进该任务的 `next_run`；被测用例验证"第一个任务被杀后，第二个仍在到期状态并于下轮补上" |
| P0 | `_synthetic_failed` 只构造 dict 不落盘，`schedule runs` 查不到这条失败 | 改为 `_record_synthetic_failed`：写盘 + 写回任务状态，并补"兜底记录可查"的断言 |
| P1 | `_save` 失败被吞后 daemon 每 1 秒重跑一次（next_run 未推进），形成端点风暴；`core.audit` 自身在 ENOSPC 时抛错还会打断整轮 | 睡眠退避下限提到 30s；新增 `_safe_save`；所有审计改走 `_safe_audit`（ENOSPC 时静默降级） |
| P1 | `--once` 遇到 `OSError` 抛未捕获异常，退出码契约失效 | once 分支与守护分支统一捕获 `(ScheduleError, OSError)` → 1；CLI 错误信息走 `core.redact` |
| P1 | `list_runs` 用未校验的 `started_ts` 排序，一条字符串时间戳就让 `schedule runs` 崩 | `_read_run` 校验时间戳为有限数值；排序键 `float(...)` 兜底 |
| P1 | 结论解析两处残余：整行加粗/缩进识别不到（误报 exit 3）；粘贴在真实结论之后的旧报告头会翻转判定 | 允许 `\s*`/整行加粗；剥离代码围栏与引用块；单独行结论优先于报告头 |
| P1 | `report_path` 未校验存在、含空格的路径被截断后仍被采信 | 正则改为整行贪婪 + `is_file()` 校验；补"空格路径"与"不存在路径"用例 |
| P2 | `every_seconds: 60.0` 会永久停用任务；`meta.last_scan` 非数值时逐个停用任务 | `_interval` 接受值等于整数的 float；`_meta_ts` 把非法时间戳按"无心跳"处理 |
| P2 | `run_id` 用 `task_id` 拼文件名，注册表里的 `id` 无字符白名单（可用 `../` 逃出 runs 目录） | `_load_state` 校验 `id` 为 `[A-Za-z0-9._-]+` |
| P2 | 任务删除后 `runs --id <前缀>` 查不到历史 | 解析失败时按前缀过滤（CLI 与工具一致） |
| P3 | 测试有效性：损坏记录用例无正向对照（"永远返回空"也能过）；失败隔离用例未验证兜底记录可查 | 补正向对照、兜底记录可查断言、队列被杀后可补跑断言 |

## 4. 验证

- `./opsx selftest`：**164/164**（W1/W2 的 143 项全部保留，新增 21 项：运行记录与查询、
  暂停/恢复与暂停取消排队、中断 vs 错过、时间预算（轮次边界 / workflow 剩余步骤 / 单步被截断）、
  重试（新会话 + deadline 传递）、`--once` 五种退出码、结论解析（含引用/围栏/加粗）、
  轮次截断降级、记录写失败隔离、损坏记录与坏配置容忍、心跳语义（自身慢不计 missed）、
  报告路径校验（越界/不存在/含空格）、锁冲突退出码、队列被杀后可补跑、前缀查询与退避、
  间隔类型归一化）。
- `python3 -m compileall -q core.py remote.py opsx mcp_server.py opsagent agent selftest.py` 通过；
  `git diff --check` 通过。
- 真机（临时状态目录）跑通：`add --timeout`、`run --once`（无 API Key → failed、退出码 1）、
  `runs`（failed + missed(9)）、`list`（显示预算与上次状态）、`pause`/`resume`、
  伪造 running 记录后 `run --once` 提示"发现 1 次中断的执行"并把它标为 `interrupted`。
- 未连接真实 Linux 目标与真实 LLM；LLM 失败路径用"缺少 API Key"实测，其余用假执行器覆盖。

## 5. 验收对照（W3 门槛）

| 门槛 | 结论 | 依据 |
| --- | --- | --- |
| 人为中断 daemon 后可辨别未执行和中断任务 | ✅ | `reconcile_runs` 把遗留 running 标 `interrupted`（读-重读-写 + 锁）；停机错过的时点记 `missed` 并注明次数；`schedule runs` 两者并列可查；`test_schedule_interrupted_vs_missed` |
| 一个任务超时不会永久阻塞其他任务 | ✅ | 时间预算在轮次边界停止（`test_run_turn_deadline_stops_at_turn_boundary` 验证预算耗尽后一次 LLM 都不调），workflow 剩余步骤显式记失败；超时任务标 `timeout` 后继续下一个；队列被杀后剩余任务下轮补上 |
| `--once` 能区分运行失败与检查发现异常 | ✅ | 退出码 1（failed/timeout/中断）vs 2（警告/异常）vs 3（不完整/未判定/错过/未执行）、0 正常；`test_schedule_once_exit_codes` 实测四条路径 |

## 6. 已知边界（不在 W3 解决）

- 任务串行执行：一个长任务会推迟后面的任务；没有并发执行器，也没有跨进程的写锁（daemon
  已用 flock 保证单实例，`--once` 拿不到锁会返回 3 而不是排队）。
- 时间预算在轮次边界检查，最坏是「预算 + 一次 LLM 调用超时（默认 300s）」；单次 `bash`
  （120s）与规则检查（单目标预算）不受该 deadline 直接约束。
- `interrupted` 依赖 pid 存活判定：pid 被复用（重启后同号进程）时可能误判为仍在运行。
- `missed` 依据心跳推断：进程被杀期间写盘失败会保守地少报；`--once` 模式没有常驻心跳，
  只在运行瞬间刷新。
- 结论解析仍是文本启发式：模型不按"单独一行 `结论：X`"输出时记「未判定」（exit 3），
  不会猜成正常；工具结果里的结构化结论尚未回填到运行记录。
- 没有通知渠道与事件去重（W4）；`runs` 只有本地文件，未做按天归档或多主机聚合。
