"""控制面核心链路测试：§9 事务纪律 + §5.7 验收流 + §8 重试/上交 + §7 护栏。"""
from __future__ import annotations

import pytest

from dpswarm.control import AdmissionError, ControlPlane, ControlPlaneError
from dpswarm.invariants import InvariantViolation
from dpswarm.types import (
    DelegationKind,
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    NodeRole,
    RejectAttribution,
    RootExecutionSpec,
    RouteSource,
)


def make_catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    cat.register(ModelFacts("p", "a-model", Level.A, aa_dimensional={"coding": 8.5}))
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


def route_s() -> ModelRoute:
    return ModelRoute("p", "s-model", level=Level.S)


def route_b() -> ModelRoute:
    return ModelRoute("p", "b-model", level=Level.B)


@pytest.fixture()
def cp(tmp_path):
    plane = ControlPlane(spec=RootExecutionSpec(max_open_work_items=2,
                                                max_active_node_points=6,
                                                max_attempts=3),
                         store_path=tmp_path / "events.jsonl",
                         catalog=make_catalog())
    return plane


def make_item(cp, kind=DelegationKind.DERIVE):
    root = cp._root_item_id()
    return cp.create_work_item(kind, parent_item=root)


class TestBootstrap:
    def test_root_started_with_lead_first_admitted(self, cp):
        snap = cp.snapshot()
        assert snap["spec_revision"] == 1
        assert any(w["kind"] == "root" and w["depth"] == 1
                   for w in snap["work_items"].values())
        # root lead 最先准入（§7）：占点不占 worker 槽
        assert cp.proj.open_worker_slots_used == 0
        assert cp.proj.active_points == 1

    def test_recovery_from_disk_replays_state(self, tmp_path):
        spec = RootExecutionSpec()
        cp1 = ControlPlane(spec=spec, store_path=tmp_path / "e.jsonl", catalog=make_catalog())
        item = make_item(cp1)
        cp1.close()  # 单写者文件锁：复盘重建前先释放（P1-1）
        cp2 = ControlPlane(store_path=tmp_path / "e.jsonl", catalog=make_catalog())
        assert item.item_id in cp2.proj.work_items  # 事件是唯一真源（§9.1）


class TestHardAdmission:
    def test_depth_two_layers_blocks_grandchild(self, cp):
        item = make_item(cp)                       # 第 2 层（默认两层 = 底层）
        node = cp.begin_node(item.item_id, route_b())
        cp.confirm_node(node.node_id)
        # 第 2 层 worker 的 item 再派生 → 第 3 层，超默认两层：拒
        with pytest.raises(AdmissionError):
            cp.create_work_item(DelegationKind.DERIVE, parent_item=item.item_id)

    def test_worker_slot_overflow_rejected(self, cp):
        make_item(cp)
        make_item(cp)  # max_open_work_items=2：占满
        with pytest.raises(AdmissionError):
            make_item(cp)  # 第三个占槽 item 拒绝
        assert cp.proj.open_worker_slots_used == 2

    def test_level_direction_rejects_higher_worker(self, tmp_path):
        # 构造 A 级 lead 的 team：root lead 换成 A 级不可行（bootstrap 为 S），
        # 直接借 invariants 场景在 test_invariants 覆盖；此处验证 catalog 解析链。
        cat = make_catalog()
        plane = ControlPlane(spec=RootExecutionSpec(), store_path=tmp_path / "e.jsonl",
                             catalog=cat)
        item = make_item(plane)
        route = ModelRoute("p", "s-model", level=Level.B)  # 级别由 catalog 修正
        node = plane.begin_node(item.item_id, route)
        assert plane.proj.nodes[node.node_id].level == Level.S  # catalog 解析为准

    def test_unknown_model_rejected(self, cp):
        item = make_item(cp)
        with pytest.raises(AdmissionError):
            cp.begin_node(item.item_id, ModelRoute("nope", "ghost"))

    def test_fission_requires_s_lead(self, cp):
        # root lead 是 S（bootstrap），fission 允许；A/B lead 场景见 invariants 测试
        item = cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id())
        assert item.kind == DelegationKind.FISSION
        assert item.depth == 2

    def test_atomic_no_partial_events_on_failure(self, cp):
        before = cp.store.last_seq
        make_item(cp)
        make_item(cp)
        with pytest.raises(AdmissionError):
            make_item(cp)   # 槽满：整组事件不落盘
        assert cp.store.last_seq == before + 2  # 零半截落盘（§9.2 方法级原子）


class TestAcceptanceFlow:
    def test_submit_finalize_accept_full_chain(self, cp):
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b())
        cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id)
        cp.begin_finalize(item.item_id)
        cp.store_evidence_package(item.item_id, "pkg-1", "evidence content")
        ev = cp.complete_accept(item.item_id, package_id="pkg-1", evidence_ready=True)
        assert ev.kind == "work_item_accepted"
        # accepted 后：槽与点数全额归还（§4 五步）、后继可见
        assert cp.proj.work_items[item.item_id].acceptance.value == "accepted"
        assert cp.proj.open_worker_slots_used == 0
        assert cp.proj.active_points == 1  # 只剩 root lead
        assert cp.proj.item_ready(item.item_id)  # 自身 accepted

    def test_accept_requires_evidence_and_finalizing(self, cp):
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b())
        cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id)
        with pytest.raises(ControlPlaneError):
            cp.complete_accept(item.item_id, package_id="pkg")  # 未 finalizing 直接 accept

    def test_reject_retry_then_accept_and_budget(self, cp):
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b())
        cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id)
        cp.reject(item.item_id, "unit error", RejectAttribution.CAPABILITY)
        cp.prepare_retry(item.item_id)                     # attempt 2
        cp.submit(item.item_id, node.node_id)
        cp.reject(item.item_id, "again", RejectAttribution.DESCRIPTION)
        cp.prepare_retry(item.item_id)                     # attempt 3
        cp.submit(item.item_id, node.node_id)
        with pytest.raises(ControlPlaneError):             # 预算耗尽（§8：最多 3 attempt）
            cp.prepare_retry(item.item_id)
        cp.escalate(item.item_id, "budget exhausted")
        assert cp.proj.work_items[item.item_id].acceptance.value == "escalated"
        assert cp.proj.open_worker_slots_used == 0

    def test_dependency_unlock_only_after_accepted(self, cp):
        a = make_item(cp)
        b = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
        cp.add_dependency(a.item_id, b.item_id)
        assert not cp.proj.item_ready(b.item_id)
        na = cp.begin_node(a.item_id, route_b()); cp.confirm_node(na.node_id)
        cp.submit(a.item_id, na.node_id); cp.begin_finalize(a.item_id)
        cp.store_evidence_package(a.item_id, "pkg-a", "evidence-a")
        cp.complete_accept(a.item_id, package_id="pkg-a", evidence_ready=True)
        assert cp.proj.item_ready(b.item_id)

    def test_escalate_unsubmitted_item(self, cp):
        """§7：超时发生在执行中、item 无裁决——blocked 后须能上交：
        (None, ESCALATED) 是合法转换（§5.7/§8）。"""
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b())
        cp.confirm_node(node.node_id)
        cp.escalate(item.item_id, "wallclock-timeout: Lead 归因后上交")
        assert cp.proj.work_items[item.item_id].acceptance.value == "escalated"
        assert cp.proj.open_worker_slots_used == 0
        assert cp.proj.active_points == 1              # 点数归还，只剩 root lead

    def test_dag_cycle_rejected(self, cp):
        a = make_item(cp)
        b = make_item(cp)
        cp.add_dependency(a.item_id, b.item_id)
        with pytest.raises((ControlPlaneError, InvariantViolation)):
            cp.add_dependency(b.item_id, a.item_id)


class TestSplit:
    def test_split_assistant_shares_item_and_peer_channel(self, cp):
        item = make_item(cp)
        primary = cp.begin_node(item.item_id, route_b())
        cp.confirm_node(primary.node_id)
        n_items = len(cp.proj.work_items)
        assistant, chan = cp.split(primary.node_id, route_b())
        assert len(cp.proj.work_items) == n_items  # 分裂不新建 item（§7 同层拓宽）
        assert assistant.assistant_of == primary.node_id
        assert assistant.item_id == primary.item_id
        assert cp.proj.open_worker_slots_used == 1  # 协助者不另占槽（§7）
        mid = cp.peer_send(chan, primary.node_id, "你做左半")
        cp.peer_deliver(mid)
        # 协助者不得独立提交验收（§7）
        with pytest.raises((ControlPlaneError, InvariantViolation)):
            cp.submit(item.item_id, assistant.node_id)
        cp.degenerate_assistant(assistant.node_id)   # 退化收回：结论落盘、主收摘要
        assert cp.proj.active_points == 2            # root lead + primary

    def test_accept_drains_assistant_same_transaction(self, cp):
        item = make_item(cp)
        primary = cp.begin_node(item.item_id, route_b()); cp.confirm_node(primary.node_id)
        assistant, chan = cp.split(primary.node_id, route_b())
        cp.submit(item.item_id, primary.node_id)
        cp.begin_finalize(item.item_id)
        cp.store_evidence_package(item.item_id, "pkg", "evidence")
        cp.complete_accept(item.item_id, package_id="pkg", evidence_ready=True)
        # 主结案同事务清理协助者（§7：主 accepted 时协助者必须已关闭）
        assert cp.proj.nodes[assistant.node_id].terminated


class TestRollover:
    def test_rollover_keeps_lease_epoch_and_depth(self, cp):
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b()); cp.confirm_node(node.node_id)
        depth_before = cp.proj.nodes[node.node_id].delegation_depth
        lease_before = cp.proj.nodes[node.node_id].lease_id
        points_before = cp.proj.active_points
        cp.begin_rollover(node.node_id, "cap://1", "h1")
        n = cp.proj.nodes[node.node_id]
        assert n.context_epoch == 1 and n.start_type.value == "rollover"
        cp.confirm_rollover(node.node_id, session_id="sess-2")
        n = cp.proj.nodes[node.node_id]
        assert n.delegation_depth == depth_before      # 深度保持（§9.3）
        assert n.lease_id == lease_before              # 同 lease 跨窗口（§5.8）
        assert cp.proj.active_points == points_before  # 不新建资源 lease
        assert cp.proj.nodes[node.node_id].successor_reg is None  # activated-success 复位

    def test_double_successor_cas_rejected(self, cp):
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b()); cp.confirm_node(node.node_id)
        cp.begin_rollover(node.node_id, "cap://1", "h1")
        with pytest.raises(ControlPlaneError):
            cp.begin_rollover(node.node_id, "cap://2", "h2")  # CAS：最多一个 successor

    def test_rollover_reweight_on_model_change(self, cp):
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b()); cp.confirm_node(node.node_id)
        s_route = ModelRoute("p", "s-model", level=Level.S, point_weight=3)
        cp.begin_rollover(node.node_id, "cap://1", "h1", new_route=s_route)
        n = cp.proj.nodes[node.node_id]
        assert n.route.model == "s-model"
        assert cp.proj.active_points == 1 + 3  # root lead(1) + reweight 后的 3

    def test_rollover_rollback_restores_active_epoch_and_fence(self, cp):
        """§5.8 预装失败回退 = 现场复原（修复链 1.1）：事务内回退 context_epoch、
        session_id 恢复 predecessor、lifecycle 回 ACTIVE 并发 node_unblocked——
        否则旧 session 被 epoch fence 永久锁死、节点卡 PROVISIONING+BLOCKED 无出口。"""
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b()); cp.confirm_node(node.node_id)
        old_epoch = cp.proj.nodes[node.node_id].context_epoch
        old_session = cp.proj.nodes[node.node_id].session_id
        cp.begin_rollover(node.node_id, "cap://1", "h1")
        cp.rollback_rollover(node.node_id, "capsule gen failed")
        n = cp.proj.nodes[node.node_id]
        assert n.successor_reg is None             # package-fail-rollback 复位
        assert n.lifecycle.value == "active"       # lifecycle 复原（原卡 provisioning）
        assert n.context_epoch == old_epoch        # epoch 回退登记值
        assert n.session_id == old_session         # predecessor session 恢复
        assert n.blocked.value == "none"           # node_unblocked：复原完成即解除
        assert cp.proj.active_points == 2          # lease 未丢
        # rollback 后旧 fence 可 submit（修复前：epoch fence 锁死 + NODE_NOT_ACTIVE）
        cp.submit(item.item_id, node.node_id, "after-rollback",
                  context_epoch=old_epoch, session_id=old_session)
        # 失败归因有界记录：blocked→unblocked 对留审计痕（全库首个 node_unblocked 发射点）
        kinds = [e.kind for e in cp.store.read_all()]
        assert "node_blocked" in kinds and "node_unblocked" in kinds

    def test_confirm_node_rejected_when_blocked(self, cp):
        """修复链 1.2（须在 1.1 之后）：blocked 节点拒绝激活——rollback 已现场复原，
        confirm 不再是 rollback 后的事实通路；超时 blocked 节点须先走 Lead 归因处置。"""
        import time as _t
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b()); cp.confirm_node(node.node_id)
        cp.tick(now=_t.time() + 7201)              # 越过默认 wall-clock 7200s → blocked
        assert cp.proj.nodes[node.node_id].blocked.value == "blocked"
        cp.begin_rollover(node.node_id, "cap://1", "h1")   # provisioning 中仍 blocked
        with pytest.raises(ControlPlaneError) as ei:
            cp.confirm_node(node.node_id)
        assert ei.value.code == "NODE_BLOCKED"

    def test_successor_registration_requires_capsule(self, cp):
        """§5.8 登记原子提交契约（修复链 1.3）：CAS 登记必须携带 capsule 引用与 hash。"""
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b()); cp.confirm_node(node.node_id)
        n = cp.proj.nodes[node.node_id]
        for missing in ({}, {"capsule_ref": "cap://1"}):
            with pytest.raises(ControlPlaneError) as ei:
                cp._transact(("successor_registered", {
                    "node_id": node.node_id, "context_epoch": n.context_epoch,
                    "control_state_revision": cp.proj.graph_revision, **missing}))
            assert ei.value.code == "BAD_PAYLOAD"


class TestSealing:
    def test_seal_three_phases_and_admission_closed(self, cp):
        item = make_item(cp)
        # 在途工作先就位（§9.6：结算期 in-flight finalizing 允许完成 accepted）
        node = cp.begin_node(item.item_id, route_b()); cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id)
        cp.begin_finalize(item.item_id)
        cp.begin_seal("root")   # 准入截止：此后新 item / 新节点被拒
        with pytest.raises(ControlPlaneError):
            make_item(cp)
        with pytest.raises(ControlPlaneError):
            cp.begin_node(item.item_id, route_b())  # start_node 同时封死（§9.6）
        cp.begin_settlement("root")
        # 结算期完成 accepted（实验悬空 #2 的既定读法）
        cp.store_evidence_package(item.item_id, "pkg-s", "evidence")
        cp.complete_accept(item.item_id, package_id="pkg-s", evidence_ready=True)
        cp.finish_seal("root")
        assert cp.proj.seal_phase["root"].value == "completed"


class TestSpecRevision:
    def test_publish_spec_lower_capacity_no_kill(self, cp):
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route_b()); cp.confirm_node(node.node_id)
        new_spec = RootExecutionSpec(max_open_work_items=1, max_active_node_points=6)
        cp.publish_spec(new_spec)
        assert cp.proj.spec.revision == 2
        # 在途节点不强杀（§2.1）：仍 active；新准入被容量挡住
        assert cp.proj.nodes[node.node_id].lifecycle.value == "active"
        with pytest.raises(AdmissionError):
            make_item(cp)


class TestObservationHooks:
    def test_token_and_economics_events_persisted(self, cp):
        cp.record_token_usage(cp.root_lead_node, input_tokens=100, output_tokens=50,
                              cache_read_tokens=20)
        cp.record_economics(cp._root_item_id(), lead_tokens=150, estimated_savings=None)
        kinds = [e.kind for e in cp.store.read_all()]
        assert "token_usage_recorded" in kinds
        assert "delegation_economics_recorded" in kinds
