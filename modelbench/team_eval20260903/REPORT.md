# Agent Team 两题 pilot 结果

更新时间：2026-09-03T00:38:26.861249+00:00。状态：**已完成**。
矩阵终止 12/12，有效评分 11，其中通过 8；进行中 0，已终止但非有效评分 1。所有条件与失败均保留。

完整矩阵总 token：**—**；实际时间跨度（最早 start → 最晚 end）：**3,050.21 秒**；各 run wall 秒之和：**5,377.33 秒**。后两项分别表示并发实验的历时与累计运行时间，不能混用。
矩阵起止 UTC：2026-09-02T23:44:32.479704+00:00 → 2026-09-03T00:35:22.691470+00:00。矩阵尚未全部终止时，完整时间统计保留 null；总 token 还要求全部调用用量完整。

固定 Planner=Sol、Verifier=Terra，仅改变 Executor；另有 Solo Sol。两题分别是 SPEC5 generated seed 0 和 INT1 tracked static。Solo 与 Team 的模型调用**上限**均为 18，不是实际 token 或费用匹配。

| Executor / 条件 | 通过题数 | 已终止 | 有效评分 | 非有效评分 | 完整团队/solo token | Executor token | 两题累计 wall 秒 |
|---|---:|---:|---:|---:|---:|---:|---:|
| GLM-5.3 / team | 0/2 | 2/2 | 2 | 0 | 492,842 | 105,155 | 664.56 |
| GLM-5.3-Flash / team | 0/2 | 2/2 | 1 | 1 | — | — | 1,946.89 |
| sol / team | 2/2 | 2/2 | 2 | 0 | 598,320 | 293,620 | 746.36 |
| terra / team | 2/2 | 2/2 | 2 | 0 | 466,997 | 217,944 | 657.91 |
| luna / team | 2/2 | 2/2 | 2 | 0 | 496,284 | 257,527 | 1,019.12 |
| sol / solo | 2/2 | 2/2 | 2 | 0 | 341,618 | — | 342.49 |

待完成条件不作失败断言；以上通过题数是当前已确认数，固定模型顺序展示，不按成绩排序。首表 token 仅在该条件两题均终止且 usage 完整时汇总，否则为 null（显示 —）；wall 时间独立按已知时间汇总，不因 token 缺失而隐藏；Solo 的 Executor token 不适用。

下面按实际 `model_requested` 汇总该模型在全部矩阵条件中担任 P/E/V/S 的调用。它与首表按 Executor 条件计算的整队成本不同；此表为已完成调用的**已知小计**，矩阵进行中时 models.csv 的完整合计字段保持 null。

| 实际调用模型 | 状态 | 实际用途（调用数） | 已完成调用 | 已知 token 小计 | token 已知调用覆盖 | 已知调用秒之和 | 已知 JSON 协议错误 |
|---|---|---|---:|---:|---:|---:|---:|
| GLM-5.3 | 已完成 | E:18 | 18 | 105,155 | 18/18 | 211.92 | 13 |
| GLM-5.3-Flash | 已完成 | E:18 | 18 | 182,317 | 17/18 | 1,521.13 | 12 |
| sol | 已完成 | P:20/E:15/S:15 | 50 | 960,733 | 50/50 | 903.47 | 1 |
| terra | 已完成 | E:11/V:55 | 66 | 1,447,978 | 66/66 | 1,942.05 | 1 |
| luna | 已完成 | E:12 | 12 | 257,527 | 12/12 | 635.53 | 2 |

模型调用秒数是各次请求耗时之和，**不等于实验历时**；调用可并发，且不包含两次调用之间的工具和控制面时间。input/output/cached/reasoning 的完整值、已知小计及覆盖数均在 models.csv，未知项不补 0。

| 任务 | 条件 / E | 状态 | raw pass | 检查 | attestation | CP 验证 / 接受 | 调用 | P/E/V/S token | 总 token | E 占比 | 总秒 |
|---|---|---|---|---:|---|---|---:|---|---:|---:|---:|
| SPEC5 | solo / sol | scored | 是 | 19/19 | pass | 是 / 是 | 7 | —/—/—/160,510 | 160,510 | — | 189.05 |
| INT1 | team / luna | scored | 是 | 12/12 | pass | 是 / 是 | 12 | 31,985/126,794/84,208/— | 242,987 | 52.2% | 505.48 |
| SPEC5 | team / GLM-5.3 | scored | 否 | 0/19 | fail | 是 / 否 | 18 | 35,798/50,604/145,675/— | 232,077 | 21.8% | 298.11 |
| INT1 | team / GLM-5.3 | scored | 否 | 1/12 | fail | 是 / 否 | 18 | 29,660/54,551/176,554/— | 260,765 | 20.9% | 366.45 |
| INT1 | team / sol | scored | 是 | 12/12 | pass | 是 / 是 | 12 | 29,192/120,494/82,605/— | 232,291 | 51.9% | 259.23 |
| SPEC5 | team / GLM-5.3-Flash | scored | 否 | 0/19 | fail | 是 / 否 | 18 | 32,488/147,750/155,850/— | 336,088 | 44.0% | 1,048.91 |
| INT1 | team / terra | scored | 是 | 12/12 | pass | 是 / 是 | 12 | 29,992/126,764/91,331/— | 248,087 | 51.1% | 370.89 |
| INT1 | solo / sol | scored | 是 | 12/12 | pass | 是 / 是 | 8 | —/—/—/181,108 | 181,108 | — | 153.44 |
| SPEC5 | team / terra | scored | 是 | 19/19 | pass | 是 / 是 | 11 | 36,022/91,180/91,708/— | 218,910 | 41.7% | 287.02 |
| SPEC5 | team / sol | scored | 是 | 19/19 | pass | 是 / 是 | 18 | 35,340/173,126/157,563/— | 366,029 | 47.3% | 487.12 |
| SPEC5 | team / luna | scored | 是 | 19/19 | pass | 是 / 是 | 12 | 35,058/130,733/87,506/— | 253,297 | 51.6% | 513.64 |
| INT1 | team / GLM-5.3-Flash | transport_error | 否 | 1/12 | fail | 是 / 否 | 18 | 29,960/—/157,034/— | — | — | 897.98 |

P/E/V/S 分别为 Planner、Executor、Verifier、Solo。进行中或用量证据不完整时总量显示 —；CSV 中为字面量 null，known_* 仅表示已知小计，不能当完整总量。调用数为已完成 ledger 条数，包含失败调用。

主指标是原版 `raw_score.pass`，不是 shell 退出码、`status=scored` 或控制面接受。检查精确比例见 `checks_ratio`；`raw_partial_score` 保留官方四舍五入结果。SPEC5 为 19 项，INT1 为 12 项且 attestation 占一项；buggy baseline 分别为 **0/19** 和 **1/12（0.08）**。

| 异常或失败条件 | 记录 |
|---|---|
| SPEC5_config_system__team__glm-5.3 | {"failure_modes": ["config_system_missing", "import_error", "missing_keys", "defaults_wrong", "env_override_fail", "cli_priority_fail", "file_load_fail", "int_range_validation_fail", "int_boundary_fail", "enum_validation_fail", "bool_coercion_fail", "bool_env_fail", "bool_invalid_fail", "int_coercion_fail", "file_not_found_fail", "int_parse_error_fail", "schema_structure_fail", "cli_file_priority_fail", "unknown_keys_error"]} |
| INT1_pipeline_repair__team__glm-5.3 | {"failure_modes": ["pipeline_crash", "wrong_record_count", "wrong_error_count", "records_silently_dropped", "plus_emails_rejected", "wrong_field_names", "collector_not_json_array", "missing_required_fields", "integration_test_fail", "report_broken", "bad_attestation"]} |
| SPEC5_config_system__team__glm-5.3-flash | {"failure_modes": ["config_system_missing", "import_error", "missing_keys", "defaults_wrong", "env_override_fail", "cli_priority_fail", "file_load_fail", "int_range_validation_fail", "int_boundary_fail", "enum_validation_fail", "bool_coercion_fail", "bool_env_fail", "bool_invalid_fail", "int_coercion_fail", "file_not_found_fail", "int_parse_error_fail", "schema_structure_fail", "cli_file_priority_fail", "unknown_keys_error"]} |
| INT1_pipeline_repair__team__glm-5.3-flash | {"failure_modes": ["pipeline_crash", "wrong_record_count", "wrong_error_count", "records_silently_dropped", "plus_emails_rejected", "wrong_field_names", "collector_not_json_array", "missing_required_fields", "integration_test_fail", "report_broken", "bad_attestation"]} |

主 ledger 去重后 completed 调用 164，其中矩阵 164，矩阵外 0；重复 completed 行 0。独立 `preflight/calls.jsonl` 有 5 次未计分调用，未加入矩阵或本报告用量。任务/隔离烟测同样不计分。

矩阵调用中，服务端未报告实际 model 的有 129/164，未报告 tier 的有 164/164；未报告字段保持 null。请求 max/fast 不等于服务端确认。GPT 经 Codex CLI，包含其系统提示开销；GLM 经 API，比较的是本次部署栈。

`input_tokens` 已包含 cached input，`output_tokens` 已包含 reasoning；总量仅 input + output，不能再次加缓存或推理 token。完整团队与各角色 input/output/cached/reasoning、调用、工具和协议错误见 CSV。calls.csv 的 external_tool_calls 是本实验 JSON 工具动作，native_tools_used 是被禁止的 CLI 原生工具，两者分开。缺失值不当作已知 0；费用缺乏可靠依据，`cost_usd=null`。

主导、搭建和分析 agent 的开销不在本候选调用 ledger 中，因此不包含在这些总量里。这部分缺少 telemetry，调用数、token、耗时和费用均为未知，不能填 0。

这只是两题工程 pilot，不能作模型综合排名或异构协作因果结论。此次接入原生 DPswarm ControlPlane 的工作项、usage、submission 与评分证据；没有验证原生 Orchestrator 自动 fission/router 或 DSH 全链路。控制面验证、接受和 grader pass 分开记录。

文件：[calls.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/calls.csv>) · [runs.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/runs.csv>) · [roles.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/roles.csv>) · [models.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/models.csv>) · [任务预检](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/task_audit/TASK_SELECTION.md>)。
