# pilot_v8/v9 总分析：团队装配 CM + 异构波次 + DeepSeek 接入

时间：2026-09-03。范围：pilot_v8（16 run：5 solo + 5 fixed + 6 hetero，修订 7 团队装配 CM）+ pilot_v9（3 run：deepseek-v4-flash 的 solo/fixed/hetero 槽位，修订 8）。审计均 PASS（v8：429 调用 6,385,984 已知 +2 未知；v9：84 调用 989,252 已知 **零未知**）。gate_revision7/8（166/168 测试）。全部旧批（v2–v7）在修订 8 审计器下兼容 PASS。

## 一、通过矩阵（Sphinx-8035，同一装配条件）

| 组 | 通过/总数 | 明细 |
|---|---|---|
| **hetero 异构** | **7/7（100%）** | terra__luna ✅ terra__glm ✅ glm__luna ✅ terra__flash ✅ sol__glm ✅ glm__sol ✅ terra__deepseek ✅ |
| fixed 同构 | 3/6 | glm ✅ flash ✅ luna ✅ sol ❌ terra ❌ deepseek ❌ |
| solo | 3/6 | sol ✅ terra ✅ luna ✅ glm ❌ flash ❌ deepseek ❌ |

**异构 7/7 是本轮最强信号**：每个含至少一个 GPT worker 的异构组合全部通过；最便宜的三个通过 run 全是异构（sol__glm 368k、terra__flash 374k、terra__glm 408k）。对照 NEXT_EXPERIMENT_PLAN §5 的假设：#1 同族互补✅、#2 glm 换回归角色✅、#3 luna 救 glm✅、#4 glm vs flash 差异（都过，flash 更省）✅、#5 最强实现+便宜回归✅（全实验最省通过）、#6 最强回归救 glm✅。

## 二、团队装配 CM 专项（核心假设检验）

1. **worker 重复探索显著下降**（两名 worker 全周期 input 合计）：
   - glm-5.3：v2 150k → v6 72k → v8 75k（降 ~50%）
   - flash：v2 161k → v8 45k+未知（大幅下降）
   - luna：v3 366k → v8 304k（降 17%）
   - deepseek（v9 首测）：108k
2. **装配链路全部真实运行**：每团队 run 均有侦察蒸馏（scout_distilled）+ 2 个装配包（带 manifest 落盘）；worker 首调即可见侦察纪要。
3. **CM 成本占比小**：每 run 5–10 次 CM、总量 30–76k，占 run 预算 <15%。
4. **局限**：所有团队 run 的 worker 仍无一正式 completed（8 次调用上限内），team_execution_valid 全 false——装配改善了起点（探索成本降、通过率高），没有改变"worker 写不完、Lead 整合"的模式。

## 三、DeepSeek V4 Flash 接入评估（修订 8）

- **可靠性（接入动机）完全验证**：84 次调用 **零超时、零未知用量**（对照 GLM flash 在 v4–v8 累计 6 次提供方读超时）。
- 能力表现：solo 未过（254k 耗尽）、fixed 未过（326k 耗尽）、hetero 与 terra 搭档**通过**（409k）——模式与 glm 系一致：单干/同构双 deepseek 弱，作异构协作者可用。
- 成本极低（off-peak $0.22/M 输入），CM 切到 deepseek 后压缩调用同样稳定。

## 四、判读边界

- 单 run/arm、单任务：方向性结论，不排模型名次、不称统计显著。
- v8/v9 与 v3/v6 是**不同条件**（装配语义+启动顺序），只在"演进证据链"意义上并列。
- solo glm-5.3-flash 因提供方超时仅 7 调用即止，其"未过"不完整。

## 结论

1. **用户定义的 CM 语义（团队装配）在真实 benchmark 上成立**：探索成本降、通过率提升（尤其异构 7/7）、机制全链路可审计。
2. **异构精选组合是当前最优配置**：三个最省通过全是异构；"GPT 实现 + 便宜模型回归"（sol__glm 368k）是全实验该题最低成本通过。
3. **DeepSeek 替换达成目的**：同价位量级下零基础设施事故。

累计实验：v8+v9 共 513 调用、约 7.37M 已知 token；全实验约 1561 次调用、约 28.9M 已知 token +5 未知。
