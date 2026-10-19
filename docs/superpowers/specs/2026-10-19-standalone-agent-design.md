# ops-agent 独立化重设计：从 Claude Code 插件到独立运维 Agent

日期：2026-10-19
状态：已评审（骨架 M0 随本文档落地）

## 1. 背景与目标

ops-agent 现状是一个**守门工具**（guard rail）：`core.py` 风险分级 + 审批戳 + 快照 + 审计，
外加 `.claude/agents/ops.md` 提示词与 PreToolUse hook——"智能"完全寄生在 Claude Code 宿主上，
离开宿主就没有 agent 可言。

**目标**：参考 pi（earendil-works/pi）与 DeepSeek Harness（deepseek-ai/deepseek-harness）
的架构，把 ops-agent 重设计为**独立可用的运维智能体**：自带 agent loop、LLM 接入、工具系统，
守门核心不变地成为其内置安全层，四种运行形态（REPL / 单发 / RPC / MCP）共享同一内核。

**非目标**：不重做守门分级规则本身（config.yaml 规则沿用）；不追求一步对齐 DSH 的全部子系统。

## 2. 参考架构提炼

### 2.1 pi（pi-mono）

- **分层**：`pi-ai`（统一 LLM provider API， Anthropic/OpenAI/Google 等 + OAuth 订阅）
  → `pi-agent-core`（agent loop + 工具调用状态机）→ `pi-tui` / `pi-coding-agent`（前端）。
- **四种形态**：interactive（REPL）、print/JSON（单发，`-p`）、RPC（JSON-RPC 长驻进程）、SDK（库嵌入）。
- **工具默认最小**：`read` / `write` / `edit` / `bash` 四件套，其余靠扩展。
- **可扩展性**：Extensions（TS）、Skills、Prompt Templates、Themes，打包为 npm/git 分发的 Pi Packages。
- **会话**：JSONL 持久化，支持分支（tree/fork/clone）与 compaction。
- **哲学**：harness 极简，功能外包给扩展——"adapt pi to your workflows"。

### 2.2 DeepSeek Harness（DSH）

cordis（Context + 插件生命周期）为骨架的**单一职责包**组合，与 ops-agent 相关的关键子系统：

| 子系统 | 对应包（举例） | 可借鉴点 |
|---|---|---|
| agent 内核 | dsh-agent-loop / dsh-agent-preset | loop 与"人设/工具集预设"分离 |
| LLM | dsh-llm / dsh-llm-pi-ai / dsh-llm-retry | provider 抽象 + 重试独立成层 |
| 工具 | dsh-tools / dsh-tool-* | 每工具一包：schema、执行、UI 呈现分离 |
| 安全 | dsh-sandbox-policy / dsh-user-approval / dsh-permission-presets | 策略与审批解耦，策略可换 |
| 会话 | dsh-session-format（v0→v4 迁移）/ -persistence-jsonl / -query-sqlite | 格式带版本号，可迁移、可检索 |
| 上下文 | dsh-compaction-* | 压缩策略插件化（basic / tool-result-pruner） |
| 会话外能力 | dsh-subagent / dsh-workflow / dsh-goal / dsh-schedule | 子代理、编排、长目标、定时 |
| 接入 | dsh-mcp-client / dsh-sdk-* / ACP | 既能调别人，也能被别人调 |

**两者共识**（ops-agent 直接采纳）：
1. loop 与 provider 解耦；2. 工具是注册表而非硬编码；3. 安全策略是独立可替换的层；
4. 会话 JSONL 追加写、格式带版本；5. 同一内核支撑多种前端形态。

## 3. 目标架构

```
┌─────────────────────────── 前端形态（均可独立启用） ───────────────────────────┐
│  REPL (agent/repl.py)   -p 单发   RPC stdio (agent/rpc.py)   MCP (mcp_server) │
├──────────────────────────────────────────────────────────────────────────────┤
│ agent 内核 (agent/loop.py)：消息装配 → LLM 调用 → 工具分发 → 结果回填，最大轮次保护 │
├──────────────┬───────────────────────────┬───────────────────────────────────┤
│ provider 层  │ 工具层 (agent/tools.py)    │ 守门层（现有，零改动）              │
│ agent/llm.py │ bash / read / ask_user /   │ core.py：分级/审批戳/快照/回滚/审计 │
│ OpenAI 兼容  │ ops_exec …注册表            │ config.yaml：R0-R3 规则            │
├──────────────┴───────────────────────────┴───────────────────────────────────┤
│ 状态层：~/.ops-agent/（approvals, snapshots, audit.jsonl）+ sessions/*.jsonl（v1） │
└──────────────────────────────────────────────────────────────────────────────┘
```

关键决策：

- **Python 3.9+，stdlib 起步**。LLM 调用用 `urllib`（SSE 流式也手写解析）；core.py 一行不改。
- **provider 先行做 OpenAI 兼容**（`/chat/completions` + tools），一个实现覆盖
  DeepSeek / OpenAI / Kimi / Qwen 等；Anthropic 原生协议留作 M2 的第二 provider。
- **工具即注册表**：`{name: {schema, handler, read_only}}`，前端只渲染、内核只分发。
- **守门在内核之内而非 hook 之外**：`bash` 工具handler 直接调 `core.check()`，
  拒绝时把变更单流程指令作为 tool result 回给模型；不再需要 PreToolUse 这一外部防线
  （MCP/Claude Code 形态下 hook 继续可用，双保险）。
- **审批的人机回路**：REPL/单发形态由 `ask_user` 工具完成（模型提问→用户终端回答）；
  RPC 形态由客户端实现同名回调；这与 pi/DSH 的 ask-user 工具一致。
- **会话格式 v1**：`~/.ops-agent/sessions/<id>.jsonl`，首行 `{"format":1,...}` 元信息，
  之后每行一条消息；为将来迁移（仿 dsh-session-format 链）留口。

## 4. 形态说明

| 形态 | 入口 | 说明 |
|---|---|---|
| REPL | `opsagent` 或 `opsagent chat` | 交互式；`ask_user` 走终端；`/new` `/quit` |
| 单发 | `opsagent -p "看一下 nginx 状态"` | 非交互，输出到 stdout，可进脚本；无 TTY 时 ask_user 返回"不可用"并降级为拒绝写操作 |
| RPC | `opsagent rpc` | stdio JSON-RPC：`prompt`（含 ask_user 回调请求）/ `reset`，供 Web UI、IM 机器人嵌入 |
| MCP | `python3 mcp_server.py` | 保留现状，作为 Claude Code 等宿主的后端工具集 |

## 5. 演进路线（对照 DSH 子系统）

- **M0（本次落地）**：loop + OpenAI provider + bash/read/ask_user 工具 + REPL/-p/RPC + 会话 v1 持久化。
- **M1**：SSE 流式输出；compaction（先 basic：超阈值裁剪早期 tool result）；`/resume`。
- **M2**：Anthropic provider；工具输出截断与 spill（仿 dsh-output-retention）；审批戳 revoke/过期清理；exec 超时与输出入审计（P0 安全债）。
- **M3**：skills 目录（仿 dsh-skill-filesystem：按需加载的运维 playbook）；子代理（只读排查代理并行跑）。
- **M4**：远程执行通道（SSH 目标注册表，远程快照），把守门能力延伸到 ops.md 声称的远程场景。
- **M5**：goal/schedule（定时巡检）、workflow 编排；按 DSH 的 cordis 思路评估是否引入插件框架（届时再议，M0 不引入）。

## 6. 目录布局

```
ops-agent/
  core.py / opsx / config.yaml / mcp_server.py   # 现状，不动
  agent/
    __init__.py
    llm.py        # OpenAI 兼容 provider（urllib，可选 SSE）
    tools.py      # 工具注册表 + 内建工具
    loop.py       # agent loop
    session.py    # 会话持久化（jsonl, format v1）
    repl.py       # 交互前端
    rpc.py        # JSON-RPC stdio 前端
  opsagent        # 入口：chat(默认) / -p / rpc
  selftest.py     # 既有守门自检 + 骨架回路自测
```

## 7. 风险与取舍

- **stdlib urllib 无 SSE 解析库**：M0 非流式（`stream:false`），REPL 体验稍钝，M1 补。
- **ask_user 在非 TTY 环境**：安全方向降级——写操作一律拒绝并提示用户改用 REPL。
- **双守门并存期**：hook（Claude Code）与内核守门（独立形态）规则同源（core.py），不会分叉。
