# DPswarm 架构设计

> 日期：2026-09-01
> 前置文档：`技术调研与方案设计.md`（调研结论与实验假设）
> 本文档：落地架构——观测、画像、注入、委派、实验五层的设计与实现路径
> 所有机制决策都锚定在已核源码事实上（§0），事实先于设计。

---

## 0. 设计基线：已核源码事实

以下每条都经过源码核对，路径相对于 `deepseek-harness-master/deepseek-harness-master/`。架构的每个决策都能回溯到这里的某一行。

| # | 事实 | 出处 | 架构影响 |
|---|---|---|---|
| F1 | 多 provider 内置；OpenAI 兼容网关纯配置接入（`api: openai-completions` + `baseURL` + `compat`） | `packages/llm/llm-pi-ai/README.zh.md:57-70` | GLM 接入 = 一个 YAML 块，零代码 |
| F2 | 逐子 agent LLM 路由已内置；spawn/fork 两进程内提供方声明 `agentOptions` 能力 | `docs/subsystems/subagent.zh.md:68-74` | 路由机制不用造，只差决策依据 |
| F3 | 白名单断言只在**显式选择**时触发；纯继承不检查 | `packages/subagent/tool-subagent/src/model-selection.ts:131-133,139-153` | 配置层钉路由可以绕开模型选择通道 |
| F4 | 随附 fork 工具禁选模型是**工具层策略**；continuable 描述符会快照 `agentOptions.provider/model/reasoningEffort` 用于冷恢复 | `README.zh.md:215`；`subagent.zh.md:291` | "fork+异模型"臂可用自定义工具实例造出，不用 fork 主仓 |
| F5 | `preflightChildLlmRoute` 在建子前用活适配器 `resolveCallConfig` 预检路由 | `model-selection.ts:176-196` | 假路由建子前即炸 → 能力画像**只能走展示层** |
| F6 | `list_subagent_models` 按委派工具实例注册，输出 schema 就是 `type: string` 纯文本 | `packages/subagent/tool-subagent/src/list-models.ts:86-113` | 富化注入点：注册自己的版本，文本行追加画像数据 |
| F7 | 提供方可持静态 `agentRouteDefaults`，供一次性子 agent 继承 | `subagent.zh.md:450-455` | "代码做路由"的官方通道（配合 F3 不触发断言） |
| F8 | 生命周期事件 `subagent/start`/`subagent/end` 是 **observe-only**；载荷 = runId/provider/id/local(+stopReason/lastAssistantMessage)，**不含 TokenUsage** | `packages/subagent/subagent/src/types.ts:36-73`；`subagent/README.zh.md:171` | 用量要从子会话事件流聚合；观测者不能影响运行 |
| F9 | `usage` 分片在 `finish` 前发出；`TokenUsage` 缓存字段不相交；`BlockAssembler` 把 usage 连同 provider/model 存入会话日志 | `docs/subsystems/llm-streaming.zh.md:210,283,319-355` | 离线解析能拿到逐次调用的缓存拆分 |
| F10 | seed 契约二元：fresh 或 fork（从 seq 0 连续、无损、轮次平衡） | `subagent.zh.md:281-288` | "手动 fork" = 工具实例 + seed + agentOptions，没有中间态要处理 |
| F11 | 会话 event-sourced + JSONL 持久化；header 带 `origin: 'subagent'`、`seedLength`（fork 谱系边界）、`delegationDepth` | `subagent.zh.md:291-297` | v0 观测可以纯离线，子 agent 身份/谱系/模型全部可从日志恢复 |
| F12 | 路由变更且未显式给 effort 时，配置层 effort 被清除 | `model-selection.ts:118-122` | 异模型臂必须显式重设 `reasoning_effort`，否则静默降级 |

---

## 1. 系统总览

```
                        ┌─────────────────────────────────────┐
                        │           harness (不动)             │
                        │  Lead Agent ── 委派工具 ── 子 Agent  │
                        └──────┬──────────────────▲────────────┘
                               │ 事件流/日志       │ 事实注入
        ┌──────────────────────▼──────────────────┴───────────┐
        │                   DPswarm（插件 + 离线分析）          │
        │                                                     │
        │  L0 观测 ──→ L1 画像 ──→ L2 注入                      │
        │  (采集)      (聚合)       (回喂决策视野)               │
        │     │                                  ▲            │
        │     └──────────→ L4 实验 ──────────────┘            │
        │               (受控测量,产出帕累托数据)               │
        │                                                     │
        │  L3 委派工具实例（fork+异模型臂的载体）               │
        │  L5 路由（后期：离线 replay → 配置层静态路由）        │
        └─────────────────────────────────────────────────────┘
```

**三条架构原则**（每条都有框架层面的硬性依据，不是口味选择）：

1. **语义归 LLM，算术归代码。** 选材料/写委派描述是语义，用 Lead 已有的生成能力（内嵌，零额外调用）；预算、token 估算、历史统计、路由查表是算术，全在代码里。
2. **事前给事实，不事后否决。** 框架根本没给事后否决的接口——生命周期事件 observe-only（F8），preflight 只查路由真实性不查好坏（F5）。所以所有智能都必须以"事实注入"的形式出现在模型做决策**之前**。
3. **观测先行，路由最后。** L0/L1 不依赖任何其他层；L2 依赖 L1 的画像；L5 依赖 L4 的实验结论。任何一层烂尾，前面的产出都独立成立。

---

## 2. L0 观测层 `dswarm-observer`

**做什么**：把每一次委派变成一条结构化记录：谁委派、委派给谁、用什么模型、判断花了多少、执行花了多少（含缓存拆分）、结果如何。

**为什么**：画像、实验、路由全部建立在这批记录上。这是整个系统唯一不可缺省的层。

### 2.1 v0：离线解析器（Python，先做）

会话日志是 event-sourced JSONL（F11），一切信息都在磁盘上，解析器零运行时侵入：

```
读取一个 session 日志目录
  ├─ header      → origin=='subagent'?  parent 链接, seedLength(fork 边界), delegationDepth
  ├─ descriptor  → agentOptions.provider/model/reasoningEffort  ← 该子 agent 的实际路由
  ├─ 事件流       → 逐次调用的 usage（cacheRead/cacheWrite 拆分, F9）+ provider/model
  └─ 终止事件     → 推导 stopReason（completed / error / max-tokens / aborted）
```

产出 `DelegationRecord` JSONL：

```json
{
  "run_id": "...", "parent_session": "...", "child_session": "...",
  "route": {"provider": "glm", "model": "glm-5.3", "effort": null},
  "mode": "fork",                       // seedLength>0 → fork，否则 fresh
  "depth": 1,
  "judge_tokens": 4200,                 // 判断成本：父会话从任务开始到委派发出的增量
  "exec_usage": {"input": 31200, "cacheRead": 26800, "cacheWrite": 4400, "output": 3800},
  "return_tokens": 900,                 // 回流成本：lastAssistantMessage 估算
  "stop_reason": "completed",
  "wall_ms": 48300
}
```

三笔成本齐全意味着 **§4.3 委派盈亏线实验零额外成本**——分子（judge_tokens）分母（exec 未进父级的部分）都在记录里。

**为什么用 Python 而不是 TS**：A/B 项目的分析栈（trace/eval）是 Python，解析器产出的 JSONL 直接进同一套分析习惯；harness 侧零依赖。

### 2.2 v1：实时插件（TS，后做）

仅在需要实时反馈（如在线路由、实验过程监控）时再做：

```ts
// Cordis 插件骨架——只做记录，不做任何干预（observe-only, F8）
export function apply(ctx: Context) {
  ctx.on('subagent/start', (info) => {
    // info: { runId, provider, id: SessionId, local }
    store.markStart(info.runId, info.id, info.provider, Date.now())
  })
  ctx.on('subagent/end', (info) => {
    // info: + { stopReason, lastAssistantMessage }
    store.settle(info.runId, info.stopReason, info.lastAssistantMessage)
  })
}
```

**常见坑**：
- continuable 子 agent 是**一个 SessionId 对多个 runId**（每次 Activation epoch 一对 start/end）——记录以 runId 为主键，不是 SessionId
- 冷恢复后 provider 可能缺席（F8 注释），解析器不能用 provider 字段做 join 键
- 插件抛异常会扩散到事件总线——observer 内部必须全 try/catch 兜底，观测者弄挂被观测者是最低级的事故

---

## 3. L1 画像层 `dswarm-profile`

**做什么**：把 `DelegationRecord` 聚合成每个 `(provider, model)` 的能力画像。

**为什么**：这是"给决策提供数据依据"里那份数据。注入层和路由层都只读画像，不读原始日志。

```sql
-- SQLite 聚合表（由 DelegationRecord 增量重建）
CREATE TABLE model_profile (
  provider TEXT, model TEXT, model_version TEXT,   -- 版本单列：模型换版后旧数据自动隔离
  bucket TEXT,                                      -- 任务桶，见下
  alpha REAL, beta REAL,                            -- Beta 后验：成功/失败伪计数
  avg_exec_input REAL, avg_cache_read REAL, avg_output REAL,
  avg_cost_yuan REAL,                               -- 按单价表折算
  p50_wall_ms REAL,
  samples INTEGER,
  updated_at TEXT
);
```

- **任务分桶** v0 用规则：按子 agent 的工具使用混合与文件类型（读多写少=探索桶、测试命令多=验证桶、编辑集中=实现桶）。v1 样本过千后再考虑 embedding 聚类——在那之前规则桶够用了，别过早工程。
- **衰减曲线**：按 `exec_usage.input` 分位数切段统计成功率，每模型一条。这是 §4.4 的直接产出，也是注入层"该给多少 context"的依据。
- **冷启动**：Beta(1,1) 均匀先验。不引 AA 榜单分做伪计数——先验弱是一回事，把弱先验编码进系统是另一回事，不如没有。

**常见坑**：桶太细会样本稀疏到没有统计意义——设最小样本阈值（如 n<8），不到的桶向上合并到 "other"。

---

## 4. L2 注入层 `dswarm-inject`

**做什么**：把画像事实在模型**做决策之前**放进它的视野。三个通道，按优先级排列。

**为什么**：这是整个系统的"发动机"——差异化全部体现在模型读到的事实从"provider+model id"升级成"画像+成本+历史"。

### 通道 1：富化发现工具（主通道）

随附 `list_subagent_models` 是按工具实例注册的、输出纯文本（F6）。关掉随附实例，注册自己的：

```ts
ctx.tools.register(defineTool({
  name: 'list_dswarm_models',          // 不同名，避免与随附实例冲突
  description: 'Discover LLM routes for subagents, with measured capability profiles.',
  parameters: { /* 与随附版同构：provider?/model? */ },
  output: { schema: { type: 'string' }, render: (_a, r) => [{ type: 'text', text: r }] },
  async execute(args, exec) {
    // 先按随附版逻辑列出白名单路由，再逐行追加画像：
    // deepseek/deepseek-chat — ¥2/M in(命中¥0.5) · 实现桶成功率 82%(n=44) · 40k 后成功率骤降 · effort: medium
    // glm/glm-5.3 — ¥0(套餐) · 探索桶 74%(n=31) · 25k 后骤降 · effort: high
    return enrichedLines
  },
}))
```

**关键细节**：富化文本**只能活在发现工具的输出里（pull 通道），不能进 persona**——每个路由的画像各不相同，进了请求前部就击穿所有共享前缀（这正是 §2.6 的缓存原则）。发现工具输出在会话后部出现，是天然的正确位置。

### 通道 2：预算与约束注入（persona / 工具 schema 描述）

给 Lead 的决策空间加护栏事实："该子 agent 至多收 X token；本次预算剩 Y；该模型超 Z token 后成功率明显下降"。其中**所有子 agent 共享的部分**（总预算、全局规则）进 persona 前部保持前缀稳定；**逐任务不同的部分**（本次剩余）进 prompt 后部。

### 通道 3：静态路由钉定（`agentRouteDefaults`）

代码路由的执行端（F3+F7）：路由表查出来什么，就在委派工具实例的配置层钉什么。模型侧无感知、不触发白名单断言。**这是 L5 在线路由的最终落点**——路由决策完全在插件代码里，harness 机制零改动。

---

## 5. L3 委派层 `dswarm-delegate`

**做什么**：一个自定义委派工具实例，挂 fork 提供方、开 `modelSelectionSettings`，得到官方故意不发货的"fork+异模型"能力（F4）。

**为什么**：实验 4.2 的核心臂全靠它；它也是日后"带上下文继承的异模型委派"的产品形态。

```yaml
# 组合配置（cordis.patch.yml 片段）
- name: '@deepseek-ai/dsh-tool-subagent'
  config:
    provider: fork                    # 提供方本身声明了 agentOptions 能力
    toolName: dswarm_fork             # 与随附实例区分
    modelSelectionSettings: true      # 打开模型自选 → schema 增加 provider/model/reasoning_effort
```

**常见坑**：
- 异模型臂必须**显式重设 `reasoning_effort`**——路由变更时配置层 effort 被静默清除（F12），不清这个坑，对比实验测的其实是"异模型+无 effort"
- 白名单 `allowedModels` 必须包含目标路由，否则 `assertAllowedModelSelection` 在建子时才炸——实验配置加载后先做一次干跑预检
- fork 的 seed 由提供方从父级日志读取（F10 契约照旧），调用方不需要手动拼装

---

## 6. L4 实验层

四个实验的实现配置。成功判定统一走 bench（SWE-bench Lite/Verified 子集，测试通过=成功），不自建评分器。

### 4.1 模型强弱 × prompt 厚薄（二乘二）

| 配置 | 弱模型（GLM，套餐零成本） | 强模型（Claude/DS） |
|---|---|---|
| 薄 prompt | 基线格 | 基线格 |
| 厚 prompt | **关键格** | 边际收益检验格 |

- **厚度统一放 `prompt` 部（用户消息），不放 persona**——这是受控变量不是口味：进 persona 会改变所有子 agent 的共享前缀，thin/thick 两臂的缓存行为天生不同，成本对比被混淆。放 prompt 部则两臂前缀一致，只差在后部内容。
- 测量：成功率（bench 测试）、总成本（含 cache 拆分）、端到端延迟。

### 4.2 缓存经济学（三臂，P0 实验）

SWE-bench 同仓库任务序列（Django/sympy 单仓库几十上百个 issue），连续跑天然共享系统 prompt+仓库上下文前缀：

| 臂 | 实现载体 | 预期测量 |
|---|---|---|
| fork+同模型 | 随附 fork 工具 | 缓存复用基线（cacheRead 高） |
| fork+异模型 | `dswarm-delegate`（L3） | 前缀重写代价（cacheWrite 飙升） |
| fresh+异模型 | spawn 实例 | 不继承前缀的对照 |

**切换模型的真实代价 = 单价差 + 被丢弃的缓存命中价值**，三项臂两两相减即得。注意各家缓存 TTL 不同（Anthropic 5 分钟级、DeepSeek/OpenAI 自动前缀匹配）——**臂内任务间隔必须控制在 TTL 内**，并把间隔时长记进 DelegationRecord 作为协变量。

### 4.3 委派盈亏线

不需要专门实验。L0 的 `judge_tokens` / `exec_usage` / `return_tokens` 三笔账在日常使用和 4.1/4.2 跑动中自然积累，事后聚合出盈亏线。

### 4.4 长上下文衰减曲线

投喂长度梯度（5k/10k/20k/40k/80k）× 同一 bench 任务族，逐模型画成功率-长度曲线。产出直接进画像层（注入通道 2 的 Z 值来源）。

**成本控制**：GLM 臂零成本（Coding Plan，需确认覆盖 API）；贵模型只跑关键格；实验运行器内置单日预算硬熔断。

---

## 7. L5 路由层（后期，本节只定方向）

1. **离线 replay 先行**：同一份 DelegationRecord 日志，离线模拟 greedy / Thompson / 预算约束 argmax 三种策略的臂选择与对应成本——策略对比零风险零在线烧钱。
2. **在线路由 = 查表 + 通道 3 钉定**：策略收敛后，路由表落到 `agentRouteDefaults`，LLM 只在你想让它选的实例上保留选择权。
3. **永不做事后否决**——框架没接口（F8），也不符合"事前给事实"的原则。

---

## 8. Provider 接入

GLM 照 `acme-gateway` 模板纯配置接入（F1）：

```yaml
- name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      glm:
        displayName: Zhipu GLM
        api: openai-completions
        baseURL: https://open.bigmodel.cn/api/paas/v4
        apiKeyEnv: GLM_API_KEY
        compat:
          thinkingFormat: deepseek
        models:
          - id: glm-5.3-flash
            name: GLM 5.3 Flash
            contextWindow: 131072
```

DeepSeek 路由用 `dsh-llm-deepseek` 直连。凭据全部走 `apiKeyEnv` + harness 凭据 seam，配置文件中不落密钥。

---

## 9. 目录结构

```
DPswarm/
├─ analyzer/            # L0 v0 + L1：Python 离线解析与画像聚合
│  ├─ parse_sessions.py #   session JSONL → DelegationRecord
│  ├─ build_profile.py  #   DelegationRecord → SQLite 画像
│  └─ replay.py         #   L5 离线策略 replay
├─ plugin/              # L0 v1 + L2 + L3：TS Cordis 插件（dswarm）
│  ├─ src/observer.ts   #   start/end 监听（只读）
│  ├─ src/inject.ts     #   富化发现工具 + 预算注入
│  └─ src/profile-feed.ts # 读 SQLite 画像供 inject 使用
├─ experiments/         # L4：bench 任务清单、臂配置、运行器、预算熔断
├─ data/                # DelegationRecord JSONL + profile.sqlite（只增不删）
└─ docs/                # 本文档与后续实验报告
```

---

## 10. 关键决策记录（ADR 摘要）

| 决策 | 依据 |
|---|---|
| 不 fork 主仓，一切走插件/配置 | fork 禁选择是工具层策略（F4），提供方能力齐全；fork 主仓只会背上 9000 文件的 rebase 地狱 |
| 画像走展示层注入，不走配置层 | preflight 用活适配器验路由（F5），假数据建子前即炸 |
| 观测先于路由，且 v0 纯离线 | 会话日志自足（F9/F11）；实时插件只在需要在线反馈时才有存在理由 |
| 富化文本只进发现工具输出，不进 persona | 逐路由不同的内容进前部 = 击穿全部共享前缀 |
| 4.1 厚度统一放 prompt 部 | 放 persona 会让两臂缓存行为失配，成本对比被混淆 |
| 在线路由落 `agentRouteDefaults` 而非模型自选通道 | 纯继承不触发白名单断言（F3），路由逻辑全在代码里可复现 |
| 先验用 Beta(1,1)，不编码榜单分 | 弱先验编码进系统比没有更糟 |

---

## 附：与调研报告的对应关系

| 报告章节 | 本文档落点 |
|---|---|
| §2.8 分层原则 | §1 原则 + L2 三通道 |
| §2.9 / §4.3 盈亏线 | L0 三笔账（§2.1） |
| §4.1 二乘二 | §6.1（厚度摆放受控） |
| §4.2 缓存经济学 | §5 L3 + §6.2 三臂 |
| §4.4 衰减曲线 | §3 画像 + §6.4 |
| §六 建议第一步 | §2.1 L0 v0 离线解析器 |
