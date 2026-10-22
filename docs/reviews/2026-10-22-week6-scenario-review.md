# W6 变更场景（预检→快照→执行→后验→回滚建议）复审记录

日期：2026-10-22｜范围：`scenario.py`（新增）、`scenarios/*.json`（3 个内置场景）、
`agent/tools.py`（`_scenario_run`）、`opsx`（scenario 子命令）、`execution.py`、
`config.yaml`、`agent/events.py`、`selftest.py`（W6 用例）｜方法：两轮独立只读复审 + 逐条修复 + 回归。

## 交付内容

| 能力 | 实现 |
| --- | --- |
| 场景编排 | `scenario.py`：预检→快照→执行→后验→回滚建议；预检/后验探针强制 R0，执行复用 `exec_change` 全部守门 |
| 内置场景 | `systemd-restart`（LoadState→active）、`docker-restart`（running+health）、`nginx-reload`（nginx -t 预检） |
| 后验语义 | 命令 exit=0 但后验不健康 → `verify_failed`，执行记录 `postcheck=failed`，事件关联 execution_id，报告留证据 |
| 审批边界 | 预检失败/无审批时不建快照、不消耗审批、不产生执行记录；agent 工具先展示完整命令与探针请求确认 |
| 回滚并验证 | `scenario rollback` 确认后恢复，重跑后验作证据（明确"不判定"：失败可能正是回滚生效） |
| 门禁分流 | 120→uncertain、121→busy、缺审批→approval_required（R3 带 --force）、守门永久拒绝→gate_denied、快照失败→snapshot_failed |

## 第一轮复审与修复（5 中等 + 5 轻微）

- **M1 门禁码误判**：120/121/快照门 2 全被错标为 exec_failed/approval_required → 按 code 分流，新增 busy/gate_denied。
- **M2 死代码**：`scenario.py` 的 `cli()` 与 opsx 内联分发双份漂移 → 删 cli()，退出码唯一落在 opsx。
- **M3 CLI 测试恒空转**：正则与真实输出格式不符且拿不到就跳过 → 按"执行记录: exe-…｜"解析，拿不到即失败。
- **M4 systemd 预检 glob 不匹配**（service=nginx 匹配不到 nginx.service）→ 改 `systemctl show -p LoadState --value` + expect: loaded。
- **M5 nginx -t 覆盖范围**：description 注明只覆盖主 conf include 范围。
- **L1-L3**：actor 未转发、rollback 记录缺 pid 过滤、catalog 损坏条目重列。

## 第二轮复审与修复（2 中等 + 7 轻微）

- **R3 建议缺 --force**（照做必失败）→ approval 建议按 classify 追加 --force 与不可逆提示，并用 shlex.quote。
- **测试尾部重复段**（编辑脚本在特定环境下执行两次导致的重复定义）→ 去重，保留逐字一致的一份；**教训：每次批量编辑后必须核对文件内容，不能只看"ok"输出**。
- approval 建议附守门原因（环境变化导致旧戳失效时不再像"系统没记住"）。
- 快照失败（exit 3）单列 `snapshot_failed`（命令从未下发，不是"执行失败"）。
- 退出码表写进 `opsx scenario --help`（原本只在源码注释）。
- `systemctl cat` 从白名单移除（本次无使用方；unit 文件可含内联凭据，读取面不扩大）。
- 三个门禁分支也落场景报告（busy/uncertain 排障最需要留痕）。
- 保留未修：`approve` 后场景 JSON 被改的 TOCTOU（fail-closed：命令变化导致戳哈希不匹配，无法写入；残留窗口极小且为本机自伤）。

## 验证

- `./opsx selftest` **300/300**（新增 8 项场景用例 + 门禁分流用例）。
- 端到端真机：无审批 → exit 2 且无任何写入（无快照/无记录）；审批后 → 通过并关联 execution_id/快照；
  回滚未确认 → 计划预览 exit 2；确认回滚 → 配置恢复、回滚后验证作证据、事件关联。
- 门禁分流实测：死 pid 记录 → uncertain；持锁并发 → busy；快照命令守门 → gate_denied；
  R3 → 建议带 --force。

## 已知边界

- 后验只能验证"场景里写了的检查项"；没写的健康维度仍只有人能发现。
- 远程场景探针走 SSH（remote._ssh），目标机必须支持对应命令（systemctl/docker/nginx）。
- 后验通过不等于"本次变更正确"，只等于"检查项当前健康"。
