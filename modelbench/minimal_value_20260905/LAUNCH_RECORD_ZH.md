# 实验启动确认记录

确认时间：2026-09-05T03:38:31.616558+00:00。本文件记录启动时快照；最新状态见下方运行状态文件。

当前A1批次：a1-v2；活动实验：a1-01-D；完成0/10；停止原因：无。
有限调度状态：running / waiting_a1。
已观测到 21 条完成的真实调用记录，包含主解题、独立测试者和上下文压缩角色。GLM和DeepSeek回显身份一致；Codex配置请求Sol且已返回usage，但CLI没有回显模型身份。

- A1十次合格后，先验证A2的16题环境资格，再执行80次比较；不会自动追加其他实验。
- 一次运行一个完整实验；双候选或团队worker在实验内部并行。
- 原授权的token、调用次数、时间与API等价费用停止线保持不变。

- [A1当前运行状态](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/batches/a1-v2/state.json)
- [有限调度当前状态](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/operations/pipeline-run-v2/pipeline_state.json)
- [冻结实验清单](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/batches/a1-v2/manifest.json)
- [启动授权记录](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/batches/a1-v2/user_authorization.json)

初次a1-v1在任何模型调用前遇到worker环境未启动问题，原始失败记录保留。修复后157项准入检查通过，D/T真实容器无模型生命周期检查通过，随后启动本批次。
- [首次故障与清理复核](K:/秋招/项目/DPswarm/modelbench/minimal_value_20260905/batches/a1-v1/recovery_audit.json)
