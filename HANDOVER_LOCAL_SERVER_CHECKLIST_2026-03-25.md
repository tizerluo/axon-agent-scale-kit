# AXON Agent Scale Kit 交接清单（本地+服务器，非通用化）

## 1. 项目与版本基线

- 本地仓库路径：`/Users/mac-mini/AXON-Chain/axon-agent-scale-kit`
- 当前分支：`main`
- 当前提交：`07dddc3`
- 关键状态文件：`state/deploy_state.json`
- 关键配置文件：
  - `configs/network.yaml`
  - `configs/agents.yaml`

## 2. 服务器与登录信息

- 目标主机：`jakarta-node`
- 公网 IP：`43.165.195.71`
- 用户：`ubuntu`
- 系统：`Ubuntu Server 24.04 LTS 64bit`
- 本地 SSH 私钥路径：`/Users/mac-mini/AXON-Chain/server/config/QQClaw.pem`
- 连接命令：
  - `ssh -i /Users/mac-mini/AXON-Chain/server/config/QQClaw.pem ubuntu@43.165.195.71`

- 主机映射文件（本地）：
  - `server/config/hosts.runtime.yaml`

## 3. 线上运行目录（Agent 相关）

- 运行根目录：`/home/ubuntu/axon-agent-scale`
- 核心文件：
  - `/home/ubuntu/axon-agent-scale/configs/network.yaml`
  - `/home/ubuntu/axon-agent-scale/configs/agents.yaml`
  - `/home/ubuntu/axon-agent-scale/scripts/axonctl.py`
  - `/home/ubuntu/axon-agent-scale/scripts/agent_worker.py`
  - `/home/ubuntu/axon-agent-scale/state/deploy_state.json`

## 4. 当前线上服务（systemd）

- `axon-heartbeat-daemon.service`
  - 状态：active
  - 作用：8 个 Agent 的链上 heartbeat 常驻守护
  - 服务文件：`/etc/systemd/system/axon-heartbeat-daemon.service`
  - ExecStart：
    - `/usr/bin/python3 /home/ubuntu/axon-agent-scale/scripts/axonctl.py heartbeat-daemon --state-file /home/ubuntu/axon-agent-scale/state/deploy_state.json --network /home/ubuntu/axon-agent-scale/configs/network.yaml --interval-sec 60`

- `axon-agent-qqclaw.service`
  - 状态：active
  - 作用：验证者侧 QQClaw Agent 守护（独立于 scale-kit）
  - 服务文件：`/etc/systemd/system/axon-agent-qqclaw.service`
  - 执行脚本：`/opt/axon-node/scripts/agent_daemon.py`

## 5. 当前线上容器（docker）

- 8 个 Agent 容器（image: `python:3.11-slim`）：
  - `axon-agent-agent-001`
  - `axon-agent-agent-002`
  - `axon-agent-agent-003`
  - `axon-agent-agent-004`
  - `axon-agent-agent-005`
  - `axon-agent-agent-legacy-006`
  - `axon-agent-agent-legacy-007`
  - `axon-agent-agent-legacy-008`

- 另有节点容器：
  - `axon-node`（image: `debian:trixie-slim`）

## 6. Agent 清单（当前纳管 8 个）

- `agent-001`：`0xF628086296B0fC4dCb8e9B8432Ca0aE89B5BA2F4`
- `agent-002`：`0xCCEa383facB2be40F4776E4B0935c4Fb3fa57C3D`
- `agent-003`：`0x596b90a3d5Df86B124d3bFbBf01B2FA3CEC0cFB8`
- `agent-004`：`0x8a9f9F5B609D93dB7B64BA2c284ddb1c067F5a11`
- `agent-005`：`0xF4914A80C40E8a4B34502B672728B60C0753574E`
- `agent-legacy-006`：`0xEDc2B7e121C4f78104dCAE669CC79E66FFEF9B50`
- `agent-legacy-007`：`0x71f3a07B95dBB283c19A7f37dc93fE50134D7250`
- `agent-legacy-008`：`0x98E33ba59e36453b5910F683040b9BE16280a2F3`

说明：
- 链上实时核验结果：`online_count = 8/8`
- `state/deploy_state.json` 中记录了最近 heartbeat 交易哈希与区块高度

## 7. 与 Agent 运行最相关的代码入口

- 本地项目：
  - `scripts/axonctl.py`
    - `heartbeat-once`
    - `heartbeat-batch`
    - `heartbeat-daemon`
    - `lifecycle-report`
  - `scripts/agent_worker.py`（容器中当前仅日志心跳）

- 线上实际心跳来源：
  - 当前依赖 `axon-heartbeat-daemon.service` 调用 `axonctl.py heartbeat-daemon`
  - 不依赖 `agent_worker.py` 直接上链

## 8. 关键非通用化信息（后续任务必须带上）

- SSH 私钥本地路径：`/Users/mac-mini/AXON-Chain/server/config/QQClaw.pem`
- 运行主机固定为：`43.165.195.71`
- 运行目录固定为：`/home/ubuntu/axon-agent-scale`
- 线上状态文件固定为：`/home/ubuntu/axon-agent-scale/state/deploy_state.json`
- 本地状态文件固定为：`/Users/mac-mini/AXON-Chain/axon-agent-scale-kit/state/deploy_state.json`

## 9. 运维常用命令（交接即用）

- 查看心跳守护状态：
  - `ssh -i /Users/mac-mini/AXON-Chain/server/config/QQClaw.pem ubuntu@43.165.195.71 "systemctl status axon-heartbeat-daemon.service --no-pager"`

- 查看心跳守护日志：
  - `ssh -i /Users/mac-mini/AXON-Chain/server/config/QQClaw.pem ubuntu@43.165.195.71 "journalctl -u axon-heartbeat-daemon.service -n 200 --no-pager"`

- 重启心跳守护：
  - `ssh -i /Users/mac-mini/AXON-Chain/server/config/QQClaw.pem ubuntu@43.165.195.71 "sudo systemctl restart axon-heartbeat-daemon.service"`

- 查看 Agent 容器：
  - `ssh -i /Users/mac-mini/AXON-Chain/server/config/QQClaw.pem ubuntu@43.165.195.71 "docker ps --format '{{.Names}}|{{.Status}}|{{.Image}}'"`

- 服务器侧健康快照：
  - `ssh -i /Users/mac-mini/AXON-Chain/server/config/QQClaw.pem ubuntu@43.165.195.71 "python3 /home/ubuntu/axon-agent-scale/scripts/axonctl.py lifecycle-report --state-file /home/ubuntu/axon-agent-scale/state/deploy_state.json --network /home/ubuntu/axon-agent-scale/configs/network.yaml"`

## 10. 继续优化时的风险点与建议

- 风险 1：本地跑守护会和服务器守护形成双写/冲突  
  - 建议：只保留服务器 `systemd` 常驻，本地只做一次性调试。

- 风险 2：改了本地 `axonctl.py` 但未同步服务器，导致“代码与线上行为不一致”  
  - 建议：每次改动后走固定发布动作：`scp -> systemctl restart -> lifecycle-report`。

- 风险 3：`agent_worker.py` 容器日志与链上真实心跳易混淆  
  - 建议：把链上活性判断统一到 `heartbeat-daemon + lifecycle-report + onchain getAgent.isOnline`。

## 11. 下一任务启动前的最小检查清单

- 确认 `axon-heartbeat-daemon.service` 为 active
- 确认 `docker ps` 中 8 个 agent 容器都在
- 跑一遍服务器侧 `lifecycle-report`
- 抽查链上 `online_count` 是否 8/8
- 确认本地与服务器 `axonctl.py` 版本一致（必要时比对 `sha256sum`）

## 12. 后续开发标准顺序（本地 / 服务器 / GitHub）

1. 本地设计与开发  
   - 仅在本地仓库改代码，不直接改线上运行目录。

2. 本地检查与回归  
   - 先跑本地测试与必要验证，确保改动可用再进入发布环节。

3. 本地一次性验证（可选）  
   - 仅允许一次性调试命令，不启本地常驻守护。

4. 提交并推送 GitHub  
   - 通过检查后 commit/push，确保发布版本有明确 commit 可追溯。

5. 服务器发布  
   - 将 GitHub 对应版本同步到服务器运行目录（不要跳过版本对应关系）。

6. 重启服务器守护  
   - 重启 `axon-heartbeat-daemon.service` 使新版本生效。

7. 服务器验收  
   - 执行 `systemctl status`、`lifecycle-report`、链上 online 抽查。

8. 回填交接记录  
   - 记录发布 commit、服务状态、验收结果与异常处理结论。

### 执行红线

- 禁止本地与服务器同时运行 heartbeat daemon（避免双写冲突）。
- 禁止“仅 scp 不 push”直接覆盖线上（避免版本不可追溯）。
- 禁止跳过服务器验收直接宣告完成。
