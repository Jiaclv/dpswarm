# 固定 Agent Team 本轮记录（2026-09-03）

状态：按用户要求停止后续实验。本批已收束，没有运行中的候选请求或本实验容器。pilot_v3 未创建、未启动。
原计划两题六组共 12 run，实际完成 8 run，剩余 4 run 未启动。当前保留第一题六组作为未触发已知消息拼接缺陷的比较记录；第二题 Luna/Terra pair 单列为受基础设施缺陷影响的记录。

本批真实使用 8 名 Sol Lead、14 名 worker，共 22 个 agent 实例和 162 个模型调用记录。已知 Token 小计 3,462,637，另有一次取消调用用量未知，所以实测总 Token 为 null。48,761 是该调用的预算预留，不是实测用量。
批次墙钟 1940.109 秒（约 32 分 20 秒），包括准备、候选执行、官方评分和清理；各 run/调用并行，不能把它们的耗时相加当作批次墙钟。

## 实际测了什么

每个固定组在 Sol Lead 首次调用前由实验协议准入两名真实 DPswarm DERIVE worker：生产实现与回归测试。五个候选为 GLM-5.3、GLM-5.3-Flash、GPT Sol、Terra、Luna。每名 worker 有独立 node/item/session、环境、交付与调用账本。
这证明了真实派生团队的运行与记账，不证明模型自主判断开启团队。产品的自动启用策略未被本轮校准。SPLIT/FISSION 未暴露，CM 未接入且用量为 null；没有真实 CM 实例，不能把占位行算成 agent 或零成本测量。DSH 原生桥接仍不在本次验证范围。
GPT 请求 max/fast；GLM 请求 thinking enabled/max。未回显的模型、effort、tier 保留 null。GPT 走 Codex 文本工具适配器，GLM 走原生工具 API；接入方式、提示开销和输出 cap 不同，因此 Token/延迟反映模型加适配器的实际运行，不是纯模型内在能力。
每 run 最多 28 次调用、600,000 Token 准入、1,800 秒执行；worker 各 8 次调用。全局四个模型槽、四个容器槽，两组并行时会排队。

## 第一题 Matplotlib：六组完整比较

六组官方结果全部 resolved=true。单次任务不能支持稳定排名或 SOTA；也没有在本题证明固定团队提高最终正确率。

| 组（Lead 均为 Sol） | 官方通过 | worker 正式完成 | CP 正式采纳 | 调用 | 总 Token | run 墙钟秒 |
|---|---|---:|---:|---:|---:|---:|
| solo | 是 | — | — | 7 | 177,552 | 233.203 |
| fixed_glm-5.3 | 是 | 0/2 | 0 | 26 | 420,578 | 424.515 |
| fixed_glm-5.3-flash | 是 | 0/2 | 0 | 27 | 484,171 | 690.953 |
| fixed_gpt-5.6-sol | 是 | 2/2 | 2 | 19 | 447,293 | 479.188 |
| fixed_gpt-5.6-terra | 是 | 2/2 | 2 | 15 | 331,667 | 236.985 |
| fixed_gpt-5.6-luna | 是 | 2/2 | 2 | 23 | 537,957 | 391.984 |

两个 GLM 组的四名 worker 均达到 8 次上限，没有正式 finish；四份 delta 非空。每轮提示都包含 local_call/local_limit，不能称为预算漏传。运行器禁止采纳未完成交付，Lead 后续收尾，整题仍通过。交付是否有参考价值、单个 worker 补丁是否正确，不能仅由正式完成状态或整队分数推断。
三个 GPT 组的六名 worker 均完整完成，六份交付获 CP 正式采纳。墙钟受容器排队影响，不据这张表给模型排速度名次。
Matplotlib 的 117 个记录中，85 个 GPT 调用各只有一个非空原始 agent_message；按终端消息解析的动作与旧记录一致。其余 32 个 GLM 调用不走消息拼接路径。

## 第二题 Sphinx：保留原始结果，不进入有效模型比较

| 组 | 原始官方结果 | 调用 | Token | worker 正式完成 | 处理 |
|---|---|---:|---|---:|---|
| fixed_gpt-5.6-luna | 通过 | 23 | 557,203 | 1/2 | 整对排除，计入基础设施开销 |
| fixed_gpt-5.6-terra | 未通过 | 22 | null（已知小计 506,216） | 1/2 | 整对排除，计入基础设施开销 |

排除决定在这对官方结果产生前已通过 STOP_AFTER_PAIR 固化。Luna 的 7 个 JSON 错误共消耗 194,640 Token：原始事件内每个非空 JSON 都合法，适配器换行拼接多个消息后才出现 Extra data。原始事件没有 phase 字段，不能凭空称为 commentary/final，但消息边界丢失已证实。
Terra 的 Lead 预算预留失败后，运行器取消了已准入的 worker 请求。它运行了 58.437 秒；“exceeded 600 seconds”是错误文案，不是真实 600 秒超时。原始流只有 thread.started/turn.started，没有完成 usage；调用使用 --ephemeral，按精确 thread ID 未找到可恢复的本地会话。该调用用量继续为 unknown。
未经处理的总表显示 7/8 官方通过，这是混有基础设施异常的原始记录，不能用作 87.5% 模型能力结论。Sphinx 的其余四组（Sol、Flash、GLM、solo）未运行。

## 按模型和角色记账

| 路由模型 | 角色 | 调用 | Input（含 cache） | Cache | Output（含 reasoning） | Reasoning | Total | 累计调用秒 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| glm-5.3 | worker | 16 | 150,163 | 117,760 | 14,565 | 10,839 | 164,728 | 352.700 |
| glm-5.3-flash | worker | 16 | 161,431 | 127,424 | 23,092 | 19,380 | 184,523 | 721.986 |
| gpt-5.6-sol | lead | 67 | 1,617,861 | 695,296 | 36,582 | null | 1,654,443 | 774.700 |
| gpt-5.6-sol | worker | 10 | 207,930 | 103,680 | 6,479 | null | 214,409 | 119.405 |
| gpt-5.6-terra | worker | 24 | null | null | null | null | null | 486.593 |
| gpt-5.6-luna | worker | 29 | 705,728 | 382,720 | 36,199 | null | 741,927 | 585.263 |

null 表示未观测到完整值，已知小计保存在 JSON/CSV；cache 和 reasoning 是 input/output 的子维度，不能重复累计。父 agent 不包含子 agent 用量，即使都使用 Sol，也分开归属。取消/错误调用保留在账本中。未核实实际收费，美元费用为 null。
逐 agent、逐调用及独立时间表：

- [22 个 agent 的用量 CSV](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/pilot_v2.agent_accounting.final8/agents.csv)
- [162 次调用的完整索引 CSV](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/pilot_v2.calls.final8.csv)
- [逐 agent 时间 CSV](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/pilot_v2.timing.final8/agents.csv)
- [逐 run 时间 CSV](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/pilot_v2.timing.final8/runs.csv)
- [CM 未接入占位记录](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/pilot_v2.agent_accounting.final8/context_managers.csv)
- [机制准入、激活、交付与采纳表](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/pilot_v2.agent_accounting.final8/mechanisms.md)

时间表把 admission→activation（等待加环境准备）、累计模型请求墙钟、worker 交付墙钟、评分阶段分开。纯队列时间、纯模型计算时间、纯测试时间无法独立测得时为 null。

## 可靠性、修复与停止状态

本轮启动前 47 项验证通过，含真实隔离、fork、delta 合并；Matplotlib 官方负对照 completed=true/resolved=false。冻结后实际发现了测试未覆盖的消息拼接问题，这说明前置测试通过不等于运行管线没有缺陷。
最终 8-run 证据审计 PASS，162 条调用的身份、用量字段、预算预留、补丁及官方评分哈希一致；这只是证据完整性通过，不会把受缺陷影响的 Sphinx pair 变成有效比较。
已修复并验证：混合文件属主的容器权限初始化，候选仍 UID1000、无网络/宿主挂载、无 capabilities。
尚未完成（截至本记录写入时）：GPT 最终消息提取修复。~~草稿已另存，未配套测试、未运行候选~~ → 2026-09-03 修订 3 已修复并重新冻结（gate_revision3 PASS、156 测试、130 调用离线回放一致），详见 HANDOFF_20260903.md 顶部续作一节。保留 STOP_AFTER_PAIR。pilot_v3 没有创建。
取消后的用量丢失、~~错误超时文案~~（修订 3 已分离）、预算耗尽后的交付处理（修订 3 已加入有界 drain 与 finish-only 收尾调用），以及完整 SPLIT/FISSION/CM 接线，仍属后续事项。用户当前要求停止，不自动推进。

## 历史消耗边界

| 批次 | 调用 | 已知 Token | 说明 |
|---|---:|---:|---|
| 旧 SWE pilot_v1 | 2 | 33,281 | 早期脱敏错误，基础设施失败 |
| 旧 SWE pilot_v2 | 106 | 3,355,362 | 5 题/10 run，无 worker，两条件各 3/5 |
| fixed pilot_v1 | 0 | 0 | 两 run 均在容器初始化失败 |
| fixed pilot_v2（本轮） | 162 | 3,462,637 + unknown | 8 run，14 名真实 worker |
| 合计 | 270 | 6,851,280 + unknown | 不是精确总量，也不是美元成本 |

开发/审计助手、离线测试、镜像准备和负对照没有混入候选模型 Token 账本。正式评分结果属于整队最终补丁，不属于单个 worker。

## 证据入口

- [最终完整性审计](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/pilot_v2.final8.audit.json)
- [关闭状态与全部证据哈希](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/USER_CLOSEOUT.json)
- [消息拼接取证](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/LUNA_PROTOCOL_ANALYSIS.md)
- [取消与未知用量取证](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/TERRA_CANCEL_USAGE_RECONCILIATION.md)
- [worker 预算与交付取证](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/WORKER_BUDGET_ANALYSIS.md)
- [GLM timeout 参数错误与恢复](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/GLM_TIMEOUT_ANALYSIS.md)
- [为何旧批次不派生](K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/validation/ACTIVATION_ANALYSIS_ZH.md)

使用 SWE-bench Verified 官方数据与未改动的 v4.1.0 run_instance。只有一题形成完整六组比较，不能宣称榜单成绩或 SOTA。官方入口：[评分说明](https://www.swebench.com/SWE-bench/guides/evaluation/)、[提交规则](https://www.swebench.com/submit.html)。
