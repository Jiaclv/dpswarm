# DPswarm 构建与验收流程修复计划

日期：2026-09-13。依据：`b7e149cb-5885-4661-8285-76f080a3b9d5` 会话及四个子会话、当前源码、2026-09-12 验收修复与 harness 计划。

状态：**计划已编写，以下改动待实施。** 本次只写计划并检查其可实施性，未修改运行代码、机制主文档、模型配置或日常宿主。阶段完成状态不能由本文推断。

目标：让验证结果准确反映实际证据；让可选建议得到明确处置而不制造无关产品返工；减少 Lead 操作验收协议的负担。保留已有身份、版本、预算与恢复约束，最后再评估并行和角色配置是否有净收益。

## 1. 本次增量与已有工作的关系

当前源码 `dpswarm-dsh-plugin/package.json` 为 **0.15.0**，Python package 为 0.1.0。本轮未认证日常宿主实际加载版本；源码版本、历史运行版本与安装目录名称不得混用。工作区有大量已有修改和未跟踪文件，实施前要按文件冻结，不能用清空工作区或回退全部变更的方式建立基线。

已存在的能力继续复用：可信用户正文与 Lead 方案分离、候选快照、稳定 finding、requirement/evidence/review revision、Python 权威验收、report-only repair、用户 amendment、staged 消费版本绑定、预算结算和清理诊断。

相关历史材料：

- [已实施的验收修复](K:/秋招/项目/DPswarm/reports/2026-09-12/acceptance-repair-implementation.md)：作为能力与历史证据索引，不把旧测试数字当本轮基线。
- [原验收计划](K:/秋招/项目/DPswarm/reports/2026-09-12/acceptance-repair-plan.md)：保留其来源、候选、发现和原子验收原则。
- [原计划审查](K:/秋招/项目/DPswarm/reports/2026-09-12/acceptance-repair-plan-review.md)：需求接续、依赖快照和 revision 自失效等反例继续回归。
- [此前 harness 提案](K:/秋招/项目/DPswarm/reports/2026-09-12/abb8d6ec-agent-harness-plan.md)：职责一致、事实复用与最终表达部分在本计划中细化；其预算预测等问题不能仅因被旧文列出就认定今天仍未修复。
- [本次审查及日志索引](D:/codex-home/visualizations/2026/09/13/01a098d7-f55f-7063-8831-6a2bf737013f/dsh-session-audit/审查证据与机制分析.md)。

## 2. 已观察问题与修复落点

| 编号 | 本次证据 | 修复内容 | 成功判据 |
|---|---|---|---|
| F1 | 未执行浏览器播放检查，但全部要求 met；acceptance 文本与结构化要求覆盖不一致 | 要求、检查计划、实际证据和未知项关联 | 缺少已选必要检查的有效证据不能完整通过；无关检查不可用不阻塞 |
| F2 | suggestion/open 导致拒收；随后只改 not-a-defect | 把发现分类与处置分开 | 有依据的非阻塞建议可保留；未裁决问题和必要要求失败仍阻塞 |
| F3 | Lead 自写报告被拒，后又指定 Reviewer 保持 pass 并改状态 | 格式修复与实质复核分路 | 格式处理不改变语义；Reviewer 可以拒绝 Lead 的实质建议 |
| F4 | Lead 11 次验收查询、两次失败接受；Implementer 调错 staged 工具 | 紧凑决策视图、职责一致、工具适用性反馈 | 当前可行动作明确，典型路径不靠试错摸清协议 |
| F5 | 首次宣布完成后尚有两个 submitted 项和持有中的租约 | 可重试、可恢复的统一收尾 | 已采用报告可正常结案，资源释放明确；不提前释放活动任务资源 |
| F6 | worker 汇总 454,154，含 Lead 的可观测总量 1,204,300 | 全任务用量与流程活动分账 | Lead/worker/CM 不漏记不双记，未知与缓存价格区别保留 |
| F7 | 同模型、同类静态检查、全部串行 | 先验证能力互补，再试验独立初审与汇合 | 新检查补足不同证据；最终裁决仍覆盖完整最新证据 |

F7 属后续优化，不作为 F1–F6 修复的前置条件。本次不默认切换模型、跳过用户配置的 Tester/Reviewer、改变并行设置或收紧用户额度；自主拓扑另按可选策略评估。

## 3. 实施中必须守住的约束

1. 用户原文、Lead 推导、实现偏好分别保留来源。Lead 可自行选择合理实现和验证方法；不能把自己的偏好写成用户硬约束。缩减用户必要要求仍走已有可信 amendment。
2. 控制面校验来源、版本、记录和状态；方法能否证明需求、缺陷是否相关仍需要语义判断。增加结构字段不能被宣传成解决所有误判。
3. 候选哈希、必要依赖、staged 消费引用、执行身份和所有 revision 检查继续生效。可读、可恢复的诊断不能因接受失败一并关闭。
4. 已知 finding 不删除、不靠换 ID 消失。建议不自动放行；它必须有当前候选上的明确裁决。必要要求 failed/unknown 不能被“非阻塞”标签覆盖。
5. 保留原角色、精确模型路线、effort、权限、已冻结预算及 grant 血缘。增加阶段不能带来隐藏免费调用、重置预算或挪用其他角色额度。
6. 静态推导、实际执行、渲染捕获与人/模型视觉观察分别描述。截图存在不等于已被检查，工具成功不等于需求正确。
7. 不给通用调度器写死 SVG 坐标、必须用 Chrome、固定检查次数或固定返工上限。能力不可用要有当前上下文证据，一次失败不等于永久不可用。
8. 计划、协议测试、真实模型行为、安装激活分别报告；当前样例是已知案例，不充当盲测或质量提升率。

## 4. 工作包与依赖

| 阶段 | 工作包 | 依赖 | 完成定义 |
|---|---|---|---|
| P0 | 冻结基线、反例、v2 合同及兼容矩阵 | 无 | 有可复现夹具、文件版本清单、已知失败清单及接口表 |
| P1 | 要求与证据关联 | P0 | 必要检查缺失不再被结构上标为充分；替代证据有据可查 |
| P2 | Finding 处置、格式修复与实质复核 | P0；与 P1 汇合后放行 | 建议不用改名为非缺陷才能结案；实质判断可被质疑 |
| P3 | 紧凑决策视图、职责与可靠收尾 | P1 + P2 | 常见路径无需试错或重写终审，完成状态和资源状态一致可恢复 |
| P4 | 端到端用量与活动分账 | P0，可与 P1/P2 并行 | 主代理成本可见，协议操作与产品工作能区分 |
| P5 | 独立初审与汇合实验 | P1–P4 通过；单独启用 | 只在授权配置下改变内部调度，最终输入屏障及预算成立 |
| P6 | 分层验证、匹配部署和回滚准备 | P1–P4；P5 另报 | 协议、模型行为、部署均有分开的证据；未验证部分不冒充完成 |

建议第一批交付 **P0–P2**，先解决判定语义；第二批 **P3–P4**，改善效率与可观测性；P5 作为可选优化实验，不阻塞核心修复上线。P6 的离线验证跟随每批，付费行为试点与日常切换沿用实际授权范围。

### P0：基线与合同冻结

- 保存本次 ZIP 的哈希及日志索引；从中提取不含无关个人信息的最小测试夹具。原始五份日志留作只读证据，测试不得执行日志中的历史命令。
- 至少提取三种协议反例：必要运行检查缺失但 met；已明确为建议但 open；Lead 指定 Reviewer 改成 pass。另保留正常交付、真实缺陷、无关检查不可用三个正反对照。
- 记录已修改文件及哈希、JS/Python 版本和相关测试基线。源码与日常运行版本分别核对；有活动任务时不做安装切换。
- JS/Python 对本次语义变化采用**显式 v2 合同与 v2 报告**；候选字节清单可继续 candidate-v1。能力协商公布支持版本集合，新任务冻结协商结果，不能只修改 v1 enum 而保留同名协议。既有 runtime-capabilities-v1 容器可增加版本能力集合，保留原 v1 字段；以能力协商而非 npm 版本号选择合同。
- 为合同、报告、状态查询和审计事件定义一张映射表，再分派实现。P1/P2 共改的 schema 与 Python 接受事务由同一集成负责人管理。

兼容策略：现有业务血缘继续其冻结的 v1 语义；历史 accepted 不重算、不自动迁移。新任务只有完整匹配的 JS/Python 能力才使用 v2。旧端不能解析新合同应返回明确能力不匹配，并保留读取、导出、取消与清理能力，不能静默降级后声称具备 v2 保证。将来迁移活动合同需专门迁移事务，不在首批实现。

### P1：把需求连接到实际证据

在已有 contract/review 中增加以下最小结构，字段名在 P0 接口表统一：

```text
Requirement
  id, description, mandatory, source_refs

VerificationPlan
  revision
  checks[]: id, requirement_ids, method, required_for_claim, rationale
  coverage: Reviewer 对派生要求与原始用户目标的覆盖判断

CheckResult
  check_id, candidate_id, manifest_digest, requirement_revision
  verification_plan_revision
  method, execution_status: completed | failed | unavailable | not_run
  evidence_refs[], limitations[], environment_ref

RequirementDecision
  id, result: met | failed | unknown
  verification_plan_revision, check_refs[], reasoning_refs[], coverage_reason
```

- VerificationPlan 是 Agent 按任务制定的可修订验证安排，不是用户新需求。它应在验证前可见，修改时留下理由和 revision；不能在失败后无记录删掉必要检查。
- 对纯静态可证明的要求允许静态检查或有依据的推理；对声称实际运行过的要求必须有实际执行来源。计划可以允许多种等价证据，不硬编码每个视觉任务必须使用某一个工具。
- 运行时生成可信工具事件/产物引用并绑定候选；模型提供的自然语言摘要属于主张，不能自行伪造 runtime-observed 身份。报告附一个字符串引用不自动成为可信工具回执。
- 程序检查 check/requirement 引用存在、候选一致、必要检查状态与需求结论一致。检查结果、需求裁决和最终 ReviewRecord 均绑定 verification_plan_revision，接受事务原子核对当前计划版本；同一 check_id 的旧 completed 结果不能被新方法或 required_for_claim 配置重新解释。需复用的旧观察保留原始绑定，由当前 Reviewer 建立当前计划下的显式映射、理由与裁决；不能原地改旧回执。Reviewer 判断方法覆盖是否充分，特别识别 XML 良构、像素差、退出码等代理指标的局限。
- unavailable/not_run 影响哪些要求必须明确。若无足够替代证据，相关必要要求保持 unknown；不影响其他要求继续执行。替代证据需要当前 Reviewer 说明等价理由，不能只改字段为 met。
- “源码支持自动播放”“已在浏览器实际播放”“已观察视觉效果”在最终摘要中分别表达。无需额外增加隐藏的终审模型。

主要文件：JS `acceptance-contract.js`、`acceptance-runtime.js`、`evidence-reader.js`、`role-guidance.js`；Python `acceptance.py` 及相关事件/状态/接口适配。首先扩展已有证据存储与索引，不另建权威数据库。

验收：同一候选只有静态证据时，不能通过一个要求实际运行证据的必要检查；静态任务可正常通过；无关浏览器检查 unavailable 不影响静态交付；工具事件伪造、错 candidate、过期验证计划拒收；错误检查器 exit=0 的语义问题进入行为评估，不宣称 schema 能自动发现。

### P2：清楚表达处置，并保护复核判断

**P2a Finding：**保留 observation、classification、历史状态和来源，增加独立的当前裁决：

```text
classification: defect | suggestion | unknown
disposition: pending | fix_required | verified_resolved |
             retained_suggestion | not_a_defect | not_applicable
decision: reviewer_identity, candidate/revisions, reason, evidence_refs
blocking: 由 disposition、必要要求状态和当前裁决有效性推导
```

- `retained_suggestion` 表示建议已被认真裁决，本次保留，不表示已实现。仅当前有权 Reviewer 能作此决定，必须说明为何不违反必要要求。
- `suggestion + open` 不因分类而自动通过，先取得明确处置；`unknown` 也不能直接标为非阻塞。
- defect 对必要要求的影响仍需修复或有依据的重新分类；不新增由 Lead 任意豁免用户必要要求的入口。
- 新候选/需求修订沿用既有重新复核规则。原 observation 和决议历史保留；v1 open 不自动映射为已处置。

**P2b 报告处理：**保留原有 repair 入口的兼容性，在 v2 区分两种目的。

- 格式修复只做可证明无歧义的规范化，如规范输出封装或从运行时补权威绑定。原报告和转换记录保存；结果、分类、处置、观察、理由等语义字段不得改变。缺失裁决、冲突文本、候选不匹配不能自动补成 pass。
- 实质复核接收争议、反证和待回答问题；Lead 推荐结论作为带来源的建议保存。Reviewer 可维持原判、要求补查或重新分类；不存在“其他结论一律不许变”的强制提示。
- 新复核继承精确角色路线、原剩余额度和报告血缘；失败准入不丢旧权威报告、不重置预算。报告修复不得写生产文件。
- 新的有效报告通过现有身份/revision 事务成为当前权威记录；未成功提交前，旧记录继续有效。不能把 API 收到请求视为报告已经被替换。

主要文件：JS `acceptance-contract.js`、`fixed-team.js`、`role-guidance.js`、`rework-verification.js`；Python `acceptance.py`。保留 Reviewer 自己提交报告不会使其输入 evidence_revision 无限变化的现有规则。

验收：本案建议可明确 retained 而文件不变；真缺陷不能通过同一路径洗成建议；格式转换不增加模型调用且不能变更裁决；Lead 提供错误建议时，Reviewer 仍可拒绝；报告补交中断可恢复；必要额度不足明确 incomplete。

### P3：让验收和收尾更容易正确完成

**P3a 决策视图与工具：**

- `dpswarm_acceptance` 的默认响应提供紧凑的当前决策视图：候选/各 revision、当前审查权威、必要要求未知/失败项、待处置发现、未收齐证据、当前合法动作及参数模板。
- 大合同保留分页；截断必须明确，不能因精简而漏掉阻塞项。需要时按稳定 revision 读取详情；查询结果不是之后接受事务的锁。
- 区分“采纳现有 Reviewer 报告并请求接受”“提交新终审”“显式 takeover”。可以保留旧工具入口，但返回模板和本地参数校验应避免 Lead 无意用自己的 report 覆盖 Reviewer。
- 统一 index 工具描述、role-guidance、fixed-team 返回说明：独立 Reviewer 有效时，Lead 检查证据覆盖和矛盾，按缺口补查；不普遍要求重做所有验证，也不禁止有依据的重新检查。
- 优先使用宿主已有公开工具可见性能力隐藏明显不适用的工具；没有该接缝时保留服务端检查并返回精确适用条件，不为少几条 schema 改私有宿主或破坏模型请求兼容性。尤其保留此前预算请求组成一致性的约束。

**P3b 可靠收尾：**

- 增加一个可重试的收尾入口，复用 Python 权威状态与既有清理接缝。接受前核对最新候选、审查、验证清单；已采用、已实际结束且报告登记成功的辅助项才可按明确结案原因关闭。
- “验证工作项已经结案”与“作品通过”是不同事实，不能通过自动接受 Tester 工作项制造作品 pass。仍活动、报告缺失、权限不符或未被采用的项不能一并静默关闭。
- Python 记录接受和待清理意图；外部子进程/工作区租约释放按幂等步骤执行，成功后登记完成。跨进程资源清理不能伪装成一个原子事务。
- 崩溃、超时、重复点击和冷恢复从已保存的 cleanup 状态继续；仍有受约束活动 worker、撤销未确认或租约属于其他会话时，不释放或接管其租约。接受失败时也允许独立取消/终止清理；partial/blocked 退出不被强迫转为 accepted。供应商 usage 缺失可继续保留 unknown/reserved，不为关单记零，也不单凭缺失 usage 永久扣住已安全停止任务的工作区。
- 状态明确区分 `accepted + cleanup_pending`、`accepted + finished` 和未接受；最终对用户说明两者。返回当前工作区与已接受快照是否一致及检查时间，不能将旧哈希默认为现在仍一致。

主要文件：`tool-view.js`、`index.js`、`role-guidance.js`、`acceptance-runtime.js`、`completion-status.js`、`workspace-admission.js`、`fixed-team.js`；Python `control.py`、`acceptance.py`、事件/状态接口。

验收：有效 Reviewer 已存在时，Lead 直接拿到正确接受动作；有一个开放建议时能直接拿到所需裁决，避免层层探索 schema；收尾在接受后崩溃仍可续做；重复调用无重复接受/重复释放；活动或缺证据项不会被提前结案。

### P4：统计整个任务的投入与新增价值

- 复用宿主原生响应 usage 和现有 sidecar 审计；增加 root 级聚合视图，分别列 Lead、implementer、tester、reviewer、CM。CM 已归属 worker 额度的部分不得再次加入消费总数。
- 用宿主 session/request/event 身份去重。父日志中的 worker 汇总、流式 usage 副本和原请求只计一次；身份冲突或未知 usage 保留 unknown/下界，不补成零。
- 分开 input、cache read/write、output、保留额度和实测消费；价格有来源、模型、时间戳时才计算费用。不同价格未知时不给虚假的总费用。
- 活动类型记录为实现、验证、产品返工、报告修复、验收控制、资源清理。按实际调用归属及事件路径归因；不能把一轮混合模型响应的 token 精确拆给多个活动，无法精分时标 mixed。
- 用量之外记录唯一新 finding、裁决变化、候选变化、已验证修复、必要证据覆盖、误放行和误阻塞。代码没变不意味着审查无价值；不能把“修改次数多”当质量指标。
- 端到端时间与各 agent 活跃时间分别表示，父等待与子执行不叠加为端到端耗时。原模型生成慢与管理流程慢分开展示。
- 首批只补事实展示与规划参考，不暗中给 Lead 添加硬上限。整任务可选预算属于另一个显式配置扩展，需在原用户额度语义下设计。

主要文件：`host-services.js`、`budget-runtime.js`、`worker-diagnostics.js`、`completion-status.js`、`client.js`，以及现有 plugin audit 汇总接缝。

验收：重放本次 34 条主/子响应得到 1,204,300 totalTokens，其中 cacheRead=755,072、input=380,831、output=68,397；worker-only 仍为 454,154，明确 Lead 被排除；标题未知 usage 不伪造补齐。再加 CM、重复投影、请求中断及缺价回归。

### P5：在保护不变的前提下试验独立初审与汇合

此阶段不是默认省略角色，也不把 Auto 预算设置解释成自主组队。仅对明确启用该调度策略的新任务生效，角色模型和额度保持用户配置。

- 对单文件/强耦合产物仍可一个实现者写入。冻结候选后，Tester 进行执行检查，Reviewer 进行初步独立检查；Reviewer 初审输出不能成为终审 pass。
- 初审减少前序 verdict/classification 的引导，保留原始用户要求与必要事实。所有历史 finding 和反证始终保存在权威账本；最终综合时完整呈现，不能以盲审为名清掉已知问题。
- Tester 的已派发报告全部入账后，Reviewer 按最新 evidence_revision 综合裁决。初审后出现新 finding、需求变化或候选变更，旧终审不得接受；新的自发现及其裁决使用现有原子登记规则。
- 优先复用同一 Reviewer 的可继续会话与原预算；若宿主缺少可靠继续能力，明确记录额外调用与会话成本。先验证接缝，再决定是否值得启用，不能把两阶段审查伪装成一轮免费审查。
- Team 模式显式为 sequential 时保留原顺序；parallel/staged 按各自已有承诺和能力门禁运行，不能将修改测试阶段顺序默认为用户允许省略角色。

主要文件：`fixed-team.js`、`team-dispatch.js`、`role-guidance.js`、`rework-verification.js`、`acceptance-runtime.js`。协议安全用例先通过，再用行为试点判断独立性和延迟是否改善。

## 5. 回归矩阵

下表中的程序断言与模型行为断言分别验证；不能仅构造一个 blocked 报告就宣称模型已经不会误判。

| 编号 | 场景 | 必须保持的行为 |
|---|---|---|
| R01 | 必要运行检查 not_run，报告却 met | 接受拒绝并指明受影响要求；其他独立工作继续 |
| R02 | 纯静态任务、无关浏览器 unavailable | 有充分静态证据时正常通过 |
| R03 | 同一检查存在允许的替代方法 | 当前 Reviewer 有理由地接受替代，不因方法名字不同一律阻塞 |
| R04 | 伪造工具事件、错候选、错需求/计划 revision；同候选与需求不变，仅修改验证计划 | 证据不能被当作当前可信执行结果；旧计划 completed 结果不能直接通过新计划，合法复用必须有显式映射与当前裁决 |
| R05 | suggestion/open | 要求明确处置；不能单凭 suggestion 自动通过 |
| R06 | suggestion/retained_suggestion 与 defect/fix_required 对照 | 前者有据可接受；后者不接受，观察与历史都保留 |
| R07 | 必要项 failed/unknown，同时写 nonblocking | 必要项约束优先，标签不能绕过 |
| R08 | 格式修复试图改 verdict、分类或处置 | 拒绝语义转换并转入实质复核；原报告不被覆盖 |
| R09 | Lead 指定“必须 pass”，实际证据有缺陷 | 行为试点中 Reviewer 可维持/提出拒绝；程序身份检查单独测试 |
| R10 | Reviewer authority、takeover、旧 epoch/fence | 保留现有身份保护，Lead 不能静默代签 |
| R11 | 查询后出现新报告/finding/候选 | 旧决策视图与旧终审不能直接提交接受 |
| R12 | Reviewer 自己提交最终报告 | 不使输入 evidence_revision 自行变化导致无限补交 |
| R13 | 接受成功后清理崩溃、重复调用、冷重启 | 持久状态可恢复，不重复接受/释放，不隐瞒 cleanup_pending |
| R14 | 活动 worker、缺报告、未采用辅助项 | 不被批量结案，不提前释放必要租约 |
| R15 | v1 旧会话与混合 JS/Python 版本 | 旧语义固定，新保证不降级冒充；诊断和清理仍可用 |
| R16 | 用户 amendment、staged 上游消费、依赖变动 | 血缘、必要依赖和 consumed_manifest_refs 的既有门禁不退化 |
| R17 | usage 重复、未知、CM、价格缺失 | 不双算、不补零、预算预留不冒充消费，费用范围可见 |
| R18 | 并行初审先完成，Tester 后发现问题 | 初审不可接受；最终必须看到新发现并重新裁决 |
| R19 | 候选真实坏但检查器 exit=0/错误指标“正常” | 独立行为评估检查是否质疑方法；不让程序通过数充当此项结果 |
| R20 | 正确产物但多个重复检查/无产品修改 | 允许正确接受；记录审查贡献，不为制造改动而返工 |

现有测试入口按模块扩展：

- JS：`acceptance-contract.test.mjs`、`acceptance-native-bridge.test.mjs`、`evidence-reader.test.mjs`、`evidence-native.test.mjs`、`fixed-team-report.test.mjs`、`report-repair-budget.test.mjs`、`fixed-team-rework.test.mjs`、`fixed-team-parallel.test.mjs`、`completion-status.test.mjs`、`tool-view.test.mjs`、`worker-diagnostics.test.mjs`、`host-session-pricing.test.mjs`。
- Python：`test_native_acceptance_20260912.py`、`test_plugin_audit.py`、`test_plugin_audit_resume.py`、`test_plugin_audit_staged_20260912.py`、`test_session_server_20260907.py`；新增测试文件仅在职责确实分离时增加。
- 跨语言：真实 Python HTTP + EventStore + 原生宿主离线夹具覆盖 accepted/rejected/retry/replay；确定性模型输出仅验证协议。

## 6. 验证与推广顺序

1. **离线：**每批先跑直接相关测试，再跑 JS/Python 全量回归并与 P0 对照。失败要区分产品、夹具、环境；不删除断言掩盖回归。
2. **宿主集成：**隔离、匹配版本验证冷启动、原生回执、候选封存、报告登记、接受与清理。模拟模型不计作真实协作收益。
3. **配对行为试点：**先只比较旧固定 Team 与修复后固定 Team，冻结模型、effort、角色、工具、任务和预算，隔离 F1–F6 修复效果。之后再比较 sequential 与独立初审汇合策略，避免多种变化混成一个提升率。
4. **任务覆盖：**至少包含视觉交付、代码行为修复和数据/文档分析；每类包含可接受产物与必要要求失败的对照，并保留一个未用来调提示的新例。本文不给付费运行自动发放额度；实施者先形成明确运行清单并遵循已有授权。
5. **评分：**已知缺陷误放行、正确产物误阻塞、证据与最终表述一致、必要检查覆盖、最终产品质量、管理调用数、端到端时间、完整用量与费用分别记录。由独立判据/人工或盲审核对，Team 自己的 accepted 不作质量真值。
6. **部署：**补丁与验证结果完整后准备匹配 JS/Python 包、能力清单、备份与回滚说明；按已有授权在合适的空闲窗口切换。只部署源码而未确认实际加载，不能写完成。

最低发布条件：R01–R17 相关确定性用例通过，核心桥接无新增失败；已知案例可正确区分“缺证据”“真实缺陷”“可保留建议”；UI/最终状态不把 accepted 等同实际视觉确认或全部资源已释放。语义试点只报告样本内结果，不预设“质量不变、降本百分之几”的结论。P5 未通过时保留顺序路径。

回滚：每批独立开关或合同版本门控；运行中的合同继续由匹配版本解释。新 v2 合同不得被旧二进制直接读取为 v1；回滚时保留配对 v2 恢复能力直到活动任务结束。数据和审计记录保留，关闭优化策略不删除历史发现。

## 7. 实施分工与最终交付

| 责任人角色 | 独占主要写入面 | 与其他工作包的交接 |
|---|---|---|
| 合同/后端负责人 | acceptance-contract.js、Python acceptance.py、相关事件/状态 schema | 冻结 v2 字段与事务语义；P1/P2 合同变更统一提交 |
| 运行时负责人 | acceptance-runtime.js、fixed-team.js、rework-verification.js、调度接缝 | 只按冻结合同接入；fixed-team.js 保持单一集成写作者 |
| 展示/用量负责人 | tool-view.js、completion-status.js、worker-diagnostics.js、client.js | 使用权威状态的派生视图，不另建接受决定或预算账 |
| 回归负责人 | fixtures、跨语言与恢复测试、证据报告 | 可与实现并行设计反例；不得用预定 pass 代替独立验证 |

所有实施者先读取当前 diff；不覆盖或回退其他任务的改动。按模块单独评审，冲突由指定集成负责人解决。

每批交付：补丁、受影响合同/接口说明、直接回归与全量对照、已知限制、恢复/回滚证据。机制主文档只在对应决定进入实际实施时更新并标注状态；本计划不把提案提前写成既成能力。

最终希望得到的流程是：**忠实理解需求 → 为关键主张选择合适证据 → 构建并封存候选 → 按能力分工验证 → 有依据地裁决缺陷与建议 → 需要时修产品或补证据 → 接受并可靠收尾。** 额外代理的价值通过新增证据和更好的决定体现，流程本身的工作量也完整记账。
