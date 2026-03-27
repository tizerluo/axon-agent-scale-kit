# AXON 生产快照（只读）

- 采集时间（本地）：`2026-03-25 18:23:30 CST`
- 本地仓库：`/Users/tizerluo/Cursor2026/AXON/axon-agent-scale-kit`
- 本地分支/提交：`main` / `abcdea3`
- 远端同步：`HEAD == upstream/main`

## 1. 服务器连通与服务状态

命令：

```bash
ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71 \
  'date "+%Y-%m-%d %H:%M:%S %Z"; hostname; whoami; \
   systemctl is-active axon-heartbeat-daemon.service; \
   systemctl is-active axon-agent-qqclaw.service'
```

输出：

```text
2026-03-25 18:23:35 CST
VM-0-13-ubuntu
ubuntu
active
active
```

## 2. Docker 运行态

命令：

```bash
ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71 \
  'docker ps --format "{{.Names}}|{{.Status}}|{{.Image}}" | sort'
```

输出：

```text
axon-agent-agent-001|Up 8 hours|python:3.11-slim
axon-agent-agent-002|Up 8 hours|python:3.11-slim
axon-agent-agent-003|Up 8 hours|python:3.11-slim
axon-agent-agent-004|Up 8 hours|python:3.11-slim
axon-agent-agent-005|Up 8 hours|python:3.11-slim
axon-agent-agent-legacy-006|Up 4 hours|python:3.11-slim
axon-agent-agent-legacy-007|Up 4 hours|python:3.11-slim
axon-agent-agent-legacy-008|Up 4 hours|python:3.11-slim
axon-node|Up 6 days|debian:trixie-slim
```

> 注意：当时仅纳管 8 个 agent，agent-009 和 qqclaw-validator 尚未加入。

## 3. Lifecycle 健康报告

命令：

```bash
ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71 \
  'python3 /home/ubuntu/axon-agent-scale/scripts/axonctl.py lifecycle-report \
    --state-file /home/ubuntu/axon-agent-scale/state/deploy_state.json \
    --network /home/ubuntu/axon-agent-scale/configs/network.yaml'
```

核心结果：

```json
{
  "ok": true,
  "summary": {
    "HEALTHY": 8,
    "DEGRADED": 0,
    "FAILED": 0
  },
  "current_block": 137336
}
```

结论：

- 生产守护正常在线；
- 当前纳管 8 个 agent 全健康；
- 可作为本周后续变更的回归基线。

## 4. 收口执行记录（2026-03-25）

- 提交信息：
  - commit: `64c6e438ef7e77e05134626be46b3c15f766268d` (`64c6e43`)
  - message: `chore: finalize handover closure with release automation and CI baseline`
- CI 结果：
  - workflow: `unittest`
  - run: `https://github.com/6tizer/axon-agent-scale-kit/actions/runs/23536993812`
  - status: `completed/success`
  - completed_at: `2026-03-25T10:44:03Z`
- 真实发布演练（非 dry-run）：
  - execute_time: `2026-03-25 18:46:19 CST`
  - script: `scripts/release_deploy_verify.sh`
  - result: `success`
- 发布后验收：
  - `systemctl is-active axon-heartbeat-daemon.service` => `active`
  - agent container count (`docker ps` with prefix `axon-agent-agent-`) => `8`
  - lifecycle summary => `HEALTHY=8, DEGRADED=0, FAILED=0`
  - lifecycle block => `137573`
