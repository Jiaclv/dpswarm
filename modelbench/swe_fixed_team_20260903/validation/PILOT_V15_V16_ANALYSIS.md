# pilot_v15 / v15b / v16 终版分析：rev11 修复线收官与决策树执行记录

时间基线：2026-09-04。协议 rev11（F1 压缩宵禁 / F2 cm_max_tokens=4096 / F3 closing 豁免 / F4 法医学字段 / F5 零编辑 banner，gate_revision11 重生成后 204 测试）。预注册计划：`NEXT_EXPERIMENT_PLAN_20260904C.md`，预测 P1–P6 在 prepare 前冻结。

## 0. 总账

| 批 | 内容 | run | 结果 | 审计 |
|---|---|---|---|---|
| pilot_v15 | rev11 修复确认（curfew15，14 run 全 solo） | 14/14 零 stop | 难档 2/8，探针 2/2 | PASS，421 调用 / 7.66M token，零告警 |
| pilot_v15b | B 支预算波（budget15b，900k/40） | 4/4 零 stop | sphinx 2/2，xarray 0/2 | PASS，149 调用 / 3.23M token，零告警 |
| pilot_v16 | 团队难档（teamhard16，全 banner off） | 10/10 零 stop | sphinx 2/2，xarray/seaborn 0/8 | PASS，342 调用 / 5.65M token，零告警 |

三批合计 912 调用 / 16.54M token / ~165 分钟。rev11 候选累计 3,706 调用 / 65.2M token（gate 口径顺延）。

## 1. 预注册预测逐条判定

**P1（默认臂难档 ≥4/8、sphinx ≥2/3）：未达标。** 实际 2/8（Wilson [.07,.59]）：sphinx 1/3、xarray 0/2、seaborn 0/2、sklearn 1/1。A 支不进。但**反证条款未触发**：非全灭，4/8 进入 edited（匹配基线 v10+v14 是 0/6 且 6/6 no_edit）——rev11 把失败模式从"零编辑死"推进到了"死前交付"，落在计划未写死的中间态。

**P2（三分支归因）：所有难档通过 run 都是 (c) 支"池耗尽+F5"路径。** sphinx/sklearn/sympy 通过 run 的首编辑 call 序为 17–19（总 call 18–20），全部晚于池 12 耗尽的 ~12 次 cm 调用；curfew skip 仅 1–3 次且全发生在首编辑之后。F1 全程基本不出场。按预注册条款，M1 结论降级为"F5+池耗尽"。

**P3（池 24 ≥1/2 → M2）：0/2，M2 第二次独立证伪。** 两个 cm24 run 各用 16 次压缩、**池 24 剩 8 格未用即 budget_exhausted 死亡**（~570–590k，与池 12 同天花板）。加池救不了 solo：绑定约束是总 token 预算，不是压缩供给。路由 B 支。

**P4（宵禁 OFF ≤1/2 → F1 归因）：反转，2/2 全过。** 按预注册措辞"活性成分为 F2/F5"。且方向性反指：sphinx 上 nocurf 2/2 vs 默认 1/3（n 小，仅方向性）——**F1 宵禁不仅不是活性成分，在 sphinx 上疑似负贡献**。sympy 探针通过（575k，skip=1）说明宵禁没杀饱和压缩题的验证尾，下行风险未坐实但也不支持保留。

**P5（v16 heterooff 难档 ≥3/4、sphinx 回归 2/2）：前者 0/4 失败，后者 2/2 通过。** canary 按设计不是等价性检验：sphinx 团队 2/2（559k/560k）证明 rev11 未伤及团队臂基本盘，A 支历史结论（v10 团队救场）不受污染。

**P6（F3/F4 效果）：两项均未达标。**
- `worker_cleanup_discarded` 且 delta>0：目标 0 例，实际 **3 例，全部发生在装配 ON 臂**（xarray ON rep1 ×1、seaborn ON rep1 ×2）——worker 编辑产物在 cleanup 被丢弃，方向性指向装配增加交接损耗。
- team_valid 率：v16 仅 1/10（xarray OFF rep2），较 v10 sphinx 的 1/3 **下降**。worker 终态分布：adopted 7/20、edited_no_delivery 7/20、delivered_not_adopted 2/20、no_edit 4/20——45% 的 worker 实例编辑了却没（被）交付。**交付链仍是机制最薄弱环节**，F3 closing 豁免在难档没挽回它。

**M4（装配 ON/OFF 任一题 2/0 分裂 → C 支）：未触发。** xarray 0/2 vs 0/2、seaborn 0/2 vs 0/2。**装配线按预注册口径关闭：四任务否定证据成立**（sphinx +11.3% 成本 / astropy −1.2% / xarray、seaborn 0 增益且 ON 臂独占 3 例交接丢弃）。定稿措辞：装配默认 off、按臂开启。

## 2. B 支预算波：预算假说部分证实

| 任务 | 600k/28 默认 | 900k/40 | 判定 |
|---|---|---|---|
| sphinx | 1/3（过的 563k） | **2/2**（778k / 827k） | 预算对 sphinx solo 是硬约束：两个通过 run 都超 600k |
| xarray | 0/2 no_edit | **0/2** edited-but-wrong（749k / 875k） | 加预算只推进了失败模式，救不了结果 |

结论：**"需求结构性超预算"因题而异，不能作为普遍解释。** 且 sphinx solo 需要 ~1.35× 预算才能过，而团队臂在 600k 共享预算下 v10 3/3 + v16 2/2——**上下文分工在 sphinx 上等价于 ~1.35 倍 solo 预算**，这是团队机制目前最硬的一条效率证据。

## 3. 难度再分层：难档不是铁板一块

三波合并后，难度谱必须细分（v14 探针的"难档"内部异质）：

- **可救难档**（sphinx-8035）：solo 600k 1/3、nocurf 2/2、900k 2/2；团队 600k v10 3/3 + v16 2/2。机制（F2/F5、组队、加预算）都有效。
- **超纲难档**（xarray-7229、seaborn-3069）：solo 600k 0/4 no_edit、solo 900k 0/2 edited-wrong、团队 600k 0/8（含一次双 worker adopted 的干净执行仍败）。**当前协议+预算包络内无臂可解**，任务本身超纲。
- sklearn-25232 介于其间（solo 1/1）。

对 xarray OFF rep2 要特别注明：双 worker adopted、team_valid=True、协议执行教科书般干净，补丁仍然错——这不是机制故障，是能力/预算包络外任务。

## 4. 机制线总判定（rev9–rev11 合并口径）

1. **F2/F5 是 rev11 的活性成分**（P4 2/2 + P2 全 (c) 支）：把 solo 难档从 0/6 no_edit 推进到 4/8 edited、2/8 通过。
2. **F1 宵禁归因失败**，方向性疑似负贡献；rev12 若保留需重做（验证期豁免或干脆移除）。
3. **M2（加池）两次独立证伪**（v13 池 24 0/2、v15 池 24 0/2，均池未耗尽先死于总预算）。CM 池 12 默认值维持。
4. **预算是因题而异的约束**（sphinx 可救、xarray 不可救），"设大预算"不是通用解；自适应预算（进展信号续命、空转止损）是产品化方向。
5. **团队价值条件性第三次独立印证**：可救难档团队救场（v10 3/3 vs solo 0/3；v16 2/2 回归），易档纯开销（v12），超纲档无效（v16 0/8）。
6. **装配线关闭**（四任务否定证据）。
7. **worker 交付链是下一阶段唯一最大瓶颈**：45% 编辑未交付，team_valid 1/10。

## 5. 后续登记

- **v17（条件性组队，需 rev12）**：Lead 判断难度再组队 + F1 重做/移除 + worker 交付链修复。选题：可救档 sphinx + 超纲档 xarray 各 n≥3，检验"Lead 能否事前分辨"。
- **v18（mini-swe-agent 同预算对照）**：外部 baseline，600k/28 与 900k/40 双口径。
- GLM×2 复验与角色反转矩阵继续停靠（模型×职责线，不与机制线混杂）。

## 6. 偏差记录

- v15 单臂实例块（xarray/seaborn/cm24/nocurf）同臂 rep 并行成对，结构性无法错时，已在计划评审中接受。
- gate_revision11 原地重生成后，pilot_v15 manifest 绑定旧 gate sha，今后对 pilot_v15 再 `cli run` 会报 drift（该批已审计完成，批内冻结副本不受影响）。
- v16 团队臂按评审修订全带 `edit_status_banner=False`，P6 的 team_valid 下降判读需注意该臂配置与 v10 不完全同构（F3 closing 豁免为设计意图）。
