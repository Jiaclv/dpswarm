# AgentTeam 修复实施记录

日期：2026-09-03。授权范围来自 `team_eval20260903/REPAIR_PLAN.md`；本记录区分实现、离线验证和真实回归。

后续状态：本报告的 51 调用/3 次通过属于已冻结的上一批真实回归。本次独立复审又发现并补修了边界问题，仍有宿主接入阻断项；没有新模型成绩。当前代码与历史版本不同，历史核心源码已保存在独立快照中。详见 [复审与重跑门槛](../postrepair_review_20260903/REVIEW.md)。

产品入口澄清（2026-09-03）：DPswarm 是当前主 agent 按需调用的协作能力。主 agent 默认单干，需要时自主调用插件并兼任 Lead，协作结束后继续原任务。本文的固定 P/E/V、独立运行命令和模型组合只用于受控实验。此前“生产 Orchestrator 尚未迁移”描述的是修复覆盖范围，不意味着产品要改成默认团队流水线；后续需把相关修复贯通宿主插件的按需委派链路。此次实验未评估“是否应该启用协作”的决策质量。

## 已落地

| 问题 | 代码处理 | 证据边界 |
|---|---|---|
| GLM 用文本模拟工具，说明文字/包装不合协议 | 新 transport 发送原生 tools，保留 tool_call_id、tool 结果与 reasoning_content；严格解析器统一校验工具名、参数、ID | 未从旧说明文字中启发式取 JSON；旧成绩未重算 |
| 原生普通文本被直接算 final | 只有显式 finish_phase 结束角色阶段，普通文本记 NoAction，非法参数消耗调用但不执行 | finish_phase 仍不等于任务 accepted |
| Planner 漏传细节 | submit_handoff 绑定公开 spec hash 和来源行号；字段/类型/适用值/交付物逐项校验，失败反馈 P，未通过则不启动 E | 这是两类 TeamBench 任务的有限事实契约，不是任意自然语言约束的完备证明 |
| 澄清消息无法重新调度 | request_clarification 挂起原 E，真实 CP 新建 P clarification item；最多一次请求、两次 P 调用；完成有效回复后恢复原 E | 普通 send_message 仍是通知；不提前接受旧 P，也不修改已提交 planning artifact |
| stale/重复回复 | 同时绑定 run、item、attempt、node、session、epoch、request ID、contract revision，回复只能被消费一次 | 不重置 E 历史、调用数、全局预算、工作区或租约 |
| 未知 usage 变 0 | 新账本保留 null、known_subtotal 和 reservation；Provider 增加 usage_observation | 兼容保留的旧 Usage 整数默认值不是实测；费用没有来源则 null |
| 超时与重启 | HTTP 子进程总 deadline、持久 hash-chain/原子快照、原生历史；恢复校验 CP 与容器身份 | 只恢复结算清楚的状态；未知在途结果不自动重发；完结结果恢复不重跑 grader |

## 代码落点

- 生产包复用模块：`dpswarm-plugin/dpswarm/team_runtime/{protocol,handoff,scheduler,ledger}.py`。
- Provider 合约：`dpswarm-plugin/dpswarm/providers/{base,openai_compat}.py`。
- 新集成运行器：`modelbench/team_runtime_v2/{runner,transport,recovery,task_contracts,tools,cli}.py`。
- 生产 `Orchestrator` 的普通文本循环未改成此 TeamBench 团队循环；本次真实集成使用原 ControlPlane 的准入、fence、记账和证据验收。

## 离线验证

- 主项目 tests：322 passed，20.22 秒。
- 最终实验运行器定向目录：75 passed，26.58 秒；JUnit 记录为 `validation/final-revision.junit.xml`。与主项目合计397项。此前63项版本记录仍保留。
- 其中调度集成使用脚本化 transport/fake sandbox，但使用真实 ExperimentControl、CP 事件和 ExecutionStore；不是模型能力成绩。
- 具体复现覆盖：缺约束阻断；错误映射反馈后重新交接；原生坏参数整批拒绝且 ID 配对；NoAction；E 批次结束但调度前中断的真实 CP 恢复；Planner 有回复却未明确结束时不得恢复 E；全局预算不重置；过期/超额快照恢复；HTTP 持续回包的总超时；完结容器只清理不执行。
- Windows 默认 pytest 临时目录存在权限问题，测试使用单独 `--basetemp` 后运行；没有通过跳过测试规避错误。
- 回归目录创建后，一次过宽的 pytest 收集因多个副本的同名 `test_pipeline.py` 中止，未执行测试函数或 pipeline。随后限定测试目录并增加 pytest 排除规则；主动核对活动实例、运行时和旧实验 hash 全部一致。收集失败记录和核查说明也保留在 `validation/`。

## 真实回归：最终修订

最终4轮已全部完成，3轮由原隔离 grader 和 ControlPlane 同时接受。Planner 固定 Sol，Verifier 固定 Terra，Executor 为两种 GLM；本轮没有重新比较 GPT Executor，也没有运行 Luna。

| Executor | 任务 | 最终成绩 | 整队调用数 | 整队 token | 含验收、清理的秒数 |
|---|---|---|---:|---:|---:|
| GLM-5.3 | SPEC5 配置系统 | 通过，19/19 | 12 | 196,900 | 381.32 |
| GLM-5.3 | INT1 流水线修复 | 未通过，11/12 | 16 | 224,832 | 419.11 |
| GLM-5.3-Flash | SPEC5 配置系统 | 通过，19/19 | 12 | 207,263 | 467.47 |
| GLM-5.3-Flash | INT1 流水线修复 | 通过，12/12 | 11 | 141,113 | 400.83 |

这4轮共51次调用、770,108 token，全部调用的总用量已知；GLM原生调用27次，工具协议错误0，transport错误0，NoAction 0。4轮交接全部通过，没有等价路径误拒绝。一个 Executor 初始阶段仍用尽6次调用且未显式结束，随后依预定流程进入验证和修复；预算提示不能保证模型一定按时结束。

INT1 的 GLM-5.3 失败属于剩余实现遗漏：已修好数据流和整数 score，但 `reporter/templates/summary.txt` 仍使用 `record.full_name`，规格要求 `record.name`。Verifier 在原工作区与临时副本均发现该问题，最终 attestation 维持 fail，grader 返回 `bad_attestation`。这里不是协议解析失败，也不是缺少 attestation；没有手动替候选修答案或追加重试来提高通过率。见[最终验证证据](<K:/秋招/项目/DPswarm/modelbench/team_runtime_v2/regression_budget_visible_20260903/results/INT1_pipeline_repair__budget_visible__glm-5.3__r1/submission/attestation.json>)。

真实模型本轮触发澄清请求0次。因此澄清、重调度及恢复的正确性目前由脚本化模型与真实ControlPlane的离线集成测试支持，尚无真实模型澄清成功率数据。两题各一次也不足以证明Flash普遍优于5.3。

### 分模型计量

下表只计最终4轮中有独立调用记录的 P/E/V 请求，不包含实现子 agent 的开发开销。

| 模型 / 角色 | 调用数 | 输入 token | 其中缓存输入 | 输出 token | 总 token | 模型调用耗时合计（秒） |
|---|---:|---:|---:|---:|---:|---:|
| Sol / Planner | 8 | 140,572 | 62,592 | 3,699 | 144,271 | 76.17 |
| Terra / Verifier | 16 | 335,875 | 192,512 | 57,877 | 393,752 | 761.53 |
| GLM-5.3 / Executor | 15 | 105,829 | 86,336 | 14,163 | 119,992 | 270.86 |
| GLM-5.3-Flash / Executor | 12 | 94,916 | 68,800 | 17,177 | 112,093 | 418.06 |

总输入677,192、输出92,916；缓存输入410,240已经包含在输入中。reasoning属于输出子集：GLM-5.3为6,952、Flash为9,837，已知小计16,789；GPT的24次调用均未提供该项，整批reasoning总量保留未知。GLM总token来自API，GPT总token由已报告输入与输出相加。总费用没有权威计价来源，保留null。

GPT请求设置为max+fast，GLM请求thinking enabled+max。实际effort/service tier均没有服务端回显，不把请求配置写成已实测生效；GLM回显模型名，GPT CLI未回显模型名。GLM请求max_tokens=32768，而GPT CLI未强制相同输出上限；全局token预算在调用边界控制准入，不能表述为服务端硬封顶。逐调用请求、响应与metadata保留请求值、回显值、usage来源和耗时。

批次最多同时运行2队，完整墙钟886.68秒（约14分47秒）；模型调用耗时合计1,526.62秒包含并行重叠。上表每轮时间由开始与沙箱closed时间相减，包含CP验收和清理；原始`result.wall_seconds`截止grader返回。这些口径分别保留，不能混用。

## 诊断批次与接入错误记录

初版v2回归暴露了两处本次接入错误：逐轮模型可见预算提示遗漏；同一交付物的工作区绝对/相对路径被当成不同文件。这是实现问题，不能归咎于候选模型。此前将等价路径拒绝描述成“准确拦截”的判断已纠正。

诊断批次原定8轮，完成6轮后触发预设的连续两次交接失败停止条件，未补跑剩余2轮。6轮中2轮通过，3轮在Planner交接阶段被等价路径问题挡住、Executor调用为0。另一个INT1 GLM-5.3失败由Verifier发现score接受字符串/浮点数导致，不能统一归因于预算提示。该批共58调用、989,824 token、0协议错误，完整墙钟1,156.72秒；原始失败及停止记录保留。

在保留初版源码和全部结果后，用独立`revisions/budget_visibility.py`补回逐轮预算提示，并只规范化已知工作区前缀和`./`的等价交付物路径；错误文件名、逃逸路径和多余字段仍拒绝。新增12项针对性测试通过，原始模型参数和工具日志不被规范化改写。上述最终4轮在新的源码快照、manifest及实例hash下运行。

初次`regression_20260903/`没有模型调用：启动前最后一处`recovery.py`修订被hash gate拦下，仅保留为预检记录。两批有调用的修复验证合计109次、1,759,932 token；分别报告结果，不合并为候选排名。旧主实验、native_followup没有重算。

## 使用入口与证据

从项目根目录运行。历史结果目录已冻结；新实验使用新的`--root`目录，先prepare，再run：

```powershell
python -m modelbench.team_runtime_v2.revisions.budget_visibility prepare --root modelbench/team_runtime_v2/regression_next
python -m modelbench.team_runtime_v2.revisions.budget_visibility run --root modelbench/team_runtime_v2/regression_next
```

此入口复用真实CP，但**生产Orchestrator的普通文本循环尚未迁移**。当前修复落地到Provider合约、可复用团队模块和实验团队运行器；没有将这轮验证表述为完整生产接入。

- [最终4轮报告](<K:/秋招/项目/DPswarm/modelbench/team_runtime_v2/regression_budget_visible_20260903/REPORT.md>)、[逐模型/逐角色/逐阶段统计](<K:/秋招/项目/DPswarm/modelbench/team_runtime_v2/regression_budget_visible_20260903/analysis.json>)、[CSV](<K:/秋招/项目/DPswarm/modelbench/team_runtime_v2/regression_budget_visible_20260903/summary.csv>)。
- [6轮诊断报告](<K:/秋招/项目/DPswarm/modelbench/team_runtime_v2/regression_20260903_v2/REPORT.md>)、[停止记录](<K:/秋招/项目/DPswarm/modelbench/team_runtime_v2/regression_20260903_v2/stopped.json>)。
- [最终JUnit](<K:/秋招/项目/DPswarm/modelbench/team_runtime_v2/validation/final-revision.junit.xml>)、[最终完整性核验](<K:/秋招/项目/DPswarm/modelbench/team_runtime_v2/validation/final_audit.json>)。
- 每批`calls/<call_id>/`保存原始请求、响应和metadata；`results/<run_id>/`保存工作区、角色阶段记录、execution日志、ControlPlane事件和grader结果；`source_snapshot/`保存对应源码快照。

最终核验：历史程序/实例/ControlPlane核心hash一致；诊断版17个和最终版20个源码与快照hash一致；109个调用ID无重复、无悬空；预算无待结算/未知调用、无超额；10轮CP不变量回放通过且任务验收与grader一致；本次50个自有容器全部移除。独立只读复核另将最终51次started/completed、原始usage、metadata、turn、result与CP rich ledger逐项对齐，未发现遗漏或重复。原始成绩及调用记录保留。当前20调用上限与旧主实验18调用上限不同，且组合修复同时改变多处机制，不能作严格单因素因果比较。
