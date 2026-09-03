# SWE 固定派生团队：五种子模型对照

用户于 2026-09-03 明确要求“先固定选择 agent team”。这是实验条件变化，产品仍默认单 agent，按需开启协作。

本目录 pilot_v1 首个 pair 在 Matplotlib 容器权限初始化时失败，两次 run 均 0 模型调用、0 token，尚未准入 worker，也未评分。记录和冻结快照保留为基础设施失败，不计模型成绩。pilot_v2 修复混合文件属主的初始化兼容问题后使用相同两题、角色、顺序和预算；候选仍为 UID1000、无网络、无 capabilities、无宿主挂载。新验证 gate 和源码重新冻结。

前一受限批次 pilot_v2 已在 5 题、10 run 完整结束后停止；106 次调用、3,355,362 token、实际派生 0。双方各 3/5 通过。SPLIT/FISSION 未暴露、CM 未接入，因此它不能代表完整 DPswarm。更早 pilot_v1 的脱敏基础设施失败另有 2 次调用、33,281 token。旧结果、快照和审计保留，新候选不接收旧回答或评分反馈。

## 固定设计

- 数据与官方 grader 继续使用原始 SWE-bench Verified revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`、官方 harness v4.1.0 commit `726c5461e2ef52d83cf1ea2107870a8bb3328d57`。沿用已验证的隔离容器与可信评分端。
- 在原来按元数据冻结的 10 题顺序中，取此前未开始模型调用的前两题：`matplotlib__matplotlib-25122`、`sphinx-doc__sphinx-8035`。不是按答案、难度或结果择题。
- 每题 6 个 arm：Sol 单 agent；Sol 主 agent＋两个 GLM-5.3 worker；两个 GLM-5.3-Flash worker；两个 GPT Sol worker；两个 Terra worker；两个 Luna worker。共 12 run、20 个预定 worker 实例。
- 固定团队的两个 worker 在 Lead 首次模型调用前由实验协议准入并启动：一位负责生产代码修复，一位负责针对完整 issue 写回归测试。两位看到同一问题、同一初始仓库快照，在独立容器工作。Lead 负责获取交付、验收、采用 delta、整合及验证。
- 这是协议强制启动，不能称为主模型自主判断。Lead 不再额外派生第三人，所有模型 arm 使用相同两个角色、工具与预算。没有固定的额外 Planner 模型。
- 使用真实 DPswarm DERIVE、独立 item、manifest、session、准入与交付账本。SPLIT/FISSION 仍未暴露，明确记 `not_exposed`；CM 自修订 4 起按需接入（见"预算、并行和工具"），此前批次保持 `not_integrated`。本阶段先测真正发生的派生团队，不声称完整机制覆盖。

## 预算、并行和工具

每 run 总计最多 28 次模型调用、600,000 token 准入额度、1,800 秒推理执行时间。两个 worker 各最多 8 次工作调用，与 Lead 共享预算，并给 Lead 保留最后 2 次调用。第 8 次工作调用未 finish 的 worker 获准一次 finish-only 收尾调用；该调用照常准入、结算、记账并标记 closing 事件，不绕过取消与时间上限。Lead 预算准入失败时，已准入仍在执行的调用获得一个有界收束窗口以结算真实用量，期间禁止新准入；用户取消与真实时间上限仍立即生效。输入预留是字符估算＋请求输出 cap，不能保证服务端绝对 token 封顶；实际超额仍记录。GPT CLI 不强制输出 cap，差异披露。

GPT 请求 max / fast，GLM 请求 thinking enabled / max；只有真实回显才填写 observed，否则 null。预定最多 336 次调用、7,200,000 token 准入额度，不为追求更好成绩补跑。前批费用不混入本批模型对照，但单列累计开销。

最多两个 run 并行，全局四个模型调用槽、四个容器槽。每个候选或 testcase 容器 2 CPU / 3 GiB；可信 Linux grader controller 另占一个槽并限制 768 MiB / 1 CPU。容器槽不足时 worker 排队，队列时间计入墙钟。grader 预留两个槽。

同一实例六个 arm 按固定顺序两两并行；第二题倒序，减轻顺序混杂。同一 arm 只试一次。镜像按实例准备，六个 arm 收束后只删除本实验拥有且无容器引用的 image，不全局 prune。

工具仅在隔离、无网络、无宿主挂载及凭据的 /testbed 执行。通用提示明确 bash 中不保证存在 apply_patch，可使用 Python 或已发现的命令修改文件；两个条件相同。原生工具结果按 ID 对齐，非法历史不继续；取消、超时、未完成交付及清理失败均保留。

CM（context manager）按需接线：代码（非模型）在序列化历史超过 `cm_context_budget`（默认 24,000 est tokens，量自 pilot_v3 p90）时触发一次零损压缩调用。切分只落在 user 消息边界，保证原生 tool_call/响应配对完整；CM 调用使用独立 `cm-` call_id、role `cm`、模型 `glm-5.3-flash`，计入同一预算与全局调用槽；归因经 `cm_call_*` 事件（含触发 node/item），不挂在 agent handle 上。CM 失败或低调用预算（剩余≤4）时静默降级为未压缩历史；压缩前原文落盘 `cm/<call_id>.before.json`。

## 评分、日志与停止

所有候选结束并冻结最终补丁后才进入官方评分；gold/test_patch/F2P/P2P 不进入候选上下文。评分结果不反馈给该 run 的模型。worker 的交付或 CP 采纳不等于该 worker 独立通过官方测试，最终分数属于整支队伍。

每个模型请求都有独立 call_id 和原始请求/响应；按 run、arm、agent、node、item、session、attempt、epoch、parent、role、model 归属。分别记录输入、缓存、输出、reasoning、总 token、起止时间和服务设置。缓存已含于输入，reasoning 已含于输出，不重复累计。按模型聚合时仍分开 Lead 和 worker；同模型的多个 agent 各有一行。

强制请求、真实准入、激活、首次实际模型调用分别记事件，并标记 `experiment_protocol` 来源。若预定 worker 没有真实调用或执行失败，记录覆盖/失败状态，不能悄悄按单 agent 成功算作正常团队。CM 单列未接入、usage null，占位不进入真实 agent 或调用总数。没有可靠收费来源，美元费用为 null。

每个并行 pair 收束后，出现基础设施/grader 故障、未知或悬空用量、固定团队实际 worker 调用覆盖不足时停止后续调度；普通候选 unresolved 继续。明确的空补丁候选失败也继续，官方是否完整评分单列。新 pair 调度截止为开始后 3 小时，已开始的 run 按各自上限收束。

`STOP_AFTER_PAIR` 控制文件允许用户调整方向时在安全边界暂停；不改变冻结 manifest，不杀掉正在进行的模型请求。已完成 run 可跳过，未完成 run 不自动重放。每次执行/暂停写独立 invocation 记录。发现实现 bug 需停止并开新修订，不覆盖原批次。

## 解释范围

两题只用于检验管线与初步比较同角色子模型，不足以给五个模型排稳定名次，更不是 500 题榜单成绩或 SOTA。强制团队结果不能证明自动启用策略正确。该适配器没有验证 DSH 宿主桥接；先前桥接 HOLD 保持。

完整分裂/裂变/CM 的接线缺口与建议单独保留在旧批 validation/FULL_MECHANISM_GAP.md，不把提案当成实现。

官方依据：[数据](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)、[评分说明](https://www.swebench.com/SWE-bench/guides/evaluation/)、[榜单](https://www.swebench.com/)、[提交规则](https://www.swebench.com/submit.html)。
