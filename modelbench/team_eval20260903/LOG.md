# 实验主导日志

所有时间以机器 UTC 日志为准；此文件记录决策，逐调用事实以 `calls.jsonl` 和 artifact 为准。

## 计分前

- 用户明确要求适合 Agent Team 的 benchmark。停止 HumanEval 草案，改用 TeamBench 的规划、执行、验证角色协议。早期草案没有计分模型运行。
- 冻结 TeamBench commit `d185aef1916fd86a9ba554d581fd256319a973af`；审查多个任务后选 SPEC5 generated seed 0 与 INT1 tracked static；在看到候选成绩之前排除确认存在 grader 缺陷的任务。
- 2026-09-02 23:37–23:38 UTC：五模型连通性预检全部返回 OK；不计入成绩。详见 `preflight/summary.json`。GPT 实际响应中的模型和服务 tier 未回显，保留 null；CLI prompt overhead 纳入使用量。
- Docker 权限预检通过；未修复实例原始评分为 SPEC5 0/19、INT1 1/12。官方脚本取 Git 原始 LF 字节，不执行 Windows CRLF checkout。
- 保留官方角色提示和工具 schema，采用自己的严格 JSON 循环，避免官方 loop 的 DONE 子串误终止。修复复检临时目录复用问题；最终先冻结所有候选容器，再记录 attestation/工作区哈希、运行一次隐藏 grader。
- DPswarm ControlPlane 接入范围为 item/node 生命周期、session/epoch fence、token、提交、证据验收和回放。动态路由、原生 Orchestrator 与 DSH 插件不在本轮覆盖范围。
- 预注册矩阵、参数、预算和限制见 `PLAN.md`，实际冻结程序及输入哈希见 `manifest.json`。不根据分数临时改提示、追加修复、替换模型或选择最好的一次。

## 结果入口

运行完成后由 `analyze_results.py` 从原始日志重建 `REPORT.md`、`calls.csv`、`roles.csv`、`runs.csv`。全部结果（包括失败与基础设施异常）保留在 `results/`；逐模型调用包括开始/结束 UTC、使用量来源、参数、原文、工具动作、错误和耗时。

## 运行中发现与补充实验

- 主矩阵多次 GLM 文本 JSON 动作解析失败，导致模型生成内容没有转化为工作区修改。为避免把这一适配因素混同编码能力，额外冻结 `native_followup/PLAN.md`：两 GLM × 同两题的四次探索性原生工具调用复测。它在主矩阵结束后启动，保留同一角色模型、预算、输入实例和 grader；单独记账，不替换主分。P/V 每次重新生成，仍存在规划内容的随机差异，不能把小样本前后差异称为严格因果证明。
- `SPEC5_config_system__team__gpt-5.6-sol` 的初次实现阶段：Planner 提供了规则但漏传完整十项 schema 的若干名称/default/env_var；Executor 多次通过 send_message 请求补全。当前顺序 phase 调度不会在 E 阶段重新激活 P，直到 V 阶段才补齐 schema 并触发既定修复。证据：该运行 `messages/dialogue.jsonl`、`first_attestation.json`。这是团队信息交接和调度的观察，不是纯 Executor 编码能力结论；运行器没有在中途替模型补答案或改变预算。

## 主矩阵结束与复测启动

- 2026-09-03 00:35:22 UTC：12 轮主矩阵全部终止，164 次调用。8 轮通过；3 轮正常结束但未通过；Flash 的 INT1 轮包含一次 600.3 秒传输超时，最终原始检查为 1/12，单列 `transport_error`。
- 该超时调用没有返回 usage。163/164 次调用的 token 已知，完整总量为 null。冻结 runner 的该轮 `result.total_tokens=221561` 是已知小计；正式 CSV 和报告按 ledger 重建，保留未知完整总量。不能把此小计或缺失用量当作完整用量或零。
- 主矩阵实际历时 3050.212 秒（UTC 23:44:32.480 至次日 00:35:22.691），各轮累计 wall 时间 5377.33 秒；并发历时与累计时间分别报告。
- 最终离线账本/哈希核验：0 项已知不一致，7 项 unknown 均由这一次 usage 缺失传播而来。因此 `FINAL_AUDIT.json` 的全量结论为 unknown，不将其宣称为全部审计通过。无重新调用模型或 grader。
- 主矩阵结束后启动已冻结的四轮 native followup；只改变 GLM 工具传输接口，其他条件按复测计划执行。主矩阵结果、失败和冻结文件保持原样。
- 原生复测的 GLM-5.3 SPEC5 首次验证发现 `WEB_DEBUG_MODE` 与 `[0,3600]` 不符规格。只读核对实际规划消息及写入前的 wire request：Planner 未传递正确的 `WEB_DEBUG` 与 `[1,300]`，Executor 也未追问即填写猜测值。Verifier 反馈后既定修复改正并最终通过；不能归因为 Executor 收到完整正确规格仍实现错误。详细证据写入综合报告，未把事后分析发送给候选模型。

## 全部结束

- 2026-09-03 00:47:54.907 UTC：四轮原生工具复测全部结束、全部通过。GLM-5.3：SPEC5 19/19、INT1 12/12；Flash：SPEC5 19/19、INT1 12/12。三轮首次验证未通过后使用了计划内修复，GLM-5.3 的 INT1 无需修复。原生复测的 66 次调用未出现通道协议错误或传输异常。
- 复测完整用量为 1,004,076 token；GLM-5.3 整队两题 455,933（E 127,480），Flash 整队两题 548,143（E 175,185）。复测实际并发历时 747.244824 秒，各 run wall 累计 1363.814 秒，各模型请求 wall 累计 1298.609 秒。
- 复测最终账本/评分/哈希一致性核验通过：1788 项通过、0 项失败、0 项未知。主矩阵原有 7 项 usage 未知仍保留。每轮只运行一次隐藏 grader，没有事后补调用、重新评分或选择最佳运行。
- 两阶段仅在账务层合计：230 个唯一 completed 调用，229 次完整 input/output 已知；已知 input=3,549,719、output=408,067、total=3,957,786。完整总量为 null，不能把已知小计当完整消耗；两阶段分数不合并。五次连通性预检和主导/搭建/分析活动不计入此账务小计，前者另有日志，后者缺少 telemetry。
- 最终实时检查 `docker ps -a --filter label=dpswarm.teambench.owner` 返回空；所用实验角色/评分容器均已清理。TeamBench vendor 的 Git 工作区无改动；核心与冻结实验源哈希经最终审计核对。
- 报告文件：主矩阵 `REPORT.md` 与四种 CSV；复测对应文件在 `native_followup/`；跨阶段方法、结果和协作案例见 `EXPERIMENT_REPORT.md`。工具通道与信息交接会影响本次结果，两题各一次的样本不支持模型综合排名或完整原生 Orchestrator 验收结论。
