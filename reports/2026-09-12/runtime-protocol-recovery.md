# DPswarm 运行协议与受阻恢复修复（2026-09-12）

源码、基于已安装 0.9.7 的兼容候选及安装路径回归已通过，8 个最小补丁文件已安装。**真实 DPH 与 Python 控制服务均已重启，当前进程能力及端口身份已核验；原会话已通过正常终止流程结清，工作槽位归零，目录租约释放。** HTML 与设置哈希一致，原始会话和审计记录保留。该终止不代表产物通过独立 Tester/Reviewer 验收。

## 根因

此前 2026-09-11 23:21:08（Sydney）的安装已在磁盘补齐 `verification-required/binding/superseded/takeover` 与 `team-required-admission` 审计事件，但端口 8795 上的独立 Python 进程仍使用启动时加载的旧 `plugin_audit.py`。旧 Python PID 26412 自 2026-09-10 11:14:02（Sydney）运行。用户在 2026-09-11 23:40:02 重启了 DPH（PID 55952），但该 Python 进程没有退出；新 JavaScript 因而连接到了旧 Python。

旧 `/api/status` 仅公布 `plugin_audit_v1: true`，只能证明服务支持审计账本，不能证明当前进程接受新增事件或具备所需准入清理行为。新 JavaScript 因此可能通过旧检查，直到 worker 已发布后的审计写入才遇到 `PLUGIN_AUDIT_INVALID_EVENT`。固定团队任务保留为 started，但缺少明确的基础设施阻断状态，后续收尾仍可能要求继续运行或恢复。

交叉审查还复现了两个恢复缺口：本地受阻记录虽已认证，但恢复到较早的有效账本时，已发布 worker 可能被丢失并重新允许派发；同一宿主的清理证明需要 `item_id`，而旧 `STARTED` 事件只保留原生会话身份，导致有效清理无法匹配。

## 修复后的合同

| 边界 | 现在的行为 |
| --- | --- |
| 运行进程能力 | Python 状态公布启动时加载的审计 schema、完整事件集合、准入清理协议及协议 revision。PID/start_id 仅作诊断，不替代能力判断。磁盘更新不会改变旧进程已公布的能力。 |
| 任务准入 | `Sidecar.requireRuntimeCapabilities()` 只执行状态 GET。固定团队要求精确协议 schema/revision、审计 schema、五个新增事件及 `delegate-admission-cleanup-v1`；不以任意包版本字符串代替合同。不满足时返回结构化 `SIDECAR_RUNTIME_INCOMPATIBLE`。 |
| 调用顺序 | 固定团队的必需能力检查早于工作区租约、预算、CM、模型目录准入和控制面业务写入；原生 pre-step 门禁可在 prepareCall/stream 前结束为 blocked。普通未要求新合同的独立连接保留兼容路径。 |
| 稳定阻断 | 指定的运行协议及持久审计错误形成基础设施 blocked，保留原任务绑定、已发布 worker 和未完成状态。重复状态查询或收尾不追加同一错误循环，不强制新模型调用、写文件或再次派发团队。状态、只读检查、提问及受约束的恢复入口仍可使用。 |
| 审计不可写 | 在主审计不能持久化阻断时，使用按根会话与可信用户消息绑定的 HMAC 认证本地记录。该记录只有拒绝执行的作用，不授权写入、完成、验收或释放锁。冷恢复仍核验任务身份。 |
| 账本连续性 | 恢复前校验认证记录中的 revision/hash 和全部已保存 STARTED 事件。旧 revision、同 revision 不同 hash、较新账本缺失已发布 worker 均保持 blocked；错误处理再次写失败也不能缩减保存的 worker 集合。不会把本地记录合并成伪造的主账本完成事件。 |
| 恢复与结算 | 兼容能力和可写审计恢复后，已有 worker 的任务只回到 started，仍须核对控制面终态和物理清理。旧 STARTED 缺少 `item_id` 时，以根会话、run、原生 worker 身份关联诊断，再核对控制面节点与任务。只有租约已释放、活动槽为零、全部节点及任务终止/验收且物理清理证据成立，才可收尾。 |

能力签名只包含受检查的稳定合同和所需事件是否存在；PID、start_id、时间戳与无关新增事件不会触发恢复。旧失败不会因单纯重启或一个无关写入成功而被改写为模型执行成功。

## 验证结果

| 验证范围 | 结果 | 证据与边界 |
| --- | --- | --- |
| 源码 JavaScript 全量 | **542/542 通过**，无失败、无跳过 | `source-verification/summary.json`；包含源码当前功能。 |
| 0.9.7 兼容候选 | **366/366 通过**，无失败、无跳过 | `candidate-verification/summary.json`；保留 0.9.7，未混入尚未安装的 0.11 交接功能。验证入口明确排除四个只属该功能的用例，源码全量覆盖它们。 |
| 已安装路径兼容回归 | **366/366 通过**，无失败、无跳过 | `installed-verification/summary.json`；测试读取安装路径，再在隔离状态中启动服务。不是对现有后台进程的载入证明。 |
| Python 源码全量 | **692 通过、1 跳过**，0 errors/0 failures | `pytest-full-source.xml` 共 693 项；唯一跳过为未设置 `DPSWARM_TDAI_ENDPOINT` 的真实网关 BM25 检查。 |
| 真实旧 Python 进程 | 通过 | 先启动冻结旧包，再更新其隔离目录中的磁盘文件；实际 FixedTeamController.run 仍拒绝旧进程。租约、预算、CM、模型目录、原生 worker 启动与 HTTP 写入计数均为 0；业务事件未变，无新增业务会话目录。 |
| 原生受阻与冷恢复 | 通过 | 使用实际安装的 DSH ReactLoopAgent/Inbox、真实隔离 Python HTTP 服务；测试已发布 worker 后的受阻收尾、无重复 steering、HMAC 认证、任务隔离及恢复到 started。 |
| 独立交叉审查回归 | 通过 | 主恢复及错误处理两条路径均拒绝账本回退；真实 markStarted 与 reviewSettlementEvidence 组合接受匹配的控制面关联，拒绝错误任务关联及缺失物理清理证明。 |
| 原会话复制演练 | 通过 | `recovery-rehearsal.json`：复制的原会话状态从 started 收尾至 finished，对应控制面任务终止，剩余槽位 0，租约释放；`original_state_modified: false`、外部模型调用 0。 |

复制演练针对 `session-0a9b0a41-0326-4a19-bcec-1a9703474918` / `wi-9363b2e76e`，经过实际 controller、dispatcher 和 CM 路径。原生输入存档收据记录 134 行、最后 seq 132 及压缩文件 SHA-256；报告不复制原任务正文。演练中的终止和租约释放仅作用于副本；下文另列后续对原会话执行恢复取得的独立收据。

## 安装范围与证据

本轮安装为 6 个 JavaScript 文件及 2 个 Python 文件：`fixed-team.js`、`index.js`、`infrastructure-blocked.js`、`sidecar.js`、`team-dispatch.js`、`team-required.js`、`server.py`、`session_server.py`。保留实际包版本 0.9.7；源码 0.11.0 与兼容候选分别验证，不整包覆盖未安装功能。逐文件安装前后 SHA-256、审查结果及回滚备份见收据；安装操作未修改设置或工作区租约。

所有下列本地收据位于 `.tmp/runtime-protocol-fix-20260911/`，目录日期沿用故障调查开始日期；本报告使用 Australia/Sydney 的 2026-09-12 日期。

- `reviewed-manifest.json`：冻结的 8 文件哈希及独立审查范围。
- `deployment.json`、`installed-backup/`：磁盘安装收据和原文件备份；安装时间为 2026-09-11T14:23:27Z。
- `source-verification/summary.json`、`candidate-verification/summary.json`、`installed-verification/summary.json`：三套 JavaScript 结果及对应完整日志路径。
- `pytest-full-source.xml`：Python 全量结果和跳过原因。
- `native-input-receipt.json`、`recovery-rehearsal.json`：输入存档身份与副本恢复结果。
- `baseline/`、`source-before/`、`candidate/`：用于区分旧安装、原始源码和最小兼容候选的快照。

可复现入口为项目根的 `node .tmp/runtime-protocol-fix-20260911/verify.mjs source`，兼容候选和安装路径分别使用 `candidate` / `installed`。Python 全量在 `dpswarm-plugin` 目录运行 `python -m pytest tests -q`；该 Windows 环境使用项目内全新的 `--basetemp`，避免默认临时目录的权限冲突。测试结果并不证明外部供应商连接、模型求解质量、费用收益或真实任务验收通过；本轮上述工程验证和复制演练的外部模型调用均为 0。

## 真实运行重启与原会话恢复：已完成

实际操作前核对了绑定的 108 个原生执行记录（82 个 worker）：无活动轮次、工具调用或待处理队列；同时核对进程路径、创建时间、命令行、端口和当前连接。只停止已确认空闲的旧 DPH PID 55952 与旧 Python PID 26412，没有停止其他端口的服务。保存了目标控制面、审计账本、产物、租约和原生记录备份。未手工删除租约或改写完成事件。

| 实际核验 | 结果 |
| --- | --- |
| Python 重启 | 2026-09-12 00:27:38 Sydney，新 PID **31324**，原端口 **8795**、原工作区，当前能力 schema `dpswarm-runtime-capabilities-v1`。响应的 PID/start_id 与实际进程和监听端口一致。 |
| 原任务恢复 | 00:27:51 Sydney，对 `wi-9363b2e76e` 执行正常 `review terminate`。控制面 `submitted → terminated`，任务要求 `started → finished`，结算 `failed_takeover`；没有改为 accepted。 |
| 清理证明 | 采用原始 worker 的真实原生终态、物理清理诊断和控制面身份关联；工作槽位 **0**，CM 团队已结束，原目录租约经正常协调路径释放。 |
| 剩余点数 | **1**，仅保留 root 的点数；worker 已不占用。最初 `live-recovery.json` 的辅助点数字段误用了不存在的字段而被省略；后续只读 `live-validation.json` 通过实际 `active_points` 独立核验，未重跑恢复。 |
| DPH 重启 | 00:28:21 Sydney，新 PID **52328**，原 Node/CLI 与 `web` 参数，端口 **3080** 由该 PID 监听。使用现有本机认证验证页面 HTTP **200**，未认证访问仍为 **401**。 |
| 启动目录 | 原宿主 cwd 无法由已获准的只读进程信息确定；本次显式使用该用户任务的原生 cwd `K:/秋招/项目/DPswarm/911`。未修改持久设置；该目录无 `.env`。 |
| 安装文件 | 重启后重新核验全部 **8** 个安装文件与已审查候选 SHA-256 一致，Python 返回的是新进程加载的实际能力。 |
| 原文件保全 | HTML **30,963 bytes**，SHA-256 与操作前一致；设置文件 SHA-256 一致，原审计和业务日志保留完整前缀，原产物保留。 |
| 原生会话 | 恢复完成、宿主启动前压缩文件哈希未变。DPH 重新打开后追加一条 `session/end-seed`（seq 133）元数据，现为 **135** 条；原 **134** 条逐条完全一致，无新增模型请求。不能将重启后的压缩文件描述为字节不变。 |
| 模型与验收边界 | 本轮测试、演练、恢复和健康核验的外部模型调用 **0**。未执行新的真实供应商团队任务；未为 HTML 添加独立测试者/审查者通过结论。 |

实际收据：`runtime-before.json`、`stopped-processes.json`、`sidecar-restart.json`、`live-recovery.json`、`dph-restart.json`、`live-validation.json`。`deployment.json` 的 `processRestarted: false` 记录的是较早的磁盘安装瞬间；上述后续进程与恢复收据证明已完成实际重启。实际操作日志和含本机认证信息的启动日志仅留在忽略的本地 `.tmp`，本报告不复制认证信息。

网页可刷新后继续使用。历史失败轮次仍保留原错误；本次结清解除其遗留占用，不伪装成当时已成功。完整源码、兼容安装及真实控制服务合同通过并不等同于一次新的外部供应商端到端验收。

## 后续：恢复用户手动启动方式（00:40 Sydney）

用户关闭原终端后再次运行 DPH，遇到 `EADDRINUSE 127.0.0.1:3080`。实时核验确认占用者仍为本次验证启动的隐藏独立 DPH PID 52328，它不随用户原终端关闭。确认无活动执行并按同一进程句柄复核身份后，已停止此实例；3080 监听数为 0，未另行启动宿主，留给用户从前台终端手动启动。Python 控制服务和已安装修复保持不变。此处更新的是较晚的运行状态，不撤销上文重启与恢复的历史证据。收据：`.tmp/runtime-protocol-fix-20260911/manual-launch-port-release.json`。
