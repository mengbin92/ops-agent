# 2026-10-21 W1 安全边界：实现与复审记录

对照 [roadmap](../2026-10-21-personal-ops-agent-roadmap.md) W1（秘密输出保护、回滚确认与回滚前副本、
审批绑定实际执行环境、自检接入 CI）完成实现，并对改动做了完整复审。本轮确认的问题已全部修复；
没有引入第三方依赖。基线 `./opsx selftest` 111/111，修复与新增后 **123/123**。

## 交付内容

| 工作包 | 实现位置 | 结果 |
| --- | --- | --- |
| 秘密输出保护 | `core.redact` / `redact_obj`、`agent/tools.py` 出口与 `_cap` 落盘、`agent/session.py` 落盘、`core.audit`、MCP `_result`、CLI `exec`/`rollback`/`list`、`agent/memory.py` | 命令/工具输出、会话、审计、长输出落盘、审批列表、MCP 回包、CLI stdout 统一脱敏；`remember` 拒绝含密钥条目；快照保留原始内容（0700，`read` 与命令入口都不读快照原件） |
| 敏感文件读取限制 | `core._SENSITIVE_FILE_PATTERNS` / `_SENSITIVE_DIR_PATTERNS` / `sensitive_command`、`agent/tools._read` | 私钥、`.env`、`.netrc`、`.kube/config`、`.aws/credentials`、`.docker/config.json`、`/etc/shadow` 与 `.ssh`/`.aws`/`.kube`/`.gnupg` 目录在 `read` 与命令入口（含 hook、远程、符号链接）都被拒绝；`ls -l`/日志搜索/`find -name` 不误伤 |
| 回滚确认与回滚前副本 | `core.rollback_plan` / `format_rollback_plan` / `_rollback_backup` / `rollback(confirm=)`、`remote.rollback_plan` / `rollback(confirm=)`、`opsx rollback [--yes]`、`ops_rollback` 工具、MCP `confirm` | 先展示目标/文件/差异并确认；未确认不改文件；执行前把当前内容另存为「回滚前副本」快照并报告 id，可再回滚副本撤销；本地/远程一致 |
| 审批绑定实际执行环境 | `core.resolve_env` / `local_env` / `k8s_scope`、`remote.target_env`、`approve` / `check_stamp` | 戳绑定本机 host+cwd+`KUBECONFIG` 或目标的实际 host/user/port/identity/options，以及 kubectl 的 `--context`/`--namespace`；kubectl 变更必须显式 `--context`；环境变化即失效（旧戳安全失效） |
| CI | `.github/workflows/selftest.yml` | push/PR 上 `compileall` + `./opsx selftest`（Ubuntu/macOS × Python 3.9/3.12） |

## 复审发现并修复的问题

除实现中的自检外，本轮另做了一次独立复审（按 ponytail-review：先读 diff，再读调用方、测试与
README，逐条给复现场景）。确认的问题与修复：

| 优先级 | 问题与复现场景 | 修复位置 |
| --- | --- | --- |
| P1 | JSON 里的密钥不脱敏：`{"password": "hunter2"}`、`{"client_secret":"GOCSPX-…"}` 原样进入模型/会话/审计 | `core._SECRET_KV` 允许键两侧成对引号，替换时保留引号；补 JSON 用例 |
| P1 | 常见凭据文件仍可直接 `cat`：`/etc/kubernetes/admin.conf`、`/etc/ssh/ssh_host_*_key`、`/etc/ssl/private/*.pem`、`~/.config/gcloud/*`；`ssh host "cat id_rsa"` 的内层命令未检查 | 补敏感文件/目录规则；`sensitive_command` 对 `ssh` 的引号内层命令递归检查（strict） |
| P1 | kubectl context 可伪造：全路径 `/usr/local/bin/kubectl`、`sh -c "kubectl …"` 不被识别；`kubectl apply … # --context=prod` 的注释参数被当成真 context，实际用默认 context | kubectl 段按词识别（含全路径/引号内）、扫描前去 shell 注释；注释/别段的 `--context` 不再有效；补回归用例 |
| P1 | 远程回滚的「回滚前副本」不可用：meta 缺 `target`、条目缺 `mode`，`load_snapshot` 报“快照元数据损坏”，承诺的撤销命令必然失败 | `_rollback_backup` 写入 `target` 与目标指纹；远程副本可加载、可再回滚（用例验证推回的是回滚前内容） |
| P1 | 模型宿主出口未统一脱敏：MCP 工具结果、CLI `exec`/`rollback` stdout（Claude Code Bash 会进模型）、`opsx list` 的审批命令展示、拒绝文案里的 `命中分段` 都能带出明文 | MCP `_result`/`_tool_error`、CLI `exec`/`rollback`/错误路径、`format_state`、`core.check` 拒绝文案统一 `redact` |
| P1 | 默认状态目录的快照原件可 `cat`；`remember` 可把密钥写进 `memory.md` 并每轮注入 system prompt | `_SENSITIVE_DIR_PATTERNS` 增加默认 `~/.ops-agent/snapshots`；`agent/memory.py` 拒绝脱敏后变化的条目 |
| P2 | 远程副本/回滚解析目标名时按当前注册表：`targets.json` 把 web1 改指向另一台机器后，旧快照会覆盖新机器 | 远程快照记录 host/user/port/identity/options；`rollback_plan` 与 `rollback` 校验不一致即拒绝，预览显示主机名 |
| P2 | 目标指纹漏 identity/options：加 `-o HostName=…`/`ProxyCommand=…`/换密钥不会让旧戳失效 | `remote.target_env` 纳入 `identity`/`options`，仍复用 `ssh_argv` 字段校验 |
| P2 | 多文件回滚中途失败时，错误信息丢掉副本 id，用户看到半恢复状态却找不到备份 | `rollback`/`remote.rollback` 失败信息附带「回滚前副本: id」；补模拟第二个文件写失败用例 |
| P2 | 没有旧内容（文件原本不存在）时仍创建空副本并承诺可撤销 | `_rollback_backup` 无内容时返回 None、不建空快照，报告不输出副本行 |
| P2 | `KUBECONFIG=~/.kube/config kubectl get pods` / `--kubeconfig …` 被敏感检查误挡且无法批准 | 环境变量赋值与 `--kubeconfig` 参数不算读取文件；`KUBECONFIG=` 前缀的只读 kubectl 仍判 R0（写标记仍按原始段检查） |
| P2 | 只读 kubectl 段也会强制 `--context`：`systemctl restart nginx && kubectl get pods` 在发戳阶段被拒 | `_kubectl_read_only` 按动词过滤；同一命令混用不同 context 直接拒绝（避免展示与执行不一致） |
| P2 | 审批后换 cwd/host 只提示“请提交变更单”，用户不知道旧戳已存在只是环境不同 | `core.stamp_env_mismatch`：戳存在但环境不同时明确提示重新批准 |
| P2 | 旧格式（无 env）的审批戳仍出现在 `opsx list` 的 approvals 里，但永远无法执行 | `collect_state` 只列带 env 的戳 |
| P3 | `.env.example` 被当成密钥文件 | `.env.example/.sample/.template` 豁免 |
| P3 | 回滚预览/副本 IO 错误以 traceback 冒到 CLI；回滚前副本失败未写审计；注册表字段非法也能建戳 | 统一包装 `OpsxError`、`rollback-failed` 审计覆盖备份失败、`target_env` 复用 `ssh_argv` 校验 |
| P3 | assistant prompt 的「回滚」小节插在「远程目标」列表中间；测试与文档仍按旧签名/旧行为描述 | 调整小节顺序；更新 `selftest.py`、README、tutorial、`.claude/agents/ops.md` |

## 验证

- `./opsx selftest`：**123/123**（基线 111 项全部保留，新增 12 项边界回归：脱敏形态、各出口
  哨兵、敏感文件限制、回滚预览/确认/副本、agent 回滚确认、cwd/host/KUBECONFIG 绑定、kubectl
  context、旧戳失效、CLI/MCP 输出脱敏、远程回滚目标校验与副本可撤销、多文件回滚失败带副本
  id、CI 接线）。
- `python3 -m compileall -q core.py remote.py opsx mcp_server.py agent selftest.py`（Python 3.9.6）通过；
  `git diff --check` 通过。
- 手工验证：非交互 `opsx rollback` 拒绝且文件不变、`--yes` 恢复并打印回滚前副本 id；
  `opsx approve 'kubectl apply -f x'` 拒绝，带 `--context` 通过且显示环境（全路径/`sh -c`/
  注释伪装同样拒绝）；hook 对 `cat ~/.ssh/id_rsa`/`.ssh/config` 返回 2，对 `ls -l ~/.ssh` 放行；
  `opsx exec` 与 MCP 回包中的哨兵密钥都被替换；`KUBECONFIG=…` 的只读 kubectl 判 R0。
- 未连接真实 LLM、SSH、K8s 或生产服务；没有修改用户真实状态目录。

## 已知边界（不在 W1 解决）

- 脱敏是正则启发式：加密内容、二进制、自定义字段/用户自拼格式会漏报；按“宁可多拦”处理，
  可能挡住合法密钥操作。
- 命令分类仍是线性匹配、不解析 shell AST；经人工审批的复杂命令（如 `python -c` 读文件）
  仍可能把内容带进输出，依赖脱敏兜底，不宣称能识别所有秘密。
- 回滚覆盖写会把符号链接路径变成普通文件；快照只能恢复文件，不能替代数据库/卷/部署回退。
- W1 之前创建的审批戳（无环境信息）一律失效需重新授权；旧会话、旧审计与旧记忆中的明文不会
  回溯清理。
- 插件形态（Claude Code）的人工确认来自 AskUserQuestion 与 CLI 预览，`--yes` 供人或脚本使用；
  与既有审批戳一致，防误执行、不构成对恶意 Agent 的隔离。
