# pilot_v5（修订 5 调参后的 GLM CM 对照）分析记录

时间：2026-09-03。范围：Sphinx `fixed_glm-5.3` 与 `fixed_glm-5.3-flash` 两 run，CM 已按修订 5 调参（thinking disabled、socket 120s、预留 slack 8192）。对照基线为 pilot_v3 同 arm 无 CM。审计 `pilot_v5.glm.audit.json` PASS（56 调用、995,805 已知 token +1 未知）；逐 agent 账 `pilot_v5.agent_accounting.glm/`；gate_revision5（161 测试）。

## 结果对照（单 run，方向性）

| arm | 版本 | 官方 | agent 调用 | CM | 已知 token | worker 终态 |
|---|---|---|---:|---:|---:|---|
| glm-5.3 | v3 无 CM | 通过 | 28 | 0 | 409,224 | blocked×2 |
| glm-5.3 | **v5 CM** | **通过** | 27 | 1 | 472,197 | **completed×1** + budget_exhausted×1 |
| glm-5.3-flash | v3 无 CM | 通过 | 25 | 0 | null(1未知) | blocked×2 |
| glm-5.3-flash | **v5 CM** | **通过** | 25 | 3 | null(1未知) | budget_exhausted×1 + transport_error×1 |

## 修订 5 调参的实证效果

- **CM 成本大幅下降**：单次成功压缩 10,940 / 24,616 总计 token（v4 同指标 23k–32k/次）。thinking 关闭后 reasoning 开销从万级降到忽略不计；本轮 CM 调用零失败、零挤占事故（v4 的 sol 组退化未复现，两组均通过）。
- **压缩有效性参差**：4 次压缩削减 2%–49%。2% 的那次是摘要本身写得过长（无 thinking 的 flash 倾向冗长转述）——摘要长度没有上限约束，是下一个可调点。
- **socket 超时仍在普通 worker 调用上发生**（flash worker-2，transport_error），与 CM 无关，属提供方时段性不稳；unknown 纪律保持，预声明门禁按设计触发（本批 schedule 已尽，无实际影响）。
- **正面异常值**：glm-5.3 组 worker-1 首次以 `completed` 终态交付（全实验 GLM worker 首例）；其历史未被压缩（该组唯一一次 CM 触发方是 worker-2），不能归因于 CM，属单 run 方差，如实记录。

## 结论

1. 修订 5 达成目标：CM 从"有害"（v4 挤占致退化）变为"无害"（两组通过、零预算事故、成本降 ~60%）。
2. 但"有益"仍未证成：glm-5.3 组总 token 反而高 15%（472k vs 409k，含 CM 成本与 run 方差）；flash 组含 unknown 不可严格比较；压缩收益被摘要冗长稀释。
3. CM 的价值假设（worker 重复探索的解药）需要更高触发密度与更紧的摘要预算才可能兑现：阈值 24k est 在 GLM worker 的历史上只触发 1–3 次/run。

## 后续可调点（未实施）

- 摘要输出加上限（如 ≤2k est tokens），或给 CM 请求 max_tokens 收紧；
- 阈值下调或改为"增量触发"（每次超过即压），提高触发密度；
- CM 摘要质量抽样人工评估（零损约束是否被遵守）。

单 run 对照不支持统计结论；本记录与 pilot_v4 记录共同构成 CM 的探索性证据链。
