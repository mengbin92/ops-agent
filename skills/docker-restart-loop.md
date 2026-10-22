---
description: Docker 重启循环：最小 inspect 字段、日志与计数对照；审批变更及健康验收
---

# Docker 重启循环排查

## 证据清单

确认注册 target/实际主机、Docker endpoint、容器全 ID、镜像 ID、Compose 项目/
服务、近期配置差异。记录 UTC 时间、State.Status、RestartCount、ExitCode、
OOMKilled、StartedAt/FinishedAt、restart policy、health 状态、资源水线及日志引用。
容器名可能被重建复用，两次采样必须比较完整 ID，不能把计数清零当作恢复。

禁止整份 `docker inspect`、`docker compose config` 或读取环境变量/私钥；其内容
可能含秘密。日志按窗口取样并脱敏，不运行 `docker exec` 作为默认只读诊断。

## 只读命令

以下以测试容器 `app` 为例，换成核实的容器名；每条独立经 `bash` 守门，远程用
注册 target。保留命令、退出码、采样时间和完整证据引用。

```bash
date -u
docker ps --all --no-trunc
docker inspect --format '{{.Id}} {{.Image}} {{.State.Status}} {{.RestartCount}} {{.State.ExitCode}} {{.State.OOMKilled}} {{.State.StartedAt}} {{.State.FinishedAt}}' app
docker inspect --format '{{.HostConfig.RestartPolicy.Name}} {{.HostConfig.RestartPolicy.MaximumRetryCount}}' app
docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' app
docker logs --tail 100 --timestamps app
docker stats --no-stream app
df -h
```

将退出码、OOMKilled、日志和资源观测交叉比较，区分应用退出、内存不足、依赖
不可用和 healthcheck 失败。`starting` 不能当 healthy；无 healthcheck 的 `none`
只代表没有内置健康检查，必须有业务后验。读取日志失败或字段不存在记 unknown。
restart policy 不能替代根因修复；不因循环重启就调整策略或无限重启。

## 变更与审批

先列目标/endpoint、容器 ID、完整命令、镜像/挂载/数据影响、快照或无文件快照
原因、回退方案和业务后验。仅当容器已稳定 running 且确需重启时，优先
`scenario_run` 的 `docker-restart`（它自带人工确认）。循环中的 restarting/exited
会被该场景预检拒绝，不能修改场景去跳过预检。其他修复走 `ops_approve` →
`ops_exec`；文件变更带 `snapshot_file`，卷/数据库另需应用一致性备份。
工具内确认即可，`ask_user` 仅作澄清；无确认通道或拒绝即停止。`ops_rollback`
恢复文件前另行确认；之后 restart/recreate 仍是需新审批的变更。停止/删除容器、
清理镜像/卷、改 restart policy、Compose up/down 均不能裸 `bash` 执行。

## 停止条件

endpoint/容器 ID 不明、共享卷或数据库风险不明、备份无法验证、磁盘满/宿主 OOM、
后验失败时停止扩大变更。running/unknown、超时断线先查 `ops_executions` 并只读
核实，由人处理 `opsx executions resolve`；不自动重放、回滚或重复重启。守门
拒绝时不得改白名单、加 shell 包装或换命令路径绕审批。

## 可重复验收

在隔离测试容器保存故障前后 ID、计数和日志，约定窗口（建议至少 5 分钟且覆盖
healthcheck 的 start_period/interval/retries）与业务成功标准。在窗首、窗中、
窗末重复采样，要求同一 ID 持续 running、RestartCount 不增加、无新增同类退出，
有 healthcheck 时持续 healthy，且无副作用业务探针通过。若重建容器，重新记录
ID 与基线并重计观察窗。命令 exit=0 或 running/none 不能单独验收；证据缺失记
unknown，缺业务后验记 unverified。保存 execution_id、快照 id 和报告引用。

参考：[Docker 自动重启](https://docs.docker.com/engine/containers/start-containers-automatically/)、
[Docker inspect](https://docs.docker.com/reference/cli/docker/inspect/)。
