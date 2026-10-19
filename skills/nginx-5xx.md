---
description: nginx 返回 5xx 时的只读排查顺序（日志 → 上游 → 资源 → 配置）
---

# nginx 5xx 排查

## 适用
用户报告站点 5xx、502/504、间歇性错误页。

## 排查顺序（全部只读，bash 直接执行）

1. 服务与进程状态：
   `systemctl status nginx`、`ps aux`、`docker ps`（容器化部署时）。
2. 错误日志与上游错误（先看最近的）：
   `tail -n 200 /var/log/nginx/error.log`、
   `journalctl -u nginx -n 200`、
   容器：`docker logs --tail 200 <容器名>`。
3. 上游是否存活/超时（502/504 常见原因）：
   从 error.log 确认 upstream 地址与错误类型（connect refused / timeout / reset）。
   若上游也是本机服务：`systemctl status <upstream>`、`docker ps`、`journalctl -u <upstream> -n 100`。
4. 资源与水线：
   `df -h`（磁盘满会导致写日志/临时文件失败）、`du -sh /var/log/nginx`、
   `free` 或 `vm_stat`（内存压力）。
5. 配置核对（只读）：
   `tail -n 200 /etc/nginx/nginx.conf`、`cat /etc/nginx/conf.d/<站点>.conf`（路径按实际）。
   重点看 `proxy_pass`、超时时间、`client_max_body_size`、证书路径。
6. 结论：给出「最可能原因 + 证据 + 下一步建议」。下一步若需要写操作（重启、改配置、
   清日志、扩容），先出变更单并走审批流程，不要直接改。

## 注意
- 只读命令不受审批限制；任何 `systemctl restart/reload`、改配置、删文件都是写操作，
  必须：变更单 → ask_user 确认 → ops_approve → ops_exec（带快照）。
- 远程主机：命令带 `target`，只允许注册过的目标。
