"""端到端：MockProvider 驱动 机制一→五 全链（决策→准入→执行→验收→封存→观测）。"""
from __future__ import annotations

import json

import pytest

from dpswarm.control import ControlPlane
from dpswarm.orchestrator import Orchestrator
from dpswarm.providers import MockProvider
from dpswarm.types import (
    DelegationKind,
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
)


def catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    cat.register(ModelFacts("p", "a-model", Level.A, aa_dimensional={"coding": 8.5}))
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


def make_orch(cp, script):
    return Orchestrator(cp, MockProvider(script=script), store_dir=None,
                        lead_route=ModelRoute("p", "s-model", level=Level.S))


@pytest.fixture()
def cp(tmp_path):
    return ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                               max_active_node_points=8),
                        store_path=tmp_path / "events.jsonl", catalog=catalog())


class TestE2E:
    def test_single_mode_default_stays_single(self, cp):
        """§7 默认态单 agent：Lead 判定不必委派，root item 直接结案。"""
        orch = make_orch(cp, [
            {"text": json.dumps({"action": "single"})},
            {"text": "最终答案：42"},
        ])
        out = orch.run_task("the answer task")
        assert out["final"] == "single"
        root = cp._root_item_id()
        assert cp.proj.work_items[root].acceptance.value == "accepted"

    def test_fission_two_workers_parallel_accept(self, cp):
        """裂变：2 worker 并行执行（物理并行），Lead 逐个验收，全 accepted。"""
        orch = make_orch(cp, [
            {"text": json.dumps({"action": "fission",
                                 "route": {"provider": "p", "model": "b-model",
                                           "reasoning_effort": "default"},
                                 "subtasks": ["part A", "part B"]})},
            {"text": "WORKER-OUTPUT"},
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "good"})},
        ])
        out = orch.run_task("split into two")
        assert len(out["items"]) == 2
        assert all(i["outcome"] == "accepted" for i in out["items"])
        kinds = [e.kind for e in cp.store.read_all()]
        # 2 个 worker + root 终局闭环（P1-6：Lead accept → seal 链推 root accepted）
        assert kinds.count("work_item_accepted") == 3
        assert cp.proj.work_items[cp._root_item_id()].acceptance.value == "accepted"
        # 观测全账（§4）：两笔委派记录 + token 账
        assert kinds.count("observation_recorded") == 2
        assert "token_usage_recorded" in kinds
        assert cp.proj.open_worker_slots_used == 0  # 结案后槽全归还

    def test_reject_capability_retry_then_accept(self, cp):
        """§8 打回→归因(capability)→重试→通过：验收门把坏交付拦在交付外。"""
        orch = make_orch(cp, [
            {"text": json.dumps({"action": "derive",
                                 "route": {"provider": "p", "model": "b-model"}})},
            {"text": "坏交付：单位错了 ×10"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "unit error",
                                 "attribution": "capability"})},
            {"text": "好交付：单位已修正"},
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "fixed"})},
        ])
        out = orch.run_task("wealth table task")
        assert out["items"][0]["outcome"] == "accepted"
        kinds = [e.kind for e in cp.store.read_all()]
        assert "work_item_rejected" in kinds
        assert "work_item_retried" in kinds
        # 事件流可采打回理由（免费失败归因数据 §4）
        rejected = next(e for e in cp.store.read_all() if e.kind == "work_item_rejected")
        assert rejected.payload["attribution"] == "capability"

    def test_budget_exhausted_escalates(self, cp):
        """§8 预算耗尽走上交：escalated 无裁决、释放资源。"""
        reject = json.dumps({"verdict": "reject", "verdict_reason": "bad",
                             "attribution": "capability"})
        orch = make_orch(cp, [
            {"text": json.dumps({"action": "derive",
                                 "route": {"provider": "p", "model": "b-model"}})},
            {"text": "attempt-1"},
            {"text": reject},
            {"text": "attempt-2"},
            {"text": reject},
            {"text": "attempt-3"},
            {"text": reject},
            {"text": "attempt-4"},
        ])
        out = orch.run_task("hard task")
        assert out["items"][0]["outcome"] == "escalated"
        kinds = [e.kind for e in cp.store.read_all()]
        assert "work_item_escalated" in kinds
        assert cp.proj.open_worker_slots_used == 0

    def test_seal_and_replay_consistency(self, cp):
        """§9.6 封存三段式 + §9.1 离线 replay 全量重验一致。"""
        orch = make_orch(cp, [
            {"text": json.dumps({"action": "single"})},
            {"text": "done"},
        ])
        orch.run_task("quick")
        cp.begin_seal("root")
        cp.begin_settlement("root")
        cp.finish_seal("root")
        assert cp.proj.seal_phase["root"].value == "completed"

        from dpswarm import invariants, state
        events = cp.store.read_all()
        proj = state.replay(events)
        assert proj.active_points == cp.proj.active_points
        assert proj.graph_revision == cp.proj.graph_revision
        # 逐事件重验不抛（对账 §9.1）
        p = state.Projection()
        for ev in events:
            p = invariants.check_event(p, ev)

    def test_cli_mock_run(self, tmp_path, monkeypatch):
        """CLI 冒烟：init + run(mock) + status + replay。"""
        from dpswarm import cli
        ws = tmp_path / "ws"
        script = tmp_path / "script.json"
        script.write_text(json.dumps([
            {"text": json.dumps({"action": "single"})},
            {"text": "cli answer"},
        ]), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["init", "--dir", str(ws)])
        assert rc == 0
        rc = cli.main(["run", "--task", "cli task", "--mock", str(script), "--dir", str(ws)])
        assert rc == 0
        rc = cli.main(["status", "--dir", str(ws)])
        assert rc == 0
        rc = cli.main(["replay", "--dir", str(ws)])
        assert rc == 0


class TestWiringFixes:
    """核对后补的接线测试：pull 兜底（§5.4）/ 记忆闭环（§5.7）/ 画像搬运（§4）。"""

    def _cp(self, tmp_path):
        return ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                                  max_active_node_points=8),
                            store_path=tmp_path / "events.jsonl", catalog=catalog())

    def test_pull_roundtrip_and_memory_loop(self, tmp_path):
        from dpswarm.context import MemoryService
        cp = self._cp(tmp_path)
        memory = MemoryService(sink=cp._record)
        entry = memory.add_candidate("关键指标 X=42（已验收事实）", "root", ["ev-1"])
        memory.promote(entry.memory_id)
        script = [
            {"text": json.dumps({"action": "derive",
                                 "route": {"provider": "p", "model": "b-model"}})},
            {"text": "PULL: 关键指标"},
            {"text": "最终答案：X=42"},                       # pull 补料后的第二轮
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "ok"})},
        ]
        orch = Orchestrator(cp, MockProvider(script=script), store_dir=None,
                            memory=memory,
                            lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("查指标任务")
        assert out["items"][0]["outcome"] == "accepted"
        assert out["items"][0]["submission"] == "最终答案：X=42"   # 用了补料后的输出
        # §5.7 记忆闭环：验收通过的内容晋升 durable memory
        kinds = [e.kind for e in cp.store.read_all()]
        assert "memory_candidate" in kinds and "memory_promoted" in kinds
        # 单套账本（阶段 4）：memory_* 只来自 MemoryService 的 sink，每种各两笔
        # （预置 1 + 验收提取 1），不再有 orchestrator 手工重录的第二套；
        # 字段以条目 payload 口径为准（此前手工重录缺 source_ids/artifact_hash）
        assert kinds.count("memory_candidate") == 2
        assert kinds.count("memory_promoted") == 2
        mem_events = [e for e in cp.store.read_all() if e.kind.startswith("memory_")]
        assert all("source_ids" in e.payload and "artifact_hash" in e.payload
                   for e in mem_events if e.kind == "memory_candidate")
        assert not hasattr(memory, "events")   # 私有双账本已删
        hits = memory.retrieve(scope="root", query="最终答案")
        assert any("X=42" in m.content for m in hits)

    def test_profile_sink_and_verdictless_exclusion(self, tmp_path):
        from dpswarm import profile
        cp = self._cp(tmp_path)
        store = profile.ProfileStore()
        script = [
            {"text": json.dumps({"action": "derive",
                                 "route": {"provider": "p", "model": "b-model"}})},
            {"text": "deliverable"},
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "ok"})},
        ]
        orch = Orchestrator(cp, MockProvider(script=script), store_dir=None,
                            profile=store,
                            lead_route=ModelRoute("p", "s-model", level=Level.S))
        orch.run_task("t")
        # §4 闭环 V1：只攒不用——accepted 样本已入画像
        assert store.bucket_stats("p/b-model", "overall")["attempts"] >= 1
        # 全账修正（§4）：topology/accepted_by.level/stop_reason 已填
        from dpswarm import observation
        recs = observation.ObservationSink(cp.store.read_all()).delegation_records()
        assert recs and recs[0].accepted_by and "level" in recs[0].accepted_by


class TestMechanismWiring:
    """核对后补的机制接线：split 链（§7/§9.5）/ 换模型升级（§8）/ 背压（§4）。"""

    def _cp(self, tmp_path):
        return ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                                  max_active_node_points=8),
                            store_path=tmp_path / "events.jsonl", catalog=catalog())

    def test_split_primary_assistant_peer_channel(self, tmp_path):
        cp = self._cp(tmp_path)
        script = [
            {"text": json.dumps({"action": "split",
                                 "route": {"provider": "p", "model": "b-model"}})},
            {"text": "协助者产出：后半部分"},          # 协助者
            {"text": "主执行者整合交付"},               # 主（首轮）
            {"text": json.dumps({"verdict": "accept"})},  # review
        ]
        orch = Orchestrator(cp, MockProvider(script=script), store_dir=None,
                            lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("可二分任务")
        assert out["items"] and out["items"][0]["outcome"] == "accepted"
        kinds = [e.kind for e in cp.store.read_all()]
        assert "peer_channel_opened" in kinds
        assert "message_queued" in kinds and "message_delivered" in kinds
        # 协助者不独立提交（§7）；通道随 accepted 关闭（§9.5）
        chan = next(e.payload for e in cp.store.read_all()
                    if e.kind == "peer_channel_opened")
        closed = [e.payload["channel_id"] for e in cp.store.read_all()
                  if e.kind == "peer_channel_closed"]
        assert chan["channel_id"] in closed

    def test_capability_reject_upgrades_model(self, tmp_path):
        """§8：capability 归因 → 升级重试（b→a），路由变更持久化对账（§2）。"""
        from dpswarm import observation, profile

        cp = self._cp(tmp_path)
        prof = profile.ProfileStore()
        script = [
            {"text": json.dumps({"action": "derive",
                                 "route": {"provider": "p", "model": "b-model"}})},
            {"text": "坏交付"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "能力不足",
                                 "attribution": "capability"})},
            {"text": "升级后好交付"},
            {"text": json.dumps({"verdict": "accept"})},
        ]
        orch = Orchestrator(cp, MockProvider(script=script), store_dir=None,
                            profile=prof,
                            lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("coding task")
        assert out["items"][0]["outcome"] == "accepted"
        events = cp.store.read_all()
        assert "lease_reweight" in [e.kind for e in events]      # 升级 reweight
        re_resolved = [e for e in events if e.kind == "route_resolved"]
        assert any(e.payload["resolved"]["model"] == "a-model" for e in re_resolved)
        # 换模型经 RESUME 复投（§9.3 唤起协议）持久化新路由
        resumes = [e for e in events if e.kind == "node_provisioning"
                   and e.payload.get("start_type") == "resume"]
        assert resumes and resumes[-1].payload["route"]["model"] == "a-model"
        # 0.10：打回样本进能力画像——记**当次**路由（升级前 b-model），带归因
        audit = prof.failure_audit()
        assert len(audit) == 1
        assert audit[0]["attribution"] == "capability"
        assert audit[0]["model"] == "p/b-model"
        assert audit[0]["outcome"] == "rejected"
        # 0.10：归因透传进观测全账（DelegationRecord，§4 免费失败归因数据）
        recs = observation.ObservationSink(events).delegation_records()
        assert recs and recs[0].attribution == "capability"

    def test_contradiction_escalates_without_retry(self, tmp_path):
        """§8：任务矛盾归因 → 不硬磕直接上交（无重试消耗）。"""
        cp = self._cp(tmp_path)
        script = [
            {"text": json.dumps({"action": "derive",
                                 "route": {"provider": "p", "model": "b-model"}})},
            {"text": "交付"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "任务自相矛盾",
                                 "attribution": "contradiction"})},
        ]
        orch = Orchestrator(cp, MockProvider(script=script), store_dir=None,
                            lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("矛盾任务")
        assert out["items"][0]["outcome"] == "escalated"
        kinds = [e.kind for e in cp.store.read_all()]
        assert "work_item_retried" not in kinds    # contradiction 不烧重试预算

    def test_rate_limit_backoff_and_quota_terminal(self, tmp_path):
        """§4：429 背压退避重试；QUOTA 明确终止不进画像。"""
        from dpswarm.providers.base import ProviderResult, QuotaExhausted, \
            RateLimitBackoff, Usage
        from dpswarm.types import StopReason

        class FlakyProvider:
            def __init__(self):
                self.calls = 0

            def complete(self, route, messages, **kw):
                self.calls += 1
                if self.calls == 1:
                    raise RateLimitBackoff(retry_after=0)
                if self.calls == 2:
                    return ProviderResult(text="ok-after-backoff",
                                          stop_reason=StopReason.COMPLETED, usage=Usage())
                raise QuotaExhausted(status=402)

        from dpswarm import profile
        cp = self._cp(tmp_path)
        store = profile.ProfileStore()
        orch = Orchestrator(cp, FlakyProvider(), store_dir=None, profile=store,
                            lead_route=ModelRoute("p", "s-model", level=Level.S))
        route = ModelRoute("p", "b-model", level=Level.B)
        # 0.14：record_stop_reason 校验节点存在（防幽灵节点入账）——用真实节点
        item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
        node = cp.begin_node(item.item_id, route)
        res = orch._complete_with_backoff(route, [], node.node_id)
        assert res.text == "ok-after-backoff"          # 429 退避后成功
        with pytest.raises(QuotaExhausted):
            orch._complete_with_backoff(route, [], node.node_id)  # QUOTA 上抛
        assert len(store.ops_audit()) == 1             # 进运维审计
        assert store.bucket_stats("p/b-model", "overall")["attempts"] == 0  # 不进画像
