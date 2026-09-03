# pilot_v6/v7（修订 6：CM 调参第二轮）横向对比分析

时间：2026-09-03。范围：Sphinx 四 arm 在修订 6 CM 配置（阈值 12k est、keep_recent 4、CM max_tokens 2048、brief 2000）下的重跑：pilot_v6（solo、fixed_glm-5.3、fixed_gpt-5.6-luna）+ pilot_v7（fixed_glm-5.3-flash 补跑）。gate_revision6（161 测试）；四个旧批次在修订 6 审计器下全部 PASS（CM 期望按各批 manifest limits 取历史真值）。审计：pilot_v6 3 run 84 调用 1,333,037 已知 token PASS；pilot_v7 1 run 28 调用 344,581 全已知 PASS。

## 横向对比总表（同 arm 跨配置；各批独立 manifest，逐批引用）

| arm | v3 无 CM | v4 未调参 CM | v5 调参① CM | v6/v7 调参② CM | 调参② 结论 |
|---|---|---|---|---|---|
| solo | 未过 / 15 调用 / 512k | 未过 / 15+4CM / ~457k+2未知 | — | 未过 / 18+10CM / **473k** | −8% 成本，结局未变 |
| glm-5.3 | 通过 / 28 / 409k | — | 通过 / 27+1CM / 472k | 通过 / 23+5CM / **309k** | **−24% vs v3，该 arm 全配置最低** |
| flash | 通过 / 25 / null(1未知) | — | 通过 / 25+3CM / null(1未知) | 通过 / 22+6CM / **344,581（全已知）** | 首次无 unknown，成本可严格比较 |
| luna | **未过** / 24 / 576k 耗尽 | — | — | **通过** / 24+4CM / **551k，双 worker 完成，team valid=true** | **翻转：失败→通过** |

## CM 机制指标（本轮 25 次压缩）

- **触发密度**：每 run 4–10 次（v5 为 1–3 次）——阈值 12k 生效，solo 的 Lead 全程保持台阶状低水位。
- **削减率**：min/avg/max = 25%/48%/78%（v5 均值 30%、含 2% 反例）——摘要上限 2048 消除了冗长摘要反例。
- **CM 成本**：每 run 34k–76k（按压缩次数线性）；单次约 6–9k，max_tokens 上限有效。
- worker 终态改善：luna 双 worker completed（该 arm 首次）；glm-5.3 worker-2 completed；flash worker-1 completed。GLM worker 完成态在 v5 出现 1 例后本轮再增 3 例——与 CM 的相关性仍属观察，不归因。

## 判读（对照事先声明的标准）

1. **经济性成立**：glm-5.3 −24%、solo −8%；luna 通过且低于 v3 失败成本。压缩台阶在 prompt 曲线图可见（solo 曲线 v6 vs v3）。
2. **续航假设成立（强信号）**：luna 由 budget_exhausted 失败翻转为 completed 通过，且 team_execution_valid=true（worker 交付被正式采纳）。这正是 CM 设计目标——"给预算受限的 run 续航"。
3. 边界：单 run 方差仍在（solo 未因 CM 通过；flash/luna 单例翻转未复现样本）；结论为方向性，不宣称统计显著。

## 结论

修订 6 把 CM 从"无害"推进到"**方向性有益**"：预算受限场景（luna）翻转结局，宽裕场景（glm-5.3）显著降本，最差情况（solo）小幅降本不伤结局。CM 参数链 v4→v5→v6 的每次变更都有独立批次证据与审计，构成完整的调参证据链。

累计实验消耗：pilot_v6+v7 共 112 调用、1,677,618 已知 token；全实验约 619 次调用、约 13.5M 已知 token +3 次 unknown。
