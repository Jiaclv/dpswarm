"""TencentDB Agent Memory 后端适配试点（memory_tencentdb.py）测试。

口径：
- 晋升判据在本侧：未过 promotion_check（accepted_by 为空）的 candidate 走
  reject_candidate，永远不写入对方服务（fake 远端零记录）。
- 事件词汇/单账本不变：sink 直通，词汇与 MemoryService 完全一致。
- 降级：远端报错时不抛、不丢条目，retrieve 回落与 MemoryService 逐字节一致
  的本地词面排序。
- retrieve 往返：默认跑内嵌 fake 网关（hermetic）；置环境变量
  DPSWARM_TDAI_ENDPOINT 时追加跑真 Gateway 的 live 用例（缺 httpx/服务不可达
  时 pytest.skip）。
"""
from __future__ import annotations

import os

import pytest

from dpswarm.context.memory import CANDIDATE, REJECTED, MemoryService
from dpswarm.context.memory_tencentdb import TencentDBMemoryService


class _Resp:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


class FakeTdaiClient:
    """对方 Gateway /v2 会话面的最小内嵌仿真（add/search/delete 三端点）。

    打分用朴素词面重叠——目的不是复刻 BM25，而是验证适配器的连线、
    memory_id↔message_id 台账与降级切换；真 BM25 由 live 用例覆盖。
    """

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._seq = 0

    def post(self, path: str, json: dict | None = None) -> _Resp:
        body = json or {}
        if path == "/conversation/add":
            ids = []
            for m in body["messages"]:
                self._seq += 1
                mid = f"fake-msg-{self._seq}"
                self.messages.append({"id": mid, "session_id": body["session_id"],
                                      "content": m["content"]})
                ids.append(mid)
            return _Resp({"code": 0, "message": "ok",
                          "data": {"accepted_ids": ids, "total_count": len(ids)}})
        if path == "/conversation/search":
            q = body["query"]
            hits = [(sum(m["content"].count(t) for t in q.split()), m)
                    for m in self.messages
                    if m["session_id"] == body["session_id"]]
            hits = [(s, m) for s, m in hits if s > 0]
            hits.sort(key=lambda x: -x[0])
            msgs = [dict(m, score=s) for s, m in hits[: body.get("limit", 5)]]
            return _Resp({"code": 0, "message": "ok",
                          "data": {"messages": msgs, "total": len(msgs)}})
        if path == "/conversation/delete":
            drop = set(body["message_ids"])
            before = len(self.messages)
            self.messages = [m for m in self.messages if m["id"] not in drop]
            return _Resp({"code": 0, "message": "ok",
                          "data": {"deleted_count": before - len(self.messages)}})
        raise AssertionError(f"unexpected path: {path}")


class DeadClient:
    """远端全挂：所有调用抛连接错误。"""

    def post(self, path: str, json: dict | None = None) -> _Resp:
        raise ConnectionError(f"unreachable: {path}")


def _make(client, seen=None) -> TencentDBMemoryService:
    sink = (lambda kind, payload: seen.append((kind, payload))) if seen is not None else None
    return TencentDBMemoryService(sink=sink, client=client)


class TestPromotionGateOnOurSide:
    """铁律 1：晋升判据在本侧，未验收内容不写入对方资产层。"""

    def test_unaccepted_candidate_never_reaches_backend(self):
        from dpswarm.orchestrator import Orchestrator
        remote = FakeTdaiClient()
        m = _make(remote)
        cand = m.add_candidate("未验收的结论", "root", ["src"])  # accepted_by=""
        assert not Orchestrator.promotion_check(cand)     # 判据在本侧裁决
        m.reject_candidate(cand.memory_id)
        assert m.get(cand.memory_id).status == REJECTED
        # 对方服务零写入：candidate/rejected 都不是对方资产
        assert remote.messages == []
        assert cand not in m.retrieve(scope="root", query="结论")

    def test_only_accepted_content_is_mirrored(self):
        from dpswarm.orchestrator import Orchestrator
        remote = FakeTdaiClient()
        m = _make(remote)
        bad = m.add_candidate("未验收", "root", ["src"])
        good = m.add_candidate("已验收的结论", "root", ["src"],
                               accepted_by="root-lead")
        assert Orchestrator.promotion_check(good)
        m.reject_candidate(bad.memory_id)
        m.promote(good.memory_id)
        assert [msg["content"] for msg in remote.messages] == ["已验收的结论"]
        # 非 candidate 不能重复 promote（状态机约束与 MemoryService 同款）
        with pytest.raises(ValueError):
            m.promote(good.memory_id)


class TestEventLedgerParity:
    """事件直通口径与 MemoryService 一致：同词汇、单账本、无私有日志。"""

    def test_same_vocabulary_same_order(self):
        seen: list = []
        m = _make(FakeTdaiClient(), seen)
        e = m.add_candidate("事实", "root", ["src"], accepted_by="lead")
        m.promote(e.memory_id)
        c = m.add_candidate("被拒", "root", ["src"])
        m.reject_candidate(c.memory_id)
        assert [k for k, _ in seen] == [
            "memory_candidate", "memory_promoted",
            "memory_candidate", "memory_rejected"]
        assert not hasattr(m, "events") and not hasattr(m, "_event_log")


class TestRetrieveRoundTrip:
    """retrieve 往返：本侧过滤不变，远端可用时用远端排序。"""

    def test_remote_ranking_applies(self):
        m = _make(FakeTdaiClient())
        a = m.add_candidate("部署流水线必须先跑回归再发布", "root", ["s"],
                            accepted_by="lead")
        b = m.add_candidate("数据库迁移要向后兼容两个版本", "root", ["s"],
                            accepted_by="lead")
        m.promote(a.memory_id)
        m.promote(b.memory_id)
        hits = m.retrieve(scope="root", query="数据库 迁移")
        assert hits and hits[0].memory_id == b.memory_id
        hits = m.retrieve(scope="root", query="回归 发布")
        assert hits and hits[0].memory_id == a.memory_id

    def test_visibility_and_status_filter_stay_local(self):
        m = _make(FakeTdaiClient())
        na = m.add_candidate("node-a 私有结论", "node.a", ["s"], accepted_by="lead")
        m.promote(na.memory_id)
        # 跨 node 不互穿（§5.6 过滤在本侧，与远端检索无关）
        assert na not in m.retrieve(scope="node.b", query="私有结论")
        assert na in m.retrieve(scope="node.a", query="私有结论")
        # candidate 默认不进检索，即便远端也无镜像
        c = m.add_candidate("待审候选 私有", "node.a", ["s"])
        assert c.status == CANDIDATE
        assert c not in m.retrieve(scope="node.a", query="私有")
        assert c in m.retrieve(scope="node.a", query="私有",
                               include_candidate=True)

    def test_supersede_and_invalidate_unmirror(self):
        remote = FakeTdaiClient()
        m = _make(remote)
        old = m.add_candidate("旧事实", "root", ["s"], accepted_by="lead")
        m.promote(old.memory_id)
        assert len(remote.messages) == 1
        from dpswarm.context.memory import MemoryEntry
        new = MemoryEntry(memory_id="", scope="root", content="新事实",
                          visibility="root")
        m.supersede(old.memory_id, new)
        # 旧项远端镜像删除；新项未 promote 前不镜像
        assert remote.messages == []
        m.promote(new.memory_id)
        assert [msg["content"] for msg in remote.messages] == ["新事实"]
        hits = m.retrieve(scope="root", query="事实")
        assert [e.memory_id for e in hits] == [new.memory_id]
        m.invalidate(new.memory_id, "src-changed")
        assert remote.messages == []
        assert m.retrieve(scope="root", query="事实") == []


class TestGracefulDegradation:
    """对方服务不可用：不抛、不丢，回落与 MemoryService 完全一致。"""

    def test_dead_backend_falls_back_to_local(self):
        m = _make(DeadClient())
        e = m.add_candidate("降级也要能检索到", "root", ["s"], accepted_by="lead")
        m.promote(e.memory_id)                    # 镜像失败但不抛
        assert m.backend_available is False
        assert m.last_backend_error is not None
        # 与 MemoryService 同数据同口径对比，结果逐字节一致
        ref = MemoryService()
        r = ref.add_candidate("降级也要能检索到", "root", ["s"], accepted_by="lead")
        ref.promote(r.memory_id)
        assert [x.content for x in m.retrieve(scope="root", query="检索")] == \
            [x.content for x in ref.retrieve(scope="root", query="检索")]

    def test_backend_recovers_without_restart(self):
        dead = DeadClient()
        m = _make(dead)
        e = m.add_candidate("先写于远端宕机时", "root", ["s"], accepted_by="lead")
        m.promote(e.memory_id)
        assert m.backend_available is False
        remote = FakeTdaiClient()
        m._client = remote                        # 服务恢复（等价于重启后重连）
        m._mirror(e)                              # 补镜像（生产路径由下次 promote 触发）
        hits = m.retrieve(scope="root", query="宕机")
        assert [x.memory_id for x in hits] == [e.memory_id]
        assert m.backend_available is True

    def test_construct_requires_endpoint_or_client(self):
        with pytest.raises(ValueError):
            TencentDBMemoryService()              # 无 client 且无 endpoint


class TestLiveGateway:
    """真 Gateway 往返（BM25 排序）：置 DPSWARM_TDAI_ENDPOINT 才跑。

    本机试点：MemoryCore standalone（SQLite + BM25，embedding=none）。
    缺 httpx 或服务不可达 → skip，不算失败。
    """

    @pytest.fixture()
    def live(self):
        endpoint = os.environ.get("DPSWARM_TDAI_ENDPOINT", "").strip()
        if not endpoint:
            pytest.skip("DPSWARM_TDAI_ENDPOINT 未设置")
        httpx = pytest.importorskip("httpx")
        try:
            httpx.get(endpoint.rstrip("/") + "/health", timeout=3).raise_for_status()
        except Exception as exc:
            pytest.skip(f"gateway 不可达: {exc}")
        return TencentDBMemoryService(
            endpoint=endpoint, service_id="default",
            session_id="dpswarm-pilot/pytest-live", timeout=5.0)

    def test_live_roundtrip_bm25(self, live: TencentDBMemoryService):
        a = live.add_candidate("部署流水线必须先跑 pytest 全量回归再发布",
                               "root", ["s"], accepted_by="lead")
        b = live.add_candidate("数据库迁移要向后兼容两个版本",
                               "root", ["s"], accepted_by="lead")
        live.promote(a.memory_id)
        live.promote(b.memory_id)
        try:
            assert live.backend_available is True
            hits = live.retrieve(scope="root", query="数据库 迁移 兼容")
            # 远端 BM25 排序生效：相关条目居首；与 MemoryService 一致不设
            # 相关度截断，未命中条目按本地口径补尾（远端只重排、不增减可见性）
            assert hits and hits[0].memory_id == b.memory_id
            assert [e.memory_id for e in hits].index(b.memory_id) < \
                [e.memory_id for e in hits].index(a.memory_id)
            hits = live.retrieve(scope="root", query="回归 发布")
            assert hits and hits[0].memory_id == a.memory_id
        finally:
            live.invalidate(a.memory_id, "test-cleanup")
            live.invalidate(b.memory_id, "test-cleanup")
