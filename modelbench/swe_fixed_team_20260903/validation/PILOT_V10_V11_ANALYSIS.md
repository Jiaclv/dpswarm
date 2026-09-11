# pilot_v10 / pilot_v11 分析报告：装配消融三连环 + 预算修复验证（rev9）

> **2026-09-04 复核更正（外部复核 + 数字重验）：** 初版 §2.7"v11 七 run CM 池全部 12/12 用满"失实——实为 5/7 饱和，B1 fixed_glm 两 run 为 9/12 与 11/12（零拒绝）。该更正使"唯一未饱和臂恰为唯一 2/2 通过且成本最从容的臂"这层强相关得以呈现。同时补入三处复核发现：§1.1 成本工程功劳归属 on-demand 压缩而非装配；§1.2 A1 solo 归因的 CM 池制度性混杂；§1.5 rev9 独立池消除 v8 worker 池尽饿死（13/26→0/12）。§5 下一波按此重排。原始 run 数据未动。

时间基线：2026-09-04。协议：rev9（gate_revision9.json PASS，52 runtime sources；离线测试 178/178；旧批 v2–v9 复审 8/8 PASS）。
两批审计均 PASS 零告警：v10 312 调用 / 4,950,784 known token；v11 343 调用 / 5,122,373 known token（合计 10.07M，与计划 §5 的 ~10M 上限估算一致）。
单任务 sphinx-doc/sphinx-8035；16 run 全部完成，零基础设施错误，零 stop。执行墙钟：v10 42.7 min，v11 50.1 min（2-run 并行）。

## 1. pilot_v10 装配消融（A 波，9 run；预算与 v8 名义相同：28 调用/600k/1800s，rev9 起 CM 走 12 次独立池）

| 臂 | N | resolved | Wilson 95% | run token（均值） | worker input 均值 | Lead input 均值 | CM 调用 | worker completed | team_valid |
|---|---|---|---|---|---|---|---|---|---|
| A1 solo_gpt-5.6-sol | 3 | **0/3** | [0.00, 0.56] | 565,729 | — | 432,054 | 12/12/12（池尽） | — | — |
| A2 hetero 装配 ON | 3 | **3/3** | [0.44, 1.00] | 550,813 | 201,299 | 234,265 | 6/10/12 | 4/6 | F,F,T |
| A3 heterooff 装配 OFF | 3 | **3/3** | [0.44, 1.00] | 533,719 | 202,782 | 238,082 | 7/4/9 | 4/6 | F,F,T |

run token 逐 run：A1 [544,819 / 574,411 / 577,958]；A2 [508,455 / 566,623 / 577,360]；A3 [506,689 / 539,046 / 555,423]。

### 预设判定（NEXT_EXPERIMENT_PLAN_20260904 §2，先定规则后看数据）

1. **A2 vs A3 = 装配 CM 净效应：无独立贡献。** 通过率同为 3/3；成本 A3 均值还略低 3.1%（550.8k → 533.7k，远未触发">20% 才算装配有价值"的阈值方向）；worker 探索量（input 均值）几乎相同（201.3k vs 202.8k）；worker completed 与 team_valid 模式逐 run 完全一致（F,F,T）。装配三件套（scout 蒸馏/bootstrap 包/团队记忆）在预算内既不提升通过率也不节省成本，反而多耗 CM 调用（均值 9.3 vs 6.7）。
   - **成本工程功劳的进一步归属（复核补充）：** A3 无装配但有 on-demand 压缩（v5/v6 引入），成本已与 A2 持平且低于 v8 之前的批——v8 报告中"worker 探索成本降"的主要功劳应记在**压缩机制本身**，而非 v7 的装配三件套。v8 fixed_glm ✅278k 的跨批下降同理属压缩账（演进证据链口径）。
2. **A1 vs A3 = 团队协议净效应：强方向信号，但含一个制度性混杂（复核补充）。** 0/3 vs 3/3，Wilson 区间仍重叠（[0,0.56] vs [0.44,1.00]），按 §4.2 措辞为"强方向信号"，不得称显著。机制可见：solo 的 Lead input 达 432k（单 agent 长上下文），三个 run 全部死于 token 准入（TOKEN_BUDGET_EXHAUSTED，20–22 调用即耗尽 600k）；团队臂把探索拆给两个 worker（合计 ~203k input），Lead 自身 input 降至 ~238k 且留有复审余量。
   - **混杂：rev9 固定 CM 池 12 对 solo 制度性不友好。** solo 对 on-demand 压缩需求最大（三 run 全部 12/12 打满 + 9 次 cm_call_not_admitted 静默降级），池尽后长上下文无人压缩，20 次工作调用即 token 死；对照 v8（CM 调用挤占工作池时代）同模型同任务的 solo_sol 曾 ✅502k 完成。A1 的失败里混着"CM 供给不足"，不全是"缺团队"。纯化办法：solo_sol + cm_call_allowance=24 补档（若 solo 复活，团队协议净效应需再打折）。
3. **A2 ≈ A1？否。** 团队整体有增益（3/3 vs 0/3），机制价值不只剩成本工程——但在本任务上增益来自协议分工而非装配。
4. **v8 归因改写成立：**"异构优势"应归因于 **Lead + 团队协议**，装配 CM 在 Sphinx-8035 上无独立贡献。v8 的 worker 探索成本降 ~50% 在本波不复现（A2 与 A3 worker input 相同）——该结论的适用范围是"预算充足、单任务简单上下文"场景；装配的 context 工程价值若存在，应在更难的上下文瓶颈任务上复测（→ pilot_v12，astropy-14995）。
5. **rev9 CM 独立池治好了 v8 的 worker 饿死（v8→v10 正面演进证据，复核补充）：** v8 复盘 26 个 worker 实例中 13 个死于共享 28 调用池尽；v10 的 12 个 worker 实例死于池尽者为 **0**（8 completed + 4 blocked）。CM 挪出工作池后，worker 在 28 调用制下不再被 CM 挤占额度。

## 2. pilot_v11 预算修复验证（B 波，7 run；40 调用/900k/worker 16/CM 池 12/墙钟 1800 不动）

| 臂 | N | resolved | Wilson 95% | run token | worker input | worker outcome | team_valid |
|---|---|---|---|---|---|---|---|
| B1 fixed_glm-5.3 | 2 | **2/2** | [0.34, 1.00] | 541,845 / 507,186 | 180.8k / 192.6k | 1/2、1/2 completed | F,F |
| B2 hetero terra__glm | 2 | **1/2** | [0.09, 0.91] | 837,772❌ / 863,114✅ | 521.7k / 409.9k | 0/2、1/2 | F,F |
| B3 fixed_gpt-5.6-sol | 2 | **1/2** | [0.09, 0.91] | 854,227❌ / 855,330✅ | 553.2k / 563.9k | 1/2、2/2 | F,**T** |
| B4 fixed_deepseek-v4-flash | 1 | 0/1（方向性） | [0.00, 0.79] | 662,899 | 299.2k | 0/2 | F |

worker outcome 汇总（14 实例）：completed 6、budget_exhausted 7、local_call_limit 1。对照 v10（12 实例）：completed 8、blocked 4、budget_exhausted 0。

### 首要读数

1. **team_execution_valid 破零：成立。** 三波历史（v2–v9）为零，本日两批共 3 个 True：v10 hetero.rep3、v10 heterooff.rep3、v11 fixed_sol.rep2。全部出现在同臂较晚的 rep 上——协议与预算都不是充分条件，worker completed 收尾仍高度随机。
2. **预算借口被排除，结论指向能力/协议问题。** 三约束同松一档（40/900k/16）后：worker 死于个人 16 次上限的仅 1 个，死于共享 40 池尽的有 7 个；v11 有 3 个 run 的 Lead 死于 TOKEN_BUDGET_EXHAUSTED（850k+ 的 run 真实逼近 900k，"token 是否重新咬人"的观察点=是）。放开预算没有提高 worker completed 数（8→6），只提高了烧钱上限。
3. **B3"强模型同构失败格救活"未兑现但翻转：** v8 ❌489k → 本波 854k❌ / 855k✅（1/2）。成本 +75% 买来一次通过，跨 run 翻转依旧（fixed_sol 在 v2✅ v3✅ v4❌ v8❌ 后继续不稳定）。
4. **B2 异构最省格在预算松后反而变贵变脆：** v8 ✅408k → 838k❌ / 863k✅（1/2）。与 B3 同为 1/2 翻转，均值 ~850k 已触及 token 咬人线。
5. **B1 最稳格子依旧最稳：** 2/2 通过、token 均值 525k（40 调用用满但 token 从容），是全部配置中"成本-通过"帕累托最优。
6. **B4 弱模型上限（方向性，N=1）：** 40 调用用满、663k、worker 0/2 completed、零通过。deepseek-v4-flash 作为 worker 的交付能力不足被再次观察（v8 solo/fix 同样零提交）。
7. **CM 独立池：v11 七 run 中五个饱和（12/12），两个未饱和——未饱和的恰是帕累托最优臂（更正：初版误写"全部 12/12 用满"）。** 逐 run 池占用：B1 fixed_glm **9/12（0 拒绝）与 11/12（0 拒绝）**；B2 hetero 12/12（拒绝 17 与 4）；B3 fixed_sol 12/12（拒绝 7 与 12）；B4 deepseek 12/12（拒绝 6）。全部 46 次 cm_call_not_admitted 与全部 3 个 run 级 TOKEN_BUDGET_EXHAUSTED 都落在饱和 run 上；唯一 2/2 通过且成本最从容的 B1（507k/542k）是唯一池未饱和的臂。方向性因果链在数据里完整：**CM 池打满 → 压缩静默停止 → context 自由膨胀（B2 rep1 的 terra worker 单 worker input 达 374k，全场最高）→ token 准入死亡**。需保留的混杂声明：池只在压缩需求大的 run 打满，饱和既是膨胀的原因（压缩停止）也是膨胀的症状（需求大才打满）——因果方向由机制成立，强度需 CM 池扫描（{6,12,24}）才能分离。对照 v10：团队臂 CM 池最高用到 12/12 但**零拒绝**（够用），仅 solo 臂出现 9 次拒绝——CM 需求随预算与探索深度增长（v10 团队 → v11 预算放大），固定池 12 在 40 调用制下系统性偏小。池尽降级路径本身零事故（无 run 因 CM 崩溃）；CM token 均值 108,883/run（占 run 预算 13–16%；v10 为 88,640/run）。

## 3. 统计与措辞规范（§4 逐条落实）

- 主指标用连续量：上表以 run token / worker input / Lead input / CM 调用为主，resolved 二值为辅。
- N=3 的 3/3 vs 0/3 仅表述为"强方向信号"（区间 [0.44,1.00] vs [0.00,0.56] 仍重叠）；1/2、0/1 一律"方向性"。
- 同臂重复在同一 revision、同一镜像、同一冻结 prompt 下错时执行；均值±极差见逐 run 数据。
- 与 v8/v9 的跨批并列仅具"演进证据链"意义（rev9 起 CM 独立池改变了调用预算制；B 波另有 40/900k/16）。

## 4. 综合结论

1. **归因缺口（G1）关闭：** 异构 7/7 的功劳从"装配 CM"改判为"Lead+团队协议"；装配三件套在本任务上无净效应（通过率、成本、worker 探索量三口径一致）。**推论（从 §5 建议提级为结论）：v8 报告的成本工程效果应改记在 on-demand 压缩（v5/v6）账上，装配 CM（v7）当前证据下的正确定位是可选组件（默认 off、按臂开启），pending pilot_v12 的复测。**
2. **预算缺口（G2）关闭：** 预算三约束同松一档后 worker 仍不 completed、通过率不升、成本大涨（+50–75%）——预算不是结构性瓶颈的根因，剩余约束是"共享调用池的先到先得 + worker 交付能力"。worker 个人 8→16 上限几乎不再咬人（仅 1 例 local_call_limit）。附带正面成果：rev9 CM 独立池消除了 v8 的 worker 池尽饿死（13/26 → 0/12）。
3. **CM 池成为新的结构性瓶颈（提级）：** 固定池 12 在 40 调用制下系统性偏小——5/7 run 饱和、46 次静默降级全部集中在饱和 run，3 次 token 死亡亦然；唯一未饱和臂 B1 恰是唯一 2/2 通过且成本最从容的臂。CM 供给不足与 solo 全灭（A1 的制度性混杂）、B2/B3 翻车全部相关。它是下一波的第一扫描变量。
4. **噪声缺口（G3）部分量化：** 同臂 3 rep 仍出现 1/2 翻转臂（B2、B3）；N=3 的强方向信号（A1 vs A3）需要 N≥5–8 或第二任务才能收紧区间，且 A1 归因在 CM 池扫描前不能完全记给"缺团队"。
5. **CM 独立池机制（rev9）工程上工作正常：** 池尽静默降级零事故，账目（cm_call_count/remaining_cm_calls、v2 快照）审计全过。

## 5. 建议的下一波（复核后重排，2026-09-04）

1. **CM 池 {6,12,24} 扫描（前置）：** 同时解决"CM 供给 vs 需求"的因果分离与下条的 A1 归因纯化；池尺寸与 max_calls 的联动比例也应进入设计（v10 团队 28 调用制下 12 够用、v11 40 调用制下系统性不足）。
2. **solo_sol + CM 池 24 补档：** 纯化"团队协议净效应"——若 solo 复活，A1 的 0/3 需再分一部分给 CM 供给不足。
3. **第二任务外部效度：** 已启动 pilot_v12（astropy-14995，事前元数据选择：问题陈述实质长度全池第二 + repo 规模全池最大），复用 ablation9 波与 v10 逐臂对照。
4. **装配默认 off、按臂开启写进 LIMITS：** 以本波否定性结论为据，待 v12 复测后定稿。
5. team_valid 追踪转向 worker 收尾纪律（closing-call 触发条件/finish 提醒强度），不再放预算。

## 6. 证据索引

- 批次：`pilot_v10/`（manifest 55f443fe…，9/9，42.7 min）、`pilot_v11/`（manifest b2e61dad…，7/7，50.1 min）
- 审计：`validation/audit.pilot_v10.json`、`validation/audit.pilot_v11.json`（均 PASS，0 error 0 warning，含 run_started limits 与 manifest effective_limits 一致性断言）
- 每批 `summary.json` / `summary.csv` / `REPORT.md` 为派生报告；原始证据在各自 `results/<run_id>/`。
- 旧批复审：`validation/rev9reaudit.pilot_v{2..9}.json`（8/8 PASS）。
