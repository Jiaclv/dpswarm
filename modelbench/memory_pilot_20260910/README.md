# Memory 层试点：TencentDB-Agent-Memory 接为 MemoryService 可选后端

日期：2026-09-10 · 状态：试点完成（本机全离线跑通，含真服务 live 验证）

## 1. 侦察结论（它实际怎么跑、依赖什么）

来源：`git clone --depth 1 https://github.com/TencentCloud/TencentDB-Agent-Memory`
→ `.tmp/tencentdb-am/`，HEAD = `906b5823`（2026-09-10），LICENSE = MIT（只引用
不复制，经其 HTTP API 接入）。

**项目形态**：不是一个 pip 包，而是一组 TypeScript 服务：
- `MemoryCore/`：记忆与元数据核心（L0 会话 / L1 记忆原子 / L2 场景文件 / L3
  核心人格 + Skill/Wiki/CodeGraph 元数据），HTTP Gateway 默认 :8420。
- `MemoryKnowledge/`（wiki/codegraph，:8424）、`MemoryPanel/`（管理面板，
  :8125）、`MemoryProxy/`（Anthropic/OpenAI 双协议代理，:8096）。
- Python SDK 在 `sdk/memory-core/python/`（`tencentdb_agent_memory`，httpx
  薄封装）。**PyPI 不可达**（本机 DNS 解析失败；且 pyproject 名称是
  `...-v2`，疑似未正式发布），故适配器按 SDK 文档化的 HTTP API 直接用
  httpx 接入——任务规则允许的三条接入路径之一。

**最小本地运行（已实测跑通）**：
```bash
cd .tmp/tencentdb-am/MemoryCore && npm install        # 618 包，Node >=22.16
TDAI_GATEWAY_CONFIG=../tdai-gateway.pilot.yaml node --import tsx src/gateway/server.ts
```
试点配置 `.tmp/tencentdb-am/tdai-gateway.pilot.yaml`：standalone 模式、SQLite
存储（默认）、`embedding.provider=none`（BM25-only）、dummy LLM key、数据落在
`.tmp/tencentdb-am/data/`。本机端口 8429，`GET /health` 返回
`stores.vectorStore=true, embeddingService=false`。

**零外部依赖边界（关键发现）**：
- ✅ 离线可用：L0 `conversation/add|query|search|delete`（逐字写入 +
  服务端 BM25 检索，jieba 中文分词 + FTS5，embedding 关闭时无需任何外部
  API）。已实测中文内容 UTF-8 往返逐字一致、相关查询命中、无关查询零命中、
  删除生效。
- ❌ 离线不可用（强依赖 LLM API）：**L1/L2/L3 的"创建"没有 HTTP 入口**——
  L1 atomic 只能由 L0→L1 LLM 抽取管线产生（`atomic/update` 对不存在记录
  返回 404，v2-router.ts:1066）；L2 `scenario/write` 同样只更新已存在文件
  （v2-router.ts:1937）；L3 同理。即对方的"资产层"写入权在其 LLM 管线手里。
- ⚠️ 每次 `conversation/add` 会无条件异步触发 L1 抽取（notifyPipeline）；
  试点 yaml 里 `extraction.enabled=false` 未抑制该触发（配置键路径未生效），
  dummy LLM 下表现为后台重试 3 次（约 6s）后放弃并标记完成——不影响数据面，
  但有日志噪音。若将来配真 LLM key，我们写入 L0 的精选条目会被对方管线二次
  蒸馏进 L1/L2/L3，需评估是否想要这个副作用。
- 鉴权：standalone 不校验 Bearer 内容（仅要求非空 + `x-tdai-service-id`
  头，v2-router.ts:344-365）；网关级 `TDAI_GATEWAY_API_KEY` 可选。

**结论**：它能在离线模式当"带 BM25 检索的 L0 存储后端"用；但它的核心卖点
（L0→L1→L2→L3 蒸馏、混合检索、Skill/Wiki 资产化）强依赖一个
OpenAI 兼容 LLM API。这恰好与我们"晋升判据必须在我侧"的铁律互补——详见下节。

## 2. 适配器设计

新增 `dpswarm-plugin/dpswarm/context/memory_tencentdb.py`
（`TencentDBMemoryService(MemoryService)`，同构接口，不改 memory.py 一行）：

- **职责边界（铁律 1）**：candidate→promote→reject/supersede/invalidate
  生命周期、`accepted_by` 晋升判据、visibility 过滤、ttl、事件词汇
  （memory_candidate/promoted/superseded/invalidated/rejected 经 sink 直通
  控制面 EventStore，单账本）全部走 `MemoryService` 既有逻辑。对方服务只是
  durable 条目的**存储副本 + 打分引擎**，永远收不到未过晋升门的内容。
- **离线映射**：`promote()` 成功后才把条目逐字镜像为 L0 message
  （`conversation/add`，记 memory_id→message_id 台账）；`supersede()` /
  `invalidate()` 随即删除远端镜像（不再被检索命中）；candidate/rejected
  从不触网。
- **retrieve()**：先用 `MemoryService` 原代码取 eligible 集合（状态/ttl/
  visibility 过滤逐字节不变），query 非空且远端可用时按 `conversation/search`
  的 BM25 命中序重排；未镜像成功的 eligible 条目按本地口径补尾——远端只
  重排，不增减可见性（与 MemoryService 不设相关度截断的语义一致）。
- **降级**：任何远端异常只记 `last_backend_error`（`backend_available` 可
  观测），retrieve 整体回落本地词面排序，与 MemoryService 结果一致；
  不抛错、不丢条目。
- **依赖守卫（铁律 2）**：模块 import 零依赖；构造时未注入 `client=` 且缺
  httpx 才抛 ImportError（参照 orchestrator_lg.py 的 langgraph 守卫）。
  注入 `client=`（任何带 `.post(path, json=)` 的对象）可无 httpx 运行。

用法（接入 orchestrator 时只需替换构造处，接口不变）：
```python
from dpswarm.context.memory_tencentdb import TencentDBMemoryService
memory = TencentDBMemoryService(sink=cp._record,
                                endpoint="http://127.0.0.1:8429",
                                service_id="default")
```

## 3. 测试与跑通证据

新增 `dpswarm-plugin/tests/test_memory_tencentdb.py`（10 例）：
- 晋升门在我侧：未验收 candidate → reject，fake 远端零写入；只有
  accepted_by 非空且过 promotion_check 的条目被镜像（2 例）。
- 事件口径：sink 事件序列与 MemoryService 完全一致、无私有账本（1 例）。
- retrieve 往返：内嵌 fake 网关下远端排序生效、§5.6 可见性/状态过滤不受
  远端影响、supersede/invalidate 远端镜像同步删除（3 例）。
- 降级：远端全挂时 promote 不抛、retrieve 与 MemoryService 逐字节一致；
  服务恢复后无需重启即生效；无 endpoint 无 client 构造报 ValueError（3 例）。
- live：置 `DPSWARM_TDAI_ENDPOINT` 跑真 Gateway 的 BM25 往返（缺 env/httpx/
  服务不可达 → pytest.skip）。

实测记录（本机，2026-09-10）：
- `pytest tests/test_memory_tencentdb.py` → **9 passed, 1 skipped**（hermetic）。
- `DPSWARM_TDAI_ENDPOINT=http://127.0.0.1:8429 pytest ...` → **10 passed**
  （真 MemoryCore standalone + SQLite + BM25，含中文检索排序与清理）。
- 依赖守卫验证：meta_path 屏蔽 httpx 后 import 正常、构造抛 ImportError、
  注入 client 可用（脚本实测输出见会话记录）。
- **全量回归：`pytest tests/ -q` → 608 passed, 1 skipped（44.8s），零破坏。**
- 未触碰 orchestrator.py / orchestrator_lg.py / compare.py；改动只有
  context/memory_tencentdb.py、tests/test_memory_tencentdb.py、本目录与
  .tmp/tencentdb-am/（侦察 clone + 试点配置 + 服务数据，可随时删）。

## 4. A/B 实验的下一步建议（本次不实跑）

1. **离线 A/B（立即可做）**：同一 workload 下 MemoryService（词面）vs
   TencentDBMemoryService（服务端 BM25）的检索质量对比——用既有
   experiment-01/02 的历史交付作为记忆语料，比对 `retrieve` top-k 命中率与
   重排质量。两个后端同接口，注入点只有构造处一行。
2. **带 LLM 的完整管线验证（需一枚 OpenAI 兼容 key）**：把试点 yaml 的
   llm 组配真，观察我们 promote 进 L0 的精选条目被蒸馏成 L1/L2/L3 后的
   检索增益（`atomic/search` 才会真正有料）。同时评估"二次蒸馏"是否符合
   我们 provenance 口径（对方蒸馏产物不经我们 promotion check，默认不可进
   我们的 durable 检索面——建议用独立 service_id 隔离）。
3. **持久化价值验证**：MemoryService 是纯内存实现，重启即丢；对方 SQLite
   持久化是本后端的真实增益点。可做一个"控制面重启后记忆仍可检索"的场景
   实验（需要适配器补一个启动时从 L0 会话重建本地台账的 restore 路径，
   目前未实现——镜像台账与 MemoryService 一样在内存里）。
4. 已知留尾：每次 `conversation/add` 的异步抽取噪音（dummy LLM 下重试
   3 次放弃）；`extraction.enabled=false` 未生效的根因（配置键路径）值得
   在正式接入前查清；ttl 过期条目目前只在本侧过滤，远端镜像未主动清理。
