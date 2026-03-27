# AXON Agent 注册燃烧与声誉修复计划

## 0. 执行状态（已完成实施）

- 计划状态：**已完成实施**（当前范围内已落地并上线）。
- 已完成要点：`register(payable)` 主路径、`registration-audit`、`lifecycle-report` 审计字段、回归测试与服务器验收。

你的判断：
001-005 自动分流修复命令化
结论：已完成（2026-03-27 验证）。
原因：agent-001~005 当前 reputation=7，agent-legacy-007/008=27，
所有 10 个 agent 链上 registered=true, staked=true, 在线。
注册燃烧修复计划已成功落地，所有 agent 正常。

## 1. 官方仓库确认到的“正确修复方法”

基于 `axon-chain/axon` 官方代码，注册与燃烧的正确路径是：

1. 必须调用 `IAgentRegistry.register(string,string)`（预编译 `0x0000000000000000000000000000000000000801`），并以 `payable msg.value` 发送质押金额。
2. 燃烧 `20 AXON` 不是单独交易，而是链上 `Register/RegisterFromPrecompile` 内部自动执行。
3. 只要走了正确注册路径，事件里会带 `burned` 字段，且 Agent 会被正式写入链上状态。

对应官方证据点：

* `contracts/interfaces/IAgentRegistry.sol`：`register` 为 `payable`，注释明确“initial stake 中 20 AXON will be burned”。

* `precompiles/registry/registry.go`：`register()` 把 `msg.value` 作为 `Stake` 转给 `RegisterFromPrecompile`。

* `x/agent/keeper/keeper.go`：`RegisterFromPrecompile` 内部执行 `BurnCoins(... RegisterBurnAmount ...)`。

* `x/agent/types/params.go`：默认 `RegisterBurnAmount=20`。

结论：修复方向不是“增加单独 burn 接口”，而是“确保我们的工具链始终走官方 register(payable) 主路径，并可证明该路径被执行”。

***

## 2. 当前项目的根因判断

对 `axon-agent-scale-kit` 现状判断：

1. `scripts/axonctl.py` 的 `execute_scale` 目前仅写本地状态（`registered/staked=True`），没有发起链上 `register` 交易。
2. 历史真实注册逻辑在 `scripts/archive/register_five_onchain.py`，但该脚本已归档，不在主流程。

   * 这会直接影响修复有效性：如果不把注册能力迁回主链路，后续扩容仍可能只写本地状态，继续复发“未形成可审计链上注册证据”的问题。
3. 因此 001-005 很可能存在“本地标记已注册，但链上注册与燃烧证据不可追溯/不可重复验证”的产品缺口。

补充：v2 声誉为 epoch 结算，且 L1/L2 毫分制同步到 legacy `uint64` 时会截断，短周期内显示 `0` 也可能由结算节奏导致；需与“是否完成注册燃烧”分开诊断。

***

## 3. 修复目标（本次实施）

1. **注册一致性**：主流程中所有 Agent 注册都必须走 `0x0801 register(payable)`。
2. **燃烧可证据化**：每个注册结果要能输出并持久化“tx hash + receipt status + registered\_onchain + burn证据”。
3. **声誉诊断拆分**：将“注册问题”和“epoch声誉未上升”分离，避免误判。
4. **存量修复策略**：对 001-005 给出自动分流（未注册→补注册；已注册但声誉低→心跳/epoch观测；异常→repair建议）。

***

## 4. 详细实施步骤

### Phase A：实现官方 register 主路径（替换本地伪注册）

1. 在 `scripts/axonctl.py` 新增 `register-onchain-once` 与 `register-onchain-batch`（或并入现有 scale/deploy 管道），参数包含：

   * `--state-file`

   * `--network`

   * `--request-id` / `--agent`

   * `--stake-axon`

   * `--wait-receipt-timeout`

2. 交易构造严格对齐官方接口：

   * 合约地址固定 `0x...0801`

   * ABI 使用 `register(string,string)`、`isAgent(address)`、`getAgent(address)`

   * `value=stake_wei`（payable）

3. `execute_scale` 不再直接把 `registered/staked=True` 写死；改为：

   * 先远端容器部署

4. 将 `scripts/archive/register_five_onchain.py` 中已验证可用的交易构造逻辑“迁入正式命令”，并把归档脚本仅保留为历史参考，避免双入口漂移。

### Phase B：落地“燃烧证据链”

1. 每次注册后保存：

   * `register_tx_hash`

   * `register_receipt_status`

   * `registered_onchain`（`isAgent`）

   * `agent_id/reputation/is_online`（`getAgent`）
2. 增加事件解析（若日志可解）或最小可行证据：

   * 交易成功 + 已注册 + 官方代码路径证明 burn 内置。
3. 在 `lifecycle-report` 增加字段：

   * `registration_path=precompile_register_payable`

   * `burn20_expected=true`

   * `burn_evidence_level`（`receipt_only` / `event_decoded`）。

### Phase C：001-005 存量修复流程

1. 新增 `registration-audit`：

   * 输入：agent 列表（默认 001-005）

   * 输出：`isAgent`、历史注册tx、当前reputation、最后heartbeat、分类结论。
2. 自动分流：

   * **未注册**：执行补注册（会触发官方内置 burn）。

   * **已注册且声誉=0**：执行 `heartbeat-batch` 并跨多个 epoch 观测，验证是否按 v2 规则增长。

   * **已注册但长期无增长**：输出 gate 原因（validator状态、challenge参与、epoch窗口、离线惩罚）。
3. 仅在“确认为历史错误注册且必须重做身份”时，提供可选方案：

   * 新地址重注册（代价：身份迁移）

   * 旧地址注销后重注册（受冷却周期约束）。

### Phase D：文档与可回归

1. README 增加“官方注册与燃烧机制”章节：

   * 明确“无单独 burn 交易”

   * 明确“必须 register(payable)”

   * 给出审计命令序列。
2. 新增/更新测试：

   * 注册交易构造测试

   * 回执失败分支

   * 已注册跳过幂等

   * audit 分类逻辑测试。
3. 验收脚本：

   * 对 001-005 输出前后对比报表（注册状态、心跳、声誉）。

***

## 5. 验收标准

1. `execute_scale` 路径中不再出现“仅本地标记 registered”的行为。
2. 任一新注册 Agent 均可提供可追溯 tx 证据，并确认 `isAgent=true`。
3. 001-005 的审计结论可复现、可解释，并有明确修复动作或观察结论。
4. 回归测试通过，且文档与命令口径一致。

***

## 6. 风险与回滚

1. 链上交易失败风险：通过 dry-run + 小批次 + 幂等检查（`isAgent`）降低。
2. 误把“epoch结算慢”当成“注册失败”：通过 `registration-audit` 分层诊断规避。
3. 若新流程异常：保留现有 deploy/heartbeat 命令，注册模块可独立回滚到只读审计模式。

***

## 7. 针对 001-005 的具体修复方案与影响

### 当前状态（2026-03-27 更新）

| Agent | Reputation | 状态 |
|-------|-----------|------|
| agent-001 ~ 005 | 7 | ✅ 正常在线，epoch 持续累积 |
| agent-legacy-006 | 19 | ✅ 正常 |
| agent-legacy-007 | 27 | ✅ 最高 |
| agent-legacy-008 | 27 | ✅ 最高 |

**结论：注册燃烧修复计划已成功落地。**
所有 001-005 均已完成官方 `register(payable)` 路径，声誉从 epoch 持续增长中，
不再是当前故障点，无需进一步干预。

### 修复方案（按顺序执行）

1. **先审计，不直接重注册**\
   对 001-005 逐个查询：

   * `isAgent(address)`

   * `getAgent(address)`（`agentId/reputation/isOnline`）

   * 最近心跳与挑战 gate 状态

   * 本地 state 是否存在“仅本地标记已注册”。

2. **按审计结果分流处理**

   * **类型A：链上未注册**\
     走新的 `register-onchain-batch`，触发官方内置 Burn20，并写入注册证据。

   * **类型B：链上已注册但声誉仍为0**\
     不重注册；执行心跳连续观测跨 epoch，验证是否从 0 开始爬升（v2 毫分制→整数显示存在滞后）。

   * **类型C：链上已注册但状态异常（长期离线/被惩罚）**\
     执行 lifecycle repair，并输出 gate 原因（validator/窗口/challenge/离线惩罚）。

3. **最后统一产出 001-005 修复报告**\
   每个 Agent 输出：注册状态、是否完成官方 register 路径、证据 tx、当前声誉、后续动作。

### 影响评估

1. **对线上可用性的影响**：低到中。

   * 类型A 会新增注册交易与gas消耗；

   * 类型B/C 主要是诊断与心跳修复，不影响身份连续性。
2. **对身份连续性的影响**：默认不破坏。

   * 仅在必须“重做身份”时（例如放弃旧地址）才会有身份迁移影响。
3. **对资金的影响**：会增加可观测的链上成本（注册 value 与 gas）；但这是合规注册的必要成本。
