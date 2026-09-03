# DPswarm Agent Team 两题实验报告（全部 16 轮完成）

**全部 16 轮已结束：预注册主矩阵 12/12，探索性原生工具复测 4/4。** 主数据依据 2026-09-03 00:35:42 UTC 的最终汇总，复测依据同日 00:48:00 UTC 的最终汇总，两阶段独立报告。主矩阵有 11 个 `scored`、1 个 `transport_error`，原版 raw pass 共 8：三个 GPT Executor 条件及 Solo Sol 均两题通过；两个 GLM 在本次文本工具配置下均未通过，Flash 的 INT1 另有一次调用超时。原生工具复测中，两 GLM 各自两题均通过；这不会替换主矩阵原分。该前后差异仍混有规划采样、信息交接和部署栈因素，不能据此形成综合模型排名。

本实验考察五个候选模型担任 Agent Team 的 Executor 时，在固定 Planner=Sol、Verifier=Terra 的条件下，能否完成规格传递、实现、独立验证与一次反馈修复。候选模型是 `glm-5.3`、`glm-5.3-flash`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`，另设获得完整信息的 Solo Sol 对照。它是两题、每条件一次的工程 pilot；结果只能描述这些任务和部署条件，不能形成模型综合排名或异构团队收益的因果结论。

## 方法与冻结范围

采用官方 [TeamBench](https://github.com/ybkim95/TeamBench/tree/d185aef1916fd86a9ba554d581fd256319a973af)，固定 commit `d185aef1916fd86a9ba554d581fd256319a973af`。两题均在该版本的 [TeamBench-90 清单](https://github.com/ybkim95/TeamBench/blob/d185aef1916fd86a9ba554d581fd256319a973af/leaderboard/data/leaderboard_90_tasks.json) 内，但本次只选取适合当前依赖环境且通过评分器审查的两题，没有复现完整官方排行榜或官方 seed sweep。来源与许可证保留为 [TeamBench MIT](https://github.com/ybkim95/TeamBench/blob/d185aef1916fd86a9ba554d581fd256319a973af/LICENSE)。

计分开始前冻结计划、模型请求参数、预算、顺序、任务实例、程序、DPswarm 核心和 Docker image 哈希。主矩阵每题六个条件，共 12 runs；固定随机种子 `20260903` 打乱顺序，最多两个 run 并发。各运行使用独立工作区、对话、消息和容器，保留所有失败与提前结束记录。实验主导者不为候选模型补写答案，不用隐藏 grader 的结果追加修复或挑选最好的一次。依据：[预注册计划](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/PLAN.md>)、[主 manifest](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/manifest.json>)、[决策日志](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/LOG.md>)。

| 项目 | 固定设置 |
|---|---|
| Team 初始阶段 | Planner 最多 2 次、Executor 6 次、Verifier 4 次模型调用 |
| 反馈修复 | 首次 attestation 未 pass 时，最多 Executor 修复 3 次、Verifier 复检 3 次 |
| 总调用上限 | 每个 Team 或 Solo run 均最多 18 次；协议错误同样消耗调用 |
| Token 边界 | 每 run 输入+输出累计达到 600,000 后停止新增调用；在调用之间检查，可被最后一次调用越过 |
| 工具与超时 | 每回复最多请求 8 个工具动作；模型调用 600 秒、工具命令 60 秒、grader 180 秒 |
| 对照含义 | Solo Sol 获得完整规格和工作区权限；匹配最大调用次数，不匹配实际 token、费用或信息获取过程 |

阶段按 P → E → V → 可选 E 修复 → V 复检顺序运行。每个 run 都重新生成规划与验证响应。`send_message` 记录本运行的内部消息，但发送消息本身不会重新唤醒已结束的 Planner 阶段。这一调度约束会影响协作结果；它在后面的 SPEC5/Sol 案例中有直接证据。[阶段调度代码](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/run_experiment.py:327>)

## 两题选择、实例与评分

| 任务 | 本次冻结实例 | 实际任务内容 | 原版未修复基线 |
|---|---|---|---|
| SPEC5_config_system | 官方 generator，seed=0；spec、brief、workspace、expected 来自同一次生成 | 10 个 Web Service 配置键、精确默认值和环境变量、类型转换、校验、CLI > env > file > defaults；初始 2 个文件 | `pass=false`，0/19，官方 partial=0.00 |
| INT1_pipeline_repair | 该 commit 的 tracked static workspace、spec、brief，seed=null | 12 个初始文件；修复 CSV → collector → processor → reporter 的接口、字段、数据质量和输出格式 | `pass=false`，1/12，官方 partial=0.08 |

SPEC5 seed 0 是 **Web Service / WEB**，不能与仓库静态 **Worker Service / CELERY** 规格混用。Executor 的初始 schema 不完整，完整信息由 Planner/Verifier 持有。INT1 刻意使用静态输入：其 generator seed 0 的邮件与姓名样本和原 grader 固定断言不一致，而静态输入匹配这些断言。正式运行从冻结实例创建新副本，未复制已经跑过 smoke 的工作区。

DIST1 的动态导入与 dataclass future annotations 存在评分假阴性；INFRA2 有字面量路径检查问题，另有若干任务存在规格与评分不一致或答案资产暴露问题，均在候选计分前排除。没有修 grader 或编写参考解来改变选择结果。两题 smoke 仅确认原版 grader 能在当前环境产生原始分数，不证明完整规格已被评分器覆盖。具体复现、排除项和源码链接见 [任务预检](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/task_audit/TASK_SELECTION.md>) 与 [审计 JSON](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/task_audit/audit.json>)。

执行的 shell 脚本取自 Git 原始 LF blob，避免 Windows CRLF checkout 改变 Bash 行为；保留 canonical grader SHA-256。两题任务代码只需 Python 标准库，INT1 grader 另使用 pytest；镜像提供 Python 3.11.16、pytest 8.4.2、pytest-timeout 2.4.0、PyYAML 6.0.2 和 Bash 工具。固定 image ID 为 `sha256:2e803b4f52ffaa2735f32d5ac5eaf5d24837f0cc30a850d64357ff5b0d8d2f77`。

主指标是最终原版 `score.raw.json.pass`。`status=scored` 仅表示成功取得可用评分，脚本退出 0 也不等于任务通过。辅助指标保留精确的 `checks_passed/checks_total`、官方四舍五入的 `partial_score`、失败项和 Verifier verdict。SPEC5 共 19 项；INT1 共 12 项，其中包含 attestation 的 `verdict=pass` 以及 Python 文件总字节数小于 10,000。因此检查比例与纯功能正确性并不完全等价。SPEC5 的部分优先级和类型检查也较弱，满分不能证明覆盖所有合法输入。

## 信息隔离与控制面证据

| 角色 | 可获得信息与权限 |
|---|---|
| Planner | 完整 spec/brief，只读；不挂载工作区 |
| Executor | brief、本运行内部规划消息；workspace/reports 可写；不挂载完整 spec |
| Verifier | 完整 spec/brief、workspace/reports 只读；submission 可写，可在自己的 `/tmp` 新副本运行需要写文件的测试 |
| Solo | 完整 spec 与工作区、reports、submission 所需读写权限 |
| 最终 grader | 独立执行原版评分；需要的 private expected 仅在 grader 容器可见 |

上述边界使用 Docker 的挂载权限实现，不只依赖提示词。容器无网络、非 root、只读根文件系统、移除 capabilities，未挂载宿主凭据、其他运行、审计答案目录或仓库。预检记录了 35 项通过的权限/运行检查；其中 DIST1 只是隔离 smoke 载体，不计入两题模型分数。最终 grader 运行前冻结候选角色容器并保存工作区哈希，避免评分期间继续修改。依据：[隔离预检](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/sandbox_preflight.json>)、[sandbox 实现](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/sandbox.py>)。

保留 TeamBench 的角色提示与工具 schema，使用实验自己的严格 JSON agent loop。采用该 loop 是因为官方 loop 可将回复中的 `DONE` 子串误判为完成。隐藏 grader 在模型阶段结束后执行一次；模型阶段的测试由角色自行执行，Verifier 的反馈才能触发既定修复。

DPswarm 接入的是现有 **ControlPlane** 的 work item/node 生命周期、session/epoch fencing、usage、submission、证据接受和 invariant replay。没有验证原生 Orchestrator 自动分裂、路由或 DSH 插件全链路。必须分别报告：

- `raw pass`：任务评分通过。
- `oracle.verified`、`invariant_replay_passed`：控制面保存的评分证据核验与回放结果。
- `control_accepted`：评分证据核验通过、raw pass 为真且角色提交证据完整时才接受。

所以 raw fail、CP verified=true、accepted=false 是一致的合法失败记录。最终离线审计还会核对调用配对、预算、token 算术、phase/turn 协议计数、原始评分、文件哈希与 cleanup 记录。该审计核对保存的 replay 和 cleanup 证据，不自行重放核心测试、不再次运行 grader，也不据此声称做过实时容器检查。[控制面桥接](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/control_bridge.py:344>)、[离线审计脚本](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/verify_experiment.py>)

**主矩阵离线审计：`status=unknown`、`audit_pass=null`，4,300 项通过、0 项已知失败、7 项未知，0 项 pending。** 七个未知均源于同一次 GLM-5.3-Flash 超时调用没有 usage：该次调用的 input+output 算术无法核验，相应 run 的三项汇总和控制面的三项汇总也无法确认完整总量。现有证据没有发现不一致，但不能宣布全量计量审计通过。[FINAL_AUDIT.json](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/FINAL_AUDIT.json>)

**原生工具复测离线审计：`status=pass`、`audit_pass=true`，1,788 项通过、0 项失败、0 项未知、0 项 pending。** 调用配对、输入/输出/总量、评分证据、冻结哈希、phase/turn 计数与落盘 cleanup 均通过本脚本规定的检查。这里的“0 项未知”只针对这些审计检查项；GPT reasoning、实际 effort/tier 仍没有回显，不代表所有 telemetry 都完整。[复测 FINAL_AUDIT.json](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/FINAL_AUDIT.json>)

另经实验主导者收尾实测，`docker ps -a --filter label=dpswarm.teambench.owner` 没有返回实验容器，vendor TeamBench 的 `git status --short` 为空。这是独立的收尾检查，和离线脚本只核对落盘 cleanup 的范围分开记录。

## 模型请求参数与实际回显

| 候选模型 | 本次调用通道 | 明确请求的参数 | 实际回显的边界 |
|---|---|---|---|
| glm-5.3 | 已配置 GLM coding API | `thinking.type=enabled`、`reasoning_effort=max`、`temperature=1.0`、`max_tokens=32768` | 已观察的正常响应回显 `glm-5.3`；实际 effort/tier 未报告则 null |
| glm-5.3-flash | 同上 | 同上 | 正常响应可回显 `glm-5.3-flash`；无回显/异常调用保留 null |
| gpt-5.6-sol | 已登录 ChatGPT 的 Codex CLI 0.149.0 | `model_reasoning_effort=max`、`service_tier=fast` | 本稿观察的调用未回显实际 model、effort、tier，均为 null |
| gpt-5.6-terra | 同上 | 同上 | 同上 |
| gpt-5.6-luna | 同上 | 同上 | 同上 |

`max` 和 `fast` 是请求值，不是服务端实际采用值的证明。GLM 未请求 fast tier。GPT CLI 显式忽略用户配置，使用只读、ephemeral、approval=never 模式，并禁用 CLI shell、多 agent 和 web；候选只能把动作交给实验的外部工具层。GPT CLI 自身系统提示和序列化包装带来的 token 计入 ledger，GLM 则直接经 API 请求，故本次比较包含部署栈差异。GPT CLI 无可强制的相同输出 token 上限，记录 `cap_enforced=false`，不能把账本中的 `max_tokens_requested` 当成其服务端硬上限。依据：[transport 实现](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/transports.py:243>)、[原始调用 ledger](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/calls.jsonl>)。

主矩阵 164 个 completed 调用中，model 回显已知 35、未知 129：GLM-5.3 为 18/18，Flash 为 17/18；GPT 共 128 次均未回显。实际 effort 与 tier 均 0/164 已知。这里的 0 是已知回显数量，不代表实际 effort/tier 为零。费用缺乏可核实的实际账单或一致价格依据，`cost_usd=null`；token 不能直接换算为本次实付金额。

## 主矩阵：JSON 文本工具协议

主矩阵所有候选都通过严格 JSON 文本表达 `tool_calls` 或 `final`，GLM 在这一阶段没有使用 provider 的原生函数工具接口。无效 JSON、错误字段或超出批量限制会消费一次调用并反馈协议错误；运行器不修字符串、不执行未通过解析的动作，也不把事后“可恢复解析”改计为成功。

已观察到 GLM 的非 JSON 输出、字符串/分隔符语法错误，以及把工具参数放错层级等问题。具体例子包括 `e3ae4dce-6a03-4e1e-a2d6-128311124f12` 的首字符解析失败，以及 `a3e33a1b-7b6e-4347-b53e-caa80e3ba22d` 的工具参数结构错误。它们与 SPEC5 主矩阵中最终缺少 `config_system.py` 的记录共同表明：生成的意图未必被成功执行成工作区修改。不能据此把所有任务失败归因于编码能力。[初始执行 turn](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__glm-5.3/phases/executor/turn_001.json>)、[修复 turn](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__glm-5.3/phases/repair/turn_002.json>)

协议诊断是离线检查，不调用模型、不执行恢复出的工具、不重评分。[最终诊断报告](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/PROTOCOL_DIAGNOSTICS.md>) 覆盖 164 个 completed 调用；其中 163 次传输成功且有 turn、经过 driver 检查，共 29 次协议错误（17.8%）。另一次 Flash 传输超时不进入协议错误率分母。没有未完成调用、成功但缺少 turn 的调用或重复 completed；phase 汇总与逐 turn 的协议错误数一致。

| 模型 / 角色 | 协议错误 / 实际检查调用 | 比例 | 另列传输失败 |
|---|---:|---:|---:|
| GLM-5.3 / E | 13/18 | 72.2% | 0 |
| GLM-5.3-Flash / E | 12/17 | 70.6% | 1 |
| Sol / E | 0/15 | 0.0% | 0 |
| Terra / E | 1/11 | 9.1% | 0 |
| Luna / E | 2/12 | 16.7% | 0 |
| Sol / P | 0/20 | 0.0% | 0 |
| Terra / V | 0/55 | 0.0% | 0 |
| Sol / Solo | 1/15 | 6.7% | 0 |

下表列出全部 12 个运行，保留固定展示顺序，不按成绩排名。Flash 的一个调用缺少 usage，所以其完整团队和 Executor token 为 null；时间有独立证据，仍可报告。E 占比为两题 Executor token / 完整团队 token。

| 条件 / Executor | SPEC5 原始结果 | INT1 原始结果 | 两题通过数 | 完整团队/solo token | Executor token | E 占比 | 两题 run wall 秒之和 |
|---|---|---|---|---:|---:|---:|---:|
| Team / GLM-5.3 | fail，0/19 | fail，1/12 | 0/2 | 492,842 | 105,155 | 21.3% | 664.564 |
| Team / GLM-5.3-Flash | fail，0/19 | transport_error；raw fail，1/12 | 0/2；含 1 次异常 run | null | null | null | 1,946.890 |
| Team / Sol | pass，19/19 | pass，12/12 | 2/2 | 598,320 | 293,620 | 49.1% | 746.359 |
| Team / Terra | pass，19/19 | pass，12/12 | 2/2 | 466,997 | 217,944 | 46.7% | 657.907 |
| Team / Luna | pass，19/19 | pass，12/12 | 2/2 | 496,284 | 257,527 | 51.9% | 1,019.125 |
| Solo / Sol | pass，19/19 | pass，12/12 | 2/2 | 341,618 | 不适用 | 不适用 | 342.490 |

主矩阵终止 12/12：11 个 `scored` 和 1 个 `transport_error`，无 `grader_error` 或 `infrastructure_error`。12 个 run 都保存了原始 grader 分数；raw pass 和最终 attestation pass 均为 8，未发现最终 attestation pass 而 raw fail 的误放行。CP oracle verified=12/12、保存的 invariant replay passed=12/12、角色证据完整=12/12、accepted=8/12；落盘 cleanup 记录没有错误。这里的 verified/replay 数描述控制面证据，不能把四个 raw fail 改写为任务通过。

Flash / INT1 的 run wall 为 **897.984 秒**，其中调用 `2936228d-3b42-4866-a63e-7550dd0e9c53` 在约 600.297 秒发生 `TimeoutError`，没有 token usage。该 run 虽最终仍产出 raw 1/12，但状态保留 `transport_error`。原 `result.total_tokens=221561` 是原汇总逻辑得到的**已知小计**，不是完整消耗；分析器与本报告将完整 total 置为 null，并保留原 result 不改。[异常运行 result](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/INT1_pipeline_repair__team__glm-5.3-flash/result.json>)

主矩阵完整 input/output/total 均为 **null**；163/164 次调用已知 input=2,625,860、output=327,850，合计 **2,953,710 token 已知小计**。最早 run start 为 `2026-09-02T23:44:32.479704+00:00`，最晚 run end 为 `2026-09-03T00:35:22.691470+00:00`，实际并发跨度 **3,050.211766 秒**；12 个 run wall 累加 **5,377.33 秒**。全部 164 次模型调用 wall 累加 **5,214.096 秒**，含超时调用；这三个时间指标不可互换。

## 已完成案例：SPEC5 / Team Sol 的信息交接

运行 ID 为 `SPEC5_config_system__team__gpt-5.6-sol`。本例的最终结果已经落盘，可独立分析；不依赖其余运行完成。时间均为 UTC。

| 时间/阶段 | 可核对事件 | 证据 |
|---|---|---|
| 00:12:47，Planner | 发送优先级、校验和边界规则，要求使用完整十项 schema，却没有把所有键及精确 default/env_var 一并传递；例如 `debug_mode` 未在规划消息中给出 | [规划消息](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__gpt-5.6-sol/phases/planner/turn_001.json>) |
| 00:13:13–00:13:41，Executor | 先后向 P/V 请求完整 schema；已有 brief 和 partial schema 不足以确定缺失项，未直接猜测隐藏规格 | [内部消息时间线](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__gpt-5.6-sol/messages/dialogue.jsonl>) |
| 00:14:41，初始执行结束 | 用完 6 次调用，声明因精确 schema 缺失而 blocked，未创建目标实现 | [Executor phase](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__gpt-5.6-sol/phases/executor/phase.json>) |
| 00:14:54–00:16:24，首次验证 | Terra 向 E 补齐十项 schema，独立检查发现模块缺失，写入 fail attestation | [首次 attestation](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__gpt-5.6-sol/first_attestation.json>) |
| 00:16:25–00:17:53，既定修复 | Sol 获得完整 schema 与验证反馈，创建实现并测试，使用 3 次修复调用 | [Repair phase](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__gpt-5.6-sol/phases/repair/phase.json>) |
| 复检与最终评分 | Terra 在新的工作区副本上验证并写 pass；最终原版 grader 为 pass、19/19 | [最终 attestation](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__gpt-5.6-sol/submission/attestation.json>)、[原始 score](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__gpt-5.6-sol/sandbox/grade/score.raw.json>) |

该 run 实际用了全部 18 次调用：P=2、初始 E=6、V=4、修复 E=3、复检 V=3；完整团队 366,029 token，其中 P=35,340、E=173,126、V=157,563，run wall=487.12 秒。该例所有 phase 的 protocol_errors=0，因此不能用 JSON 协议失败解释这次初始阻塞。[最终 result](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/results/SPEC5_config_system__team__gpt-5.6-sol/result.json>)

日志支持“规划信息遗漏、Executor 请求澄清、顺序调度延迟补充、Verifier 反馈后修复成功”的过程判断。它显示固定强 Planner 也可能漏传关键信息，且内部消息功能不等于动态再调度。单次案例不能量化各因素的独立贡献，不能把成本全部归因于 Executor，也不能证明某个新的调度策略一定更优。

## 探索性复测：GLM 原生工具调用（独立结果）

本轮是在**看到主矩阵文本协议错误后**增加的探索性敏感性实验，`experiment_kind=native_tools_followup`，标签为“GLM 原生工具调用探索性复测”。它不是主矩阵的预注册分数，不合并、不覆盖、不替换原分。四个运行在主矩阵全部结束后启动，现已全部完成。

两 GLM × 同两题，共四个新 run；P/V 仍为 Sol/Terra，同预算、镜像、权限、实例和 grader。模型输入不读取主矩阵代码、规划消息、attestation 或评分。GLM 改用 provider 的 `tools` / `tool_calls`、`role=tool` 与对应调用 ID，保留同一角色历史中实际返回的 `reasoning_content`；本轮只机械替换传输专用指令，并把原生响应规范化后交给相同工具执行层。无效参数等仍按失败消费调用。GPT P/V 继续使用原来的 JSON 文本协议。依据：[复测计划](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/PLAN.md>)、[独立 manifest](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/manifest.json>)。

| 原生工具 Team / Executor | SPEC5 原始结果 | INT1 原始结果 | 两题通过数 | 完整团队 token | Executor token | E 占比 | 两题 run wall 秒之和 |
|---|---|---|---|---:|---:|---:|---:|
| GLM-5.3 | pass，19/19 | pass，12/12 | 2/2 | 455,933 | 127,480 | 28.0% | 543.594 |
| GLM-5.3-Flash | pass，19/19 | pass，12/12 | 2/2 | 548,143 | 175,185 | 32.0% | 820.220 |

四个 run 均为 `scored`，raw pass、最终 attestation pass、CP oracle verified、invariant replay passed、角色证据完整及 accepted 均为 4/4。没有最终 attestation 误放行，没有 transport/grader/infrastructure error，四份落盘 cleanup 的 errors 均为空。复测没有新的 Solo 或 GPT Executor 条件，因此不为这些未运行条件列出 0/0。

| 任务 / Executor | P/E/V 调用数 | P/E/V token | 团队总 token | E 占比 | 实际工具动作 | run wall 秒 |
|---|---|---|---:|---:|---:|---:|
| SPEC5 / GLM-5.3 | 2/9/7 | 32,996 / 82,342 / 178,988 | 294,326 | 28.0% | 28 | 328.859 |
| INT1 / GLM-5.3 | 2/6/4 | 30,410 / 45,138 / 86,059 | 161,607 | 27.9% | 44 | 214.735 |
| SPEC5 / GLM-5.3-Flash | 2/9/7 | 32,272 / 117,582 / 154,751 | 304,605 | 38.6% | 21 | 401.860 |
| INT1 / GLM-5.3-Flash | 2/9/7 | 29,286 / 57,603 / 156,649 | 243,538 | 23.7% | 25 | 418.360 |

复测共 **66 次模型调用、118 个实际角色工具动作**。INT1 / GLM-5.3 用 12 次调用完成；其余三个 run 都经过既定修复，分别使用 18 次调用。E/V 列包含修复/复检，不额外增加预算。各 run 明细见 [复测 runs.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/runs.csv>) 与 [roles.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/roles.csv>)。

GLM 原生通道 33 次调用的参数/协议错误为 0，GPT P/V 文本通道 33 次调用的 JSON 协议错误也为 0；66 次均无传输失败。33 次 GLM 调用请求了 57 个原生函数动作，实际由同一角色工具层执行；另外 61 个动作来自 GPT P/V 的文本请求。`adapter_mode` 区分两种通道，`native_tool_calls_requested` 只在 GLM 原生通道有对应记录，不能把 GPT 的不适用字段补成原生请求数 0。`tools_used` 保留 ledger 的原字段，本轮全部为 0，不能因此把那 57 个 GLM 原生请求说成没有发生。主矩阵的纯文本解析率也不能直接套到本轮原生响应。[复测 calls.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/calls.csv>)、[ledger](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/calls.jsonl>)

全部 66 次调用的 input/output/total 均已知：input=923,859、output=80,217，完整团队合计 **1,004,076 token**。起止 UTC 为 `2026-09-03T00:35:27.662305+00:00` → `2026-09-03T00:47:54.907129+00:00`，实际并发跨度 **747.244824 秒**；四个 run wall 累加 **1,363.814 秒**；66 次模型调用 wall 累加 **1,298.609 秒**。这些复测总量独立于主矩阵，主矩阵的超时 usage 缺失仍然保留。

两 GLM 在本次文本工具配置下均为 0/2，在原生工具复测中均为 2/2，且复测未出现通道协议错误。这是改变工具通道后实际表现变化的探索性证据；两阶段同时包含重新采样的规划、验证和执行响应，复测由先前观察触发且只有四个样本，不能称为工具接口的严格因果效应。

GLM-5.3 的 SPEC5 复测还出现了一个信息交接案例：Planner 收到的完整规格包含 `debug_mode` 的环境变量 `WEB_DEBUG` 和 `keep_alive_timeout` 的范围 `[1,300]`，但实际规划消息只泛称精确环境变量名和包含边界的范围，没有传递这两个值。Executor 在写入实现前没有收到正确值，也没有追问，写入了 `WEB_DEBUG_MODE` 和 `[0,3600]`。Verifier 独立发现差异、发送明确反馈后，Executor 在既定修复轮内改正，最终通过 19/19。该过程同时涉及规划遗漏和执行者对缺失信息的处理，不能归类为“已收到正确规格却写错”。证据：[实际规划消息](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/results/SPEC5_config_system__native_team__glm-5.3/messages/dialogue.jsonl:1>)、[写代码前的请求](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/calls/7d97d3e5-e523-4292-a439-ea770c8c4b0e/wire.request.json>)、[首次验证](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/results/SPEC5_config_system__native_team__glm-5.3/first_attestation.json>)、[修复检查](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/results/SPEC5_config_system__native_team__glm-5.3/phases/repair/turn_003.json:26>)。

Flash 的 SPEC5 复测同样未从 Planner 收到 `WEB_DEBUG` 的精确映射，随后写成 `WEB_DEBUG_MODE`。工作区自身 66 项测试通过，但 Verifier 的独立映射检查失败；既定修复后最终通过 19/19。这说明本例中工作区测试并未覆盖完整规格。[本轮规划](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/results/SPEC5_config_system__native_team__glm-5.3-flash/messages/dialogue.jsonl:1>)、[写入前请求](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/calls/9d6969eb-e1e6-4832-b51a-8fd4685e43a0/wire.request.json>)、[首次验证](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/results/SPEC5_config_system__native_team__glm-5.3-flash/first_attestation.json>)。Flash 的 INT1 首次验证则是功能检查通过、提交目录缺少要求的三份输出文件；Verifier 在自己的临时副本生成产物不等于 Executor 已交付，因而触发修复。[交付检查](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/results/INT1_pipeline_repair__native_team__glm-5.3-flash/first_attestation.json>)。

## 用量、时间与异常的统计口径

调用账本按 `call_id` 关联 started/completed；统计以唯一 completed 为单位，包含失败调用。配对缺失、重复、run 归属不一致和 phase/turn 计数不一致交给最终审计核查，不能静默丢弃异常。独立 `preflight/calls.jsonl` 的五次模型连通性调用，以及任务/隔离 smoke，不计入候选矩阵用量。

`total_tokens=input_tokens+output_tokens`；cached input 已含在 input 中，reasoning 已含在 output 中，不重复相加。未报告字段为 `null`，不是已知 0。`known_subtotal` 只能说明已知部分；该条件两题尚未完成或用量不完整时，完整总量仍为空。Executor 成本还应与整个 P+E+V 团队成本并列，并报告 E 占比，不能只用执行者用量代表团队成本。

下表按实际 `model_requested` 汇总主矩阵所有角色调用，和前面的“以 Executor 条件定义的完整团队成本”口径不同。Sol/Terra 既可能是固定 P/V，也可能在某些条件担任 E，Sol 还承担 Solo。具体 input/output/cached/reasoning、调用、工具、协议错误和覆盖数由 [models.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/models.csv>)、[roles.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/roles.csv>) 提供。

| 主矩阵实际调用模型 | 用途（调用数） | 完成调用 | Input / Output / Total | Cached / Reasoning（覆盖数） | 模型调用秒之和 | JSON 协议错误 |
|---|---|---|---|---|---|---|
| GLM-5.3 | E:18 | 18 | 84,640 / 20,515 / 105,155 | 63,616 / 5,685（均 18/18） | 211.924 | 13 |
| GLM-5.3-Flash | E:18 | 18 | 完整值 null；已知 122,328 / 59,989 / 182,317（17/18） | 完整值 null；已知 94,336 / 37,615（均 17/18） | 1,521.126 | 12；另 1 次传输失败 |
| Sol | P:20 / E:15 / Solo:15 | 50 | 901,417 / 59,316 / 960,733 | 314,368（50/50） / null（0/50） | 903.469 | 1，发生于 Solo |
| Terra | V:55 / E:11 | 66 | 1,308,902 / 139,076 / 1,447,978 | 745,728（66/66） / null（0/66） | 1,942.047 | 1，发生于 E |
| Luna | E:12 | 12 | 208,573 / 48,954 / 257,527 | 89,600（12/12） / null（0/12） | 635.530 | 2 |

缓存 input 的已知小计为 1,307,648（163/164 调用有报告）；reasoning 的已知小计为 43,300（35/164 有报告），二者完整矩阵值仍为 null。GPT reasoning 没有独立回显，不能从输出 token 中推定为 0。主矩阵共执行 306 个外部工具动作；CLI 原生工具调用事件为 0，和上述外部工具动作属于不同字段。

原生工具复测按实际调用模型的独立明细如下；此处 Sol 只担任 P，Terra 只担任 V，GLM 两型只担任 E，没有调用 Luna。[复测 models.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/models.csv>)

| 复测实际调用模型 | 用途（调用数） | Input / Output / Total | Cached / Reasoning（覆盖数） | 模型调用秒之和 | 通道协议错误 | 实际工具动作 |
|---|---|---|---|---:|---|---:|
| GLM-5.3 | E:15 | 113,277 / 14,203 / 127,480 | 89,088 / 4,596（均 15/15） | 214.719 | 原生 0/15 | 31 |
| GLM-5.3-Flash | E:18 | 156,559 / 18,626 / 175,185 | 130,112 / 7,468（均 18/18） | 387.452 | 原生 0/18 | 26 |
| Sol | P:8 | 118,686 / 6,278 / 124,964 | 31,104（8/8） / null（0/8） | 104.986 | 文本 JSON 0/8 | 14 |
| Terra | V:25 | 535,337 / 41,110 / 576,447 | 300,800（25/25） / null（0/25） | 591.452 | 文本 JSON 0/25 | 47 |

复测 E 合计 33 次、302,665 token；P 为 8 次、124,964 token；V 为 25 次、576,447 token，不能把 302,665 当成四轮完整团队用量。缓存 input 总量为 551,104，66/66 次均有报告；reasoning 只有 GLM 的 33/66 次有报告，已知小计 12,064，完整 reasoning 总量仍为 null。模型名称回显已知 33/66，均来自 GLM；GPT 33 次 model 未回显，实际 effort/tier 在全部 66 次中均未知。请求 max/fast 与费用未知的边界和主矩阵相同。

时间保留三种互不替代的指标：每次模型请求 wall 之和；各 run wall 之和；最早 run start 到最晚 run end 的实际矩阵跨度。前者不包含调用间的工具和控制面时间，后两者还因并发而不同。跨时区时间统一按带时区的 UTC 时间戳计算；报告小数舍入不改变原始账本。

**仅作跨阶段账务合计，不合并成绩：** 两阶段共 230 个唯一 completed 调用，其中 229 笔 input/output 已知；已知 input=3,549,719、output=408,067，合计 **3,957,786 token 已知小计**。因为主矩阵的一次超时没有 usage，跨阶段完整 input/output/total 仍为 null；该合计也不包含下述实验组织开销。

主导、搭建和分析 agent 的调用/token/耗时/费用**不在候选 ledger 中**，本稿没有这些活动的 telemetry，不能填 0 或声称已计算实验全部人机投入。CLI 包装开销在候选调用用量内，但不能从现有字段准确拆成一个统一的“模型本体成本”。

## 解释范围与工程建议

本报告可以描述每个条件的原版分数、检查比例、实际团队与角色用量、延迟、协议适配问题，以及有原始消息支持的协作过程。不能据两题单次运行宣称模型综合能力优劣、真实单位费用优劣、强 Planner 保证团队成功、异构方案优于单体，或原生 DPswarm Orchestrator 已被完整验证。Solo 同时拥有完整信息且无需跨角色交接，这些差异也阻止将 Team/Solo 差值解释为纯协作收益。主矩阵的七项未知保留，复测通过不会补齐主矩阵缺失的 usage。

以下是后续产品与实验设计建议，本次没有验证其普遍收益，也不表示已作为产品机制实施。建议优先评估模型支持的原生工具接口，并单独监测协议成功率，因为本次文本格式失败阻止了部分动作落地，而原生复测没有这类错误。建议把规划交接改为带完整 schema、精确默认值、环境变量映射和约束的结构化清单，并检查缺项，因为三个 SPEC5 案例都暴露了信息遗漏。建议让关键澄清消息能在明确预算内重新调度 Planner，避免只能等待后续 Verifier 才补齐信息。建议扩充任务、种子与重复次数，并控制规划和部署栈差异，再讨论模型排名或异构收益。

主矩阵最终明细见 [REPORT.md](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/REPORT.md>)、[runs.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/runs.csv>)、[calls.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/calls.csv>) 与 [FINAL_AUDIT.json](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/FINAL_AUDIT.json>)。原生复测独立明细见 [复测 REPORT.md](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/REPORT.md>)、[runs.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/runs.csv>)、[calls.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/calls.csv>)、[roles.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/roles.csv>)、[models.csv](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/models.csv>) 与 [FINAL_AUDIT.json](<K:/秋招/项目/DPswarm/modelbench/team_eval20260903/native_followup/FINAL_AUDIT.json>)。
