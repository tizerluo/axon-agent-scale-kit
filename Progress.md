# 工作进度日志

## 2026-03-29 — 四端同步核对 + SSH 服务器连接尝试

### 我们实现了哪些功能？

- 根据仓库内文档核对了 **jakarta-node** 的 SSH 参数：`ubuntu@43.165.195.71`，私钥路径 `/Users/tizerluo/Downloads/QQClaw.pem`，工作目录 `/home/ubuntu/axon-agent-scale`（来源：`HANDOVER_LOCAL_SERVER_CHECKLIST_2026-03-27.md`、`docs/DEVELOPER_REFERENCE.md`、`configs/runtime/hosts.runtime.yaml`）。
- 对 **本地 / tizerluo GitHub / 6tizer GitHub** 三端执行了 `git fetch` 并记录了各端 `main` 与当前功能分支的短 SHA，用于对比进度。

### 我们遇到了哪些错误？

1. **SSH 无法完成握手**：使用文档中的密钥与命令连接 `43.165.195.71:22` 时，TCP 可建立，但在 **`kex_exchange_identification` 阶段被服务端关闭**（`Connection closed by 43.165.195.71 port 22`），未能进入身份认证，因此**无法读取**服务器上的 `.release_meta.json` 或 `git rev-parse`。
2. **本机 OpenSSH 10.2 对 `QQClaw.pem` 的提示**：`-vvv` 中出现 `no pubkey loaded` / `identity file ... type -1`。将密钥**副本**转为 `BEGIN OPENSSH PRIVATE KEY` 后重试，**仍然在握手阶段被断开**，说明当前阻塞**主要不是**密钥文件格式，而是**服务端在版本交换/握手早期即断开**（常见原因：云安全组/防火墙仅允许部分来源 IP、fail2ban、sshd 并发或策略限制等）。

### 我们是如何解决这些错误的？

- **密钥格式**：用临时副本执行 `ssh-keygen -p` 得到 OpenSSH 格式密钥后重试，结果仍为握手被断开，排除了「仅因 PEM 格式导致无法连接」这一单一假设。
- **服务器端版本对比**：在**无法登录**的前提下无法自动拉取第四端（服务器）的部署 SHA；需你在**当前出口 IP 已被放行**的网络下执行文档中的 `ssh -i ... ubuntu@43.165.195.71`，并查看 `/home/ubuntu/axon-agent-scale/.release_meta.json`（若存在），或联系维护者检查 **腾讯云安全组 / 服务器 sshd 与封禁策略**。

### 四端进度结论（截至本次 fetch；服务器端未实测）

| 端 | 状态 |
|----|------|
| **本地** | 分支 `feature/challenge-real-tx`，`0ec4ec8`。 |
| **tizerluo GitHub** | `origin/main` = `7bfaf3d`（落后 6tizer `main` 3 个提交）；`origin/feature/challenge-real-tx` = `0ec4ec8`（与本地该分支一致）。 |
| **6tizer GitHub** | `upstream/main` = `049714d`（当前 **`main` 线领先** tizerluo 的 `main`）。 |
| **服务器 (43.165.195.71)** | **本次未能连接**，线上代码版本**未知**；历史交接文档曾记载线上为 tar/scp 部署、可能与 Git 历史不一致，需登录后单独核对。 |

---

## 2026-03-29（第二次）— 用户确认腾讯云防火墙放行 22 后复测 SSH

### 我们实现了哪些功能？

- 在用户确认 **腾讯云轻量防火墙已对「Linux 登录(22)」允许、来源为所有 IPv4** 后，再次使用文档密钥执行 `ssh` 与 `ssh -vvv` 复测，并用 Python `socket.recv` 观察是否在收到任何字节前对端即关闭连接。

### 我们遇到了哪些错误？

- **现象不变**：`kex_exchange_identification: Connection closed by remote host`；在发送客户端 `SSH-2.0-OpenSSH_10.2` 版本串后，**仍读不到服务端 SSH 欢迎行**。
- **补充观测**：纯 TCP 连接后 `recv` 得到 **空字节**（对端在未发送 SSH banner 的情况下结束连接），说明问题发生在 **云外防火墙之后的链路**（实例内 `sshd`/系统防火墙/fail2ban/安全组件等），与「控制台 22 放行」可以**同时成立**——控制台只保证流量到达实例网卡，**不保证**实例内进程会回复 SSH 协议。

### 我们是如何解决这些错误的？

- **未解决（本机仍无法完成握手）**：建议在有 **VNC/控制台登录** 权限时，在实例内检查：`systemctl status ssh`、`sudo ufw status`、`sudo iptables -L -n`、`sudo fail2ban-client status`，以及 `sudo tail -50 /var/log/auth.log`（或 `journalctl -u ssh -n 50`）。
- **客户端侧**：OpenSSH 10.2 对原始 `QQClaw.pem` 仍报 `type -1`，将副本转为 **OpenSSH 私钥格式** 后客户端可 `loaded pubkey` / `type 0`，但**握手仍被对端关闭**，进一步说明瓶颈在**服务端或源 IP 在实例侧被封禁**，而非仅密钥文件扩展名问题。

---

## 2026-03-29（第三次）— VNC 登录服务器，日志确认根因

### 根因确认：通过 VNC 进服务器，查 sshd 日志，交叉比对出口 IP

- **服务器 sshd 完全正常**：`active (running)`，UFW `inactive`，无 fail2ban，未见对本机 IP 的拒绝记录。
- **VNC 登录时（你当前网络）**：服务器 sshd 日志出现 `Accepted publickey for ubuntu from 106.55.203.47`（公钥 SHA256 与 `QQClaw.pem` 完全一致）——说明**密钥是对的，服务器完全接受这把钥匙**。
- **Cursor Agent（我这边）出口 IP**：`103.142.140.56`（`curl -s ifconfig.me`）。
- **结论**：Agent 运行环境（`103.142.140.56`）在某个层级被拒绝，而你本机（`106.55.203.47`）网络完全正常。

### 可能的限制层级

1. **腾讯云「登录限制」/「来源 IP 白名单」**：轻量应用服务器控制台可能有「仅允许特定 IP 通过 SSH 登录」功能，白名单含 `106.55.203.47`，不含 `103.142.140.56`。
2. **腾讯云云防火墙（Cloud Firewall）**：如有开通，可在云防火墙控制台查看 22 端口策略。
3. **网络层面差异**：Cursor Agent 运行环境与你本机可能走了不同出口，腾讯云对不同来源的路由/策略不同。

### Cursor Agent（我这边）连接状态记录

| 尝试次数 | 出口 IP | SSH 结果 |
|---------|---------|---------|
| 第1次 | `103.142.140.56` | ❌ 握手被关 |
| 第2次 | `27.38.239.181` | ✅ `CONNECT_OK` |
| 第3次（验证） | `27.38.239.181` | ✅ 成功读取 `.release_meta.json` |

### 服务器当前状态

- **hostname**: `VM-0-13-ubuntu`
- **部署 commit** (`.release_meta.json`): `7bfaf3d3c0cb1626584834a1362c760111c99d2d`（`2026-03-27T11:13:23Z` 部署）
- **Git 仓库**: 服务器上 **不存在 `.git` 目录**，是 tar 包解压部署，非 clone
- **部署 SHA 与 tizerluo GitHub `main` 一致**：服务器运行的就是 `tizerluo/main` 的 `7bfaf3d`

### IP 限制分析

- 第1次失败（`103.142.140.56`）与第2次成功（`27.38.239.181`）出口 IP 不同，但均来自 Cursor Agent 的云端 IP 池
- 腾讯云侧**未发现永久 IP 黑名单**，可能是当时的 rate limit 或 TCP 连接抖动
- 你本机 VNC 登录成功 IP 为 `106.55.203.47`，与 Agent 的云端 IP 池（`27.x` / `103.x` / `43.x`）不在同一网段

### 四端 SHA 对照（最新）

| 端 | SHA | 备注 |
|----|-----|------|
| 本地 `feature/challenge-real-tx` | `0ec4ec8` | 待推送/合并 |
| tizerluo `main` | `7bfaf3d` | 与服务器部署一致 ✅ |
| 6tizer `main` | `049714d` | `main` 线领先（比 `7bfaf3d` 新 4 个 commit） |
| 服务器 | `7bfaf3d` | 与 `tizerluo/main` 一致 ✅ |
