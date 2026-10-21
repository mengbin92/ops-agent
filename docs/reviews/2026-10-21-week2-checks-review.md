# 2026-10-21 W2 确定性巡检：实现与复审记录

对照 [roadmap](../2026-10-21-personal-ops-agent-roadmap.md) W2（完整 workflow 汇总、最小资产/服务配置、
连通性/资源/服务三组确定性检查与结构化结果、固定检查先运行且 LLM 解释可选）完成实现，
并对改动做了两轮独立复审。基线 `./opsx selftest` 123/123，交付后 **143/143**。
没有引入第三方依赖，未改动用户真实状态目录。

## 1. 交付内容

| 工作包 | 实现位置 | 结果 |
| --- | --- | --- |
| 三组确定性检查 | 新增 `agent/checks.py`（探针、解析、状态判定、报告） | `connectivity`（`uname -s`）、`resources`（`df -P -k`、`/proc/meminfo` 或 `vm_stat`、`/proc/loadavg` 或 `sysctl -n vm.loadavg` ÷ 核数）、`services`（`systemctl show`、`docker inspect --format`）。每条结果带观测值、阈值、采样时间、命令、退出码与证据片段 |
| 状态与结论语义 | `checks._verdict` / `_target_verdict` / `_item` | 五态 `ok/warn/critical/unknown/error`；`unknown`/`error` 不计入健康；结论"正常"要求「无异常 + 无未采集 + 三组全跑」，否则为「警告」「异常」「不完整」；只跑部分分组时目标级写「未完整检查」 |
| 最小资产/服务配置 | `checks.load_inventory`、`remote.get_target` / `describe` | 复用 `~/.ops-agent/targets.json`，新增可选 `tags/env/services/checks/enabled`；本机检查项写保留条目 `_local`（不允许连接字段，也不能当 SSH 目标）；顶层与 `checks`/`services` 的未知字段一律报错 |
| 固定检查先跑、LLM 可选 | `agent/tools._workflow`、`agent/workflow.py`、`workflows/daily-inspection.json` | workflow 新增 `checks` 块：先跑规则（不调用模型），再逐步只读子代理解读；占位符 `{checks}`/`{prev}`/`{steps}` 单次替换；API 完全不可用时规则结果照常产出，解释段标注失败 |
| 完整汇总 | `tools._workflow_footer`、`checks.save_report` | 汇总由程序生成：规则计数、全部异常（逐条）、每步状态与失败原因、否决语；每步原文完整入报告文件；输出超长截断时保留末尾汇总，避免早期异常被最后一步覆盖 |
| 入口 | `opsagent inspect`、`inspect` 工具、`SCHEDULED_TOOLS`、`config.yaml` | CLI 支持 `--target/--group/--timeout/--json/--explain`，退出码 0/2/3/1；agent 工具与无人值守定时任务可用（只读、规则先跑）；白名单补 `nproc`、`sysctl -n`（`-w` 仍非 R0） |
| 报告落盘 | `checks.save_report` / `_prune_reports` | `~/.ops-agent/reports/*.md`，0600 + `fsync`，保留 100 份，落盘前统一脱敏；写不进去时返回 None，调用方降级为直接返回文本 |

## 2. 复审发现并修复的问题（第一轮）

第一轮按"读 diff → 读调用方 → 跑真机"的方式复审，确认并修复：

| 优先级 | 问题与复现场景 | 修复 |
| --- | --- | --- |
| P0 | `--group connectivity` 只跑一组也能得到「结论: 正常｜完整性: 完整」：`verdict` 只看已执行项 | 报告增加 `coverage`（请求/跳过的分组、`enabled=false` 目标），`complete` 要求三组全跑；新增「覆盖」行；目标级在未跑全分组时写「未完整检查」 |
| P0 | `enabled=false` 的目标在 `--target all` 里静默消失 | `coverage.targets_skipped` + 报告「覆盖」行点名；完整性口径明确为「已启用目标」 |
| P1 | `_norm_env` 的错误文案是坏 f-string（`{字符串: 字符串}`），配置写错时抛 `NameError` 而不是可读错误 | 改为普通文本；`cli` 与 `_inspect` 增加非 `CheckError` 的兜底 |
| P1 | workflow 报告落盘不再脱敏（旧 `_cap` 会 `redact`），步骤自由文本可能把密钥写进报告文件 | `save_report` 内统一 `core.redact`；`_item` 改为**先脱敏再截断**（先截断会让私钥的 `BEGIN/END` 被切开而漏过正则） |
| P1 | 报告落盘失败（磁盘满/只读 home）会把已跑完的检查结果整体变成工具异常 | `save_report` 返回 `None` 并降级为直接返回文本；三个调用方都处理了 None |
| P1 | `checks` 块执行失败（如目标名写错）时汇总没有否决语，模型可以自行宣称正常 | footer 增加「规则检查未执行 → 不得汇总为正常」，`{checks}` 占位符也明确禁止宣称正常 |
| P1 | 不选 `connectivity` 分组时不探测平台，macOS 会去跑 `free`/`nproc`，结果全是误导性的 unknown | 平台探测与分组解耦，始终先跑 `uname -s`；失败时也保留证据 |
| P1 | `df` 表头被 locale 翻译（zh_CN 为"文件系统…"）导致磁盘整组 unknown | 解析不依赖表头（POSIX `-P` 第 5 列为容量）；Linux 内存改用 `/proc/meminfo`、负载改用 `/proc/loadavg`，全部与 locale 无关 |
| P1 | 保留名 `_*` 收得过宽：既有下划线目标（`_db`）在 `get_target`/`describe`/巡检里全部失效 | 只保留 `_local` 一个名字，其余下划线目标行为不变 |
| P2 | 单目标探针无上界（最多 33 次 × 单条超时） | 增加 `budget_seconds`（默认 120s，时间闸）与 60 次探针硬上界；超预算标 `unknown`，不漏项 |
| P2 | `opsagent inspect` 恒返回 0，cron 无法判定 | 退出码 0/2/3/1 并写入 `--help` 与 README |
| P2 | 目标顶层字段不校验，`disk_paths` 写错层级会静默用默认 `/` | `TARGET_FIELDS` 白名单，未知字段报错并提示"阈值要写在 checks 里" |
| P2 | `df` 挂载点取 `row[-1]`，含空格时展示错；`parse_free` 在缺列时把猜测当观测值 | 挂载点取容量列之后的全部字段；删除 `parse_free`，改用 meminfo |
| P2 | `tags`/`env` 可带换行进入报告与提示词 | 拒绝控制字符（后续第二轮补齐 host/user/env 键） |
| P3 | `save_report` 无 `fsync`、已存在目录不收紧权限、轮转无注释 | 补 `fsync`、`chmod 0700` 与 why 注释 |

## 3. 复审发现并修复的问题（第二轮）

第二轮针对"修复本身"复审，又找出几处：

| 优先级 | 问题与复现场景 | 修复 |
| --- | --- | --- |
| P0 | 输出超长时 `text[:OUTPUT_LIMIT]` 会把**末尾的汇总完整性整段截掉**，防"谎报正常"的最后一道程序约束消失；落盘失败时更无从恢复 | 截断策略改为"保头 + 保尾"：正文中段截断，footer 始终保留；报告文件保存完整正文 |
| P1 | `core.audit` / `core.check` 抛 `OSError`（磁盘满、只读 home）会让整次巡检与 workflow 全部丢失 | `run_checks` 的审计改 best-effort；`_probe` 捕获 `OSError`；新增 `_safe_target` 做单目标异常隔离 |
| P1 | 步骤"成功"只认异常前缀：模型空回复、`run_turn` 到 `max_turns` 都算成功，footer 会写"全成功" | `_step_reason` 把空输出与「已达最大轮次保护」判为失败并写明原因 |
| P1 | 单步文本被 `_clip(12000)` 截断后，报告文件里也没有完整内容（与注释承诺相反） | 取消单步截断：正文完整入报告，只有整体预览按总量截断 |
| P1 | `parse_df` 倒序扫描会把 `[stderr]` 告警行里的 `90%` 当容量列 | 只解析 stdout 段，并要求容量列固定在第 5 列 |
| P1 | `host`/`user`/`env` 的键未做控制字符校验，可在报告标题里伪造一行「结论: 正常」 | 一并校验；`_label` 仍走 `core.redact` |
| P1 | `--json --explain` 把解读打到 stdout，JSON 不再可解析 | `--json` 时解读写 stderr |
| P2 | argparse 用法错误默认退出 2，与"2 = 有警告/异常"冲突 | 自定义 `error()` 退出 1 |
| P2 | 最终汇总步骤只拿 `{steps}`（每步 400 字符摘要），多异常时会看不到早期异常 | `daily-inspection` 第 3 步同时注入 `{checks}` |
| P2 | `_PATH_RE` 拒绝空格/非 ASCII，`/Volumes/My Disk` 无法配置；且会让整份巡检失败 | 允许空格与非 ASCII（仍拒绝控制字符与 `..`，长度 ≤500），命令侧由 `shlex.quote` 保证 |
| P3 | 远端超时原因被 `_fail_detail` 丢掉；核数探测失败时证据只剩"（不可用）" | 前缀表补 `[opsx] 超时`；证据保留原始输出 |
| P3 | 预算 docstring 说"钉在配置值上"，实际上界是 `budget + 单条 timeout` | 更正说明；补 `_budget_state` 的时间闸/次数闸单测 |
| P3 | `_with_targets` 的临时目录断言可被 `/tmp-foo` 绕过，且 `python -O` 会剥离 assert | 改为 `raise` + `resolve()` 后比较父目录名 |
| P3 | `parse_meminfo`/`parse_vm_stat` 的 `int()` 未捕获 `ValueError`（超长数字触发 3.11+ 位数上限） | 按"无法解析"返回 None |
| P3 | `_connectivity` 取首行未跳过 `[stderr]` 标记，平台可能显示成 `[stderr]` | 复用 `_headline` |

## 4. 验证

- `./opsx selftest`：**143/143**（基线 111 项与 W1 的 12 项全部保留；新增 20 项：
  三台主机结构化报告、解析器（含 zh_CN 表头/含空格挂载点/stderr 干扰）、配置校验、
  unknown 不谎报、覆盖不足不出「正常」、跳过目标可见、探针预算（时间闸+次数闸）、
  下划线目标兼容、落盘失败降级、退出码、CLI 用法错误、`--json --explain` 输出合法性、
  大输出保留汇总、步骤未完成不算成功、workflow `checks` 校验、LLM 不可用仍出规则结果、
  汇总保留每步与早期异常、白名单新增命令仍非写）。
- `python3 -m compileall -q core.py remote.py opsx mcp_server.py opsagent agent selftest.py` 通过；
  `git diff --check` 通过。
- 真机 macOS 跑通 `opsagent inspect`（平台/磁盘/内存/负载全部解析正确，`vm_stat` 由
  原先重复计数压缩内存导致 100% 的算法改为 active + wired）、`--json`、`--group`、
  不可达目标（`connect_timeout=2` 得到 `error: ssh: connect ... timed out`）与配置报错路径。
- 未连接真实 Linux 目标、真实 Docker 服务与真实 LLM；Linux 侧由解析单测与假执行器覆盖。

## 5. 验收对照（W2 门槛）

| 门槛 | 结论 | 依据 |
| --- | --- | --- |
| 一台正常、一台磁盘超阈值、一台不可达 | ✅ | `test_checks_three_hosts_report`：web1 正常、web2 `/data` 97% 异常、db1 不可达不完整 |
| 报告保留全部异常 | ✅ | 异常清单在报告头部与 footer 各列一次，逐条断言 `line in text`；输出截断时 footer 仍保留 |
| 失败检查明确 unknown/error | ✅ | 不可达 → `error` + 各组 `unknown`；解析失败/单元不存在/docker 不可用 → `unknown`；均不进「正常」 |
| API 完全不可用仍产出规则检查结果 | ✅ | `test_workflow_checks_first_and_llm_down`（无 API Key 实测同样成立）：规则段完整、异常逐条、解释步标注失败 |

## 6. 已知边界（不在 W2 解决）

- 探针是固定少数几项，不等于健康全景：不覆盖 HTTP/TLS 探针、数据库连通性、证书有效期、
  备份新鲜度；`unknown` 只表示"没采到"，需人按证据复核。
- 调度层仍未做整任务时间预算（单目标有预算，但多目标串行累加）、run/session 记录、
  暂停/恢复与 `--once` 退出码——这些属于 W3；`inspect` 的 0/2/3/1 已就位可被复用。
- 巡检期间若有人改写 `targets.json`，同一次报告的不同探针可能打到不同主机（报告按开始时的
  目标快照展示，未加锁也未记录注册表哈希）。
- 未配置 `services` 的目标会把服务组记 `unknown`（业务可用性未被检查），这是有意的严格口径；
  本地机器可通过 `_local` 配置服务清单来消除该项。
- 探针命令仍是线性白名单判定，不解析 shell AST；`df`/`meminfo` 等命令本身只读，
  但目标机上其他进程的行为不在本工具控制范围内。
