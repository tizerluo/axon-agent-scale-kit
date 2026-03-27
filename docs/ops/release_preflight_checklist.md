# 发布前检查清单（防混乱版）

## 0. 使用边界（红线）

- 禁止本地 `heartbeat-daemon` 与服务器 `axon-heartbeat-daemon.service` 同时运行。
- 禁止“只 scp 不 push”直接覆盖线上。
- 禁止跳过服务器验收直接宣告完成。

## 1. 本地发布前

- 当前分支与目标提交明确（建议记录 commit SHA）。
- `git status` 可解释（无未知脏改动进入发布）。
- 本地回归通过：`python3 -m unittest tests.test_axonctl -q`。
- GitHub Actions `unittest` 工作流状态为通过（若本次变更触发了 CI）。
- 配置已核对：`configs/network.yaml`、`configs/agents.yaml`、`configs/runtime/hosts.runtime.yaml`（或你的 `configs/runtime/*.yaml` 私有配置）。

## 2. 发布动作（固定顺序）

1. 优先执行统一脚本：`scripts/release_deploy_verify.sh`。
2. 若需手工执行：`git push` 到 GitHub（确保可追溯）。
3. 服务器同步到该 commit（不要跨版本复制脚本）。
4. 重启守护：`sudo systemctl restart axon-heartbeat-daemon.service`。

## 3. 服务器验收（必须）

- `systemctl is-active axon-heartbeat-daemon.service` 为 `active`。
- `docker ps` 中目标 agent 容器均在运行。
- 执行 lifecycle：

```bash
python3 /home/ubuntu/axon-agent-scale/scripts/axonctl.py lifecycle-report \
  --state-file /home/ubuntu/axon-agent-scale/state/deploy_state.json \
  --network /home/ubuntu/axon-agent-scale/configs/network.yaml
```

- 验收结果记录到 `docs/ops/prod_snapshot_YYYY-MM-DD.md`。

## 4. 回填记录

- 记录发布 commit、发布时间、操作者。
- 记录验收摘要（HEALTHY/DEGRADED/FAILED）。
- 记录异常与处置结论（如有）。
