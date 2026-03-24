# Tasks
- [ ] Task 1: 定义资金触发扩容的数据契约与状态机
  - [ ] SubTask 1.1: 定义 `scale_request` 字段与状态流转
  - [ ] SubTask 1.2: 定义 `scale_plan` 数据结构与预算字段
  - [ ] SubTask 1.3: 定义统一执行报告与错误码

- [ ] Task 2: 实现到账监听与前置校验能力
  - [ ] SubTask 2.1: 实现链ID、RPC可用性与确认块数校验
  - [ ] SubTask 2.2: 实现到账金额阈值判断与超时失败处理
  - [ ] SubTask 2.3: 输出 `PENDING_FUNDS -> FUNDED/FAILED` 状态变更

- [ ] Task 3: 实现预算生成与批次扩容执行
  - [ ] SubTask 3.1: 实现目标数量对应的预算拆分策略
  - [ ] SubTask 3.2: 实现幂等执行器，跳过已完成步骤
  - [ ] SubTask 3.3: 实现失败项重试与失败清单输出

- [ ] Task 4: 实现链上+服务双通道核验与修复入口
  - [ ] SubTask 4.1: 聚合链上注册/质押状态
  - [ ] SubTask 4.2: 聚合 systemd 服务状态与心跳状态
  - [ ] SubTask 4.3: 生成 `SUCCESS/PARTIAL/FAILED` 报告并给出 repair 建议

- [ ] Task 5: 完成技能化触发映射与端到端验证
  - [ ] SubTask 5.1: 定义自然语言金额/数量抽取与参数映射
  - [ ] SubTask 5.2: 接入标准流程 `validate -> fund-check -> scale -> status -> repair`
  - [ ] SubTask 5.3: 完成“1500 AXON 扩容10个Agents”端到端验收

# Task Dependencies
- Task 2 depends on Task 1
- Task 3 depends on Task 1 and Task 2
- Task 4 depends on Task 3
- Task 5 depends on Task 2, Task 3, and Task 4
