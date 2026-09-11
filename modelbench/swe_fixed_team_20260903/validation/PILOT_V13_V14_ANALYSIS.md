# pilot_v13 / pilot_v14 分析报告：CM 池扫描收官 + 任务探针面板（rev10）

时间基线：2026-09-04。协议 rev10（gate_revision10 PASS，53 sources，184 离线测试，装配默认 off；旧批 v2–v12 复审 11/11 PASS）。
两批审计均 PASS 零告警：v13 188 调用 / 3,335,159 token（25.8 min）；v14 165 调用 / 2,915,752 token（43.7 min）。合计 6.25M，与计划 §7 估算 ~6M 一致。

## 1. pilot_v13：CM 池扫描（sphinx-8035，28 调用/600k/1800s，装配 off）

| 臂 | 池 6 | 池 24 | 池 12 基线（v10） |
|---|---|---|---|
| solo | **2/2 ✅** | **0/2 ❌** | 0/3 ❌ |
| hetero（装配 off） | ✅（outcome=budget_exhausted） | ✅（completed） | 3/3 ✅ |

**关键细读数（全部 4 个 solo run 的 outcome 都是 budget_exhausted——包括两个通过的）：**

| run | resolved | Lead input | CM 调用/池 | cm 拒绝 | CM token |
|---|---|---|---|---|---|
| solo.cm6 | ✅ | 484k | 6/6 | 9 | 52k |
| solo.cm6.rep2 | ✅ | 455k | 6/6 | 8 | 57k |
| solo.cm24 | ❌ | 412k | 16/24 | 0 | 150k |
| solo.cm24.rep2 | ❌ | 399k | 17/24 | 0 | 166k |
| hetero.cm6 | ✅ | 255k | 6/6 | 5 | 49k |
| hetero.cm24 | ✅ | 248k | 7/24 | 0 | 65k |

### 判定（NEXT_EXPERIMENT_PLAN_20260904B §2 预设规则）

1. **C2 落定：池 24 仍 0/2 → "团队协议净效应"在难任务上坐实，且机制比预设判定更干净。** 池 24 给足了压缩供给（16–17 次压缩、零拒绝、CM token 150–166k），solo 仍然全部死于 token 准入——**不是 CM 供给不足，是 solo 的上下文需求结构性超出预算**（Lead input ~400–412k，压缩后仍持续膨胀）。v10 的 solo 0/3 归因不用改判。
2. **池 6 solo 2/2 是"死前交付"的运气，不是池尺寸效应。** 两个 run 同样 budget_exhausted（Lead input 455–484k，比池 24 更臃肿——压缩少 10 次上下文反而更大），只是恰好在预算耗尽前交付了正确 patch。非单调谱（6→2/2、12→0/3、24→0/2）在 N=2 下 Wilson 区间大幅重叠，记"方向性反常，无机制解释"；sphinx solo 的真实画面是：**通过 = 与 token 死亡赛跑并恰好抢先完成**，run 间方差主导。
3. **团队臂对池尺寸完全不敏感**（6/12/24 全过、Lead input 恒 ~250k）——任务分解把 Lead 上下文压到 solo 的 60%，远离死亡线；池 6 的 5 次拒绝也不影响交付。CM 池尺寸在团队配置下不是有效杠杆。
4. **CM 池扫描结论：12 作为默认值可保留**（solo 需求超池时加池无效——池 24 证据；团队需求 7 以下）。压缩的价值在"维持 Lead input 稳态"（团队臂 250k vs solo 400–480k 的差异主要来自任务分解而非压缩），不在"救 solo"。

## 2. pilot_v14：任务探针面板（8 题 × solo_gpt-5.6-sol × 1，28 调用/600k/CM 池 12）

| 任务 | resolved | run token | Lead input | CM 池 | cm 拒绝 | TOKEN 死 | 档 |
|---|---|---|---|---|---|---|---|
| pallets__flask-5014 | ✅ | 91k | 90k | 0/12 | 0 | 无 | 易 |
| matplotlib-25122 | ✅ | 174k | 149k | 2/12 | 0 | 无 | 易 |
| pytest-7571 | ✅ | 187k | 160k | 2/12 | 0 | 无 | 易 |
| django-14140 | ✅ | 291k | 250k | 3/12 | 0 | 无 | 易→中 |
| sympy-16792 | ✅ | 511k | 383k | 12/12 | 3 | 无 | 中 |
| pydata__xarray-7229 | ❌ | 550k | 412k | 12/12 | 3 | **是** | 难 |
| mwaskom__seaborn-3069 | ❌ | 558k | 432k | 12/12 | 5 | **是** | 难 |
| scikit-learn-25232 | ❌ | 552k | 416k | 12/12 | 4 | **是** | 难 |

**十题难度谱**（8 探针 + 2 标定点）：易 5（flask/matplotlib/pytest/django/astropy✅~334k）、中 1（sympy）、难 4（xarray/seaborn/scikit-learn/**sphinx**）。

### 读数

1. **难档三题与 sphinx 机制画像完全同型**：CM 池 12/12 饱和 + 3–5 次静默降级 + Lead input 410–432k + TOKEN 死亡。易档全部 completed、零拒绝、Lead input <300k（计划 §3 的预设分档线）。**探针信号（成本逼近准入线 / CM 饱和 / TOKEN 死）与任务难度高度一致，探针选任务的方法学成立**——这正是替代作废的"陈述长度/repo 规模"代理所要的。
2. **陈述长度代理再次证伪**：seaborn（1,188 字符，中位长度）进难档、django（1,152，几乎同长）进易档；xarray（最长）难、astropy（次长）易。长度与难度无单调关系。
3. **"难"的可操作定义**（本谱给出）：solo 探针 TOKEN 死亡或成本 ≥~510k。中/易的分界 ~300k。sympy 是珍贵的"中档"样本（通过但贵）。
4. **探针数据即外部效度小样本**：8 题 × solo 的通过率 5/8——solo 在易档 5/5、难档 0/3，再次印证团队协议价值的条件性（v10/v12 结论的独立重复）。

## 3. v15/v16 选题建议（覆盖三档；统计规范第 6 条：跨任务外推需两难度档各 N≥3）

- **难档（N≥3 载体）**：sphinx-8035（12+ run 历史，最深）、xarray-7229、seaborn-3069（scikit-learn-25232 备选）
- **中档**：sympy-16792
- **易档（N≥3 载体）**：astropy-14995（已标定）、flask-5014、pytest-7571
- v15（条件性组队，rev11）：难档 2 题 + 易档 2 题 × {强制组队, Lead 决策组队, solo} 对照，N=3/臂/题——预算另计。
- v16（baseline 对照）：同面板上 mini-swe-agent 同预算。

## 4. 统计与措辞规范执行记录

- Wilson 95%：v13 solo 池 6 2/2 [0.34,1.00]、池 24 0/2 [0.09,0.91]——重叠，全部按"方向性"措辞；C2 判定依据的是预设规则的 0/2 事实，非区间分离。
- N=1 探针仅作分档信号，不做通过率推断；跨任务结论（团队价值条件性）此前已有 sphinx N=3 + astropy N=3 支撑，符合第 6 条。
- 同臂重复错时执行；跨批并列（v13 vs v10 池 12）为演进证据链口径（同任务同预算，池为唯一变量，可比性较强）。

## 5. 证据索引

- 批次：`pilot_v13/`（manifest f7d3ef87…，6/6）、`pilot_v14/`（manifest 62f7dbe3…，8/8，8 个 instance 各自镜像）
- 审计：`validation/audit.pilot_v13.json`、`validation/audit.pilot_v14.json`（均 PASS，0 error 0 warning）
- 计划：`validation/NEXT_EXPERIMENT_PLAN_20260904B.md`；gate：`validation/gate_revision10.json`
- 旧批复审：`validation/rev10reaudit.pilot_v{2..12}.json`（11/11 PASS）
