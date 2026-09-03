# pilot_v4（CM 接线首批）分析记录

时间：2026-09-03。范围：修订 4（CM 按需接线）下 Sphinx `solo` 与 `fixed_gpt-5.6-sol` 两 run，与 pilot_v3 同题同 arm 无 CM 的结果作探索性对照。审计 `pilot_v4.cm3.audit.json` PASS（41 调用、909,436 已知 token）；逐 agent 账 `pilot_v4.agent_accounting.cm3/`。gate_revision4（160 测试、50 源）。

## CM 机制实证（本批首次真实运行）

- 触发 7 次、成功压缩 3 次（solo 2 次、sol 1 次），历史 est token 削减 **53%–57%**（32,009→15,020、26,253→11,164、24,631→10,877）。
- 切分落在 user 消息边界，原生 tool 配对无损；压缩前原文落盘 `cm/<call_id>.before.json`。
- 归因正确：context_managers.csv 首次出现 `was_instantiated=True` 的真实 CM 行（trigger_agent_id 指向触发 node），不挂 agent handle；mechanisms.json CM 从 `not_integrated` 变为 `integrated_on_demand`。
- 4 次 GLM socket 超时（2 次在 CM 调用上）全部按纪律处理：静默降级不压缩、usage 保留 null、事件记录 `cm_call_failed`。
- 旧批次（pilot_v2/v3）在 CM 感知的审计器下重审全部 PASS，零新观察。

## 与 pilot_v3 同 arm 对照（单 run，仅方向性）

| arm | 版本 | 官方 | agent 调用 | CM 调用 | Lead/agent token | 结局 |
|---|---|---|---:|---:|---:|---|
| solo | v3 无 CM | 未过 | 15 | 0 | 512,199 | budget_exhausted |
| solo | v4 CM | 未过 | 15 | 4(2成2超时) | **401,667** | budget_exhausted |
| fixed_gpt-5.6-sol | v3 无 CM | **通过** | 25 | 0 | 561,397（全队） | completed，worker 2/2 采纳 |
| fixed_gpt-5.6-sol | v4 CM | 未过 | 19 | 3(1成2超时) | 425,065（全队 agent） | worker 双双 budget_exhausted |

**正面**：solo 的 Lead 输入降 **21.5%**（压缩直接兑现）；机制、账务、归因、降级全部按设计工作。

**负面（关键发现）**：
1. **CM 调用本身不便宜**：单次成功压缩 ≈ 23k–32k token，其中 reasoning 占 8.8k–20k——CM 路由沿用了 GLM thinking-enabled 设置，摘要任务不需要重推理。solo 净节省仅约 11%（省 110k 输入、花 55k+2×未知 CM 成本）。
2. **CM 与准入预算的相互作用是负的**：每次调用预留 `est+32768`，3 次 CM ≈ +150k committed；2 次超时 CM 的预留永久 held（usage unknown 不能释放）。600k 准入上限被 CM 挤占 → sol 组 worker 在第 6/7 次调用即 `TOKEN_BUDGET_EXHAUSTED`/`DRAIN_IN_PROGRESS`，交付未完成——**同 arm 从 v3 的通过退化为未过**。CM 的本意是省预算，当前实现反而加剧预算压力。
3. GLM socket 超时率在本时段偏高（4 次），与 CM 无关但放大了上述挤占。

## 结论与待办

CM"能不能工作"已证实：触发、压缩、记账、归因、降级全部正确。CM"值不值得开"当前为否——成本结构（thinking 开销）与预算耦合（预留挤占）两处需要修正后才值得再测：

1. CM 调用关闭 thinking（或换摘要友好配置），预期单次成本降 2–3 倍；
2. CM 预留 slack 从 +32768 降到 +8192，或给 CM 独立小额子预算、不计入 agent 准入池；
3. 超时 CM 的 held 预留需要有界衰减或显式核销事件。

以上属策略调参，未实施；需用户确认后作为修订 5 处理。本批不因此改写 pilot_v2/v3 任何结果。
