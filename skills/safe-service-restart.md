---
description: 安全重启服务的变更流程（快照 → 审批 → 执行 → 验证 → 回滚预案）
---

# 安全重启服务

## 适用
改配置后需要 `systemctl restart/reload`、`docker restart` 等 R1 操作；
或服务异常需要重启恢复。

## 流程

1. **摸清现状（只读）**：`systemctl status <svc>`、`journalctl -u <svc> -n 50`、
   `ps aux`；确认服务名、启动方式（systemd / docker / supervisor）、依赖。
2. **快照**：配置文件是影响面最大的部分，用 `snapshot_file` 记录：
   `/etc/<svc>/...`（systemd unit 变体也一并拍）。
   unit 文件路径用 `systemctl show -p FragmentPath <svc>` 确认（只读）。
3. **变更单**：目标（本机/远程 target）、完整命令、风险（重启期间不可用窗口）、
   回滚计划（恢复配置 + 再重启；快照 id）、验证命令。
4. **确认与审批**：ask_user 让用户确认 → `ops_approve`（R3 才需 `force=true`）。
5. **执行**：`ops_exec`（带 `snapshot_file`）执行重启；输出与 exit code 一并汇报。
6. **验证（只读）**：`systemctl status <svc>`、`journalctl -u <svc> -n 100`、
   端口/进程检查；确认业务恢复。
7. **失败则回滚**：`ops_rollback <snapshot_id>` 恢复配置，再重启；把失败证据一并报告。

## 禁止
- 不拍快照就改配置；`reload` 失败的场景也按重启处理（保留回滚路径）。
- 用 `&&` 把重启和验证/清理串成一条命令——验证必须能看到独立的结果。
- 远程目标：必须用注册过的 target，`ops_approve`/`ops_exec` 的 target 要一致。
