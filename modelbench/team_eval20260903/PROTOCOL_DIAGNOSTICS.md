# JSON 文本工具协议事后诊断

生成时间：2026-09-03T00:35:43.492799+00:00

本报告仅检查本次冻结 JSON 文本工具栈中的格式遵循与解析结果。它没有恢复失败调用、执行模型提出的工具、重评分或调用模型，不能据此推断某个模型的编码能力高低。

快照包含 **164** 个完成调用；有阶段 turn 证据且传输成功的 **163** 个调用中，driver 记录了 **29** 个协议错误（17.8%）。

错误率分母为已关联阶段 turn 的传输成功调用。传输失败、尚未落盘的 turn、未完成调用单列；初始执行与修复阶段的调用均计入。P/V/E 与 solo 的角色差异必须保留，因此模型家族对照也按角色列出。

“围栏内有效”只表示删除完整的单层 Markdown 外围围栏后，离线 JSON/协议校验能通过；原始调用在执行时仍然失败，其评分与执行状态不变。“引号/转义”按 JSON 解析器错误分类，其中 delimiter 错误不能单独证明一定由引号导致。native 格式表示文本中的对象形状，实际工具执行事件另列。

## 按模型与角色

| 模型 | 角色 | 完成调用 | driver 已检查 | 协议错误 | 错误率 | 纯围栏 | 围栏内 JSON / 协议有效 | native / 工具字段错误 | 引号/转义语法 | 传输失败 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| glm-5.3 | executor | 18 | 18 | 13 | 72.2% | 0 | 0 / 0 | 0 / 2 | 3 | 0 |
| glm-5.3-flash | executor | 18 | 17 | 12 | 70.6% | 0 | 0 / 0 | 0 / 0 | 0 | 1 |
| gpt-5.6-luna | executor | 12 | 12 | 2 | 16.7% | 0 | 0 / 0 | 0 / 0 | 1 | 0 |
| gpt-5.6-sol | executor | 15 | 15 | 0 | 0.0% | 0 | 0 / 0 | 0 / 0 | 0 | 0 |
| gpt-5.6-sol | oracle | 15 | 15 | 1 | 6.7% | 0 | 0 / 0 | 0 / 0 | 1 | 0 |
| gpt-5.6-sol | planner | 20 | 20 | 0 | 0.0% | 0 | 0 / 0 | 0 / 0 | 0 | 0 |
| gpt-5.6-terra | executor | 11 | 11 | 1 | 9.1% | 0 | 0 / 0 | 0 / 0 | 0 | 0 |
| gpt-5.6-terra | verifier | 55 | 55 | 0 | 0.0% | 0 | 0 / 0 | 0 / 0 | 0 | 0 |

## GLM 与 GPT 的同角色对照

| 家族 | 角色 | driver 已检查 | 协议错误 | 错误率 | 离线严格协议无效 |
|---|---|---:|---:|---:|---:|
| GLM | executor | 35 | 25 | 71.4% | 25 |
| GPT | executor | 38 | 3 | 7.9% | 3 |
| GPT | oracle | 15 | 1 | 6.7% | 1 |
| GPT | planner | 20 | 0 | 0.0% | 0 |
| GPT | verifier | 55 | 0 | 0.0% | 0 |

这是该快照的描述性对照；不同任务、消息历史、角色调用次数以及内置 CLI 上下文均可能不同。表中不提供编码能力排名或因果归因。

## 按运行与阶段

| 运行 | 阶段 | 模型 | driver 已检查 | 协议错误 | 错误率 |
|---|---|---|---:|---:|---:|
| INT1_pipeline_repair__solo__gpt-5.6-sol | solo | gpt-5.6-sol | 8 | 1 | 12.5% |
| INT1_pipeline_repair__team__glm-5.3 | executor | glm-5.3 | 6 | 3 | 50.0% |
| INT1_pipeline_repair__team__glm-5.3 | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| INT1_pipeline_repair__team__glm-5.3 | repair | glm-5.3 | 3 | 3 | 100.0% |
| INT1_pipeline_repair__team__glm-5.3 | reverify | gpt-5.6-terra | 3 | 0 | 0.0% |
| INT1_pipeline_repair__team__glm-5.3 | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| INT1_pipeline_repair__team__glm-5.3-flash | None | glm-5.3-flash | 0 | 0 | — |
| INT1_pipeline_repair__team__glm-5.3-flash | executor | glm-5.3-flash | 5 | 2 | 40.0% |
| INT1_pipeline_repair__team__glm-5.3-flash | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| INT1_pipeline_repair__team__glm-5.3-flash | repair | glm-5.3-flash | 3 | 2 | 66.7% |
| INT1_pipeline_repair__team__glm-5.3-flash | reverify | gpt-5.6-terra | 3 | 0 | 0.0% |
| INT1_pipeline_repair__team__glm-5.3-flash | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| INT1_pipeline_repair__team__gpt-5.6-luna | executor | gpt-5.6-luna | 6 | 1 | 16.7% |
| INT1_pipeline_repair__team__gpt-5.6-luna | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| INT1_pipeline_repair__team__gpt-5.6-luna | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| INT1_pipeline_repair__team__gpt-5.6-sol | executor | gpt-5.6-sol | 6 | 0 | 0.0% |
| INT1_pipeline_repair__team__gpt-5.6-sol | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| INT1_pipeline_repair__team__gpt-5.6-sol | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| INT1_pipeline_repair__team__gpt-5.6-terra | executor | gpt-5.6-terra | 6 | 1 | 16.7% |
| INT1_pipeline_repair__team__gpt-5.6-terra | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| INT1_pipeline_repair__team__gpt-5.6-terra | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| SPEC5_config_system__solo__gpt-5.6-sol | solo | gpt-5.6-sol | 7 | 0 | 0.0% |
| SPEC5_config_system__team__glm-5.3 | executor | glm-5.3 | 6 | 4 | 66.7% |
| SPEC5_config_system__team__glm-5.3 | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| SPEC5_config_system__team__glm-5.3 | repair | glm-5.3 | 3 | 3 | 100.0% |
| SPEC5_config_system__team__glm-5.3 | reverify | gpt-5.6-terra | 3 | 0 | 0.0% |
| SPEC5_config_system__team__glm-5.3 | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| SPEC5_config_system__team__glm-5.3-flash | executor | glm-5.3-flash | 6 | 5 | 83.3% |
| SPEC5_config_system__team__glm-5.3-flash | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| SPEC5_config_system__team__glm-5.3-flash | repair | glm-5.3-flash | 3 | 3 | 100.0% |
| SPEC5_config_system__team__glm-5.3-flash | reverify | gpt-5.6-terra | 3 | 0 | 0.0% |
| SPEC5_config_system__team__glm-5.3-flash | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-luna | executor | gpt-5.6-luna | 6 | 1 | 16.7% |
| SPEC5_config_system__team__gpt-5.6-luna | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-luna | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-sol | executor | gpt-5.6-sol | 6 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-sol | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-sol | repair | gpt-5.6-sol | 3 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-sol | reverify | gpt-5.6-terra | 3 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-sol | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-terra | executor | gpt-5.6-terra | 5 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-terra | planner | gpt-5.6-sol | 2 | 0 | 0.0% |
| SPEC5_config_system__team__gpt-5.6-terra | verifier | gpt-5.6-terra | 4 | 0 | 0.0% |

## 错误调用证据

只列调用 ID、错误类别、解析位置和文件路径；完整模型输出保留在原始 artifact 中。

| Call ID | 模型 / 角色 | 类别 | 短说明 | 证据 |
|---|---|---|---|---|
| f3b9ac58-e604-4de5-bb5a-a15e03a9c6c0 | gpt-5.6-luna / executor | json_property_quotes_or_delimiter | Expecting property name enclosed in double quotes @ 1:1604 | results/INT1_pipeline_repair__team__gpt-5.6-luna/phases/executor/turn_003.json; calls.jsonl:17 |
| e3ae4dce-6a03-4e1e-a2d6-128311124f12 | glm-5.3 / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3/phases/executor/turn_001.json; calls.jsonl:35 |
| f4003a52-0fe8-49d6-b201-562d913c03fe | glm-5.3 / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3/phases/executor/turn_002.json; calls.jsonl:37 |
| af91d608-7db5-4317-8b8a-8fec2c7f2f03 | glm-5.3 / executor | json_quote_or_delimiter_syntax | Expecting ',' delimiter @ 1:821 | results/SPEC5_config_system__team__glm-5.3/phases/executor/turn_004.json; calls.jsonl:41 |
| 5e1168e0-a1f4-49d4-bfc3-a9420cda3142 | glm-5.3 / executor | json_quote_or_delimiter_syntax | Expecting ',' delimiter @ 1:529 | results/SPEC5_config_system__team__glm-5.3/phases/executor/turn_005.json; calls.jsonl:43 |
| cb6c88ba-8000-452f-bf12-359838d67d0c | glm-5.3 / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3/phases/repair/turn_001.json; calls.jsonl:57 |
| a3e33a1b-7b6e-4347-b53e-caa80e3ba22d | glm-5.3 / executor | wrong_protocol_schema | Each tool call requires name and arguments; argument_fields_at_call_top_level, arguments_missing_or_not_object | results/SPEC5_config_system__team__glm-5.3/phases/repair/turn_002.json; calls.jsonl:61 |
| 091bdde4-2264-4aea-b645-2aa865bd600b | glm-5.3 / executor | json_quote_or_delimiter_syntax | Expecting ',' delimiter @ 1:9612 | results/SPEC5_config_system__team__glm-5.3/phases/repair/turn_003.json; calls.jsonl:63 |
| 4ae29610-a818-49a3-b156-1001e1ed8133 | glm-5.3 / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3/phases/executor/turn_001.json; calls.jsonl:81 |
| a73aaada-6954-4e24-90d3-3bfa2c13ede6 | glm-5.3 / executor | wrong_protocol_schema | Each tool call requires name and arguments; argument_fields_at_call_top_level, arguments_missing_or_not_object | results/INT1_pipeline_repair__team__glm-5.3/phases/executor/turn_002.json; calls.jsonl:83 |
| b1167af0-d09a-47ee-9841-88a5a8006c30 | glm-5.3 / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3/phases/executor/turn_006.json; calls.jsonl:99 |
| 2f5efe5a-169f-4340-a674-854b85a8e09e | glm-5.3 / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3/phases/repair/turn_001.json; calls.jsonl:121 |
| c56e5131-b12c-48d3-b6b2-b79d9e11a41b | glm-5.3 / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3/phases/repair/turn_002.json; calls.jsonl:123 |
| e911c28d-be7e-496c-8c31-adf846af058c | glm-5.3 / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3/phases/repair/turn_003.json; calls.jsonl:125 |
| 17351cee-c202-47ab-91c0-0bd0b67a34c7 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3-flash/phases/executor/turn_001.json; calls.jsonl:135 |
| c2128627-3ee1-401c-b259-e125d622a879 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3-flash/phases/executor/turn_003.json; calls.jsonl:139 |
| 2208dc98-5d38-449a-a630-cc0b84547de2 | gpt-5.6-terra / executor | json_trailing_data | Extra data @ 1:521 | results/INT1_pipeline_repair__team__gpt-5.6-terra/phases/executor/turn_001.json; calls.jsonl:151 |
| 8666c5c5-2ea9-4f28-a35d-868900a256b3 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3-flash/phases/executor/turn_004.json; calls.jsonl:159 |
| 47497209-88c0-4b55-92bf-d76feba04fcc | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3-flash/phases/executor/turn_005.json; calls.jsonl:167 |
| f7bee6d4-82a5-4b35-9cc8-d48053cfc0b4 | gpt-5.6-sol / oracle | json_property_quotes_or_delimiter | Expecting property name enclosed in double quotes @ 1:6153 | results/INT1_pipeline_repair__solo__gpt-5.6-sol/phases/solo/turn_003.json; calls.jsonl:179 |
| df27255a-0eb5-4c77-a0a4-06496c6145f6 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3-flash/phases/executor/turn_006.json; calls.jsonl:181 |
| 1cda9e30-d0fd-49e8-9db9-9b303fc0ccc0 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3-flash/phases/repair/turn_001.json; calls.jsonl:215 |
| 9d348bc1-44d6-4b37-9f9a-af22f783ade4 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3-flash/phases/repair/turn_002.json; calls.jsonl:223 |
| 9dd7e164-9654-430f-b8d6-7b07e6c3d87b | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/SPEC5_config_system__team__glm-5.3-flash/phases/repair/turn_003.json; calls.jsonl:229 |
| 4e0bd4b7-6b93-42ae-bfe8-d699dea81040 | gpt-5.6-luna / executor | json_trailing_data | Extra data @ 1:8702 | results/SPEC5_config_system__team__gpt-5.6-luna/phases/executor/turn_002.json; calls.jsonl:265 |
| 755b8fcd-d6ef-4eef-9bc9-c7e15c32f815 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3-flash/phases/executor/turn_001.json; calls.jsonl:291 |
| 82981e71-b190-4320-8481-f6ee70672413 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3-flash/phases/executor/turn_003.json; calls.jsonl:295 |
| 2936228d-3b42-4866-a63e-7550dd0e9c53 | glm-5.3-flash / executor | transport_error / empty_output | TimeoutError | calls/2936228d-3b42-4866-a63e-7550dd0e9c53/output.md; calls.jsonl:308 |
| e0c449e1-a925-4c28-8523-e828591b9575 | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3-flash/phases/repair/turn_001.json; calls.jsonl:318 |
| c635fe6f-bc5e-4cf3-9e17-ad2f83abfe1f | glm-5.3-flash / executor | non_json_prose_or_code | Expecting value @ 1:1 | results/INT1_pipeline_repair__team__glm-5.3-flash/phases/repair/turn_003.json; calls.jsonl:322 |

## 证据完整性

- 未完成调用：0；成功但未关联 turn：0。
- 重复 completed 行：0；输入读取警告：0。
- driver 解析后错误：0；实际 native 工具事件调用：0。
- 完成阶段的 protocol_errors 与逐 turn 核对不一致：0。
- 解析器与冻结 manifest 一致：True。
- calls.jsonl 快照 SHA256：`3505b9b7e2646953d07ec4bd0011bb48a33e6ca74f98918a5fe9cf51e9e74c8d`。

每个调用的细分标记、原始证据路径、行号及聚合分母见 `protocol_diagnostics.json`。
