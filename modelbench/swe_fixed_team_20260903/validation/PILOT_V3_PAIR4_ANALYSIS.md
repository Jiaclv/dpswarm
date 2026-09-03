# pilot_v3 前两对（4 run）分析记录

时间：2026-09-03。范围：revision 3 适配器上 Sphinx 六组中的前四组（Luna、Terra、Sol、Flash）。批次在 Flash run 后被预声明门禁停止（unknown_calls=1），GLM-5.3 与 solo 两组未启动。本文件只读记录，不改写任何原始证据。审计：`pilot_v3.pair4.audit.json` PASS；逐 agent 账：`pilot_v3.agent_accounting.pair4/`。

## 结果概览

| run | 官方 resolved | 调用 | 已知 Token | 结局 | team_execution_valid |
|---|---|---:|---:|---|---|
| fixed_gpt-5.6-luna | false | 24 | 576,435 | budget_exhausted | true |
| fixed_gpt-5.6-terra | false | 23 | 499,631 | completed | true |
| fixed_gpt-5.6-sol | **true** | 25 | 561,397 | completed | true |
| fixed_glm-5.3-flash | **true** | 25 | null（已知小计见逐 agent 账） | completed | false |

四组共 97 调用、2,131,510 已知 token + 1 次未知用量调用。与 pilot_v2 的 Sphinx pair 相比：Luna 从"7 次 Extra data 烧 194,640 token"变为零适配器错误；Terra 从"预算级联取消 worker + 假超时文案 + 用量永久丢失"变为 completed 全结算。

## 修订 3 修复的实证核验

- **消息拼接（Luna 路径）**：修复生效。Luna 24 次 Codex 调用零错误；全部 run 的 Codex 调用均带 `--output-last-message`，`codex_response_selection` 元数据完整，终态工件（last-message.txt）逐调用落盘。
- **取消文案**：本批未发生取消，未触发该路径（单元测试已覆盖）。
- **预算收束 drain**：Flash run 真实触发一次 `worker_drain_started/completed`——Lead 预算预留失败时 worker-2 正处于在飞调用中，drain 窗口让该调用正常结算，期间无新准入。
- **worker 收尾调用**：Flash worker-2 真实触发 `closing_call_started`，随后 `closing_call_not_admitted`（DRAIN_IN_PROGRESS）。即预算已耗尽时收尾调用不被准入——这是设计内行为：预算诚实优先于交付完成，已记录而非暗改。
- **未知用量纪律**：Flash worker-1 一次 GLM 原生调用 `socket_timeout`（301.2s 读超时），usage 保留 null 未编造；预声明门禁据此停止后续 pair，符合冻结政策。

## 非缺陷观察

- Sol 的 Lead 有一次 `invalid_json`（`Extra data: char 497`）：绑定元数据显示单消息、单 turn、工件一致——是**模型自身**在终态消息末尾多输出了 `]}`，适配器正确拒绝并计账，run 后续完成且官方通过。属候选侧偶发，非适配器缺陷。
- Flash 的 socket_timeout 为提供方/网络侧读超时（300s 上限），非管线缺陷；按既有政策不重试。
- Luna/Terra 两组 resolved=false：两题难度与单 run 方差属正常范围；单题不支持排名结论。pilot_v2 中 Sphinx Luna 的原始"通过"是在受污染条件下取得，不能与新结果直接比较。

## 结论

修订 3 的三项修复在本批全部按预期工作，无新增适配器缺陷。剩余 Sphinx 两组（GLM-5.3、solo）未执行：批次被预声明门禁停止（unknown usage 须人工复核——已复核为提供方超时）。是否移除 STOP_AFTER_PAIR 续跑第三对，需用户确认。


---

# 附：第三对执行后六组总结（2026-09-03 补记）

用户确认续跑最后一对（GLM-5.3、solo），批次以 `stop: null` 正常结束。审计：`pilot_v3.final6.audit.json` PASS；逐 agent 账：`pilot_v3.agent_accounting.final6/`。

| arm | 官方 resolved | 调用 | 已知 Token | 结局 | team_execution_valid |
|---|---|---:|---:|---|---|
| fixed_gpt-5.6-luna | false | 24 | 576,435 | budget_exhausted | true |
| fixed_gpt-5.6-terra | false | 23 | 499,631 | completed | true |
| fixed_gpt-5.6-sol | **true** | 25 | 561,397 | completed | true |
| fixed_glm-5.3-flash | **true** | 25 | null（1 未知） | budget_exhausted | false |
| fixed_glm-5.3 | **true** | 28 | 409,224 | completed | false |
| solo | false | 15 | 512,199 | budget_exhausted | — |

六组共 **140 调用、3,052,933 已知 token + 1 次未知用量**。16 个 agent（6 Lead + 10 worker）。Sphinx 上 3/6 官方通过。

## 第三对的机制观察

- **GLM-5.3 两名 worker 都真实使用了 finish-only 收尾调用**（`closing_call_settled` ×2），并各自诚实交付 `blocked` 终态（"预算耗尽前未能写入补丁""delta 以 inline 说明交付"）。对比 pilot_v2：同类情况当时是静默的 `local_call_limit`，Lead 无法区分"撞限"与"无内容"；现在终态可读、信息可达 Lead，且未把 failed 伪装成 completed。整队官方通过仍记 `team_execution_valid=false`。
- solo 15 次调用 512,199 token 未通过：Sphinx 这题对单 agent 也确实困难；它与"固定团队 5 组中 3 组通过"共同说明该题区分度高于 Matplotlib，但单 run 仍不支持模型排名。
- 本批再无 Extra data、无假超时文案、无预算级联误杀；唯一未知用量为 GLM 提供方 socket 超时（已按门禁人工复核并保留 null）。

## 两题组合口径（绑定而非改写）

- Matplotlib 六组：pilot_v2（修订 2 适配器，经离线回放核验 85 个 Codex 调用 action 与记录一致）。
- Sphinx 六组：pilot_v3（修订 3 适配器）。两批 manifest、result、patch、report 哈希各自独立；组合比较必须逐批引用，不能混称同一版本。
- 全实验累计（含旧批）：约 410 次候选/实验调用、已知约 9.9M token + 1 次未知；本轮 pilot_v3 未超 336 调用/7.2M 准入的修订 3 预算约束。
