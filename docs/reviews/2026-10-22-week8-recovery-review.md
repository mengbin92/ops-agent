# W8 恢复准备、演练与候选版交付

日期：2026-10-22。范围按 [路线图](../2026-10-21-personal-ops-agent-roadmap.md) W8。
交付状态：**代码、本地恢复演练和候选版已完成；真实环境与连续观察验收尚未完成**。
本机没有注册 SSH target，未配置 LLM 凭据；本次没有联系外部通知接收人。

## 已交付

| 内容 | 入口与行为 |
| --- | --- |
| 故障 playbook | `skills/k8s-crashloop.md`、`docker-restart-loop.md`、`dns-tls.md`、`backup-recovery.md`；每份包含证据、只读诊断、审批、停止条件和重复验收 |
| 旧手册同步 | 服务重启与 nginx 排查去掉重复 ask_user 审批，明确回滚和后续 restart/reload 都须确认 |
| 离线状态备份 | `opsx state backup/verify/restore`；原始字节、manifest、归档与逐文件 SHA-256，拒绝链接、特殊文件、路径穿越及覆盖现有目录 |
| 安全恢复 | 恢复到新目录，审批隔离，running→unknown/interrupted，排期与通知停用；预算和目标原样保留；所有目录/文件为 0700/0600 |
| 读取保护 | 状态目录外的备份容器和未启用恢复副本也阻止 read/cat/递归内容读取；正式切换后普通报告可读，凭据和快照仍受保护 |
| 本地演练 | `scripts/w8_acceptance.py`：真实文件/权限、独立 HTTP 服务、子进程超时/信号、LLM/通知回环桩，结构化导出 0600 JSON 证据 |
| CI | 现有 macOS/Linux、Python 3.9/3.12 矩阵编译新增文件、跑全部 unittest 和本地演练；完整矩阵未在本机执行 |
| 候选版 | [候选版说明](../releases/2026-10-22-controlled-change-candidate.md)、[实际环境验收步骤](../ops/w8-real-environment.md)、[状态操作文档](../ops/state-backup-recovery.md) |

## 本次修复与交叉复核

- macOS 系统 `/var`、`/tmp`、`/etc` 别名曾使标准临时目录被拒绝；仅规范化固定系统别名，仍拒绝自建链接，并在规范路径上校验父子范围。
- 未切换 `OPSX_STATE_DIR` 前，恢复副本的原始通知凭据和快照会绕过旧读取限制；加入专用 `.ops-agent-recovered` 标记保护。切换后的报告读取兼容性也有回归。
- 演练 worker 启动 HTTP 子进程后异常退出会留下子进程；父层在所有退出路径回收继承进程组，并用真实异常退出 fixture 验证端口关闭。业务命令独立进程组仍受各自 timeout 管理。
- 演练 worker 禁止生成仓库字节码；业务文件、临时状态和服务进程清理后，只有显式指定的脱敏证据导出持久保留。
- 状态备份/恢复不暴露给 Agent 的工具或 hook 豁免；离线操作由停止所有写入者后的操作者执行，避免执行记录自身破坏停写前提。

## 验证

- `./opsx selftest`：309/309。
- `python3 -m unittest discover -s tests -p 'test_*.py' -v`：104/104，其中本次 W8 新增 44 项，前次复审回归 60 项。
- W8 本地演练：10/10；[JSON 证据](../validation/2026-10-22-w8-local-evidence.json)含 UTC、脚本摘要、配置前后哈希、权限、执行/快照 id、实际 HTTP 请求与响应状态。
- 语法编译与 `git diff --check` 通过。没有新增运行依赖。

本地演练退出 0 仅对应 `local_status=passed`，整体为 `overall_status=blocked`。
`--require-external` 对缺失的外部门禁退出 2，本地失败退出 1；这两种失败不能当作完整验收通过。

## 剩余发布门禁

真实测试 SSH 主机上的配置/权限恢复和断线、真实 provider、实际通知受众、真实服务后验及修复后连续 7 天观察仍需执行。
需要提供专用 target、允许写入的绝对目录、可中断测试服务，并在运行环境配置测试 LLM/通知渠道。
当前本地桩、加速循环或已有自检均不能替代这些证据；没有把 W8 标为整体完成，也没有发布受控变更正式版。

离线备份不是在线事务快照；`--quiesced` 是操作者的停写声明。归档哈希不是签名，原始状态备份含秘密且不自动加密。
恢复 Agent 状态不会自动恢复业务系统，文件快照也不能代替数据库/卷备份。7 天试运行从本次修复版本实际接入后重新计时。
