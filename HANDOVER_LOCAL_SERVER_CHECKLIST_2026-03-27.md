# AXON Agent Scale Kit 交接清单（本地+服务器，非通用化）

## 1. 项目与版本基线

- 基线更新时间：`2026-03-27 CST`
- 本地仓库路径：`/Users/tizerluo/Cursor2026/AXON/axon-agent-scale-kit`
- 当前分支：`docs/collaboration-workflow`
- 当前提交：`6db57f4`
- 远端同步状态：`HEAD == origin/feature/cursor-dev`
- 关键状态文件：`state/deploy_state.json`
- 关键配置文件：
  - `configs/network.yaml`
  - `configs/agents.yaml`
  - `configs/runtime/hosts.runtime.yaml`（gitignored，不入仓库）

## 2. 服务器与登录信息

- 目标主机：`jakarta-node`
- 公网 IP：`43.165.195.71`
- 用户：`ubuntu`
- 系统：`Ubuntu Server 24.04 LTS 64bit`
- 本地 SSH 私钥路径：`/Users/tizerluo/Downloads/QQClaw.pem`
- 连接命令：
  - `ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71`

- 主机映射文件（本地）：
  - `configs/runtime/hosts.runtime.yaml`

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
  - 作用：9 个 Agent 的链上 heartbeat 常驻守护
  - 服务文件：`/etc/systemd/system/axon-heartbeat-daemon.service`
  - ExecStart：
    - `/usr/bin/python3 /home/ubuntu/axon-agent-scale/scripts/axonctl.py heartbeat-daemon --state-file /home/ubuntu/axon-agent-scale/state/deploy_state.json --network /home/ubuntu/axon-agent-scale/configs/network.yaml --interval-sec 60`

- `axon-agent-qqclaw.service`
  - 状态：inactive (disabled, migrated 2026-03-27)
  - 作用：已迁徙——qqclaw-validator 私钥已导入 scale-kit，由 heartbeat-daemon 统一管理
  - 归档文件：`/opt/axon-node/scripts/agent_daemon.py.bak`（服务器上保留备份）

## 5. 当前线上容器（docker）

- 9 个 Agent 容器（image: `python:3.11-slim`）：
  - `axon-agent-agent-001`
  - `axon-agent-agent-002`
  - `axon-agent-agent-003`
  - `axon-agent-agent-004`
  - `axon-agent-agent-005`
  - `axon-agent-agent-009`
  - `axon-agent-agent-legacy-006`
  - `axon-agent-agent-legacy-007`
  - `axon-agent-agent-legacy-008`

> 注意：qqclaw-validator 无独立 Docker 容器——它的 agent daemon 已停用，由 heartbeat-daemon 通过 RPC 直接管理链上心跳。

- 另有节点容器：
  - `axon-node`（image: `debian:trixie-slim`）

## 6. Agent 清单（当前纳管 10 个）

- `agent-001`：`0xF628086296B0fC4dCb8e9B8432Ca0aE89B5BA2F4`
- `agent-002`：`0xCCEa383facB2be40F4776E4B0935c4Fb3fa57C3D`
- `agent-003`：`0x596b90a3d5Df86B124d3bFbBf01B2FA3CEC0cFB8`
- `agent-004`：`0x8a9f9F5B609D93dB7B64BA2c284ddb1c067F5a11`
- `agent-005`：`0xF4914A80C40E8a4B34502B672728B60C0753574E`
- `agent-legacy-006`：`0xEDc2B7e121C4f78104dCAE669CC79E66FFEF9B50`
- `agent-legacy-007`：`0x71f3a07B95dBB283c19A7f37dc93fE50134D7250`
- `agent-legacy-008`：`0x98E33ba59e36453b5910F683040b9BE16280a2F3`
- `agent-009`：`0x7B4A3F8d501FDD31A9dC4Bc8dbE312121D276b57`
- `qqclaw-validator`：`0xA98dC2a1E964ED8fB96539045C7dab75C3Ddd34f`

说明：
- 链上实时核验结果：`online_count = 10/10`（2026-03-27 验证）
- qqclaw-validator 无 Docker 容器，由 heartbeat-daemon 直接发 RPC 心跳

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

- SSH 私钥本地路径：`/Users/tizerluo/Downloads/QQClaw.pem`
- 运行主机固定为：`43.165.195.71`
- 运行目录固定为：`/home/ubuntu/axon-agent-scale`
- 线上状态文件固定为：`/home/ubuntu/axon-agent-scale/state/deploy_state.json`
- 本地状态文件固定为：`/Users/tizerluo/Cursor2026/AXON/axon-agent-scale-kit/state/deploy_state.json`

## 9. 运维常用命令（交接即用）

- 查看心跳守护状态：
  - `ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71 "systemctl status axon-heartbeat-daemon.service --no-pager"`

- 查看心跳守护日志：
  - `ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71 "journalctl -u axon-heartbeat-daemon.service -n 200 --no-pager"`

- 重启心跳守护：
  - `ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71 "sudo systemctl restart axon-heartbeat-daemon.service"`

- 查看 Agent 容器：
  - `ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71 "docker ps --format '{{.Names}}|{{.Status}}|{{.Image}}'"`

- 服务器侧健康快照：
  - `ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71 "python3 /home/ubuntu/axon-agent-scale/scripts/axonctl.py lifecycle-report --state-file /home/ubuntu/axon-agent-scale/state/deploy_state.json --network /home/ubuntu/axon-agent-scale/configs/network.yaml"`

## 10. 继续优化时的风险点与建议

- 风险 1：本地跑守护会和服务器守护形成双写/冲突  
  - 建议：只保留服务器 `systemd` 常驻，本地只做一次性调试。

- 风险 2：改了本地 `axonctl.py` 但未同步服务器，导致“代码与线上行为不一致”  
  - 建议：每次改动后走固定发布动作：`scp -> systemctl restart -> lifecycle-report`。

- 风险 3：`agent_worker.py` 容器日志与链上真实心跳易混淆  
  - 建议：把链上活性判断统一到 `heartbeat-daemon + lifecycle-report + onchain getAgent.isOnline`。

## 11. 下一任务启动前的最小检查清单

- 确认 `axon-heartbeat-daemon.service` 为 active
- 确认 `docker ps` 中 9 个 agent 容器都在
- 跑一遍服务器侧 `lifecycle-report`
- 抽查链上 `online_count` 是否 9/9
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

## 13. 关键口径补充（下阶段修复必读）

- 注册 Burn20 机制口径  
  - AXON 的 20 AXON 燃烧是 `register(payable)` 内置流程，不存在“单独补交 burn 接口”。
  - 已注册地址若声誉异常，应走审计与生命周期修复，不应假设可补一笔 burn 修复历史。

- 官方 skill 口径风险待回报  
  - 已识别“注册是否需要单独 burn”存在误导风险，后续需要整理证据并向官方 GitHub 反馈。
  - 新任务中如果涉及注册流程说明，必须以官方代码口径优先，不沿用旧说法。

- 并行守护系统说明（已更新：qqclaw daemon 已停用）
  - scale-kit 心跳守护：`axon-heartbeat-daemon.service`（`/home/ubuntu/axon-agent-scale`）— **唯一**心跳来源
  - QQClaw 独立守护：`axon-agent-qqclaw.service` — **已停用并 disabled**，qqclaw 私钥已导入 scale-kit
  - qqclaw-validator 无 Docker 容器，由 heartbeat-daemon 直接通过 RPC 发链上心跳

## 14. 本次只读生产快照（2026-03-27）

- 快照时间：`2026-03-27 CST`
- 核对结果：`axon-heartbeat-daemon.service=active`，`axon-agent-qqclaw.service=inactive (disabled, migrated)`
- Docker 结果：9 个 agent 容器 + 1 个 `axon-node` 容器均在运行（qqclaw-validator 无容器）
- 生命周期结果：待 `lifecycle-report` 确认
- 详细记录：`docs/ops/prod_snapshot_2026-03-25.md`（上次）

## 15. 统一发布入口（已落地）

- 发布脚本：`scripts/release_deploy_verify.sh`
  - 流程：`push -> deploy -> restart -> verify`
  - 演练命令（不改线上）：`scripts/release_deploy_verify.sh --dry-run --allow-dirty --skip-tests`
  - 正式命令：`scripts/release_deploy_verify.sh`
- CI 基线：`.github/workflows/unittest.yml`
  - 触发：push/pull_request 到 `main`
  - 校验：`python -m unittest tests.test_axonctl -q`
