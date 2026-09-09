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
