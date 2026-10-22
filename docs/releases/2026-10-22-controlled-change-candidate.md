# 受控变更候选版（W8）

状态：代码交付与本地演练候选版；**尚未通过真实环境及 7 天连续运行验收，不作为受控变更正式版发布**。

## 包含内容

- W1–W7 的巡检、审批、执行记录、快照恢复、通知、场景后验及稳定运行修复。
- K8s CrashLoop、Docker 重启循环、DNS/TLS、备份新鲜度与恢复演练 playbook。
- `opsx state backup/verify/restore`：停写后完整备份、逐文件 SHA-256 校验、恢复到新目录。
  原件保留供恢复，审批隔离，排期/通知停用，running 转为待核实或中断。
- 可重复的 W8 本地演练与 JSON 证据，覆盖审批拒绝、配置/权限恢复、命令成功但服务不健康、
  超时/未知状态、LLM 错误与预算、通知失败/补发/恢复。

## 本地复现

```bash
./opsx selftest
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/w8_acceptance.py --output /tmp/ops-agent-w8-evidence.json
```

本地演练的退出 0 仅表示 `local_status=passed`。报告同时保存 `overall_status=blocked` 和外部门禁；
要求完整发布验收时运行 `--require-external`，未满足时退出 2。
备份/恢复用法见 [操作文档](../ops/state-backup-recovery.md)。

2026-10-23 [W8 复审](../reviews/2026-10-23-week8-followup-review.md)后，原有自检 309/309、全部独立回归 168/168，
[最新本地证据](../validation/2026-10-23-w8-local-evidence.json)为 10/10，通过代码与策略摘要绑定保存。
恢复同时暂停长目标，演练拒绝缺失检查、源码变化和不安全输出；外部门禁仍未通过。

## 待通过的发布门禁

按 [W8 测试环境验收](../ops/w8-real-environment.md) 接入实际 LLM、专用 SSH 主机/服务和通知渠道，
完成恢复及断线演练；再填满 [7 天连续试运行记录](2026-10-22-readonly-trial.md)。
没有目标或凭据时保留未执行状态。不能用本地 HTTP 桩或加速循环代替这些验收。
