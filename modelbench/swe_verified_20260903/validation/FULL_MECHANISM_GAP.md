# 当前 SWE 接线范围与完整机制缺口

2026-09-03 用户追问是否实际启用 DPswarm、分裂/裂变及 CM 后核对。
这是对既有冻结批次的范围说明，不修改 pilot_v2 的条件，也不把建议当成已实现。

## 已确认的事实

- pilot_v2 的 `SweControl` 初始化真实控制面；这不是“主 agent 启用了协作”的证据。
- 单 agent 条件只有 bash/finish；DPswarm 条件还提供 delegate/collect/review_worker/reply_worker。
- delegate 仅调用 `create_work_item(DERIVE)`。截至核对时，已启动的 8 个 run 尚无派生请求/子 agent 模型调用。
- SPLIT、FISSION 均没有在此适配器暴露，CM 没有接入。应分别记 `not_exposed`、`not_integrated`，不能说模型自主选择了 0 次。
- 原产品核心含这些机制；SWE 适配器覆盖不足，不能将本批称为完整 DPswarm 多机制能力评测。

## 下一批的最小接线方案（尚未实施）

1. 保留现有主 agent 单干起步；派生、分裂、裂变作为按需工具，不强制固定 Planner 或团队。
2. DERIVE 保持独立 work item、manifest、session、容器、交付和验收。
3. SPLIT 使用 `ControlPlane.split`，让现有 Lead 成为主，只增加一个同模型 assistant。
   两者共享 item、使用 peer 通道；副手不能独立 submit，取消不能 terminate 共享主任务。
   预检查重复分裂并在创建失败时结清 provisioning 副手，防止半准入。
4. FISSION 必须真实创建子 Team、多个 item 和 DAG，不能把循环 delegate 改名充数。
   全请求、依赖、权限与容量先验证；依赖产物被 Lead 接受后才启动下游。
   当前 Lead 为 B，核心只允许 S Lead 裂变；当前 root 点数也不足以容纳两个子 Team worker。
   下一批须冻结实验准入等级与容量，不能伪造 AA 评分或绕过核心门禁。
5. CM 通过 ContextManagerLLM 的 complete_fn 接入真实 SweTransport，使用独立 call_id、同一预算及全局调用槽。
   现有 ContextAssembler 在选材超过 token_budget 时才触发压缩；不是达到 70% 就必然触发。
   CM 不占常驻 node/worker 点，但实际调用、token 和耗时必须计入总预算。
6. CM 的简账包含字符估算及默认零费用，不能作为实测权威账。
   以原始 nullable usage 为准，新增 context job 记录、触发 node/item 归属；缺失不填零、不以估算冒充实测。
7. 每个机制分别记录 exposed、requested、admitted、activated、completed/rejected/failed。
   每个 agent 按 node/session/epoch 汇总，CM 按 job 汇总，二者通过触发关系关联；总账只累加一次。

## 新批次启动前的必要验证

- 默认单干不生成子节点；SPLIT 同 item、同模型、一主一副、副手失败不终止主。
- FISSION 的 S 权限、真实两 worker 容量、批量预检查、依赖准入与清理。
- CM 阈值未触发零调用、触发后真实 usage、未知保留、预算恰好记一次、并行 job 不串账。
- 总账等于 Lead＋worker＋assistant＋CM；所有真实模型请求可回查身份与原始证据。
- 使用新批次/独立 source snapshot，保留旧批次结果及基础设施开销，不向新候选提供旧评分反馈。

只读接线依据：`dpswarm/control.py` 的 create_work_item/split/peer_send/peer_deliver/degenerate_assistant/record_tokens；
`context/assembler.py` 的 assemble；`context/manager.py` 的 ContextManagerLLM/_account；
`observation.py` 的 token_ledger；当前 SWE control.py 的 delegate/get_usage。
