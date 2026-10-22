# W5 复审记录：变更执行记录、目标级写锁、审批次数、远程原子恢复

日期：2026-10-22。范围：W5 全部未提交改动（`execution.py` 新增，`core.py` / `remote.py` /
`opsx` / `mcp_server.py` / `agent/tools.py` / `agent/loop.py` / `.claude/agents/ops.md` 修改）。
方式：两轮独立只读复审（各自完整读 diff + 实际执行复现脚本）+ 自查；每条严重问题都写了复现命令。

## 1. 交付内容与验收映射

| 验收门槛 | 实现 | 证据 |
|---|---|---|
| 两个会话不能重叠修改同一目标 | `core.target_lock`（flock，`~/.ops-agent/locks/<target>.lock`，本机用 `@local` 避免与名为 `local` 的远程目标抢锁）；锁覆盖"快照 + 执行"整段；抢不到 exit 121 并记 `aborted` | `test_target_lock_rejects_concurrent_change`、`test_target_lock_across_processes`（真跨进程）、CLI 实测：`目标 local 正在被另一次变更占用（当前持有者：pid 99868）`，exit 121，且**不消耗审批** |
| 断线进入 unknown，不自动重放 | SSH 255 / 远程超时 → `unknown`；`_prepare_exec` 在门禁处查未核实 unknown；`_view` 让"进程已死的 running"也进同一门禁 | `test_remote_timeout_and_ssh255_become_unknown`、`test_dead_process_running_blocks_replay_without_reconcile`、CLI 实测 exit 255 → `unknown` → 再执行 exit 120 |
| 恢复中断不留下半份配置 | 恢复脚本：同目录 `mktemp` → 写入 → **校验字节数** → `chmod` → `rename`；失败/中断时 `trap` 清临时文件，原文件不动 | `test_remote_restore_atomic_keeps_mode_and_rebuilds`、`test_restore_verifies_size_before_replacing`（真 `/bin/sh` 执行模板） |
| 已删除文件的恢复权限有明确策略并测试 | 快照记 `mode` + 属主；回滚优先用快照 mode；快照无 mode 时**覆盖沿用目标当前权限、重建用 0600**；属主/属组不假装能还原（报告写明需人工 chown） | `test_restore_without_mode_keeps_existing_permissions`、`test_rollback_script_fully_rendered_and_verified` |
| 只读版通过 7 天试运行 | **未完成**（2026-10-22 起算，最早 10-29 判定） | `docs/releases/2026-10-22-readonly-trial.md` 逐日记录，未填满前不宣称通过 |

附加交付：审批次数 one-shot（`approve --uses`，R3 只能 1 次，`consume_stamp` 在 approvals
flock 内原子 +1），`opsx executions list|show|resolve`，MCP/agent 只读 `ops_executions`，
README「变更执行记录与并发控制（W5）」章节。

自检：**241 → 261 项，全部通过**。

## 2. 第一轮复审：3 严重 / 9 中等

### 严重

| # | 问题 | 复现 | 修复 |
|---|---|---|---|
| S1 | 远程 kubectl 变更被**永久拒绝**：`remote.exec_change` 用 `target_env`（无 `k8s` 键）与审批时的 `resolve_env`（有 `k8s`）比对，必然判"执行环境已变化"，重新批准也不通 | `core.approve(cmd, target="web1")` 后 `check(cmd,"web1", remote.target_env("web1"))` → 2 | 三处统一 `core.resolve_env(cmd, name)`；补 `test_remote_kubectl_approval_env_matches_execution` |
| S2 | "不自动重放"只在有人先跑过 `opsx executions list`（唯一会 reconcile 的地方）时生效：进程被 kill 后记录停在 `running`，`pending_unknown` 看不到 → 重放被放行 | 写一条 `status=running, pid=99999999, phase=exec` 后 `_prepare_exec` 返回 0 | `execution._view()`：读取时把死 pid 的 running 呈现为 unknown/aborted，`pending_unknown` 经 `list_records` 自然看到；`list_records` 不写盘（只读工具不再 reconcile） |
| S3 | `except BaseException → unknown` 把"根本没执行"的路径（`--wait-lock -1`、非法目标名、等锁时 Ctrl-C）也记成 unknown，并锁住该目标该命令 | `opsx exec --wait-lock=-1 ...` → 一条 unknown + 后续 exit 120 | `dispatched` 布尔（只有 `_run_change` 前才置真）+ `_validate_lock_args` 提前校验 + `phase` 区分（`lock/snapshot/authorize/backup` → aborted）；`resolve` 允许直接核实"running 但进程已死"的记录 |

### 中等

- **M1 退出码撞车**：门禁码 5/6 与命令自身退出码重叠，agent 会把真实失败报成"未执行"。改为 120/121，并把 agent 判据从"看退出码"改成"看有没有说明命令已下发的记录"。
- **M2 多层软链**：`a → b → c` 只解析一层，会把中间软链换成普通文件而真正目标未改却报成功。改为 `while [ -L ]`（上限 8 层）。
- **M3 后验可静默跳过**：`wc` 失败/输出缺失时 `size=None` 直接宣告成功。改为脚本内 `mv` 前比对字节数，Python 侧 `size is None` 视为"无法确认"（`_UncertainChange` → unknown）。
- **M4 旧快照无 mode 被统一降权到 0600**：`/etc/init.d/*` 会失去执行位。改为覆盖沿用目标当前权限、仅重建用 600。
- **M5 提前 EOF 仍会覆盖**：原子 rename 只保证"不留下写一半的文件"，不保证"收到的字节是完整的"。由 M3 的字节数校验解决。
- **M6 MCP `ops_executions` 泄露命令原文**：复核确认 `_result` 内部已 `redact_obj`，**误报**，未改。
- **M7 `uses` 不进确认框、R3 不设限**：模型可用 `uses=100` 换到与 1 次相同的确认框。确认文案加"可用次数"，R3 强制 `uses=1`。
- **M8 用尽戳仍显示为可用授权**：`collect_state` 只判 TTL。补 `used >= uses` 过滤（`stamp_exhausted` 仍保留，用于给"已用尽"提示）。
- **M9 `approve` 与 `consume_stamp` 不共用锁**：并发时 `consume` 的 `os.replace` 会用旧内容覆盖新授权。`approve`/`revoke` 都进 approvals flock + 原子替换。

## 3. 第二轮复审：3 严重 / 7 中等（含 1 条"上轮修复没落地"）

| # | 问题 | 说明与修复 |
|---|---|---|
| S1' | **上轮 #5 未真正落地**：`remote.rollback` 仍在替换旧占位符 `__PATH__/__MODE__`，模板已改成 `@@PATH@@/@@MODE@@/@@SIZE@@` → 远程回滚 100% 失败（`p=@@PATH@@`、`@@SIZE@@` 永不相等导致 exit 3） | 改为 `_render_restore(item["src"], mode_arg, len(payload))`；补 `test_rollback_script_fully_rendered_and_verified`：fake ssh **真实执行**下发脚本并断言 `"@@" not in cmd` |
| M1' | 新增恢复测试只调 `_render_restore`，**绕过 rollback 调用点**，所以 S1' 逃过自检 | 同上，新增全链路用例 |
| M2' | "后验缺失视为失败"未实现（`size is not None and ...` 会整段跳过），且 `_UncertainChange` 根本不存在 | 加 `_UncertainChange(RemoteError)` → `finish unknown`；`size is None` 直接判"无法确认"；补 `test_rollback_without_backcast_is_uncertain` |
| M3' | `phase="exec"` 在 `consume_stamp` **之前**写入：快照后、授权前被 kill 会记 unknown（过拦）；rollback 的 `phase="backup"` 也不在"未下发"集合里 | 该处改 `phase="authorize"`，`BEFORE_EXEC_PHASES = (lock, snapshot, authorize, backup)`；补 `test_execution_authorize_phase_is_not_unknown` |
| M4' | `ops_executions show` 走 `execution.get()`（不经视图、不 reconcile），与 list 自相矛盾（list 说 unknown，show 说 running） | `get(execution_id, view=True)` 默认返回视图；`format_record` 对 `stale` 记录加一行说明 |
| M5' | 重复的测试定义（8–12 个用例各两份），`ALL` 按名字去重导致前一份是死代码 | 全部删除，`dupes: {}`；同时把"插入测试"改为先断言 marker 唯一 |
| M6' | agent `_ops_exec` 把"本次没执行"配上**上一次**的 execution_id 与状态（"失败 + 状态 succeeded"） | 记 `started`，只认 `started_ts >= started` 的记录；补 `test_agent_exec_reports_only_current_run` |
| M7' | `revoke` 不进 flock：并发消耗在锁内已读到戳，撤销删除后它又写回 | `revoke` 进 `.mutex` 后再 unlink |
| L2 | reconcile 把 `aborted` 也记成 `execution-unknown` 审计事件 | 按实际状态分事件名 |
| L3 | `_parse_restore_info` 取第一条、`mode=?` 未校验 | 取最后一条；`mode` 必须 `[0-7]{3,4}` |
| L5 | `chmod "$keep"` 遇非八进制会让整次回滚中止 | `case "$keep" in [0-7]*) chmod ... ;; esac` |
| L7 | 损坏的 execution JSON 既不计入统计也永不清理 | `prune` 按 mtime（7 天）顺带清理读不出来的文件 |
| L10 | `agent ops_executions` 允许 `tail=0` 而 `opsx` 拒绝 | 统一为"tail 须为正整数"（agent 侧返回提示） |
| L1 | 退出码注释同时写着 5/6 和"不用 5/6" | 统一为 120/121 |

### 特别记录的教训

第二轮报出的 S1'（rollback 未接渲染函数）来自一个**修复脚本的写入步骤没有执行，却被误判为成功**。
当时的批量替换脚本在 `assert` 处失败，而命令输出里同时出现了"ok"与"COMPILE_OK"（另一次执行的产物），
于是被当成已修复，后续的自检又因为新测试只测 `_render_restore`（不测调用点）而全绿。

处理方式（已落实）：**每次改动后立刻用独立命令核对文件内容**（`grep`/`python -c` 判断关键字符串），
不再依赖"替换脚本自己说成功"；新增测试必须覆盖**真实调用点**（本次是让 fake ssh 真正执行下发脚本）。

## 4. 验证

```
./opsx selftest                 # 261/261 passed（W5 新增 20 项）
python3 -m compileall -q ...    # 通过
git diff --check                # 通过
```

真实 CLI 路径（临时状态目录 + fake ssh 二进制）：

```
approve → exec                        exit 0，executions 显示 succeeded + snapshot_id
exec 再跑                              exit 2（审批戳已用尽，一次批准一次执行）
ssh exit 255 → exec                   exit 255，记录 unknown，提示核实命令
exec 再跑                              exit 120（同目标同命令被未核实记录挡住，一次 ssh 都不发）
executions resolve --observed-failed  核实后重新批准可执行
另一进程持有 local 锁时 exec            exit 121，记 aborted，且不消耗审批
```

## 5. 未完成与已知边界

- **7 天连续试运行未完成**：只读版（巡检 + 通知）自 2026-10-22 起观察，最早 2026-10-29 判定。
  记录表在 `docs/releases/2026-10-22-readonly-trial.md`，未填满前 W5 的这条验收保持"未通过"。
- pid 复用方向是**少拦**（新进程占旧 pid → 看起来在跑 → 放行）。要更严需比对 `/proc/<pid>` 启动时间。
- `unknown` 只表达"无法确认"，不代表命令一定生效；核实依赖人工按证据判断。
- 目标锁是单机文件锁：多台机器各自管各自的目录时互不可见（本项目是单机工具，不涉及）。
- `opsx rollback` 的超时也记 `unknown`（`mv` 可能已完成），但 rollback 不参与 exec 的重放门禁
  （回滚正是处置手段之一，不应该被挡住）。
