---
description: K8s CrashLoopBackOff 只读证据、停止条件和审批后可重复恢复验收
---

# K8s CrashLoopBackOff 排查

## 证据清单

先确认实际集群 context、namespace、Pod UID、容器名（含 init container）、
所属控制器、镜像版本与近期发布。记录 UTC 采样时间、重启计数、waiting/last
termination reason、exit code、OOMKilled、readiness、事件及前一实例日志引用。
CrashLoopBackOff 是现象，不能仅凭名字认定 OOM 或探针配置错误。

不读取 Secret 值、kubeconfig 原文、私钥，不整份输出 Pod/Deployment YAML 或
describe 中的环境变量。应用日志也可能含凭据：只取必要窗口，先脱敏；未检出秘密
不等于没有秘密。命令失败、无前一实例日志或事件已过期均明确记录为证据缺口。

## 只读命令

以下使用占位测试对象 `test` context、`ops` namespace、`app-abc` Pod、`app`
容器。先替换为已核实的固定值；每条独立经 `bash` 守门，远程使用注册 target。
动词置前、每条显式写 context/namespace，避免切换当前 context 后误查。

```bash
date -u
kubectl get pod app-abc --context=test --namespace=ops -o wide
kubectl get pod app-abc --context=test --namespace=ops -o 'jsonpath={.metadata.uid}{"\t"}{.metadata.ownerReferences[*].kind}{"\t"}{.metadata.ownerReferences[*].name}{"\n"}'
kubectl get pod app-abc --context=test --namespace=ops -o 'jsonpath={range .status.containerStatuses[*]}{.name}{"\t"}{.restartCount}{"\t"}{.ready}{"\t"}{.state.waiting.reason}{"\t"}{.lastState.terminated.reason}{"\t"}{.lastState.terminated.exitCode}{"\n"}{end}'
kubectl get pod app-abc --context=test --namespace=ops -o 'jsonpath={range .status.initContainerStatuses[*]}{.name}{"\t"}{.restartCount}{"\t"}{.state.waiting.reason}{"\t"}{.lastState.terminated.reason}{"\n"}{end}'
kubectl get events --context=test --namespace=ops --field-selector=involvedObject.name=app-abc --sort-by=.metadata.creationTimestamp
kubectl logs app-abc --context=test --namespace=ops -c app --previous --tail=100 --timestamps --request-timeout=20s
kubectl logs app-abc --context=test --namespace=ops -c app --tail=100 --timestamps --request-timeout=20s
kubectl get deployment app --context=test --namespace=ops -o 'jsonpath={.metadata.generation}{"\t"}{.status.observedGeneration}{"\t"}{.status.replicas}{"\t"}{.status.readyReplicas}{"\n"}'
```

最后一条仅适用 Deployment，其他控制器按实际类型取状态。事件按 Pod 名查询后
还需与 UID、时间匹配，不能把旧同名 Pod 的事件归给新实例。将退出原因、日志、
探针失败事件和节点/依赖证据交叉比较，给出证据支持的候选原因及待核实项。

## 变更与审批

删除 Pod、rollout/restart/undo、扩缩容、改资源/探针/配置及 debug/exec 均不属于
本 playbook 的只读步骤。先列目标、完整命令、context/namespace、影响、配置/数据
备份与业务后验，再 `ops_approve` → `ops_exec`；工具内请求人工确认，不再额外
调用 `ask_user`。所有 kubectl 变更须在命令中显式写固定 `--context` 和 namespace；
文件快照不能恢复集群对象或卷，必须另列可执行的部署/数据回退方案。
`ops_rollback` 只恢复已拍文件，工具内再次确认，不能当作 Kubernetes rollout undo。

## 停止条件

context/namespace/UID 不明、无权限、日志含未能安全处理的秘密、存储/数据损坏、
配额或节点级故障超出授权范围时停止；不要自动删 Pod 或去掉健康探针掩盖故障。
running/unknown、超时断线先通过 `ops_executions` 只读核实，由人处理
`opsx executions resolve`；不自动重放。用户拒绝、无确认通道、守门拒绝即停止
变更；禁止裸 `bash`、替换二进制路径或改白名单绕审批。

## 可重复验收

在隔离测试 namespace 用已批准的故障样例保留基线，约定观察窗（至少 5 分钟且
覆盖启动、探针和重启退避周期）、期望副本数及无副作用业务探针。恢复后在窗首、
窗中、窗末重复同一组只读采样：新 Pod 记录新 UID，重启计数在同一 UID 内不增加，
所有预期容器 ready、控制器 observedGeneration 跟上 generation、就绪副本达到
预期、无新同类事件且业务探针通过才验收。一次 Running、短时间计数未变或命令
exit=0 均不够；采样失败记 unknown，未验证业务记 unverified。

参考：[Kubernetes 官方 Pod 调试](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/)、
[kubectl logs](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_logs/)。
