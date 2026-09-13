# 验收修复计划审查

日期：2026-09-12。对象：[acceptance-repair-plan.md](acceptance-repair-plan.md)，185 行，SHA256 `598f0aac3dab17f15a1a851e41274503ad16b929c7a9c1ca626a51131f340767`。

结论：方向成立，但尚不宜按当前正文直接实施。确认 **2 项 P1、4 项 P2** 计划缺口；应先补齐接口及反例，再进入开发。计划正文和产品代码未修改。

范围：对照当前源码，由三个独立审查分支分别核对任务语义与验证、执行生命周期、文件快照与分阶段交接。主要依据是源码及具体状态反例；分阶段读写另做了一个纯内存 hook 探针，没有写入作品文件。未运行供应商模型、未启动服务，也未把拟议协议当成已经实现。

## 1. [P1] 可信用户修订缺少接回原任务血缘的操作

位置：[计划第 43 行](acceptance-repair-plan.md:43)，以及第 78 行。

计划允许后续可信用户消息建立新 requirement revision，却没有定义新消息与原交付 lineage 的关联。现有 `team-required.js:35–64,168–172` 按最新用户消息生成新 binding；`fixed-team.js:976–977` 又要求返工的当前 binding 与首次冻结值完全一致。

触发例：用户说“改成静态图，取消踏频要求”。这是合法的同一交付需求修订，但按现有入口会得到 `REWORK_TASK_MISMATCH`，或被迫开新 run，旧 finding 如何继承再次悬空。稳定 finding ID 和普通 generation 更新不能自动解决跨用户消息的接续。

修订要求：增加同一交付的 amendment 操作，原子绑定原 lineage、父 requirement revision 和可信新消息，再生成新 revision；返工校验接受已认证的修订关系，仍拒绝无关新任务。对已不适用的 finding 记录“因需求修订排除”的依据，不能删除历史或假称代码已修复。

回归：用户取消一项要求 → 原观察与裁决历史保留 → 该项按新 revision 合法排除 → 原血缘继续返工和验收；无关任务不能借此接入。

代码入口：[team-required.js](../../dpswarm-dsh-plugin/lib/team-required.js:35)、[fixed-team.js](../../dpswarm-dsh-plugin/lib/fixed-team.js:976)。

## 2. [P1] 候选快照清单没有定义必要依赖的范围

位置：[计划第 47 行](acceptance-repair-plan.md:47)，以及第 49–51 行。

“明确交付范围内的文件集合”没有区分修改文件与影响运行结果的未修改依赖。现有候选路径采集主要来自成功的 write/edit；`write_globs` 是写权限范围，二者都不能直接充当完整的运行快照清单。

触发例：只修改 `index.html`，它加载已有 `motion.js`、CSS 或图片。如果 manifest 仅封存 HTML，Reviewer 可能在现有工作区中借助正确的依赖通过检查，快照却缺少依赖；或之后同一个 HTML 加载不同依赖而改变行为。无需发生外部写入竞争，HTML 哈希仍然不够。

修订要求：区分 `changed_paths/edit_scope` 与 `candidate_files`，明确交付入口、必要本地依赖、保留目录结构的验证视图及无法封存时的 unknown 规则。无需建立无限依赖扫描：可先把强保证限定于经过检查的自包含文件；含未封存必要依赖的任务不能被描述为完整的独立快照验收。

回归：HTML 字节不变、依赖字节不同；缺必要依赖；完整封存依赖的正例。验证不应从只写了一个文件推导它只有一个运行输入。

代码入口：[worker-diagnostics.js](../../dpswarm-dsh-plugin/lib/worker-diagnostics.js:73)、[fixed-team.js](../../dpswarm-dsh-plugin/lib/fixed-team.js:335)、[types.py](../../dpswarm-plugin/dpswarm/types.py:385)。

## 3. [P2] 报告格式错误后的补交没有可执行的恢复路径

位置：[计划第 84 行](acceptance-repair-plan.md:84)，以及第 144 行。

计划允许在原有额度内修正报告，但现有 native worker 在提交前已经终态并 dispose；已提交的非空报告包不可替换，重复提交会触发非法 `submitted → submitted`。当前 `dpswarm_rework` 仅接实现者，Reviewer 重跑附着实现返工并使用返工额度，不能直接兑现“只修报告、沿用原额度”。

触发例：Reviewer 已检查正确候选且还有额度，最后 JSON 缺少标记。严格门禁应拒绝解析，但它随后没有计划所承诺的补交入口，只能停住、无关地重跑实现，或改由 Lead takeover。

修订要求：明确 report-only continuation 的入口、执行血缘、新身份、新报告包/修订、原授权额度的结转计量、当前 Reviewer 绑定与清单重封口；保留错误旧报告，不替换旧包。也可选择在 worker 终态前完成格式校验和有界纠正，但必须明确该生命周期设计，而不是假设已 dispose 的会话还能继续。

回归：作品未改、原 Reviewer 会话已结束、原报告不合规 → 合法只修报告 → 正常计量并重新接受；重复 API 请求不增加重复报告或费用事件。

代码入口：[subagent-run.js](../../dpswarm-dsh-plugin/lib/subagent-run.js:57)、[delegation.js](../../dpswarm-dsh-plugin/lib/delegation.js:261)、[server.py](../../dpswarm-plugin/dpswarm/server.py:978)、[fixed-team.js](../../dpswarm-dsh-plugin/lib/fixed-team.js:957)。

## 4. [P2] 输入证据版本与 Reviewer 自身提交版本需要分开

位置：[计划第 72 行](acceptance-repair-plan.md:72)，以及第 88 行。

计划要求所有验证报告入账，且最终 ReviewRecord 覆盖最新 revision；没有说明最终 Reviewer 自己的报告是否属于这个输入集合。若使用同一 revision，可能出现：Tester 入账 r7 → Reviewer 审查 r7 → Reviewer 自身报告入账变 r8 → 接受因只审过 r7 被拒 → 补充报告又变 r9。

这是**采用同一版本域时会触发的设计风险**，不是声称现有系统或所有实现必然死循环。当前 Reviewer 确实作为普通工作项提交，因此实现者需要明确的版本定义，不能自行猜测。

修订要求：区分封存输入的 `evidence_revision`、审查决定/报告 revision 和事务用的 `ledger_revision`。最终 Reviewer 自身入账不自动使其输入过期；新增外部证据才使输入失效。Reviewer 自己新增发现与相应裁决采用明确的原子登记规则。

回归：无新外部证据时，一次最终审查可以接受；审查期间新外部报告入账必须使旧判断过期；提交审查本身不造成无限补交。

代码入口：[fixed-team.js](../../dpswarm-dsh-plugin/lib/fixed-team.js:776)、[delegation.js](../../dpswarm-dsh-plugin/lib/delegation.js:261)。

## 5. [P2] staged 实际消费版本缺少固定与记录接缝

位置：[计划第 168 行](acceptance-repair-plan.md:168)。

计划要求最终 Reviewer 核对下游实际消费的上游版本，但最小合同没有消费引用，实施表也没有明确把读取、写入工具接到快照。现有状态板的 version 随状态事件递增，不随文件内容变化；read hook 只检查 ready/frozen/done，write hook 只检查写范围。

纯内存反例：A 为 ready/version=4 后，受管 edit 仍允许修改 A；B 读取同一路径也被允许；状态版本仍为 4。探针结果为 `writeAfterReady.permitted=true`、`downstreamRead.permitted=true`。因此 A1 封存后 A 变成 A2，B 可读到 A2，而状态板与旧快照不能证明它实际使用了哪一份。

修订要求：ready 绑定 manifest digest；下游读取固定快照并记录 `consumed_manifest_refs`，或采用同等可证明的版本交接机制；最终整合候选和 ReviewRecord 绑定该集合。必要依赖的实际读取版本不能证明时，不标为已核验。补入对应的读取/写入接缝文件，不改变阶段调度语义，也不需要增加模型角色。

回归：A1 ready → 受管写为 A2 → B 实际消费版本可确定；未声明新版本不得静默复用 A1 的验证结论。

代码入口：[write-scope.js](../../dpswarm-dsh-plugin/lib/write-scope.js:149)、[types.py](../../dpswarm-plugin/dpswarm/types.py:380)、[fixed-team.js](../../dpswarm-dsh-plugin/lib/fixed-team.js:323)。

## 6. [P2] 模型试点没有强制重现本案的错误分类上下文

位置：[计划第 158 行](acceptance-repair-plan.md:158)，以及第 160 行。

当前试点只明确好坏候选及旧/新合同对照，允许对干净坏 HTML 做审查。旧、新 Reviewer 都可能正确拒绝，八次 canary 全部达标，但真实返工里遇到 Lead“远腿已正确，禁止修改”与 Tester“反向分支，但 background/non-blocking/out of scope”时，仍沿用错误分类。

这不是要求程序解决全部语义判断。缺口是已知事故的触发上下文没有被明确纳入它自己的修复验收；预制结构化 finding 的确定性回归也不能替代这一检查。

修订要求：不增加调用次数，把已有本案对照固定为包含上述错误处方与错误 proposed_classification 的输入。坏样本的成功判据必须包括保留 observation、推翻错误分类、保留 unresolved defect 并拒收；正确样本和纯偏好正常通过。其余新例用于有限泛化检查，不把已知题当盲测。

事故依据：[根因报告](session-6ccc299c-root-cause.md)。

## 修订顺序及保留判断

先定义需求 amendment、report-only recovery 和三个 revision 域，再确定候选依赖与 staged 消费记录；最后把相应反例并入原计划矩阵。上述主要是补齐合同和验证，不建议因此再扩建一个通用调度器。

以下原计划判断成立，应保留：编辑范围与验收范围分离；缺陷跨轮保留；Python 作为权威接受事务；旧会话不伪造新保证；staged ready 不强行等同 accepted；未知不冒充成功；真实模型试点与确定性测试分别报告。没有把“同模型必然失败”或“缺一张截图”当作唯一根因。

本次审查没有运行新合同测试，因为相关功能尚未实现；内存探针只验证当前 staged 接缝存在的行为。以上发现是对计划可实施性及测试覆盖的评价，不是新实现的生产故障清单。
