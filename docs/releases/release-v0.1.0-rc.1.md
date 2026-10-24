# ops-agent v0.1.0-rc.1 发布：首个候选版与安全可靠性审查修复

> 2026-10-25 · 上一版：无（首次版本化发布）· [GitHub Release](https://github.com/mengbin92/ops-agent/releases/tag/v0.1.0-rc.1)

v0.1.0-rc.1 是首个 **候选发布版本（prerelease）**，包含独立 Agent、确定性巡检、调度通知、
受控变更与离线恢复能力，以及本轮全项目审查修复。运行时使用 Python 3.9+ 标准库，支持 macOS/Linux。

真实 SSH、LLM provider、通知渠道及修复后连续 7 天观察尚未完成；本版供测试环境验证，
**不宣称已通过受控变更正式版验收**。本地演练的 `local_status=passed` 与 `overall_status=blocked`
同时保留，详见 [W8 验收](../ops/w8-real-environment.md)。

## 发布内容

### 1. 独立 Agent 与受控运维能力

- REPL、单发、stdio RPC 与 MCP 共用工具内核；支持 OpenAI 兼容和 Anthropic 原生协议、流式输出与会话恢复。
- 技能、只读子代理、工作流、长期记忆与 REPL 长目标；无人值守任务保持只读。
- 三组确定性巡检、定时运行记录、通知去重/升级/恢复、事件时间线、doctor 与调用预算。
- 审批绑定命令与实际执行环境，一次性授权、目标写锁、快照/确认回滚、未知执行结果防重放及场景后验。
- 离线状态备份恢复：保留快照原始字节、隔离旧审批、暂停排期/通知/活动目标，将在途记录转为待核实或中断。

### 2. 只读守门与敏感读取修复

**根因**：原始字符串检查不能完整反映 shell 解码和展开后的选项；部分读取命令可指定可执行 helper，
文件通配与动态 Kubernetes 资源名也可能绕过敏感路径/Secret 输出检查。

**修复**：按解析后的参数检查引号/转义与长选项缩写；拦截危险动态展开、Git helper、less 写入参数；
检查本机 glob 的实际匹配项及软链接目标，远程内容 glob 和内容 brace 展开保守拒绝；
Kubernetes raw API、动态资源及 Secret 值输出统一受敏感门禁保护，审批不能覆盖。

**影响入口**：`opsx check/exec/snapshot`、hook、Agent bash 与 MCP 共享守门。

### 3. 回滚与秘密出口修复

**根因**：快照创建后的父目录软链接改向可能将本机回滚发送到另一目录；人工编辑的记忆和旧事件没有经过写侧脱敏。

**修复**：本机预览、回滚前副本与写入复核父目录真实路径；校验快照环境元数据。
记忆在截尾前全文脱敏，历史事件读取时再次脱敏，原始证据保留。

**影响入口**：本机快照回滚、模型 system prompt、事件查询与常规展示。

### 4. 巡检判定与调度证据修复

**根因**：歧义结论、旧 Markdown 代码块、stderr 或模型最终回复可能让不完整采集/证据保存失败被报告为正常；
固定重试等待可能越过整任务截止时间，doctor 还会漏掉合法的下划线目标名。

**修复**：结论整行匹配并隔离引用代码，平台只认 stdout；规则采集、摘要或完整报告保存失败通过内部状态
传到调度，程序降为“不完整”（退出 3），不依赖模型复述。重试受剩余时间约束，doctor 仅排除 `_local`。

**影响入口**：`inspect`、`workflow`、`schedule`、`doctor`。

### 5. 会话、provider、RPC 与 CLI 修复

**根因**：畸形调用结构、深层 JSON、非有限 RPC ID、Anthropic 历史非对象参数、未回答交互和损坏日志
可让会话或 CLI 中断，并影响后续对话。

**修复**：校验并规范化消息协议，深层数据正常处理或受控拒绝；无效 ID 返回 `id=null` 的 Invalid Request；
Anthropic 以合法对象回放错误参数；未回答明确视为拒绝；CLI 和审计坏字节采用受控错误/只读替换展示。

**影响入口**：Agent loop、会话恢复、OpenAI/Anthropic provider、RPC、`opsagent -p` 与 `opsx audit`。

## 兼容性说明

- 无第三方 Python 运行依赖；SSH、systemd、Docker、kubectl 等仅对应功能需要。
- 包含 `$HOME`、`${...}` 等动态 shell 参数的命令需审批；可改用确认过的绝对路径。
- 远程内容读取的未引用 glob、内容读取的 brace 文件名展开被拒绝；先 `ls` 再使用明确路径。
  本机普通日志 glob、`ls/find` 路径通配和引用的 grep 正则仍可用；本机内容匹配上限 1024 项。
- 已知敏感凭据/Secret 读取不能通过批准放行；普通 Secret 表格与 `-o name` 保留。
- 明确畸形的历史消息拒绝恢复；有效中断调用会补“先核实、勿重试”结果，不自动重放写操作。
- 文件状态目录布局不变，升级不会主动改写现有状态；旧环境不匹配的审批需重新批准。
- 守门与秘密识别属于保守的静态/启发式规则，不构成抵抗恶意 Agent 或同 UID 对手的完整沙箱。

## 安装与升级步骤

首次安装：

```sh
git clone --branch v0.1.0-rc.1 https://github.com/mengbin92/ops-agent.git
cd ops-agent
python3 --version  # Python 3.9+，macOS/Linux
./opsx selftest
python3 -m unittest discover -s tests -p 'test_*.py' -v
./opsagent inspect --target local
```

首次尚未配置 `services` 时，巡检会因业务覆盖不足返回“不完整”（退出 3）；这不表示安装失败，
应按 README 配置需要验证的服务后再判定完整健康。

也可从 GitHub Release 下载 `ops-agent-v0.1.0-rc.1.tar.gz` 与 `SHA256SUMS`，在同一目录校验后解包：

```sh
# Linux：sha256sum -c SHA256SUMS
# macOS：shasum -a 256 -c SHA256SUMS
tar -xzf ops-agent-v0.1.0-rc.1.tar.gz
cd ops-agent-v0.1.0-rc.1
./opsx selftest
```

已有源码安装升级：先结束在途变更，停止所有调度、Agent、RPC/MCP 与其他状态写入者。
按 [状态恢复操作文档](../ops/state-backup-recovery.md)备份当前 `OPSX_STATE_DIR`，
`--quiesced` 必须建立在人工停写确认之后；它不是在线事务锁。

```sh
# 先按上述要求停写，并准备由自己控制的私有备份父目录。
./opsx state backup /secure/new-v0.1.0-rc.1-backup --quiesced
./opsx state verify /secure/new-v0.1.0-rc.1-backup

git status --short  # 先保留自己的未提交改动
git fetch origin --tags
git switch --detach v0.1.0-rc.1
./opsx selftest
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

核对相同的状态目录、SSH 端点、模型配置与托管入口后再启动测试环境；不要同时更改目标别名或 SSH 配置。
源码降级与业务回滚分别处理：本轮包含守门修复，不建议恢复旧守门；状态恢复必须进入新目录，
业务文件回滚仍需确认及服务后验。LLM 凭据和通知渠道由操作者另行配置，本发布不发送外部测试消息。

## 验证

- macOS Python 3.9.6、3.12.13：分别 **309/309** 内置自检、**223/223** 独立回归通过；本轮新增 **52** 项。
- 两个解释器的语法编译与 `git diff --check` 通过。
- [本地恢复演练](../validation/2026-10-24-w8-local-evidence.json) **10/10**，24 项代码/策略摘要与发布实现一致。
- 发布流水线在 tag 对应提交上重跑 Ubuntu/macOS × Python 3.9/3.12 矩阵，全部成功后才创建 Release 和源码制品。
- 实际 workflow 结果见 GitHub Actions；真实 SSH/LLM/通知与连续 7 天观察仍是未完成的外部验收。

## 完整变更日志

首个版本包含此前全部功能/修复提交及本次发布基础设施提交，以下使用实际 Conventional Commit 标题：

- docs: 运维 Agent 设计文档（权限分级、审批戳、快照回滚、审计）
- docs: 实现计划（8 个任务，TDD 步骤含完整代码）
- feat: 配置加载与命令风险分类（R0-R3）
- feat: JSONL 审计日志
- feat: 审批戳（哈希+TTL+force）
- feat: check() 白名单/审批戳判定与拒绝指引
- feat: 快照与回滚（文件+状态捕获，保留200个）
- feat: exec 原子执行（校验→快照→执行→审计）
- feat: opsx CLI（含 Claude Code hook 入口）
- feat: ops agent 定义、hook 配置与使用文档
- fix: 终审修复——R1/R2 补审批步骤、rm 组合flag与systemctl分级归位、白名单写标记防护、hook空命令拒止
- fix: 关闭终审遗留缺口——rm 长选项变体归 R3、审计日志单备份轮转，补 2 项自检
- feat: 白名单补 git 只读命令（status/log/diff/show 等）与 vm_stat，写形态仍落 R2 需审批
- docs: ops-guard MCP server 设计文档（spec §10 演进升级为正式规格）
- docs: ops-guard MCP server 实现计划（3 个任务，代码完整给出）
- feat: ops-guard MCP server 骨架——协议循环与握手/帧错误处理
- feat: MCP 工具面——tools/list 与 8 个 tools/call 分发
- docs: README 补 MCP server 注册与确认流说明
- docs: 修复已知差异——规格行数/ttl单位/auto-stamp表述归位，README与MCP规格路径占位符化
- docs: 新增《从零创建一个带权限闸门的 Claude Code Agent》教程与封面图
- feat: ops-agent 独立化——自带 agent 内核，支持 REPL/单发/RPC 三种形态
- feat: M1 流式/compaction/resume + REPL 语义配色 + 全库 code review 修复
- feat: M4 远程执行通道（SSH 目标注册表 + 远程快照/回滚）
- feat: M2 Anthropic provider + 输出 spill + 审批戳 revoke + RPC 流式推送
- feat: M3 技能库（按需加载 playbook）+ 只读排查子代理
- feat: M5 定时巡检 + 长目标 + 工作流编排
- fix: 只读白名单扩充——ssh 直连命令集与本机对齐 + echo 入白名单
- fix: 流式渲染超屏重复打印 + systemctl/w/who 只读白名单补齐
- feat: 长期记忆—remember 工具 + memory.md 注入 system prompt
- feat: 记忆写法约定—主机属性/depends_on 结构化，为演进留基础
- fix: harden ops guard and agent reliability after full review
- docs: add personal ops agent roadmap
- feat: W1 安全边界—秘密脱敏、审批绑定执行环境、回滚确认与副本、CI 自检
- feat: W2 确定性巡检—三组固定探针、结构化报告与完整汇总
- feat: W3 调度可追踪运行—运行记录、中断/错过识别、整任务预算与失败退出码
- feat: W4 通知与事件—单渠道 webhook、去重/恢复/静默、事件时间线与规则检查前移
- fix: 修复 W1–W4 复审发现的安全、巡检与通知问题
- feat: W5 变更执行记录、目标级写锁、审批次数与远程原子恢复
- fix: 自检不再覆盖系统剪贴板
- fix: 修复 W5 执行安全与远程恢复复审问题
- feat: W6 变更场景—预检→快照→执行→后验→回滚建议
- feat: W7 体检、心跳告警、provider 退避与调用预算
- feat: 交付 W8 本地恢复演练并修复复审问题
- fix: 稳定 macOS CI 的 W8 本机演练
- docs: README 移除 Claude Code 插件形态的说法
- fix: 原子写入 W8 http-port 消除 CI 读取竞态
- fix: harden execution safety and agent reliability
- ci(release): publish verified source archives from version tags
- docs(release): v0.1.0-rc.1
- docs(release): align candidate publication date
