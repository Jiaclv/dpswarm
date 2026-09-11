# DPswarm 机制架构

> 日期：2026-09-01；2026-09-03 补充默认单 agent 与按需协作的产品入口边界。
> 边界声明：本文档只收录用户在对话中讨论过并认可的机制设计。实现细节（数据 schema、配置、代码骨架）与对话未覆盖的延伸部分一律不写，清单见 §10。
> 与《技术调研与方案设计.md》的关系：那份是调研结论（现状是什么），这份是机制设计（系统怎么运转）。

---

## 1. 一句话机制

**给委派决策提供数据依据，并从执行结果中闭环。**

harness 已经把"选哪个模型、用哪种委派形态"的决定权交给了 LLM，但 LLM 手上没有任何成本、能力、历史表现的数据，只能凭训练先验猜。本机制补的不是决策权，是**决策所需的事实**，以及让事实随使用越来越准的闭环。

### 1.1 产品入口：单 agent 默认，协作能力按需调用

**用户把任务交给当前主 agent，主 agent 先以单 agent 方式工作；DPswarm 作为宿主中的能力／插件，在主 agent 判断需要多 agent 协同时被自主调用。** 调用指引可以由 skill、persona 或工具说明承载。插件已安装、工具已发现、控制服务已连接，表示能力可用，不表示已经创建团队。

是否调用由当前主 agent 在理解任务和执行过程中判断，依据仍是任务可分性、上下文交接成本、可用模型与资源事实。已有任务授权和运行边界内，无须用户另说“开启多 agent”或手动进入团队模式；用户明确指定的模型、协作方式及限制仍优先。

主 agent 调用协作能力时兼任 Lead，按需选择派生、分裂或裂变并负责分工与验收；控制面负责准入、状态、证据及计量。协作结案或收拢后，主 agent 接回摘要与产物引用，继续原任务。保持单干、启用协作和收回协作都属于同一任务的执行过程，不预设固定人数或固定 Planner／Executor／Verifier 流水线。

---

## 2. 谁做什么：职责切分

整个机制的第一块基石。调度者本身是 LLM 这件事不是问题——关键是把任务按性质分开：

> 术语约定：**主 agent** 指默认单干态的顶层 agent（见机制五）；**Lead** 指它演化出的调度者角色。委派发生时主 agent 即担任 Lead，下文按语境混用，指向同一实体。

| 任务 | 性质 | 承担者 |
|---|---|---|
| 要不要委派、拉起什么形态（subagent / team / worker） | 语义判断 | **Lead LLM**（内嵌） |
| 定义 context 需求：任务意图、选材标准、排除项 | 语义判断 | **Lead LLM**（内嵌） |
| 装配 context：按作用域、权限、版本与 token 预算取材；必要时做语义压缩 | 确定性检索为主、LLM 按需参与 | **Memory Service + Context Assembler；可选 context manager LLM** |
| 选择具体模型（provider / model / reasoning effort） | 结合任务语义与已注入事实作离散选择 | **人工（明确指定时）> Lead LLM（未指定时）** |
| 模型目录与事实：可用性、能力画像、价格/点数权重、延迟、历史成功率 | 数据采集与确定性计算 | **代码** |
| 路由硬准入：模型是否存在且可用、级别方向（显式人工 override 例外）、深度、节点数量、节点点数容量与权限 | 规则校验与原子资源获取 | **代码** |
| token 算术：这段 context 给某模型是多少 token、多少钱 | 确定性计算 | **代码** |
| 历史统计：某模型超过多少 token 成功率下降 | 实测数据 | **代码** |
| 可复现性：实验对照 | 系统属性 | **代码** |
| 控制面事件合法性：节点状态机、DAG 环、lease 原子性、容量准入 | append 前回放校验 | **代码**（自建 invariant 校验，见 §9.1） |

**内嵌是省钱的来源**：Lead 调用委派工具时，那个 `prompt: ContentBlock[]` 参数就是它的上下文调度决策——它是"决定调哪个工具"这同一次生成的一部分，不为调度单独发起调用，零额外成本。（对照组：另起一个独立监督者 LLM 专门跑一轮返回意见，关键路径多一次往返，路由器可能比被路由的还贵。）

**具体模型的语义选择权是“人工 > Lead”，执行权受代码硬准入约束。** 人工明确指定 `provider / model / reasoning effort` 时，该选择优先；Lead 可以给出更合适、更便宜或风险更低的建议及理由，但不能覆盖人工决定。人工未指定时，Lead 在委派请求中直接写出精确路由。代码在选择前提供目录与事实，在节点任务真正启动前只验证硬约束：普通 worker work item 取得一个 worker 槽，所有常驻节点（worker 与控制节点）按选定模型取得节点点数容量——按需瞬时调用的 context manager 不占点数（§7）；若提案不合法或当前容量不足，代码返回结构化原因，由人工或 Lead 重选或等待，不能静默替换、降级或改选另一个模型。选择来源（`lead | human`）、提议路由、实际解析到的路由以及目录/point policy 版本都随 node task / attempt 持久化，保证验收、容量追踪与 replay 对得上同一次决定。人工按机制五的既有结论可越过“只能召唤同级或低级”的级别方向；模型存在性/权限、root 容量、深度与槽位等其余硬约束能否被人工越过，仍需单独确定。

### 2.1 `RootExecutionSpec`：整棵任务树的运行边界合同

`RootExecutionSpec` **应当存在，但只能保存 root 级稳定约束，不能变成容纳所有状态的“大对象”**。一个 root task 在概念上分成三部分：任务目标与输入、版本化的 `RootExecutionSpec`、持续变化的 `RootRuntimeState`。前者回答“要完成什么”，Spec 回答“整棵树被允许怎样运行”，Runtime State 回答“此刻正在发生什么”。这种分离使 DSH、Kimi 或其他 runner 都只能执行同一份 DPswarm 边界，不能各自解释或生成预算。

V1 的最小 `RootExecutionSpec` 固化：`maxOpenWorkItems`、`maxActiveNodePoints`、默认子 Team 点数比例（当前为父 Team 有效上限的 50%）、`rootAcceptanceMode`、人工 override 始终有效、权限配置引用、`ModelPointPolicy` 版本，以及**时间方向两件**——树级 `deadline`（可选但默认开启，触发后走 §9.6 封存三段式）与**单节点 wall-clock 超时**（默认值在此固化，见 §7 时间护栏）。累计金额、token、调用次数另保留为 **optional cumulative-budget Hook**；它们若启用属于不可回收的消费约束，不能混入会随节点结案全额归还的 node points。`rootAcceptanceMode` 的具体模式和最终发布权仍按 §10 留待讨论，本节只确认它必须由 root 合同显式承载，而不能由某次 Lead 运行临时决定。

当前槽位/点数占用、各节点精确路由、DAG 状态、lease、attempt、session/context epoch、artifact/memory 引用以及真实 token/金额消耗都属于 `RootRuntimeState` 或事件日志，**不得写回 Spec 充当实时状态**。子 Team 也不得复制一份可独立修改的 Spec；所有层级只引用同一个 `root_spec_id + revision`，因此裂变不会获得一套新边界。

Spec 不原地覆写。人工调整边界时发布新 revision，并把变更作为持久事件审计。若新 revision 将容量降到当前占用以下，系统默认不强杀已经在跑的节点，而是停止新的准入，等待占用自然回落；终止现有节点必须由人工另行明确。这样人工仍高于 Lead，但修改配置本身不会被误解释为取消任务。

---

## 3. 机制一：事实注入（事前给事实）

这是整个系统的发动机。

- **现状**：`list_subagent_models` 只返回 provider + model id，模型凭训练先验选。
- **目标**：Lead 在做委派决策并直接选择具体 `provider / model / reasoning effort` 时，手上有一份事实——这个模型什么价位、在什么任务上历史成功率多少、上下文超过多少开始退化、当前 root 点数总容量/已占用/可用各是多少。
- **注入位置**（对话中确认的三处）：工具 schema、persona、发现工具的输出。例如在 schema 或 persona 里写明"该子 agent 至多收 X token；当前点数总容量 Y、已占用 Q、可用 Y-Q；该模型超过 Z token 后成功率明显下降"，让 LLM 在**有约束的空间里**做语义选择。
- **定位**：这是 `list_subagent_models` 已有模式的升级——从"只有 id"到"带画像、成本、历史表现"。发现层帮助 Lead 选择，准入层验证其精确路由；二者都不接管 Lead 的语义决策。

**V1 注入来源（与 §8 定调对齐）**：V1 注入的事实 = AA 分维度评分（按任务类型匹配，§8）、目录与价目、当前容量、以及 L4 bench 产物（衰减曲线、缓存经济学等实验层事实）。**自家生产画像只落库、不注入**——"该模型超过 Z token 后成功率下降"这类衰减阈值 AA 没有，只能来自 bench；bench 产物是实验层事实，不构成"画像只攒不用"的违反。

事实从哪来？见 §4。

---

## 4. 机制二：观测与闭环

事实不会凭空存在，必须实测，且随使用持续更新。

**观测**：每次委派记一笔全账——

- 谁委派给谁、用的哪个模型（provider/model）
- **拓扑上下文**：所属 work item、DAG 依赖边、分裂主从角色（主执行者 / 协助者）——分裂结构必须在观测中标注（§7）
- **验收者身份**（结案时补记：`acceptedBy` / `rejectedBy`：验收 Lead 的路由与模型级别，打回路径对称记录）——验收是分层的（子团队 Lead 审自己的 worker，父 Lead 只见节点结果），各层 Lead 级别不同是**结构性**的；不记验收者，聚合时"审查者松紧"的差异会被算进 worker 画像——A 级审得松 ≠ worker 表现好。借 DSH 任务事件记 `ownerId` 等行为者身份的模式
- **两层终止原因**：harness 侧 `stopReason` 五值（completed / error / max-tokens / aborted / refusal）照记；work item 终局原因另记一层（`accepted / rejected-exhausted / escalated / timeout / deadline-stopped / manual-stopped`）。**429 与账户配额分开**：`RATE_LIMIT` 是背压信号——退避重试、不进 failure audit 当负面样本、不污染画像；`QUOTA`（余额耗尽）不是背压，走明确终止 / 上交人工并进运维审计（仍不进能力画像）——DSH 默认重试集本就只含 RATE_LIMIT 不含 QUOTA
- token 账，且缓存读写单独计账（cacheRead / cacheWrite 与 input 不相交）

**采集路径（经源码核实，不是"三个埋点零侵入"）**：`subagent/start` / `subagent/end` 事件对提供 runId、后端与五值 stopReason，但**不含 TokenUsage 与精确模型路由**，且 publication 前失败不发事件——token 账须从 child session 日志的 `TokenUsage` 关联，精确路由与验收者身份由控制面自己的准入 / 结案事件记录，启动失败由 §9.3 provisioning 记录覆盖。

**闭环**：执行结果回来 → 更新该模型在该类任务上的画像（贝叶斯更新）→ 画像成为下一次决策时注入的事实 → 下一次委派在更准的事实上进行。**V1 限定：闭环只跑到"更新画像"为止**——画像只攒不用（§8），注入事实来自 AA 分维 + 目录价目 + bench 产物（§3）；画像回流注入是数据成熟后的开关，不在 V1。

**结果判定（奖励信号）**：闭环的前提是知道每次执行"干得好不好"——这个判定依据即奖励信号。completed ≠ 成功，判定采用 **Lead 验收制**：子 agent 提交结果，Lead 审查——通过 = 成功、打回 = 失败。显式判定胜过行为推测，且打回理由是免费的失败归因数据，全在事件流中可采（一次过与打回两次才过，质量信号不同）。三层校准：stopReason 抓硬失败（免费、粗糙）；Lead 验收做主奖励（显式、量大）；bench 测试判定做标尺（精确、昂贵）——定期求"Lead 验收通过"与"测试通过"的一致率，校准 Lead 审查的严格度（审查者也是 LLM，会放水）。审查深度分级：机械任务轻审，进资产库的关键产出重审——防审查本身成为成本黑洞。

**结案流程（Lead 决定通过后的固定五步）**：节点先进入 `finalizing`，槽位与点数 lease 继续占用 → 原始过程、产物及其 hash 写入 evidence / artifact store，并提交供后继节点读取的最小 dependency package revision → 以一次原子状态转换发布 `accepted`，同时结算 todolist、解锁后继并释放节点 lease → 从这份已验收证据异步提取 candidate memory，再按晋升规则写入 durable memory → 清理当前 agent 窗口（只留 checkpoint 引用）。这样 `accepted` 对外可见时，其依赖材料已经可读，不会出现后继节点先启动、package 后落盘的竞态。五处闭合：① 机制五"退化收摘要"从原则变成固定流程；② 默认 durable memory **只收 Lead 验收过的内容，外加 Lead 确认的失败归因（failure finding）这一显式例外**；③ `submitted` / `rejected` 结果仍进入 evidence 与失败审计库，但不会自动注入后续 agent；④ durable memory 抽取失败不回滚已经成立的任务验收，后继正确性依赖已提交的 evidence / dependency package，而不是等待语义记忆生成；⑤ "清理"是窗口层操作，不是磁盘删除——原始证据保留，只标记、索引和失效，不静默删除。

**顺序原则：先做观测，不做决策。** 第一块代码只挂事件采集数据，不碰任何路由逻辑。无论最终形态如何，这批数据都不会白费——画像、实验、路由全部建立在它之上。

---

## 5. 机制三：上下文分发

**管理权归属**：Lead 只负责表达任务意图、选材标准与排除项，不亲自拼装每个 context 包。实际管理交给持久化 **Memory Service + Context Assembler**：代码先按作用域、权限、版本、依赖和目标模型 token 预算做确定性检索与装配；只有需要跨材料概括、冲突消解或语义改写时，才按需唤起一个 context manager LLM。它是可选的压缩执行者，不是唯一事实源，也不要求把全局材料长期放在自己的窗口里。

### 5.1 为什么异构反转了分发策略

- **同构团队**的省法是"统一大前缀 + 缓存复用"：全局 context 喂给所有子 agent，前缀相同，后续子 agent 吃 cache read 折扣。
- **异构团队**这条路物理上不存在：KV cache 绑死在权重与 tokenizer 上，跨模型是数学无意义而非格式不兼容。每个子 agent 收全局 context 都是全价，统一前缀从省钱机制退化成纯粹浪费。
- **因此策略反转：不追跨模型前缀共享，改追检索与裁剪。** 成本算术：异构直传 = n × 全局 × 全价；外部记忆方案 = 一次索引 / 提取成本 + 按任务检索候选 + 必要的语义压缩 + n × 小包。子 agent 越多、全局越大，裁剪方案赢面越大；但索引、召回和压缩也必须单独记账，不能把它们假定为免费。
- 精确化：异构只杀**跨模型**缓存；若两个 worker 恰好同模型，其间前缀共享仍然成立。裁剪方案在"每角色不同模型"的真异构团队下是压倒性胜利。
- **Memory Service / Assembler 是通用基础设施，manager LLM 才是按需件**：纯同构且 context 未接近阈值时可直连统一前缀，不触发语义压缩；异构、超长材料或多来源冲突时才更有理由唤起 manager。是否触发先按模型构成、窗口占用和检索结果由代码判断，不让 LLM 为“要不要调用自己”做决定。

### 5.2 Context Assembler：确定性骨架 + 按需语义压缩

真正长期存在的是外部记忆与索引，不是某个 LLM 的窗口。Assembler 先用确定性规则选择当前有效、权限可见且版本匹配的材料；若仅靠规则无法得到可用小包，再唤起 context manager LLM 读取这些候选材料并压缩。相同 provider / model 的连续压缩请求仍可利用稳定前缀缓存，但这只是优化，不能成为正确性依赖。异构摧毁 KV 层共享后，DPswarm 用**可持久化、可版本化、可复核的语义包**代替“长生不老的活缓存”。

### 5.3 分发通道与落盘

**硬约束不变：seed 是二元的。** harness 给子 agent 灌上下文的官方通道只有 fresh（全不给）和 fork（从 seq 0 连续给到某个轮次边界），没有"挑三段相关的给"这个中间态。所以 Assembler / 可选 manager 裁剪后的定制 context 走这三条通道到达子 agent：

| 通道 | 位置 | 特点 |
|---|---|---|
| `prompt` | 子 agent 初始用户消息 | 完全自由，最直接，在请求后部 |
| `persona` | system prompt 的 scoped section，支持插值 | 在请求**前部**，能吃到前缀缓存 |
| 自定义工具 | 子 agent 主动调用 | pull 模式，精准但多一轮往返 |

**裁剪产物落盘，大包不直接塞 prompt。** Assembler 把定制 context 写入 artifact store 或共享工作区的约定位置，并记录不可变版本、来源指针和内容 hash。同一包可被多个 agent 引用，包是资产、路径 / ID 是引用。投递契约分三种：低于可配置 inline token 上限的小包可进入 persona / prompt；大包只传不可变引用；启动前 runtime 必须预取并校验 package manifest、权限、revision 与 hash。manifest 还要把正文条目标成 `required` 或 `optional`：所有 `required` 条目必须在 node 变为 runnable 前由 runtime 预取并注入或挂载成 agent 可直接读取的内容，不能依赖 agent 主动 pull；pull 只用于 `optional` 补充材料。**排列顺序原则在包内部仍然成立**：稳定内容在前、任务特定在后；子 agent 侧稳定进 persona、动态进 prompt。

### 5.4 质量保险：裁剪是初筛，pull 是兜底

裁剪本质是 push 预判——预判错就双输（给多了烧 token，给少了任务失败），且不能假设子 agent 总能可靠识别并主动说明“我还缺哪一块”。两条保险：

1. **分工**：Lead 给 Assembler 明确的选材标准（要什么、排除什么），Assembler / 可选 manager 做检索、阅读与压缩劳动，**不替 Lead 做任务判断**——对任务意图理解最深的是 Lead，不能让二次概括者自由改写目标。
2. **兜底**：子 agent 可以经 pull 通道查询 Memory Service，缺口需要语义合成时再唤起 manager。裁剪因此不是"开工前必须一次裁对"的单次事件，而是任务全程可修正的交互：开工给小包，工作中缺什么就补什么。对弱模型 worker，补充材料仍由 Assembler 组织，而不是让它独自翻阅全部原始材料。

### 5.5 生命周期：下线封存、唤起与重开

**原则：物理 agent 窗口可下线、封存、唤起或重开，逻辑 node task 不因此变化**——避免长期存活导致上下文污染（失败尝试与中间垃圾在窗口里累积，窗口再大也会跑飞）。harness 的唤起 = continuable 子 agent 冷恢复；重开 = spawn fresh。这里的 `fresh` 只表示新物理 session / window，不代表新建 work item：若是硬切续接，`node_id`、`work_item_id`、点数 lease 与 worker 槽全部沿用。

**模式选择按任务形态，不按角色一刀切**：

| 任务形态 | 模式 | 理由 |
|---|---|---|
| 探索 / 一次成型 | **重开（fresh）** | 防污染最大化，垃圾随实例死掉；context 由外部 package 提供 |
| 迭代收敛（改-测-改循环） | **封存-唤起；到阈值后硬切续接** | 未到阈值时保留细粒度迭代状态；窗口过大后以 checkpoint 进入 successor session |
| context manager LLM | **按 context job 唤起或重开** | 长期知识在 Memory Service；manager 只为当前装配任务做语义压缩，可在同一 job 内 continuable，不跨 Team 生命周期持有全局真相 |

配套必然推论：**持久状态不在任何 agent 的上下文里。** 它被拆成结构化 control state、不可变 evidence / artifact、以及只含已验收知识的 durable memory。agent 可以随便换窗口，状态和证据不随之死亡；successor session 从 checkpoint 与外部 package 续接，而不是把完整旧 transcript 再灌一遍。

### 5.6 外部记忆分层与事实源

这里借鉴 OpenAI 当前公开的两种分离机制，而不宣称复刻其未公开内部实现：一是 [Memories](https://learn.chatgpt.com/docs/customization/memories) 把记忆定位为辅助召回层，并将摘要、持久条目、近期输入与支持证据分开；二是 [Compaction](https://developers.openai.com/api/docs/guides/compaction) 在窗口达到阈值时压缩上下文、继续后续回合。DPswarm 进一步把多 Agent 控制状态从两者中剥离：

| 层 | 存什么 | 权威性 |
|---|---|---|
| 当前 model window | 本次推理所需的短期消息、工具回执与小包 | 可丢弃，不是事实源 |
| Control State / Event Log | `root_spec_id + revision`、DAG 边与节点状态、owner、`submitted / finalizing / accepted / rejected / terminated`、当前槽位与点数占用及 lease、幂等键、重试和外部副作用状态 | 强一致控制事实，不能只存在摘要或向量库；RootRuntimeState 必须能追溯到生效的 RootExecutionSpec revision |
| Evidence / Artifact Store | 原始日志、工具结果、产物、测试证据、精确版本与 hash | 可追溯事实源，默认不可变 |
| Durable Memory + Retrieval Index | 已验收的结论、决策、项目知识、经确认的失败模式 | 辅助召回层，可版本化、可失效，不替代控制事实 |
| Checkpoint Capsule | 旧窗口到 successor window 的可移植续接包 | 上下文桥梁；必须能追溯到上面三层 |

**状态词汇分三层，勿混用**：① **验收流状态**（内容裁决）：`submitted / finalizing / accepted / rejected / terminated`——上表 Control State 列的枚举即此层；② **物理生命周期状态**（§9.3 启动协议）：`provisioning / active / failed`；③ **调度阻塞态**：`blocked / recovery`（§5.8 预装失败、§7 超时等）。一个节点同一时刻处于三层的各一个值（例：`active` + `submitted` + 无阻塞）。

硬规则、人工明确约束和权限策略应进入配置、项目文档或 control state，不能只靠“也许被检索到”的记忆。记忆读取与记忆贡献分开授权：worker 可以获得读权限却没有晋升权；来自 web、MCP 或其他外部工具的内容必须保留 provenance，不能因模型复述一次就自动成为项目事实。任何层都不保存模型私有 chain-of-thought，只保存决策、证据、产物与可复核理由。

### 5.7 记忆晋升、版本与失效

任务结果与记忆条目使用两条相连但不混同的生命周期：

```text
worker output → submitted
        ├─ Lead 打回 → rejected → retry / terminated
        └─ Lead 决定通过 → finalizing → 原子发布 accepted

accepted evidence / Lead-confirmed failure finding
        → candidate memory
        → promotion check
        → durable memory
        → retrieved into context package
        → superseded / invalidated
```

- `submitted` 只代表待审，不能晋升；`finalizing` 表示 Lead 已决定通过、但依赖材料尚未完成原子提交，此时仍不解锁后继或释放资源；`rejected` 原文留在 evidence / failure audit，但不进入默认检索。Lead 确认的“此方案为何失败”可以作为独立 failure memory 晋升，不能把被拒答案整体包装成知识。
- 新事实与旧记忆冲突时不覆盖历史：新项声明 supersedes，旧项进入 `superseded`；代码、配置、DAG、权限或来源产物变化时，依赖它们的 memory / package 进入 `invalidated`，后续检索默认排除。
- 每项 durable memory 至少带逻辑元数据：`memory_id`、scope（user / project / root / team / node）、source IDs、artifact hash、accepted by、memory revision、created at、TTL、supersedes、visibility。字段的物理 schema 仍留到实现阶段决定。
- 抽取与 consolidation 可以选不同模型，并接入与点数类似的模型选择 Hook；抽取发生在 node 稳定结案或空闲后，不在 worker 刚 `submitted` 时抢跑。

### 5.8 硬切窗口与同节点续接

每个 agent 按目标模型自己的 tokenizer、context window 与输出预留分别计算阈值，不使用 root 统一 token 数。初始建议设两道可配置阈值，后续由实测衰减曲线校准：软阈值约 70% 时去重、检索并准备 capsule；硬阈值约 85% 时必须完成 checkpoint 并切换窗口。

硬切采用以下原子顺序：

1. **预装 capsule**（软阈值触发）：Assembler 生成 provider-neutral checkpoint capsule——必要时唤起 context manager LLM 做语义压缩——包含目标、已验收决定与不变量、当前 node / DAG 引用、产物版本与 hash、未决问题、已确认死路、下一动作、memory / package revision，以带 hash 的不可变包落盘。**预装契约**：所有窗口启动（新建 / 硬切 / 唤起）的 context 必须先以已校验的不可变包形式存在，启动协议只负责提交、不现场拼装——fresh 路径的包 = Assembler 产物；**fork 路径的包 = 父级日志前缀**（seq 0..seedLength 天然不可变、可引用），对账时校验 seedLength 与 parent 谱系。内容生产归 Assembler / manager（§5），提交执行归控制面——manager 不成为启动协议的执行臂。生成失败：节点保持原 lease 进 blocked / recovery，无任何状态变更。
2. **CAS 登记 successor**（硬阈值触发）：以 predecessor 的 session 状态和 `context_epoch` 做 compare-and-swap，原子提交 capsule 引用、来源、当前 control-state revision 和唯一 successor 登记；`(node_id, context_epoch)` 必须有唯一约束，副作用经 transactional outbox 发出，保证 watchdog 建议、人工指令等并发意图不会登记出两个 successor（执行归唯一写者，见 §9.2 进程模型定调；outbox 防的是写者进程内"事件落了、副作用没发"的半截状态）。CAS 只保证"最多承认一个 successor"，不保证它真的开起来——后者由下一步管。
3. **两阶段启动 successor**（复用 §9.3 通用协议）：provisioning 落盘（rollover 语义：同 `node_id + work_item_id + lease_id`、`context_epoch + 1`、`predecessor_session_id + checkpoint_id`、预装包 hash）→ 建 fresh session 并提交预装包 → hash 比对确认持久接受 → `active` → 原始 session / event log 转只读 predecessor，保留用于审计，不默认回灌。这是 context rollover，不是新节点、不是新重试预算，也不释放或重新取得 worker 槽和节点点数；崩在半路的恢复对账见 §9.3。
4. 若 rollover 同时换模型，沿用机制五的原 lease 原子 reweight；拿不到点数差额就等待，不能先启动再补账。
5. provider 原生 compaction item 只能作为同 provider 路径的可选加速。它不透明且不可作为跨模型契约；OpenAI → Kimi / DeepSeek 等异构续接一律以可读 capsule、外部证据和版本化 package 为准。

**不设 delta 通道（已确立）**：successor 执行中发现缺料，一律走失败归因重试，每轮重试喂更丰富的 capsule——不开"从 predecessor 拉取增量"的口子，防止它演变成"方便就好用"的泄漏后门。predecessor 只读、不回灌、无例外通道。注意与 §5.4 的区分：**活的 worker 经 pull 通道向 Memory Service 补料是正常运行机制**（裁剪初筛、pull 兜底），不受此限；被禁的只是 successor 从 predecessor 旧日志拉增量。

映射到 harness 时，真正的硬切优先使用 **fresh session + context package**；fork 会复制连续历史前缀，适合短期同构继承，却不是清除旧窗口的可靠方式。切窗只封存旧物理 session，不封存整个 Team；Team 仍要等 root task 最终 accepted 或明确 terminated 才结案。

---

## 6. 机制四：委派的经济性边界

调度者是 LLM 的自带代价：**它得先把材料读进自己的上下文，才能决定分发什么。**

- 极端情况：Lead 为了写好委派 prompt 读完 50k 材料，那委派没省下任何东西，还不如自己干完。
- **边界条件：只有当子任务的上下文需求能被一段简短描述覆盖时，委派才划算。**
- 形态选择交给 Lead：它探查材料后自己判断——自己做、拉 subagent、还是拉 team/worker。这是 harness 现状，也是合理设计；要点是**判断的成本已经发生**，所以判断点越晚越亏，最优点在"读到刚好够判断"而非"读完"。
- **这条边界可测**：量"Lead 为完成委派消耗的 token"对"委派节省的 token"，找盈亏线。该数字是帕累托曲线的一部分，也是观测数据里天然包含的。

---

## 7. 机制五：动态拓扑演化

**默认态是单 agent，拓扑是执行过程中按需生长与收缩的。** 不预设多 agent 结构：主 agent 先正常读取任务，在理解需求后或执行途中自行分析决定是否转换角色；也可以由人工定向指定（自主判断为默认，人工指定为 override，两个来源并存）。

**三个演化方向**（按任务结构选择）：

| 方向 | 命名 | 适用 | 缓存特性 |
|---|---|---|---|
| parent + 单个较轻量子 agent | **派生** | 单点任务外派，子 agent 通常为低级模型 | 跨模型缓存归零，走 Assembler 裁剪、必要时 manager 压缩（§5） |
| 1 主 + 1 副同构协作 | **分裂** | 任务可分、且每块都需要与主 agent **相同能力** | **同构 fork：唯一能保留前缀缓存的扩展方式**（继承前缀，同 provider 继续命中） |
| 扩展成多 agent team | **裂变** | 子任务多且有依赖，需要 DAG（有向无环任务依赖图）协调 | 跨模型缓存归零，走 Assembler 裁剪、必要时 manager 压缩（§5） |

（展示层远期方向：三态演化做成液态分裂/聚合动效——裂变碎成多块、退化时碎块流回主体，扎克既视感。）

**演化护栏（硬约束，代码层强制，不需 LLM 判断）**：

| 约束 | 默认值 | 机制理由 |
|---|---|---|
| 拓扑深度 | 用户可配，**默认两层**；层按**验收单元**计（主 agent 为第 1 层；派生 / 裂变产生新 work item，+1 层；**分裂是同层拓宽**——协助者与主执行者共享同一 work item，不加层），派生/分裂/裂变**共享同一 root 容量与 worker 槽池** | harness 的 `maxDepth` / `delegationDepth` 只作**物理上限**放置（需 ≥ 语义深度 + 1，以容纳同层 fork 协助者的物理 child），语义深度门禁由 DPswarm 控制面执行；默认两层下第 2 层 worker 仍可分裂（协助者同处第 2 层），但派生 / 裂变式的再拉起一律禁止 |
| 裂变规模 | **`maxTeamWorkers` 用户可配，默认 ≤ 3 个同时存在的直属子 agent（不含 Lead）**；Lead 在上限内决定本次实际人数，已结案并回收的 worker 不再计数 | 限制一支 Team 当前的成员规模而非生命周期累计创建数；默认 3 是审查带宽的初始值而非目标值，后续可用验收数据调整 |
| 开放任务并发 | **同一 root task 树共享一个由 `RootExecutionSpec` 固化的 `maxOpenWorkItems` 硬上限，且只统计普通 worker 任务**；worker 从启动到 Lead 验收通过或明确终止前持续占槽；Lead、context manager、reviewer 不占普通槽位 | 控制面不能因 worker 占满而失去调度、供料或验收能力；子 Team 不获得一套新槽位，任何层级的普通 worker 都从 root 同一池获取。模型结束但等待验收时仍占 worker 槽，防止待审结果堆积 |
| 节点点数容量 | **同一 root task 树共享由 `RootExecutionSpec` 固化的 `maxActiveNodePoints` 加权容量，并预留 `ModelPointPolicy` Hook**；每个 DAG 节点任务启动前按其选定模型取得点数，直到 Lead 验收通过或明确终止才全额归还 | 与开放节点数量组成双重准入；点数是当前尚未结案节点的占用量，不是用完即少的消费预算。节点内部多次 LLM 请求不重复占点；后续按具体模型价格与综合能力画像计算权重，当前不把 S/A/B/C 固定映射成某个分值 |
| 分裂规模 | **1 主 1 副** | 写范围二分最干净（`writeScopes` 一人一半）；更多克隆体写冲突协调成本陡增 |
| 召唤级别 | **只能召唤同级或低级** | **验收制要求审查者能力 ≥ 执行者**——低级 Lead 审高级 worker 审不出错，奖励信号链当场断裂。管的是能力方向 |
| 裂变权限 | **仅 S 级**（A/B 只可派生与分裂） | 裂变 = 审多份异构输出（默认上限 3）+ DAG（任务依赖图）协调 + 失败归因，最高难度调度工作；A/B 审单份可胜任、审多份异构即放水。管的是复杂度上限 |

两条级别约束是同一原理的两个侧面：**拓扑的能力方向与复杂度都不得超过调度者的审查冗余**。

**点数 Hook 的当前边界**：点数是**节点任务容量权重**，不是货币、累计消费预算或单次 API 调用配额；本轮只确定 Hook 与占用生命周期，不确定权重公式。Lead（或人工 override）先为 DAG 节点任务提出精确的 `provider / model / reasoning effort`，`ModelPointPolicy` 再读取节点类型（lead / worker / reviewer）、精确模型身份、当时价格快照、该任务桶的综合能力画像、上下文规模与预计工作，返回该节点固定的 `nodePointWeight + policyVersion`。节点从准入启动到 **Lead accepted 或明确终止**始终占用这份点数；worker 提交结果、模型某次请求结束、等待验收、Lead 打回后准备重试，都不代表节点结案，因此都不释放。节点内部不论调用 LLM 一次还是多次，都只占这一次节点 lease；真实 token、缓存、金额、调用次数、延迟与结果单独写入观测和画像，不从点数池永久扣除。若同一节点换模型重试，代码在新 attempt 前原子调整原 lease：权重下降就归还差额，权重上升必须先取得差额，否则节点保持等待且不得启动新模型；同模型重试则 lease 不变。资源准入分两层：**新 work item 才占 worker 槽**（无论并行还是顺序依赖）；**每个 LLM 节点各自占点**——含控制节点与依附型协助者，协助者不新建 work item（见本节分裂主从结构）。rollover 与同节点重试不新建资源 lease。**context rollover 不是新节点：同一 lease 跨窗口保持占用，不释放后重取。** 若节点/Lead 崩溃，先按故障原因恢复、替换或明确终止；在节点去向未确定前 lease 继续占用，不能因为某次 API 失联就假定整个节点已经关闭。Provider 对瞬时在途请求数可以另设 transport semaphore，但它属于适配器限流，不参与节点点数的获取与归还。

**控制面 LLM 不占普通 worker 槽；点数按"是否常驻节点"分两类（已确立）。** root Lead 是根控制节点，从 root task 启动到最终验收/终止持续占自己的 `nodePointWeight`——它最先准入，不存在被 worker 挤占的问题。独立 reviewer（角色语义待定，§10）若启用，按控制节点占点。**context manager 不是节点、不占点数**：它是代码按条件触发的按需瞬时调用（§5.1），成本记触发方（节点 / context job）账下，并发由独立的 control-plane / adapter semaphore 约束并记账，防止"不占槽不占点"演变成隐藏的无限并发——由此 §5.8 硬切预装的 manager 唤起**没有点数准入失败路径**，点数被 worker 吃满也卡不死硬切。Memory Service、索引和 Context Assembler 的确定性部分属于运行时基础设施，不申请模型点数。

**worker 槽绑定 work item，不绑定 agent 角色。** 一个 S worker 在执行中裂变并转为子 Team 的 Lead 时，原父级 work item 仍未结案，因此继续保留原来的一个 worker 槽；“成为 Lead”本身不再新增槽。它新拉起的普通子 worker 则各自从 root 池取得槽，直到对应 work item 验收或终止。

**嵌套 Team 继承的是节点容量上限，不是可消费余额。** root 保持唯一的 `maxActiveNodePoints`；每个子 Team 只有一个本地子树上限，默认以父 Team 有效上限的 **50%** 为起点，由父 Lead 协调具体比例，但必须小于父级。子树内每个尚未结案节点都同时计入自身、全部祖先 Team 与 root 的当前占用；节点 accepted 或明确终止时在所有层级全额释放。因此多个子 Team 可以各有本地上限，却不能让整棵树的当前节点点数突破 root 上限，也不存在“裂变产生新点数”。一个父 worker 裂变为子 Team Lead 时沿用原节点，并依据新的 role/model 对原点数 lease 做原子 reweight，不因角色转换重复计点；其新增的 manager、reviewer 或普通 worker 才分别形成新节点并占点。目前按“每个子 Team 的本地子树上限”理解 50%；“整层所有子 Team 合计上限”式的 layer cap 是否需要，列入 §10 留白。

**裂变者即 Lead**：S 拉起 team 后即成为该 team 的 Lead——从执行者转为调度者，负责该 team 的分发与验收。任务上交通路中接管的 S 同理：接管后裂变，即为其所拉 team 的 Lead。

三个推论：① **裂变一次即到底（默认）**：子 agent 即第 2 层 = 底层，该子树内后续的派生 / 裂变一律禁止——多层 team 嵌套在默认配置下不可能发生；**但第 2 层 worker 仍可分裂**：分裂是同层拓宽（协助者与主共享 work item、不加层），不消耗深度预算，只走点数准入。② **深配置下的嵌套裂变不需要专门通路**：权限由 agent **自身级别**决定（S 级三向全开，A/B 级派生+分裂），与被谁、以哪种方式生成无关；S 级 agent 出现在下层有两条现成来路——**同级派单**（S Lead 给子任务直接排 S 级模型，"同级或低级"包含同级；派生的"较轻量"是典型经济用法而非硬约束）与**换模型重试**（打回归因为能力弱项后升级至 S；升级上限 = Lead 级别——A Lead 的 worker 最高升到 A，需要 S 执行时走任务上交而非继续升级，此为验收制的直接推论）。任何下层 S 自然拥有裂变权，嵌套由此产生；每层 Lead 仍 ≥ 其全部成员，验收链逐层成立，能力单调不升不变量保持。③ A/B 级想要 team 的通路只有两条：**人工 override**（人不受级别约束），或**任务上交**（退化/上报给 parent 或人工，由 S 级接管后裂变）。不存在"召唤上级"的路径——召唤方向严格向下、指挥链永远单向：A 级派生 S 级既违反级别约束，也会让验收制双断（A 审不了 S 的输出，S 又不归 A 指挥）。注意区分：异常路径里的模型升级（B/C/D→A→S）是**换模型重试**（同一位置执行者的自身替换），不改变指挥链，与召唤上级无关。

**可分性在执行中显现。** 开工前看索引判断可分性只是初判；很多任务要做着做着才发现"这三块其实独立"。拓扑决策因此不是一次性事件，而是执行全程可做：任何时刻发现一个人干不合适就生长，发现团队多余就退化。判断点随理解加深持续修正，比开工前的一次性猜测鲁棒。

**退化是精髓：可逆性降低决策门槛。** 委派决策里有不可逆成分（读入税付了退不回），所以"要不要拉团队"曾是重决策；可退化把它变成"试试看，不行收回来"。这是整套机制中第一个带容错结构的决策点。但退化不免费：拓扑收缩时子 agent 上下文要么回流要么弃置——回流过猛等于退化瞬间二次污染主 agent，所以收缩时**结论落盘、主 agent 只收摘要**（§5.3 落盘机制在此兜底）。退化的验收终局（已确立）：被收回的子节点记 `terminated`——不算验收、不进画像；主 agent 收回摘要后继续，其 work item 本体仍在原生命周期。

**分裂的主-从结构（已确立）**：分裂产生 **1 个 work item、1 个主执行者、1 个协助者**——主从关系类似 Claude Code 主 agent 与其 Task 子 agent：协助者只为主执行者服务，不出现在 DAG 依赖里，也不独立提交验收；work item 完成时由**主执行者统一提交**，parent Lead 只面对主执行者。记账：work item 占 **1 个 worker 槽**，协助者不另占槽；协助者是独立执行 session、真实占用调度容量，因此计自己的节点点数——但它是**依附型节点**：无 DAG 依赖边、生命周期严格依附主节点（主 accepted / terminated 时协助者必须已关闭；协助者崩溃由主执行者决定是否重拉，重拉走正常点数准入）。指挥链保持单向且显式：parent Lead → 主执行者 → 协助者。协助者的 token 与成本记在主 work item 账下，观测中标注分裂结构。分裂时机的经济权衡（早分裂前缀干净 vs 晚分裂判断更准）不设规则，留作观测假设：记录每次分裂的前缀大小、成功率与总成本，用数据画时机曲线。

**协助者的超时与奖励信号（已确立）**：依附型节点超时**不走任务上交**（上交是 work item 级语义）——只 wakeup 主执行者（§9.4），由主执行者决定重拉（走点数准入）或收拢单干。协助者的奖励信号由主执行者代写：`assistant-accepted / assistant-rejected` 随主 work item 的结案事件一并进观测（标拓扑上下文，§4），协助者模型的画像由此获得样本；parent Lead 只验收主执行者，不变。

**克隆的协调成本**：主执行者与协助者并行写同一文件会冲突——按任务划分写范围（harness team 的 `writeScopes` 为现成机制，但它仅作提示、可被 Bash / formatter 等绕过，真实文件隔离见 §9.7）。主从之间的分工与接口对齐走 §9.5 的 peer 通道，不经 parent Lead 中转。

**时间方向护栏（已确立三件套）**：占用语义（槽 / 点数）管的是"现在挤不挤"，管不了"这棵树永远不收敛"。V1 配三件：① 节点执行预算 = 首次 + 重试 ≤ 2 次（§8 重试预算，已确立）——失控的第一道刹车；② **单节点 wall-clock 超时**（默认值固化于 §2.1 RootExecutionSpec，量级参考 Kimi Code subagent 2h 先例）——超时节点转 `blocked`，由 Lead 归因后重试（计预算）或上交，防单个节点无声卡死；③ **树级 deadline**（`RootExecutionSpec` 可选但**默认开启**，§2.1）——触发后走 §9.6 封存三段式：禁止再派生、在途结案、完整状态留档。V1 默认选 deadline 而非金额：deadline 零依赖（只要时钟），而在线金额熔断需要全树实时用量归集——V1 观测是离线聚合的，撑不起在线熔断，**金额熔断等在线观测（v1 插件）之后**。业界对照：DSH 只有 ralph 循环的 `maxRounds`（耗尽进 `budget-limited` 终态）与 token-meter 计量（不限额）；Kimi 是单 subagent 2h 超时；Claude Code 是 `maxTurns`——全是单体粗熔断，树级收敛约束是编排层自己的新问题，没有可抄的答案。

**判断依据不变**：仍是那套算术（任务可分性、上下文需求能否简短描述、成本与当前点数容量），只是从"要不要委派"扩展成完整的拓扑生命周期决策——代码供事实，主 agent 做语义判断，人工可 override。

**层次变化**：机制五收编了机制四——经济性边界从"开工前的决策规则"退位为"每次拓扑判断时的参考事实"。

---

## 8. 机制六：模型分级与异常升级路径

**模型分级（先验 + 实测两层）**：

- **全局分级 S/A/B/C/D**：外部综合评定，初始先验来自 Artificial Analysis 等公开榜单（AA 只配当冷启动先验，见调研报告 §2.2），更新慢
- **桶内修正**：画像实测数据按任务桶修正分级（全局 B 级可能在"整理日志"桶是 S 级表现），更新快；**V1 只攒不用（见下），数据成熟后才接管**
- 升级决策看桶内分级（V1 期间 = Lead 归因 + 级别上限 + AA 分维分）；全局分级只在无实测数据时兜底

**V1 选型依据（已确立）**：按任务类型匹配 **AA 的分维度评分**（编码任务看 coding 分、推理任务看 reasoning 分，全局总分兜底）——选型依据是外部数据。自家画像在 V1 **只攒不用**：L4 bench 与生产观测数据持续写入画像存储，但不参与选择。由此"画像自我冻结"在 V1 架构上不成立——冻结循环的前提是"选择依赖自家画像"，V1 不满足。桶内修正何时接管选择由实验层按数据成熟度判定，是 V1 之后的开关。级别体系维持 **S/A/B/C/D** 五档自定口径（已确立），不改用数字档或精确路由名。

**异常路径：验收打回后的处置**。先归因，再处置——归因由 Lead 做语义判断，分级/画像事实由代码提供：

| 归因 | 处置 |
|---|---|
| **能力弱项**（模型对不上任务） | 升级模型：B/C/D → A 级合适模型；A 仍不行 → S（升级上限 = Lead 级别；A Lead 的 worker 需要 S 执行时走任务上交，见机制五推论②） |
| **context 问题**（裁错/缺料） | 修 context 重试，不动模型（兼作 manager 裁偏率的观测点） |
| **任务描述问题**（Lead 没说清） | 修描述重试 |
| **任务本身矛盾/不可行** | 退化或上报人工，不硬磕 |

**重试预算（已确立）**：同一 work item 打回后最多重试 **2 次**——换模型重试、修 context 重试、修描述重试共用同一预算；attempt 口径 = 首次执行 + 最多 2 次重试 = **最多 3 次 attempt**。仍不通过则不再硬磕，走机制五的**任务上交**通路返回父节点 / 人工处置。rollover（§5.8）不消耗重试预算——它是窗口续接，不是重试；**启动失败也不耗预算**——provisioning `failed`（§9.3，节点根本没跑起来）是启动事务失败，不是执行失败。

**上交的资源与终局语义（已确立）**：上交是一个**控制面事务**——原 work item 转 `escalated` 终态、释放其 worker 槽与节点点数，父级接管（自干或分解新 work item）在**同一事务内**完成准入：无死锁、无双占（单写者串行链天然支持）。原 attempt 记 `escalated`——**无裁决、不进画像**（它不是模型能力失败），打回样本保留在 failure audit。

**A→S 之前必须先深挖失败原因**——最贵的升级前先做归因，防的是把系统问题（context 裁错、描述不清、任务矛盾）误当能力问题：那种失败升到 S 也救不了，纯属烧钱。B/C/D→A 是"加钱买能力"，决策轻；A→S 是"可能方向就错了"，决策重。

**失败同时是数据**：打回理由进 failure audit（观测），负面样本更新画像（该模型在该桶降级）；被拒输出本身不进默认 durable memory。只有 Lead 确认后的失败归因 / 死路结论，才作为独立 failure memory 晋升并供后续 worker 检索。**V1 换模型只凭 Lead 归因 + 级别上限 + AA 分维分（画像只攒不用）；画像接管后，换模型重试是画像数据产生决策价值的第一个真实场景**。

---

## 9. 机制七：控制面一致性与恢复

前面各节确立了控制面要管什么（DAG、lease、验收门禁、容量）；本节确立控制面**自身不能出错**的机制。模式全部借自 DSH experimental agent-team 的源码实现——该包为 private、不直接依赖，但其模式经过实现验证。控制面事件的存储选址已定为**独立存储**（理由见 §9.1），harness 只作执行底座。

### 9.1 事件先于状态，校验先于落盘；事件存储独立

- **控制面事件属于 root task，不依附任何 agent 的物理 session（选址：独立存储）**。候选方案"写进 root Lead 的 harness session 日志"（DSH agent-team 的做法：`TeamId = Lead SessionId`，可白得 projection / invariant / flush 全套设施）被否决，原因有二：root Lead 会硬切 rollover，硬切 = 换物理 session = 事件流断成两截，而 root Lead 贯穿任务树整个生命周期，断链是常态不是边缘；且 harness invariant 的挂载点是 `internal/dispatch` 内部钩子，developer preview 阶段有变更风险。§5.6 自己的分层哲学也是这个结论：持久状态不在任何 agent 的上下文里。
- **模式照搬，设施自建**：append 前回放校验、串行事务、投影派生照搬 DSH 模式，在自己的 root task 事件存储上实现（JSONL + flush 纪律可参考 `dsh-session-persistence-jsonl` 的写法）；与 harness 的交互（建 session、读子 agent 日志做观测）走 public API 不变。
- **持久日志派生状态**：控制面全部状态（DAG 边与节点状态、lease、roster、点数占用）从事件日志回放派生，事件是唯一真源，内存视图只是投影。这是 §5.6"控制事实以事件/结构化存储为准"的实现形态。
- **invariant 伴生校验**：任何控制面事件 **append 前**，先把候选事件在已提交前缀的投影上回放一遍，非法转换拒绝落盘。节点状态机（`submitted → finalizing → accepted` 等）、DAG 环检测、lease 原子性、容量准入全部注册为 invariant——事件合法性在落盘前强制，不靠事后对账。
- **append-and-flush 后才报告成功**：任何控制面操作在事件 flush 完成前不返回成功、不唤醒等待者。这是 §4 结案流程"`accepted` 对外可见时依赖材料已可读"的底层保证形式。

### 9.2 per-root 串行化事务与 CAS

- **进程模型定调：单写者进程。** 控制面所有状态变更发生在一个写者进程内，per-root promise 链管的就是这个进程内部的串行化；watchdog / recovery 等监控组件**不直接执行变更**，只向写者队列投建议事件（"该硬切了""该对账了"），由唯一写者消费并亲自执行。此时 CAS 防的是**写者内部重入**（watchdog 建议与人工指令同时到达同一节点），不是跨进程竞争——跨进程竞争不存在。控制面写入频率极低（图变更 / 准入 / 验收都是低频事件），单进程绰绰有余，且与 §9.1 独立存储、本节 per-root 单链全部自洽。此定调即 DSH agent-team 的原始形态：它本就运行在单进程内，所有变更过同一条 transact 链——"两个 watchdog 实例"之类的多进程表述随之失效。
- **人工指令分三类，均无生命周期歧义（已确立）**：即时生效型（明确指定模型、级别方向 override、定向指定拓扑）、配置变更型（发布 `RootExecutionSpec` 新 revision 调边界——不强杀在途节点，§2.1）、终态型（停止任务）。交互形态借**选项式提问卡片**——agent 主动向用户发结构化选项、用户点选，与 DSH 的 `dsh-user-questions` 子系统、Claude Code 的 `AskUserQuestion`、Cursor 的 AskQuestion 同构（注意区分：DSH 的 approval `ask` 策略只做允许/拒绝，做不出结构化选项事件）；指令作为控制面事件进同一事件链，可审计。不存在"人工暂停 / 置位某节点等待恢复"之类的中间态操作，因此自动恢复与看门狗永远不会跟人工决定打架：`terminated` 是终态，恢复流程不碰。更细粒度的人工干预（部分暂停、单节点 override）如未来需要，先经 §10 人工 override matrix 讨论再进。
- **一棵 root task 树一条事务链**：所有层级的图变更、准入、验收、lease 调整都排进 root 的同一条串行队列，读-校验-写在队列内完成。选 per-root 而非 per-Team 的理由：默认两层、裂变 ≤3 的配置下一棵树的变更频率极低，串行化瓶颈是理论上的；而 root 容量准入跨所有层级，per-root 单链使其天然原子，不用拼跨锁协议。
- **CAS**：图变更携带 `expected_graph_revision`，基于过期副本的变更被拒绝，两个并发修改不互相覆盖。每次变更后对全图重跑校验（依赖缺失 / 重复 / 环），而不是只验增量——图小时全量重验最不容易漏。
- **id 永不复用**：`work_item_id`、`node_id` 一旦终止，同 root 内永不复用；id 空间耗尽报类型化错误而非复用——审计链要求。
- **链内只放快速事务**：`finalizing` 的发布与 `accepted` 的发布是链上两个独立事件，中间的证据落盘与 dependency package 提交在链外异步完成——单链不阻塞慢 IO，§4 结案五步的等待期不占 root 事务链。

### 9.3 节点启动两阶段提交与崩溃对账

**所有节点物理启动走同一个协议**——新建（派生 / 分裂 / 裂变）、硬切 successor（§5.8）、唤起恢复：业务理由不同，物理动作相同。节点启动是控制面最容易崩在半路的动作（落 lease、建 session、灌 context 跨多个步骤），采用两阶段：

1. **先落 `provisioning` 意图记录并 flush**：含启动类型（新建 / rollover / 唤起）、精确路由、lease id、fence token、预装包引用与 hash（预装契约见 §5.8：内容先成包，启动只提交）；rollover 场景另带同 `node_id`、新 `context_epoch`、`predecessor_session_id + checkpoint_id`——意图先于执行持久化。
2. **建 child session 并提交预装包，hash 比对确认其持久接受后，才提交 `active`**——"吃进初始内容"从严密的语义判断变成严格的 hash 比对。

崩溃恢复时，未终结的 `provisioning` 记录对照 child 独立持久化的 session 对账：parent 匹配 + 描述符匹配 + provider 匹配 + 初始内容 hash 一致（rollover 场景另校验 checkpoint 引用与 predecessor 状态）→ `active`；任何不符 → `failed`。若恢复与进行中的创建竞争（对账判 failed 时它其实已 active），报类型化冲突并 drain 多建的 child，不静默并存。rollover 场景还有一种半截态：§5.8 的 CAS 登记已存在但 `provisioning` 未落盘（崩在两步之间）——恢复者补走 provisioning 及后续步骤，幂等。

与 fence 的分工：fence（§5.8 的 CAS + 唯一 successor 登记）防"旧 session 超时后复活、覆盖新 attempt"，并保证"最多承认一个 successor"；对账防"建子动作本身崩在半路"，保证"被承认的那个真的开起来了"。两者覆盖不同的故障窗口。

**深度保持（实现约束，经源码核实）**：DSH 的 `delegationDepth` 在 spawn 时单调 +1（`childDepth = parent + 1`，超 `maxDepth` 直接拒绝）。rollover / 唤起的 successor **必须保持 predecessor 的深度**——rollover 不加层是 §5.8 已定原则，实现上显式复制 predecessor 的 `delegationDepth`，不走标准 spawn 的 +1 路径；否则默认两层配置下切几次窗就撞 `maxDepth=3` 的物理上限。只有新建（派生 / 分裂 / 裂变）才 +1。

**终态优先（已确立）**：外部终态压过一切恢复补完——`terminated` / deadline 封存生效后，未完成的 `provisioning` 一律作废并 drain 已建 child，§5.8 CAS 登记的 successor 作废；`finalizing` 节点允许完成原子发布，或被显式取消为 `aborted-finalize`（已落盘证据保留、不解锁后继、释放 lease）——`finalizing → aborted-finalize` 注册为合法转换进 invariant。节点超时时钟从 `active` 起算；rollover 两阶段与等待点数差额（reweight-wait）期间为**超时抑制窗口**——超时只作用于 `active` 节点，对 `provisioning` 只能取消启动意图并 drain child；reweight-wait 经 `node_reweight_wait` 事件标记进入/退出，等待期间 tick 豁免且不耗重试预算，成功 reweight 同事务原子清除。

### 9.4 通知不带状态

唤醒/等待原语只报告"有变化或超时"，**不携带状态内容**；被唤醒方重新读控制面投影。通知与状态读取之间的 TOCTOU 从设计上消除——等待者拿到的永远是自己重新读到的当前状态，而不是通知发出时的过期快照。首个实际用例：协助者崩溃时，控制面 wakeup 主执行者（只报"有变化"），主读控制面看到协助者 `failed`，决定重拉（走点数准入）或收拢单干。

### 9.5 分裂对的 peer 通道

拓扑通信规则：**派生、裂变保持星型**（一切经 Lead 中转），**仅分裂自带双向 peer 通道**——1 主 1 副同构协作时，让两半经 Lead 对齐接口等于把 Lead 变成两根同构管道之间的路由器，浪费且慢。

- 通道由代码随分裂动作建立；不是 LLM 的可选项。
- 借 DSH mailbox 全套：消息先 queued 并 flush、target 持久化后才写 delivered（queued-minus-delivered 即恢复重投集）；target 侧以消息 id + 发送者做去重键，崩溃重投不复制；quiet / wakeup 两种投递模式（可等待的更新 vs 立即移交）。
- **通道承载主→协助者的轻量分工与协助者→主的回报**，与"指挥链单向"不冲突——parent Lead → 主执行者 → 协助者这条链是显式的。**通道不是验收通道**：协助者不独立提交，work item 由主执行者统一提交 parent Lead 验收（§7）。消息全部进 evidence，验收时 parent Lead 可审计主从实际分工；消息会进接收方模型历史，token 成本照常记账。
- **关闭与在途消息**：通道随分裂退化或 work item 进入任一终态（`accepted` / `terminated` / 上交 / 封存）关闭——不止绑 accepted。关闭 = 停止接受新消息；已 queued 未投递的按 §9.6 封存三段式做有界结算（投递完毕或超时后记录丢弃），不静默消失。
- **通道绑定 work item，不绑定物理 session**：主执行者自身硬切 rollover 后，新 session 接入原通道；通道历史经 evidence / 检索对新 session 可见，不回灌窗口。
- **退化时的证据**：分裂退化收回协助者时，协助者 transcript 先落 evidence（不验收、不晋升记忆），再关闭通道与节点。

### 9.6 Team 封存三段式

Team 或 root task 封存按固定顺序：**准入截止**（停止一切新准入，单一取消事实；准入截止同时封死 `start_node`——item 创建与节点启动是同一扇门的两侧）→ **有界结算**（等待已准入操作完成，取消类失败不算错误，其余失败聚合报告）→ **超时兜底**（结算不得超过配置上限，超时明确失败而非悬挂）。实现口径：上限由 `RootExecutionSpec.settlement_timeout_seconds` 固化（默认 600s），结算起始时刻以 `seal_settlement_started` 事件 ts 计，超时由 tick 驱动 `finish_seal(timed_out=True)`；finish_seal 对子树内全部在途 item 推终态（记 `deadline-stopped`）、关闭 peer 通道（payload 显式记 dropped 消息），子 Team 封存只作用于 `team_subtree_ids` 子树。

### 9.7 借 / 改 / 不借清单（DSH experimental agent-team）

| 处置 | 内容 |
|---|---|
| **直接借模式** | 两阶段 spawn + 对账恢复；invariant append 前校验；串行化事务链；append-flush 后才报告成功；CAS + 全图重验；id 永不复用；封存三段式；mailbox 去重键与恢复重投；通知不带状态 |
| **改造后借** | 任务板：DSH 是黑板模式（成员自主 claim / release / reopen），DPswarm 是指挥链模式（Lead 派单 + 验收）——成员自主权不借，CAS / 环检测 / 删除保护 / tombstone 借；扁平 roster → 嵌套 Team + root 容量（§7） |
| **不借** | 单进程共享 cwd（DPswarm 文件写入要 staging / worktree + gated merge）；write scope 仅作提示（官方自述可被 Bash / formatter 绕过）；全开放 peer 消息（只分裂对开） |
| **官方自证，印证已有决策** | 不自动释放任务 owner（印证 lease 保守持有，§7 点数 Hook）；team 事件从不进入模型历史（印证 §5.6 控制事实不进 model window） |

---

## 10. 留白：对话未覆盖、暂不写入的部分

《DPswarm-架构设计.md》（保留未动）中以下内容属于延伸设计，对话里没有讨论过或你尚未确认，**本机制文档不收录**，待逐条讨论后再决定去向：

- DelegationRecord / 画像库的字段级 schema 与 SQL
- ~~先验的具体参数形式~~（已定：S/A/B/C/D 全局分级 + 桶内实测修正，见机制六）
- 任务分桶规则（V1 暂以 AA 分维度作桶代理，见 §8）
- ~~"fork+异模型"的实现臂（自定义工具实例）~~（已排除：§5.8 确立硬切优先 fresh + capsule，fork 只适合同构前缀继承，异构续接不以 fork 为契约）
- ~~`agentRouteDefaults` 静态钉路由通道作为在线选模权威~~（已排除：它最多只能做部署默认，不能替代 Lead 显式选择）
- 实验的臂配置细节（三臂、厚度摆放位置、TTL 协变量）
- GLM 接入的 YAML 配置
- 路由层的离线 replay 策略
- 实时插件骨架与目录结构
- `ModelPointPolicy` 权重公式（§7 点数 Hook 只确定了 Hook 与占用生命周期）
- 独立 reviewer 的角色语义（何时拉起、谁验收其产出、与 Lead 轻/重审的关系；§7 已把它当占点控制节点使用，语义待定）
- layer cap：是否需要"整层所有子 Team 合计上限"（§7 的 50% 现为每子 Team 本地口径）
- 人工 override matrix：除已确认可越过“级别方向”外，权限、root 容量、深度、worker 槽和节点点数能否越过以及如何审计
- root task 的最终验收权：由人工、policy gate 还是显式 auto-accept 发布 root `accepted` 并释放 root Lead lease

其中每一条我都有源码层面的核实结论，随时可以拿出来逐条过。

---

## 附：机制全景

```
 ┌────────────────────────────── 闭环 ──────────────────────────────┐
 │                                                                 │
 │ 任务 → 主agent(默认单干) → 拓扑判断(全程可做·人工可 override)     │
 │          │ 保持单干 / 派生 / 分裂(1主1副) / 裂变(默认≤3·可配置)    │
 │          │ root 共享 worker 槽与节点点数；子 Team 本地上限默认半额 │
 │          ▼                                                      │
 │  人工指定优先，否则 Lead 选择精确 provider / model / effort       │
 │          │ 代码硬准入：路由·级别·深度·槽位·点数 lease             │
 │          ▼                                                      │
 │  Memory Service / Context Assembler                             │
 │   ├─ 代码按 scope·权限·版本·token 预算检索                       │
 │   ├─ 必要时唤起 context manager LLM 做语义压缩                   │
 │   └─ 输出带来源、revision、hash 的 context package               │
 │          ▼                                                      │
 │  agent node 执行 ──缺料→ pull Memory Service 补料(§5.4)          │
 │          │                                                      │
 │          ├─ 达窗口阈值 → checkpoint capsule → fresh successor   │
 │          │                 同 node/work item/lease/worker 槽     │
 │          │                                                      │
 │          ├─ submitted → Lead 验收                               │
 │          │      ├─ finalizing → 提交证据/package → accepted      │
 │          │      │                解锁后继·释放槽和点数            │
 │          │      └─ rejected → 归因重试(≤2次·lease保留)·超限→上交 │
 │          ▼                                                      │
 │  观测更新画像(V1 只攒不用) · 注入事实来自 AA 分维/bench(§3§8)   │
 └─────────────────────────────────────────────────────────────────┘

 外部持久层（不依附任何 agent window）：
   Control State / Event Log  |  Evidence / Artifact  |  Accepted Durable Memory

 控制面一致性（机制七）：事件日志独立存储为真源 · invariant 落盘前校验
   单写者进程 per-root 串行事务(watchdog 只投建议) · 两阶段启动对账
   通知不带状态 · 分裂对 peer 通道（其余星型）

 横向约束：KV cache 不可跨模型（同构前缀复用只是优化）
           记忆只做召回，控制状态以事件/结构化存储为准
           时间护栏：首次+重试≤2 · 单节点超时 · 树级 deadline 默认开启
           委派经济性边界（读到刚好够判断 / 盈亏线）
```
