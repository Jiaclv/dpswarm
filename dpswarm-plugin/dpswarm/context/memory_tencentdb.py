"""TencentDB Agent Memory 后端适配（试点，可选）。

把 TencentDB-Agent-Memory（github.com/TencentCloud/TencentDB-Agent-Memory，
MIT）接为 MemoryService 的可选存储/检索后端。职责边界（铁律）：

- **晋升判据永远在本侧执行**：candidate/promote/reject/supersede/invalidate
  生命周期与 promotion check（accepted_by 必填）完全由 MemoryService 既有
  逻辑裁决；对方服务只是 durable 条目的存储副本 + BM25 打分引擎，不是审计
  权威。未过晋升门的内容永远不写入对方资产层。
- **事件口径不变**：事件仍由 MemoryService 的 sink 直通上层 EventStore
  （memory_candidate / memory_promoted / memory_superseded /
  memory_invalidated / memory_rejected），本类不新增事件词汇、不另存日志。
- **离线可用的映射**：对方的 L1（atomic）/L2（scenario）/L3（core）资产层
  只能由其 LLM 抽取管线写入（HTTP 面无 create 入口，update 对不存在记录
  返回 404）；离线可写且可检索的只有 L0 会话层。因此 promote 的 durable
  条目镜像为 L0 message（逐字 content），retrieve 用 conversation/search
  的服务端 BM25 打分排序。详见 modelbench/memory_pilot_20260910/README.md。

降级语义：远端不可达/报错时自动回落为纯本地行为（与 MemoryService 词面
检索完全一致），不抛错、不丢条目；backend_available / last_backend_error
暴露后端健康状态供观测。依赖守卫与 orchestrator_lg 同款：import 本模块零
依赖，构造时缺 httpx 才抛 ImportError（或注入 client 绕过）。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .memory import MemoryEntry, MemoryService

# 缺省实例与服务内会话：所有镜像条目写入同一条 L0 session，visibility/状态
# 过滤全部在本侧完成，远端只负责打分（服务端 session 内检索）。
_DEFAULT_SESSION = "dpswarm/memory"


class TencentDBMemoryService(MemoryService):
    """MemoryService 的 TencentDB 后端版：生命周期本侧权威 + 远端 BM25 排序。

    参数：
    - sink：与 MemoryService 相同的事件出口（如 ControlPlane._record）。
    - endpoint / api_key / service_id：对方 Gateway 地址与鉴权头
      （standalone 模式下 Bearer 只做非空校验，见对方 v2-router.parseV2Auth）。
    - session_id：镜像条目写入的 L0 会话 ID（缺省 dpswarm/memory）。
    - timeout：单次 HTTP 超时（秒）；远端慢/挂时由它卡住上限后降级。
    - client：可注入的 HTTP 客户端（需有 .post(path, json=...) 并返回带
      .raise_for_status()/.json() 的响应）。注入时不需要 httpx——便于测试
      与无 httpx 环境；不注入则按 endpoint 构造 httpx.Client。
    """

    def __init__(self, sink: Optional[Callable[[str, dict], None]] = None, *,
                 endpoint: str = "", api_key: str = "",
                 service_id: str = "default",
                 session_id: str = _DEFAULT_SESSION,
                 timeout: float = 5.0,
                 client: Optional[object] = None) -> None:
        super().__init__(sink)
        self._session_id = session_id
        self._last_error: Optional[str] = None
        # memory_id -> 远端 L0 message_id（镜像台账，与 _entries 同生命周期，
        # 同为进程内状态——MemoryService 本身也是纯内存实现，口径一致）。
        self._mirrored: Dict[str, str] = {}
        if client is not None:
            self._client = client
        else:
            try:
                import httpx  # noqa: PLC0415 — 依赖守卫：构造才报错
            except ImportError:
                raise ImportError(
                    "TencentDBMemoryService 需要 httpx：pip install httpx "
                    "（或注入 client= 使用自带传输；核心包保持零依赖）") from None
            if not endpoint:
                raise ValueError("未注入 client 时必须提供 endpoint")
            self._client = httpx.Client(
                base_url=endpoint.rstrip("/") + "/v2",
                headers={"Authorization": f"Bearer {api_key or 'dpswarm-pilot'}",
                         "x-tdai-service-id": service_id},
                timeout=timeout)

    # -- 观测 ----------------------------------------------------------------

    @property
    def backend_available(self) -> bool:
        """远端健康位：False = 最近一次远端调用失败（尚未调用过视为可用）。"""
        return self._last_error is None

    @property
    def last_backend_error(self) -> Optional[str]:
        return self._last_error

    # -- 写路径：先走本侧生命周期裁决，再镜像远端（best-effort） ----------------

    def promote(self, memory_id: str) -> MemoryEntry:
        """candidate → active（本侧裁决不变）；晋升成功后才镜像写入对方 L0。

        晋升判据（accepted_by 必填）在调用方/orchestrator 侧；本方法维持
        MemoryService 的状态机约束（非 candidate 抛 ValueError）。"""
        entry = super().promote(memory_id)
        self._mirror(entry)
        return entry

    def reject_candidate(self, memory_id: str) -> None:
        """拒候选：纯本侧操作——candidate 从未镜像，远端零接触。"""
        super().reject_candidate(memory_id)

    def supersede(self, old_id: str, new_entry: MemoryEntry) -> None:
        """接替：本侧生命周期不变；旧项的远端镜像随即删除（不再被检索命中），
        新项仍是 candidate，待 promote 时才镜像。"""
        super().supersede(old_id, new_entry)
        self._unmirror(old_id)

    def invalidate(self, memory_id: str, cause: str) -> None:
        """失效：本侧状态翻转后删除远端镜像，默认检索两侧同步排除。"""
        super().invalidate(memory_id, cause)
        self._unmirror(memory_id)

    # -- 读路径：本侧过滤（状态/ttl/visibility）+ 远端 BM25 排序 ---------------

    def retrieve(self, scope: str, query: str = "", limit: int = 8,
                 include_candidate: bool = False) -> List[MemoryEntry]:
        """与 MemoryService 同构：同一套过滤规则，query 非空且远端可用时改用
        对方服务端 BM25 排序。

        排序口径：先取本侧 eligible 集合（status/ttl/visibility 过滤与
        MemoryService 完全一致），再按远端 search 命中顺序排列；未镜像成功
        的 eligible 条目按本地词面分补尾——远端只增强排序，不增减可见性。
        远端报错 → 整体回落本地词面排序（与 MemoryService 逐字节一致）。
        """
        eligible = super().retrieve(scope, query, limit=len(self._entries) or 1,
                                    include_candidate=include_candidate)
        if not query or not eligible:
            return eligible[:limit]
        ranked = self._remote_ranks(query, max(limit * 4, 20))
        if ranked is None:                      # 远端不可用：本地口径兜底
            return eligible[:limit]
        order = {mid: rank for rank, mid in enumerate(ranked)}
        eligible.sort(key=lambda e: (order.get(e.memory_id, len(ranked)),
                                     -e.created_at, e.memory_id))
        return eligible[:limit]

    # -- 远端镜像（全部 best-effort：任何异常只记账、不抛出） --------------------

    def _mirror(self, entry: MemoryEntry) -> None:
        """把 durable 条目逐字写入对方 L0 会话；记 memory_id→message_id。"""
        if entry.memory_id in self._mirrored:
            return
        data = self._post("/conversation/add", {
            "session_id": self._session_id,
            "messages": [{"role": "user", "content": entry.content}],
        })
        if data is None:
            return
        ids = data.get("accepted_ids") or []
        if ids:
            self._mirrored[entry.memory_id] = ids[0]

    def _unmirror(self, memory_id: str) -> None:
        message_id = self._mirrored.pop(memory_id, None)
        if message_id is not None:
            self._post("/conversation/delete", {"message_ids": [message_id]})

    def _remote_ranks(self, query: str, limit: int) -> Optional[List[str]]:
        """远端 BM25 检索，返回按命中顺序排列的 memory_id 列表；失败 → None。"""
        data = self._post("/conversation/search", {
            "session_id": self._session_id, "query": query, "limit": limit,
        })
        if data is None:
            return None
        by_message = {msg: mid for mid, msg in self._mirrored.items()}
        return [by_message[m["id"]] for m in data.get("messages", [])
                if m.get("id") in by_message]

    def _post(self, path: str, body: dict) -> Optional[dict]:
        """单次远端调用：成功更新健康位并返回 data；任何异常降级返回 None。"""
        try:
            resp = self._client.post(path, json=body)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"tdai {path} code={payload.get('code')}: "
                                   f"{payload.get('message')}")
            self._last_error = None
            return payload.get("data") or {}
        except Exception as exc:  # noqa: BLE001 — 降级点：远端故障不传染本侧
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None
