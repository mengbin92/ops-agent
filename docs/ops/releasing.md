# 发版流程

本项目参考同级 `micro-one-api` 的发版约定：先准备版本文档，再经过 `develop → main → tag → GitHub Release`。
Python 项目发布完整源码包及校验和，无 Docker 构建或生产部署步骤。

## 1. 准备版本与文档

1. 从最新 `main` 创建 `codex/release-vX.Y.Z` 工作分支，确认待发布修复已经提交并通过检查。
2. 更新根 `VERSION`，内容不带 `v`；版本 tag 使用 `vX.Y.Z`，候选版使用 `vX.Y.Z-rc.N`。
3. 创建 `docs/releases/release-<tag>.md`，包含日期/上一版/GitHub Release 链接、版本定位、
   编号“根因 → 修复 → 影响入口”、兼容性、安装升级步骤、实际验证与完整 Conventional Commit 变更日志。
4. 在 `CHANGELOG.md` 的 `[Unreleased]` 后插入新版本，并同步 README 的“最新发布”和“发布与升级”列表。
5. 发布说明、CHANGELOG、README 与 VERSION 以 `docs(release): <tag>` 提交。

真实环境及连续 7 天验收未通过时，仅发布候选版并保留未完成状态；本地桩成功不能替代外部验收。
标准说明见 [v0.1.0-rc.1](../releases/release-v0.1.0-rc.1.md)。

## 2. develop 与 main

以下分支名使用本轮工作分支的实际名称；首次没有 `develop` 时从远程 `main` 创建。
发布前更新远程引用，使用正常合并，不覆盖已有提交。

```sh
git fetch origin --tags
git switch develop
git merge --no-ff codex/release-vX.Y.Z
git push origin develop
```

等待 `develop` 对应提交的 selftest 检查成功，再合入 `main`：

```sh
git switch main
git merge --ff-only develop
git push origin main
```

如果 `main` 已有其他提交，先将其正常合并并重新验证，不能强制推送绕过分叉。

## 3. tag 与自动发布

确认 VERSION、发布说明、CHANGELOG、README 同步，目标提交在 `main` 后创建 tag：

```sh
git tag -a vX.Y.Z -m 'ops-agent vX.Y.Z'
git push origin vX.Y.Z
```

[Release workflow](../../.github/workflows/release.yml)检查精确 tag、VERSION 和发布说明，
并复用 [selftest workflow](../../.github/workflows/selftest.yml) 的 Ubuntu/macOS × Python 3.9/3.12
矩阵（编译、自检、独立回归、本地演练）。通过后从已提交的 tag 生成：

- `ops-agent-<tag>.tar.gz`：完整 Git 源码归档，保留 `opsagent` / `opsx` 的可执行权限。
- `SHA256SUMS`：归档的 SHA-256 校验和。

然后读取 `docs/releases/release-<tag>.md` 创建 GitHub Release。候选 tag 标为 prerelease，
不替代稳定版 latest。源码包不包含 `.git`、未跟踪缓存或用户实际状态。

## 4. 核实结果与失败处理

```sh
gh run list --repo mengbin92/ops-agent --workflow release.yml
gh release view vX.Y.Z --repo mengbin92/ops-agent
```

回读 Release 的 tag、提交、prerelease 标记、发布说明与两个制品；下载后核对 SHA-256 并做解包检查。
失败时先读 job 日志：临时基础设施故障可重跑相同 workflow；需修复源码时创建后继版本，
不要移动/覆盖已有 tag 或已发布资产。手动 workflow 输入必须是已经存在的 tag，不能用分支冒充。
