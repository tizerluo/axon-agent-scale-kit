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
