# Linux 常驻只读巡检

当前（2026-10-28 起）连续 7 天观察的调度器部署在别名目标 `obsServer`，由 root 用户级 systemd 托管。
Mac 仅用于管理：关闭终端、休眠或断开 SSH 都不会停止服务器上的调度。
实际部署与验证见[7 天观察部署记录](../validation/2026-10-28-observation-deploy.md)；
更早的 `localServer` 试运行记录见[2026-10-26 记录](../validation/2026-10-26-readonly-trial.md)，该环境已不再运行调度。

## 当前运行布局

以下路径均指 Linux 服务器的用户家目录：

| 内容 | 路径或配置 |
| --- | --- |
| 固定版本源码 | `~/.local/share/ops-agent/releases/<commit>/` |
| 当前版本入口 | `~/.local/share/ops-agent/current`，指向固定版本 |
| 试运行状态 | `~/.ops-agent-trial/` |
| 模型环境 | `~/.ops-agent-trial/.env`，权限 0600；不写入仓库或 service 文件 |
| 用户级服务 | `~/.config/systemd/user/opsagent-schedule.service` |
| 自动恢复 | `Restart=always`，`RestartSec=10`；`UMask=0077` |
| 登出后常驻 | `loginctl show-user "$USER" --property=Linger` 为 `Linger=yes` |

服务通过绝对 Python 路径启动 `opsagent schedule run`，显式设置 `OPSX_STATE_DIR` 和 `TZ=Asia/Shanghai`，
由 `EnvironmentFile` 加载必要模型配置。用户级服务启用到 `default.target`。
不要依赖交互式 shell 的环境，也不要在排障输出中打印环境文件或完整 Environment 属性。

调度器与被检查服务位于同一台 Linux 主机，任务的检查目标为内置 `local`，服务清单写在 `targets.json` 的 `_local`。
这里的 `local` 指服务器，不是 Mac；无需在服务器复制 SSH 私钥。服务基线固定，不按每轮现存容器自动增删。

## 日常管理

以下命令使用已经配置的 OpenSSH `Host obsServer` 别名；它与 `opsx targets` 的注册别名相互独立。
在其他管理机执行时，先确认同名 SSH 别名已配置。

```bash
# 后台服务状态与近期日志
ssh obsServer 'systemctl --user status opsagent-schedule.service --no-pager'
ssh obsServer 'journalctl --user -u opsagent-schedule.service -n 40 --no-pager'

# 调度心跳、排期与实际运行记录
ssh obsServer 'OPSX_STATE_DIR="$HOME/.ops-agent-trial" python3 "$HOME/.local/share/ops-agent/current/opsagent" schedule status'
ssh obsServer 'OPSX_STATE_DIR="$HOME/.ops-agent-trial" python3 "$HOME/.local/share/ops-agent/current/opsagent" schedule runs --id sch-200a62 --tail 20'

# 仅在没有正在执行的任务时重启本调度服务
ssh obsServer 'systemctl --user restart opsagent-schedule.service'

# 停用托管：阻止当次运行及后续自动启动
ssh obsServer 'systemctl --user disable --now opsagent-schedule.service'
```

观察期间保持唯一调度：不要同时恢复 Mac 或其他主机的排期。文件锁不能跨机器阻止重复巡检，
迁移前先停用本服务，再核对旧状态与凭据。

## 连续观察的边界

- 固定版本部署不会随 Mac 工作区修改而自动更新；更换执行版本后按观察规则判定是否重新计时。
- `Restart=always` 恢复进程退出；服务器断电、网络中断或系统故障仍需按实际记录核对中断和漏跑。
- 已核验 enabled、linger 和自动拉起；未实际重启服务器验收启动恢复。
- 默认报告保留 100 份，运行记录保留 200 份；每日证据归档与通知送达仍须独立验收。
