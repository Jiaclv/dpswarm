# 本轮实验恢复记录

用户指令：2026-09-05「那就继续」。范围承接已授权的 A1 十次及资格通过后 A2 八十次，不追加其他实验。

首题 a1-01-D 已完成模型工作并通过官方评分，随后打印结果中的特殊字符触发 Windows GBK 编码异常；原控制器又以候选阶段的结果覆盖了完整终态。此次恢复只修复外围调度和结果投影，冻结的 86 份模型运行源码、实验顺序、模型配置和原始评分数据保持一致。

首题恢复没有新增模型或评分调用。恢复前后均为 28 次真实调用、444,663 tokens、USD 2.3334739373975615 API 等价费用；该费用不是订阅实际扣款。官方报告包含 1 项 FAIL_TO_PASS 和 179 项 PASS_TO_PASS 全部通过。推理耗时保留为 320.016 秒，原始包含评分的总耗时已丢失，明确记录为未知。

恢复前备份了 8 份旧投影和控制记录，并复验 11 份来源文件哈希。首题补丁、原始候选结果、调用元数据、冻结预算及官方报告均保留。阶段承接已完成 1 次、已准入 600,000 tokens 和已知费用，不重新计算一轮预算；A1 继续采用原始开始时间加 9 小时的派发截止，整个首包也保留原始总时限。

恢复脚本与断点控制器位于 operations 下，未修改已冻结运行代码。后续进程使用 UTF-8 输出；异常退出时保留已提交结果，并结算已经发生的调用、确认资源清理。A1 仍须通过原有 D/T 实际交付和 R2 真实选择资格，之后才能准备及启动 A2。

- [首题恢复审计](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/batches/a1-v2/recoveries/stdout-utf8-20260905/recovery.json)
- [首题恢复结果](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/batches/a1-v2/results/a1-01-D/episode_result.json)
- [A1 当前状态](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/batches/a1-v2/state.json)

恢复启动时间：2026-09-05 14:36:50（悉尼时间）。A1 控制器 PID 41052；后续有限流水线 PID 41244。已核验第 2 个实验 a1-01-T 收到 Sol、Terra、GLM 的真实响应，未见调用错误；完整调用数随运行增长，以状态文件和调用账本为准。恢复控制器 33 项测试、流水线及新增 Unicode/时限 22 项测试通过。

- [恢复启动与实测快照](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/batches/a1-v2/recoveries/stdout-utf8-20260905/resumed_observation.json)
- [恢复后的有限流水线状态](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/operations/pipeline-run-v3/pipeline_state.json)

本记录为启动时快照；后续进度以运行状态文件为准。阶段已结算费用目前只包含已完成的首题，不能当成活动实验已产生费用的总数。
