# 下一轮实验计划：CM 池扫描收官 + 任务探针面板 + 条件性组队（rev10 / pilot_v13–v16）

时间基线：2026-09-04（第二版）。前置文件：`NEXT_EXPERIMENT_PLAN_20260904.md`（v10/v11 计划，已执行完）、`PILOT_V10_V11_ANALYSIS.md`（含 2026-09-04 复核更正）、`PILOT_V12_ASTROPY_ANALYSIS.md`、`gate_revision9.json`。
本计划为描述/提案，不动已完成批次的 manifest / result / 冻结快照。

## 0. 依据（三波合并结论，详见两份分析报告）

| 命题 | 证据状态 | 对本计划的含义 |
|---|---|---|
| 装配 CM 无独立贡献 | 两任务一致否定（sphinx OFF 省 3.1%；astropy ON 省 2.5%，方向相反均在噪声内） | **rev10 默认 off、按臂开启**（待用户确认） |
| 团队协议价值是条件性的 | 难任务 solo 0/3→团队 3/3；易任务 solo 3/3 最省、团队 +17% 零增益 | "Lead 判断难度再组队"成为正命题（pilot_v15） |
| CM 池 12 的供给不足有真实代价 | v10 solo 全饱和+9 降级全灭；v11 5/7 饱和、46 降级、3 次 TOKEN 死亡；唯一未饱和的 B1 恰好帕累托最优 | CM 池 {6,12,24} 扫描为第一优先（pilot_v13） |
| worker 收尾难度相关，非协议缺陷 | astropy 12/12 completed、6/6 team_valid=True | 不再为收尾改协议 |
| 任务事前选择代理（陈述长度/repo 规模）作废 | astropy 选法失手：长陈述自带先验降低探索需求 | 改用探针 run 信号选任务（pilot_v14） |
| v10 A1 solo 0/3 归因含 CM 供给混杂 | v12 确认为 sphinx 特有 | v13 的 solo+池24 档顺带纯化 |

## 1. rev10 代码改动（先离线测试 + gate_revision10，再跑真实批）

1. **装配默认 off**：`LIMITS` 中 `cm_team_memory` / `cm_scout_distill` / `cm_bootstrap_package` 默认改 False；装配 ON 仅经 `limits_override` 按臂开启（rev9 机制已有）。旧批复审语义不变（审计按各批 manifest effective_limits 断言）。
2. **CM 池成为正式臂级变量**：`cm_call_allowance` 经 `limits_override` 按臂取值（rev9 已支持），审计断言覆盖 {6,12,24}。
3. **探针臂支持**：`cli.py` 支持多 instance × 单臂（`--wave probepanel`），选择规则写入 manifest（预注册、禁看答案）：从官方冻结池取"从未启动"的实例，按 repo 分层抽样 8 个。
4. gate_revision10：全量离线测试 + 旧批（v2–v12）复审 PASS 后放行。

## 2. pilot_v13：CM 池扫描（载体必须 sphinx-8035；预算 28 调用/600k/1800s，装配默认 off）

| 格 | 臂 | CM 池 | N | 测什么 |
|---|---|---|---|---|
| C1 | `solo_gpt-5.6-sol` | 6 | 2 | 池下限：是否加速 TOKEN 死亡 |
| C2 | `solo_gpt-5.6-sol` | 24 | 2 | **池上限：solo 是否复活（纯化 v10 A1 归因）** |
| C3 | `hetero_gpt-5.6-terra__glm-5.3`（装配 off） | 6 | 1 | 团队臂池需求下限 |
| C4 | `hetero_gpt-5.6-terra__glm-5.3`（装配 off） | 24 | 1 | 团队臂池上限 |
| — | 对照基线：v10 的 solo 池12（0/3）与 hetero 池12（3/3）已有 | 12 | — | 不重跑 |

共 6 run。预设判定：
- C2 若 ≥1/2 通过 → v10 的 solo 0/3 归因改判为"CM 供给不足为主、缺团队为辅"；若仍 0/2 → "团队协议净效应"在难任务上坐实。
- 读数：TOKEN 死亡线、cm_call_not_admitted 计数、CM token 占比、solo Lead input 随池尺寸的变化。
- 顺带产出 rev10 默认池尺寸建议（12 是否提高为全局默认）。

## 3. pilot_v14：任务探针面板（选任务的元方法 + 外部效度入口）

- 8 个候选实例（§1.3 预注册规则选出），各跑 1 个低成本探针 run：`solo_gpt-5.6-sol`，28 调用/600k/CM 池 12（与 v10/v12 的 solo 臂同配置，保证信号可比）。
- 信号（每任务）：Lead input 分布、CM 调用与 not_admitted、是否 TOKEN 死亡、resolved、探针 wall。
- 判定：按信号把 8 题分"难（TOKEN 死或未过）/ 中 / 易（从容通过且 Lead input <300k）"三档；与 sphinx-8035、astropy-14995 两个已标定点连成 10 题难度谱。
- 产出：为 v15/v16 选定 3–5 题（覆盖三档），写入选题备忘录；探针数据本身即 8 任务 × solo 的外部效度小样本。
- 预算：8 run × ~350k ≈ 2.8M token。

## 4. pilot_v15（rev11，机制层，下下波，本计划只登记不排期）

- **条件性组队受控变体**：协议从"强制 derive 两 worker"改为"Lead 在 N 轮侦察后可选择激活/不激活"——测"Lead 判断难度再组队"是否复现人工三连环的对照结果（难任务组队、易任务单干）。
- 设计要点：组队决策事件进账本（可审计）；与强制组队臂在 v14 选出的难/易双任务上对照；N=3。
- rev11 改动面：`bootstrap_team()` 的强制准入改为协议提供 + Lead 决策；审计新增"决策事件"断言。属机制层实验，先 gate 再跑。

## 5. pilot_v16（登记）：公开 baseline 对照

- 在 v14 选定的任务面板上接入 1 个开源 baseline（mini-swe-agent 优先，MIT、最轻；OpenHands 备选），同预算同任务对照我们的最佳配置。
- 回答"机制有效是否只是内部对照的假象"；产出可公开辩护的外部效度证据。

## 6. 统计与措辞规范（沿用并加一条）

1–5 条沿用 `NEXT_EXPERIMENT_PLAN_20260904` §4（连续量主指标、Wilson、方向性措辞、同冻结重复、跨批仅演进并列）。
6. **新增**：凡跨任务外推的结论，必须至少两个难度档位的任务各 N≥3 支撑；单任务"强方向信号"不得外推（v10→v12 的教训）。

## 7. 预算与排期估算

- pilot_v13：6 run × ~550k ≈ 3.3M token；约 1.5–2 小时（2 并行）。
- pilot_v14：8 run × ~350k ≈ 2.8M token；约 2 小时。
- 合计 14 run、~6M token；v15/v16 待 v13/v14 结果后单独排期与做预算。

## 8. 风险与边界

- C2 若 solo 复活，v10 的"A1 vs A3 = 团队协议净效应"需按 §2 判定规则改写——这是设计好的可证伪点，不是风险。
- 探针面板的选题规则必须预注册（写进 manifest），禁止看过探针结果后换题；换题须另起新批次并保留原批。
- v15 的 Lead 自主组队触及"model_requested_delegation=0"的历史空白，若 Lead 从不主动组队，这本身是重要数据（当前模型可能不具备该判断能力），报告不得隐瞒。
- rev10 默认 off 仅改默认值，所有 v13+ 批次 manifest 记录 effective_limits，旧批（v2–v12）复审语义不得改变。

## 9. 明确不做

- 不动已完成批次冻结产物；不为 worker 收尾改协议（已证难度相关）。
- 不在 astropy 上测 CM 供给（检验不了压缩）；不在本计划内刷榜（Pro 提交属传播策略，另议）。
- 不做 split/fission/DSH 桥；不做全矩阵异构；不换 Lead 模型（留待 v15 后）。
