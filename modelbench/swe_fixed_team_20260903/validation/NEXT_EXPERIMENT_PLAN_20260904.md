# 下一轮实验计划：装配消融三连环 + 预算修复验证（rev9 / pilot_v10 + pilot_v11）

时间基线：2026-09-04。前置文件：`NEXT_EXPERIMENT_PLAN.md`（v8 计划，已执行完）、`PILOT_V8_V9_ANALYSIS.md`、`gate_revision8.json`。
本计划为描述/提案，不动已完成批次的 manifest / result / 冻结快照。旧批命名不动；新批为 pilot_v10（消融波）与 pilot_v11（预算波）。

## 0. 本计划要回答的问题（来自 v8/v9 复盘的三个缺口）

| # | 缺口 | 证据 |
|---|---|---|
| G1 | **归因缺口**：异构 7/7 无法分离"装配 CM / 团队协议 / Lead 能力"三因子 | 7 个异构通过中 5 个零 worker 采纳，Lead 主导；无"无装配异构"对照 |
| G2 | **预算结构性瓶颈**：worker 被共享 28 调用池饿死 | 26 个 worker 实例：13 死于 budget_exhausted（池尽）、仅 2 死于个人 8 次上限、2 空交付、1 超时；token 与墙钟从未咬人（v5 起零 TOKEN_BUDGET_EXHAUSTED；墙钟最高 1019s/1800s） |
| G3 | **单 run 噪声**：同臂跨批翻转 | solo_sol v3❌v4❌v6❌v8✅；fixed_sol v2✅v3✅v4❌v8❌（跨协议变更，混杂不可分） |

## 1. rev9 代码改动（全部小而可控；先离线测试 + gate，再跑真实批）

### 1.1 按臂 limits 覆盖（机械重构）
- `cli.py`：schedule entry 支持可选 `limits_override` 字典；manifest 记录每 entry 合并后的有效 limits。
- `runner.py`：`SweRun.__init__` 合并 `self.limits = {**LIMITS, **(entry.get('limits_override') or {})}`；类内全部 `LIMITS[...]` 引用改 `self.limits[...]`——共 29 处，以 `grep -n 'LIMITS\[' runner.py` 为准（`:45-46` 模块级信号量 `MODEL_SLOTS`/`CONTAINER_SLOTS` 保持全局，不动）。
- **审核发现**：`LEAD_RESERVE` 的"Lead 预留最后 2 次调用"是硬编码字面量（`runner.py:309` `remaining_calls <= 2`），不在 LIMITS 里；rev9 一并参数化为 `lead_reserve_calls`（默认 2）。

### 1.2 CM 调用独立池（`team_runtime/ledger.py`）
- `RunBudget` 新增 `cm_call_allowance: int | None = None`（默认 None = 旧行为，保证旧批复审语义不变）。
- `reserve()` 中 `role == 'cm'` 的 ticket 计入独立池，不占 `max_calls`；summary 增加 `cm_call_count` 字段分列。
- `runner.py` 传 `self.limits['cm_call_allowance']`（rev9 默认 12）。
- **审计口径预警**：新批 `calls` 需分报"工作调用 / CM 调用"；`audit_results.py` 按各批 manifest 的 limits 断言（与 rev8 "per-batch audits read historical cm models from each manifest" 同思路），旧批复审语义不得改变。

### 1.3 装配消融开关（零新机制代码）
- 复用现有三个 flag：`cm_team_memory`、`cm_scout_distill`、`cm_bootstrap_package`（`runner.py:44`），经 `limits_override` 按臂关闭。
- **关键设计**：消融臂只撤装配三件套，`cm_enabled` 保持 True（on-demand 压缩保留）——隔离的是"装配"的贡献，不是"压缩"的贡献。
- **审核确认**：无装配降级路径已存在——`start_workers()` 在 `cm_bootstrap_package=False` 时跳过装配包、worker 直接冷启动（退回 v6 模式，`runner.py:295-297`）；rev9 只需补该路径的测试覆盖，无需新机制代码。

### 1.4 gate_revision9
- 全部既有离线测试重跑 + 新增：limits_override 合并、CM 独立池准入/结算、消融臂 manifest 断言、旧批复审兼容。
- 负对照复用 revision 2（environment/grader 契约不动）。

## 2. pilot_v10：装配消融三连环（协议冻结 rev9；预算与 v8 相同：28 调用/600k/1800s）

| 格 | 臂 | N | 测什么 |
|---|---|---|---|
| A1 | `solo_gpt-5.6-sol` | 3 | Lead 能力基线 + 噪声底板（v8 单次 ✅502k） |
| A2 | `hetero_gpt-5.6-terra__glm-5.3` 装配 ON | 3 | 团队+装配（v8 单次 ✅408k） |
| A3 | `hetero_gpt-5.6-terra__glm-5.3` 装配 OFF | 3 | 团队无装配（撤 scout/bootstrap/team memory） |

读数与预设判定规则（先定规则再看数据）：
- **A2 vs A3** = 装配 CM 的净效应（成本、通过率、worker 探索量）。
- **A1 vs A3** = 团队协议本身的净效应。
- 若 A3 ≈ A2 → 装配无独立贡献，v8 的"异构优势"归因应改写为"Lead+协议"。
- 若 A3 成本显著高于 A2（>20%）或通过率低 → 装配的 context 工程价值成立。
- 若 A2 ≈ A1 → 团队整体无增益，机制价值只剩成本工程。

## 3. pilot_v11：预算修复验证（rev9 代码生效后）

预算改动（三约束同松一档，**目的：排除一切预算借口**——若这样 worker 仍不 completed，结论就是能力问题而非预算问题）：
- `max_calls` 28→40；`token_limit` 600k→900k（v8 实际均值 ~15k/调用，40 调用 ×20k ≈ 800k，900k 留余量；token 是否重新咬人本身即观察点）；`worker_calls` 8→16；`cm_call_allowance`=12（独立池）；墙钟 1800 不动（从未咬人）。

| 格 | 臂 | N | 测什么 |
|---|---|---|---|
| B1 | `fixed_glm-5.3` | 2 | 最强同构格（v8 ✅278k）：预算松后成本/通过率走向 |
| B2 | `hetero_gpt-5.6-terra__glm-5.3` | 2 | 最省异构格 |
| B3 | `fixed_gpt-5.6-sol` | 2 | 强模型同构失败格（v8 ❌489k）：预算修复能否救活 |
| B4 | `fixed_deepseek-v4-flash` | 1 | 弱模型交付上限（v8 零提交） |

首要读数：**team_execution_valid 是否破零**、worker completed 率、采纳率变化；次要读数：通过率、成本、CM 独立池后工作调用利用率。

## 4. 统计与判读规范（本计划起生效，写入每批分析报告）

1. 主指标用连续量：run 总 token、worker 探索 input、Lead input、CM 开销占比；二值 `resolved` 为辅。
2. 通过率一律带 Wilson 95% 区间。N=3 时 3/3 vs 0/3 仅算"强方向信号"（区间仍宽），不得称"显著"。
3. 1-run 格子只许"方向性"措辞（沿用既有声明传统）。
4. 同臂重复在同一 revision、同一镜像、同一冻结 prompt 下错时执行；报告列均值±极差。
5. 与 v8/v9 的跨批并列只在"演进证据链"意义上成立（预算制不同）。

## 5. 预算与排期估算

- pilot_v10：9 run × ≤28 调用/600k，按 v8 实际均值 ~410k/run 估 ≈ 3.7M token；2 并行 × ≤1800s → 约 2.5–4 小时。
- pilot_v11：7 run × ≤40 调用/900k ≈ 上限 6.3M token；约 2–3 小时。
- 合计 16 run，token 上限 ~10M；全程 deepseek-v4-flash 任 CM。

## 6. 风险与边界

- A3（无装配）可能 context 膨胀更快 → token 准入可能重新咬人，这本身是数据，不算事故。
- B 波与 v8 可比性受预算制变化限制，报告必须分开并列、不混用。
- deepseek 提供方稳定性延续性是假设（v9 84 调用零事故，样本仍小）。
- 审计改动先于真实批：rev9 离线测试 + gate 全绿 + 旧批（v2–v9）复审 PASS 后才准启动 pilot_v10。
- 本计划仍单任务（Sphinx-8035）；第二任务的外部效度验证留作再下一波。

## 7. 明确不做

- 不动已完成批次的任何冻结产物。
- 不引入新模型、不换 Lead 模型（Lead 变体留作更后一波）。
- 不做 split/fission/DSH 桥（scope_limits 维持 HOLD）。
- 不做全矩阵异构。
