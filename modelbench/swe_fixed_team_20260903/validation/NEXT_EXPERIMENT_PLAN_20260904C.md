# pilot_v15 / v16 实验计划：rev11 机制确认波（2026-09-04C，评审修订版）

时间基线：2026-09-04。协议 rev11（gate_revision11 PASS，54 sources，201 离线测试；F1 压缩宵禁 / F2 cm_max_tokens=4096 / F3 closing 豁免 / F4 法医学字段 / F5 零编辑 banner，默认 ON、可 limits_override 单臂关闭）。
依据：`TRAJECTORY_FORENSICS_20260904.md`、`PILOT_V13_V14_ANALYSIS.md`、`INSIGHTS_ZH.md`；前置 `NEXT_EXPERIMENT_PLAN_20260904B.md`（本计划取代其 §4 排序）。
本版已经独立评审（plan 子代理），阻塞项全部修订：P2 归因签名补第三分支、基线分母改为匹配口径、v16 团队臂屏蔽 F5 banner、排程改为实例块连续、统计框架明示为第 6 条的收窄。

## 1. 机制命题与因果模型（评审确认成立）

零编辑死亡的 run（v10×3、v13cm24×2、v14×3 共 8 例）从未进入实现期——**F1 宵禁在这类 run 里不会触发**（宵禁从首次编辑起生效，`runner.py:461`）。各机制作用域：

| 机制 | 作用域 | 对零编辑死亡 | 对实现期死亡 |
|---|---|---|---|
| F1 宵禁 | 首次编辑之后 | 不直接作用 | 保护实现/验证期不被压缩打断 |
| F2 4096 | 每次压缩 | 摘要保住"下一动作"，降低压缩后迷失 | 同左 |
| F5 banner | token 过半且零编辑时 | **直接针对**：陈述编辑状态事实 | — |
| 池耗尽时点 | 结构性 | 池早尽=压缩早停=上下文累积（v13 cm6 机制） | 同左 |

**复合预测**：rev11 默认（F1+F2+F5、池 12）下，F2/F5 把首次编辑提前到压缩循环锁死之前，F1 随后保护实现期。**但最可能的通过路径是"池 12 中途耗尽→压缩停止→上下文滞留→F5 促成编辑"**（v13 cm6 已事件级证实该路径），此路径 F1 全程不出场——归因必须靠 forensics 签名区分，见 P2 三分支。

## 2. pilot_v15：solo 难档确认波 + 回归探针（14 run）

载体：难档四题 + 两个通过型回归探针；全部 solo_gpt-5.6-sol，预算 600k/28 不变。

| 臂 | 题 | n | override | 检验 |
|---|---|---|---|---|
| rev11 默认 | sphinx | 3 | 无 | M1 主检验（匹配基线 v10 0/3） |
| rev11 默认 | xarray | 2 | 无 | 难档外推（v14 0/1） |
| rev11 默认 | seaborn | 2 | 无 | 同上（0/1） |
| rev11 默认 | sklearn | 1 | 无 | 同上（0/1），撑 tier 分母 |
| 宵禁 OFF | sphinx | 2 | `cm_edit_curfew=False` | F1 归因消融 |
| 池 24 | sphinx | 2 | `cm_call_allowance=24` | M2（v13 基线 0/2） |
| rev11 默认 | sympy | 1 | 无 | **F1 下行风险探针**：v14 该题靠 12/12 饱和压缩通过（511k）；宵禁在首编辑后切断压缩，长验证尾是否被杀 |
| rev11 默认 | astropy | 1 | 无 | 易题快路回归（v12 3/3，均 334k） |

**匹配基线（评审修订）**：池 12 solo = v10 sphinx 0/3 + v14 三题 0/3 → **0/6 通过、6/6 no_edit**。v13 cm24（池 24）与 v11（团队）只作支持性法医学证据，不进基线分母。

**预注册预测**（prepare 前冻结）：
- P1：默认臂难档合计 ≥4/8（匹配基线 0/6）；sphinx ≥2/3。
- P2（三分支归因签名）：通过 run 按 forensics 归类——
  (a) 首编辑早于池耗尽且后续出现 `cm_skipped(edit_phase_curfew)` → **F1 证据**；
  (b) 首编辑早于池耗尽但无 curfew skip → F2/F5 路径；
  (c) 首编辑晚于第 12 次 cm_call（池耗尽后）→ **"池耗尽+F5"路径，不作 F1 证据**；若默认臂通过主要由 (c) 构成，M1 结论降级为"F5+池耗尽"，F1 归因只能由宵禁 OFF 臂的对等失败佐证。
- P3：池 24 臂 ≥1/2 通过 → M2 成立；若 0/2 且 forensics 显示压缩循环重现 → F2/F5 不足以突破编辑前区间，转决策树 B 支。
- P4：宵禁 OFF 臂 ≤1/2 通过且重读序列重现 → F1 归因成立；2/2 通过 → 活性成分为 F2/F5（v17 细分）；**1/2 记方向性存疑，不进归因**。
- **反证条款**：默认臂难档 8 run 全灭且 `death_phase` 全 no_edit → F2/F5 干预未改变零编辑死亡，"任务需求超 600k 预算"复位为竞争解释 → B 支。
- sympy/astropy 探针读数：sympy 若失败且 forensics 显示宵禁切断验证期压缩 → F1 下行风险坐实，rev12 需"验证期豁免"细化；astropy 若失败 → rev11 有未预期回归，整波停检。

## 3. pilot_v16：团队难档 + 装配决策树（10 run）

terra 实现 → glm 测试固定组合（同 v10/v12/v13），载体 xarray / seaborn + sphinx 回归控制。
**评审修订：v16 全部团队臂带 `edit_status_banner=False`**——Lead 作为整合者不产生 bash 编辑（采纳走 `review_worker`），`edit_detected` 永不置位，而难档团队 run 必跨 300k 阈值 → banner 会对每个团队 Lead 中后段持续误报 "no edits yet"，语义误导且动摇"逐字采纳 W1"主模式。F5 的作用域限定 solo 是 rev12 候选修复，本轮用既有 override 机制隔离。

| 臂 | 题 | n | override | 检验 |
|---|---|---|---|---|
| heterooff | xarray | 2 | banner off | 团队难档外推 |
| hetero（装配 ON，ASSEMBLY9_ON 钉住） | xarray | 2 | banner off | M4 ON/OFF 差 |
| heterooff | seaborn | 2 | banner off | 同上 |
| hetero（ON） | seaborn | 2 | banner off | 同上 |
| heterooff | sphinx | 2 | banner off | rev11 团队回归（v10 基线 3/3） |

**预注册预测**：
- P5：heterooff 难档（xarray+seaborn）≥3/4 通过；sphinx 回归 2/2。**注意 n=2 回归臂是 canary 不是等价性检验**（F3/closing 豁免改变团队收尾行为是设计意图）；若 sphinx 回归 0/2 或 1/2 → A 支结论受污染，先诊断再定。
- P6（F3/F4 效果）：`worker_cleanup_discarded` 且 `delta_bytes>0` = 0 例；`edited_no_delivery`/`delivered_not_adopted` 占比下降；team_valid 率较 v10 sphinx（1/3）上升。
- M4 判定：任一题 ON/OFF 出现 2/0 或 0/2 → 触发 C 支装配分解；两题均无差 → 装配线关闭，定稿口径为"**四任务否定证据**"（sphinx+astropy+xarray+seaborn；此时恰好满足规范第 6 条：难档 sphinx N=3、易档 astropy N=3）。

## 4. 工程需求（coder）

1. cli.py 新增两 wave（仿 CMSCAN13 四元组 + probepanel 多实例模式）：`curfew15`（14 条）、`teamhard16`（10 条）；各条目 (instance, arm, rep, override)，effective_limits 合并照旧。
2. **排程（评审修订）**：实例块连续（probepanel 先例，`instance_blocks` 按连续条目分块，避免镜像反复重建与并行度退化）；交错只在块内进行（同题内 default/宵禁OFF/池24 轮转，任意相邻 pair 不含同臂重复，§4.4 错时惯例）。
3. **gate 重生成**：sources() 含 cli.py（:78），prepare 断言 `gate['runtime_sources'] == sources()`（:108）——cli 变更后必须重跑 201 离线测试并按 rev6–11 惯例重新生成 gate_revision11.json（原地安全：rev11 gate 下尚无已 prepare 批次）。**不要动 PLAN.md**（同在 sources 内）。旧批复审不重跑（rev11reaudit 13/13 已存档，wave 新增不改审计口径）。
4. 审计照旧产出 audit.pilot_v15/v16.json，须 PASS 零告警；读 forensics 时按事件自带 `pattern`+`tool_call_id` 交叉核对首编辑触发命令（`>>`/`tee` 可能误报、`echo >` 漏报 fail-open——读数时须知）。

## 5. 执行与决策树（触发条件已写死）

顺序：**v15 → 审计+forensics 快读 → v16 → 审计 → 分析定稿**。
- **A 支**：P1 达标（难档 ≥4/8 且 sphinx ≥2/3）即为主体成立；P2/P3/P4 只决定归因注记（活性成分记为 F1 / F2/F5 / 池耗尽+F5，按签名三分支如实写）。→ 登记 v17（条件性组队，需 rev12）/ v18（mini-swe-agent 同预算对照）。
- **B 支**（反证条款触发）：停 v16，改 900k/40 solo 预算波（sphinx+xarray 各 2）检验预算假说。
- **C 支**（M4 触发）：装配分解波（scout-only / package-only × sphinx+xarray，各 n=2）。
- 统计框架（评审修订）：**本轮只做难档单档内的跨任务方向性结论，是对规范第 6 条的显式收窄**（单档、合并分母、题间 n 不均衡），不构成跨档外推；报告须同时给单题数字与合并数字；Wilson 95% 照旧；n≤2 格一律方向性措辞。

## 6. 预算与排他说明

- v15：14 run，上限估算 14×~575k ≈ 7.7M token / ~70 min（注意：v13 证据是通过 run 仍收敛 529–575k 的竞速型，"通过更省"只对提前 finish 型成立，估算不依赖省）。
- v16：10 run ≈ 5.5M / ~50 min。合计 ~13M / ~2h。
- 原 v15（条件性组队）/ v16（baseline）顺延 v17/v18；GLM×2 复验与角色反转矩阵停靠（模型×职责线，不与机制线混杂）。
- 预注册纪律：§2/§3 预测在 prepare 前冻结；计划外改臂必须新开文件记录。

## 7. 风险与回退

- 全部改动可回滚：LIMITS 旗标 + git 还原；新 wave 不影响旧批。
- F1 已知风险面：sympy 探针专测"宵禁误伤长验证阶段"；若确认，rev12 加验证期豁免。
- F5 已知风险面：团队 Lead 误报——本轮 v16 团队臂已屏蔽；solo 侧是设计目标场景。
- 监护：后台任务 + 完成通知，不用 cron（上轮清理教训）。
