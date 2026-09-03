# DPswarm 按需协作能力与控制面

> 规格来源：`cursorhandoff/docs/DPswarm-机制架构.md`（共识型文档，六大机制）。
> 实现原则：**控制面事件存储独立、harness 只作执行底座**（§9.1
> "模式照搬，设施自建"），LLM 传输层走 Provider 抽象（MockProvider 可测、
> OpenAI 兼容网关可接真模型如 GLM Coding Plan）。

产品默认入口是宿主中的单 agent。它在执行任务时自主判断是否调用 DPswarm 插件能力；需要协作时兼任 Lead，分工、验收并收回结果，随后继续原任务。DSH 接入位于 `dpswarm-dsh-plugin`，目前通过系统提示与工具说明提供调用指引。控制面服务就绪与多 agent 任务启动是两个不同事件。

## 机制文档 ↔ 模块映射

| 机制文档章节 | 模块 | 要点 |
|---|---|---|
| §2 职责切分 / §2.1 RootExecutionSpec | `types.py` | 边界合同；LLM 管语义、代码管算术；Spec 只存稳定约束、不原地覆写 |
| §2.1 | `control.publish_spec` | 新 revision 降容不强杀在途、只停新准入 |
| §3 机制一：事实注入 | `routing.py` + `types.ModelCatalog` | AA 分维 + 价目 + 当前容量注入 Lead 决策 prompt；V1 画像只攒不用 |
| §4 机制二：观测与闭环 | `observation.py` / `profile.py` / `control.record_*` | 全账（路由/拓扑/验收者身份/两层终止原因/token 缓存分账）；429≠QUOTA；Lead 验收制奖励信号；failure audit |
| §5 机制三：上下文分发 | `context/`（memory/assembler/manager） | 异构反转→检索裁剪；包落盘带 hash/manifest；required 预取、optional pull；CM 按需瞬时调用不占点 |
| §5.5 生命周期 | `control.begin_rollover` 等 | 物理窗口可死、逻辑 node task 不变；**留白**：封存-唤起/重开仅有 rollover 硬切，`StartType.RESUME` 仅内部使用 |
| §5.6 分层记忆 | `context/memory.py` | 五层分离；晋升（candidate→durable，显式 promotion_check）；supersede 不覆盖历史（走生命周期、链式 revision）；TTL 失效；team/node 限定可见性隔离 |
| §5.8 硬切窗口 | `control.begin_rollover/confirm_rollover/rollback_rollover` | 预装 capsule → CAS 唯一 successor → 两阶段启动；epoch+1、深度保持、同 lease；rollback 复原现场（epoch/session/lifecycle 回退 + node_unblocked） |
| §6 机制四：经济性边界 | `control.record_economics` + `observation.economics_summary` | Lead 消耗 vs 委派节省的盈亏线数据（节省侧 = worker token 合计代理估算） |
| §7 机制五：动态拓扑 | `orchestrator.py` + `control.create_work_item/split/degenerate_assistant` | 默认单干；派生/分裂(1主1副+peer通道)/裂变(仅S、≤3)；退化可逆、结论落盘收摘要；深度按验收单元计、分裂同层拓宽不建 item |
| §7 时间护栏 | `control.tick` / `begin_seal` | 首次+重试≤2（max_attempts=3）；单节点超时转 blocked；树级 deadline 默认开启→封存 |
| §8 机制六：分级与升级 | `levels.py` + `control.prepare_retry` | S/A/B/C/D + AA 分维选型；归因四分支；升级上限=Lead 级别；预算耗尽走上交（无裁决不进画像） |
| §9.1–9.2 控制面一致性 | `control.py` + `state.py` + `invariants.py` | 事件唯一真源、JSONL+fsync；append 前回放校验；方法级原子事务；per-root 单写者串行链；CAS graph_revision；id 永不复用 |
| §9.3 两阶段启动 | `control.begin_node/confirm_node/reconcile_provisioning` | provisioning 意图先落盘 → hash 确认 → active；崩溃对账；终态优先 |
| §9.4 通知不带状态 | `control.wakeup` | 只报"有变化"，被唤醒方重读投影 |
| §9.5 分裂 peer 通道 | `control.split/peer_send/peer_deliver/peer_close` | 仅分裂对开通道；绑 work item 不绑 session；关闭后拒新消息、在途有界结算 |
| §9.6 封存三段式 | `control.begin_seal/begin_settlement/finish_seal` | 准入截止→有界结算→超时兜底（`settlement_timeout_seconds` 到期由 tick 自动 finish_seal）；在途 item 推终态、关通道、消息丢弃显式记录；子 team 封存只作用子树 |
| §3/§8 AA 接入 | `aa.py` + `levels.py` | AA 快照解析与分维评分；`DPSWARM_ROOT_MODEL` 指定 root Lead 级别 |
| Provider 传输层 | `providers/`（mock/openai_compat） | MockProvider 脚本化确定性回放；OpenAI 兼容网关接真模型 |

## AgentTeam 工具协议与协作运行时（2026-09-03）

`dpswarm.team_runtime` 新增严格工具协议、公开规格交接契约、有界澄清状态机和持久执行账本。OpenAI 兼容 Provider 返回原始 `assistant_message`、`response_kind`、`continuation` 与 nullable `usage_observation`；合法 `tool_calls` 表示本次调用完成并要求工具续轮，不再一律标成 aborted。旧 `Usage` 的整数默认值为兼容保留，缺失计量应读取 `usage_observation`，不能把默认零当实测。

本轮修复的实验集成接入点是仓库内 `modelbench/team_runtime_v2`：GLM 原生 tools、GPT 严格文本 adapter、显式 `finish_phase`、校验后交接、E 提问后新建 P clarification 工作项、原 E 恢复与全局预算复用。它复用真实 ControlPlane 的准入、fence、提交、证据验收。固定 P/E/V 和下述命令是实验控制条件，不是产品默认入口；修复尚未全部贯通宿主 `dpswarm_*` 委派链路。后续接入应保持“单 agent 默认、按需启用”，不要求把普通任务改成固定团队循环。

上一批实验入口是 `modelbench.team_runtime_v2.revisions.budget_visibility`；它补回逐轮预算提示，并规范化工作区绝对/相对交付物路径，参数见 `modelbench/team_runtime_v2/revisions/PLAN.md`。该批源码与成绩已冻结。本次新增 `ReviewFixedTeamRun` 修复澄清过期与无法配对的原生工具历史；尚未启用新的模型回归。当前 `server.py` 已修改，旧 CLI 的历史核心 hash 检查会拒绝运行，不能通过修改旧 manifest 绕过。新批次须独立冻结当前版本，并保留原始证据。复审缺口及重跑条件见 [复审记录](../modelbench/postrepair_review_20260903/REVIEW.md)。

## 控制面板（挂在 dph Web UI 上）

```bash
# 1) 启动 DPSwarm 控制服务（标准库 http.server，零依赖）
cd dpswarm-plugin
python -m dpswarm.server            # http://127.0.0.1:8790/

# 2) dph 侧入口（已挂载，免编译）：
#    apps/web/public/dpswarm.html  →  dsh web 启动后访问
#    http://127.0.0.1:3080/dpswarm.html（iframe 内嵌面板，离线时显示启动指引）
```

页面能力（对齐机制文档条文）：
- **Spec 参数控制**（§2.1）：槽位/点数/子Team比例/深度/裂变规模/attempt/deadline/
  节点超时——发布新 revision（降容不强杀在途、只停新准入），字段白名单校验；
- **人工指令三类**（§9.2）：immediate（wakeup 通知不带状态 §9.4）/ config（转
  Spec revision）/ terminal（停止任务树，入链可审计）；
- **封存三段式**手动触发（§9.6）与 tick 时间护栏巡检（§7）；
- **运行状态**：投影快照（槽/点数/seal 相位/graph revision）、work items/nodes/
  teams 表格、事件流（唯一真源 §9.1）、观测汇总（§4 全账 + §6 经济性）；
- **演示任务**：MockProvider（single/裂变）或 OpenAI 兼容真模型一键下发。

API：`GET /api/status|/api/events?limit=N|/api/observation`；
`POST /api/spec|/api/directive|/api/task|/api/seal|/api/tick|/api/delegate|/api/submit|/api/review|/api/peer`
（CORS 仅回显 loopback Origin，写操作需 bearer token——token 存 `.dpswarm-token`，
面板 HTML 仅在同源或无 Origin 时注入）。服务器测试见 `tests/test_server.py` +
`tests/test_server_fixes.py`。

## 快速开始

```bash
cd dpswarm-plugin
pip install -e .[dev]        # 或直接 PYTHONPATH=. 使用

# 1) MockProvider 演示（零外部依赖，确定性回放）
python -m dpswarm.cli init --dir .demo
python -m dpswarm.cli run --task "整理日志并汇总统计" \
    --mock examples/mock_single.json --dir .demo
python -m dpswarm.cli status --dir .demo
python -m dpswarm.cli replay --dir .demo   # 离线全量重验（§9.1 对账）

# 2) 接真模型（OpenAI 兼容网关，如 GLM Coding Plan）
export DPSWARM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
export DPSWARM_API_KEY=sk-...
python -m dpswarm.cli run --task "..." --model bigmodel/glm-4.7 --dir .demo

# 3) 测试（211 个，13 个测试文件：逻辑/案例/控制面/e2e/server/安全/
#    编排修复/机制锚点/记忆生命周期等）
#    逻辑套件：验收/生命周期转换矩阵穷举（合法边可达、非法边全拒）、
#              §7 护栏正负例矩阵、CAS/恢复/终态优先、时间护栏
#    案例套件：完整机制场景（DAG 解锁/分裂全链/升级链/硬切续接/
#              deadline 结算/退化摘要/人工指令/观测全账/拓扑指纹/事务原子）
python -m pytest -q
```

## 架构一句话

**事件日志是唯一真源**（`events.jsonl`，append 前 invariant 回放校验，flush 后才返回）。
可靠性四件套（2026-09-02 审查修复，`tests/test_mechanisms2.py` 回归锚点）：
事务以**单行 envelope** 落盘（一次 write+fsync，崩溃只可能留下被 fail-closed 丢弃的
残缺尾行）；回放校验 seq 严格连续（脏账本拒绝启动）；`<events.jsonl>.lock` 跨进程
文件锁物理兜底单写者（第二个写者进程即刻失败）；submit 的交付正文**内容寻址落盘**
`artifacts/<sha256>.txt` 并绑定 package——review accept 只准引用 submit 落盘的包
（证据不可替换、重启可恢复）；submit 强制携带 delegate 回传的
`(context_epoch, session_id)` fence（rollover 后旧 session 提交即拒）；
root 终局闭环（Lead accept → seal 链推 root accepted + 全树 drain + lease 归还，
seal 超时 → root terminated）；root Lead 级别可经 `DPSWARM_ROOT_MODEL`（AA 快照
解析）或 `ControlPlane(root_level=…)` 指定，不再永远硬编码 S。
（`events.jsonl`，append 前 invariant 回放校验，flush 后才返回）；
一切状态由回放投影（`state.py`）；一切变更走 `control.py` 的单写者串行事务链
（方法级原子：一组事件要么全落盘要么零落盘）；Lead（LLM）只做语义决策，
硬准入（级别方向/深度/槽位/点数/权限）全在代码层。

## 机制文档歧义处的实现选择（对照实验 01 悬空清单）

| 悬空问题 | 本实现选择 |
|---|---|
| §7"首次+重试≤2" vs §8"最多 3 attempt" | 按 §8 权威口径：`max_attempts=3`（四份逻辑原型一致收敛） |
| deadline"在途结案" | 结算窗口内允许 finalizing 完成 accepted（R-J04 读法；settlement 期链路合法） |
| accepted 清理窗口 | accepted 与节点 drain/lease 释放**同事务**发布，无清理窗口例外（invariant 硬性：accepted 后同 item 无 active 节点） |
| WI 占槽起点 | **创建即占**（含依赖未满足的后继，兼作防 DAG 无限展开护栏）——注：实验记录"三家均选创建即占"失实，Luna 原型是启动才占；本实现按 Grok/Composer 收敛口径 |
| 子 Team 50% 计入裂变者自身 | 计入（子树占用按节点所属 team 聚合，裂变者转入子 Team 后计入其子树） |
| ModelPointPolicy 权重公式 | §10 留白；占位 = `max(1, context_window // 64000)`，Hook 生命周期已实现（reweight 原子、差额先取） |

## V1 留白与实现层既定映射（§10 留白不实现；以下为核对后明确的实现层决策）

- DelegationRecord 字段级 SQL、任务分桶规则（现为关键词粗分）、GLM YAML、
  独立 reviewer 语义、layer cap、人工 override matrix、root 最终验收权——
  §10 留白，未实现（`root_acceptance_mode="lead"` 是对留白项的显式占位）。
- **reweight 差额不足 = 同步拒绝 + reweight-wait 等待标记**：结构化拒绝（调用方
  稍后再试），节点经 `node_reweight_wait` 事件标记等待——tick 超时抑制（§9.3
  抑制窗口），等待不耗重试预算；成功 reweight 同事务原子清除标记。
  不存在先启动再补账。
- **软/硬窗口阈值（§5.8 70%/85%）无观测对象**：V1 Provider 单轮调用、无长窗口
  会话，窗口占用监控留待接 harness 真实 session 时实现；rollover 全链（CAS/
  三路复位/深度保持/lease 保持）已完备，`begin_rollover` 为纯 API。
- **bench 衰减曲线（§3 V1 注入来源之一）未注入**：L4 实验层产物未产出前无数据可注。
- 权限校验（§2 硬准入之"权限"）与 quiet/wakeup 双投递模式（§9.5）：字段/词汇
  留位，语义待讨论。
- 终态型人工指令（"停止任务"）当前只记事件，执行器待人工 override matrix 定案。
- `_record` 纯记录类事件直通 append（不经 check_event）：记录类事件无不变量约束
  ——§9.1 字面纪律的已知例外（`_bootstrap` 已合规化：`_validate_spec` + 逐事件
  check_event + 单事务落盘；半截引导日志显式报 BOOTSTRAP_INCOMPLETE）。

## 核对修复记录（2026-09-02，对照机制文档全量核对后）

必修项（已修）：分裂 1主1副基数校验（SPLIT_PAIR_ONLY）；peer 通道随 item 任一
终态同事务关闭（CHANNEL_NOT_CLOSED）；§4 结案第 2 步真实化（`store_evidence_
package` + `PACKAGE_NOT_STORED` 投影校验，evidence_ready 不再自证）；§8 归因
四分支处置接线（capability→升级模型（上限=Lead 级别、A→S 深挖门）/context→
重建包/description→修述/contradiction→不硬磕上交）；换模型重试路由持久化
（RESUME 复投 + route_resolved 对账）；orchestrator split 分支（协助者经 peer
通道回报、主统一提交）；§4 429≠QUOTA 承载（背压退避 / 终止进运维审计不进画像）；
§5.4 pull 通道接线（PULL 协议多轮）；§5.7 记忆闭环（accepted→candidate→promote
+ 事件）；§5.3 大包引用投递；§7 退化动作进决策协议；escalated/terminated 不进
画像（§8 明文）；stopReason 聚合回退消费 stop_reason_recorded 事件。

## 二轮机制审查修复记录（2026-09-02，多 agent 复审 + 逐条验证后）

- **状态机/终局**：tick 豁免已 drain 墓碑节点（修 NODE_TERMINATED 穿透）；
  `(None, ESCALATED)` 入合法转换（超时 blocked 可上交，修 Lead 决策分支过滤方向）；
  terminate 系 reason 全面对齐六值词汇（落盘前校验，非法拒写）；
  `subteam_point_ratio` 收紧 `<1`；裂变建队与 lead 登记原子化（删吞错）。
- **rollover 链**：rollback 复原现场（epoch/session/lifecycle 回退 +
  `node_unblocked` 首个发射点）；`_pre_node_activated` 拒 blocked；CAS 登记强制
  capsule 引用/hash;tick 激活时间走投影缓存。
- **封存三段式**:finish_seal 对子树在途 item 推终态 + 关通道（修
  CHANNEL_NOT_CLOSED 卡死与槽位悬挂）；子 team 封存过滤子树不再拔整树；
  `settlement_timeout_seconds`（默认 600s）到期 tick 自动 finish_seal;
  通道关闭 payload 记 dropped 消息；封存完成后通道拒新消息（SEALED_ADMISSION）。
- **bootstrap/对账**:bootstrap 过 check_event + 单事务落盘 + 半截态显式报错；
  `reconcile_provisioning` 携带观测 session/manifest_hash，不再合成 id。
- **执行层接线**：分裂协助者产出拼回主执行者包 + confirm_node（修协作空转）;
  split 不再新建 DERIVE item（对齐 §7 同层拓宽，前后 work_items 数量不变）;
  每次 reject 喂画像负样本（failures 表不再恒空）+ DelegationRecord.attribution
  透传；验收 Lead token 入 `token_usage_recorded`（含 cache 分账）;429 背压耗尽
  有收口（对称 QUOTA 路径）；缺省归因回退 DESCRIPTION 并记审计；Lead 的
  degenerate/escalate 决策遍历投影非终态项（修 no-op);economics 节省侧接线
  （worker token 合计代理）。
- **memory 子系统**:retrieve 限定可见性精确匹配（修跨 node/team 互穿）;
  supersede 走 candidate→promote 生命周期 + 链式 revision；显式 promotion_check;
  `memory_rejected` 事件词汇；删 MemoryService 私有双账本（sink 直通控制面）。
- **server**：畸形输入 400/500 结构化（不再连接重置 + traceback 泄漏）;
  submit 先过 fence 再记观测（伪造 fence 不再污染唯一真源）;/api/seal 结局由
  控制面推导；面板 token 仅同源注入。
- reweight-wait 抑制窗口见上节；§5.3 包契约消费端（manifest 校验/预取强制）与
  §9.3 对账四维接线仍留白，待真实 harness 接入。
