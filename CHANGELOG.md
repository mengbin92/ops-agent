# Changelog

本文件记录 `ops-agent` 的版本变更，采用 Keep a Changelog 的分类与 Semantic Versioning 版本号。

## [Unreleased]

### Changed

- **7 天连续观察重计（R2）**：排期网格锚定属运行行为重大修复，按
  [下一阶段规划](docs/plans/2026-10-25-next-stage.md) §2 规则，2026-11-07 09:38（北京时间）
  在 `v0.1.0-rc.3` 上重启连续 7 天只读观察（`sch-35e3e0`，3h / 56 槽位，预期 11-14 09:38 满 168h）。
  R1（10-28 → 11-04）归档为历史轮次，记录与证据原样保留；新观察使用独立状态目录以避开证据轮转。
  详见 [R2 重启记录](docs/validation/2026-11-07-w8-observation-restart.md)。本轮仅文档与观察环境变更，
  不改产品代码、不升版本。

- **文档状态一次性回填（R2 后，2026-11-07）**：修正 rc.3 发布说明正文与自家补记自相矛盾、
  11-05 验收记录「W8 整体」判定仍把 R1 七天算作成立、通用验收模板与试运行记录的旧状态陈述；
  同步 R1 两条已移除的旧任务、以及 322/322 + 319 OK 的当前测试基线到规划与记录。
  rc.1 / rc.2 历史发布说明按发版纪律保留发布当时的状态，不改写；R1 原始逐日数据一律保留。

### Added

- **邮箱通知全链路演练（2026-11-09）**：在隔离状态目录用调低规则阈值的方式完成
  异常 / 去重 / 升级 / 恢复 / 失败 / 补发跳过 / 收尾 7 个场景，收件端确认 5 封投递邮件全部收到；
  未加压、未改运行版本、未污染 R2 观察数据。演练后以原子 `mv` 移入私有归档，原目录已移除。
  详见 [演练记录](docs/validation/2026-11-09-notify-drill.md)。**该演练不构成 R2 观察期内的送达证据**，
  S3 / S4 门禁状态不变。

### Fixed

- **2026-11-09 全项目审查 9 项修复（R01–R09）+ 两轮复审补修（F01–F05、V01）**。
  未升版本、未改发布门禁；修复后 `./opsx selftest` 322/322、
  `python3 -m unittest discover -s tests` 362 项通过（14 skipped）；
  首轮反例 9/9、复审反例 5/5、边界核验 2/2 通过。
  S3/S4 与 R2 观察结论不因此提前成立。

  - **R01 A2A 确认绑定变更单**：答复按「工具名 + 完整归一化参数 + 解析后的执行环境」
    匹配上一轮展示的请求；命令、目标、快照路径、次数或影响面变化一律重新进入
    INPUT_REQUIRED，不再出现"批准 A、模型改成 B 却生效"。确认框同时逐条展示快照
    文件与命令（原先只报数量）。
  - **R02 A2A `--read-only` 真正只读**：改用无人值守只读工具集
    （`tools.SCHEDULED_TOOLS`），schema 与 dispatch 同时受限；`ops_exec` 等写操作
    即使目标机已有合法审批戳也不可见、不可用，戳不会被消费。
  - **R03 模型传输禁止重定向**：OpenAI 兼容、Anthropic 与 `doctor` 探活统一不跟随
    3xx，避免 `Authorization` / `x-api-key` 被 Location 带到其它端点（含 HTTPS→HTTP）。
  - **R04 远程快照复查真实路径**：符号链接解析后再做一次敏感路径校验，凭据文件
    无法借普通文件名进入快照；输入路径校验保留。
  - **R05 远程读取失败不再成为空文件**：`cat` 失败立即 `exit`，不会被后续 `printf`
    洗成成功；回滚前副本读失败即中止，恢复命令不下发。
  - **R06 远程文件字节保真**：`_pull_file` 复用统一读取协议，登录 banner 不再污染
    快照；标记改为每次调用随机生成，正文含旧标记也不会被截断；支持 timeout 传递。
  - **R07 SMTP 部分拒收**：按收件人记账（散列标识，不存明文地址），部分失败记为
    failed 且只补发未成功者；不重复打扰已送达的人。
  - **R08 常驻调度心跳恢复**：`--once` 与常驻循环在扫描后复用健康检查，启动时建立的
    「心跳中断」告警在恢复后自动发出恢复通知。
  - **R09 本机日志采集合并 stderr**：`docker logs` 等容器错误日志不再被静默丢弃；
    非零退出统一按采集失败报错。

  同日复审补充修复（原 9 项反例已转绿，以下为残留缺口）：

  - **F01 确认身份使用完整执行环境**：`request_key` 与场景确认改用
    `core.env_identity(env)` 稳定序列化，纳入 `kubeconfig`、SSH identity/options；
    展示侧 `format_env` 同步显示这些字段，人才能核对批准的是哪个环境。
  - **F02 普通澄清不再保留旧审批身份**：`ask_user` 展示普通问题时清空待确认映射；
    对"继续只读诊断吗？"回答 yes 不会再批准此前被拒绝的写变更。
  - **F03 回滚预览前置校验**：`rollback_plan` 在读取前固定目标、解析真实路径、
    拒绝改向并按真实路径复查敏感路径；预览阶段即拒绝，秘密不进入 diff/工具输出。
  - **F04 部分送达接入恢复状态机**：状态项记录 `delivered_recipient_ids`，纯恢复
    消息按该集合定向投递（只发给收到过异常的人）；恢复后旧异常标记 superseded，
    不再补发，也不产生无来源的恢复消息。
  - **F05 心跳恢复意图不再过早清除**：移除"已告警"旗标，成功扫描后每轮交给通知
    状态机判重；首次心跳写盘失败时下一轮仍会补发恢复通知。
  - **V01 部分重试的新增收件人同步状态机**：`retry_pending` 在通知仍为 failed 但
    多送达了收件人时也更新 `delivered_recipient_ids`（只有全部成功才清恢复态并记
    恢复事件）；纯恢复消息的部分失败不并入该集合，避免把原异常受众反向缩小。

## [0.1.0-rc.3] - 2026-11-06

候选版：通知新增 SMTP 邮箱渠道（与 webhook 二选一，通知语义不变）+ W8 7 天验收结论回填。
真实渠道投递与 W8 通知全链路已于 2026-11-06 验收通过（见发布公告补记）；
真实 SSH 恢复、断线 unknown、服务后验仍未执行，且 7 天观察按规则在修复版本上重计（R2），
按发版纪律本版仍为候选版，不替代 latest。

### Added

- **邮箱通知（SMTP）**：`notify` 新增 `channel: email`，与既有 webhook 渠道二选一，去重/恢复/
  静默/失败重试语义完全一致。`opsagent notify setup --channel email --smtp-host … --from …
  --to a@x,b@y [--smtp-user …] [--smtp-password …]`；465 端口走隐式 TLS，其它端口要求
  STARTTLS（本机回环中继除外，便于本地验证），密码也可用 `OPSAGENT_SMTP_PASSWORD` 提供。
  收件人上限 20，地址/主机/端口在配置入口校验（拒绝空白、CR/LF、多 `@`、host 带端口，防邮件头注入）。
  事件与发件箱只记录 `smtp://host:port → N 个收件人`，SMTP 错误回显的密码/账号同样脱敏；
  `notify test`、`notify status` 与 `doctor` 均已支持邮箱渠道。

## [0.1.0-rc.2] - 2026-11-01

候选版：全项目审查（codex review）20 项发现的根因修复 + A2A 协议形态与 7 天观察沉淀。
真实环境连续 7 天只读观察进行中（day5），按发版纪律仍为候选版，不替代 latest。

### Added

- **A2A 协议形态（`opsagent a2a`）**：基于官方 `a2a-sdk` 1.3.0（协议 1.0，protobuf 类型 + `SendMessage` 方法名）把同一组受控能力以 AgentCard + JSON-RPC 暴露，并可反向调用其它 A2A agent。
  - `opsagent a2a serve|card|call|doctor`；skill 列表直接从 `skills/` 目录生成 AgentCard，能力声明与可加载 playbook 一致。
  - **只读直通 / 写操作二次确认**：R0 命令沿用既有守门直接执行；R1–R3 转 `TASK_STATE_INPUT_REQUIRED` 并带出整张变更单，调用方带同一 `task_id` 回复「确认执行」才建戳执行。一次答复只批准一个待确认项，拒绝路径不产生审批戳与执行记录。
  - **依赖可选**：SDK 懒加载，`requirements-a2a.txt` 独立管理；REPL/单发/RPC/MCP/巡检/调度/通知等形态仍零第三方依赖，缺依赖时给安装命令并退出 2（要求 Python ≥ 3.10）。
  - **安全默认**：只绑 `127.0.0.1`；非回环监听未配 `OPSAGENT_A2A_TOKEN` 直接拒绝启动；RPC 端点强制 Bearer 校验（定长比较），AgentCard 公开以便客户端先发现能力。
  - A2A 的 `context_id` 映射为 `a2a-<context_id>` 会话，落在同一 `sessions/` 目录，可 `/resume` 回看；同一 context 串行执行，全局并发默认 2。
  - 新增 20 项回归（`tests/test_a2a.py` 11 项 + `tests/test_a2a_e2e.py` 7 项，后者真实 uvicorn 服务端 + 官方客户端 + 桩 LLM，未装 SDK 自动跳过）。

- 7 天只读观察部署：`obsServer` 固定 `cc941d78` 用户级 systemd 托管（每 3 小时，`sch-200a62`），2026-10-28 16:57 起计时；实机自检 321/321、规则巡检 24/24、真实模型调用通过；通知渠道未配置为已知未完成项。证据见 docs/validation/2026-10-28-observation-deploy.md。

- 写操作工具层默认"先预演后执行"：`kubectl diff` 与 `kubectl apply/create/replace/patch/set/scale/annotate/label --dry-run=server|client|true` 进入 R0 白名单，would-be 变更零摩擦预演；`KUBECTL_EXTERNAL_DIFF` 赋值失配锚定白名单、进程环境变量由 bash 工具执行前剔除，堵死借 diff 执行任意命令的绕过。
- 新只读工具 `logscan`（确定性日志粗筛）：错误级别/关键词收口 + 模式聚类去重 + 时间桶分布，只把可疑聚类的少量样本与行号/时间指针交给模型读语义；支持本机文件、journalctl、docker logs（远程目标仅后两者）。模型独占日志第一道成为过去式：子代理与无人值守定时上下文均可用，敏感路径同 read/bash 一致拒绝，样本统一脱敏，部分扫描如实标注，无命中明确提示"≠没问题"。
- 变更单结构化进审批戳：ops_approve（agent 工具 / MCP / CLI）新增必填 `impact`（影响面：将改什么状态、影响哪些服务）与 `snapshot_files`/`snapshot_cmds` 快照声明，确认框整段渲染；R1/R2 未声明快照时确认框醒目标注"无自动回滚点"。
- 快照声明与执行参数强一致：审批戳声明过快照计划后，core/remote `exec_change` 校验执行参数与声明完全一致，不一致拒绝执行；未带任何快照的变更执行后明确警告无自动回滚点。

### Fixed

- **全项目审查修复（2026-11-01，20 项 + 若干小项）**：
  - **守门**：敏感文件绕过快照通道封堵——`core._snapshot_file_into` / `remote._pull_file` 拒绝敏感路径（与 `cat` 同一套判定），MCP `ops_snapshot_file`、`opsx snapshot --file`、`ops_exec` 快照声明与远程拉取四个入口一并收紧；凭据不再经快照落盘/进 `state.tar`/回流回滚预览。
  - **守门**：`core.exec_change` 与 `remote.exec_change` 合并为单一骨架（远程差异点经 `_ExecTransport` 注入），消除 110 行双拷贝漂移；远程执行记录补齐 `host` 字段；快照拉取/抓取接受执行超时（大文件慢链路不再被 IO_TIMEOUT=120s 一刀切）。
  - **远程**：`_remote_read` 改尾标记协议（与 `_target_resolved_path` 同思路），rc/MOTD 噪声不再让远程回滚预览/回滚前副本全部失败；`opsx rollback` 对 `_UncertainChange` 返回 120（与 exec/scenario rollback 契约一致，不再被自动化当"失败"重跑）；ssh `options` 缺 `-` 前缀时在发 SSH 前拒绝（不再被当目标主机解析）。
  - **agent 工具**：`ops_exec` 支持 `snapshot_files` 数组并与 `ops_approve` 共用同一份参数归一化（多文件变更单不再"批准后必被拒"）；bash 子进程环境剔除 `OPSAGENT_API_KEY`/`OPSAGENT_A2A_TOKEN`（R0 `env`/`echo` 不再回显凭据），dispatch 额外按值清洗；`ops_executions`/schedule `tail` 收口 1-200（MCP 同步 1-200）。
  - **会话**：修剪跳过 24h 内有活动的会话文件（子代理/工作流高频建会话不再误删活跃空闲会话）；`list_sessions` 改纯文本流式读取（列表操作不再写文件/全量解析）。
  - **调度**：every 任务改槽位网格锚定、finish 不再重锚（漂移从"无上界累积"变为常数 ≤60s，day5 实测 20min 漂移的根因）；daemon 启动对账与逐条记录写盘容忍 OSError（磁盘满不再裸崩）；睡眠下限 30s→1s（到期任务不再恒定晚 30s）；删除 `--once` 冗余通知调用。
  - **场景**：占位符在 load 时全量校验 + postcheck 执行前兜底替换（后验拼写错误不再"变更已执行、验证没跑"）；保留字参数名 `if/else/end` 拒绝（不再打碎 docker `{{end}}` 模板）。
  - **通知/巡检**：checks 状态→severity 单一映射（未知状态不再静默通知为最高级别"不可采集"）；`notify.process_run` 抽出 `_apply_delivery`；logscan 聚类组数上限 5000（全唯一行输入不再无界占内存，截断如实标注）。
  - **MCP**：`ops_audit` 断行/半行容忍（崩溃瞬间的半条审计不再让查询变砖，坏行如实标注）。
  - **usage**：账本轮转移入状态锁内（daemon+CLI 并发轮转不再提前清掉归档代），补轮转测试。
  - **A2A**：`opsagent a2a serve --read-only`（写操作在无确认通道时自动降级为拒绝）；补"一次答复只批准一个变更"不变量测试（变异验证）。
  - **内核小项**：rpc SIGINT 干净退出；`/resume` 损坏会话不再崩 REPL；流式渲染去重复 render；compact 不再把系统消息计入保留窗口；Anthropic 默认模型名单一来源；跨目标同 host:port 防重放补回归。
  - **CI**：新增独立 A2A job（装 `requirements-a2a.txt` 跑单测+e2e，此前全部静默跳过）；W8 演练不再每腿双跑；删除无调用死代码（`usage.ensure_budget`、`scenario._compact`、`remote._target_owner`/`_parse_restore`、doctor 未用 import）。

- **A2A 首轮交付的 code review 修复**（11 项）：
  - `requirements-a2a.txt` 版本冲突：`sse-starlette 3.6.0` 声明 `starlette>=0.49.1`，原锁的 `0.41.3` 会导致 `ResolutionImpossible`（或在已有环境被 pip 破坏性降级成"能跑但元数据不成立"）。改为 `starlette==0.49.1` + `uvicorn==0.34.0`，并实际安装 + 跑通全量用例验证。
  - 确认中断后的会话修补（settle）从锁外移入会话锁内，避免同 context 并发任务重复补写悬空 tool_call。
  - 复用终态 task 的请求显式拒绝（SDK 已在 handler 层拦截，executor 内保留纵深防御，不再可能被 `start_work` 重新激活）。
  - 执行槽获取加 30s 上限并明确报「服务繁忙」，不再让调用方无限挂在 WORKING。
  - 入口限制单条消息 20,000 字符，超长粘贴引导走 logscan/分段。
  - IPv6 监听时 AgentCard 里的 URL 补方括号，否则客户端无法解析。
  - 客户端不再依赖签名随版本变动的 `get_message_text`，直接遍历 `Part.text`。
  - `a2a_client.send()` 在已有事件循环内调用时改走新线程，避免 `asyncio.run` 嵌套崩溃。
  - CLI 异常分支引用了仅在 call 分支导入的模块（潜在 `NameError`），改为本地常量。
  - 缺 uvicorn / 端口被占用时给可执行提示，不再抛出裸 ImportError 或 uvicorn 栈。
  - 常驻会话表加上限（200）并做 LRU 淘汰：`Session` 缓存全部 messages，不设限等于长期运行的内存泄漏；正在执行（持锁）的会话不淘汰，避免同一 sid 出现两把锁。

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

[Unreleased]: https://github.com/mengbin92/ops-agent/compare/v0.1.0-rc.3...HEAD
[0.1.0-rc.3]: https://github.com/mengbin92/ops-agent/compare/v0.1.0-rc.2...v0.1.0-rc.3
[0.1.0-rc.2]: https://github.com/mengbin92/ops-agent/compare/v0.1.0-rc.1...v0.1.0-rc.2
[0.1.0-rc.1]: https://github.com/mengbin92/ops-agent/releases/tag/v0.1.0-rc.1
