# Changelog

本文件记录 `ops-agent` 的版本变更，采用 Keep a Changelog 的分类与 Semantic Versioning 版本号。

## [Unreleased]

### Added

- 7 天只读观察部署：`obsServer` 固定 `cc941d78` 用户级 systemd 托管（每 3 小时，`sch-200a62`），2026-10-28 16:57 起计时；实机自检 321/321、规则巡检 24/24、真实模型调用通过；通知渠道未配置为已知未完成项。证据见 docs/validation/2026-10-28-observation-deploy.md。

- 写操作工具层默认"先预演后执行"：`kubectl diff` 与 `kubectl apply/create/replace/patch/set/scale/annotate/label --dry-run=server|client|true` 进入 R0 白名单，would-be 变更零摩擦预演；`KUBECTL_EXTERNAL_DIFF` 赋值失配锚定白名单、进程环境变量由 bash 工具执行前剔除，堵死借 diff 执行任意命令的绕过。
- 新只读工具 `logscan`（确定性日志粗筛）：错误级别/关键词收口 + 模式聚类去重 + 时间桶分布，只把可疑聚类的少量样本与行号/时间指针交给模型读语义；支持本机文件、journalctl、docker logs（远程目标仅后两者）。模型独占日志第一道成为过去式：子代理与无人值守定时上下文均可用，敏感路径同 read/bash 一致拒绝，样本统一脱敏，部分扫描如实标注，无命中明确提示"≠没问题"。
- 变更单结构化进审批戳：ops_approve（agent 工具 / MCP / CLI）新增必填 `impact`（影响面：将改什么状态、影响哪些服务）与 `snapshot_files`/`snapshot_cmds` 快照声明，确认框整段渲染；R1/R2 未声明快照时确认框醒目标注"无自动回滚点"。
- 快照声明与执行参数强一致：审批戳声明过快照计划后，core/remote `exec_change` 校验执行参数与声明完全一致，不一致拒绝执行；未带任何快照的变更执行后明确警告无自动回滚点。

### Fixed

- 修复 Docker inspect 格式串中的重定向样式文本、引号内 grep 管道符和正则尾部 `$` 被误判为 R2；共享 shell 分段与写标记检查保留引用和转义语义。
- 补齐 Docker 对象只读别名、固定 `xargs ... docker inspect` 与数字行号 `sed -n`；真实重定向、任意脚本及 Docker 写操作仍需审批。
- 拒绝 SSH 外层双引号中的本地命令/参数展开，以及 Compose config 的写文件选项；覆盖引用重定向目标和非 shell 空白的防绕过回归。

## [0.1.0-rc.1] - 2026-10-25

首个 **候选发布版本**：汇集独立运维 Agent、确定性巡检、调度通知、受控变更、快照回滚与离线状态恢复，
并包含本次全项目审查修复及 52 项新增回归。真实 SSH、LLM、通知渠道和连续 7 天观察仍未完成，
本版不作为受控变更正式版。详见 [发布说明](docs/releases/release-v0.1.0-rc.1.md)。

### Added

- 独立 Agent loop、OpenAI 兼容与 Anthropic provider、REPL/单发/RPC/MCP、流式输出、会话恢复、记忆、技能与只读子代理。
- 确定性巡检、工作流、定时任务运行记录、单渠道 webhook、事件时间线、体检与调用预算。
- 审批绑定执行环境、一次性授权、目标写锁、执行记录与未知结果防重放、本机/SSH 快照回滚、固定变更场景后验。
- 离线状态备份/校验/恢复、故障 playbook 与绑定源码摘要的本地恢复演练。
- 版本化发布说明、源码归档和 SHA-256 校验和；tag 发布复用 Ubuntu/macOS × Python 3.9/3.12 检查矩阵。

### Fixed

- 引号/转义、动态参数、glob/brace 和长选项缩写导致的只读守门绕过，以及 Git helper、less 初始命令/日志参数写入。
- Kubernetes Secret/raw API 与文件通配敏感读取绕过、本机回滚父目录软链接改向、非法快照环境元数据。
- 人工导入记忆及历史事件读取时的凭据泄漏。
- 歧义结论、旧代码块与 stderr 引发的巡检误判；规则采集、摘要或完整报告保存失败不再被模型正常回复覆盖。
- 调度重试超预算、doctor 漏检下划线目标、畸形会话/深层 JSON、RPC 非有限 ID、Anthropic 非对象工具参数及未回答交互。
- 单发 CLI 状态错误和中断 UTF-8 审计日志导致的 traceback。

### Changed

- 动态 shell 参数需审批；远程内容 glob、内容 brace 展开保守拒绝；本机内容 glob 复核实际匹配及软链接目标，上限 1024 项。
- 明确畸形会话拒绝恢复；规则证据采集/保存失败由程序将调度结论降为“不完整”。

[Unreleased]: https://github.com/mengbin92/ops-agent/compare/v0.1.0-rc.1...HEAD
[0.1.0-rc.1]: https://github.com/mengbin92/ops-agent/releases/tag/v0.1.0-rc.1
