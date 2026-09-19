# ops-agent

默认只读、高风险操作需会话内授权并附风险说明与快照回滚的 Claude Code 运维子代理。

## 组成

- `.claude/agents/ops.md` — Agent 定义（R0-R3 风险分级与流程纪律）
- `.claude/settings.json` — PreToolUse hook，对所有 Bash 命令执行 `opsx check`
- `opsx` / `core.py` — 纯标准库 CLI/核心库（分类、审批戳、快照、回滚、审计）
- `config.yaml` — 只读白名单与风险规则；`~/.ops-agent/config.override.yaml` 追加覆盖

## 使用

必须在 `ops-agent/` 目录内启动（hook 为项目级配置，目录外只有 prompt 约束一层防线）。

```bash
# 自检
./opsx selftest

# 手动预授权（AskUserQuestion 不可用时的降级路径）
./opsx approve 'systemctl restart nginx' --ttl 900

# 查看审计 / 快照 / 审批戳
./opsx audit --tail 20
./opsx list
./opsx rollback <snapshot_id>
```

## 防线说明

两层防线：① Agent 流程纪律（变更单 → AskUserQuestion → opsx exec）；
② PreToolUse hook 硬拦截（无白名单命中且无有效审批戳 → exit 2 拒绝）。

已知边界：审批戳由 Agent 侧在获得用户确认后创建，防"误执行"不防"恶意 Agent"；
命令分类为线性正则匹配，不解析 shell AST（复合命令按 `;` `&&` `||` `|` 分段取最高级）。

## 演进

core.py 为纯函数库；演进 MCP server 时直接包装同一组函数，状态目录布局即对外契约。
