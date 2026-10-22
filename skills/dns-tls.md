---
description: DNS/TLS 分层排查：解析状态、SNI/主机名/证书链、有效期与审批边界
---

# DNS/TLS 排查

## 证据清单

确认实际客户端/注册 target、目标域名和端口、预期 DNS resolver、分区解析范围、
预期记录/TTL、证书主机名、信任链与维护窗口。记录 UTC 时间、时钟偏差、DNS
响应状态/答案/resolver、握手退出码、验证错误和证书有效期。证据不含 URL token、
cookie、Authorization、私钥或证书仓库口令；只读取确认不含私钥的公开证书文件。

## 只读命令与守门

用已核实的固定值替换示例 `app.example.test` 与测试 resolver `192.0.2.53`。
不把内部域名送往未授权的公共 DNS。下面 R0 命令独立通过 `bash` 守门：

```bash
date -u
cat /etc/resolv.conf
which timeout
```

以下是无配置写入的诊断命令，但默认 `dig`/`openssl` **不在 R0 白名单**。每一条
先提交完整命令由 `ops_approve` 工具内请求人工确认，再通过 `ops_exec` 执行，
明确无文件快照原因（仅查询 DNS、读取公开证书或建立诊断连接）。没有确认通道/
被拒绝即停止，不能直接交裸 `bash`、换壳或扩大白名单绕审批。分别保留
stdout/stderr、退出码；TLS 查询使用已安装的 GNU `timeout` 限时，完整包装命令
也需审批。未安装时停止该探针，由人选已有受限诊断方式，不自行安装工具。

```bash
dig @192.0.2.53 app.example.test A +time=3 +tries=1
dig @192.0.2.53 app.example.test AAAA +time=3 +tries=1
timeout --kill-after=2s 20s openssl s_client -connect app.example.test:443 -servername app.example.test -verify_hostname app.example.test -verify_return_error -brief </dev/null
openssl x509 -in /etc/nginx/tls/app.crt -noout -dates -issuer -subject -ext subjectAltName
openssl x509 -in /etc/nginx/tls/app.crt -noout -checkend 604800
```

分别解释：DNS 超时/SERVFAIL 为解析失败或不确定，NXDOMAIN 是不存在的响应，
NOERROR 无答案需结合预期 RR 类型判断，不能把任何 exit=0 当作正确解析。
TLS 必须保留 SNI、主机名与信任链校验；`-verify_return_error` 避免握手忽略验证
错误。`-checkend 604800` 只检公开文件证书是否在 7 天内过期，不证明服务在线、
链可信或远端正提供同一张证书；其失败还需区分过期、文件/解析错误。自建 CA 时
由人确认可信 CAfile 来源，再把具体路径纳入新审批，不能关闭校验来宣称恢复。

## 变更与审批

DNS 记录、resolver、hosts、证书部署、权限、系统时钟与服务 reload 都是新变更。
先列影响域、TTL/缓存窗口、证书/配置备份和回退方案，`ops_approve` → `ops_exec`；
已有 nginx 场景可用 `scenario_run`，它自带确认。文件变更带 `snapshot_file`，
不把私钥读进模型。恢复配置调用 `ops_rollback`，它另行请求人工确认；后续 reload
需新的审批。`ask_user` 仅澄清，不代替具体命令审批。

## 停止条件

resolver/域名范围不明、发现秘密、证书含私钥、信任来源未知、时钟明显不准、
DNS/TLS 结果互相矛盾时停止并报告缺口。running/unknown、超时断线先用
`ops_executions` 核实，由人处理 `opsx executions resolve`；不自动重放或回滚。
禁止用跳过验证（如 curl -k）结果作为健康证据；不自行安装诊断工具。

## 可重复验收

事先记录正确答案集合、resolver、TTL/缓存窗口、域名/端口、可信 CA、到期阈值及
无副作用业务请求。固定同一客户端条件，在变更前、变更后、TTL/缓存窗口结束
重复采样，要求 DNS 响应与预期匹配、TLS 主机名和链验证成功、远端证书满足有效期
阈值、业务请求通过。多视图 DNS 逐个授权客户端验收，不能一次本机成功就宣称
全部恢复。缺工具/权限/超时记 unknown；只有文件证书通过记 unverified。

参考：[ISC BIND dig 手册](https://bind9.readthedocs.io/en/stable/manpages.html)、
[OpenSSL s_client](https://docs.openssl.org/3.0/man1/openssl-s_client/)、
[OpenSSL x509](https://docs.openssl.org/3.0/man1/openssl-x509/)、
[GNU timeout](https://www.gnu.org/software/coreutils/manual/html_node/timeout-invocation.html)。
