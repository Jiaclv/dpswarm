# B1：预算与固定团队对照运行器

本目录保存 2026-09-06 的预算／角色配置实验代码与冻结计划。实际执行后来收缩到核心 MVP；原计划的 180 格不是完成数量，也不是新的默认启动授权。

后期 Coding Plan 结果包含 16 格，其中核心 9 格是三题 × T00、T11、S-S 三臂，不能再与 16 格相加。核心观察：T00 通过 2/3，T11 1/3，S-S 0/3；预算增加没有带来一致收益。不同阶段通道、候选与资格见[全历史报告](../../reports/2026-09-07/full-history/REPORT_ZH.md)。

- `contracts.py`、`plan_contract.json`、`planned_cells.csv`：配置与预设单元格合同。
- `runtime.py`、`execution.py`：预算、调度、续接、MVP 收束及证据冻结。
- `transport.py`、`glm_stream_http_worker.py`、`coding_quota.py`：实际通道、流式读取、超时边界与 Coding Plan 额度识别。
- `provider.py`：共享 provider 并发准入。
- `tests/`：离线合同测试；其中资格、镜像描述和续接测试依赖本地 A2/B1 原始归档。

历史实验以 Coding Plan 接口完成后期运行，普通按量接口的余额与 Coding Plan 额度分开判读。凭据通过本地配置提供，不包含在公开源码或报告中。

代码导入和查看计划不启动模型。新执行需要重新明确范围、准备输入和镜像、完成相应资格检查，并在新的运行目录冻结配置；本仓库没有打包历史 batch、Docker 镜像和完整调用记录。归档依赖测试在原始工作区运行的结果，不等于空归档克隆环境可以复现同一资格证明。

[预算计划](../../reports/2026-09-07/plans/B1_PLAN_ZH.md) · [机制贡献拆解](../../reports/2026-09-07/component-attribution/REPORT_ZH.md) · [验证记录](../../reports/2026-09-07/SYNC_VALIDATION.json)
