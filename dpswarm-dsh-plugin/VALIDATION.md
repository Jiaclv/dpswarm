# 0.11.x b7e149cb 计划第一批判定语义：dpswarm-review-v2（2026-09-13）

依据 `reports/2026-09-13/b7e149cb-mechanism-fix-plan.md` 实施 P0 核心 + P1 + P2a：新增 **dpswarm-review-v2 报告合同**（verification_plan/check_results/requirement.check_refs/findings.disposition），Python 权威语义 `verify_v2_semantics` 落 R01（met 必要要求必须有 completed check 支撑）、R04（check_result 绑定候选/计划 revision，错配拒）、R05/R06（pass 前所有 finding 需终态处置；defect 不可洗成 retained_suggestion）；能力协商以 additive 字段 `acceptance_review_contracts` 发布（revision 保持 1，旧 JS 完全兼容，未上报集合降级 v1）；合同 bind 时冻结版本，错版报告 `REVIEW_CONTRACT_MISMATCH`。JS：v2 schema/语法校验（占位值在提交前即拒）/双 fence 解析/v2 模板与指引/协商接线。回归：Python 12 个新用例（R01–R07+版本冻结）+ 全量 **752 passed + 1 skipped**；JS 3 个新单测 + bridge 40 用例升级 v2 报告形态 + 全量 **754/754**；v1 路径逐字节保留零回归。**未安装日常宿主**；P2b（repair 分路+R08）与 P3–P6 未实施。[实施记录](../reports/2026-09-13/b7e149cb-p1-p2a-implementation.md)

**同日第二批（P2b+P3a）**：`dpswarm_repair_report` 增加 `purpose: format|substantive`（默认 substantive 兼容）——format 路径 Python `verify_format_repair` 语义等价门（verdict/requirement result/finding observation·classification·disposition 漂移即 `REPAIR_SEMANTIC_DRIFT` 事务级拒、旧报告保持当前；无可读基线 `REPAIR_SEMANTIC_BASE_MISSING`），substantive prompt 明示旧报告与 Lead 偏好为不可信输入；`acceptance` 视图新增 `decision_summary`（blocking 四类 + ready_to_accept + 五个合法动作，纯 contract 派生不新建决定面）。R08 四用例 + 决策视图两用例；JS **756/756**、Python **756+1**。P3b 收尾状态机、P4 用量、P5 实验仍未实施；未装宿主。

**同日第三批（P3b 核心+P4）**：`completionStatus` 增 `finalization`（accepted+cleanup_pending / accepted+finished / not_accepted）；新工具 `dpswarm_finalize(reason)`——候选成员与已登记验证项拒静默关闭、可证终态辅助项以 terminate 关闭（非 accept）、幂等 reconcile、返回 retryable；`workspace_consistency` 对已接受 manifest 逐文件重哈希（consistent|drifted|unavailable，漂移只报告不回滚）。新模块 `usage-ledger.js`：`dpswarm_status.usage_ledger` 按活动（implementation/verification/product_rework/report_repair/cm/lead_control）与角色分账，settled call_id 恰好聚合一次、CM 不双计、未知保留预留下界不补零、账本重放不重复计；bridge fixture budget journal 改 Memory+sidecar 双写镜像对齐生产共享审计存储。计划中 34 条响应重放验收因原始日志不在本机无法执行，以单元+集成测试替代。JS **763/763**（新增 7 用例）、Python 756+1 未触及复跑确认。P3b 跨进程清理状态机、P4 价格/质量计数、P5/P6 未实施；未装宿主。

# 0.11.x 会话 88122af1 三 bug 修复 + never-issued 补发 + Auto 移除（2026-09-13）

**触发源**：live 会话 88122af1（glm-5.3-flash 全线，DSH 0.14.1，Auto 预算模式）暴露三个真实缺陷，其中两个为全新 bug 类；修复后按用户决策移除 Auto（Lead 预估）预算模式。

- **Bug 1（maxTokens 超路由上限）**：30 万 Auto 护栏整形出 `maxTokens 131883`，超 glm 路由 131072 硬上限，实现者首请求 400 零产出。修复四层：能力层读 `resolveModelInfo` 的 `defaultMaxTokens`/`context.contextWindow`（仅正整数可信）；`outputLimit` 第四参 routeCap 取 min 钳制（unlimited 透传不变）；`budget.js` 请求 hook 按路由惰性缓存查上限（仅权威成功入缓存，失败降级不钳制）；`budget_planning.route_limits` 向 Lead 披露宿主观测窗口。回归：runtime 钳制单测复刻事故数字（168117 输入估算 + final_only 无储备 → 131883 → 钳 131072）+ 请求级端到端 + 降级不钳制。
- **Bug 2（返回体非无损 JSON）**：`fixedProfile` 两处（model 路由 + lead 模式 implementer）显式写 `reasoning_effort: undefined`，宿主 `isJsonValue` 拒绝整个 `dpswarm_run` 返回（执行已完整发生）。deepseek 会话全配 max 强度故从未触发。修复为条件展开；`tests/fixed-team-lossless.test.mjs` 以宿主 `dsh-util-values` 的 `isJsonValue` 验证完整返回体，stash 双向红/绿验证（红点精确命中 `profile.implementer/tester.reasoning_effort :: undefined value`）。
- **Bug 3（结构死锁）+ 方案 A**：首跑实现者在 tester 出生前死亡 → rework 后 `REVERIFY_SOURCE_UNAVAILABLE`（无血缘可续）→ acceptance 硬要求 tester 证据 → `VERIFICATION_INCOMPLETE` 永久，四个杠杆全被正确拒绝。实现 never-issued 补发：`verifyRework` 准入双路径——无 ready 延期决定时 `probeUnissuedVerification`（纯读：live implementer → budget `policy_binding.run_id` → resume 未领 → 同进程 verifiers 全空）成立则写与延期决定同构的合成决定（`issuance: 'never-issued-original-role'`，复用 `claimReworkVerification` 防重入），claim 后 `resumeTeamRun` 恢复 `unissued_original_role` 额度（"发行后失败"形态被 `WORKER_BUDGET_ROLE_ALREADY_ISSUED` 正确挡住），`dispatchUnissuedVerification` 以首验形态发行 tester/reviewer + 建立血缘 + 逐角色 cleanup 确认 + finally 吊销未绑定余额；`dpswarm_status` 展示 probe 视图（纯读不写）。死锁全链复刻测试（死亡 implementer 含 budget frozen + zero-usage settle）+ 已发行边界测试。
- **Auto 模式移除（用户决策）**：Lead 事前预估 `worker_budgets` 无信息基础（不掌握路由上限/窗口/任务真实难度，v14 难度谱证伪全部事前代理；B 波实验证明 rail 精确调值零收益），首次实战即系统性定价错误。移除产生路径：设置 UI 两模式（存量 auto 配置回落 manual 固定 rail）、`dpswarm_run` 删 `worker_budgets` 参数（传入以 `USER_WORKER_LIMITS_AUTHORITATIVE` 拒绝且防在参数白名单前）、删 `dpswarm_prepare_worker` 工具、`beginTeamRun` 删 decisions（record.decisions 恒空保留回放兼容）、`plan()` 显式拒绝、预算建议层删 envelope 估算代理并重写 planning（固定 rail + 到量收尾 + Lead 裁决继续/验收/终止）、Lead/worker 指引同步。历史 auto 账本解释路径全部保留（issueTeamWorker/allocation/fixedSource 的 auto 分支）。
- 第 4 项（`unavailable_checks` 条目形态）写入 `REVIEW_FORMAT_INSTRUCTIONS` 与 schema description：一条一字符串 `"<what was not checked> :: <why it was unavailable>"`。

插件回归 **746/746**（仓库根 `node --test dpswarm-dsh-plugin/tests/*.test.mjs`；删 12 个纯 Auto 语义用例、新增回落/拒绝/死锁/边界用例）、控制服务 **740 passed + 1 skipped**。3 个强依赖 Auto 的手动真机探针标注 OBSOLETE 保留为历史记录；`acceptance-native-bridge` 存在已知真实 Python 子进程时序 flaky（与改动无关）。

**同日第二轮（live 会话 e3589816 复盘，0.15.0 真实链路）**：重装后首轮真机测试暴露两个残留缺陷并已修复——①**钳制对 glmcp 失效**：`resolveModelInfo` 对 glmcp 只返回 `context.contextWindow`（无 `defaultMaxTokens`），`routeOutputCap` 只认后者 → cap=null 不钳制，补发 tester 的整形 `maxTokens 269854` 仍死于路由 400（限制 [1,131072]）。修复：上限取 `min(defaultMaxTokens, contextWindow)`，窗口是输出的物理上界（输出超窗口必然非法），任一来源存在即参与钳制。新增 window-only（glmcp 形态）与双来源取小两个请求级用例。②**0.15.0 自身的 lossless 回归**：首跑实现者死亡 + requirements 已建 contract 时，候选从未捕获，`acceptanceRuntime.view()` 的 `candidate: a.candidate, review: a.review` 无条件赋值写出显式 `undefined`；且 execute 边界 `tool-view.acceptanceView()` 的 `candidate: candidateView(value.candidate)` 在 clone 之后又造回一个显式 `undefined`（此前所有测试或无 acceptance、或有候选，从未覆盖"有 contract 无候选"形态）。两处改条件展开；新增 execute 边界钉子测试（真 Python 链 + 宿主 `isJsonValue` 同时检查 controller 级与 `modelToolView` 级产物——宿主实际检查点）。会话行为面：never-issued 补发在真实链路被 Lead 正确使用（含 takeover 被 tester 守卫正确拒绝），机制面无回归。插件回归 **749/749**。**补充（同日第三批）**：注册表盲区路由增加实测静态兜底——`budget.js` 内置 `FALLBACK_OUTPUT_CAPS`（当前仅 `glmcp: 131072`，数据来源为网关自身 400 拒绝消息实测，provider 级参数校验），仅在 `resolveModelInfo` 对该路由 `defaultMaxTokens` 与 `contextWindow` **均无**数据时参与钳制；任一注册表值存在时权威优先（静态表永不覆盖注册表，未知路由保持不钳制）。优先级用例：fallback 命中 / 注册表优先（独立路由缓存） / 未知不钳。插件回归 **751/751**。**第四批修正（live 会话 03e8db1b）**：glmcp 宿主注册表上报 262144（2^18，乐观值），而网关真实参数上限 131072——"注册表值权威"的优先级设计被证伪（网关参数校验上限可以小于注册表任何字段）。改为**全来源取 min**：实测静态表、defaultMaxTokens、contextWindow 任一存在均参与钳制（钳紧方向错误是安全的——只是少输出；钳松必死 400）。用例：注册表乐观 262144 被实测 131072 压住、注册表保守 65536 仍生效。插件回归 **751/751**。

**同日第四批（P5 机制与协议安全）**：新设置 `reviewerIndependence`（默认 guided 零变化；UI「验证独立性」）；independent 模式——`dispatchIndependentVerification` 并发发行 tester 与盲初筛 reviewer（各自原额度/冻结路由），初审永不注册 reviewer evidence、汇合前 terminate 结案释放槽位，汇合轮走初审 reviewer 的 rework 链（额外调用入账）、prompt 携带 tester+初审（untrusted），唯一注册为终审；R18 由 evidence_revision 门+用例钉住（tester 后发现 → 汇合 needs-rework、accept 拒、初审 session 不在 evidence）。顺带生产级修复：controller/budget 共享单 AuditJournal（并行时跨实例 CAS 冲突）。JS **765/765**（bridge 46）、Python 756+1 未触及；行为试点未运行；未装宿主。

# 0.11.0 有界持久 mailbox（Lead↔worker 直系通道）验证（2026-09-11）

**测试环境披露（重要）**：本轮回归在本机遇到宿主版本漂移——全局安装的 `@deepseek-ai/dsh` 已升级到 `0.1.5-rc.1`（dsh-session `0.1.5-rc.2`），其 Session 头要求 `version 3` 并更改了 settings `installSection` API，而插件测试夹具（与仓库内 `deepseek-harness-master` 源码，`SESSION_FORMAT_VERSION = 0`）仍按 v0 构建。改动前基线实为 **303 通过 / 83 失败**（仓库根 `node --test dpswarm-dsh-plugin/tests/*.test.mjs`，386 项），失败全部集中在 cm / lead-route / budget-host / mechanism-911 的宿主 API 族（40× "session header version must be 3, got 0"、41× settings/registry API 变更、1× python 非 ASCII 路径、1× installSection），与本轮改动无关；0.10.0 记录的 381/381 生成于宿主升级之前。改动后同环境 **318 通过 / 83 失败**：原 303 全绿保持、新增 15 项 mailbox 测试全绿、失败集逐名 diff 与改动前完全一致（零回归）。**同日补注**：83 项宿主夹具债已在后续独立轮次修复（夹具 v3 头/新 API 对齐 + `lib/host-session-compat.js` 等价读取层，机制语义零改动），全量 **401/401 绿**；详见 `reports/2026-09-11/宿主夹具修复-plan.md` 的结果一节。

语义来源：宿主 `packages/experimental/agent-team/src/mailbox.ts` 的有界模式（`maxPendingMessagesPerMember` 默认 64 超限 `TEAM_MAILBOX_FULL`、`maxMessageBytes` 64KiB 成帧检查）与宿主 agent 收件箱双投递语义（`core/agent/src/runtime-types.ts`：`inject` 只注入不唤醒、`followup` 排队并唤醒；`packages/subagent` 的 `startContinuable`/`followup` 持久子会话通道）。v1 只做 Lead↔worker 直系通道（平级 worker 互发不在稳定 API 内）。改动：

- **新 `lib/mailbox.js`**：`TeamMailbox`（准入/有界/持久化/投递路由，按根会话链式串行化 + KV 单调用原子）+ `KvMailboxStorage`（宿主 `ctx.storage` KV 适配，prefer `json` backend，懒开 `dpswarm_mailbox` 单元；hub 缺席时 `available()=false` 降级关闭）。三类消息 kind 封闭（fact/clarify/block），控制性意图词表额外以 `DPSWARM_MAILBOX_CONTROL_REJECTED` 拒绝并指向 dpswarm_run/rework/review；超限 `DPSWARM_MAILBOX_FULL`/`DPSWARM_MESSAGE_TOO_LARGE`；去重按 message_id 幂等（重发返回原条目，不重复排队/审计）；每条消息 `{message_id, run_id, from, to, kind, refs, ts}` 持久化；`register()` 换 run_id 接管时旧 run 未投递消息按 `DPSWARM_MAILBOX_RUN_SUPERSEDED` 拒绝留痕。审计词汇 `dpswarm/mailbox-queued|delivered|rejected`（delivered 带 `via: inject|followup|acknowledge` 与 `carrier: live-channel|wake-prompt|rework-prompt|lead-read`）；审计失败按 worker 诊断先例收进结果字段不阻断机制。
- **`lib/fixed-team.js` 接线**（不碰产物 ready 唤醒语义，mailbox 为叠加层）：run 起点注册成员表（lead + 子任务 id/角色名 + tester + 独立 reviewer）；staged 唤醒循环（`drain(carrier='wake-prompt')`）与返工延续 prompt（`carrier='rework-prompt'`）承载目标成员 pending；`[DPSWARM_WAITING]` 解析点（首轮 + 唤醒后再等待）自动落 worker→lead 的 `block` 消息（确定性 message_id `wait-<item>-<artifact>` 幂等，失败收进 `cleanup.mailbox_error` 不阻断运行）；`dpswarm_run` 返回体新增 `mailbox.pending_for_lead`；`dpswarm_status` 新增 `mailbox` 段（只读不确认）；新增 `mailbox()` 工具方法与 `resolveTeamWorker()`（worker 会话→成员身份，rework 延续子会话合法，其消息 run_id 盖邮箱注册的 run 而非 reworkId）。
- **`lib/index.js`**：注册 `dpswarm_mailbox` 工具（Lead/worker 双面；TEAM_REQUIRED 的 `openTools` 放行）；`lib/role-guidance.js` worker 指引声明其 worker 侧工具身份；`dpswarm_artifact` 描述同步（不再是"其余全 Lead-only"）。
- **不碰**：写锁、返工血缘、准入、串行/parallel/staged 三档互斥、REVIEWER_PENDING 门控、产物 ready 唤醒链。

与宿主的有意偏差：宿主 experimental mailbox 是成员间任意互发 + Lead 日志事务账本；本插件 v1 只有 Lead↔worker 直系（worker 互发与自我消息拒绝 `DPSWARM_MAILBOX_ROUTE_REJECTED`/`SELF_MESSAGE`），持久化走 `ctx.storage` KV 而非事务账本（跨进程权威仍在 sidecar 审计）。在场即时投递的 `inject`/`followup` 通道以 `attachChannel` 挂接（宿主 continuable 子会话语义），但当前 fixed-team 派发是一次性 SubagentRun（无在场 continuable 子会话），所以实际承载是唤醒/返工延续 prompt——语义等价（clarify/block 随延续唤醒送达、fact 静默作上下文），测试对两条路径都钉了路由正确性。

新增测试 `tests/mailbox.test.mjs` 15 项：核心 11（三类消息在场通道路由 fact→inject / clarify、block→followup 且不误唤醒、无通道 pending FIFO + drain 的 via/carrier、通道失败结构化降级、64/65 有界 + 跨成员独立计数 + 释放额度、64KiB 帧上限（CJK 成帧）、FileKv 冷恢复三实例往返（含成员表与投递态持久）、message_id 幂等去重（含不重复审计）、控制词表拒绝 + kind 封闭 + 校验族、run 接管拒绝留痕、有界渲染）+ 控制器集成 4（staged 等待 worker 的 block 入 lead 收件箱且 run 返回体呈出、lead clarify 随唤醒 prompt 送达并记审计、worker 工具纪律（只能发 lead/控制词拒/未知会话拒）、返工延续承载 post-run 消息 + rework 子会话发件、storage 缺席降级不阻断含等待超时路径）。`index.test.mjs` 工具清单同步加 `dpswarm_mailbox`。未跑真实模型试点。

---

# 0.10.0 三层交接包 / verbatim 直贴契约 / 验收可见性验证（2026-09-11）

插件回归 **381/381**（仓库根 `node --test dpswarm-dsh-plugin/tests/*.test.mjs`）、控制服务未改动（本轮纯插件侧）。`npm test`（cwd=插件目录）380/381，唯一失败是 `host-model-registry-integration` 的既有 cwd 敏感路径拼接（`resolve('dpswarm-dsh-plugin/tests/…')` 相对进程 cwd，基线同样失败），从仓库根运行即绿。语义来源：Python 编排器五轮实验验证的三项机制（`dpswarm-plugin/dpswarm/orchestrator_lg.py` 的 `_handoff_section` / L1 提取正则组 / verbatim 措辞、`orchestrator.py` 的 `_upstream_evidence` 验收可见性），本轮回移到 JS 插件。改动：

- **三层交接包**（新 `lib/handoff.js` + `fixed-team.js` 相位门）：staged 新相位首波的"前序相位交付摘要（不可信摘要）"升级为三层——L2 摘要层（保留 2000 字符有界截断，措辞改"摘要仅供导航，逐字内容以 L1/原文为准"）+ L1 原子事实层（确定性逐字提取，不走 LLM：fenced 代码块 > JSON/schema 行 > 函数/类签名 > 表格行/键值对/文件路径行/版本号；未闭合围栏按 fenced 级收到 EOF；去重、按优先级丢弃、单块超剩余额度且剩余 ≥200 截断标注、输出恢复原文序；上限 semantic 4000 / verbatim 8000）+ L0 原文层（产物 id + 文件路径 + 读门回查指引——读门 `ARTIFACT_NOT_READY` 对 ready 产物放行，正是现成回查通道）。
- **verbatim 判型与直贴契约**：产物 id/标题/任务描述命中逐字信号（逐字/引用/一致/schema/签名/verbatim/quote）→ 该产物交接标 `handoff_profile=verbatim`：L1 前置并放宽上限，头部明示"逐字内容禁止凭记忆复述，必须先读取上游产物原文再逐字直贴；L1 层仅供定位预览"。判型恒规则兜底（插件无 CM，`decider=rule`），每次注入记审计事件 `dpswarm/handoff-profile{artifact_id, profile, decider, upstreams}`；相位内依赖的唤醒延续 prompt 对 verbatim 产物同样附直贴契约（`via: 'wake'`）。
- **验收可见性**（消灭"跨 item 约束物理上不可核验"的盲放）：带 deps 的产物在验收/review 时必须能看到上游 ready 交付——独立 Reviewer 派发 prompt、`dpswarm_run` 返回体 `acceptance_visibility`、`dpswarm_review` 返回体 `upstream_evidence`、返工轮 Reviewer 血缘复核 prompt 四处同构携带（每份 ≤4000、总量 ≤8000；超限降级 L1 逐字段 + 路径/`dpswarm_report` 引用；未 ready 记"交付内容不可见"）；提示明示"缺材料不可验收通过"。Review 门禁语义不变（advisory 共置，不新增硬门）。
- **不碰**：写锁语义、返工血缘、准入、串行/parallel/staged 三档互斥规则、REVIEWER_PENDING 门控。

与 Python 的有意偏差：无 CM（L2 恒截断摘要、无 playbook 覆盖、判型无 llm 路径）；无 PULL 兜底通道（L0 回查 = 读门 + 文件路径，截断标注相应改写）；验收超限降级从"逐字截断 + package ref"改为"L1 逐字段 + 路径引用"（插件无 submission_package 引用面）；交接触发点保持插件既有的"相位首波"（Python 按 deps 逐项注入）；相位交接上游集合保持"前序全部交付"（Python 仅 deps）。新增测试：`tests/handoff.test.mjs` 11 项单元（判型/提取优先级与丢弃序/未闭合围栏/去重与原文序/截断标注/verbatim 前置与契约/验收材料降级与缺材料行），`fixed-team-parallel.test.mjs` 4 项集成（三层结构与层序、verbatim 契约与审计、Reviewer prompt 可见性与缺材料、run/review 返回体携带证据）；0.9.0 的相位交接表征测试在 L2 层保留"前序相位交付摘要"定位词后保持绿。未跑真实模型试点。

---

# 0.9.7 工作区 lease 自愈与可见性验证（2026-09-10）

插件回归 **366/366**、控制服务 **580/580** 通过（不含外部模型调用）。起因：02:58 真实死锁——01:56 会话 QUOTA 失败后 lease 泄漏（测试者/Reviewer 交付未结案），新会话同目录派发被 `WORKSPACE_BUSY` 拒绝；status 只看本会话所以"什么也没显示"，TEAM_REQUIRED 又拦了 Lead 的排查命令。三层修复：`acquire()` 对宿主进程已死的 lease 跨会话自动接管（run 返回体记录 `lease_takeover`）；活主 lease 的 `WORKSPACE_BUSY` 报错携带持有方 session/pid/恢复路径；`dpswarm_status` 新增 `workspace_lease` 块（含"死主将自动接管/活主将拒派发"的指引文案）。设计红线保留：活进程 lease 不自动让渡、清理不确定不自动清除。新增表征（fixed-team）：死主 lease 跨会话接管成功且记录 takeover、活主报错含 `session parent`、status 两种指引文案。事故当时的手动恢复：核对失败会话审计（全部 item 终态、清理确认、最后一笔 56 分钟前）后按显式恢复路径删除 lease 文件。未跑真实模型试点。

---

# 0.9.6 谁提的缺陷谁验收（Reviewer 血缘返工复核）验证（2026-09-10）

插件回归 **365/365**、控制服务 **580/580** 通过（不含外部模型调用）。原则核对：谁写谁修此前已成立（返工恒回原实现者血缘）；谁提谁验只覆盖测试者（0.8.1 复验），Reviewer 发现的缺陷在返工后无人回头确认（01:03 实证：真缺陷是 Reviewer 抓的）。改动：rework() 在测试者复验之后接续 Reviewer 血缘复核（可信终态校验、上一论 verdict 与发现作为不可信上下文、VERDICT 收尾、独立返工链额度），新 verdict 经既有 REVIEWER_PENDING 门禁自动门控本轮实现者交付（state.reviewers 录入新血缘即生效）；容量自适应从"实现者+测试者"扩到按真实接续数计算；`compactWorkerEntry` 补出 `verification_of` 字段（此前测试者复验的归属标记被摘要层吞掉）。新增表征：配置 Reviewer 的返工轮交付序列为 [implementer, tester, reviewer]、复核 prompt 携带其上轮报告、容量 4→6、复核未结案时 REVIEWER_PENDING、结案后可验收。Lead 提示词与 rework 返回体 next 同步更新。未跑真实模型试点。

---

# 0.9.5 返工默认护栏验证（2026-09-10）

插件回归 **364/364**、控制服务 **580/580** 通过（不含外部模型调用）。起因：01:56 真实运行中一次要求精确数值 IK 的返工在出厂默认的 unlimited 返工额度下螺旋失控——57 次调用 / 371 万 token 反复写数值脚本，最终打穿 DeepSeek 账户余额（QUOTA），整个会话失败。改动仅默认值：`reworkBudgetMode` 出厂默认 unlimited → fixed（沿用既有 600,000 token / 28 次字段默认）；`budget-runtime` 缺省回退同步改为 fixed。固定返工本就走与首轮 worker 完全相同的受限执行路径（预约-结算、1/3 slack 收尾停泊、逐调用账本），worker 提示词本就声明"触轨即报告已存内容、Lead 裁决"——本次只是把默认打开。测试语义分层：index.test 改钉 Config 新默认与 Lead 提示词的固定额度文案；fixed-team-rework 与 worker-rework-budget 两个套件显式传入 `reworkBudgetMode: 'unlimited'`，继续钉 unlimited 模式的机器行为（该模式仍受支持，只是不再是默认）。已显式保存过 rework 模式的安装不受影响。未跑真实模型试点。

---

# 0.9.4 终态 item 验收幂等化验证（2026-09-10）

插件回归 **363/363**、控制服务 **580/580** 通过（不含外部模型调用）。01:45 真实运行中 Lead 试图验收已被返工派发自动终止的原始实现者 item，被控制面状态机以 `ILLEGAL_TRANSITION`（terminated → finalizing）拒绝——信息真实但生硬，且同一瑕疵在 01:03 已出现一次。修复：`dpswarm_review` 对已处于终态的 item 返回幂等结果 `already_accepted` / `already_terminated`（附"请验收最新实现者 item"说明），不再落到状态机；防虚假验收防线保持不变——无交付包的失败 worker 被 accept 时仍抛 `DELIVERY_PACKAGE_REQUIRED`（fixed-team-rework 既有表征钉死，本轮一度被初版改动撞红后按"有交付包才幂等"收紧）。返工返回体 `next` 明示源 item 已终止。新增表征：fixed-team-parallel 钉"终止后 accept → already_terminated 且无幻影迁移"。未跑真实模型试点。

---

# 0.9.3 模式透传与 staged 工具声明验证（2026-09-10）

插件回归 **362/362**、控制服务 **580/580** 通过（不含外部模型调用）。起因：01:03 真实运行中用户已在弹层把任务切为 parallel（`~/.dsh/settings.yaml` 的 `teamModeOverrides` 已正确持久化、sessionId 键与 `enabledSessions` 同构），但 Lead 全程看不到该选择——并行只是上限不是意图，单文件任务按惯性跑了串行（合法但非用户所愿）。修复：`teamModeGuidance(mode)`（role-guidance）统一生成给 Lead 的指引文案；`dpswarm_status` 新增 `team_mode{mode,source,instruction}`；`team_requirement.next` 在 required 相位携带同一指引；`dpswarm_run` 返回体新增 `team_mode{mode,split_form,note?}`（parallel/staged 下未拆分会显式记录说明）。另发现并修复 0.9.0 遗留缺口：`staged` 形态从未声明进 `dpswarm_run` 工具 schema（模型不可见、不可调），本期补全（phases 1–4、artifacts 1–8，字段与 `validateStaged` 对齐，含互斥/无环/相位顺序语义的描述）。新增表征：index（schema 双形态声明 + status team_mode 默认 serial）、team-required（next 文案随模式切换）、fixed-team-parallel（status team_mode 为 parallel 且无形拆分运行时返回体带 note）。未跑真实模型试点。

---

# 0.9.2 收尾停泊估计误差余量验证（2026-09-10）

插件回归 **360/360**、控制服务 **580/580** 通过（不含外部模型调用）。00:14 真实运行的死因分析：final-only 停泊按 pre-step 估算输入判断、硬轨按请求时刻实测信封判断，二者不一致时永远是估算偏乐观，实现者在文件落盘后的下一步被 `request_output_limit` 直接打死且无报告（该点上报告/CM 请求的输入同样塞不进额度，提前停泊是唯一有效手段）。修复：`closeoutForecast` 对（当前步输入 + 最终步输入）加 `CLOSEOUT_ESTIMATE_SLACK_RATIO = 1/3` 余量，forecast 新增 `estimate_slack` 观测字段；Auto 预算建议文案补充"写完再自查还要花几个全量信封"的口径。曾评估在 `agent/request` 用实测信封重判停泊，核查宿主 dsh-agent-loop 后放弃：system 在 `agent/request` waterfall 之前已渲染成串（lib/index.js:611 vs 708），此时注入的报告指令上不了线，只会把死因换成 `WORKER_CLOSEOUT_INSTRUCTION_MISSING`。表征更新：17:32 账本（35,852 剩余 / 14,028 步）从"不停泊"改钉为"以 budget_rail 停泊且 estimate_slack=9,352"（budget-rail-characterization 与 mechanism-911 各一处，原为 0.8.2 钉死的不停泊行为，本次为有意变更）；其余预算钉全部保持不变。未跑真实模型试点。

---

# 0.9.1 用户侧协作模式选择（串行/并行/分阶段门禁）验证（2026-09-09）

插件回归 **360/360**、控制服务 **580/580** 通过（不含外部模型调用）。新增：设置项 `teamModeOverrides`（按会话，重复条目取最后）+ `teamModeFor()` + `run()` 用户模式门禁（serial 拒 `subtasks`/`staged` → `USER_TEAM_MODE_SERIAL`；parallel 拒 `staged` → `USER_TEAM_MODE_PARALLEL`；staged 拒 `subtasks` → `USER_TEAM_MODE_STAGED`；缺省 serial）；罗盘弹层三档选择器（`teamModeOf` 与后端同语义，写设置经既有原子 mutate 通道）。表征测试新增「用户模式门禁」用例（默认拒拆分/越形态拒绝/放行匹配形态）；`fixed-team-parallel` fixture 默认 parallel、staged 用例逐个翻模式。SSR 冒烟（宿主 React 18.3.1 + react-dom/server 渲染三个 slot，`.tmp/ssr-smoke/smoke.mjs`）通过：弹层含三枚模式 chip 且按 `teamModeOverrides` 正确选中。`client-browser.mjs` 未跑（本机无 Playwright，选择器交互未经真实浏览器检查）。

---

# 0.9.0 分阶段协调（产物状态板 / 读锁 / 挂起唤醒 / 相位交接）验证（2026-09-09）

插件回归 **359/359**、控制服务 **580/580** 通过（不含外部模型调用）。实现 fixed-team-v3：`dpswarm_run` 的 `staged` 参数（产物板 + 相位，缺省=串行不变、与 subtasks 互斥）；控制面新增 artifact 实体（`tests/test_artifact_board_20260909.py` 44 项：注册校验/转换表全正反/回放一致/HTTP 认证与双会话隔离）；JS 侧状态读锁（`ARTIFACT_NOT_READY`）、`dpswarm_artifact` worker 工具（归属校验）、Kahn 波次派发、`[DPSWARM_WAITING]` 挂起标记 + 产物就绪驱动的链接延续唤醒（超时记 `ARTIFACT_WAIT_TIMEOUT`）、相位交接摘要注入。串行与 0.8.0 并行的既有表征钉死全部保持绿。真实 resume 探针与 CM 精选摘要分发列入后续轮次；浏览器面板新卡片未经实机检查（本机无 Playwright）。

---

# 0.8.4 职责分离与 Reviewer 裁决门禁验证（2026-09-09）

插件回归 **351/351**（无外部模型调用）。动机：用户设计决策——Lead 负责调度/安排/掌控全局，核查判断归 Reviewer。

- 配置独立 Reviewer（reviewerMode=model）时，`dpswarm_review` 接受实现者交付须先看到 Reviewer 结论：Reviewer item 未结案时接受被 `REVIEWER_PENDING` 拒绝；Reviewer 死亡/失败时须显式终止其 item 并记录接管原因，Lead 才可直接核验（接管路径有审计）。
- Reviewer 提示词升级为"核查权威"：必须读实际文件、不信任报告，以 `VERDICT: pass|needs-rework|blocked` 收尾；其结论驱动验收，Lead 不复核其覆盖范围。
- Lead 指引按 reviewerMode 分支：配了独立 Reviewer 时 Lead 只做编排（派发、依据 Reviewer 发现管理返工、执行控制面裁决）；未配置时维持 Lead 自行核验的原语义。
- 无 Reviewer 配置时门禁不生效（既有测试钉死）。

---

# 0.8.3 收尾报告可读性修复验证（2026-09-09）

插件回归 **348/348**（无外部模型调用）。动机证据：20:24 真实运行中三个 worker 的 closeout 最终报告全是 DSML 工具调用标记而非文本（Lead 实测"reports are mostly tool-call traces"）。

根因：final-only 调用把工具 schema 从请求中整个摘除，DeepSeek 系模型对无工具的请求会把自己的 DSML 调用 DSL 当文本输出。修复：收尾时**保留工具 schema、执行层拒绝**（工具闸门返回"写最终报告"的指引），并在收尾准入口径中放行一次"工具尝试步 + 报告步"（`calls_at_closeout + 2`）；指令文案明确"工具会被拒、报告用纯文本、不要写调用标记"。工具始终不被摘除后，过小额度在派发前以真实信封（含 schema）诚实拒绝，不再产出"假装工作过"的报告。

---

# 0.8.2 预算即异常护栏验证（2026-09-09）

插件回归 **348/348**、控制服务 **536/536** 通过（不含外部模型调用）。动机：三次真实运行（911／17:32／19:30）证明事前精估 token 预算不可行，紧预算反复制造半途死亡。

- `outputLimit` 删除渐进减半：worker 请求保留完整输出上限，只受"剩余额度 − 本次输入"硬约束；17:32 的 5,710 token 挤压死亡场景在 `tests/budget-rail-characterization.test.mjs` 中被钉为"不再发生"。
- `closeoutForecast` 简化为单规则：下一步完整输入 + 最终报告（4,096 下限 + 2,048 储备）放不下才停车，触发码 `budget_rail`（替代 `cannot_afford_exploration_and_delivery`／`output_below_report_floor`；后者是 0.8.1 的过渡形态，被本版取代）。
- 收尾指令改写为"触轨待命"语义：报告已完成／已保存路径／未完成／预计剩余，由 Lead 决定接续（dpswarm_rework 链接延续）、接受部分交付或终止。
- 新装默认额度升为 1,200,000 token／50 次调用；Lead／worker 指引与 Auto 规划文案改为护栏表述。
- 911 与 17:32 两组真实账本数字在新语义下重新钉死（911 测试者以 `budget_rail` 停车并交付；17:32 测试者既不提前停车也不被挤压）。两个既有测试因指令文案更新同步了断言；`host-model-registry-integration` 在多文件并行下偶发端口冲突，单独与重跑均通过。

---

# 0.8.1 收尾输出下限与返工链路完善验证（2026-09-09）

插件回归 **341/341**、控制服务 **536/536** 通过（不含外部模型调用）。动机证据：17:32 真实运行（tester 60k、reviewer 90k 双双 `WORKER_OUTPUT_LIMIT_REACHED` 无交付）的审计账本实数。

- **closeout 输出可行性**：`closeoutForecast` 新增输出可行性条件——当挤压规则会把下一步输出压到 4,096 token 的报告下限以下时，提前进入 final-only 收尾（最终交付调用不受挤压，保留完整空间）。触发 `output_below_report_floor`；输入侧规则与语义不变（既有 911 两组表征钉死保持绿）。表征输入直接取 17:32 tester 账本（60k 预算、两次调用后剩 35,852、挤压后 2,864 < 下限）。
- **返工携带前序上下文**：返工提示词现在包含原实现者的最终报告与已保存候选路径（截断有界、标注不可信证据），新会话不再盲目重新探索。
- **原测试者自动复验**：实现者返工交付后，原测试者血缘自动接续一次复验（自己的返工链预算分配；提示词含原任务、返工反馈、返工交付报告与该测试者上一轮报告）；复验报告是 Lead 评审的参考证据，永不自动验收。测试者血缘记录最新复验项，连续返工逐轮接续。
- **裂变规模容量自适应**：固定团队的未结案 item 计入 §7 `max_team_workers`（默认 3）——三路并行 + 汇合测试者、或返工的"实现者 + 测试者复验"都会超限（19:30 真实运行中复验被 `TEAM_WORKERS_EXCEEDED` 拒绝）。现在 run/rework 在派发前按所需容量发布经审计的 spec revision（只抬 `max_team_workers`），全部结案后 reconcile 恢复原值；宿主中途崩溃则保留已审计的较高值，可在控制面板调回。

注：commit b7fea1b 消息中的 "0.7.9" 标签已并入本 0.8.1 版本。真正的会话级 resume（fork/seed 恢复 worker 完整上下文）需要宿主 fork 元数据与预算身份的专门探针轮次，本期以"富化上下文交接"过渡。

---

# 0.8.0 并行实现者与写范围认领验证（2026-09-09）

插件回归 **340/340**、控制服务 **536/536** 通过（不含外部模型调用）。实现要点：`dpswarm_run` 可选 `subtasks`（1–3 条互不重叠写范围）触发并行实现者相位，join 后测试者／Reviewer 串行汇合；写范围经 `tools/pre-execute` 在 write/edit 落点强制执行（`write-scope.js`，Windows 路径规范化、保守相交判定、审计事件 `dpswarm/write-scope`、冷恢复可重建）；预算发放扩展为"角色+子任务"键（`budget-runtime.js` 血统/返工/收尾全链路对齐）；`delegateOnce` 的冻结路由校验放开为"同路由 N 子任务"（`delegation.js`）；返工子会话接续原子任务写范围。串行缺省行为由表征测试钉死（`tests/fixed-team-v2-characterization.test.mjs`），并行端到端、部分失败汇合、返工认领、Auto 按子任务预算见 `tests/fixed-team-parallel.test.mjs`。

浏览器套件未运行（本机无 Playwright／esbuild），弹层 token 条按子任务分段、后台面板子任务标识仅经 SSR 冒烟。真实模型下 Lead 拆分质量与并行净收益未测量——需另行试点（全角色 glm-5.3-flash）后再下结论。shell 类工具的路径解析本期不做，已在文档明示。

---

# 0.7.8 返工限额、收尾估算与报告读取验证（2026-09-09）

插件回归 **328/328**、控制服务 **536/536** 通过。外部模型调用为 0；动机证据为 0.7.7 首次真实模型运行（911 动画任务会话）审计账本中的实际数字。

- 911 表征测试（`tests/mechanism-911-characterization.test.mjs`，9 项）钉死真实运行数字：测试者剩 106,078/200,000 token 与 9/12 次调用时触发 `cannot_afford_exploration_and_delivery`（差额仅 18 token），最终交付调用实际只需 21,516；返工在 manual／auto／unlimited 三种首轮模式下均为不限额；5,422 字符报告截断至 600。
- 收尾预测接入宿主 token meter（`budget.js` pre-step 与 `agent/request` 同一基准，取实测与字符估算的较大者）后，911 场景在实测估算（≈31,000／调用）下不再触发提前收尾；既有「110,000 剩余 vs 70,000 输入必须收尾」下界测试保持绿色，储备规则与 outputLimit 联动未改。
- 返工限额新增 `reworkBudgetMode`（默认 `unlimited`，保持 0.7.7 语义）与 `fixed`（配合 `reworkTokenLimit`／`reworkCallLimit`）：fixed 返工与首轮 worker 共用受限路径（预约、收尾、逐调用账本、链式血统与冷恢复校验），无效配置、发放与宣告不一致均在子会话启动前拒绝，且不终止原交付。
- `dpswarm_report(item_id, offset, limit)` 从审计账本分页读取完整 worker 报告：分页拼接一致、越界、未知 item、非 root 身份、空报告与冷恢复均有测试。
- 收尾估算的路由解析按会话缓存（`budget.js`）：冷恢复子会话不再每次 pre-step 全量重读审计账本。
- 控制面三处持久化／恢复接缝修复（`dpswarm-plugin/tests/test_p2_recovery_fixes_20260909.py`，15 项）：证据文件在事件事务前 fsync（崩溃后 accept 不再因 `EVIDENCE_NOT_READABLE` 断链）；seal 的 CUTOFF／SETTLEMENT 断点可幂等续走（两事务间隙崩溃不再永久卡死，终态重入仍拒绝）；迟到 cleanup 确认可对已终止 item 落账、delegate 门改按各执行会话最新失败观测判定、且 cutoff 仅因未确认清理时随确认同事务安全恢复 OPEN（手动 cutoff 不回退）。JS 侧子 agent disposal 增加有界重试（0/250/750ms，取消即停），瞬时清理失败可直接确认。
- 罗盘弹层重构为「主开关 + 关键参数 + 按角色分段的总 token 占比条（悬停查看各角色明细，含各自 CM；Lead 主对话不计量）」；子代理执行状态明细移入控制面板新增的「子代理执行状态」卡片。

浏览器检查（设置页返工卡片、合并开关、弹层新版面、控制面板新卡片）：`tests/client-browser.mjs` 预期已同步（合并开关启停、隔离、缺配置拦截、开启后轮询），但本机无 Playwright／esbuild，浏览器套件未运行；仅做了 SSR 冒烟渲染（三个 slot 均通过）。安装后需人工核对开关与保存交互。原生 DPH 探针未重跑。真实模型的服从程度、质量和成本收益仍未测量；911 日志仅作动机证据，不计入工程验证计数。

---

# 0.7.7 角色提示与不限额返工验证（2026-09-09）

插件回归 **307/307**、控制服务 **521/521**、浏览器 **4 项**、原生 DPH **4/4** 场景通过。首次 worker 额度保持原设置；返工不限累计 token／调用次数，旧用量不清零；修改由原模型实现者完成。外部模型调用为 0，真实模型的服从程度、质量和成本收益未在本次测量。详见 [本版记录](../reports/2026-09-09/worker-rework/README.md)。

---

# 0.7.6 Worker 收尾与诊断验证（2026-09-09）

插件完整回归 **276/276**、控制服务 **520/520**、原生 DPH **4/4**、浏览器 **4 项**检查通过。所有原生验证使用本地确定性模型，外部模型调用为 0。详见 [本版验证记录](../reports/2026-09-09/worker-closeout/README.md)。提前收尾不增加用户额度；候选文件、报告和 Lead 验收分别记账。

---

# 0.7.4 显式团队与预算验证（2026-09-08）

完整插件回归 **229/229**、控制服务 **512/512**，以及 **5/5** 原生 DPH 场景通过。手动额度原样执行；Auto 由当前 Lead 决定；无限制不采用残留旧值。新增宿主工具／Code Mode／终止边界检查，阻止勾选团队后直接单干。外部模型调用为 0。

详细口径、原生案例与源码哈希见 [0.7.4 验证记录](../reports/2026-09-08/team-required/README.md)。此处是工程验证，部署需在宿主空闲时单独安装配套 JS 和 Python；不改写历史实验版本或结论。

---

# 0.7.2 有效 Lead 路由与恢复验证（2026-09-08）

本版工程验证的原始门槛为本地归档 `.tmp/effective-route-deploy-20260908/GATE_READY.json`，完成于 **2026-09-08 04:06:51 UTC**。公开 [SYNC_VALIDATION.json](../reports/2026-09-08/SYNC_VALIDATION.json) 重新核对了 44 个插件源文件及各验证凭据的哈希；发布时没有再运行这些测试。以下工程探针均使用本地受控模型，**外部模型调用为 0**。实际鹈鹕实验单独发布。

| 验证 | 已执行结果 | 范围与限制 |
|---|---:|---|
| Node 插件完整套件 | **143 通过，0 失败，0 跳过** | 设置、团队生命周期、CM、worker 额度与独立审计，新增有效路由、身份、防重放及冷恢复合同 |
| Python 控制服务完整套件 | **479 通过** | 原有控制逻辑、独立审计的 CAS／哈希链／锁、route-bound 类型与身份字段验证 |
| 当时的实验恢复／采集套件 | **73 通过** | 0.7.2 部署门槛中的批次测试；早于后续 collector-4 修复，不能当作当前采集器复跑 |
| 原生固定团队闭环 | 通过 | 实际 Lead 工具决定不同 worker 额度，真实 Python sidecar 与两个原生 child；大默认输出下完成派发，中途设置变更不改变冻结配置，交付验收后释放 lease |
| 原生 worker 生命周期 | 通过 | 手动、不限、实际 Lead 决策、独立累计、缺决定及重复授权拒绝 |
| 预算与 CM 的进程冷恢复 | 通过 | 独立宿主和 sidecar 重启，未知预约仍占用；原生历史可冷读，插件自定义事件不再写入 DSH 原生日志 |
| 实际 Lead 路由 | **2 个场景通过** | 原生 session.selectModel 后，Flash Lead／实现者均收到 max；GLM 未指定强度时两者均不继承启动值；显式 Tester 与 CM 路由保持自己的设置 |
| 路由的进程冷恢复 | **2 个场景通过** | 新进程通过公开 agents.resume 恢复冻结路由；缺失或重复绑定在 provider 前拒绝；核对实际请求头与适配器收到的配置 |
| 设置客户端 | **复用此前 6 项实机检查** | 客户端源码字节一致；本次没有重跑浏览器，不计作新增 6 项验证 |
| 后续 collector-4 修复 | **另有 33 项离线测试通过** | 路由审计类型、作品输入与采集证据链；独立凭据已核对。不是上面 73 项的一部分，也不新增模型调用或实验尝试 |

本版修复了两个实际宿主差异：Web 的对话选择不等于 agent 创建时的 options；创建 child 时携带的 reasoningEffort 也不保证会进入最终原生请求。现在从当前 Lead 的真实请求头读取路由，在控制面绑定实际 child、持久化 `dpswarm/route-bound` 后才放行首个请求。缺省强度明确移除旧值。冷恢复只接受与 root／child／角色相符的独立绑定，并与已有原生请求头交叉核验。

旧 0.6／0.7.1 的“跟随对话”检查未覆盖上述选择漂移，不能据此声称旧版本已实现相同行为。旧缺绑定会话继续执行会被拒绝，但不改写其原生记录或把失败自动换成新尝试。预算计量与 CM 采用语义保持独立。

本地原始入口：`node-latest.json`、`python-latest.json`、`batch-latest.json` 位于 `.tmp/effective-route-deploy-20260908/`；原生路由与冷恢复记录分别在 `.tmp/lead-effective-route-native-probe-20260908/latest.json`、`.tmp/lead-effective-route-cold-20260908/latest.json`。collector-4 的独立记录为批次本地归档 `evidence/heartbeat-20260908T0432/COLLECTOR_REPAIR_VALIDATION.json`。公开 JSON 保留这些凭据的哈希与口径，未打包私人日志、安装元数据或凭据。

安装和常规测试命令见 [README](README.md)。原生探针脚本位于 `tests/`，需要真实 DSH 和隔离 profile／sidecar；不能当作普通网页测试在日常任务中加载。工程检查证明这些受控路径按预期运行，不证明外部 Provider 额度、所有中断的物理取消、摘要语义无损或机制净收益。

[鹈鹕实验快照](../reports/2026-09-08/pelican/README.md) 与 [动画对比页](../reports/2026-09-08/pelican/index.html) 保留实际模型任务的另一条证据链，不计入 SWE 评分，也不与本文件测试数量合并。

---

# 历史：0.7.1 预算与持久审计修复验证（2026-09-08）

以下保留 0.7.1 当时的工程证据；不替代上方 0.7.2 的当前路由验证。外部模型调用数为 0，受控模型仅用于宿主集成验证，不代表动画质量、团队增益或 CM 净收益。

| 验证 | 结果 | 核验内容 |
|---|---:|---|
| Node 插件完整测试 | 111 通过，0 失败，0 跳过 | 独立 worker 限额、Auto 决策、模型设置、团队交付/复核、CM、独立审计 |
| Python 控制服务完整测试 | 470 通过 | 原有控制逻辑、证据绑定、恢复与新审计 CAS/哈希链/进程锁 |
| 原生固定团队 | 通过 | 当前 Lead 正常工具决定实现者 80,000/40、测试者 20,000/10；默认输出 256,000/32,768 时两名 worker 均实际进入本地 provider；中途设置变更不改本轮已冻结额度 |
| 原生逐 worker 生命周期 | 通过 | 独立手工额度、Lead 不受限、无限额忽略残留值、实际 Lead 分配、原生 spawn、额度累计与授权防重放 |
| 真正进程冷恢复 | 通过 | 强停自建宿主和 sidecar 后重启，使用公开 agents.resume；未结算 80,000 预约及 1 次调用保留；后续请求在 provider 前拒绝；未绑定授权恢复后仅绑定一次 |
| 自动 CM 与原生日志 | 通过 | 正常 hook 自动触发 CM；5 会话共 128 条记录全部为 DSH 原生已知类型，0 条插件自定义原生事件；冷读后原消息和压缩记录哈希保持 |
| 48 格恢复与采集边界 | 48 个离线样本通过 | 33 个恢复/模型漂移/调度样本，15 个独立审计采集样本；后续排除 V4 Pro Lead |

预算额外边界包括：完整系统提示与工具封装计入预约，结算超出预约后停止下次请求，结算写入失败保留未知预约，session override 实际生效，Auto 任务标记篡改及子 worker 伪造分配拒绝；同进程和双 runtime 预约竞争均有检查。CM 的两个 runtime 争抢最后一个 turn 槽时只有一个准入，其他 agent 的额度独立。

Python 首次全量检查因系统 pytest 临时目录 WinError 5 出现 337 个 setup 错误；使用仓库内全新隔离临时目录复跑后 470 项全部通过，未改系统目录权限。Node 控制台摘要曾遇编码问题，但已保存的进程退出码为 0，完整日志为 111/111，通过状态与源码哈希另行核对。

可复核的本地证据：`.tmp/worker-budget-failure-recovery-20260908/NODE_FULL_VALIDATION.json`、`.tmp/worker-budget-python-validation-20260908/latest.json`、`.tmp/worker-budget-native-recheck-20260908/{fixed,loop}-latest.json`、`.tmp/worker-budget-audit-cold-20260908/latest.json`。这些是工程验证；新实验实际使用版本、部署哈希和模型用量另由该批次 `HOST_VALIDATION.json` 与逐组日志记录。

旧 0.7.0 日志的原生冷读取不受支持，不能把独立解码导出称为原生恢复成功。旧实验 15 根＋12 子会话原字节已经保存；失败和未知用量不会被新版本抹去。生产安装需要等待所有在途任务结束并核验文件版本。

---

# 固定团队与 CM 插件 0.5.0 验证记录

日期：2026-09-07。范围：DPH 模型目录选择、Reviewer 默认／独立路径、DPswarm 设置页、固定团队入口、可配置模型的 CM、安装与原生请求/回放合同。没有调用外部模型，没有修改或重跑既有实验；安装和宿主联调使用隔离 DSH home。

| 验证层 | 结果 | 说明 |
|---|---|---|
| Python 控制面 | 0.2.0 阶段全量 435 passed | 本次仅更新 bridge 的 CM 归属说明；会话服务与 Node 调用真实服务的集成检查仍覆盖该边界 |
| Node 插件合同和集成 | 72 passed，0 skipped | 原 68 项，加 4 项 Reviewer 默认无额外调用、独立路由冻结、配置拒绝与取消检查；真实 Python sidecar 集成也覆盖三份交付与逐项验收；实际使用安装的 DSH Session、TokenMeter、CompactionEngine、Cordis API |
| Chromium 界面行为 | 24 项行为检查通过，无 page error | 实际客户端与 React；目录/设置使用 fixture。涵盖四个角色选择、Reviewer 沿用 Lead、搜索与键盘、强度兼容、原子保存与失败草稿、目录部分失败/过期保留、自定义路由、标签页、会话开关及 390px 深色界面 |
| 安装 | 隔离 DSH home 与含空格运行目录安装成功 | Python 私有目录打包安装，pnpm 正式登记 bundle，默认不开启任务 |
| 真实 DSH Web | 9 项检查通过 | 原生设置页；通过真实 `llm.models` 目录选择四个角色并跨刷新保留准确的 Provider、模型、强度；Reviewer 可切回 Lead，Escape 不关闭宿主设置；测试修改的 13 个字段已恢复 |
| 真实 DSH 请求链 | 5 个用例通过：主会话、直接子 agent、关闭对照、minimal 预设、手动指定 CM 模型与 max 强度 | 正式安装包与真实 AgentLoop/预设/持久化；本地可控 LlmAdapter，核对实际发出的下一次请求，不依赖伪造 Session |
| 安装自检 | 宿主定位、Python 与新版 sidecar 导入成功 | 自检不会证明 Provider 连通、额度充足或实际模型执行成功 |

## 本次模型选择与 Reviewer 改造

- 宿主原生 `settings.section` 注册独立页面，拆为“模型分工 / 运行规则 / 高级设置”。可搜索的模型窗口读取 `llm.models`，模型与 Provider 一起选择；支持宿主推理元数据与手动配置。
- Lead 沿用实际宿主主模型，通过宿主模型选择器调整；已有团队的根模型约束仍保留。
- 实现者、测试者、Reviewer 和 CM 的 Provider、模型、推理强度每组一次提交到 `settings.mutate`（Reviewer 同时提交 mode），使用宿主 revision 防止并发覆盖。只在宿主返回所提交的字段后显示保存成功；失败明确展示，保留未保存草稿。
- 保留 `cmModel`、`cmEffort`，旧配置保留 DeepSeek v4 Flash / off 默认值；空强度不向宿主传入伪造值。实测本地适配器请求收到用户配置的模型和 max 强度，审计也记录同一路由。
- 配置更新不启用团队或 CM。团队运行中保持冻结配置，重启恢复后也不改变，验收完成后新配置才用于下一团队。

- Reviewer 默认沿用 Lead，不产生第三个子 agent。显式选择独立模型后，测试者清理完成才创建 Reviewer，带入原任务与两份未信任报告；只读是角色约定。其报告进入原有交付/验收链，无权自动接受前两份交付。全部验收完成前保留项目 lease。

## 关键合同

- 关闭时，即使模型直接调用运行工具，也在启动服务和创建 worker 前拒绝。
- 实现者交付并完成物理清理之后才启动测试者；运行中的角色和服务地址保持最初配置。
- 公开 SubagentRun id 绑定到真实执行身份；提交必须等结果和 dispose，未知 usage 保持 null。
- 同一项目未完成验收时，另一会话无法开始覆盖运行；控制面状态按宿主 session 分开。
- 清理不确定时保留根封存与项目 lease；不把逻辑资源释放误当物理执行已停止。
- 主机结束后，已验收但未删除 lease 的情况可以在原会话重复确认原决定并释放；不会追加第二次验收事件。
- Lead 的自由文本说明单独存入验收事件或终止摘要，终止原因仍使用控制面规定的词汇。

## 发现并修复

设置页联调核实，宿主 `SettingsScope.set()` 在写入被拒绝后可能刷新并正常返回，不能仅凭 Promise 完成显示“已保存”。新页面使用公开原子写接口并核对成功响应，接口拒绝、传输失败和不可写状态都不会被显示为保存成功。

真实服务联调发现，旧接口把自由文本说明传入 terminate 的枚举原因，导致不能完成终止；现已分字段处理，并在真实事件上验证记录保留。Windows 安装验证发现大小写不同的 PATH 键导致 pnpm 丢失；现已保留原键并验证隔离安装。包清单比对还发现 pnpm 本地 file 依赖在同版本更新时会漏掉新文件，安装器已改用带内容哈希的固定包快照，避免同路径缓存漏文件，并核对全部发布文件。界面增加了服务已就绪但尚无任务的状态，并对窄屏弹层做定位约束。

## CM 验证要点

- 正式安装包通过独立 compaction realm 加载；标准预设仍使用原来的 `BasicCompactionEngine`。主会话、继承标准预设的直接子 agent、minimal 预设均可运行 DPswarm CM，关闭对照不发出 CM 请求。
- 通过真实 AgentLoop 发出的后续模型请求核对：被压缩的旧记录退出可见历史，近期记录与摘要仍在；原始事件没有删除。JSON 回放重建相同的模型可见历史。
- 空摘要、输出截断、缺结束标记、未知/负值用量、工具输出、Provider 失败、摘要变长及并发历史变化均拒绝采用；未结束/未知调用不归零。
- 硬超时和关闭开关会结束等待；即使测试 Provider 故意忽略取消，迟到输出也只补用量，不会晚到改写。原请求尚未结束时，同一会话不会并发新 CM 请求。
- 12 次 CM 请求上限随会话日志恢复；团队冻结记录带真实所属 session，验收后释放。普通用户 fork 不继承团队冻结或开关，继承的历史账目不重复算成子 agent 消耗。
- DSH 的输入与缓存用量是分开的。这里只记录观察值和历史估算，不把一次上下文缩短算成净节省，也不把本地响应当作线上模型结果。

## 尚未证明

本次未进行线上模型任务的完整求解，新增独立 Reviewer 尚无收益对照，未实测 GLM Coding Plan 路由、主 agent 是否遵循协作指引、补丁质量或成本收益。浏览器 fixture 中的开关与取消合同不代替真实 Provider 取消验证。角色文件所有权仍是提示约定，不是 worktree 或文件 ACL。CM 的原生历史接入已验证，但未测线上 DeepSeek 连通、长会话摘要语义、缓存效应及净费用收益；可控摘要响应不能证明语义无损或实际模型效果。

## 复现与本地证据

控制面与 Node 测试命令见 [README](README.md)。浏览器脚本为 `tests/client-browser.mjs`，需提供本机 esbuild、Playwright 模块入口；宿主共享依赖通过 `DSH_HOST_ROOT` 或自动发现取得。

0.5.0 的临时证据在仓库 `.tmp/model-settings-20260907/`；0.4.0 证据在 `.tmp/settings-ui-20260907/`；前版证据在 `.tmp/plugin-mvp-20260907/` 与 `.tmp/cm-integration-20260907/`：浏览器行为 JSON、桌面/窄屏截图、真实 DSH 设置页截图和隔离安装目录。它们不进入 npm 包或公开实验报告。此记录描述工程验证，不作为新增 benchmark 结果。

原生请求链复现脚本为 `tests/cm-host-probe.mjs`，仅应作为额外插件加载到隔离 profile：设置 `DPSWARM_CM_PROBE_OUTPUT`，通过额外 overlay 关闭 `llm-deepseek`、`llm-pi-ai`、`session-title-llm`，插入脚本的文件 URL，再启动 DSH Web。脚本使用本地 `dpswarm-cm-fixture` 适配器，临时启用自己的测试会话，记录实际请求，释放创建的 agent 并恢复设置。不要把该测试插件安装到日常 profile。

0.5.0 真实设置页复现：在上述隔离宿主完成请求链探针后，设置 `DPSWARM_TEST_URL` 为本地地址、`DPSWARM_TEST_PLAYWRIGHT` 为模块入口，运行 `node tests/settings-host-browser.mjs`。探针适配器向真实宿主目录公布固定测试模型；浏览器从这个目录选择四个角色，验证跨刷新配置，再恢复自己修改的 13 个字段。测试不修改凭据或启动团队。原生 CM 请求链与目录/界面检查使用本地确定性模型响应，不能替代实际 Provider 的效果验收。


## 0.5.1：运行面板改版与日常 DPH 设置落地（2026-09-07）

本轮对 `dpswarm/static/index.html` 重做布局，并将原生设置快捷入口置于插件卡片和任务菜单。没有重跑效果实验，未调用外部模型。

- 运行面板 Chromium 检查：指标与未知费用、只读加载、键盘切换、团队表、活动筛选、原文安全呈现、保留展开状态、刷新保留草稿、保存失败、POST 调度、停止确认、离线禁写、未启动会话、会话隔离、明暗主题与窄屏布局。
- 现有模型设置 Chromium fixture 24 项仍通过；真实隔离 DSH 的四角色模型选择、保存、刷新及恢复通过，新增原生设置链接验证。
- Python 控制服务、鉴权、页面注入和会话服务回归：63 passed。
- 日常 DPH 从插件 0.1.0 更新为 0.5.1；重启前确认 44 个任务均无运行中的模型。实际浏览器确认原生 DPswarm 子页面、四角色配置、Reviewer 默认 Lead、11 个已有目录选项，以及插件卡片/任务菜单直达入口。
- 日常设置保留按任务默认关闭。旧控制服务 8791 仍有 1 项待验收交付，保持原进程与记录；新版设置选择空闲的 8792，按需启动独立服务。没有调用旧任务停止或验收接口。

证据位于工作区 `.tmp/panel-redesign-20260907/`：`browser-results.json`、`native/browser-host.json`、`daily-validation.json`、`daily-browser.json`、安装日志及界面截图。截图与模型目录核验不代表模型质量或协作净收益。


## 0.6.0：实现者默认沿用当前对话（2026-09-07）

本节保留当时的验收记录；未覆盖后续发现的原生模型选择与推理强度投影差异，当前保证以上方 0.7.2 原生路由探针为准。

- 默认模式解析真实父对话的 Provider、模型与推理强度，作为实现者请求的精确路由；每次启动固定配置，新一轮重新读取。显式模型配置覆盖继承；旧版完整手动路由保持原意。没有真实对话模型时拒绝执行，不换用 GLM。
- Node 合同与本地服务集成：75 passed，0 skipped。新增不同对话、冻结配置、切换后的下一轮、遗留显式路由、缺失主模型与真实控制面路由记录核验。
- Chromium 模型设置检查：26 项通过，新增默认继承、手动覆盖、整组保存切回继承与推理强度界面校验。
- 实际隔离 DSH：四角色模型选择、保存、刷新、实现者返回跟随对话及 14 个配置字段恢复通过；原有 CM 宿主探针 5 个场景通过。
- 证据：工作区 `.tmp/impl-inherit-20260907/node-tests.log`、`browser/results.json`、`native/browser-host.json`。这些是实现与设置合同核验，不是模型质量实验；无外部模型调用。


## 0.6.0：团队开关缺项提示修正（2026-09-08）

- 本机复现：实现者已默认跟随对话，测试者只有模型名、没有 Provider，导致团队开关禁用；完整的 CM 配置不受影响。旧提示错误地要求补齐“两个角色”，现按实际启用模式列出缺少的角色和字段。未配置完成的模型卡片显示“待配置”。
- 展示与保存前校验共用相同的缺项规则。沿用对话的实现者和沿用 Lead 的 Reviewer 不要求显式路由，手动选择的角色仍须保存 Provider 与模型；关闭开关仍可取消任务。
- Chromium fixture 共 31 项通过，新增测试者缺 Provider、继承角色无须补填、CM 独立可用、未保存选择不解锁、只保存测试者即可解锁团队的检查。
- 日常 DPH 的真实设置和页面核验 9 项通过：测试者绑定宿主目录中唯一匹配的默认模型 Provider 后，团队与 CM 开关均可操作；实现者继承、Reviewer 默认和其余配置保持不变。修复仅写入缺少的 `testProvider`，未改动任务开关，未调用外部模型。
- 客户端通过宿主现有文件服务与热更新加载，核对实际返回内容与源码哈希一致；未重启 DPH、未停止任务，9 个服务端模块保持不变。

证据在工作区 `.tmp/team-toggle-20260908/`：`config-fix.json`、`browser/results.json`、`client-deploy.json`、`daily-validation.json` 与页面截图。此次验证说明设置和开关可用，不代表完成了线上团队求解或验证了 Provider 额度。


## 0.7.0：每个子 agent 的额度与真实 Lead 决策（2026-09-08）

- 验证 unlimited 忽略留存数值、手动额度独立计量、Auto 无隐藏模型调用、实际 Lead 工具记录决定、任务与真实 child 身份绑定，以及缺少决定／重放／改写／跨根／fork 的拒绝路径。
- 固定团队启动策略按角色冻结。运行中修改模式或数值，不改变后续 Tester／Reviewer；同一 Lead 的普通原生子任务继续使用实时设置。已绑定 child 重启不重置，运行结束拒绝未绑定的旧凭证。
- 真实 DSH `defineTool` 注册和嵌套参数校验通过，修复了 property-spec required 与 JSON Schema 写法的差异。状态与团队输出剔除 undefined 字段，避免宿主拒绝非 JSON 输出。
- Chromium 设置交互 20 项通过，覆盖三模式、手动原子保存、无效输入、失败保留草稿、刷新、根会话覆盖、390px 深色布局。
- 原生 DSH AgentLoop、真实 standard preset、当前 Lead 工具调用和 native spawn 已在隔离宿主通过，使用本地响应适配器，外部模型调用为 0。验证每 child 独立限制、Lead 无限制、缺决定／重复决定在请求前停止、原生默认输出不被提高。
- 证据：`.tmp/worker-budget-host-20260908/`、`.tmp/worker-budget-settings-20260908/`。完整团队与日常宿主部署核验记录将在最终检查后另行追加；上述检查不等于模型质量或机制收益实验。


0.7.0 最终宿主闭环：真实 Lead `dpswarm_models → dpswarm_run → dpswarm_review → dpswarm_status`、真实 Python 会话控制面与两个原生 child 已通过。两个角色采用 Lead 选择的不同额度；实现者开始后更改全局手动额度，测试者仍沿用原 Auto 分配；两项交付验收后项目 lease 释放。此探针使用本地响应，外部模型调用为 0。日常 DPH 预算设置另有 6 项实际页面检查通过（保存、重新进入、三模式与其他角色/开关保留），证据 `.tmp/worker-budget-host-20260908/daily-budget-ui.json`。

旧默认运行目录有 8 项待处理 lease，原目录与旧记录完整保留。0.7.0 本轮使用独立运行目录 `tihu test/dph-budget-comparison-20260908/runtime` 与本地 8793 控制服务，通过安装器支持的独立状态目录安装；不是强行清除旧任务锁。日常 Settings 显式记录新运行目录、Python 包路径与端口；迁移前后值保存在 `runtime-migration.json`。新实验只有完整通过代码、宿主和设置哈希门槛后才派发。


## 0.7.3：DPH 模型注册接入（2026-09-08）

源码检查已通过：Node插件154项，Python控制服务511项；包含新模型不依赖AA即可进入固定团队、Provider撤销、角色推理强度、已交付结果在撤销与重启后继续验收。另有4个原生宿主/冷恢复受控场景通过；均未调用外部模型。新增跨语言集成使用真实本地HTTP侧服务与Fixture子角色，不能称作真实模型效果实验。

可复现入口：`node --test dpswarm-dsh-plugin/tests/*.test.mjs`、`python -m pytest dpswarm-plugin/tests`。本轮原生输出独立存入 `tihu test/dph-timeout60-teamcm-20260908/upgrade-evidence/20260908T104247Z-e16e8c99/PROBES.json`，旧0.7.2记录未覆盖。宿主安装与切换已由本批专属 `HOST_VALIDATION_0.7.3.json` 核验：安装文件与源码一致，宿主和新 sidecar 均在安装完成后启动。已发布[去除本机配置的验证摘要](../reports/2026-09-08/host-model-registry/README.md)；此前实验记录保持原版本。


## 0.7.5：按模型窗口压力触发 CM

[诊断与验证报告](../reports/2026-09-08/cm-window-pressure/README.md)记录原生会话在约4.6%–5.5%窗口占用时被旧12K策略提前压缩的问题。修复后按各角色模型窗口80%触发，保留兼容的usage校准、最小选区保护、角色路由及未知容量跳过。完整Node回归248/248、5类隔离原生场景通过（6个agent）；没有外部模型调用，未部署到正在运行的日常宿主。

## 2026-09-11：预算输入、收尾请求与零调用重试兼容修复

完整源码回归 **457/457**、基于实际安装 0.9.7 的候选包与更新后的安装路径回归各 **185/185** 通过；无跳过，无外部模型调用。预算按原生请求生命周期计入待提交消息与系统包装，收尾校验兼容实际 system-role messages，完整零调用预算准入失败可用新 attempt 重试；保留硬预算、清理及真实执行失败的接管约束。六文件最小补丁已更新到本机安装，运行中的 DPH 仍需重启载入；未将本地模拟回归宣称为真实供应商端到端验收。详见 [修复记录](../reports/2026-09-11/team-budget-closeout-fix.md)。

## 2026-09-11：准入补偿、工作区预检与返工复验

源码插件 **502/502**、Python **691 通过/1 跳过**，实际安装 0.9.7 的兼容候选和安装路径分别 **326/326** 通过；外部模型调用为 0。修复返工占用、准入后空任务泄漏、预算数组参数、调用模型前的占用检查及当代评审关联。九文件最小补丁已安装；历史空任务已正常终止，运行中的 DPH 和 Python 控制服务仍需重启载入。详见 [修复与验证记录](../reports/2026-09-11/team-lifecycle-fix.md)。


## 2026-09-12：运行协议握手、基础设施受阻与安全恢复

源码 JavaScript **542/542**、基于实际 0.9.7 的兼容候选及安装路径分别 **366/366** 通过，均无失败、无跳过；Python **692 通过/1 跳过**，唯一跳过为未设置 `DPSWARM_TDAI_ENDPOINT` 的真实网关检查。新能力握手核验进程已加载的审计事件及准入清理协议，在工作区租约、预算、业务写入及模型调用前拒绝旧运行进程；不会把磁盘包版本或 `plugin_audit_v1` 单个标志当作兼容证明。

基础设施错误保留原任务及已发布 worker，允许正常受阻收尾，不强制继续模型调用或重复团队派发。审计不可写时使用任务绑定的 HMAC 拒绝执行记录；恢复前校验账本连续性，账本回退及错误处理再次失败不能抹掉已发布 worker。旧启动记录通过控制面身份关联清理证据，只有控制面终态与物理清理均成立才可结算。

8 文件最小补丁已安装并核对哈希，保持包版本 0.9.7。副本演练后，已实际重启 Python（PID 31324 / 8795）与 DPH（PID 52328 / 3080），核验运行能力、监听 PID 及认证页面 HTTP 200。原会话通过正常 terminate 流程收尾至 finished / failed_takeover：工作槽位 0、仅 root 剩余点数 1、CM 团队结束、目录租约释放。HTML 与设置哈希一致；原审计前缀和 134 条原生记录保留，DPH 打开会话只追加一条 end-seed 元数据。本轮工程验证、演练和实际恢复外部模型调用均为 0；终止旧任务不等于 HTML 通过独立验收，也没有执行新的真实供应商团队任务。 详见 [运行协议与恢复记录](../reports/2026-09-12/runtime-protocol-recovery.md)，完整收据位于 `.tmp/runtime-protocol-fix-20260911/`。此前各阶段记录保持原样。


## 2026-09-12 acceptance contract repair

Implementation and live-test boundary: [repair record](../reports/2026-09-12/acceptance-repair-implementation.md).
Protocol tests: acceptance-native-bridge.test.mjs, acceptance-contract.test.mjs, candidate-snapshot.test.mjs, write-scope-acceptance.test.mjs, report-repair-budget.test.mjs, and Python test_native_acceptance_20260912.py.
Cross-language fixtures use real HTTP/EventStore but deterministic mock model output. Actual Luna Max pelican generation runs separately through Codex; the local DPH has no configured exact Luna route. Final JavaScript suite: 589/589 passed, including 13 real HTTP bridge cases and 9 report-repair budget cases. Python full suite: 714 passed / 1 skipped; after the final audit vocabulary update, 73 related Python tests passed. The Luna Max pelican browser regression passed at 721 cycle samples with zero runtime errors or external requests. This is a known-bug, non-blind case; it is not a production deployment or a native DPH model-quality run.


## 2026-09-12: 0.12.0 request accounting, report recovery and completion facts

The 09291d59 repair aligns normal/final/compatibility-recovery request estimates with actual tools and system-message projection; exports the real report-repair service using the original remaining grant; distinguishes final reports from progress/truncation/legacy text; exposes versioned report schema and current unknown/blocked template to the Lead; validates report syntax/bindings before takeover and surfaces partial commits in error messages. Completion facts separate execution, current acceptance and filesystem lease ownership, including amendment/evidence invalidation and cold controllers. A single factual closeout notice permits honest partial/blocked endings rather than creating another team-required retry loop.

Full JavaScript regression: 633/633 passed, zero skipped. Relevant authoritative Python regression: 81/81 passed; Python source/runtime files are unchanged. Real native-loop fixtures use deterministic provider responses, not external model calls. The first full test invocation used the wrong repository cwd for three harness paths and exposed four stale system-message pricing assertions; both were corrected before the complete passing run. Tests now compare the native committed request surface rather than fixed token constants.

Installation and live loading evidence is tracked separately in `reports/2026-09-12/harness-fix-0.12.0-install.md` and `.tmp/harness-fix-20260912/`; passing these checks does not accept the historical animation candidate or prove a real-model task succeeds. Historical report-only recovery preserves old reports as unclassified text; no audit record is rewritten. Budget forecasts cover one refused tool attempt followed by a report, with uncertainty; repeated refusals or unusually large arguments can still hit the unchanged hard limit.


## 2026-09-12：0.13.0 候选恢复

最终完整 JavaScript 回归 684/684 通过，0 跳过；相关 Python 验收、审计、恢复协议与会话回归 107/107 通过。真实 Python 控制服务与原生派发边界的候选/验收集成 31/31（已含在 JS 总数中），包括外部观察路径、封版失败保留交付、原未发额度恢复、重复/并发拒绝、任务/配置/租约变化、部分提交、取消和清理失败。未调用外部模型。

第一次全量回归仅因工具注册测试仍使用旧工具清单失败；补入新增 dpswarm_resume 后完整重跑通过。恢复过程不会替本次旧会话验收；claim 后重启重试仍有明确边界。安装状态与最终加载证据见 reports/2026-09-12/session-ec26c449-recovery-fix.md，包内回归通过不等于已部署。


## 2026-09-12：0.14.0 续接、返工决策与证据读取

[本轮修复与验证记录](../reports/2026-09-12/session-5479d74d-fix.md)覆盖同任务续接、相同候选延期验证、冻结额度下的无工具收尾、证据解码分页、输入能力提示与用量展示。真实 Python journal 的续接往返验证合同不变、0 新 worker、冷重放与幂等；真实候选与预算链验证相同内容只派一次实现者、后续验证只能领取未发行授权、清理不确定时停止派发。历史旧快照缺入口的兼容分支单独覆盖。

读取工具沿用宿主 fs，并接入 staged read 门禁：未 ready 拒绝，ready 时读封版快照，成功读取才记录消费版本。覆盖 UTF-16 数值、权限拒绝、跨页变化、无 Base64 的工具摘要及可完整还原的中文报告。模型能力来自宿主精确路由；元数据缺失或超时为 unknown，取消仍停止。模型路由未替换。

定向真实原生请求验证 TRANSPORT 失败缺 usage 的预约保留、重试重新计量、无工具最终报告，以及报告修复使用剩余 211,353 token / 9 次调用读取 UTF-16 结果后交报告。这是可控 provider fixture，无外部模型调用；不是实际供应商质量/效率测评。

最终完整 JavaScript 回归 **731/731 通过，0 跳过**；Python 完整回归 **740 通过、1 跳过**，跳过项为未设置 DPSWARM_TDAI_ENDPOINT 的真实网关测试。首次 JS 为 723/725，两个旧预算断言更新后，加上独立审查发现的输入预约和读取门禁回归，再完整重跑通过。安装与加载状态单独见修复记录；历史验收记录不随升级改变。


## 2026-09-12：真实鹈鹕冷启动修复 0.14.1

首次真实新会话在第一个模型请求前以 ROOT_MODEL_REQUIRED 结束，模型请求和用量均为零：新增能力提示在 system prompt 装配阶段过早要求已解析请求路由。0.14.1 在实际请求头尚不存在时省略可选能力提示，首请求完成后仍只使用已解析的精确路由；正式派发的路由校验保持不变。冷请求头与可控原生宿主回归另行记录，真实实验保留这次失败，不将后续修复后的结果标成原0.14.0通过。


## 2026-09-12：0.14.2 收尾伪调用检测与 Lead 明示

动机：pelican-014 真实回归（`reports/2026-09-12/pelican-014-live/`）中，三个初始角色在 final-only 收尾的最后一步输出 DSML 伪工具调用文本而非报告——工具已被预算轨移除，这些"调用"从未执行；tester/reviewer 报告全部解析失败（`REVIEW_REPORT_INVALID`），implementer 的收尾修改实际未落盘，而 Lead 侧只有 `report_available: true`，必须读全文才能发现。0.8.3 曾以"保留工具 schema、执行层拒绝"修复同一根因；0.12.0 起请求会计对齐后设计回到无工具收尾。本版不改该设计，按 live 报告改进方向 1 让失败显式化。

改动（纯叠加，不动预算准入与会计）：

- 新 `lib/output-nature.js`：`pseudoToolCallMarkup(text)` 检测两族伪调用标记——实测的 `<｜｜DSML｜｜ …>` 标签族（含 invoke 工具名提取）与 provider 特殊 token `<｜tool▁…｜>` 族；建议性标记，不阻断、不改写、不执行。共享指引文案 `PSEUDO_MARKUP_GUIDANCE`。
- `worker-diagnostics.js`：诊断 `closeout.output_nature` 新增伪标记分类、末步真实工具调用布尔、是否预算轨移除工具；生成时按全文计算并随 `dpswarm/worker-diagnostic` 入审计。compact 视图投影该字段，检出时附 `lead_note`（这些调用从未执行、结构报告可能缺失/无效、考虑 dpswarm_repair_report）；无该字段的旧账本条目按文本重算（`recomputed_from_text` 标记），不改写历史记录。
- `acceptance-runtime.js` `record()`：报告解析失败时 `report_error` 增加 `output_nature` 与 `guidance`；发往 Python 控制面的 evidence 载荷不变。
- `fixed-team.js` `report()`：分页视图对完整文本计算 `output_nature`，检出时 `note` 附同一指引。
- 提示强化：`CLOSEOUT_INSTRUCTION` / `BUDGET_POLICY_INSTRUCTION` / worker 指引点名 DSML/invoke 标记"是纯文本、什么都不执行、会被标记为未执行"。

验证：完整 JavaScript 回归 **745/745** 通过（仓库根 `node --test dpswarm-dsh-plugin/tests/*.test.mjs`，含此前单独通过的 host-dispatch-integration）。新增 12 项：`output-nature` 6（三个真实样本逐字裁剪段、provider token 族、阴性组、围栏内引用的文档化行为）、`worker-diagnostics` 3（DSML 收尾分类、干净文本无标记、旧账本重算）、`acceptance-report-nature` 2（DSML 交付的 report_error 富化、纯文本无效报告无标记）、`fixed-team-report` 1（run 交付与分页视图的标记贯通）。外部模型调用为 0。

边界：检测是建议性标记，不防止模型输出 DSML；提示强化在真实模型下的服从率未测量。Tester 补报告 max-tokens 截断（单次输出上限放不下完整报告）是独立的预算数学缺口，本版未动。0.8.3 式"可见 schema + 执行层拒绝"的机制层根因修复（逆转无工具收尾设计）保留为后续可选方向。本版未安装/打包到日常 profile，安装与真实模型试点另行记录。


## 2026-09-12：0.15.0 恢复可见工具收尾（0.8.3 语义回归）

动机：0.14.1 真实回归中三个初始角色的无工具收尾全部产出 DSML 伪调用文本（详见 0.14.2 节与 `reports/2026-09-12/pelican-014-live/`）。0.8.3 曾以"保留工具 schema、执行层拒绝"修复同一根因（commit `7df7520`），0.12.0 请求会计对齐时退回工具摘除，本版恢复 0.8.3 语义并适配现行会计。

改动：

- `budget.js` `applyCloseout` 不再清空 `assembly.tools`；收尾估算按真实含 schema 信封计价（pre-step 的 finalInput 与 `agent/request` 的实际测量一致）。**不**额外预留恢复输入：1/3 估算余量与报告下限已缓冲典型的一次拒绝尝试；紧额度不应在到达即停泊（0.8.2 教训）；尝试耗尽余量时以诚实准入拒绝收场，不再产出假报告（0.8.3 教训）。宿主工具管线确认：闸门/前置钩子的拒绝会转为错误工具结果喂回模型，循环继续到报告步。
- `budget-runtime.js` 收尾准入：移除 `WORKER_CLOSEOUT_TOOLS_PRESENT` 拒绝；`WORKER_CLOSEOUT_ALREADY_SENT` 阈值 `calls_at_closeout + 1` → `+ 2`（一次工具尝试步 + 一次报告步）；系统提示指令校验与 CM 延迟不变。describe 指引文案同步。
- 呼叫数泊车规则保持 `remainingCalls <= 1` 不变：`+2` 准入在令牌轨泊车下提供尝试空间；按呼叫数在 ≤2 停泊会让 2 次呼叫的 worker 完全无法工作（预算宿主测试实证），被呼叫轨停泊且仅剩 1 次时模型应直接写文本报告——烧掉尝试则报告撞上 `WORKER_CALL_LIMIT_REACHED`，与 0.8.3 同为已知残余。
- `budget-advice.js` Lead 规划文案同步（触轨不再"丢失全部工具 schema"）。

测试：完整 JavaScript 回归 **746/746**（全量运行 745 通过 + `fixed-team-rework` 的真实 Python sidecar 用例单独通过；该用例与既有 `host-dispatch-integration` 同属并行执行资源争用 flake 类）。新增 1 项端到端（`native-budget-pipeline`：收尾时真实工具调用 → 闸门拒绝为错误工具结果 → 下一步纯文本报告被接受，第三次调用被拒；全程零执行）。重钉既有行为：`native-budget-pipeline` 4 处（收尾信封含 schema、传输重试后 schema 保留、报告修复续接的收尾请求保留 evidence 工具 schema）、`budget-host` 3 处、`worker-closeout-request` 2 处（+2 准入边界）、`mechanism-911-characterization` 1 处（真实账本重钉：第二收尾调用仍准入、第三次拒绝）、`worker-rework-budget` 1 处。`budget-runtime` 的恢复储备兼容通道测试不变。外部模型调用为 0。

边界：真实模型在可见 schema + 指令下选择直接写报告的比例需试点验证（0.8.3 证据来自当时模型版本）；伪调用检测（0.14.2）作为兜底显式层保留。呼叫轨泊车仅剩 1 次时的尝试烧毁、拒绝后历史增长超出估算余量两类残余与本设计同存并已在测试注释中记录。本版未安装/打包到日常 profile。
