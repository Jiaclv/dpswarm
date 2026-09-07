# C1：CM 开关消融运行器

**C1 已完成 18 次运行：三题 × CM ON/OFF × 三次重复。**模型为 Sol Lead＋两个 GLM-5.3-Flash worker，CM 使用 DeepSeek-v4-flash；Memory 与编辑保护关闭。

两臂均通过 4/9、生产交付均为 8/9。CM ON 相对 OFF 的总 token 减少 5.125%、冻结价卡费用减少 20.373%，未达到预设 15% token 节约门槛，也未观察到正确答案增加。普通调用输入减少与压缩自耗的详细拆分见[贡献报告](../../reports/2026-09-07/component-attribution/REPORT_ZH.md)，跨历史适用边界见[综合报告](../../reports/2026-09-07/full-history/REPORT_ZH.md)。

- [PLAN_ZH.md](../../reports/2026-09-07/plans/C1_PLAN_ZH.md)、`PLAN.json` 与 `C1_PLANNED_RUNS.csv` 保留最初的实验设计，不将计划验证写成运行验证。
- `contracts.py`、`runtime.py`、`execution.py` 实现开关合同、角色执行、候选冻结与调度。
- `environment.py` 保留实际测试退出信息；`transport.py` 处理工具适配边界；`resources.py` 记录容器与资源限制。
- `B1_OPTIONAL_RUNS.csv` 的 36 格角色份额实验，以及计划中的 Terra／Luna 模型对照仍为可选后续，不代表已经完成或已进入新队列。

执行计划与实际运行范围分开。新的模型调用须使用新的运行目录和重新核对的资格、预算、并发与输入合同；仓库不包含本地凭据、batch 工作区、模型响应和完整评分归档。当前实验结束，源码和报告同步不会自动续跑。

[报告索引](../../reports/README.md) · [同步验证](../../reports/2026-09-07/SYNC_VALIDATION.json)
