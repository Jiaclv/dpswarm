# 后续实验计划：补齐 solo 基线覆盖 + 异构团队（最终版）

时间基线：2026-09-03。当前 harness 已推进至 revision 6/7（`gate_revision6` 为当前 gate，CM 已调参：`cm_context_budget:12000`、`cm_max_tokens:2048`、`cm_keep_recent:4`）。
前置文件：`PILOT_V5_GLM_CM_ANALYSIS.md`、`DETAILED_TABLES_20260903.md`、`PLAN.md`。
本计划为描述/提案，不动已完成批次的 manifest / result / 冻结快照。

## 0. 用户已拍板的决策（2026-09-03）

| 决策点 | 结论 |
|---|---|
| solo 基线 | **全 5 模型**各跑一个 solo |
| 异构 worker 范围 | **全组合** 实现×回归 = 5×4 = **20**（实现≠回归，有序） |
| 统计强度 | **1 run / arm，不重复**（与 rev3 口径一致，方向性结论） |
| 任务 | **只 Sphinx-8035**（难题，区分度高）；更难任务留作下一波 |
| 预算 | **无限**（run 数 / token 不设上限） |
| lead 模型 | **保持 gpt-5.6-sol**（我们一开始选定的）；lead 变体留作可选后续 |

## 1. 设计缺口回顾

- **缺口 A（solo 覆盖）**：只有 `solo`(gpt-5.6-sol)，无 `solo_<model>`，答不出"某模型单干 vs 当 worker 哪个好"。
- **缺口 B（异构）**：只覆盖 lead≠worker；队内两个 worker 恒同模型、不同角色（实现/回归）。**worker-1≠worker-2 不支持**（`runner.py:127` 强制 `one exact worker_model`）；**lead 换模型不支持**（`runner.py:143` `lead_model='gpt-5.6-sol'` 写死）。

## 2. 本轮 arm 清单（Sphinx-8035，同一套当前适配器）

候选模型 M ∈ {gpt-5.6-sol, gpt-5.6-luna, gpt-5.6-terra, glm-5.3, glm-5.3-flash}，记 5 个。

### 2.1 solo（单 agent = 该模型）——5 个
```
solo_gpt-5.6-sol   solo_gpt-5.6-luna   solo_gpt-5.6-terra
solo_glm-5.3        solo_glm-5.3-flash
```

### 2.2 同构固定团队（lead=gpt-5.6-sol + 2×M）——5 个
```
fixed_gpt-5.6-sol   fixed_gpt-5.6-luna   fixed_gpt-5.6-terra
fixed_glm-5.3        fixed_glm-5.3-flash
```

### 2.3 异构团队（lead=gpt-5.6-sol + worker-1=实现模型A + worker-2=回归模型B，A≠B）——20 个（有序）
对每个 (A,B)，A 从 5 选，B 从剩余 4 选，共 5×4=20。例如：
- `hetero_glm-5.3__gpt-5.6-luna`（glm 实现 + luna 回归）
- `hetero_gpt-5.6-terra__glm-5.3-flash`（terra 实现 + flash 回归）
- …（全部 20）

**总计：30 个 run。** 每个 arm 1 个 run，预算 ≤28 调用 / 600k token / 1800s。全部走当前适配器（CM 开启）。

> **口径说明**：rev3 的 fixed/solo 数据跑在修订 3（CM 未接入），本轮全部在当前（CM-on）适配器重跑，保证本轮内部**同条件可比**。rev3 数据保留为"无 CM 历史基线"，与本轮并列、不混用。

## 3. 代码改动点（开新 revision，不覆盖旧批）

### 3.1 `cli.py · prepare()`
- arm 生成改为三类：
  ```python
  arms = ['solo_' + m for m in MODELS] \
       + ['fixed_' + m for m in MODELS] \
       + ['hetero_' + a + '__' + b for a in MODELS for b in MODELS if a != b]
  ```
- schedule 条目：`condition ∈ {solo, fixed_team, hetero_team}`；`lead_model`（本轮恒 gpt-5.6-sol）；`worker_models` 为列表（solo 为空；fixed 为 [m,m]；hetero 为 [a,b]）。
- manifest：`worker_model` 标量 → `worker_models` 列表；新增 `lead_model`；`conditions`、`arms` 相应扩展。

### 3.2 `runner.py`
- 撤掉 `fixed_team requires one exact worker_model` 断言（`runner.py:127`），改为按 `worker_models[i]` 给第 i 个 worker 分配模型；`request = {'model': self.entry['worker_models'][worker_index], ...}`（替换 `runner.py:248`）。
- `lead_model` 从 `self.entry` 读（替换 `runner.py:143` 写死）；solo_<model> 的单 agent 模型 = `<model>`（允许 solo 的 agent 模型参数化）。
- 两个 worker 仍按 `worker_roles`（生产实现 / 独立回归）分工；模型与角色正交。

### 3.3 `validation/audit_results.py` + gate
- 现有条件判断集中在 `audit_results.py:495,505,517,603,634`（把 `fixed_team` 当唯一团队条件）。需识别 `hetero_team`、`solo_<model>`，避免把异构误判为 solo/固定、或在"固定团队 worker 覆盖"检查里误报。
- 保持"旧批在被扩展审计器下仍可复审、零新观察"的承诺；gate 重跑通过后才有新一轮。

### 3.4 LIMITS / CM
- 保留 CM 当前参数（budget 12000 / max_tokens 2048 / keep_recent 4）。
- `cm_model='glm-5.3-flash'` 固定，与 lead/worker 解耦，避免引入额外变量；本轮不因异构改动 CM。
- 记录每个 arm 的 CM 触发次数、单次削减%、CM 成本占比——用于评估 rev6/7 调参是否解决"摘要冗长/触发太稀"。

## 4. 度量与评判

| 问题 | 对比组 |
|---|---|
| 开团队值不值？ | `solo_M` vs `fixed_M` / `hetero_*`（同题同预算） |
| 单模型当 worker 是否超越单干？ | `fixed_M`(worker=M) vs `solo_M` |
| 异构是否系统性优于同构？ | 20 个 `hetero_(A,B)` 聚合 vs 10 个同构（5 fixed + 5 solo），比 resolved 率、采纳率、总 token |
| 具体某对组合是否更好？ | `hetero_(A,B)` vs `fixed_A` vs `fixed_B` |
| 回归 worker 的价值 | "独立回归测试"这一角色（不同模型）是否带来更高采纳率 / 更多被采用 delta |
| CM 收益 | 本轮 30 run 的 CM 触发/削减/成本占比 vs rev5 同指标 |

度量：官方 `resolved`、F2P/P2P、worker 采纳率、整队总 token、调用次数、每节点 token、worker 各自 delta 是否被采用、CM 触发与成本。

> **诚实声明**：1 run/arm 仍是方向性对照，不能稳定排名或 SOTA。本轮目标是**覆盖完整性**（每种组合都有可比较的点），不是统计显著性。真正统计需同 arm 多 run 采样（本轮按用户决策不做）。

## 5. 风险与边界

- **30 个 run 的墙钟**：每 run ≤1800s，最多 2 并行，且 `PLAN.md` 有"pair 调度 3h"上限——可能需分多批，属于预算无限、但墙钟受既有批次节流约束。
- **异构行为不平衡**：不同模型"实现 vs 回归"表现天然有别，账务上按节点归因，避免把模型差异误读为团队收益。
- **gate/审计改动**：新增 arm 类型必须先更新审计断言并重跑 gate，否则新批过不了验证；不要破坏旧批复审。
- **CM 与 worker 模型耦合**：CM 只对 worker 历史压缩，不同 worker 模型触发频率不同（glm 易超 12k、gpt 可能不触发）——这本身是观察点，不是缺陷。

## 6. 后续可选（本轮不做）

- **更难任务**：用户指定本轮后找一个比 Sphinx-8035 更能区分的任务，做同样 30-run 矩阵。
- **lead 变体**：把 lead 从 gpt-5.6-sol 换成其它模型（如 glm-5.3），测 lead 敏感性（涉及 `lead_model` 参数化，本轮已具备）。
- **统计强度**：若需稳定结论，对少数关键对照（solo vs fixed vs 某异构）各跑 N≥3 次。
