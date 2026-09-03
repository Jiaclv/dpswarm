# DPswarm

> 给多模型 Agent 团队的每一次委派决策提供**数据依据**，并从执行结果中**闭环**。
> 基于 DeepSeek Harness（DSH）的按需协作控制面与插件。

DPswarm 不接管调度决策——"选哪个模型、要不要拉起团队"仍由 LLM 自己判断。它补上的是 LLM 手上一直缺失的东西：**事实**（价目、能力画像、当前容量、历史成功率、上下文衰减阈值），以及让事实随使用越来越准的闭环（全量观测 → 画像更新 → 事实注入）。

## 为什么做

对 DeepSeek Harness 源码逐包核查后的三个结论（出处见[技术调研与方案设计](技术调研与方案设计.md)）：

1. **多模型路由已是 harness 出厂能力**——多 provider 适配、逐子 agent 指定 LLM、模型自选白名单均已实现，只是默认关闭；
2. **真正的空白不在机制，在判断依据**——无论"选哪个模型"还是"用哪种委派形态"，决定权都交给了 LLM，而 LLM 没有任何成本、能力、历史表现的数据，只能凭训练先验猜；
3. `subagent/start` / `subagent/end` 事件对与 `TokenUsage` 缓存分账是现成观测埋点，可低成本采集每一次委派的真实成本与结果。

由此确定的三条产品原则：

- **单 agent 默认，协作按需调用**——主 agent 先单干，判断需要协同时自主调用 DPswarm 能力并兼任 Lead；
- **LLM 管语义，代码管算术**——委派什么、给什么 context 是语义判断；token 算术、路由硬准入、容量与状态机一致性是确定性计算；
- **先观测，后决策**——第一块代码只挂事件采集，不碰路由逻辑。

## 六大机制

| # | 机制 | 一句话 |
|---|---|---|
| 一 | [事实注入](DPswarm-机制架构.md#3-机制一事实注入事前给事实) | 价目、AA 分维评分、当前点数容量、衰减曲线等事实注入 Lead 的决策上下文，让 LLM 在有约束的空间里做语义选择 |
| 二 | [观测与闭环](DPswarm-机制架构.md#4-机制二观测与闭环) | 每次委派记全账：路由、拓扑、验收者身份、两层终止原因、token 缓存分账；Lead 验收制做奖励信号，贝叶斯更新画像 |
| 三 | [上下文分发](DPswarm-机制架构.md#5-机制三上下文分发) | 异构团队没有跨模型 KV cache——用"检索 + 裁剪"替代"统一大前缀"；语义包落盘带 hash/manifest，required 预取、optional pull |
| 四 | 经济性边界 | 点数容量准入（worker 槽 + 节点点数）、Lead 消耗 vs 委派节省的盈亏线计量 |
| 五 | 动态拓扑 | 默认单干；派生 / 分裂 / 裂变按需生长，退化可逆；树级 deadline 与封存三段式兜底 |
| 六 | 分级与升级 | S/A/B/C/D 模型分级 + AA 分维选型；失败归因四分支；升级上限 = Lead 级别 |

## 控制面核心保证

- **事件日志是唯一真源**：JSONL append 前回放校验 invariant、fsync 后才返回；seq 严格连续，脏账本拒绝启动；跨进程文件锁物理兜底单写者；
- **`RootExecutionSpec` 边界合同**：整棵任务树只引用同一份 `root_spec_id + revision`，裂变不获得新边界；revision 化发布，降容不强杀在途、只停新准入；
- **两阶段启动 + 崩溃对账**：provisioning 意图先落盘、hash 确认后 active，崩溃后 reconcile 终态优先；
- **证据不可替换**：submit 交付正文按 sha256 内容寻址落盘，review accept 只准引用已落盘的包；`(context_epoch, session_id)` fence 拒绝 rollover 后的旧会话提交；
- **通知不带状态**：wakeup 只报"有变化"，被唤醒方重读投影，避免消息与状态漂移。

## 快速开始

```bash
cd dpswarm-plugin
pip install -e .[dev]        # 或直接 PYTHONPATH=. 使用

# 1) MockProvider 演示（零外部依赖，确定性回放）
python -m dpswarm.cli init --dir .demo
python -m dpswarm.cli run --task "整理日志并汇总统计" \
    --mock examples/mock_single.json --dir .demo
python -m dpswarm.cli status --dir .demo
python -m dpswarm.cli replay --dir .demo   # 离线全量重验（事件溯源对账）

# 2) 接真模型（OpenAI 兼容网关）
export DPSWARM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
export DPSWARM_API_KEY=sk-...
python -m dpswarm.cli run --task "..." --model bigmodel/glm-4.7 --dir .demo

# 3) 控制面板（标准库 http.server，零依赖）
python -m dpswarm.server     # http://127.0.0.1:8790/
#    Spec 参数控制 / 人工指令 / 封存三段式手动触发 / 事件流 / 观测汇总 / 演示任务

# 4) 测试
python -m pytest -q
```

DSH 侧接入见 [`dpswarm-dsh-plugin/`](dpswarm-dsh-plugin/README.md)（委派工具 + 控制面 sidecar 客户端）；机制文档 ↔ 模块映射表见 [`dpswarm-plugin/README.md`](dpswarm-plugin/README.md)。

## 实验验证

DPswarm 不宣称"多 agent 总是更好"。全部实验预注册封板、双臂对照，量化的是**何时**团队有增益（TNI，团队必要性指数）：

| 实验 | 任务 | 结果 |
|---|---|---|
| 实验 02 · TeamBench 分工 | 数据工程管道 | TNI < 1，单干更优——正是机制设计预期；机制全链路（拆分→契约冻结→并行准入→验收门禁→依赖解锁→汇合验收）零故障运转 |
| 实验 03 · WideSearch 财富榜 | 真实 benchmark，60 行 × 6 列 | 验收门把团队质量 **F1 0.947 → 1.0**；三模型画像分化（单位/口径自觉性成为区分维度） |
| 实验 04 · 全 Grok 对照 | WideSearch 8748 格 | 团队时间 1.7×、token 9.7×——根因是任务选型而非机制失效，据此沉淀**选型门**（难度以基线预跑标定） |
| 实验 05 · 双题连跑 | 05A / 05B 两道真题 | **+6.6pp / −1.8pp 一正一反**：团队增益依赖任务类型；05B 零多交 + 130/130 全覆盖，Lead 口径指令精准拦截基线踩坑 |
| modelbench | SWE fixed-team 基准 | GLM / GPT / Codex 等多 provider 固定团队跑 SWE-bench 类任务，复用真实 ControlPlane 的准入、fence、提交与证据验收 |

原始台账与逐轮事件流见[实验记录](实验记录-逻辑原型验证.md)，modelbench 结果见 [`modelbench/`](modelbench/)。

## 仓库结构

```
dpswarm-plugin/        Python 控制面：事件溯源、准入、编排、观测、上下文分发、控制面板
dpswarm-dsh-plugin/    DSH 侧插件：委派工具 + 控制面 sidecar 客户端（dsh client 插件形态）
prototype*/            四个逻辑原型：控制面机制的早期验证（prototype / -composer / -grok / -luna）
modelbench/            模型与团队基准：SWE fixed-team、多 provider 矩阵、审计与复审记录
技术调研与方案设计.md    源码核查结论与差异化判断（为什么做、不做什么）
DPswarm-机制架构.md     六大机制的共识设计文档（系统怎么运转）
DPswarm-架构设计.md     分层架构：L0 观测 → L1 画像 → L2 注入 → L3 委派 → L4 实验 → L5 路由
实验记录-逻辑原型验证.md  预注册实验的完整台账（设计 / DelegationRecord / 双臂对比 / 结论）
```

## 测试

`dpswarm-plugin` 当前 **363 个测试全部通过**（控制面事务与恢复、生命周期转换矩阵穷举、§7 护栏正负例矩阵、CAS / 终态优先 / 时间护栏、机制场景 e2e、server 与安全）：

```bash
cd dpswarm-plugin && python -m pytest -q
```
