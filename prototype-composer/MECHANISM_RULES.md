# DPswarm 可检验机制规则清单（自机制文档提取）

> 供 prototype-composer 逻辑原型与场景测试对照；编号 R-xxx。

## A. RootExecutionSpec 与边界（§2.1）— 8 条

| ID | 规则 |
|---|---|
| R-A01 | Spec 只承载 root 级稳定约束，实时状态不得写回 Spec |
| R-A02 | 子 Team 不得复制可独立修改的 Spec，只引用 root_spec_id + revision |
| R-A03 | Spec 不原地覆写，边界调整发布新 revision 并审计 |
| R-A04 | 新 revision 容量低于当前占用时：不强杀在途节点，停止新准入 |
| R-A05 | maxOpenWorkItems 固化于 Spec，只统计普通 worker |
| R-A06 | maxActiveNodePoints 固化于 Spec，为加权占用上限非消费预算 |
| R-A07 | 子 Team 本地点数上限默认为父有效上限的 50% |
| R-A08 | 树级 deadline 默认开启，触发 §9.6 封存三段式 |

## B. 路由与硬准入（§2, §7）— 10 条

| ID | 规则 |
|---|---|
| R-B01 | 人工指定 provider/model/effort 优先于 Lead |
| R-B02 | 代码不得静默替换、降级或改选 Lead/人工提议路由 |
| R-B03 | 硬准入：模型存在且可用、级别方向、深度、槽位、点数、权限 |
| R-B04 | 级别方向：只能召唤同级或低级（人工 override 可越级） |
| R-B05 | 裂变权限：仅 S 级 Lead 可裂变 |
| R-B06 | 语义深度默认两层；派生/裂变 +1 层，分裂不加层 |
| R-B07 | 裂变规模默认 ≤3 个同时存活直属子 agent |
| R-B08 | context manager 不占 worker 槽与节点点数 |
| R-B09 | Lead/root Lead 作为控制节点占点，最先准入 |
| R-B10 | 路由来源、提议路由、解析路由、policy 版本须持久化可对账 |

## C. 资源池：槽位与点数（§7）— 9 条

| ID | 规则 |
|---|---|
| R-C01 | worker 槽绑定 work item，从启动到 accepted/明确终止持续占用 |
| R-C02 | Lead/context manager/reviewer(待定语义) 不占普通 worker 槽 |
| R-C03 | 每个 LLM 常驻节点启动前取得 node points lease |
| R-C04 | 节点内部多次 LLM 调用不重复占点 |
| R-C05 | accepted 或明确终止才全额归还 lease |
| R-C06 | submitted/finalizing/rejected/等待验收/打回准备重试均不释放 |
| R-C07 | 换模型重试：原子 reweight；增重须先取得差额，否则等待 |
| R-C08 | 同模型重试 lease 不变 |
| R-C09 | rollover 不新建 lease，不释放后重取 worker 槽 |

## D. 验收状态机（§4, §5.7）— 11 条

| ID | 规则 |
|---|---|
| R-D01 | submitted → finalizing → accepted 为合法主路径 |
| R-D02 | submitted → rejected 合法；rejected 可回到 submitted（重试） |
| R-D03 | finalizing 期间 lease 与槽位继续占用 |
| R-D04 | finalizing 期间不解锁后继、不释放资源 |
| R-D05 | accepted 发布须 evidence/dependency package 已就绪（原子） |
| R-D06 | accepted 后释放 lease 与 worker 槽 |
| R-D07 | submitted 不可晋升 durable memory |
| R-D08 | rejected 内容进 evidence/failure audit，不进默认检索 |
| R-D09 | finalizing → aborted-finalize 合法；释放 lease，不解锁后继 |
| R-D10 | 协助者不独立提交验收 |
| R-D11 | 退化/收回子节点记 terminated，不算验收、不进画像 |

## E. 重试与上交（§8）— 6 条

| ID | 规则 |
|---|---|
| R-E01 | 同一 work item 最多 3 次 attempt（首次 + 2 次重试） |
| R-E02 | 换模型/修 context/修描述共用重试预算 |
| R-E03 | rollover 不消耗重试预算 |
| R-E04 | provisioning failed 不消耗重试预算 |
| R-E05 | 预算耗尽走上交，原 item 转 escalated |
| R-E06 | escalated 无裁决、不进画像，释放槽位与点数 |

## F. 硬切 / rollover（§5.8）— 8 条

| ID | 规则 |
|---|---|
| R-F01 | 预装 capsule 失败：节点进 blocked/recovery，无验收态变更 |
| R-F02 | CAS 登记 successor：(node_id, context_epoch) 唯一 |
| R-F03 | successor 激活成功 / 预装失败回退 / 终态作废 须复位登记 |
| R-F04 | rollover 复用 §9.3 两阶段启动，保持 node/work_item/lease |
| R-F05 | context_epoch +1；delegationDepth 不变（rollover 不加层） |
| R-F06 | 禁止 successor 从 predecessor 日志拉增量（delta 通道） |
| R-F07 | 换模型 rollover 须 lease 原子 reweight |
| R-F08 | predecessor 只读、不回灌 |

## G. 控制面一致性（§9.1–§9.2）— 10 条

| ID | 规则 |
|---|---|
| R-G01 | 事件为唯一真源，状态由回放投影 |
| R-G02 | append 前 invariant 校验，非法拒绝落盘 |
| R-G03 | flush 完成后才报告成功 |
| R-G04 | per-root 单写者串行事务链 |
| R-G05 | CAS expected_graph_revision，过期拒绝 |
| R-G06 | 每次图变更后全量重验（环/依赖） |
| R-G07 | work_item_id / node_id 终止后同 root 内不复用 |
| R-G08 | finalizing 与 accepted 为链上独立事件 |
| R-G09 | 方法级原子：迁移与 append 同单元，失败整体回滚 |
| R-G10 | DAG 必须无环 |

## H. 启动与恢复（§9.3）— 7 条

| ID | 规则 |
|---|---|
| R-H01 | 统一两阶段：provisioning 意图 flush → hash 确认 → active |
| R-H02 | hash 不匹配 → failed |
| R-H03 | 对账：parent/descriptor/provider/hash 一致才 active |
| R-H04 | rollover 保持 predecessor delegationDepth |
| R-H05 | 终态优先：deadline/terminated 作废未完成 provisioning |
| R-H06 | 超时时钟从 active 起算；provisioning/reweight-wait 抑制超时 |
| R-H07 | 崩溃恢复 provisioning 幂等补走 |

## I. 通知与 peer 通道（§9.4–§9.5）— 5 条

| ID | 规则 |
|---|---|
| R-I01 | 唤醒/通知不携带状态内容 |
| R-I02 | 仅分裂对建立 peer 通道 |
| R-I03 | peer 通道绑定 work item，不绑定物理 session |
| R-I04 | work item 任一终态或退化时关闭 peer 通道 |
| R-I05 | 协助者超时只 wakeup 主执行者，不上交 |

## J. 封存三段式（§9.6）— 5 条

| ID | 规则 |
|---|---|
| R-J01 | 顺序：准入截止 → 有界结算 → 超时兜底 |
| R-J02 | 准入截止同时封死 start_node |
| R-J03 | 批量终态：先 drain 节点/作废 CAS，后改 item 终态 |
| R-J04 | 结算期 in-flight finalizing+evidence 可完成 accepted |
| R-J05 | 超时兜底不得悬挂 |

## K. 时间护栏（§7）— 3 条

| ID | 规则 |
|---|---|
| R-K01 | 单节点 wall-clock 超时转 blocked |
| R-K02 | 超时后 Lead 归因重试（计预算）或上交 |
| R-K03 | 树级 deadline 触发封存，禁止再派生 |

**合计：82 条可检验机制规则**（V1 留白如 ModelPointPolicy 公式、reviewer 语义、layer cap 等未纳入 executable 断言）
