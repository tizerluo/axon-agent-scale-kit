# AXON 生产快照（2026-03-27，只读）

- 采集时间：`2026-03-27 CST`
- 本地仓库：`/Users/tizerluo/Cursor2026/AXON/axon-agent-scale-kit`
- 本地分支/提交：`docs/collaboration-workflow` / `cfadb3e`
- 远端同步：PR #2 (docs/collaboration-workflow, cfadb3e) 待 6tizer review；领先 origin/main (d88c68d) 4 个 commit

## 1. 健康状态摘要

**HEALTHY=9, FAILED=1**

所有 10 个 agent 均 registered=true, staked=true，链上心跳正常。

| Agent | Health | Registered | Staked | Reputation | 备注 |
|-------|--------|-----------|--------|------------|------|
| agent-001 ~ 005 | HEALTHY | ✅ | ✅ | ~7 | 首批 5 个 |
| agent-legacy-006 | HEALTHY | ✅ | ✅ | 19 | |
| agent-legacy-007 | HEALTHY | ✅ | ✅ | 27 | 最高 |
| agent-legacy-008 | HEALTHY | ✅ | ✅ | 27 | 最高 |
| agent-009 | HEALTHY | ✅ | ✅ | 16 | |
| qqclaw-validator | ❌ FAILED | ✅ | ✅ | 21 | 误报，见 §2 |

## 2. qqclaw-validator FAILED 误报根因与修复

### 症状

`lifecycle-report` 显示 qqclaw-validator 为 FAILED（reasons: `service_inactive`），
但链上查询 `last_heartbeat_block=164901`，心跳实际上**正常发送**。

### 根因

`scripts/axonctl.py` `heartbeat_once()` 函数在心跳交易成功时，
没有回写 `service_active=true`。

正常情况下 `service_active` 由 `remote-status` 命令通过检查 Docker 容器状态来更新，
但 qqclaw-validator 没有 Docker 容器（它的 agent daemon 独立于 scale-kit），
所以 `service_active` 永远是 `false`，导致 `lifecycle-report` 误判。

### 修复（PR #N）

```python
# scripts/axonctl.py heartbeat_once() 成功分支（约 line 897）
state["agents"][agent]["heartbeat_at"] = now_ts()
state["agents"][agent]["service_active"] = True  # ← 新增：即使没有容器也标记
state["agents"][agent]["last_heartbeat_block"] = tx["block_height"]
```

## 3. 服务器连通状态（block ~164499）

| Service | Status |
|---------|--------|
| axon-heartbeat-daemon.service | active (running) |
| axon-agent-qqclaw.service | inactive (dead, disabled) — 已迁徙到 heartbeat-daemon |
| axon-node 容器 | Up 19 hours |
| Agent 容器 | 10 个全部 Up |

## 4. 本次文档修复记录

| 文件 | 问题 | 修复 |
|------|------|------|
| `scripts/axonctl.py` | heartbeat_once 不回写 service_active | 新增 `service_active=True` |
| `state/deploy_state.json` | 缺 agent-009/legacy-006~008/qqclaw | 从服务器同步 |
| `.cursor/rules/axon-context.md` | 未说明 qqclaw 特殊情况 | 补充说明 |
| `docs/ops/prod_snapshot_2026-03-27.md` | 原内容完全错误（未注册） | 重写为真实状态 |
| `configs/agents.yaml` | 缺 agent-009 和 qqclaw-validator | 补充两个 agent 条目 |
| `docs/plans/axon-register-burn-reputation-fix-plan.md` | §7 "声誉仍为0" 过时 | 更新为 reputation=7 |
| `docs/ops/prod_snapshot_2026-03-25.md` | 路径/分支/状态过时 | 更新为当前值 |
| `docs/DEVELOPER_REFERENCE.md` §4 | qqclaw service 描述简略 | 更新为迁徙说明 |

## 5. qqclaw-agent 迁徙说明

- `qqclaw-validator` agent 已于 2026-03-27 完成迁徙
- 由 `axon-heartbeat-daemon.service` 统一维护心跳（safety measure）
- 旧 daemon `axon-agent-qqclaw.service` 已 disabled，内容归档到 `scripts/archive/`
- qqclaw 同时是验证节点（`axon-node` 容器），这部分不受影响，正常运行
