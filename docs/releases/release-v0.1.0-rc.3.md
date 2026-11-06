# ops-agent v0.1.0-rc.3 发布：邮箱通知渠道（SMTP）

> 2026-11-06 · 上一版：[v0.1.0-rc.2](release-v0.1.0-rc.2.md) · [GitHub Release](https://github.com/mengbin92/ops-agent/releases/tag/v0.1.0-rc.3)

v0.1.0-rc.3 是第三个**候选发布版本（prerelease）**：通知模块在既有 webhook 之外新增 SMTP 邮箱渠道，
告警出口覆盖没有 Slack/飞书/钉钉的场景。去重 / 恢复 / 静默 / 失败重试语义与 webhook 完全一致，
实现参考同级 user-center 的 SMTP 语义（465 隐式 TLS、远程强制 STARTTLS）。

真实通知渠道投递验收（专用 webhook 或真实邮箱投递）仍未执行，按发版纪律本版仍为候选版；
本地 322/322 自检与全量回归通过不替代外部验收。运行时零第三方依赖，Python 3.9+。

## 发布内容

### 1. 邮箱通知渠道（SMTP）

**需求**：W4 起通知只有 webhook 一种渠道，没接 IM 的人收不到告警；邮箱是最通用的兜底出口。

**实现**：`notify` 新增 `channel: email`，沿用「只启用一个渠道」的设计（换渠道改配置即可，
不多渠道路由）：

- `opsagent notify setup --channel email --smtp-host … --from … --to a@x,b@y [--smtp-user …]
  [--smtp-password …]`；只给邮箱参数时自动推断 email，给 `--url` 视为 webhook。
- 465 端口走隐式 TLS；其它端口要求 STARTTLS，远程主机强制加密、本机回环中继除外（便于本地验证）。
- SMTP 认证可选；密码可用 `--smtp-password` 或环境变量 `OPSAGENT_SMTP_PASSWORD`（避免进 shell 历史）。
- 配置入口校验主机 / 端口 / 地址：拒绝空白、控制字符、CR/LF（防邮件头注入）、多 `@` 地址、
  host 里塞端口；收件人上限 20。
- 凭据处理与 webhook 同级：密码只存 0600 的 `notify.json`（敏感文件拦截不变）；
  事件与发件箱只记 `smtp://host:port → N 个收件人`；SMTP 错误回显的密码 / 账号同样脱敏。
- `notify test / status / list / retry / mute` 与 `doctor` 通知检查全面对齐邮箱渠道。

**影响入口**：`opsagent notify` 全部子命令、`doctor` 通知项、巡检 / 调度的通知出口。

### 2. 文档与验收沉淀

- 回填 W8 7 天连续运行验收结论（56/56 成功、0 漏跑；滑动排期漂移发现并定案）。
- README / CHANGELOG / state-backup 文档同步邮箱渠道与凭据说明。

## 兼容性

- 既有 webhook 配置零改动：`notify.json` 缺 `channel` 字段一律按 webhook 处理。
- 换渠道后旧渠道的凭据字段不再保留（改配置即重填），发件箱与事件时间线历史可查。
- 无新增依赖；纯 stdlib（`smtplib` / `ssl` / `email.message` / `ipaddress`）。

## 安装升级

```sh
git fetch origin --tags && git checkout v0.1.0-rc.3
./opsx selftest          # 可选：本地自检
./opsagent notify setup --channel email --smtp-host smtp.example.com --smtp-port 465 \
    --from ops@example.com --to oncall@example.com --smtp-user ops@example.com
./opsagent notify test   # 真实投递一条测试通知
```

## 实际验证

- `./opsx selftest`：322/322 通过（新增邮箱渠道回归：本地回环 SMTP 真实投递、配置校验、
  密码不进输出与发件箱、SMTP 错误回显脱敏）。
- `python3 -m unittest discover -s tests`：319 tests OK（14 skipped）。
- 已验证边界：email ↔ webhook 渠道互切、静默 / 停用对邮箱渠道生效、`doctor` 输出。
- **未验证**：真实邮件服务商投递（企业邮箱 /  Relay），仅本地 SMTP 桩验证协议互通；
  升级后请用 `notify test` 对真实渠道做一次投递确认。

## 变更日志

- `d6c59d1` feat: 通知新增邮箱渠道（SMTP）
- `df9d4c2` docs: 回填 W8 7 天连续运行验收结论
