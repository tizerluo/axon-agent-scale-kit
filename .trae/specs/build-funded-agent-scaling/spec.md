# 资金触发型日常扩容 Spec

## Why
当前扩容流程偏手工，无法把“到账资金”与“扩容执行”绑定成可重复闭环。需要把“收到 1500 AXON 后扩容 10 个 Agents”沉淀为标准化、可验证、可恢复的流程。

## What Changes
- 新增“扩容请求”模型，支持目标数量、最小到账额、确认块数、超时策略。
- 新增“到账监听与确认”能力，基于 AXON 主网参数完成金额与链环境校验。
- 新增“预算计算与批次计划”能力，自动生成扩容批次与资金分配。
- 新增“执行流水线”能力，串联注册、质押、服务启动与幂等状态写入。
- 新增“结果核验报告”能力，汇总链上状态与服务状态。
- 新增“技能触发映射”规则，将自然语言请求映射为标准扩容请求。

## Impact
- Affected specs: 扩容请求管理、资金监听、扩容执行、运行状态观测、技能触发器
- Affected code: `scripts/axonctl.py`, `scripts/agent_worker.py`, `configs/*.yaml`, `templates/axon-agent@.service.j2`, `state/*.json`, `.trae/skills/*`

## ADDED Requirements
### Requirement: 资金触发扩容请求
系统 SHALL 支持创建资金触发型扩容请求，并在请求生命周期内追踪状态。

#### Scenario: 创建请求成功
- **WHEN** 用户提交“1500 AXON 扩容 10 个 Agents”的请求
- **THEN** 系统创建唯一 `request_id`，状态为 `PENDING_FUNDS`，并记录目标数量与最小到账额

#### Scenario: 请求参数非法
- **WHEN** 目标数量小于 1 或最小到账额小于质押最低预算
- **THEN** 系统拒绝创建请求并返回字段级错误信息

### Requirement: 到账确认与链环境校验
系统 SHALL 在执行扩容前完成到账确认、链ID校验与确认块数校验。

#### Scenario: 到账确认通过
- **WHEN** 监听地址收到不低于最小到账额的转账且确认块数达到阈值
- **THEN** 请求状态更新为 `FUNDED`

#### Scenario: 到账不足或超时
- **WHEN** 到账金额不足或超过超时窗口仍未满足条件
- **THEN** 请求状态更新为 `FAILED`，并记录失败原因

### Requirement: 自动生成扩容计划
系统 SHALL 基于目标数量和预算策略生成可执行扩容计划。

#### Scenario: 计划生成成功
- **WHEN** 请求进入 `FUNDED`
- **THEN** 系统生成 `scale_plan`，至少包含 Agent 名单、批次划分、预算占用与并发参数

### Requirement: 幂等扩容执行
系统 SHALL 以幂等方式执行注册、质押、服务启动，并可对失败项重试。

#### Scenario: 重复执行同一请求
- **WHEN** 同一 `request_id` 被再次执行
- **THEN** 系统跳过已完成步骤，仅执行未完成或失败步骤

#### Scenario: 部分步骤失败
- **WHEN** 某些 Agent 在执行中失败
- **THEN** 系统继续执行其他 Agent，并输出失败清单与可重试标记

### Requirement: 双通道交付核验
系统 SHALL 在扩容结束后同时校验链上状态与服务状态，并输出统一报告。

#### Scenario: 核验通过
- **WHEN** 10 个目标 Agent 全部完成链上注册/质押且服务处于 active
- **THEN** 报告状态为 `SUCCESS`，返回成功数量与实例明细

#### Scenario: 核验不通过
- **WHEN** 任一 Agent 链上状态或服务状态异常
- **THEN** 报告状态为 `PARTIAL` 或 `FAILED`，并提供 repair 建议

### Requirement: 技能触发到命令映射
系统 SHALL 支持将自然语言扩容请求映射为标准执行流程参数。

#### Scenario: 技能触发扩容
- **WHEN** 用户输入“我打 1500 AXON，扩容 10 个 Agents”
- **THEN** 系统提取金额与数量，按标准流程执行 `validate -> fund-check -> scale -> status -> repair`

## MODIFIED Requirements
### Requirement: 日常扩容流程
系统原有“手工触发扩容”流程修改为“资金前置确认 + 自动化执行 + 统一核验”流程。默认必须先满足到账条件，才允许进入扩容执行阶段。

## REMOVED Requirements
### Requirement: 无资金门禁的直接扩容
**Reason**: 该流程容易导致余额不足、执行中断和不可预测失败。  
**Migration**: 迁移到“创建扩容请求 -> 到账确认 -> 执行扩容 -> 输出报告”的统一流程。
