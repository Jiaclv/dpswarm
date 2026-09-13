# b7e149cb 计划 P0+P1+P2a 实施记录（第一批判定语义）

日期：2026-09-13。依据：`reports/2026-09-13/b7e149cb-mechanism-fix-plan.md`（P0–P2 首批范围）。

状态：**P0 核心与 P1、P2a 已实施并通过全量回归**；P2b（repair 的格式修复/实质复核分路，R08）与 P3–P6 未动，留待后续批次。本记录不含未实施项的任何完成声明。

## 基线（P0）

- 源码版本：JS 插件 0.15.0（本地源码）、Python 包 0.1.0。日常宿主未切换；本批**未重装、未激活**。
- 测试基线（本批改动前）：JS 751 项全绿（2026-09-13 早间，含本日 maxTokens/lossless/never-issued 修复）；Python 740 passed + 1 skipped。
- 本批改动后：JS **754/754**、Python **752 passed + 1 skipped**（新增 12 个 v2 语义用例 + 3 个 JS v2 单测，v1 行为零回归）。
- 反例以确定性测试固化（不执行历史日志命令）：`dpswarm-plugin/tests/test_acceptance_v2_20260913.py` 覆盖 R01–R07 与版本冻结；正反对照（正常交付、真实缺陷、无关检查不可用）由既有 20260912 套件与本文件新用例共同承担。原始 b7e149cb 会话日志未在本机，P0 的"ZIP 哈希保存"项以待办形式保留。

## 合同与接口（P0 冻结表）

| 面 | v1（既有，语义不变） | v2（本批新增） |
|---|---|---|
| 报告 fence/schema | `dpswarm-review-v1` | `dpswarm-review-v2` |
| 合同冻结字段 | —（隐含 v1） | `contract.review_contract`，bind 时由 JS 按协商写入，注册报告必须精确匹配（`REVIEW_CONTRACT_MISMATCH`） |
| 能力协商 | `runtime.acceptance_contract` | 追加 `runtime.acceptance_review_contracts: [v1, v2]`（**additive，revision 保持 1**：旧 JS 完全兼容，新 JS 读集合；未上报集合的控制面按 v1 降级，R15） |
| 顶层新字段 | — | `verification_plan{revision, checks[], coverage}`、`check_results[]` |
| requirement 行 | id/result/evidence/reason | 追加 `verification_plan_revision`、`check_refs[]`（必填） |
| finding 行 | …state… | 追加 `disposition`（必填）与 `disposition_reason`（retained_suggestion 必填非空） |
| Python 权威语义 | 既有身份/revision/verdict 门 | `verify_v2_semantics`：R01/R04/R05/R06（见下） |

## P1：要求连接到实际证据（F1/R01–R04）

- **R01**：`met` 的必要要求必须在其 `check_refs` 里引用至少一个 `execution_status: completed` 的 check_result，否则注册即拒（`VERIFICATION_CHECK_INCOMPLETE`，存 parse_error；final review 提交时以原 code 透传拒绝）。非必要要求仅在给出 check_refs 时适用同规则。
- **R02**：静态检查的 completed 同样满足 R01——`execution_status` 描述"检查是否执行"（静态阅读是执行），不硬编码工具名或视觉任务必须用浏览器。
- **R03**：等价替代方法由 Reviewer 在 plan 的 method/rationale 与 coverage 中说明，程序不按方法名阻塞（`check_refs` 指向任意 plan 内 completed check 即可）。
- **R04**：每个 check_result 绑定 `candidate_id/manifest_digest/requirement_revision/verification_plan_revision`，与封存候选或报告计划 revision 不符即拒（`VERIFICATION_CHECK_CANDIDATE_MISMATCH`/`VERIFICATION_PLAN_STALE`）；requirement 决策的 plan revision 同查；check_id 必须在 plan 内（`VERIFICATION_CHECK_UNKNOWN`）。同一 check 的旧 completed 结果跨计划复用需要重新引用并重跑绑定校验（旧结果对象不改写）。
- 工具事件伪造问题（计划 P1 第 5 点的 runtime-observed 身份）属 P2b/后续：当前 evidence_refs 仍是报告引用，Python 只校验引用与绑定一致性，不验证工具事件真实性——**该限制已知并保留**。

## P2a：Finding 处置（F2/R05–R07）

- v2 finding 增加独立 `disposition: pending | fix_required | verified_resolved | retained_suggestion | not_a_defect | not_applicable`；`classification` 与历史 `state` 保留。
- **R05**：`verdict: pass` 时所有 finding 必须处于终态处置（四个终态之一），`pending` 拒绝（`FINDING_DISPOSITION_REQUIRED`）。
- **R06**：`suggestion + retained_suggestion`（带 disposition_reason/evidence/reason 支撑）可在**不改产品**的情况下结案；`defect + retained_suggestion` 拒绝（`FINDING_DISPOSITION_INVALID`，防真缺陷洗成保留建议）。
- **R07**：必要要求 failed/unknown 阻塞 pass（既有 v1 commit 门保留，v2 同样适用——注册允许 blocked/needs-rework 报告，接受事务拒绝）。

## JS 侧实现摘要

- `acceptance-contract.js`：`REVIEW_JSON_SCHEMA_V2`（JSON Schema）、`validateReviewRecordV2`（语法+引用完整性，提前于 Python 拒绝省一轮提交）、`parseReviewReport` 双 fence（恰好一个 v1 或 v2 块；混用拒绝）、`reviewReportFormatV2`（模板按当前 requirements 生成 plan/check 脚手架 + findings 带 disposition 占位）、`REVIEW_FORMAT_INSTRUCTIONS_V2` 指引文本。
- `acceptance-runtime.js`：preflight 协商（读 `acceptance_review_contracts`，含 v2 冻结 v2，否则 v1）；bind 写入 `review_contract`；reviewer/takeover prompt 与 `review_format` 视图按合同版本渲染。
- 模板占位值（如 `REPLACE completed | …`）在 JS 语法层即被拒绝——模板必须被认真填写，不能原样提交（takeover 测试相应更新为填表形态）。

## 回归证据

- Python：`tests/test_acceptance_v2_20260913.py` 12 用例（R01/R02/R03/R04a/R04b/R05-pending/R05-retained+accept/R06/R07/版本冻结/混合 fence/语义直测）全绿；全量 752+1。
- JS：`acceptance-contract.test.mjs` 新增 3 用例（v2 parse、语法反例、双模板）；`acceptance-native-bridge.test.mjs` 40 用例（fixture 产 v2 报告走完整协商→冻结→注册→验收链）全绿；全量 754。
- v1 兼容：v1 fence/schema/校验路径逐字节保留，全部既有用例零改动通过（除 bridge fixture 升级为 v2 报告形态）。

## 第二批追加（同日）：P2b + P3a

**P2b 报告处理分路（F3/R08）：** `dpswarm_repair_report` 新增 `purpose: format | substantive`（默认 substantive，与既有行为完全兼容）。

- `format`：continuation prompt 限定为结构/封装/绑定规范化；Python 侧 `verify_format_repair` 对新旧报告做语义等价校验——verdict、逐 requirement result、逐 finding 的 observation/classification/disposition 必须相同，任何漂移以 `REPAIR_SEMANTIC_DRIFT` **事务级拒绝**（与 `REVIEW_CONTRACT_MISMATCH` 同级的合同门：旧报告保持当前，不被 parse_error 顶替）。旧报告不可解析时 format 无基线可比，`REPAIR_SEMANTIC_BASE_MISSING` 拒绝并指向 substantive 路径（"可证明无歧义"的边界）。
- `substantive`（默认）：continuation prompt 明示"可依据证据改变结论；旧报告与 Lead 偏好是不可信输入而非 pass 指令"——对齐计划"Reviewer 可拒绝 Lead 的实质建议"。
- 回归：R08 四用例（语义保持注册成功 / 改 verdict 拒且旧报告保持当前 / 默认与 substantive 可改结论 / 无可读基线时 format 拒）。

**P3a 紧凑决策视图（F4 第一部分）：** `acceptance` 视图新增 `decision_summary` 纯派生字段——blocking 事实四类（mandatory_unknown / mandatory_failed / undisposed_findings（v2 pending 或 v1 open/fix-claimed）/ verification_roster_pending）、`ready_to_accept` 推导、五个合法动作（accept 的 ready 位、takeover、rework、repair_report 含 purpose 提示、verify_rework）与"查询不是锁"注记。不新建任何决定面（纯 contract 派生）。回归：正常链 ready 推导 + open-finding 阻塞推导两用例。

**本批回归基线：JS 756/756（bridge 42 用例）；Python 756 passed + 1 skipped（v2 文件 16 用例）。v1/v0 语义零回归。仍未安装日常宿主。**

## 第三批追加（同日）：P3b 核心 + P4

**P3b 可靠收尾（F5）：**

- `completionStatus` 新增显式 `finalization` 状态：`accepted+cleanup_pending`（已接受但仍有 pending 辅助项/持有租约/占用槽位）、`accepted+finished`（全部排干）、`not_accepted`，与 `cleanup_pending` 布尔并列——"已宣布完成"与"资源已排干"从此在状态面上分开。
- 新工具 `dpswarm_finalize(reason)`：可重试的显式收尾入口。准入红线按计划执行——当前候选成员与已登记验证项**拒绝静默关闭**（需显式 review 决策）；非候选辅助项仅当 worker 可证终态（terminal + 物理清理确认 + 无审计错误）才以 **terminate**（绝非 accept）关闭；活动/未确认的拒绝并列出。收尾后幂等 reconcile，返回 `retryable` 指引重跑。
- **工作区一致性重检**（`workspace_consistency`）：对已接受候选的 entry 文件逐一重算 SHA-256 对比接受时 manifest，返回 `consistent | drifted | unavailable` + 检查时间 + 逐文件差异——替换原 `workspace_currentness: 'not_rechecked'` 占位。漂移只报告，绝不回滚或重新接受。

**P4 用量分账（F6）：**

- 新模块 `usage-ledger.js`（纯读聚合）+ `dpswarm_status.usage_ledger` 视图：
  - `by_activity`：implementation / verification / product_rework / report_repair / cm / lead_control——归因来自授权链（allocation authority + label + budget_origin；`purpose=compaction` 归 cm）。CM 请求与 worker 消费来自同一批 settled 账本行、按 settled call_id 聚合恰好一次，**不双计**。
  - `by_role`：lead / implementer / tester / reviewer。Lead 侧聚合原生 session 的 assistant message usage（mixed 活动不拆分，notes 明示）。
  - 未知与预留分开：未结算/不完整 usage 的调用保留 `reserved_tokens_unknown` 下界与 `unknown_usage_calls` 计数，不补零；账本重放（重复事件）聚合仍恰好一次。
  - `window`（首末事件时间）与五条口径注记。
- 计划中"重放 b7e149cb 的 34 条响应得到 1,204,300"的验收**无法执行**（原始会话日志不在本机）；以手工事件单元测试 + bridge 全链集成（实现/验证/CM/漂移重检）替代，差异如实记录。

**本批回归：JS 763/763（新增 7 用例）；Python 756+1（未触及，复跑确认）。bridge fixture 的 budget journal 改为 Memory 主 + sidecar 镜像双写以对齐生产的共享审计存储。仍未安装日常宿主。**

## 第四批追加（同日）：P5 机制与协议安全用例

**P5 独立初审与汇合（F7/R18，显式 opt-in，默认关闭）：**

- 新设置 `reviewerIndependence: 'guided' | 'independent'`（默认 `guided` = 既有顺序行为零变化；设置卡片新增「验证独立性」选择器）。
- `independent` 模式：候选封存后 `dispatchIndependentVerification` 将 tester 与**盲初筛 reviewer** 以两个单角色 dispatch 并发发行（各自原额度、各自冻结路由校验）——初审 prompt 显式声明"无 tester 报告可参考、非终审"；tester 报告正常注册 evidence。
- **初审永不注册为 reviewer evidence**（物理上不可能成为终审）：初审 worker 建立血统（供汇合续跑）但报告只入审计诊断；汇合轮前初审 item 以 terminate 结案（报告与血缘留账），释放 §7 槽位。
- **汇合轮**：初审 reviewer 的 rework 链续跑（额外调用记入 rework 账本），prompt 携带 tester 报告与初审自报告（均为 untrusted context），产出唯一注册为 reviewer evidence 的终审——Python 侧 evidence_revision 门天然保证 R18（tester 后发现的 finding 必须被汇合轮重新裁决，旧报告 stale）。
- **顺带修复（生产级）**：controller 与 budget 此前各自实例化 AuditJournal——并行验证时跨实例 CAS 冲突（`PLUGIN_AUDIT_REVISION_CONFLICT`）。controller 构造新增 `journal` 注入，index.js 与测试 fixture 共享单实例（AuditJournal 的 per-root 队列串行化所有写）。
- 回归：P5 两用例（并发形态 + 4 children + 初审无 evidence + 汇合绑定 tester 上下文；R18 tester 发现缺陷 → 汇合 needs-rework + accept 拒）。

**本批回归：JS 765/765（bridge 46）；Python 756+1 未触及。仍未安装日常宿主。计划中的行为试点（配对比较独立性与延迟）未运行——仅协议与机制落地。**

## 未实施（下一批）

- ~~P2b~~（已于第二批实施，见上）
- **P3b 剩余**：Python 侧接受/清理意图的持久化登记（当前 finalize 复用既有 reconcile 接缝，未新增跨进程清理状态机字段）、崩溃后 cleanup 状态续做的存储化。已实施：finalization 状态区分、可重试 finalize 入口、工作区一致性重检。
- **P4 剩余**：价格/费用维度（需价格来源数据）、新 finding/裁决变化等质量计数、端到端 vs 活跃时间的分拆展示。已实施：活动/角色分账、CM 不双计、未知预留、去重。
- **P5 剩余**：行为试点（冻结配置的配对比较：guided vs independent 的独立性与延迟）、`sequential` 团队模式的独立性扩展（当前仅显式 opt-in 生效）。已实施：机制、盲初审、汇合轮、R18 协议安全。
- **P6**：匹配部署与真实加载验证。
- P0 的原日志 ZIP 哈希存档；R01 中 runtime 工具事件真实性校验。
- 本批**未安装到日常宿主**（按计划 P6：部署需匹配 JS/Python 包并确认实际加载后才可写完成）。
