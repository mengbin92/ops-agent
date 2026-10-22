---
description: nginx 5xx 只读定位：日志、上游、资源和配置证据；审批后 reload 并独立验证
---

# nginx 5xx 排查

## 适用与证据清单

站点 5xx、502/504 或间歇错误。确认实际主机/注册 target、站点、时间窗口、
请求路径（不含 token/用户数据）、状态码比例、upstream 与部署方式，保留日志时间、
采样时间和证据引用。先取局部日志，配置/日志可能含秘密，禁止整份导出进模型。

## 只读命令

以下示例独立经 `bash` 守门，路径、服务名和容器名替换为已核实值。
仅 R0 可直接诊断；守门拒绝不表示可以换壳绕审批。

```bash
date -u
systemctl status nginx --no-pager
journalctl -u nginx -n 200 --no-pager
tail -n 200 /var/log/nginx/error.log
df -h
du -sh /var/log/nginx
systemctl status app --no-pager
journalctl -u app -n 100 --no-pager
docker ps --all
docker logs --tail 100 --timestamps app
nginx -t
```

按实际部署只跑适用命令。结合错误日志中的连接拒绝/超时、服务状态、资源水线
定位候选原因；单条错误不能证明根因。使用 `read` 定位必要的非敏感配置行，核对
proxy_pass、超时、证书路径和最近差异；不要读取私钥、Secret 或把 `nginx -T`
整份配置作为常规证据。需要 DNS/TLS 时加载 `dns-tls`。

## 变更与审批

改配置、重启/reload、清日志或扩容均先列完整变更单和回滚计划。优先用
`scenario_run` 的 `nginx-reload`，工具内会确认；其他变更由 `ops_approve` 确认后
交 `ops_exec`，文件变更带 `snapshot_file`，多文件须覆盖全部影响文件。
`ask_user` 仅澄清，不重复作为审批前置步骤；用户拒绝/无确认通道立即停止。
配置恢复调用 `ops_rollback`，它会单独展示差异并请求人工确认；恢复后的 reload
仍须新的审批。禁止用裸 `bash` 写配置、删日志、重启，或以 `&&` 合并变更与验证。

## 停止条件

目标不明确、日志被截断/权限不足、上游数据损坏、服务后验失败，或配置语法检查
失败时停止变更并报告未证实项。遇到 running/unknown、超时断线先用
`ops_executions` 核实，由人处理 `opsx executions resolve`，不自动重放或回滚。

## 可重复验收

事先记录同一站点/路径、请求率、正常状态码与可接受 5xx 阈值，约定观察窗
（建议至少 5 分钟，低流量时补经批准的无副作用业务请求）。固定相同请求条件，
比较变更前后及窗口末尾的访问日志/已有监控，确认 5xx 回到阈值内、upstream
稳定、无新增同类错误且 `nginx -t` 通过。仅 reload exit=0 不算恢复；缺业务采样
或日志解析失败记 unknown/unverified。保留 execution_id、快照 id 和后验证据。
