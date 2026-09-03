# 后续实验计划：补齐 solo 基线覆盖 + 精选异构团队（精简版）

时间基线：2026-09-03。当前 harness 已推进至 revision 6/7（`gate_revision6` 为当前 gate，CM 已调参：`cm_context_budget:12000`、`cm_max_tokens:2048`、`cm_keep_recent:4`）。
前置文件：`PILOT_V5_GLM_CM_ANALYSIS.md`、`DETAILED_TABLES_20260903.md`、`PLAN.md`。
本计划为描述/提案，不动已完成批次的 manifest / result / 冻结快照。

## 0. 用户已拍板的决策（2026-09-03）

| 决策点 | 结论 |
|---|---|
| solo 基线 | **全 5 模型**各跑一个 solo |
| 异构 worker 范围 | **精选组合（时间有限，不做全矩阵）**，含 glm×gpt 双向混搭，定为 6 个 |
| 统计强度 | **1 run / arm，不重复**（方向性结论） |
| 任务 | **只 Sphinx-8035**（难题，区分度高）；更难任务留作下一波 |
| 预算 | 每 run ≤28 调用 / 600k token；**run 数控制在精简范围（约 11）** |
| lead 模型 | **保持 gpt-5.6-sol**（一开始选定）；lead 变体留作可选后续 |

## 1. 设计缺口回顾

- **缺口 A（solo 覆盖）**：只有 `solo`(gpt-5.6-sol)，无 `solo_<model>`，答不出"某模型单干 vs 当 worker 哪个好"。
- **缺口 B（异构）**：只覆盖 lead≠worker；队内两个 worker 恒同模型、不同角色（实现/回归）。**worker-1≠worker-2 不支持**（`runner.py:127` 强制 `one exact worker_model`）；**lead 换模型不支持**（`runner.py:143` `lead_model='gpt-5.6-sol'` 写死）。

## 2. 精选异构的依据（为什么挑这 3 个）

rev3 同构 worker（Sphinx-8035）已给出各模型作为 worker 的行为：

| worker 模型 | 被采纳 | 消耗 token | 备注 |
|---|---|---:|---|
| gpt-5.6-sol | 采纳 | 186k+175k | run 通过 |
| gpt-5.6-terra | 采纳 | 145k+188k | run 未过 |
| gpt-5.6-luna | 采纳 | 228k+160k | run 未过（预算耗尽） |
| glm-5.3 | **阻塞未采纳** | 90k+75k（最低） | Lead 独自写完并通过 |
| glm-5.3-flash | **阻塞未采纳** | 135k | run 通过 |

**规律**：gpt 系 worker 能完成、被采纳；glm 系 worker 全"阻塞"（写不完 / 不 finish）。所以异构最有价值的假设是：**换对角色分工，能否把 glm 的短板救回来 / 让不同 gpt 子模型各展所长。**

## 3. 本轮 arm 清单（Sphinx-8035，同一套当前适配器，CM 开启）

> **口径**：rev3 数据在修订 3（CM 未接入）上跑；本轮全部在当前适配器（CM-on）重跑，保证本轮内部同条件可比。rev3 保留为"无 CM 历史基线"，并列、不混用。

### 3.1 solo（单 agent = 该模型）——5 个
```
solo_gpt-5.6-sol   solo_gpt-5.6-luna   solo_gpt-5.6-terra
solo_glm-5.3        solo_glm-5.3-flash
```

### 3.2 同构固定团队（lead=gpt-5.6-sol + 2×M）——组合涉及的全部 5 个锚点
```
fixed_gpt-5.6-sol   fixed_gpt-5.6-terra   fixed_gpt-5.6-luna
fixed_glm-5.3        fixed_glm-5.3-flash
```

### 3.3 精选异构团队（lead=gpt-5.6-sol + worker-1=实现 + worker-2=回归，A≠B）——6 个
| # | 组合 | 实现(worker-1) | 回归(worker-2) | 测什么 | 对照 |
|---|---|---|---|---|---|
| 1 | `hetero_gpt-5.6-terra__gpt-5.6-luna` | terra（高效） | luna（彻底） | 同可靠家族内"各展所长"是否优于同构 | fixed_terra / fixed_luna |
| 2 | `hetero_gpt-5.6-terra__glm-5.3` | terra | glm-5.3 | **角色适配**：glm 写实现会阻塞，写测试会不会更强 | fixed_terra / fixed_glm-5.3 |
| 3 | `hetero_glm-5.3__gpt-5.6-luna` | glm-5.3 | luna | **救援**：可靠 luna 回归能否兜住 glm 实现短板 + 成本 | fixed_glm-5.3 / fixed_luna |
| 4 | `hetero_gpt-5.6-terra__glm-5.3-flash` | terra | **glm-5.3-flash** | 与 #2 配对，剥出 **glm-5.3 vs glm-5.3-flash** 差异 | fixed_terra / fixed_flash |
| 5 | `hetero_gpt-5.6-sol__glm-5.3` | **gpt-5.6-sol** | glm-5.3 | 最强实现 + 便宜回归：能否仍像 fixed_sol 那样过、且更省 | fixed_sol / fixed_glm-5.3 |
| 6 | `hetero_glm-5.3__gpt-5.6-sol` | glm-5.3 | **gpt-5.6-sol** | 最强回归能否兜住 glm 实现短板 | fixed_glm-5.3 / fixed_sol |

> 设计：`glm-5.3` 在 #2/#3/#5/#6 里分别任"回归/实现"，形成受控角色对比；`#2 vs #4` 剥出 GLM 两变体；`#5/#6` 用"最强 gpt（同时也是 Lead 模型）gpt-5.6-sol"分别充当实现与回归，与 terra(高效)、luna(彻底) 互补。精选核心是用最少 run 覆盖"角色 × GLM/GPT 家族 × GLM 变体"三轴。

**总计：16 个 run**（5 solo + 5 fixed 锚点 + 6 异构）。每 run 1 次、≤28 调用 / 600k token / 1800s。

## 4. 代码改动点（开新 revision，不覆盖旧批）

### 4.1 `cli.py · prepare()`
- arm 生成改为支持三类；异构用**精选清单**而非全矩阵：
  ```python
  HETERO = [("gpt-5.6-terra","gpt-5.6-luna"),   # 同族互补
            ("gpt-5.6-terra","glm-5.3"),        # glm-5.3 当回归（角色适配）
            ("glm-5.3","gpt-5.6-luna"),         # glm-5.3 当实现（救援）
            ("gpt-5.6-terra","glm-5.3-flash"),  # flash 当回归（对照 glm-5.3）
            ("gpt-5.6-sol","glm-5.3"),          # 最强实现 + 便宜回归
            ("glm-5.3","gpt-5.6-sol")]          # 最强回归救 glm
  ANCHOR_MODELS = ['gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','glm-5.3','glm-5.3-flash']
  arms = ['solo_'+m for m in MODELS] \
       + ['fixed_'+m for m in ANCHOR_MODELS] \
       + ['hetero_'+a+'__'+b for a,b in HETERO]
  ```
- schedule 条目：`condition ∈ {solo, fixed_team, hetero_team}`；`lead_model`（本轮恒 gpt-5.6-sol）；`worker_models` 为列表（solo 空；fixed 为 [m,m]；hetero 为 [a,b]）。
- manifest：`worker_model` 标量 → `worker_models` 列表；新增 `lead_model`；`conditions`/`arms` 扩展。

### 4.2 `runner.py`
- 撤掉 `fixed_team requires one exact worker_model` 断言（`runner.py:127`），按 `worker_models[i]` 给第 i 个 worker 分配模型；`request = {'model': self.entry['worker_models'][worker_index], ...}`（替换 `runner.py:248`）。
- `lead_model` 从 `self.entry` 读（替换 `runner.py:143` 写死）；solo_<model> 的单 agent 模型 = `<model>`。
- 两 worker 仍按 `worker_roles`（生产实现 / 独立回归）分工；模型与角色正交。

### 4.3 `validation/audit_results.py` + gate
- 现有条件判断在 `audit_results.py:495,505,517,603,634`（把 `fixed_team` 当唯一团队条件）。需识别 `hetero_team`、`solo_<model>`，避免异构被误判为 solo/固定、或在"固定团队 worker 覆盖"检查误报。
- 保持"旧批在被扩展审计器下仍可复审、零新观察"承诺；gate 重跑通过后再推进。

### 4.4 LIMITS / CM
- 保留 CM 参数（budget 12000 / max_tokens 2048 / keep_recent 4）；`cm_model='glm-5.3-flash'` 固定、与 lead/worker 解耦。
- 记录每 arm 的 CM 触发次数、单次削减%、CM 成本占比 → 评估 rev6/7 是否解决"摘要冗长/触发太稀"。

## 5. 度量与评判

| 问题 | 对比组 |
|---|---|
| 开团队值不值？ | `solo_M` vs `fixed_M` / `hetero_*` |
| 单模型当 worker 是否超越单干？ | `fixed_M` vs `solo_M` |
| 高效实现 + 彻底验证是否优于同构？ | `hetero_terra__luna` vs `fixed_terra` / `fixed_luna` |
| glm 换角色（写测试）是否比写实现强？ | `hetero_terra__glm-5.3` vs `fixed_glm-5.3`(实现阻塞) / `fixed_terra` |
| 可靠验证者能否兜住 glm 实现短板？ | `hetero_glm-5.3__luna` vs `fixed_glm-5.3` / `fixed_luna` |
| glm-5.3 vs glm-5.3-flash 谁擅长回归测试？ | `hetero_terra__glm-5.3` vs `hetero_terra__glm-5.3-flash` |
| 最强实现+便宜回归是否保住通过且更省？ | `hetero_sol__glm-5.3` vs `fixed_sol` / `fixed_glm-5.3` |
| 最强回归能否兜住 glm 实现？ | `hetero_glm-5.3__sol` vs `fixed_glm-5.3` / `fixed_sol` |
| 回归 worker 的价值 | "独立回归"角色（换不同模型）是否带来更高采纳率 / 更多 delta 被采用 |
| CM 收益 | 本轮 16 run 的 CM 触发/削减/成本占比 vs rev5 同指标 |

度量：官方 `resolved`、F2P/P2P、worker 采纳率、整队总 token、调用次数、每节点 token、worker 各自 delta 是否被采用、CM 触发与成本。

> **诚实声明**：1 run/arm 且只有 6 个异构组合，是**针对性、方向性**对照，能回答上述 6 个具体假设，**不能**推广到"异构整体更好"或稳定排名。真正统计需同 arm 多 run 采样（本轮按用户决策不做）。

## 6. 风险与边界

- **16 个 run 墙钟**：每 run ≤1800s、最多 2 并行、`PLAN.md` 有"pair 调度 3h"上限，可能分多批。
- **异构行为不平衡**：不同模型"实现 vs 回归"表现天然有别，按节点归因，避免把模型差异误读为团队收益。
- **gate/审计改动**：新增 arm 类型必须先更新审计断言并重跑 gate，否则新批过不了验证；不破坏旧批复审。
- **CM 与 worker 模型耦合**：CM 只对 worker 历史压缩，不同 worker 模型触发频率不同（glm 易超 12k、gpt 可能不触发）——这是观察点，非缺陷。

## 7. 后续可选（本轮不做）

- **更难任务**：本轮后找比 Sphinx-8035 更能区分的任务，跑同套 solo+fixed+精选异构。
- **补齐剩余 2 个 fixed 锚点**（`fixed_gpt-5.6-sol`、`fixed_glm-5.3-flash`）与**全矩阵异构**，视时间与需求再议。
- **lead 变体**：把 lead 换成其它模型测敏感性（涉及 `lead_model` 参数化，本次已具备）。
- **统计强度**：若需稳定结论，对少数关键对照各跑 N≥3 次。
