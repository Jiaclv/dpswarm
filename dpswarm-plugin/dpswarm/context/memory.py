"""Memory Service：外部分层记忆与检索（机制三 §5.6 / §5.7）。

对应《DPswarm-机制架构.md》：
- §5.5  持久状态不在任何 agent 的上下文里——durable memory 是独立于
        model window 的可丢弃性之外的一层（辅助召回层，不是事实源）。
- §5.6  分层记忆：只收已验收结论/决策/项目知识/经确认的失败模式；
        provenance（source_ids / artifact_hash）必须保留；读权限与晋升
        分开授权（visibility 与晋升流互不混同）。
- §5.7  两条生命周期：candidate → promotion check → durable（active），
        之后可 superseded / invalidated；历史不覆盖，新项声明 supersedes；
        被拒/失效/过期条目默认不进检索。

设计口径（本实现）：
- scope 层级链 user > project > root > team > node（祖先在前）。检索按
  传入 scope 及其祖先链匹配 visibility：裸层名（user/project/root/…）
  条目对更内层 scope 可见；带限定符的条目（'team:t1' / 'node.n1'）仅
  与调用方 scope 精确匹配可见——跨 team / 跨 node 私有记忆不互穿（§5.6）。
- 检索为纯词面匹配（query 分词 + 命中率排序），不引入向量库。
- 记忆操作映射为控制面事件（memory_candidate / memory_promoted /
  memory_superseded / memory_invalidated / memory_rejected），经可选
  sink 直通上层 EventStore 落盘（单套账本，不另存私有事件日志）。
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..types import new_id

# §5.6 scope 层级：user 最外层，node 最内层（祖先在前）
SCOPE_CHAIN: List[str] = ["user", "project", "root", "team", "node"]

# 状态词汇：candidate（待晋升检查）/ active（durable，默认可检索）/
# superseded / invalidated / rejected（候选被拒，原文留 evidence 审计）
ACTIVE = "active"
CANDIDATE = "candidate"
SUPERSEDED = "superseded"
INVALIDATED = "invalidated"
REJECTED = "rejected"


class MemoryLifecycleError(ValueError):
    """记忆生命周期非法跃迁（§5.7）：前置状态不满足（如 supersede 旧项
    非 active）。继承 ValueError，与 promote/reject 的既有错误口径兼容。"""

_WORD_RE = re.compile(r"[A-Za-z0-9_.\-]+|[\u4e00-\u9fff]+")


def _layer_of(scope: str) -> str:
    """取 scope 的层名：支持 'team:t2' / 'node.n1' 这类带限定形式。"""
    head = re.split(r"[:./]", scope.strip(), maxsplit=1)[0] if scope else ""
    return head if head in SCOPE_CHAIN else scope


def _qualified(scope: str) -> bool:
    """带限定符的 scope（'team:t1' / 'node.n1'）：层名合法且带限定后缀。"""
    layer = _layer_of(scope)
    return layer in SCOPE_CHAIN and scope.strip() != layer


def _ancestor_chain(scope: str) -> List[str]:
    """传入 scope 自身 + 全部祖先（user>project>root>team>node）。

    未知 scope 不在层级链内：只做精确匹配（链 = [scope] 本身）。
    """
    layer = _layer_of(scope)
    if layer in SCOPE_CHAIN:
        return SCOPE_CHAIN[: SCOPE_CHAIN.index(layer) + 1]
    return [scope]


def _tokenize(query: str) -> List[str]:
    """词面分词：ASCII 词 + 中文连续段（长段再补滑窗 bigram 提命中率）。"""
    tokens: List[str] = []
    for raw in _WORD_RE.findall(query.lower()):
        tokens.append(raw)
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw) and len(raw) >= 3:
            tokens.extend(raw[i: i + 2] for i in range(len(raw) - 1))
    return tokens


def _score(content: str, tokens: List[str]) -> int:
    """命中率计分：各 token 在内容中出现次数之和（简单词面检索）。"""
    low = content.lower()
    return sum(low.count(t) for t in tokens)


@dataclass
class MemoryEntry:
    """一条记忆条目（§5.7 最小逻辑元数据集）。

    - source_ids / artifact_hash：provenance，保留来源指针，不因复述转正（§5.6）。
      artifact_hash = 未截断来源原文的 hash；content 可能是截断副本。
    - content_truncated：content 为原文截断副本时置 True（截断副本与全文
      hash 口径不同属预期，靠本标志区分）。
    - accepted_by：验收 Lead 身份；durable memory 只收验收过的内容（§4）。
    - ttl：created_at + ttl 过期即默认排除；None = 永久。
    - supersedes：新事实与旧记忆冲突时不覆盖历史，新项声明旧项 id。
    - status：candidate/active/superseded/invalidated/rejected。
    - visibility：读权限层（与晋升授权分开，§5.6）。
    """

    memory_id: str
    scope: str                  # user/project/root/team/node（可带限定后缀）
    content: str
    source_ids: List[str] = field(default_factory=list)
    artifact_hash: str = ""
    content_truncated: bool = False
    accepted_by: str = ""       # Lead 验收者（durable 只收验收过的内容 §4）
    revision: int = 1
    created_at: float = 0.0
    ttl: Optional[float] = None  # 过期默认排除；None 永久
    supersedes: Optional[str] = None
    status: str = "active"      # active/superseded/invalidated（+candidate/rejected）
    visibility: str = "team"    # 读权限与晋升分开授权（§5.6）

    @property
    def expired(self) -> bool:
        """ttl 未过期 = created_at + ttl > now；ttl=None 永久有效。"""
        return self.ttl is not None and (self.created_at + self.ttl) <= time.time()

    def to_payload(self) -> dict:
        return {
            "memory_id": self.memory_id, "scope": self.scope,
            "source_ids": list(self.source_ids), "artifact_hash": self.artifact_hash,
            "content_truncated": self.content_truncated,
            "accepted_by": self.accepted_by, "revision": self.revision,
            "created_at": self.created_at, "ttl": self.ttl,
            "supersedes": self.supersedes, "status": self.status,
            "visibility": self.visibility,
        }


class MemoryService:
    """分层记忆服务：晋升/版本/失效流 + 词面检索 + 事件映射。

    长期知识住在这里（§5.5 配套推论），不占用任何 agent 的窗口；
    活的 worker 经 pull 通道到这里补料是正常运行机制（§5.4）。
    """

    def __init__(self, sink: Optional[Callable[[str, dict], None]] = None) -> None:
        """sink：可选事件出口（如 ControlPlane._record），收 (kind, payload)。
        None = 只维护条目、不发事件（独立用法）。事件的唯一账本在上层
        EventStore——本类不再另存私有事件日志（单套账本，§5.6/§9.1）。"""
        self._entries: List[MemoryEntry] = []
        self._by_id: dict = {}
        self._sink = sink

    # -- 写路径（§5.7 生命周期） --------------------------------------------

    def add_candidate(self, content: str, scope: str, source_ids: List[str],
                      artifact_hash: str = "", accepted_by: str = "",
                      ttl: Optional[float] = None,
                      visibility: str = "",
                      content_truncated: bool = False) -> MemoryEntry:
        """登记 candidate：promotion check 未过前不是 durable，
        默认不被 retrieve 命中（include_candidate=False 时）。

        visibility 缺省取 scope 原样：裸层名 scope（'root'/'team'）得到
        裸层可见性（按祖先链对更内层可见）；带限定符的 scope（'team:t1'/
        'node.n1'）默认条目私有——仅与调用方 scope 精确匹配可见（§5.6）。
        content_truncated：content 为截断副本时置 True（见 MemoryEntry）。
        """
        entry = MemoryEntry(
            memory_id=new_id("mem"),
            scope=scope,
            content=content,
            source_ids=list(source_ids or []),
            artifact_hash=artifact_hash,
            content_truncated=content_truncated,
            accepted_by=accepted_by,
            revision=1,
            created_at=time.time(),
            ttl=ttl,
            status=CANDIDATE,
            visibility=visibility or scope,
        )
        self._entries.append(entry)
        self._by_id[entry.memory_id] = entry
        self._emit("memory_candidate", entry.to_payload())
        return entry

    def promote(self, memory_id: str) -> MemoryEntry:
        """candidate → active（durable）：过 promotion check 后才进默认检索。"""
        entry = self._get(memory_id)
        if entry.status != CANDIDATE:
            raise ValueError(f"memory {memory_id} status={entry.status}, not candidate")
        entry.status = ACTIVE
        self._emit("memory_promoted", {
            "memory_id": memory_id, "accepted_by": entry.accepted_by,
            "scope": entry.scope, "revision": entry.revision,
        })
        return entry

    def reject_candidate(self, memory_id: str) -> None:
        """候选被拒：不晋升、不进默认检索；原文留 evidence / failure audit
        （§5.7：rejected 不等于知识，经确认的失败结论应另立条目晋升）。
        事件词汇为 memory_rejected（独立于 memory_invalidated：拒候选 ≠
        durable 失效，旧日志的 invalidated cause="candidate-rejected" 由
        回放侧容错兼容）。"""
        entry = self._get(memory_id)
        if entry.status != CANDIDATE:
            raise ValueError(f"memory {memory_id} status={entry.status}, not candidate")
        entry.status = REJECTED
        self._emit("memory_rejected", {
            "memory_id": memory_id, "scope": entry.scope,
        })

    def supersede(self, old_id: str, new_entry: MemoryEntry) -> None:
        """新事实与旧记忆冲突：不覆盖历史（§5.7）。

        前置：旧项必须 active（只有 durable 允许被接替），否则抛
        MemoryLifecycleError。新项强制 candidate——晋升检查独立于本操作，
        过 check 后由 promote 转正，此前不进默认检索；revision 沿
        supersedes 链取 max+1（链上版本严格递增，兼容历史非单调数据）。
        旧项 → superseded，默认检索排除但保留审计轨迹。
        """
        old = self._get(old_id)
        if old.status != ACTIVE:
            raise MemoryLifecycleError(
                f"memory {old_id} status={old.status}, supersede 要求旧项 active")
        revision = old.revision
        cursor = old
        while cursor.supersedes:
            cursor = self._get(cursor.supersedes)
            revision = max(revision, cursor.revision)
        new_entry.supersedes = old_id
        new_entry.revision = revision + 1
        new_entry.status = CANDIDATE
        if new_entry.created_at == 0.0:
            new_entry.created_at = time.time()
        if not new_entry.memory_id:
            new_entry.memory_id = new_id("mem")
        self._entries.append(new_entry)
        self._by_id[new_entry.memory_id] = new_entry
        old.status = SUPERSEDED
        self._emit("memory_superseded", {
            "old_id": old_id, "new_id": new_entry.memory_id,
            "revision": new_entry.revision, "scope": new_entry.scope,
        })

    def invalidate(self, memory_id: str, cause: str) -> None:
        """代码/配置/DAG/权限或来源产物变化 → 依赖记忆失效，默认检索排除。"""
        entry = self._get(memory_id)
        entry.status = INVALIDATED
        self._emit("memory_invalidated", {"memory_id": memory_id, "cause": cause})

    # -- 读路径 -------------------------------------------------------------- 

    def get(self, memory_id: str) -> MemoryEntry:
        return self._get(memory_id)

    def retrieve(self, scope: str, query: str = "", limit: int = 8,
                 include_candidate: bool = False) -> List[MemoryEntry]:
        """按 scope 祖先链 + 状态 + ttl 过滤，query 词面命中率排序。

        过滤规则：
        - status：默认只 active（durable）；include_candidate=True 追加
          candidate（供晋升检查预览，非默认命中）。
        - ttl：created_at + ttl > now 才有效，None 永久。
        - visibility（§5.6 读权限）：裸层名条目（'root'/'team'/…）按祖先链
          匹配，祖先层记忆对更内层 scope 可见；带限定符的条目（'team:t1'/
          'node.n1'）仅与调用方 scope 精确匹配可见——跨 team / 跨 node
          私有记忆不互穿。
        """
        allowed_status = [ACTIVE] + ([CANDIDATE] if include_candidate else [])
        chain = _ancestor_chain(scope)
        hits = [
            e for e in self._entries
            if e.status in allowed_status and not e.expired
            and (e.visibility == scope if _qualified(e.visibility)
                 else _layer_of(e.visibility) in chain)
        ]
        tokens = _tokenize(query)
        if tokens:
            hits.sort(key=lambda e: (-_score(e.content, tokens),
                                     -e.created_at, e.memory_id))
        return hits[:limit]

    # -- 内部 ----------------------------------------------------------------

    def _get(self, memory_id: str) -> MemoryEntry:
        try:
            return self._by_id[memory_id]
        except KeyError:
            raise KeyError(f"unknown memory_id: {memory_id}") from None

    def _emit(self, kind: str, payload: dict) -> None:
        """记忆操作 → 控制面事件（memory_* 词汇见 events.py）：经可选 sink
        直通上层 EventStore，单套账本、字段以 payload 为准；无 sink 时只
        维护条目状态，不发事件。"""
        if self._sink is not None:
            self._sink(kind, payload)
