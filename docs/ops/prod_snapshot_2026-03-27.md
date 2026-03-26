# AXON 生产快照（只读）

- 采集时间（本地）：`2026-03-27 CST`
- 本地仓库：`/Users/tizerluo/Cursor2026/AXON/axon-agent-scale-kit`
- 本地分支/提交：`docs/collaboration-workflow` / `6cc032c`
- 远端同步：`HEAD == origin/feature/cursor-dev`

## 1. 问题修复记录（2026-03-27）

### 问题描述

`agent_wallet_import` 导入钱包时未写入 `container_name`，
导致 agent-001 ~ agent-005 等 5 个 agent 在 heartbeat-daemon
状态中该字段为空。

### 根因

`axonctl.py` `_agent_wallet_import_to_state()` 函数在 reused 和
new/updated 两个分支中均未写入 `container_name`，该字段只在
`remote_deploy()` 中写入。

### 修复

- **代码修复**（PR #1）：
  - 提交：`b96fc2d` — `fix(state): populate container_name on agent_wallet_import`
  - 改动：`scripts/axonctl.py` `_agent_wallet_import_to_state()`
    - 第 1462 行：reused 分支补写 `container_name`
    - 第 1480 行：new/updated 分支补写 `container_name`
  - 测试：`tests/test_axonctl.py` 新增 3 个回归用例
    - `test_wallet_import_preserves_container_name_template`
    - `test_wallet_import_batch_preserves_container_name`
    - `test_wallet_import_reused_preserves_container_name`
  - PR: https://github.com/6tizer/axon-agent-scale-kit/pull/1

- **数据修复**（服务器已完成）：
  - 对 agent-001 ~ agent-005 执行 `import-wallet` 命令回填 `container_name`
  - `state/deploy_state.json` 中 5 个 agent 均已补全该字段

### 后续动作

- [ ] 等待 6tizer Merge PR #1
- [ ] Merge 后：服务器执行 `git pull origin main`，重启 heartbeat-daemon
