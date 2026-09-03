# AgentTeam 修复回归 v2（运行前冻结）

目标是验证工具协议、交接责任和澄清调度的组合修复。保持旧主实验及 native_followup 的程序、实例、调用和成绩不变。新结果不得解释为单因素因果或五种模型的更新排名。

## 已固定设计

- GLM-5.3 / GLM-5.3-Flash × SPEC5 / INT1 × 两次重复，共 8 轮；固定随机种子 20260903。
- Planner=GPT Sol，Verifier=GPT Terra。GPT 请求 max+fast；GLM 原生函数调用、thinking enabled、max。实际未回显设置保留 null。
- 每轮最多 20 次模型请求：P2、E6、V4、repair3、reverify3，另允许一次澄清、最多 P2 回复调用。Solo 若加入新版对照也必须使用相同 20 次上限。
- 每轮 token 准入边界 600,000；调用前预留 32,768，未知用量继续占预留，不充当实测值。它不是可精确预测输入/输出的硬 token cap；provider 超出预算后的实际用量如实记录并停止新准入。
- 8 轮最多 160 次调用、合计 token 准入上限 4,800,000。并发最多 2 轮。单轮 admission deadline 14,400 秒，单调用总超时最多 600 秒，澄清有效期 1,200 秒。批次超过 14,400 秒不再启动新 wave；正在运行的有界轮次收尾，单独记录超出时间。
- 工程/评分器错误或连续两个无有效 handoff 的结果停止后续 wave；已经启动的并行轮次保留结果，不替换失败、不自动补跑。

## 统一语义

原生 tools 或严格 JSON 工具 envelope 都经过同一工具名、参数 Schema、ID 检查。说明文本为 NoAction，消耗一次调用；不从说明中猜取 JSON。显式 finish_phase 结束角色阶段，不能替代最终任务验收。非法原生参数必须返回匹配 tool_call_id 的失败结果，避免下一轮破坏工具历史。

Planner 的 submit_handoff 必须通过从公开 spec 解析的有限事实契约；schema 包含字段与类型，答案值由 P 阅读原 spec 填写。E 仍只获得 brief 和有效交接包。交接校验不等于全部自然语言语义被证明；V 继续直接阅读原 spec。无法交接的运行不得偷补标准答案或放宽 gate。

E 的 request_clarification 在当前工具批次结束后挂起该逻辑阶段，新增真实 CP clarification 工作项；原 planning 提交保持不可变。回复绑定 run/item/attempt/node/session/epoch、request_id 和 contract revision；只能唤醒原 E 一次，不重置 E 调用数、历史、全局预算、工作区或租约。send_message 是通知，不是等待答复协议。

## 验证与证据

先离线测试原生 HTTP 契约、严格解析、工具参数、未知用量/重复结算、deadline、过期/跨运行/旧 epoch 回复、契约缺字段及真实 CP 集成。脚本化模型 fixture 仅证明调度行为，不充当模型能力成绩。

每次调用记录 requested/reported model、effort、tier，原始请求/响应、input/cache/output/reasoning token、用量来源与未知状态、时间、错误类别。每轮额外记录初次 V、最终分数、协议/NoAction 计数、handoff、澄清时间线、执行检查点和不可变 CP 证据。

持久化原生消息历史和工具结果。仅在完整结算边界支持显式 --resume；中途进程崩溃留下的未知模型/工具/评分器结果必须人工核对，禁止盲目重发。已有完成工具可返回 ledger 缓存结果，不能再次执行。恢复先检查 manifest、CP 身份与容器身份；不提前 accept 来释放租约。

最终冻结候选容器，再运行一次原始 TeamBench grader；只有原有 CP 证据门允许 accepted。新的可复用组件位于 dpswarm.team_runtime，当前接入点为独立 TeamBench runner；生产 Orchestrator 尚未转换为这个团队循环，避免混淆验证范围。

## 启动命令

从仓库根目录执行：

```powershell
python -m modelbench.team_runtime_v2.cli prepare
python -m modelbench.team_runtime_v2.cli run --workers 2
python -m modelbench.team_runtime_v2.cli report
```

prepare 固定程序/运行时/实例/image hash；源码变化后必须新建版本目录并冻结新 manifest，保留旧运行。
