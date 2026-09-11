# v16 之后的工程完善计划与实验准入契约

日期：2026-09-05。状态：**待实施的计划**。本轮只读检查源码、已有测试和冻结证据；没有修复实现、运行测试、启动模型调用、容器或新实验。本文只负责工程准入；实验题目、样本量和费用上限由同目录总计划统一冻结。

路径约定：`B = K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903`；源码锚点使用本次读取的行号。当前 `B/runner.py` 与 `B/pilot_v16/source_snapshot/modelbench/swe_fixed_team_20260903/runner.py` 无差异。历史记录保留原样；新增修订不得改写旧 gate、旧 manifest、原始结果或旧模型输出。

## 1. 工程结论与优先顺序

**先完成宿主故障处理、工具声明契约和真实补丁状态记录，再开展 CM、装配、Lead 互换等效果实验。** v16 的数据同时含有预算快照写入失败、被声明却不可执行的 scout 工具、编辑意图与实际落盘的混淆，以及完成交付后的补丁错误。仅增加重复次数会继续混合这些因素。

建议按下列依赖推进；编号是本文工作项，不代表已经建立的 issue 或已实现功能。

| 工作项 | 优先级 | 必须先解决的问题 | 完成后解锁的实验 |
|---|---|---|---|
| E1 持久化与故障传播 | P0 | `budget.json` 写入异常不能隐身于 Worker 状态中，也不能导致重复计费/重复请求 | 全部后续付费运行 |
| E2 scout 阶段工具契约 | P0 | 模型看见的工具必须与该阶段实际可执行工具一致 | 装配 ON/OFF、scout/package 分解 |
| E3 真实工作树状态 | P0 | 编辑尝试、非空 diff、完成交付分别记录，F1/F5 不再把意图当落盘事实 | 宵禁、推进提示、阶段归因 |
| E4 closing 边界验收 | P1，后续工程 canary 前完成 | 真实账本进入保留区间；只豁免 Lead reserve，不绕过其他限制 | F3 是否有效的专门对照 |
| E5 工件与采用来源 | P0 | 缺文件不等于空文件；自动 discard 不等于模型审阅或文件删除 | 交付机制、整合收益、失败成本分析 |
| E6 分层错误分类与批次停止 | P0 | 证据完整性、执行健康、官方正确性分开；异构 Team 也须接受覆盖检查 | 公平比较与可靠累计报告 |
| E7 配置、环境及角色冻结 | 对对应实验为 P0 | 冻结真正生效的配置；修正 Lead 身份提示；先识别已知环境噪声 | Lead→Worker 反向对照、资源/预算对照 |

这些不是互相独立的新研究臂。E1/E2/E3/E5/E6 是共同运行底座的修订，应先形成一个新的冻结基线，再在该基线内随机/交错比较实验臂，不能把不同底座直接合并为同配置重复。

## 2. 当前已经可用的配置与尚未实现的能力

主入口为 `B/runner.py:162` 的 `SweRun.__init__`；`limits_override` 只允许已有键，并与默认 `LIMITS` 合并（`:179`）。`B/cli.py:166` 的 `prepare` 冻结 wave、每次运行的配置和源码指纹，`:374` 的 `verify` 检查冻结输入未漂移。已有历史 wave 不是任意新实验矩阵的通用配置生成器；新波次必须新增并验证调度定义。

| 能力或开关 | 当前状态/默认值 | 使用边界及计划要求 |
|---|---|---|
| 固定团队及异构 Worker | `solo`、`fixed_team`、`hetero_team`；异构恰好两个不同模型，顺序为 W1/W2；见 `runner.py:162`、`:303` | `fixed_team` 可做同模型双 Worker。当前强制组队源为 `experiment_protocol`，不是动态启用决策；生产/测试角色提示也要随 manifest 一起冻结 |
| Lead 模型 | `lead_model` 接受精确目录模型，默认 `gpt-5.6-sol`；`:165` | Team 提示仍写死 “You are the Sol Lead”（`:291`），CLI 历史调度默认 Sol（`cli.py:200`）。不能只换配置字段就声称已经完成公平的 GLM Lead 实验；E7 先修提示/调度/记录 |
| 主要预算 | `max_calls=28`、`token_limit=600000`、`worker_calls=8`、`lead_reserve_calls=2`、`wall_seconds=1800`；`:34` | 可覆写已有参数。调用预留与实际 usage 不同；token 上限是准入账本约束，不能把保守预留不足停止解释成实际 token 已用尽 |
| CM 基本控制 | `cm_enabled=True`、`cm_model='deepseek-v4-flash'`、`cm_provider='deepseek'`；`:39` | 可配置，但 provider、模型、thinking 必须相互兼容并通过配置验收；CM 费用和调用不能漏计 |
| CM 池/阈值 | `cm_call_allowance=12`、`cm_context_budget=12000`、`cm_keep_recent=4`、`cm_max_tokens=4096`；`:38` | `cm_call_allowance=None` 是历史 CM 共享主调用票语义；独立池仍共享总 token 账本。F2 是数值参数，不是布尔 OFF 开关 |
| F1 宵禁 | `cm_edit_curfew=True`；`:51`、`_maybe_compress_context(:435)` | 当前依赖命令字符串正则命中，尚不等于真实编辑阶段。关闭可恢复旧压缩行为，但解释 F1 需要先完成 E3 |
| F3 closing reserve 豁免 | `closing_call_reserve_exempt=True`；`:53`、`_call(:394)`、`_closing_call(:800)` | 已实现 finish-only closing 和 reserve 豁免；v16 四次 closing 准入前余票均为 8/6/7/8，未到最后 2 票区间。缺的是关键分支的有效实证，不是缺开关 |
| F5 提示 | `edit_status_banner=True`；`:57`、`loop(:700)` | 当前以“消耗超过半数且未命中编辑正则”提示；v16 Team 的波次配置关闭它。E3 后再判断真实无编辑提示的效应 |
| 装配三开关 | `cm_team_memory=False`、`cm_scout_distill=False`、`cm_bootstrap_package=False`；`cm_package_budget=6000`；`:47` | 已有 memory/scout/package 路径，但不代表所有 2³ 组合均语义有效。新消融应检查依赖，不能将实际无效的组合标作新增处理 |
| F4 交付取证 | Worker `death_phase`、cleanup delta bytes、最终 patch bytes 已实现；`:381`、`:1024` | 没有独立 F4 OFF 布尔开关，且原始状态语义需修正。应升级观测 schema，保留旧 raw 字段，不把“移除记录字段”当交付机制消融 |
| 环境资源 | `memory='3g'`、`cpus=2` 会传入环境；`:981` | 值变更会改变环境/性能边界，必须作为单独处理冻结。`model_concurrency=4`、`container_concurrency=4` 的信号量在模块加载时建立（`:59`），仅 per-run override 并不会重建这两个信号量，不能据此做有效并发消融 |
| Worker 数量 | 构造 CP 和线程池均固定为 2；`:196`、`:208` | `active_workers`/`delegations` 出现在 LIMITS 不等于具备任意 1/3/N Worker 臂。改变拓扑需要单独实现与检验 |
| 动态启用/选型/换人 | 当前 benchmark 只有上述三种 condition，固定 bootstrap | 这条 SWE 实验执行路径尚无可直接验证的动态启用、动态模型路由、执行中换人臂。仓库其他控制面/路由原型不能当作本 benchmark 已实测策略 |
| 美元约束 | 已有离线 API 等价成本估算 | `RunBudget` 仍按调用/token/时间准入；不存在已生效的实时美元预算门禁。等成本多次 Solo、按费用动态组队等需要新的调度/准入能力，不能只依赖事后画图 |

`REV11_FIX_PLAN_20260904.md` 与 `gate_revision11.json` 中“所有五项均可单臂关闭”的概括应在新计划中纠正：现有独立布尔旗标是 F1/F3/F5；F2 是 `cm_max_tokens`；F4 是记录/分类代码。也不要沿用旧计划“cleanup 物理丢弃补丁”的原因叙述，现有取证已证实文件保留。

## 3. E1：预算快照持久化、计费一致性与宿主异常

**证据。** v16 Seaborn OFF r1 的 GLM W2 记录 `PermissionError: [WinError 5] ... budget.json.tmp -> budget.json`，Worker delta 文件缺失；run 的 `infrastructure_error` 却为空。`runner.py:79` 的 `dump` 用固定临时名写入再 replace；`:226` 的 `event` 已在 `RLock` 内先追加并 fsync 事件，再写预算快照。已有锁，且没有完整锁持有者/traceback 诊断，**不能认定线程竞争、杀毒软件或某个具体进程就是根因**。需修复的是已证实的失败处理与诊断不足。

**涉及模块。** `runner.dump/event/_call/worker_run/run`（`:79/226/394/933/975`）；`dpswarm-plugin/dpswarm/team_runtime/ledger.py` 的 `RunBudget.reserve/complete/snapshot`（`:108/153/179`）；必要时提取共用的原子写 helper，但不能未经验证批量替换其他持久化模块。

**拟修改契约。**

1. 预算票据仍须先预留、后调用、再按同一 call_id 结算。对同一次已获得模型响应的调用，不得因落盘异常重复发起请求；未知 usage 不能填零或随意释放预留。
2. 快照使用同目录唯一临时文件、完整写入后原子替换。对已分类的瞬时 replace 拒绝可做有界重试，例如至多 3 次、总等待不超过 1 秒；最终参数在实现前冻结并计入 wall time。重试范围仅限尚未提交的快照替换，**不能重放事件追加、预算预留、模型请求或 CP 决策**。唯一临时名/重试是韧性措施，不是已证明的根因修复。
3. 失败诊断至少含操作阶段、errno/winerror、尝试次数、相对文件路径、call_id/agent_id、预算票据状态和脱敏 traceback。使用独立的尽力写入/内存诊断出口，禁止在 `event→dump` 失败处理内再次递归调用同一持久化链。
4. 持续宿主持久化失败使 run 进入明确 `host_io_failure`：停止新模型准入，有界回收已发请求、结算可观测 usage，保存可读取工件。若不能建立可靠的最终计量和冻结证据，该新 run 不进入正式评分，明确记录 ungraded 原因。历史已经评分的故障 run 保留原评分，另加污染标记；不得替换成“没有这次运行”。
5. 明确快照与日志的一致性/恢复契约。当前 `budget.json` 不能在尚未证明日志可完整重建时被任意降格为可忽略缓存。短期采用保守停止，不声称已支持断电后自动恢复。

**有意义的离线验收。** 不访问真实模型；用真实 `RunBudget`、受控 transport 和临时目录注入精确失败点。

| 用例 | 必须验证的结果 |
|---|---|
| 预留后、发送前 snapshot replace 持续拒绝 | transport 实际调用数为 0；停止新准入；票据状态可解释；没有自动整条 run 重试 |
| 响应到达后结算/快照拒绝 | 已发模型调用仅 1 次、原始响应与 usage 保留；无二次计费、无重复 completed ticket |
| 事件已追加后 replace 首次拒绝、随后成功 | 事件仅追加一次，最终 snapshot 合法且对得上票据；旧 snapshot 在提交前仍可读取 |
| replace 持续拒绝或临时文件写入失败 | 有界结束，不递归报错，不死锁；run/worker/batch 能看见同一宿主故障 |
| 两 Worker 与 Lead 同时结算 | 无丢记录、重复 call_id 或负预算；跨阶段注入故障后仍无多发请求 |
| 本机 Windows 中文路径、受控文件句柄阻止替换 | 文件问题被准确归类；释放句柄后允许的短重试可恢复；该测试只证明处理契约，不反推历史锁竞争 |
| 取消/关机阶段发生持久化失败 | 不越过取消或 deadline 继续尝试；已知/未知 usage 与遗留工件状态显式输出 |

**Live 准入。** 上述精确阶段故障注入全部通过；至少包含当前 Windows 文件系统上的真实原子替换验证。新小型工程 canary 中 persistent host IO、重复调用/票据、未知或未结算 usage 均为 0 才能扩大执行。出现一次同类宿主故障即停止后续排队臂并保留证据，不用额外重复悄悄填补样本。

## 4. E2：scout 工具声明与执行阶段一致

**证据/锚点。** v16 四个 ON run 各出现两次 collect W1/W2 被拒，共 8 次 `SCOUT_TOOL_UNAVAILABLE`。`_scout_round` 声明 `BASE_TOOLS+LEAD_TOOLS`（`runner.py:593`），实际仅执行 bash/finish（`:620`）；scout 发生在 `start_workers` 之前（`:989/994`）。这不是候选误用一个未声明工具，而是宿主对已声明工具的执行承诺不一致。

**拟修改契约。** 首选将 scout 定义成一个明确的、只具备 bash 的 Lead 探查阶段，提示写明 Worker 尚未开始；需要结束该单次阶段时由宿主完成，不让模型 collect 未运行的 Worker。若另选保留 finish，必须另行定义它是否结束 scout 或整条 run，并先验收与未审阅 Worker 的关系。工具声明、解析校验、实际分发必须共用阶段允许集，正常 Lead loop 再恢复完整 Lead 工具。不能为了消除拒绝记录而悄悄提前启动 Worker，这会改变 scout-before-worker 的处理定义。

**拟修改位置。** `runner._scout_round/prompt/execute_tool/loop`（`:585/267/843/700`）；阶段工具集合可新增辅助函数，当前尚不存在。`assemble_worker_package(:544)` 与 `_distill_scout_note(:640)` 保持输入来源可追溯。

**离线验收。**

- scripted transport 捕获实际 declarations：scout 只含约定工具；Worker 的 fork/start 在 scout 和蒸馏完成后；正常 Lead 后续 collect/review 可用。
- 负例强制生成 collect：在解析/阶段权限边界拒绝，`executed=false`，不读取 Worker future，不产生错误采用或额外等待。
- ON 路径的 scout→distill→package 引用同一有效工件哈希；关闭 scout/package 的有效组合不产生对应调用、记忆或隐藏 token 开销。
- 配置生成器拒绝语义无效的开关组合，或明确提供可独立工作的组合语义。不要让 package 开启却没有可用输入的静默 no-op 混入“package ON”统计。
- 协议错误、超时、空探查输出的回退行为被预先固定并记录；不能无记录地把 ON 运行变为 cold-start OFF。

**Live 准入。** 阶段声明/执行一致性离线通过，新增环境 smoke 后再做小型 ON canary；常规正常输出不再产生已声明工具不可用事件。此处只确认实现可运行，不据 canary 少量成功样本声称装配提高正确率。

## 5. E3：编辑尝试、实际 diff 与交付分类分离

**证据/锚点。** `_detect_worktree_edit(:347)` 在执行前按 bash 字符串正则置位。v16 raw `edited_no_delivery=7` 中有 5 名 W1 最终 delta 为 0；还有命令最后退出码为 0、但此前替换未成功的情况。`worker_delta_bytes(:374)` 把 `OSError` 返回 0，混淆缺文件/不可读取与实测空补丁。F1 和 F5 也使用相同的命中状态。

**拟修改契约。**

1. 分开记录 `edit_attempted`、`first_change_observed_ordinal`、`baseline_diff_present`、`delta_state`、`delta_bytes`。`delta_state` 至少有 present/empty/missing/unreadable；后三者不能都用 bytes=0 表示，缺失/不可读用 null 加原因。
2. 每个改变状态的候选工具执行后观察工作树，而不是依据命令是否包含 `write_text`。保留命令尝试事件，但将旧 `worktree_edit_detected` 的字面含义明确为历史正则探测；新增字段/schema 不覆盖原 raw 值。
3. 工作树探测区分“曾出现实际变化”和“当前相对基线仍有变化”，包含 untracked、删除、二进制修改和后来恢复为原样。Worker 基线是 fork 的 baseline；Lead 通过采用 Worker 补丁产生的变化要带 `origin=adoption`，不能记作 Lead 自行编写的第一处代码。
4. F1 的新规则建议采用“首次观察到实际工作树变更后进入编辑阶段”，即使后来 revert 仍保留已进入编辑阶段事实。F5 的提示必须使用真实观测状态，未知时显示 unknown，不声称“尚未编辑”。若采用其他阶段策略，应作为独立冻结策略，而不是悄悄改变原 flag 的解释。
5. 观察工具不能执行模型补丁、仓库 hooks 或网络请求。`SWEEnvironment._tree/export_patch`（`modelbench/swe_verified_20260903/environment.py:413/419`）提供可复用的 git 临时 index 思路，但现有 `export_patch` 会写工件，不是纯状态探针；需新增轻量只读状态接口并验证时间开销。探测开销也算 wall time，不修改模型命令去强行增加 `set -e` 等新行为。

**离线验收。** 使用真实临时 git 工作树验证语义，或用实际文件 fixture 驱动环境；不能只验证正则本身。

| 输入情景 | 预期观测/策略 |
|---|---|
| 命中编辑正则，但 anchor 不存在，写入前抛错 | attempt=true；实际 diff=false；不触发基于真实编辑的 F1/F5 状态变化 |
| 上一子命令写入失败，后续打印使总体 exit=0 | 同上；退出码不替代 diff 证据 |
| 不命中现有正则的程序实际改文件 | 观察到非空 diff，记录 first-change ordinal |
| 写入成功后程序又以非零退出 | 实际 diff 仍为 true，不因 exit 非零丢失已落盘改变 |
| 新建 untracked 文件、删除文件、二进制修改 | 均被识别，最终 export 与观测一致 |
| 修改后恢复 | ever_changed=true、current baseline diff=false；阶段状态与最终 bytes 分开 |
| Lead 只采用 Worker delta | final diff 非空；来源为 Worker adoption；不虚构 Lead 自行编辑 |
| 探针权限/超时错误 | 状态 unknown 和独立错误；不能落入 no_edit/0-byte 分支 |
| 普通 stdout 含编辑关键词但未写文件 | 无实际 diff，不误触发宵禁 |

当前 `tests/test_runner_rev11.py:70/168` 的编辑命令在 `test_runner.py` 的 fake environment 中不会真的写文件；fake 只对特殊字符串改变 `.delta`。这些已有用例能证明代码分支，不能证明新观测语义。应新增实际文件用例，并保留旧 raw-schema 兼容测试（`:220`）。

**Live 准入。** 离线全部状态边界通过；固定环境测得的探针开销与 deadline 行为已记录。少量 canary 将事件状态与实际 delta/最终 patch 逐项核对，不存在“已编辑=true但仅有命令意图”的新语义误报后，再进行 F1/F5 消融。不能直接用 v16 的 raw death_phase 重新解释成已测的真实编辑策略。

## 6. E4：finish-only closing 的真实预算边界

**证据/锚点。** `_call(:394)` 在正常 Worker 调用余票不超过 Lead reserve 时拒绝；closing 且开启 `closing_call_reserve_exempt` 可跳过这个条件。`_closing_call(:800)` 只声明 finish，不授权新增编辑。v16 的四次已结算 closing 均未进入最后两票；因此 1/10 `team_execution_valid` 不能用来否定 F3。`test_runner_rev11.py:112/117/133` 目前通过改写 `budget.summary` 模拟余票，不覆盖真实票据状态与完整 Worker 结束路径。

**拟修改契约。** 保留“至多一次 finish-only closing”和当前其他限制。新增 closing admission 事件字段：余票、余 token/预留需求、reserve 值、closing flag、是否实际使用豁免、准入/拒绝原因、call_id；这些须来自同一次锁内预算观察，避免事件中的余量与实际准入不一致。`exemption_applied=true` 只在原正常 Worker 会因 reserve 被拒、此次实际获准时为真。

**离线验收。** 用真实 `RunBudget.reserve/complete` 建立余票 3/2/1/0，使用 scripted Worker 完整走到本地调用上限；不只直接调用 `_call`。

- 余票 2/1：正常 Worker 拒绝；flag ON 的一次 closing 获准；flag OFF 的 closing 被拒。各调用仍有票据、usage 和角色记录。
- 余票 0、token 预留不足、deadline 已到、外部取消、run 已进入 drain：即使 flag ON 也拒绝；不能为 closing 创造额外免费票或越过 token 预算。
- 两名 Worker 同时争夺最后一票：至多一名成功预留，无负数、无双发；Lead reserve 被 closing 使用的实际情况显式可见。
- closing 输出 finish 外的工具、非法参数或多余工具调用：不执行 bash；记录候选协议失败并按冻结策略结束，不无限追加 closing。
- closing completed 但 patch 为空、closing blocked 但 patch 非空：状态和实际工件分别保存，不用 completed 自动推断正确性。

**Live 准入/效果边界。** 真实票据边界的离线合同必须先通过；无需为了测试代码分支而先付费耗尽模型预算。之后若做 F3 实验，预先定义会有机会触及 reserve 的任务/预算处理并固定 ON/OFF；只有实际出现 `exemption_applied` 的运行能说明该机制被触发。未触发的批次结论是“该边界未覆盖”，不是“有效”或“无效”。工程合同通过也不能替代交付率/正确率收益的重复对照。

## 7. E5：工件保留、正式采用与正确性独立记录

**证据/锚点。** `worker_run(:933)` 正常分支在 `:950` 导出 delta；异常分支 `:964` 不做尽力导出。cleanup `:1024` 释放未结算交付但不删除工件。v16 三份 cleanup delta（1,620/2,748/5,379 bytes）均仍在且 SHA 匹配；还有一份 Seaborn OFF r2 的 3,411-byte 测试是 Lead 主动 discard。`reviewed=true` 可能来自 runtime cleanup，不能当作模型确实读过。`collect(:863)` 将返回文本截到 40,000 字符，完整工件访问途径须明确。

**拟修改契约。**

1. 正常与异常结束均记录 `artifact_state/path/bytes/sha256/export_error`。异常时只在环境仍可安全访问时尽力导出；保存为未验证候选工件，不自动把 worker_error 改成 completed，不伪造 CP submit/acceptance。导出失败明确 missing/unreadable，不写一个空文件冒充零修改。
2. 原始 delta、delivery 证据哈希和最终模型补丁 immutable。cleanup 只做资源/交付状态清理，任何清理策略都不得删掉本次诊断所需工件。
3. review 加 `review_actor=lead|runtime`、decision、reason、引用的 delta SHA；把“正式采用”“人工/模型手动复用内容”“最后 patch 含有该内容”分开。可通过显式 apply 工具及前后 hash 记录可证实的来源；不能仅凭内容相似自动宣称代码作者或实际贡献率。
4. 若 collect 截断，仍必须提供安全可访问的完整工件标识/途径并告诉 Lead 截断了；不能将半份 patch 当成完整证据采用。不要默默提高工具上下文到无界。
5. 保留当前 CP 安全序：先验证决策再应用，应用成功但 CP 提交失败则禁止评分（`review_worker:873`，`:894`）。冲突不伪报采用。正式采用是流程状态，官方 F2P/P2P 是补丁正确性，互不替代。

**离线验收。** worker loop 后、export 前、export 后、delivery/CP submit 处逐点注入异常，验证保存状态和 hash；导出失败不得转为 0-byte。所有 cleanup 前后验证三类工件存在且内容不变。检验 completed 非空但被主动 discard、budget stop 非空被 runtime discard、completed+adopted 但官方 fixture 判错、patch conflict、CP commit-after-apply 失败、超长 collect 可获取完整工件。沿用已有 `test_runner.py:438/451` 的 cleanup/CP 安全约束，新增故障点而非只重复现有四态断言。

**Live 准入。** 所有 Worker 都有可解释的工件状态和 review 来源；非空 delta 到最终 patch 的直接采用链可核对。允许模型交付空补丁、主动 discard 或错误补丁，它们是实验结果；不允许缺文件被伪记为已测 0 bytes，或自动清理被写成模型审阅。

**必须保留的正确性反例。** xarray OFF rep2 的两份 Worker delta 均完成且被正式采用，合并自测先有 2 failed/59 passed；Lead 修改生产 patch 后仍有 1 failed/282 passed/1 skipped，官方 F2P 0/1、P2P 280/280。应将这条历史轨迹作为分析/报告 fixture：不得被新报表归为“团队完整交付，所以代码正确”或“无执行问题的能力上限证据”。它不是要求重新执行隐藏评分测试。

## 8. E6：错误分类、健康状态和批次停止

**现状。** `transport.py:590` 分别解析 native/text 响应，记录 protocol_error；`runner.loop:741` 反馈候选格式错误并继续固定循环。`validation/audit_results.py:697` 后检查账本等完整性，`:709/710` 输出 transport/protocol 计数；有错误记录本身不等于证据被损坏。`cli.py:464` 的 required-worker 检查却仅覆盖 `condition=='fixed_team'`，漏了 `hetero_team`；`:465` 依赖 top-level infrastructure_error，不能看到被 Worker 捕获但未上报的宿主异常。

**拟修改契约。** 保留原始错误和 raw outcome，新增结构化分类，至少含 `domain`、`stage`、`agent_id/call_id`、`evidence_path`、`severity`、`retry_policy`、`affects_execution_health`。原因只到证据支持的层级，不以“模型差”“任务超纲”代替分类。run 报告并列 `evidence_integrity`、`execution_health`、`official_score`，不能用单一 PASS 覆盖三个问题。

| 分类 | v16 例子或边界 | 处理/归因 |
|---|---|---|
| 宿主 I/O | budget snapshot WinError 5 | 按 E1 停止新准入，提升 run/batch 故障；不能归为 GLM 能力失败 |
| 阶段工具契约 | scout 声明 collect 却拒绝 | 宿主实现问题，先修 E2；不归为候选越权 |
| 候选 JSON 格式 | Seaborn ON r1 Sol 单条 terminal 文本尾部多出 `]}`，`invalid_json/Extra data` | 保留已收费调用，非法工具不执行；按冻结反馈/终止策略处理。不能套用历史多条消息拼接适配器缺陷 |
| 候选工具参数 | xarray ON r2 GLM 的 `bash.timeout` 超范围 | 在 schema 边界拒绝，不能自动夹到合法范围后执行并声称原调用合法；它不是已写好测试 |
| 适配器/传输 | terminal 选择、网络超时、响应丢失 | 必须有原始响应/绑定/传输证据才归类；不能将所有 invalid_json 自动划入 adapter |
| 工作树命令失败 | anchor 匹配失败、写文件前异常 | 工具行为结果；结合真实 diff 判断产物，不把 shell 最终 exit=0 当全部成功 |
| 环境兼容 | 缺 rg、Seaborn 已存在的 pandas OptionError | 与模型补丁新增错误分列，受影响自测保留；不能据环境噪声删除官方真实失败 |
| 正常预算/策略停止 | local worker limit、LEAD_RESERVE、token 预留不足、wall deadline | 正常边界结果，不冒充宿主崩溃；已冻结 patch 仍可能通过 |
| 补丁语义/回归 | Seaborn 显式 y 限制被反转；xarray 坐标属性；Sphinx 已有用例回归 | 用官方 F2P/P2P 与候选自测证据定位，不能用 adopted/completed 代替 |
| CP/冻结完整性 | 应用后 CP 提交失败、工件哈希不符、重复票据 | 禁止正常评分/继续批次；证据损坏与候选答错不同 |

**离线验收。**

- 在 `dpswarm-plugin/dpswarm/team_runtime/protocol.py:195/223/239` 的 parser 边界重放最小化 malformed JSON、越界 timeout 和合法 native/text fixture；验证真实声明、错误代码、`executed=false` 与 usage 记录。不得“宽松修复”输出来抹平原始候选错误。
- 对 terminal 选择契约另设 fixture：单条真实畸形消息仍判候选格式错误；多条合法 envelope 不可被错误拼接。沿用 `modelbench/swe_verified_20260903/tests/test_runner_protocol.py` 的真实 parser 测试，不只用 transport stub 直接写 protocol_error。
- 在 fixed/hetero 两类 run 都模拟 Worker 无实际请求、Worker 请求后宿主异常、正常 budget stop；验证 required-worker 规则一致，正常模型失败不被误标基础设施。
- CLI 首次发现宿主故障/计量破坏后不启动剩余计划臂；已在飞调用有界收尾。异常样本和已花费用保留；恢复后若有重新运行必须是带新 run_id 的明确追加批次，不能覆盖旧结果。
- Audit 对历史缺新字段输出 null/unknown；新 schema 将工件实际状态和原 raw 字段并列；允许“完整性 PASS、执行受故障影响、官方未通过”这一合法组合。

**Live 准入。** E1–E3/E5 的已知宿主缺陷在离线及工程 canary 中不再出现；计量/CP/工件完整性为全量必达。候选协议错误、错误补丁、空补丁不能简单设成不准入样本后从通过率分母剔除，它们须按预登记策略完整报告。若某模型持续无法遵守固定协议，应按事先冻结的故障次数/费用阈值停止该臂，并报告协议不适配，不能临场给它额外修复回合再与其他臂直接比较。

## 9. E7：环境与角色互换的额外准入

### 9.1 环境噪声先有基线，不事后改评分

全部新增 28 次 run 都出现过 `rg: command not found`。Seaborn 的历史 v14 Solo、v15 Solo、v16 Team 官方输出均有 74 个相同的 `mode.use_inf_as_null` pandas 背景错误，但官方 P2P 94/94；v16 OFF 的明确 y-limit F2P 失败是另一件事。这说明要先建立环境指纹与已知噪声清单，既不能把旧噪声算成新回归，也不能用噪声替真实补丁错误免责。

计划在下一阶段获准实施后运行**无模型**环境检查：冻结 candidate/grader 镜像 digest、基础提交、依赖版本、可用检索/测试命令，检查项目导入和预先选定的公开 baseline smoke。环境变更（例如安装 rg 或修复 pandas 兼容）需要新环境 revision，并在所有对照臂统一使用；保留旧环境批次标签，不能混成同配置重复。不要把官方隐藏目标测试、失败答案或 post-hoc 修复提示反馈给候选模型。

验收要求：基线已知错误与新 patch 错误可分列，命令不可用时有预先声明的统一替代路径；改变环境后已有 negative control/评分冻结契约重新验证。当前 gate 中沿用旧 negative control 的理由是 environment 未变化，不能直接搬到改过环境的新 gate。Sphinx 小型回归 canary 可作执行链检查，2/2 成功不能证明新环境与旧环境统计等价。

### 9.2 Lead 互换与实际角色身份

`SweRun` 接受任意目录 Lead，不等于 Lead 互换实验已经完备。先把 Team prompt 中写死 Sol 的身份改为中性 Lead/真实模型身份，准备 arm 必须显式冻结 `(lead_model, worker_1_model, worker_1_role, worker_2_model, worker_2_role)`；实际每次调用据 CP actor/model 对账。GLM Lead→GPT Worker 与 GPT Lead→GLM Worker 是 Lead 方向比较，W1/W2 互换是角色分工比较，二者必须使用不同 arm 标签。

离线验收应覆盖同模型双 Worker、GPT 家族内部异构、GLM 家族内部异构、跨家族 W1/W2 互换和非 Sol Lead 的提示/调用身份；fake transport 捕获真实模型字段与提示，避免只是 manifest 表面通过。Lead/Worker 的 adapter、effort、服务层和工具协议按模型适用性显式列出，不能把不存在的等价参数悄悄映射。新增 Lead 先完成协议工程 canary，再进入正式效果对照。

### 9.3 预算与调度新增能力必须显式登记

现有 `RunBudget` 只有 token/call/wall 约束。若总计划采用等美元成本或运行中按收益决定是否组队，新增部分至少要有：冻结价格版本及 unknown 定价策略、请求前保守预算、实际 usage 结算、跨 run 总预算、重复请求去重，以及为“已发请求可能超出事后估价”设计的明确处理。API 等价估算不是实际账单，缓存价/服务层不确定项不能成为精确硬上限承诺。此项属于后续新能力，不应阻塞最小的计量/宿主修复，也不能提前写成已具备。

## 10. 已有测试能证明什么、下一轮如何收口

已保存 `B/validation/gate_revision11.json` 记录 **204 个测试、0 errors/failures/skipped、PASS**，并明确该 revision 没有新增 provider live smoke。本轮只读该报告，**没有重跑这 204 个测试，也没有宣称修复后的任何测试已通过**。当前单元测试覆盖了原先要求的若干分支，但已知缺口包括：Windows I/O 的阶段故障、真实落盘状态、scout 声明一致性、真实 closing 边界，以及被 Worker 捕获的宿主故障进入批次停止条件。

现有可复用测试锚点：

- `tests/test_runner.py:271/316`：两名 CP Worker 与独立基线；`:342/356`：请求失败/无请求的覆盖区别；`:426`：未知 usage；`:438/451`：cleanup 与 CP 提交后失败禁止评分；`:472/499`：closing/drain。
- `tests/test_runner_rev7.py:24/77/88`：scout 顺序、W1/W2 模型顺序、Solo 目录模型。
- `tests/test_runner_rev9.py:27/44/55/86/109`：CM 独立池、历史共享池、快照兼容、配置覆写和保留规则。
- `tests/test_runner_rev10.py:28/42`：cold-start 与 ON 装配路径。
- `tests/test_runner_rev11.py:47/70/112/146/192/220/251`：宵禁、模拟余票、原四态、cleanup 字段、旧批缺字段、提示。需保留兼容性，并补本文的真实状态测试。

建议分四个交付门槛，前一门槛未满足不自动扩大下一阶段；**以下全部是后续动作，本轮均未执行**。

| 门槛 | 产物与验收 | 不可越过的边界 |
|---|---|---|
| G0 契约冻结 | 确认 E1–E7 中实际实施的项目；冻结新观测 schema、错误分类、tool phase、预算边界和旧数据映射；每项对应 owner/测试 | 不先改默认值或开始 live；没有 F4 开关就不能预登记 F4 OFF 臂 |
| G1 离线实现验收 | 按 E1–E6 精确风险新增测试；受影响现有测试及要求的完整 gate 通过；生成新的 JUnit/源码 hash/兼容审计报告 | 使用假 transport，不调用模型；原 gate 不覆盖；不以“测试数量更多”代替关键用例证据 |
| G2 环境/工程 canary | 先做无模型环境与 Windows 文件契约检查；在另行获准的冻结小预算内跑最小模型/角色/ON 路径 canary；核对所有 usage、工件、阶段和实际模型 | 任何宿主/CP/计量完整性错误即停后续调度；canary 不混入正式效果实验重复组 |
| G3 正式对照准入 | 共同新基线、精确 task/model/role/flag/adapter/预算/环境指纹，有限计划次数及预登记停止条件；组间唯一处理差异可解释 | F3 未触 reserve 不算边界收益证据；完成率/采用率/官方正确率分开；重复和费用含失败、不静默补样 |

新 gate 用新的 revision 文件名，编号在总计划中统一分配；不得回写 `gate_revision11.json`。每个实验 arm 导出“实际生效配置”而非只导出用户输入；prepare/verify 应检查参数类型/范围、开关依赖、模型角色和资源约束的一致性。回退以重新选择已冻结运行版本和新 run_id 实现，保留所有失败工件；不能对共享工作树做无差别还原，也不能删除本轮证据来恢复“干净”状态。

## 11. 原始证据索引

- `B/validation/consolidated_v16_20260905/notes/forensics_audit.md:50`：宿主异常、两次候选协议错误和 scout 契约；`:80`：F4 与 cleanup；`:121`：xarray OFF rep2 整合轨迹；`:135`：Seaborn 环境背景；`:153`：F3 真实余票。
- 同目录 `forensics_evidence.json`：28 runs、20 workers、3 cleanup 工件的原文件路径、bytes、哈希和官方 F2P/P2P；这里只作为冻结证据读取。
- `B/pilot_v16/results/mwaskom__seaborn-3069__heterooff_gpt-5.6-terra__glm-5.3/worker-2/delivery.json`：WinError 5 原始错误，delta 缺文件不能按 0 处理。
- `B/pilot_v16/results/mwaskom__seaborn-3069__hetero_gpt-5.6-terra__glm-5.3/calls.jsonl:6`：Sol 单条终结响应畸形，call_id `b58ef7bf-2f0d-43fe-88ac-7606781183a8`。
- `B/pilot_v16/results/pydata__xarray-7229__hetero_gpt-5.6-terra__glm-5.3.rep2/calls.jsonl:55`：GLM timeout 越界，call_id `e04d4cdc-9712-478a-b7c5-fd51523e5c9f`。
- `B/pilot_v16/results/pydata__xarray-7229__heterooff_gpt-5.6-terra__glm-5.3.rep2/events.jsonl:129`、`:143`：正式采用两 Worker；`:151`、`:166`：整合后自测失败。官方 F2P 0/1、P2P 280/280，不能判作已正确交付。
- `B/validation/REV11_FIX_PLAN_20260904.md` 与 `B/validation/gate_revision11.json`：原计划及已存测试范围；错误的“物理丢弃”“所有五项都可关闭”仅在新文档中纠正，不修改冻结历史。
