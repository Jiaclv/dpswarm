# 为什么先前没有子 agent

核对对象是旧 `swe_verified_20260903/pilot_v2`：5 道题、10 个完整 run。真实调用共 106 次，3,355,362 token，全部来自 Sol Lead。可选 DPswarm 的 5 个 run 没有 delegate 工具调用，没有 CP worker 准入，也没有子模型请求；不是“已经调用但账单漏记”。

旧 runner.py 的 Lead prompt 明确写了 start alone、optional capability、Do not delegate just because tools exist，并反复提醒 handoff overhead 和 shared budget。它没有要求先做可审计的复杂度/可分解性判断，也没有“除简单任务以外必须组队”的判定规则。因此，它实现的是保守的自由选择，与你希望复杂任务自动启用协作的策略并不等价。

这是代码和日志能确认的原因边界：提示词与执行策略允许全部单 agent 完成。共享 600k token 预算及协作费用提示可能进一步抑制派生，但没有独立 prompt 消融，不能定量断言是哪个措辞导致。题目都来自真实 SWE issue，也不意味着每题都必须能从并行中获益；自动策略应判断独立实现、测试、调查等子工作是否有价值。

还有实验接线范围问题：只暴露 DERIVE，未暴露 SPLIT/FISSION，CM 未接入，所以该批次从机制上就不可能产生分裂、裂变或真实 CM 用量。不能把这些项的缺失归因于模型选择，也不能把它当完整 DPswarm 效果。

当前修正按用户要求固定团队：Sol Lead + 两个指定候选模型 worker，在 Lead 首次请求前由实验协议执行真实 CP DERIVE。分别负责生产修复和独立回归测试，独立环境、相同原始 issue。来源记 experiment_protocol，逐 agent/call 记录真实请求、usage、时间和交付；未发生真实 worker 请求会触发停止。此轮先回答“真实团队是否能工作、五个子模型各有什么表现”。它不验证自动组队策略，也不改变产品默认单 agent。

后续自动启用机制需另测：在 Lead 早期检查点输出结构化决策与证据（任务范围、独立子任务、预期收益、剩余预算），简单或无法分解的任务可以单 agent，其余交给有容量/权限约束的协作准入。日志同时保留 requested/admitted/activated/actual_call/finished，区分模型未请求、控制面拒绝、环境失败与调用失败。策略应在冻结任务集上与固定团队和单 agent 对照，不以这两题结果反向调阈值。
