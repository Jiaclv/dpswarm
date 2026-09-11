# v16 后续完善与实验计划附件

主入口：[完整计划](../NEXT_EXPERIMENT_PLAN_20260905.md)。状态为可评审提案；未实施修复、prepare 或运行模型。文件中的490个格包含互斥方案、条件分支和未选择任务占位，不是已批准运行的总表。

- `stage_plan.csv`：阶段、样本单位、候选机会、依赖、调度时间窗与设计额度。
- `planned_cells.csv`：逐题/逐臂/重复数、角色顺序、参数与策略；不是现有CLI可直接执行的wave。
- `plan_contract.json`：互斥范围、模型顺序、S2资源分配、费用能力门槛与确认指标。
- `engineering_review.md`：源码锚点、工程修改契约与确定性验收。
- `design_review.md`：实验/统计审查；实施范围与最新补充以主计划和合同为准。
- `cost_review.md`、`cost_baseline.csv`：100行历史配置与显式规划代理，价格保持历史冻结情景。
- `flow.html`：可查看的依赖图；`.flowmap/post-v16-plan.json` 是对应账本。实验节点均待执行，方案互斥与条件门槛以主计划为准。
- `plan_validation.json`、`delivery_validation.json`：矩阵计数、费用额度、链接与来源核验。

`build_plan_artifacts.py` 只生成本目录设计文件，并通过FlowMap CLI更新本计划专用账本；不调用模型、CLI prepare、grader或Docker。可在当前仓库使用已有Python3.12离线复算。已有另一份 `.flowmap/flow.json` 未修改。

工程笔记的E1–E7与主计划E01–E08各自有编号，不表示两个待执行清单：主E01对应工程E1/E6，主E02对应工程E3，主E03对应工程E2，主E04对应工程E4，主E05对应工程E5，主E06对应工程E6，主E07对应工程E7，主E08是向后兼容验收。

后续修订先更新主计划，再重新生成矩阵及合同；不能手改某张CSV后跳过主计划、冻结或阶段门禁。
