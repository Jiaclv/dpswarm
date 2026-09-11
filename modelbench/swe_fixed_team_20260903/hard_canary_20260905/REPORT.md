# 固定 Agent Team SWE 探索性评测

已完成 1/1 run。下列结果仅覆盖已保存 result.json 的运行；未完成不记失败。
冻结计划覆盖 1 道任务；每次运行的 Lead 与有序 worker 模型声明保留在 summary.json rows。
已声明 Lead 模型：gpt-5.6-sol；缺失声明保持未知。
固定派生和异构方向按各 run 的冻结声明核对；协议建立团队不证明模型自主请求派生。
这些任务上的探索性结果不能据此排列模型能力、宣称 SOTA、完整 DPswarm 效果或榜单成绩。

| Arm | 完成/计划 | 官方评分完成 | resolved | worker 有实际尝试/预期 | calls | total tokens | 累计调用秒 |
|---|---:|---:|---:|---:|---:|---:|---:|
| hetero_gpt-5.6-terra__glm-5.3 | 1/1 | 1 | 0 | 2/2 | 31 | 535701 | 553.36 |

| 任务 | hetero_gpt-5.6-terra__glm-5.3 |
|---|---|
| pydata__xarray-7229 | 失败 |

| Model / role | Calls | Input 含 cache | Cache | Output 含 reasoning | Reasoning | Total | 累计调用秒 |
|---|---:|---:|---:|---:|---:|---:|---:|
| glm-5.3 / worker | 9 | 72230 | 43136 | 7601 | 5701 | 79831 | 134.451 |
| gpt-5.6-sol / lead | 10 | 216544 | 55168 | 3149 | null | 219693 | 89.923 |
| gpt-5.6-terra / worker | 8 | 175865 | 51968 | 20510 | null | 196375 | 279.393 |
| deepseek-v4-flash / cm | 4 | 30855 | 1920 | 8947 | null | 39802 | 49.592999999999996 |

Calls 是包括失败在内的已完成调用记录数。实际尝试覆盖要求 transport_attempt_count > 0；本地尝试不等于服务端接受/推理完成。
usage 和真实 model/effort/tier 缺失均保留 null，已知小计另列；cache/reasoning 已分别包含在 input/output，不重复相加。
Lead、worker、CM 分角色计量；相同模型也不合并角色。累计调用秒与累计运行秒会在并发下重叠，不能当作批次墙钟。
worker 完成、Lead 采纳、队伍执行有效和官方 resolved 是不同指标。基础设施错误、预算、补丁 SHA 和原始 score 保留在 summary.json rows。
CM 配置状态：integrated_on_demand；用量从实际 CM 调用记录汇总。SPLIT/FISSION：not_exposed/not_exposed。
执行健康和真实工作区变化按 schema 分组，缺少字段的旧运行保留 unknown；旧 edit_detected 仅指命令尝试，不计为实际落地修改。
报告内部数据一致性提示：0；这不是完整证据审计，请另运行 validation/audit_results.py。
本报告未调用模型/容器/评分器，未读取 gold patch，未改原始证据。
