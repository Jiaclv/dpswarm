"""§5.6/§5.7 记忆子系统回归（修复阶段 4）：

- retrieve 可见性：带限定符条目（team:x / node.y）仅与调用方 scope 精确
  匹配可见（修跨 node/team 互穿）；裸层名按祖先链匹配（root 层对 node
  可见、root 看不到 team 层、team 裸层调用方原语义不变）。
- supersede 走生命周期：旧项非 active 抛 MemoryLifecycleError；新项强制
  candidate（未过 promotion check 不进默认检索）；revision 沿 supersedes
  链严格递增。
- 晋升显式化：promotion_check（V1=accepted_by 非空）未过走
  reject_candidate；截断内容置 content_truncated，artifact_hash 保持全文。
- 事件词汇与单账本：reject_candidate 发 memory_rejected；MemoryService
  无私有双账本，事件经 sink 直通 EventStore；旧日志回放容错。
"""
from __future__ import annotations

import hashlib

import pytest

from dpswarm import state
from dpswarm.context.memory import (
    ACTIVE,
    CANDIDATE,
    REJECTED,
    SUPERSEDED,
    MemoryEntry,
    MemoryLifecycleError,
    MemoryService,
)
from dpswarm.events import Event, EventStore


def _active(memory: MemoryService, content: str, scope: str,
            visibility: str = "") -> MemoryEntry:
    """登记并晋升一条 durable memory（测试便利：默认检索可见）。"""
    e = memory.add_candidate(content, scope, ["src"], visibility=visibility)
    memory.promote(e.memory_id)
    return e


class TestRetrieveVisibility:
    """§5.6 读权限：限定条目精确匹配，裸层名祖先链匹配。"""

    def test_node_entries_do_not_leak_across_nodes(self):
        m = MemoryService()
        self_entry = _active(m, "node-a 的私有结论", "node.a")
        # 本人可见；兄弟 node 检索不到（修复前 visibility 落到裸层 node 互穿）
        assert self_entry in m.retrieve(scope="node.a")
        assert self_entry not in m.retrieve(scope="node.b")

    def test_team_entries_do_not_leak_across_teams(self):
        m = MemoryService()
        t1 = _active(m, "team t1 的结论", "team:t1")
        assert t1 in m.retrieve(scope="team:t1")
        assert t1 not in m.retrieve(scope="team:t2")

    def test_qualified_entry_requires_exact_scope(self):
        m = MemoryService()
        t1 = _active(m, "t1 限定条目", "team:t1")
        # 裸 team 调用方 ≠ team:t1：限定条目对其不可见（团队内层私有）
        assert t1 not in m.retrieve(scope="team")

    def test_bare_layer_visibility_unchanged(self):
        m = MemoryService()
        root_entry = _active(m, "root 层公共知识", "root")
        team_entry = _active(m, "team 层共享知识", "team")
        # root 层对更内层 node 可见（祖先链匹配）
        assert root_entry in m.retrieve(scope="node.n1")
        # team 裸层调用方可见 team 裸层与 root 层条目（生产路径原语义不变）
        team_hits = m.retrieve(scope="team")
        assert team_entry in team_hits and root_entry in team_hits
        # root 仍看不到 team/node 层
        assert team_entry not in m.retrieve(scope="root")


class TestSupersedeLifecycle:
    """§5.7：supersede 走完整生命周期（此前零调用且绕过晋升流）。"""

    def test_supersede_rejects_non_active_old(self):
        m = MemoryService()
        cand = m.add_candidate("候选", "root", ["src"])          # candidate
        with pytest.raises(MemoryLifecycleError):
            m.supersede(cand.memory_id,
                        MemoryEntry(memory_id="", scope="root", content="新"))
        old = _active(m, "旧事实", "root")
        m.invalidate(old.memory_id, "src-changed")               # invalidated
        with pytest.raises(MemoryLifecycleError):
            m.supersede(old.memory_id,
                        MemoryEntry(memory_id="", scope="root", content="新"))
        # 类型化错误与既有 ValueError 口径兼容（promote/reject 同款）
        assert issubclass(MemoryLifecycleError, ValueError)

    def test_supersede_new_entry_starts_as_candidate(self):
        m = MemoryService()
        old = _active(m, "旧事实", "root")
        new = MemoryEntry(memory_id="", scope="root", content="新事实",
                          visibility="root",
                          status=ACTIVE)   # 即便调用方给了 active 也强制回落
        m.supersede(old.memory_id, new)
        assert old.status == SUPERSEDED
        assert new.status == CANDIDATE
        # 未过 promotion check 不进默认检索；promote 后转正
        assert new not in m.retrieve(scope="root")
        m.promote(new.memory_id)
        assert new in m.retrieve(scope="root")

    def test_supersede_revision_strictly_increases_along_chain(self):
        m = MemoryService()
        e1 = _active(m, "v1 事实", "root")
        e2 = MemoryEntry(memory_id="", scope="root", content="v2 事实",
                         revision=1)
        m.supersede(e1.memory_id, e2)
        assert e2.revision == e1.revision + 1
        m.promote(e2.memory_id)
        e3 = MemoryEntry(memory_id="", scope="root", content="v3 事实",
                         revision=1)
        m.supersede(e2.memory_id, e3)
        # 沿 supersedes 链取 max+1：链上严格递增
        assert e3.revision == e2.revision + 1
        assert e1.revision < e2.revision < e3.revision


class TestEventLedger:
    """事件词汇（memory_rejected）+ 单套账本（sink 直通，无私有日志）。"""

    def test_reject_candidate_emits_memory_rejected(self):
        store = EventStore()   # 内存 EventStore：append 时校验事件词汇
        m = MemoryService(sink=lambda kind, payload: store.append(kind, payload))
        cand = m.add_candidate("未过检查的结论", "root", ["src"])
        m.reject_candidate(cand.memory_id)
        assert m.get(cand.memory_id).status == REJECTED
        kinds = [e.kind for e in store.read_all()]
        # 拒候选 ≠ durable 失效：发 memory_rejected，不再借 memory_invalidated
        assert "memory_rejected" in kinds
        assert "memory_invalidated" not in kinds
        ev = [e for e in store.read_all() if e.kind == "memory_rejected"][0]
        assert ev.payload["memory_id"] == cand.memory_id

    def test_single_ledger_no_private_log(self):
        seen = []
        m = MemoryService(sink=lambda kind, payload: seen.append((kind, payload)))
        e = _active(m, "事实", "root")
        # 私有双账本已删：events()/_event_log 不复存在，事件只有 sink 一路
        assert not hasattr(m, "events")
        assert not hasattr(m, "_event_log")
        assert [k for k, _ in seen] == ["memory_candidate", "memory_promoted"]
        # 无 sink 的独立用法：只维护条目、不发事件
        solo = MemoryService()
        solo.add_candidate("独立", "root", ["src"])
        assert [k for k, _ in seen] == ["memory_candidate", "memory_promoted"]

    def test_replay_tolerates_old_and_new_reject_vocabulary(self):
        # 旧日志：reject 曾借道 memory_invalidated cause="candidate-rejected"，
        # 回放不能崩；新词汇 memory_rejected 同样回放为忽略（§5.6 记忆账本
        # 不进控制面投影）
        events = [
            Event(seq=0, kind="memory_invalidated",
                  payload={"memory_id": "m1", "cause": "candidate-rejected"}),
            Event(seq=1, kind="memory_rejected",
                  payload={"memory_id": "m2", "scope": "root"}),
        ]
        proj = state.replay(events)   # 不抛即通过
        assert proj is not None


class TestPromotionExplicit:
    """晋升显式化（orchestrator 侧）：promotion_check 判据 + hash 口径。"""

    def test_promotion_check_requires_accepted_by(self):
        from dpswarm.orchestrator import Orchestrator
        with_by = MemoryEntry(memory_id="m", scope="root", content="x",
                              accepted_by="node-lead")
        without_by = MemoryEntry(memory_id="m2", scope="root", content="x")
        assert Orchestrator.promotion_check(with_by)
        assert not Orchestrator.promotion_check(without_by)

    def test_extract_memory_truncation_flag_and_fulltext_hash(self, tmp_path):
        from dpswarm.control import ControlPlane
        from dpswarm.orchestrator import Orchestrator
        from dpswarm.providers import MockProvider
        from dpswarm.types import ModelCatalog, RootExecutionSpec

        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                                 max_active_node_points=8),
                          store_path=tmp_path / "events.jsonl",
                          catalog=ModelCatalog())
        memory = MemoryService(sink=cp._record)
        orch = Orchestrator(cp, MockProvider(script=[]), store_dir=None,
                            memory=memory)
        submission = "长交付 " + "x" * 5000
        orch._extract_memory("item-1", "pkg-1", submission)
        entry = memory.retrieve(scope="root")[0]
        # content 截断副本 + 标志；artifact_hash 仍是未截断全文 hash
        assert len(entry.content) == 4000
        assert entry.content_truncated is True
        assert entry.artifact_hash == \
            hashlib.sha256(submission.encode("utf-8")).hexdigest()[:16]
        # 事件单账本且字段一致：candidate 事件带截断标志（条目 payload 口径）
        cand = [e for e in cp.store.read_all()
                if e.kind == "memory_candidate"][0]
        assert cand.payload["content_truncated"] is True
        assert cand.payload["artifact_hash"] == entry.artifact_hash

    def test_extract_memory_short_content_not_flagged(self, tmp_path):
        from dpswarm.control import ControlPlane
        from dpswarm.orchestrator import Orchestrator
        from dpswarm.providers import MockProvider
        from dpswarm.types import ModelCatalog, RootExecutionSpec

        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                                 max_active_node_points=8),
                          store_path=tmp_path / "events.jsonl",
                          catalog=ModelCatalog())
        memory = MemoryService(sink=cp._record)
        orch = Orchestrator(cp, MockProvider(script=[]), store_dir=None,
                            memory=memory)
        orch._extract_memory("item-1", "pkg-1", "短交付")
        entry = memory.retrieve(scope="root")[0]
        assert entry.content_truncated is False
        assert entry.content == "短交付"
