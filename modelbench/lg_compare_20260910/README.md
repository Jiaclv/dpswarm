# lg_compare_20260910：经典 Orchestrator vs OrchestratorLG 真实 API 对照

## 实验设计

**问题**：LangGraph 版实验编排器（`dpswarm-plugin/dpswarm/orchestrator_lg.py`）
声称修了两个结构问题——P1（并行执行、串行验收）与 P0（deps 串行链只解锁
不传递上游交付）——在真实模型上是否成立，且行为/账目与经典臂同口径。

**自变量**：只有调度层。
- `classic` 臂：`Orchestrator` 子类（BigToken 口径，max_tokens=16384）；
- `lg` 臂：`OrchestratorLG` 子类（同 BigToken 口径）。

**控制变量（同构臂）**：所有角色（Lead 决策 / Lead 验收 / worker / CM）一律
`glm/glm-5.3-flash`（Level.B，coding 档）；catalog 只含这一个模型（Lead 的
路由选择空间 = 臂定义）；每臂独立 ControlPlane 与隔离 run 目录；
`max_turns=6`；每 run token 护栏 200K（BudgetExceeded 中止该 run 并如实记账）。
glm-5.3 系列 thinking 强制开，CoT 计入输出上限，故全部调用面
max_tokens=16384（沿用 driver._backoff_complete 的 Phase A 实证口径）。

**任务**（compare.py TASKS，任务书强引导拓扑；Lead 实际没按预期拆则如实记录）：
- `parallel`：三个零依赖小函数（回文/罗马数字/进制转换），引导 fission
  3 subtasks 无 deps；
- `serial`：A 设计 JSON Schema → B 依据 A 的验收交付实现校验器，引导
  fission 2 subtasks 且 B 标 deps=[0]。

每任务 × 两臂，共 4 个 run：`classic-parallel`、`lg-parallel`、
`classic-serial`、`lg-serial`。

## 观测口径

- **调用级日志** `logs/<run_id>.jsonl`（LoggingProvider：`ts` +
  `latency_ms` + usage 全账）。同构臂 `role_by_model` 失效（全模型同名），
  改由 `RoleTagger` 按消息内容分类：`lead-decision` / `lead-review` /
  `worker` / `cm`。
- **事件账本** `runs/<run_id>/events.jsonl`（控制面 store 原样），
  `runs/<run_id>/outcome.json`（orchestrator 返回值 + 摘要），
  `runs/<run_id>/packages/`（装配包落盘）。
- **并行证据**：同臂同角色两次调用的时间区间 `[ts, ts+latency]` 是否重叠——
  两臂的 worker 都应重叠（父类本来就有 ThreadPoolExecutor 并行）；**只有
  lg 臂的 lead-review 应重叠**（父类是串行 `for p in pending: _review`）。
- **P0 交接证据**：serial 任务下游 item 的装配包文件，lg 臂应含
  `## 上游交付摘要` 与 `（不可信摘要，以实际文件为准）` 标注（且经 CM
  零损压缩，成本记 `ctx-job:<下游 item>`）；classic 臂应两者皆无。

## 运行方式

```bash
cd modelbench/lg_compare_20260910
python compare.py        # preflight → 4 run → analyze → results/summary.json
```

前置：`modelbench/keys.local.json` 或环境变量 `GLM_API_KEY` 可用
（preflight 一次 512-token 调用验证，不可用即退出不跑空实验）。

## 结果摘要

4 个 run 全部完成（lg-serial 首轮因后台任务超时被杀，清理残留后完整重跑；
两臂代码/配置同一会话窗口内一致）。实测：

| run | 实际拓扑 | 终局 | tokens（API 口径） | 墙钟 |
|---|---|---|---|---|
| classic-parallel | fission ×3 无 deps（符合预期） | accepted-by-lead | 10,005 | 159s |
| lg-parallel | fission ×3 无 deps（符合预期） | accepted-by-lead | 8,837 | **95s** |
| classic-serial | fission ×6（deps=[0]），5 A accepted / 6 B escalated | 未收束（max_turns=6 耗尽） | 88,780 | 1744s |
| lg-serial | fission ×6（deps=[0]），6 A accepted / 6 B escalated | 未收束（max_turns=6 耗尽） | 113,814（含 CM 16,839） | 2611s |

### P1 并行验收证据（成立）

- `lg-parallel`：3 次 lead-review 调用时间区间**存在重叠**（review_overlap=true），
  墙钟较 classic 臂 **-40%**（95s vs 159s），终局与账本口径一致
  （两臂均 3 worker accepted、同结构事件序列）。
- `classic-parallel`：3 次 lead-review **无重叠**（父类串行 `for: _review`），
  worker 调用两臂均有重叠（父类本来就有线程池并行执行）。
- serial 两臂 review_overlap 均为 false——每波只有 1 个 item，符合预期
  （无并行空间，不是回归）。

### P0 deps 交接证据（机制成立；任务级收束未达成）

- `lg-serial`：全部 6 个下游 B item 的装配包含 `## 上游交付摘要` +
  `（不可信摘要，以实际文件为准）` 标注，经 CM 零损压缩（保留 draft 版本、
  字段约束、multipleOf 等关键标识），成本记 `ctx-job:<B item>`（6 笔）。
- `classic-serial`：全部下游包两者皆无——B worker 在交付中自述
  「⛔ 阻塞：缺少上游输入」「拿不到 A 的 schema 文本」，被 Lead 打回后
  上交；Lead 再裂变新对，死循环至轮次耗尽。
- 但 lg 臂 B 同样全部 escalated（6/6），拒因变了：从「缺上游输入」变成
  「交付物理截断」与「摘要不逐字、doctest 无法逐字引用 A」。**P0 修复让
  信息到达，但未能让任务收束**（见意外发现 1/2）。

### 意外发现（真实数据，均为后续改进的输入）

1. **CM 摘要路径与"逐字引用"型验收冲突**：本任务 B 的验收条件是 doctest
   逐字引用 A 的 schema；CM 零损压缩保留语义但不保证逐字，且"不可信"
   标注本身促使 worker 不敢引用（多个 B 交付自述此顾虑）。降级路径
   （无 CM 时截断 2000 字符直贴）反而更接近逐字。启示：交接策略应按
   下游验收条件选择——逐字依赖走 artifact 引用/原文直贴，语义依赖走
   CM 摘要。（首轮 lg-serial 被中断前曾有 1 个 B 经 2 次重试后被验收，
   说明非必然失败，但方差大。）
2. **16384 max_tokens 是 B 级 thinking 模型的硬瓶颈**：B 的交付（实现 +
   3 个含逐字 schema 的 doctest）+ CoT 频繁超限截断，两臂 reject 理由
   高频出现"函数体缺失/代码在字符串字面量中断"；lg-serial 有 2 次
   验收裁决 JSON 自身被截断（protocol_failures=2，落 reject→修述重试
   保守路径，行为正确但烧预算）。
3. **同构单模型目录下 capability 归因 = 直接上交**：`recommend_upgrade`
   无升级目标 → `upgrade-exceeds-lead-level (§7 推论②)` 上交（lg-serial
   6 次 escalated 中 4 次）。打回重试预算部分被这条短路浪费——同构臂里
   capability 归因或许应改走"修描述/修 context 重试"而非升级。
4. classic-serial 的 Lead 死循环模式有完整账本：fission→A accepted→
   B escalated→再 fission，actions = [fission×3, deps-stuck, fission×2]，
   是 P0 问题最直观的体内证据。

产物：`results/summary.json`（全量对比数据）、`runs/<run_id>/`
（outcome.json + events.jsonl + packages/）、`logs/<run_id>.jsonl`
（调用级日志，含时间戳与 token 全账）。

## r2 轮次（阶梯 + 三层交接后复跑）

机制改动（r2 两臂共享 Part A，仅 lg 臂有 Part B，变量由此分离）：

- **Part A（两臂）**：角色级 max_tokens 下限（worker 32768 / Lead 16384，
  封顶 catalog 登记的模型 max_output=131072）+ 截断阶梯（stop_reason=
  max-tokens → 翻倍重试、不耗 attempt、记 `max_tokens_escalated` 审计事件）。
  落点在 dpswarm 父类 `Orchestrator._complete_worker_ladder` 等，r2 起两臂
  直接用 `Orchestrator` / `OrchestratorLG`，不再覆写 BigToken。
- **Part B（仅 lg 臂）**：deps 交接升级为三层包——L2 CM 摘要（标注改为
  「摘要仅供导航，逐字内容以 L1/原文为准」）、L1 确定性逐字原子事实层
  （fenced 代码块/签名/版本号，上限 4000 字符）、L0 artifact 引用 +
  PULL 原文回查（`_pull_respond` 先查本 run 上游 `_submissions` 再查
  MemoryService）。

运行：`python compare.py --tag r2`（runs-r2/ logs-r2/ results/summary-r2.json，
r1 数据原样保留）。

### r2 结果摘要

4 个 run 全部完成（与 r1 同任务书、同会话代码；r1 数据原样保留作基线）：

| run | 终局 | tokens | 墙钟 | 截断调用 | 阶梯升档 | r1 对照 |
|---|---|---|---|---|---|---|
| classic-parallel | accepted-by-lead | 20,108 | 301s | 0 | 0 | 10,005 / 159s |
| lg-parallel | accepted-by-lead | 9,335 | **75s** | 0 | 0 | 8,837 / 95s |
| classic-serial | 未收束（轮次耗尽） | 134,469 | 2408s | 0 | 0 | 88,780 / 1744s，B 0/6 |
| lg-serial | 未收束（轮次耗尽） | 78,199（含 CM 11,659） | 1261s | 0 | 0 | 113,814 / 2611s，B 0/6 |

**Part A 效果（两臂共享）**：截断调用 4 个 run 全为 0（r1 serial 两臂高频
max-tokens、验收裁决 JSON 都被截断过）。角色下限（worker 32768）单独就
消除了截断，阶梯全程未触发（ladder_escalations=0）——它作为兜底安全网
存在，未消耗即正确。反面：没有截断早退后，模型写更长交付，serial 两臂
token 均高于 r1（classic 89K→134K，lg 114K→78K 是例外，因失败轮数更少）。

**Part B 效果（仅 lg 臂）**：lg-serial 全部 5 个下游 B 包装配包三层齐全
（L2 CM 摘要 + L1 逐字原子事实 + L0 PULL 引用）；L1 层含 A 的 schema
**逐字** fenced 块（draft URL、字段约束原样）；B 交付明确自述"依据
[wi-xxx] 的 schema 逐字文本实现……逐字一致"——信息链完全打通。
classic 臂下游包仍三层皆无，B 0/6 原地踏步（P0 死循环模式与 r1 同构）。

**B 收束率：0/6 → 0/5（未改善，但失败模式再次迁移）**。r2 的 B 拒因不再
是"缺上游输入"也不是"截断"：5 次 escalated 中 4 次是 capability 归因 →
`upgrade-exceeds-lead-level`（同构单模型目录无升级目标，§7 推论②直接
上交，**不消耗重试预算、不给修述机会**——该分支甚至不落 reject 事件，
verdict_reason 未持久化）；1 次 contradiction；另有 1 次 Lead 验收裁决
JSON 在 16384 处截断（unparseable → 保守 reject）。任务本身（逐字引用 +
完整实现 + 3 doctest）对 B 级 flash 模型是能力天花板问题，机制已把
"信息可达"变量干净分离出来。

**P1 证据复验**：lg-parallel 的 review_overlap=true（3 次验收并行），
classic-parallel=false；lg-parallel 仍是全场最快（75s）。

**r2 新增改进输入**（比 r1 更明确的机制缺口）：
1. 同构目录下 capability 归因应改道（修述/修 context 重试），否则
   `upgrade-exceeds-lead-level` 成为重试预算的短路黑洞，且拒因不留账本；
2. Lead 验收面的 16384 下限仍会截断长 verdict_reason 的裁决 JSON——
   验收调用也值得挂截断阶梯（Part A 只接了 worker 面）。

r2 产物：`results/summary-r2.json`、`runs-r2/`、`logs-r2/`。

## r3 轮次（验收面阶梯 + 三层交接深化 + 语义型对照任务）

机制增量（详见阶段 1）：Lead 决策/验收面对称截断阶梯（surface 字段区分）、
L1 提取深化（优先级丢弃 fenced>JSON>签名>其他、未闭合围栏收容、表格行/
键值/路径行入网）、依赖类型分派（verbatim 命中 → L1 上限 8000 且前置，
记 `handoff_profile` 事件）、PULL 审计（`pull_served`）。新增语义依赖型
串行任务 `semantic`（A 归纳 12 条内联反馈 → B 写管理层简报，不卡逐字）。
驱动：`run_r3.py`（子进程逐 run、45 分钟上限、进度写 `r3-progress.log`）。

### r3 结果摘要

| run | 终局 | tokens | 墙钟 | B 收束 | 对照 |
|---|---|---|---|---|---|
| classic-serial | accepted-by-lead | 30,752 | 575s | 1/1（但见意外 2） | r1 0/6、r2 0/6 |
| lg-serial | 未收束（轮次耗尽） | 99,243（含 CM 15,726） | 1573s | 0/6 | r1 0/6、r2 0/5 |
| classic-semantic | 未收束（轮次耗尽） | 74,130 | 1374s | 1/5（但见意外 1） | 新增任务 |
| lg-semantic | 未收束（轮次耗尽） | 52,715 | 940s | 0/3（B 从未晋级） | 新增任务 |

**机制触发证据**：
- `handoff_profile`：lg-serial 6 次分派全部 verbatim（正确）；semantic 两臂
  未发生交接（A 未有效收束，B 未晋级）。
- `max_tokens_escalated`：四轮 0 触发——角色下限已消除截断，阶梯（含新的
  Lead 面）保持兜底闲置；lg-serial 仍有 1 次 protocol_failure，是模型输出
  非法 JSON（非截断），保守 reject 路径行为正确。
- `pull_served`：0 触发——verbatim 任务里 L1 逐字层已够用，B 未回查原文。
- 三层包：lg-serial 6/6 下游包含 L1 前置 + 逐字 schema（B 交付自述
  「逐字依据：模块级常量 LEDGER_SCHEMA」「严格依据 A 的交付文本」）。

**逐字 vs 语义收束率对比（核心读数）**：对比被意外 1 污染，语义任务两臂的
A 都拿不到内联在任务书里的 12 条反馈原文（见下），无法形成干净的
「语义型应显著高于逐字型」判读。逐字型 lg 臂依旧 0/6：4 次 capability
短路（同构目录无升级目标）+ 1 contradiction + 1 budget-exhausted。

### r3 意外发现（本轮最大收获）

1. **worker 装配包只含 Lead 自拟的子任务标题，父任务内联材料不下发**
   （两臂共同的 P0 级缺口，此前被逐字任务的"标题即需求"掩盖）：
   semantic 任务里 12 条反馈原文只存在于 Lead 的决策 prompt，worker 包
   里没有；A worker 拒编数据（"请提供 12 条反馈原文"），Lead 验收正确地
   归因 context，但修 context 重试（无 memory 时补充材料为空）补不上，
   循环到轮次耗尽。lg-semantic 的 3 个 B 因此从未晋级。这是比交接包更
   上游的缺口，建议下一轮处理（包内容 = 子任务标题 + 父任务全文/材料段）。
2. **classic 臂 serial 的"收束"是验收盲区**：B 交付里 doctest 的 schema
   （id/name/email）与 A 实际交付（id/amount/currency/tags）不一致，
   占位常量标着 TODO 未替换——Lead 验收时同样看不到 A 的交付，逐字约束
   物理上不可核验，只能放行。classic 的 1/1 与 lg 的 0/6 不在同一
   可核验性水平上，不能直接当收束率对比读。
3. 语义任务里 Lead 曾两次"accept"A 的阻塞报告（内容实为"缺数据无法分析"
   的说明）——Lead 验收在材料缺失时不稳定，与意外 1 同源。

r3 产物：`results/summary-r3.json`、`runs-r3/`、`logs-r3/`、
`r3-progress.log`。

## r4 轮次（材料下发 + 验收可见性 + playbook 判型）

机制增量（阶段 1/2，两臂共享前两者、playbook 判型仅 lg 臂生效）：
- worker 装配包 = 子任务标题 + 父任务全文（`_worker_task_text`，内联材料
  随包下发，重试喂料同口径）；
- 有 deps 的 item 验收 prompt 携带上游 accepted 交付（逐字截断引用，
  `_upstream_evidence`）+ 验收规则明示"阻塞报告不得 accept"；
- playbook 资产（`dpswarm/data/handoff_playbook_v1.md`）：CM 判型
  verbatim/semantic（`handoff_profile` 事件加 `decider` 字段）、L1 优先级
  可配置覆盖、CM 压缩 prompt 注入 §1 结构指引。

### r4 结果摘要（四轮对照）

逐字型 serial：

| 轮次 | 机制 | classic（终局/tokens/墙钟/B 收束） | lg |
|---|---|---|---|
| r1 | 16384 固定，无交接 | 未收束 / 88,780 / 1744s / 0/6（缺输入） | 未收束 / 113,814 / 2611s / 0/6（摘要不逐字） |
| r2 | +角色下限/阶梯+三层 v1 | 未收束 / 134,469 / 2408s / 0/6 | 未收束 / 78,199 / 1261s / 0/5 |
| r3 | +Lead 面阶梯+L1 深化+规则判型 | **accepted** / 30,752 / 575s / "1/1"（验收盲区盲放） | 未收束 / 99,243 / 1573s / 0/6 |
| r4 | +材料下发+验收可见+playbook/CM 判型 | 未收束 / 154,358 / 2560s / 0/6（**知情拒绝**） | 未收束 / 146,362 / 2092s / 0/6（**知情打回**） |

语义型 semantic：

| 轮次 | classic（B 收束） | lg（B 收束） |
|---|---|---|
| r3 | 未收束 / 74,130 / 1374s / 1/5（材料缺口主导） | 未收束 / 52,715 / 940s / 0/3（B 从未晋级） |
| r4 | 未收束 / 68,529 / 1048s / **4/6** | 未收束 / 92,936 / 1237s / **4/6** |

### 三个关键读数

**a. 语义型两臂 B 收束率大幅抬升**：classic 1/5→4/6、lg 0/3→4/6——材料
下发修复生效（r3 里 A 因拿不到内联数据拒编、B 悬空；r4 里 A 正常归纳、
B 基于交接结论写报告被验收）。lg-semantic 的 4 个 B 包均带三层交接段
（semantic 型 L1 为空属正常：markdown 结论列表无 fenced/JSON/签名可提取）。

**b. classic 验收盲区消除**：r3 的 schema 不一致盲放 accept 在 r4 绝迹；
拒因变为逐字段 diff 级核验（"$schema draft-07 vs 2020-12""tags.type 丢失
["array","null"] 联合类型""擅自新增上游不存在的约束 id.maxLength=64"），
并显式引用验收规则拒绝阻塞报告（"按验收规则不得 accept"）。classic 的
0/6 是"正确拒绝"——经典臂没有交接机制，worker 拿不到上游交付，Lead 现在
能识别而非盲放。两臂读数从此可比：r4 的 classic 0/6 与 lg 0/6 都是
知情结论，差异在 lg 的 B 至少拿到了逐字材料（仍败于复制保真度）。

**c. lg 逐字型仍 0/6，decider 分布**：handoff_profile = verbatim×4
（decider=llm）+ verbatim×1（decider=rule-fallback——一次 CM 判型输出
非法，规则兜底在体内触发并正常工作）；lg-semantic = semantic×4（llm）。
判型全对。剩余失败 = 模型复制保真度（B 反复近似改写 schema 而非逐字
拷贝）+ capability 归因短路（同构目录无升级目标，5/6 的 escalated 走
`upgrade-exceeds-lead-level`）。四轮阶梯 0 触发（角色下限已消除截断）、
PULL 0 触发（L0 指引在包里但 B 未用——见意外 2）。

### r4 意外发现

1. **rule-fallback 体内触发**：lg-serial 一次 CM 判型输出非两值，规则
   兜底接管且事件落账（decider=rule-fallback）——降级路径真实可用。
2. **B 不用 PULL 回查原文**：verbatim 任务里 L0 指引（`PULL: wi-xxx`）
   在每份下游包中，但四轮 pull_served 恒 0——模型倾向凭 L1 记忆复述
   而非取原文直贴。逐字保真的最后一厘米可能需要更强引导（如 verbatim
   判型时在包里明示"禁止复述，用 PULL 取原文逐字直贴"）。
3. **收束纪律是 Lead 行为弱点**：4 个 run 全部 final=None（轮次耗尽），
   包括 B 已 4/6 accepted 的 semantic 两臂——done 列表已有足够 accepted
   项时 Lead 仍倾向再 fission 而非 accept。后续可考虑在决策 prompt 里
   加收束判据或在编排层做"全树 accepted 即自动收束"的硬规则。

r4 产物：`results/summary-r4.json`、`runs-r4/`、`logs-r4/`、
`r4-progress.log`。

## r5 轮次（三债修复：收束硬规则 / verbatim 直贴契约 / 归因改道）

机制增量：
- **债①收束硬规则**（父类 `_all_settled`/`_settled_close`/`_closure_warning`）：
  非 root item 全部终态 → 不再问 Lead，直接收束；有 accepted →
  accepted-by-lead，无 accepted → 上交（quota/rate-limit 耗尽原因透传）。
  连续 ≥2 轮扩张且有 accepted 交付时，决策 prompt 注入收束警示。
- **债②verbatim 直贴契约**：verbatim 判型后交接包明示「逐字内容禁止凭
  记忆复述，必须 PULL <item> 取原文直贴」。
- **债③归因改道**：同构目录 capability → 修述重试（`attribution_remapped`
  审计事件），不再 `upgrade-exceeds-lead-level` 直接上交；attempt 耗尽仍
  上交（§8 预算语义不变）。
- 任务书瘦身：两个串行任务均明示「拆且只拆 2 个 subtasks、验收后收束」。

### r5 结果摘要

| run | 终局 | tokens | 墙钟 | B 收束 |
|---|---|---|---|---|
| classic-serial | **accepted-by-lead** | 36,508 | 612s | 0/1（知情拒绝后上交） |
| lg-serial | **accepted-by-lead** | 36,488 | 486s | 0/1（PULL 体内首用，仍败） |
| classic-semantic | **accepted-by-lead** | 18,073 | 347s | 0/1（上交） |
| lg-semantic | **accepted-by-lead** | 66,311 | 1000s | **3/4** |

五轮对照（逐字型 serial；终局 / tokens / 墙钟 / B 收束）：

| 轮次 | classic | lg | 主导失败模式 |
|---|---|---|---|
| r1 | 未收束 / 88,780 / 1744s / 0-6 | 未收束 / 113,814 / 2611s / 0-6 | 缺上游输入（P0）；截断 |
| r2 | 未收束 / 134,469 / 2408s / 0-6 | 未收束 / 78,199 / 1261s / 0-5 | 摘要不逐字；capability 短路 |
| r3 | accepted / 30,752 / 575s / 盲区"1-1" | 未收束 / 99,243 / 1573s / 0-6 | 验收盲区盲放（classic）；材料缺失 |
| r4 | 未收束 / 154,358 / 2560s / 0-6 | 未收束 / 146,362 / 2092s / 0-6 | 知情打回，模型复制保真度；不收束 |
| r5 | accepted / 36,508 / 612s / 0-1 | accepted / 36,488 / 486s / 0-1 | 模型早停（stop=completed 但代码半途） |

语义型：r3 classic 1-5 / lg 0-3（材料缺口）→ r4 两臂 4-6（材料下发修复）
→ r5 classic 0-1 / lg 3-4，且全部正常收束。

### 三个关键读数

**a. 收束**：4/4 run final=accepted-by-lead（r4 全为 None）——收束硬规则
体内生效，`settled-autoclose` 落 actions。**语义注意**：serial 两臂是
"A accepted + B escalated"的部分成功收束（规则口径：存在 accepted 即收），
部分收束算成功还是失败属任务语义层，机制只保证不空转。

**b. 逐字型 lg 臂**：`pull_served`（source=upstream）体内首次触发——B 在
重试轮真的 PULL 了上游原文并自述「PULL 结果与 L1 层逐字一致」。B 仍未
accepted（0/1），但拒因质变：无 capability 短路、无材料缺失，两次打回
均为「代码在 `if schema.get(` 处截断」——**模型以 stop=completed 提前结束
生成**（非 max-tokens，阶梯抓不到），第三轮回避复述问题后仍内容不完整，
最终 contradiction 上交。剩余瓶颈 = flash 模型的长代码一次成型能力。

**c. 归因改道**：`attribution_remapped` 在 classic-serial 触发 1 次
（capability→description，homogeneous-catalog），改道后走真实修述重试
（attempt 照耗），capability 不再直接上交。lg-serial 本轮无 capability
归因（未触发改道），四轮以来 escalation 首次不再以 upgrade-exceeds 为主。

### r5 意外发现

1. **模型"静默截断"**：glm-5.3-flash 会以 stop_reason=completed 结束半成品
   代码（非 max-tokens），截断阶梯抓不到——只有内容级验收能抓。这是
   flash 级模型长代码场景的共性风险，后续可在编排层加"交付完整性自检"
   （如代码块闭合/函数体存在的确定性检查）作为提交前门。
2. 部分收束语义需决策：当前"存在 accepted 即收"会把 A 过 B 败的局封成
   accepted-by-lead；若产品语义要求全树 accepted 才算成，应把
   `_all_settled` 的 True 分支改为 all-accepted。

r5 产物：`results/summary-r5.json`、`runs-r5/`、`logs-r5/`、
`r5-progress.log`。

## r6 轮次（finalize 三值 + 集成验收 + 需求覆盖映射）

机制增量：
- **finalize 三值**（`_try_finalize`/`_finalize_result`）：success=全 accepted
  且集成验收过；partial=有 accepted 未全集 / 集成未过 Lead 收束；
  failed=无 accepted（耗尽原因透传）。`outcome["final"]` 路由值不变，
  新增 `outcome["result"]` + `run_finalized` 审计。
- **父任务级集成验收**（`_ask_lead_integration`，token 记 Lead 账、接截断
  阶梯 surface=lead-integration）：父任务全文 + 需求清单 + 全部 accepted
  交付逐字截断引用 → verdict {integrated, gaps}；fail 不直接封 partial，
  Lead 续决策一次（gaps 注入 prompt）。
- **需求覆盖映射**：`extract_requirements` 确定性提取顶格编号/bullet 行
  （缩进数据行不收）；`_dispatch` 检查 covers 并集，缺漏 →
  `coverage-rejected` 不建队 + `coverage_gap` 审计；**已有 accepted 交付的
  修复轮豁免**（见意外 1）。prompt 契约（routing.py）教 Lead 输出 covers。

### r6 结果摘要

r6 先跑出一轮带缺陷数据（已归档 runs-r6-pre/），体内暴露两个缺陷后修复
重跑；下表为修复后数据，serial 两臂为 30 分钟护栏 timeout-partial：

| run | 终局 | result | tokens | 墙钟 | B 收束 |
|---|---|---|---|---|---|
| classic-serial | timeout-partial | —（被杀时 1 accepted / 集成 fail×2） | 89,268 | >1800s | 0/1 |
| lg-serial | timeout-partial | —（被杀时 1 accepted / 集成 fail×2） | 82,890 | >1800s | 0/1 |
| classic-semantic | accepted-by-lead | **success** | 81,549 | 1761s | 2/2 |
| lg-semantic | accepted-by-lead | **success** | 26,153 | 394s | 1/1 |

六轮对照（逐字型 serial；终局/tokens/墙钟）：

| 轮次 | classic | lg | 本轮机制增量 |
|---|---|---|---|
| r1 | 未收束 / 88,780 / 1744s | 未收束 / 113,814 / 2611s | 基线（无交接，16384 截断） |
| r2 | 未收束 / 134,469 / 2408s | 未收束 / 78,199 / 1261s | 角色下限+阶梯+三层 v1 |
| r3 | accepted(盲区) / 30,752 / 575s | 未收束 / 99,243 / 1573s | Lead 面阶梯+L1 深化+判型 |
| r4 | 未收束 / 154,358 / 2560s | 未收束 / 146,362 / 2092s | 材料下发+验收可见+playbook |
| r5 | accepted / 36,508 / 612s | accepted / 36,488 / 486s | 收束硬规则+直贴契约+归因改道 |
| r6 | timeout-partial / 89,268 / >1800s | timeout-partial / 82,890 / >1800s | finalize 三值+集成验收+覆盖映射 |

语义型：r3 1/5、0/3（材料缺口）→ r4 4/6、4/6（材料下发）→ r5 0/1、3/4
→ **r6 两臂 success（2/2、1/1，集成验收 pass）**。

### 三个关键读数

**a. result 三值分布**：semantic 两臂 success（全 accepted + 集成 pass）；
serial 两臂 timeout-partial。r5 的"A 过 B 败却 accepted-by-lead"语义已由
partial 接管——r6-pre（修复前代码、同机制）classic-serial 即为
result=partial（1/2 accepted + 集成 fail），r6 里 serial 两臂被杀时也是
"1 accepted + 集成 fail×2"的 partial 形态。

**b. 集成验收真实触发且 gaps 有实质**：经典实例（r6-pre classic-serial）——
「需求1（校验器实现）无任何已验收交付：已验收清单仅含 schema，缺少
validate_record 实现代码」「3 个 doctest 用例缺失」「跨交付一致性核对因
B 缺席而无证据支撑」——逐需求核对，不是摆设。反例：lg-serial r6 一次
集成验收模型输出了结构化 markdown 而非 JSON（`unparseable` → 保守 fail，
行为正确但暴露 flash 对集成验收输出协议的遵循弱点）。

**c. coverage 拒绝体内触发 4 次（r6-pre）→ 0 次（修复后）**：r6-pre 里
Lead 在 B 失败后的修复轮只想重派 B（covers=[1]），被全覆盖门槛连拒 4 次
直至轮次耗尽——覆盖门对"初始分解"是纪律、对"修复轮"是误伤，已修复为
「存在 accepted 交付时豁免」（有测试锚定）。修复后两轮 0 触发且语义任务
不再被拒（需求提取改为顶格行，12 条内联反馈不再被误当要求）。

### 变慢归因（logs 证据）

逐字两臂 r5 的 ~10 分钟 → r6 超 30 分钟被杀，主因是**轮次增多 × thinking
单调用延迟**，不是通道波动：
- 调用轮次：classic-serial r5 9 次 → r6 19 次（集成验收 +2、集成 fail 后
  修复轮、每轮 Lead 决策+worker+验收链）；
- 单调用均值 r5 68s → r6 83s（含上游交付引用的验收/集成 prompt 更长，
  thinking 更久）；wall ≈ 轮次 × 均值，两项相乘即解释 612s → >1800s；
- 对照组证伪"变慢是普遍退化"：lg-semantic r6 反而 394s/26K（r5 是
  1000s/66K）——语义任务一次成对收束，无修复轮，新机制净省时。
- 结论：机制让逐字任务"正确地不收敛"（B 不可验收 → 修复轮），成本是
  thinking 模型每轮 ~80s 的线性放大；30 分钟护栏口径对修复循环偏紧。

### r6 意外发现

1. 覆盖门修复轮误伤（上述 c）与需求提取误收数据行（缩进编号）——都是
   体内发现、当日修复、测试锚定。
2. 集成验收的输出协议（裸 JSON）flash 遵循不稳：markdown 分析写得很好但
   不是 JSON → 保守 fail。可考虑协议容错（从 markdown 提取结论）或工具
   化输出。
3. `test_artifact_board_20260909.py` 在全量回归中间歇 flake（端口争用，
   单跑必过，与本改动无关）——既有现象，如实记录。

r6 产物：`results/summary-r6.json`、`runs-r6/`、`logs-r6/`、
`r6-progress.log`；缺陷轮归档 `runs-r6-pre/`、`logs-r6-pre/`、
`results/summary-r6-pre.json`、`r6-progress-pre.log`。

## r7 轮次（①-b 任务级交付协议：三维度 + 需求登记 + 候选装配）

机制增量（①-b，评审契约逐条落实）：
- outcome 脱离 item 计数：success = 全部 mandatory 需求在最终候选交付上
  逐需求验证通过（探索性失败分支不否决）；partial = 经验证可独立交付的
  子集 + 缺口清单；failed = 无契约认可交付；unknown 阻止 success ≠ fail。
- 三维度分存：phase(running/finalizing/finalized) + outcome + stop_reason
  （completed/budget_exhausted/attempts_exhausted/cancelled/quota_exhausted/
  rate_limit_exhausted），run_finalized 与 outcome JSON 同步承载；
  final 路由值保留兼容。
- 需求登记表：{requirement_id, origin_ref, contract_version, mandatory,
  assigned_to, verification_rule}；登记完整性由集成验收拿原文复核；
  mandatory 覆盖只增不减（违规记 coverage_gap 并拒绝）。
- 候选交付装配 {candidate_id, contract_version,
  selected_submission_package_ids, artifact_manifest, bundle_digest} +
  封存核对（验 A 封 B 漂移剔除降级）+ finalize 幂等（重复调用同逻辑结果）。
- 验收材料保真：取消 submission[:2000] 硬截断作唯一材料；超 12000 字符给
  L1 逐字段 + 全文 ref +「材料缺失处不得臆断，记 unknown」（atomic.py
  中立化，验收/集成验收/交接包共用同一份确定性提取）。
- 归因分维：uncertain（证据不足→修 context）/ invalid_output（Lead 输出
  非法→修描述），不再伪装成 description。

### r7 结果摘要

| run | 终局 | result | tokens | 墙钟 | B 收束 |
|---|---|---|---|---|---|
| classic-serial | timeout-partial（30min 护栏） | — | 86,210 | >1800s | 0/2 |
| lg-serial | timeout-partial | —（被杀时 B 已有 1 accepted） | 108,071 | >1800s | **1/2** |
| classic-semantic | accepted-by-lead | **success** | 21,886 | 347s | 1/1 |
| lg-semantic | accepted-by-lead | **success** | 33,731 | 501s | 1/1 |

**读数 a（三维度分布）**：semantic 两臂完整落
`phase=finalized + outcome=success + stop_reason=completed`，
run_finalized 带 candidate_id + bundle_digest（候选装配体内可见）。
lg-semantic 过程里 Lead 首轮漏 covers 被 `coverage-rejected:missing=[0]`
结构化拒绝、次轮补齐后正常 dispatch——覆盖门体内触发且恢复正确。
serial 两臂仍未在 30 分钟内收束（逐字任务 + 修复轮 × thinking 延迟，
同 r6 归因）。

**读数 b（unknown/uncertain/invalid_output 频次）**：uncertain=0、
invalid_output=0（逐需求验收拒绝归因全部落在 context/description 正类，
解析维度本季度无需触发）；unknown 在 classic-serial 出现：集成验收
输出 markdown 而非 JSON → 保守 fail（逐需求 req-0/req-1 均记 fail），
与 r6 同型的 flash 协议遵循弱点。

**读数 c（8 条反例的真实可见性）**：测试全锚定（test_finalize_protocol.py
8 例）；真实 run 里可见的：① unknown 阻止 success（classic-serial 集成
fail 未封 success）、③ 语义两臂全 mandatory 过 → success、⑤ stop_reason
分布（serial 两臂 timeout-partial，未触发 budget_exhausted 终局）、
⑥ 长交付保真（lg-serial 多份 B 交付超 2000 字符被完整核验，
拒因引用 doctest 级细节）、覆盖门恢复（lg-semantic）。
⑦ 验 A 封 B 与 ⑧ 幂等属不变量守卫，真实 run 未触发（属正确沉默）。

**亮点**：lg-serial 的 B 首次在逐字任务被验收（1/2）；且集成验收对
**父任务原文**做了登记完整性复核——gaps 实例：「req-0：schema 含原文
未要求的第五字段 items……按原文四字段构造的合法记录无法通过该校验，
须删除后重新交付」（拿原文对清单，不只看 Lead 清单）。

r7 产物：`results/summary-r7.json`、`runs-r7/`、`logs-r7/`、
`r7-progress.log`。

## r8 轮次（Open 项 O1-O6 决定版 + 六场景复验）

机制增量（评审决定版，逐条见 dpswarm 侧）：
- O1 终局入口统一证据判定（`_close_from_evidence`：异常路径不调 LLM，
  复用持久化验证证据；证据不足 → failed + 真实 stop_reason）；
- O2 拒绝空成功：single/直接交付也过集成验收门（两臂 `_lg_single` 同修），
  零有效交付发 accept → failed；
- O3 清单外 finding 处置流（`finding_disposition` 三分支 +
  `contract_amended` revision 递增 + 修正预算 ≤2/run）；
- O4 Reviewer 回查通道（`_review_fetch` + 验收面 FETCH 协议：范围读、
  版本核对不静默 latest、越权/失配/预算不足 → unknown 指引）；
- O5 启发式边界不静默（heuristic 标记项 mandatory 进登记表、集成验收
  兜底 verdict；**不参与派发覆盖门**——r8 体内发现冲突并当日修复）；
- O6 原地弱化集中校验（`_check_registry_integrity`，每需求记一次）。

### r8 六场景读数（glm-5.3-flash 全角色同构）

| run（场景） | 终局 | result | tokens | 墙钟 |
|---|---|---|---|---|
| lg-direct（合法直接交付） | single | **success** | 2,080 | 29s |
| lg-hiddenreq（清单外原文要求） | accepted-by-lead | **success** | 22,062 | 345s |
| lg-semantic（正常交付） | accepted-by-lead | failed（见注意①） | 44,123 | 1027s |
| lg-longdoc（预览外缺陷） | timeout-partial | —（被杀时 B 未晋级） | — | >1800s |
| classic-semantic（正常/对照） | timeout-partial | —（被杀时 B 2/3） | 74,341 | >1800s |
| lg-serial（异常收尾/修复循环） | timeout-partial | — | — | >1800s |

六场景行为核对：
1. **合法直接交付 → success**（lg-direct）：single 过集成门，O2 生效；
   run_finalized 带 contract_version + candidate。
2. **预览外缺陷**：lg-longdoc 体内 `review_fetch` ×3——Reviewer 真用回查
   通道按范围取手册原文（A 的 ≥13000 字符手册生成过长 + 修复循环触
   30min 护栏，未收束）。通道在体内被真实使用即本场景读数。
3. **清单外原文要求**：lg-hiddenreq success，但 finding 流程未触发
   （reviewer 未把署名行报为 finding，交付本身满足）——机制就位、
   体内未被迫触发，如实记录。
4. **异常收尾**：lg-serial/classic-semantic 30min 护栏 timeout-partial
   （逐字/长文档任务 + 修复循环 × thinking 延迟，同 r6/r7 归因）；
   lg-serial 可见 `invalid_output` 归因分维体内出现（⑥）。
5. **注意①（诚实记录）**：lg-semantic 的 failed 不是交付质量问题——
   集成验收调用返回空文本（传输层空回复），协议层保守 fail →
   全需求 fail → Lead 收束 → failed + 候选照常装配。护栏行为正确，
   暴露的是 flash 偶发空回复这一传输事实。
6. **注意②（过程事故，如实记录）**：r8 前两轮因环境里有另一批
   `r8g` 实验并发跑同账号（run_r8g_glm.sh / runs-r8g/）+ 我的进程被
   杀残留，runs-r8 曾被污染清空；本轮（第四轮）为干净重跑，此前两轮
   的可用观察已并入上文读数（lg-direct 首轮 failed 是 `_lg_single`
   未过集成门的 bug，已修并复验为 success）。

r8 产物：`results/summary-r8.json`、`runs-r8/`、`logs-r8/`、
`r8-progress.log`。


---

## r8g 轮次（2026-09-11）：任务级交付协议的机制对照（GLM-5.3-Flash）

**目的**：r6/r7 之后的机制债清理（O1-O6 决定版）在真实模型下的行为核验，
模型与 r1-r7 一致（glm-5.3-flash 全角色），因此可直接与 r7 对照。

**执行**：6 run（lg-direct / lg-hiddenreq / lg-longdoc / lg-semantic /
classic-semantic / lg-serial），tag `r8g`，4 路并发（并发上限经实测：GLM 4 路
并发 10.6-13.6s 全部成功、无 429）。伴生一轮 DeepSeek Flash 跨模型泛化臂，
因用户指示取消，产物归档 `_abandoned-deepseek/`，不参与本小节结论。

### 终局分布（三值语义 + 停止原因）

| run | final | result | stop_reason | 墙钟 | 集成验收 |
|---|---|---|---|---|---|
| lg-direct | single | **success** | completed | 44s | pass |
| lg-hiddenreq | accepted-by-lead | **success** | completed | 620s | pass |
| lg-semantic | accepted-by-lead | **success** | completed | 943s | pass |
| lg-serial | turn-exhausted* | **partial** | attempts_exhausted | 3725s | evidence-partial |
| classic-semantic | turn-exhausted* | **failed** | attempts_exhausted | 2185s | insufficient-evidence |
| lg-longdoc | aborted | **failed** | budget_exhausted | 3883s | —（预算中止） |

\* 这两条 final 为空是收口路径缺路由标签所致，验收中已修（见下），记录保留原样。

**核心读数**：

1. **O2 正向边界成立**：`lg-direct` 零子任务、Lead 直接交付 → `success`
   （成功不靠 item 计数）。
2. **O1 负样本成立（本轮最有价值证据）**：`classic-semantic` 6 个 item
   **全部 accepted**，集成验收连续 3 次判 insufficient-evidence → `failed`
   + `stop_reason=attempts_exhausted`。局部全过 ≠ 整体交付成立，机制拒报 success。
3. **partial 语义成立**：`lg-serial` 10 个 item 中 2 个有验证证据 →
   `partial` + 缺口清单（非"存在 accepted 即 partial"）。
4. **O4 回查通道体内触发 3 次**（lg-longdoc ×2、lg-serial ×1）——
   含集成验收路径（验收中修的那处接线）。
5. **其它机制体内生效**：`attribution_remapped` ×6（同构目录 capability
   不再直接上交）、`max_tokens_escalated` ×2（截断阶梯真升档）、
   `handoff_profile` ×4、`pull_served` ×2（verbatim 直贴契约起效）、
   `integration_review` ×6。
6. **预算护栏生效**：lg-longdoc 烧 156K lead token（65 分钟、反复打回）后
   撞 200K run 级护栏中止。

### 验收中发现并修复的两个缺口

| 缺口 | 修法 | 证据 |
|---|---|---|
| 预算护栏中止不产出终局记录（final/result 双 null，等于异常收尾无结论） | `compare.py:run_one` 捕获 BudgetExceeded → `final=aborted`/`result=failed`/`stop_reason=budget_exhausted`；不按 accepted 计数推 partial | lg-longdoc 记录 |
| 轮次耗尽收口路径 `final` 路由标签为 null（消费者读到"无结论"） | `_close_dimensions` 对未设置 final 的路径兜底 `turn-exhausted`/`aborted`，不覆盖已有语义 | classic-semantic / lg-serial 记录 + `tests/test_finalize_coherence.py`（2 例） |

回归：662 passed + 1 skipped。

### 未在本轮触发的路径（如实记录）

`coverage_gap` / `finding_disposition` / `contract_amended` 在本轮场景集下
未触发（其语义由 r6 的覆盖门误伤轮与单测覆盖）；⑦验 A 封 B、⑧finalize
幂等属不变量守卫，未触发即正确沉默。

**O5 未处理标记（req-extra）反向确证**：真实 run 共出现 34 次
（classic-semantic / lg-hiddenreq / lg-longdoc），且**不是静默放过**——
lg-hiddenreq 的集成验收对 `req-extra-2` 单独给 verdict，lg-semantic 对
`req-extra-2/3/4` 逐条 verdict，全部 pass 才允许 success。即"启发式覆盖不完整"
被显式登记为 mandatory 需求并逐条核验，符合 O5 决定版。

### 产物

`results/summary-r8g.json`、`runs-r8g/`、`logs-r8g/`、`r8g-progress.log`、
`run_r8g_glm.sh`、`run_r8g_batch2.sh`。
