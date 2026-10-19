---
description: 磁盘/分区写满时的只读定位顺序（谁占的、能不能清、先保什么）
---

# 磁盘满排查

## 适用
`df` 显示使用率接近 100%、写入报 "No space left on device"、服务日志报错。

## 排查顺序（全部只读）

1. 哪个挂载点满：`df -h`（看 Use% 与挂载点，区分系统盘/数据盘/日志盘）。
2. 顶层目录占用：`du -sh /*`（可能较慢；先看 `/var`、`/tmp`、`/home`、`/data`）。
3. 继续下钻：`du -sh /var/log/*`、`du -sh /var/lib/docker/*`（容器环境）、
   `ls -lh /var/log` 找异常大的日志。
4. 已删除但仍被进程占用的文件（空间没释放的典型）：
   `ls -l /proc/*/fd 2>/dev/null | grep deleted`（Linux）或 `lsof +L1`（若可用）。
5. 日志轮转/保留策略：`cat /etc/logrotate.conf`、`cat /etc/logrotate.d/<服务>`；
   容器：`docker ps`、`docker system df`（只读统计）。
6. 结论：给出占用最大的前几项、可安全释放的空间（日志轮转、临时文件、旧镜像），
   以及必须保留的数据。

## 注意
- `rm`、`find -delete`、`docker system prune`、清空日志、扩容都是写/破坏性操作：
  先出变更单（含删什么、为什么安全、不可逆说明），确认后 `ops_exec` 执行。
- R3 不可逆操作需要用户会话内显式确认并 `force=true`；能先快照/备份的就先快照。
- 禁止用 `> /var/log/x.log` 这种重定向清日志——在 `ops_exec` 里也要写明并走审批。
