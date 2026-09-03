# DPswarm Agent Team 五模型试验计划（预注册 v2）

本计划在任何计分模型运行前冻结。2026-09-03；实验主导者为当前 Codex 主 agent。此前 `../pilot20260903/PLAN.md` 的 HumanEval 草案已停止，不属于本试验。模型连通性预检不计分。

## 问题和边界

比较 GLM-5.3、GLM-5.3-Flash、GPT Sol、Terra、Luna **作为团队 Executor 子 agent** 时的完成质量、完整团队 token 和耗时。Planner 固定 Sol，Verifier 固定 Terra，均实际调用。主 agent 不代写解答、不补救失败样本、不充当模型主观裁判。

采用 [TeamBench](https://github.com/ybkim95/TeamBench)，冻结 commit `d185aef1916fd86a9ba554d581fd256319a973af`。它提供完整规格/简要规格的信息差、规划→实现→独立验证、反馈修复及可执行 grader，比函数补全更贴合 agent team。这里只做两题小样本工程 pilot，不是官方排行榜复现，也不能据此给模型综合能力排名。

## 冻结任务

| 任务 | 实例 | 团队挑战 |
|---|---|---|
| SPEC5_config_system | 官方 generator，seed=0；spec、brief、workspace、expected 同批生成 | 规划者传递配置优先级、类型转换和校验约束；执行者实现；验证者逐项检查 |
| INT1_pipeline_repair | 官方 commit 的 tracked static 实例 | 修复多模块数据管道接口、数据质量及报告格式；验证端到端结果 |

INT1 静态实例是有意选择：其 seed=0 generator 的邮件样本与 grader 中固定样本不一致。DIST1、INFRA2、PIPE2/3 等审查发现 grader 假阴性或答案泄漏风险，本轮计分前排除；证据见 `task_audit/TASK_SELECTION.md`。不根据候选模型成绩选题或修 grader。原版脚本直接从 Git blob 取 LF 字节，避免 Windows CRLF 改变 shell 行为。

## 条件和预算

每题 6 个条件，共 **12 个运行**，每条件每题一次：

1. Team：P=Sol、V=Terra、E 分别为上述五个候选模型。
2. Solo control：Sol 获得完整规格和全部工作区工具，独立实现、自检并交付；与 Team 相同的最大模型调用次数。它是调用次数上限匹配的对照，不是实际 token/成本完全匹配。

Team 最大调用数：Planner 2、Executor 6、Verifier 4；首次验证未 pass 时最多一轮 Executor 修复 3、Verifier 复检 3，总计 **18**。Solo 最多 18。每次回复最多请求 8 个工具动作；调用超限或 JSON 格式错误消耗该次模型调用，不静默重试。角色历史在各 phase 内保留，修复阶段传入前一阶段完整历史及验证反馈。规划者每个运行重新调用，同模型响应波动是本 pilot 的限制。

整个矩阵按 Python `random.Random(20260903)` 固定打乱；最多两个运行并发。每个运行独立目录和容器，禁止跨运行通信和答案共享。每调用超时 600s，每工具命令 60s，每 grader 180s；每运行模型调用输入+输出累计达到 600,000 token 后停止新增调用（只能在调用边界检查，不是严格服务端 cap）。所有提前停止/失败留档，不补跑挑最佳。不使用隐藏 grader 结果触发修复或选择 workspace。

## 模型与调用方式

| 模型 ID | 通道 | 请求参数 |
|---|---|---|
| glm-5.3 | 已配置 GLM coding API | thinking=enabled, reasoning_effort=max, temperature=1.0, max_tokens=32768 |
| glm-5.3-flash | 同上 | 同上 |
| gpt-5.6-sol | 已登录 ChatGPT 的 Codex CLI 0.149.0 | model_reasoning_effort=max, service_tier=fast |
| gpt-5.6-terra | 同上 | 同上 |
| gpt-5.6-luna | 同上 | 同上 |

GPT 每次调用显式忽略用户配置，禁用 CLI shell/multi_agent/web，read-only、ephemeral、approval=never；使用统一 JSON 文本工具协议，由本实验 Docker 工具层执行动作。GLM 同样使用此协议。GPT CLI 有自身系统提示开销，GLM 走 API；因此比较的是这里的实际可用部署栈，不是同推理栈的纯权重比较。CLI 无可强制的输出 token 上限，记录 `cap_enforced=false`。`max` 和 `fast` 是请求值；服务端未报告的实际 model/tier/effort 保留 null。GLM 官方文档确认 max/思考参数；不把请求值冒充服务端确认。

## 隔离、控制面和评分

保留 TeamBench 官方角色 system prompt、工具 schema 和未修改 grader。实验自建严格 JSON agent loop（官方 loop 以文本中的 DONE 子串结束，可能误认代码/工具 JSON），使用 Docker OS mount 权限：Planner 仅完整规格只读；Executor 工作区可写、只能读 brief；Verifier 工作区只读、只能写 submission。Verifier 可把工作区复制到容器 `/tmp` 后执行需要写文件的测试。无网络，无宿主凭据/仓库/答案目录挂载；expected 仅最终 grader 容器可见。`send_message` 只是本运行的内部角色消息。

DPswarm 现有 ControlPlane 管理角色 work item、node、usage、submission 和最终评分证据；本轮是实验适配器接入，不声称覆盖了原生 Orchestrator 自动 fission/router 或 DSH 插件完整集成。模型声明完成不等于 accepted；最后统一由原版 grader 的新生成 score.json 计分。保存 grader 前工作区哈希和角色 attestation；grader 可运行程序生成输出，但只在模型结束后执行一次。

主指标：原版 `pass`；辅指标：原版检查通过比例、失败项、Verifier verdict、验证误放行（verdict pass 但 grader fail）、实际调用数、工具数、token、wall time、异常和超时。分别给出 Executor 指标及完整团队 P+E+V 指标；不能只算便宜 Executor 而遗漏规划/验证成本。

## 日志和证据

- `manifest.json`：本计划、程序、任务实例及 Docker image 哈希，固定顺序、预算、环境。
- `results/<run>/`：独立 task/workspace、内部消息、逐 phase 对话和工具结果、DPswarm events、最终原始 grader、result.json。
- `calls.jsonl`：每次调用 started/completed；按 call_id 去重，以 completed 为统计口径。保存 UTC 时间、模型请求/报告、参数、状态、错误、墙钟、provider usage、prompt/response/artifact 路径。
- input_tokens 包含 cached_input_tokens；output_tokens 包含 reasoning_tokens，二者不能重复相加。缓存/推理字段未报告为 null。汇总 token 为 input+output；它是使用量指标，不代表账单。价格和实际金额无可靠来源则 null。
- `runs.csv`、`roles.csv`、`REPORT.md`：所有成功和失败均列出，报告基础设施失败与模型任务失败的区别；小样本仅作观察，不报虚假显著性。

文档依据：[TeamBench 原仓库](https://github.com/ybkim95/TeamBench)、[GLM-5.3](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)、[GLM-5.3-Flash](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)、[Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)。
