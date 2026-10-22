# W7 体检/心跳/预算/退避 复审记录

日期：2026-10-22｜范围：`usage.py`/`doctor.py`（新增）、`agent/llm.py`、`agent/llm_anthropic.py`、
`agent/schedule.py`、`agent/loop.py`、`opsagent`、`docs/ops/`（systemd/launchd 示例）、
`selftest.py`（W7 用例）｜方法：两轮独立只读复审 + 逐条修复 + 回归。

## 交付内容

| 能力 | 实现 |
| --- | --- |
| doctor | LLM 配置与 API 探活（鉴权/限流/网络分流）、状态目录可写与磁盘余量、SSH 逐台探活、调度心跳、通知渠道；fail 退出码 1 |
| usage/预算 | `usage.jsonl` 记账 + `usage-state.json` 按日聚合；`budget.json`（0600）daily_calls/daily_tokens；超限 `BudgetExhausted` |
| provider 退避 | 429/408/5xx/网络瞬断重试 2 次（指数退避 + 抖动，封顶 30s）；永久错误绝不重试；流式已发内容后不重试；退避与超时裁剪进任务 deadline |
| 心跳 | daemon 每次扫描与每个任务执行期间刷心跳；过期阈值按启用任务最大超时动态计算；过期经通知状态机告警并自动恢复 |
| 部署示例 | docs/ops/：systemd service（常驻）+ oneshot service + timer + launchd plist |

## 第一轮复审与修复（2 严重 + 5 中等 + 8 轻微）

- **S1 `_budget_cli` NameError**：opsagent 从未 `import core`，所有错误路径必崩——补导入，新增子进程级回归用例。
- **S2 budget.json 损坏 → OpsxError 从 llm.chat 逃逸**：交互入口只接 LLMError，用户看到 traceback → 包成 LLMError 带修复指引。
- **M1 心跳阈值与任务超时矛盾**：默认 900s 任务会被 600s 阈值误报 → 阈值改为按启用任务最大超时 + 300s 动态计算，执行期间每次 attempt 前刷心跳。
- **M2 usage-state 损坏静默清零**：→ fail-closed（备份 .corrupt + 事件 + OpsxError），有意取舍为"告警一次后自动恢复"（docstring 写明）。
- **M3 流式重试重复推送文本**：→ tracked_delta 记录已发内容，已发不重试。
- **M4 doctor 凭记忆写字段名**（endpoint_masked 不存在）→ 用 `notify.redact_endpoint`。
- **M5 退避突破任务预算约 10 分钟**：→ chat 加 deadline 参数，单次超时与退避都裁剪到剩余时间；loop.run_turn 传入。
- 轻微：record 读写加 flock、probe 文件 unlink 分开 try、SSH 探活超时 > ConnectTimeout、service/plist 的 User/KeepAlive/日志路径、测试环境变量 finally 恢复、recent 跨轮转读取、耗尽文案带恢复方式。

## 第二轮复审与修复（2 中等 + 6 轻微）

- **自定义 timeout > 900s 仍误报 stale**：阈值从常量改为按启用任务实际超时动态计算。
- **`_alert_budget_once` 无锁写 state**（与 record 的锁内写交错会互相覆盖）→ 新增 `usage.mark_budget_alerted()` 入同一把锁。
- 结构非法分支补齐备份+事件（与 JSON 损坏对称）；_state_lock 标注不可重入；
  budget recent 防御缺字段与负 tail；BudgetExhausted 文案去重；deadline 临界亚秒 overshoot 记录不改。

## 验证

- `./opsx selftest` **309/309**（W7 新增 9 项：记账/超限阻断/退避/401 不重试/预算耗尽规则照常/
  doctor 诊断分支/心跳三态告警与恢复/服务文件/CLI 错误路径/deadline 裁剪）。
- 实测：`opsagent budget status/set`、无 key 时 `doctor`（llm-config/llm-api fail 且建议明确）；
  自定义 3600s 任务 + 1500s 前心跳 → `daemon_health()` 返回 fresh（阈值 3900s）。

## 已知边界

- 预算是本机自我约束（多进程并发计数可能差几次）；token 流式响应按字符粗估并标"（估）"。
- usage-state 损坏的取舍是"告警一次后自动恢复"（fail-open 恢复），非永久卡死。
- doctor 的 LLM 探活只验证端点+密钥，不验证模型名存在性（各家列表解析不一）。
- LLM 单次调用超时可至 deadline 剩余；无 deadline 的调用方（REPL 交互）不受限。
