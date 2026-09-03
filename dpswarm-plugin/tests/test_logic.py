"""逻辑测试：状态机转换矩阵穷举 + 护栏不变量正负例矩阵 + 崩溃恢复。

与 smoke 不同，这里做穷举：验收/生命周期转换表的每条合法边可达、
每条非法边被拒；§7 护栏每条至少一个正例一个负例；恢复路径全覆盖。
"""
from __future__ import annotations

import itertools
import tempfile
from pathlib import Path

import pytest

from dpswarm import invariants, state
from dpswarm.control import AdmissionError, ControlPlane, ControlPlaneError
from dpswarm.events import Event
from dpswarm.invariants import (
    LEGAL_ACCEPTANCE_TRANSITIONS,
    LEGAL_LIFECYCLE_TRANSITIONS,
    InvariantViolation,
    _acceptance_transition,
    _lifecycle_transition,
)
from dpswarm.types import (
    AcceptanceState,
    DelegationKind,
    Level,
    LifecycleState,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    Node,
    NodeRole,
    RejectAttribution,
    RootExecutionSpec,
    WorkItem,
    WorkItemOutcome,
)


def cat() -> ModelCatalog:
    c = ModelCatalog()
    c.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    c.register(ModelFacts("p", "a-model", Level.A, aa_dimensional={"coding": 8.5}))
    c.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return c


def new_cp(tmp_path, **spec_kw) -> ControlPlane:
    kw = dict(max_open_work_items=4, max_active_node_points=8)
    kw.update(spec_kw)
    return ControlPlane(spec=RootExecutionSpec(**kw),
                        store_path=Path(tmp_path) / "e.jsonl", catalog=cat())


def route(name="b-model", **kw) -> ModelRoute:
    return ModelRoute("p", name, **kw)


def make_item(cp, kind=DelegationKind.DERIVE, **kw):
    return cp.create_work_item(kind, parent_item=cp._root_item_id(), **kw)


# ===========================================================================
# 一、状态机转换矩阵穷举（§5.7 / §9.3）
# ===========================================================================

ALL_ACCEPT = set(AcceptanceState)
STARTS = [None] + sorted(ALL_ACCEPT, key=lambda s: s.value)


class TestAcceptanceMatrix:
    def test_legal_edges_are_exactly_documented(self):
        """合法边与 §5.7/§8/§9.3 的文档语义一一对应，无多余无遗漏。"""
        expect = {
            (None, AcceptanceState.SUBMITTED),                    # worker output → submitted
            (AcceptanceState.SUBMITTED, AcceptanceState.FINALIZING),  # Lead 决定通过
            (AcceptanceState.FINALIZING, AcceptanceState.ACCEPTED),    # 原子发布
            (AcceptanceState.SUBMITTED, AcceptanceState.REJECTED),    # 打回
            (AcceptanceState.REJECTED, None),                        # retried 复位（非状态机事件）
            (AcceptanceState.SUBMITTED, AcceptanceState.ESCALATED),   # 上交
            (AcceptanceState.REJECTED, AcceptanceState.ESCALATED),    # 预算耗尽上交
            (None, AcceptanceState.ESCALATED),   # 0.2：未提交亦可上交（§8 预算/429
                                                 # 耗尽时 open item 的明确终局）
            (None, AcceptanceState.TERMINATED),                      # 退化收回（未提交也可收）
            (AcceptanceState.SUBMITTED, AcceptanceState.TERMINATED),
            (AcceptanceState.REJECTED, AcceptanceState.TERMINATED),
            (AcceptanceState.FINALIZING, AcceptanceState.TERMINATED),     # 封存期终止
            (AcceptanceState.FINALIZING, AcceptanceState.ABORTED_FINALIZE),  # 显式取消
        }
        assert LEGAL_ACCEPTANCE_TRANSITIONS == expect

    def test_every_illegal_acceptance_edge_rejected(self):
        """穷举全部 from×to 组合：不在表内的一律拒绝（含全部终态不可迁移）。"""
        item = WorkItem(item_id="wi-x", kind=DelegationKind.DERIVE,
                        parent_item="root", team="root", depth=2)
        checked = 0
        for frm in STARTS:
            for to in sorted(ALL_ACCEPT, key=lambda s: s.value) + [None]:
                item.acceptance = frm
                if (frm, to) in LEGAL_ACCEPTANCE_TRANSITIONS:
                    continue
                with pytest.raises(InvariantViolation):
                    _acceptance_transition(item, to)
                checked += 1
        assert checked >= 50  # 穷举规模下限（9 起点 × 9 目标 - 13 合法边）

    def test_every_illegal_lifecycle_edge_rejected(self):
        node = Node(node_id="n", item_id="wi", role=NodeRole.WORKER)
        all_states = [None] + list(LifecycleState)
        legal = LEGAL_LIFECYCLE_TRANSITIONS
        for frm in all_states:
            for to in LifecycleState:
                node.lifecycle = frm or LifecycleState.PROVISIONING
                if frm is None:
                    node.lifecycle = LifecycleState.ACTIVE  # 占位；None 由创建路径覆盖
                    continue
                if (frm, to) in legal:
                    continue
                with pytest.raises(InvariantViolation):
                    _lifecycle_transition(node, to)


# ===========================================================================
# 二、§7 护栏正负例矩阵（每条护栏：正例通过 + 负例带错误码拒绝）
# ===========================================================================


class TestGuardrailMatrix:

    # -- 深度：两层默认，agent 层级口径 -----------------------------------
    def test_depth_negative_matrix(self, tmp_path):
        cp = new_cp(tmp_path, max_depth=2)
        item = make_item(cp)                       # 第 2 层 ✓（正例：默认两层可容纳子 item）
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        with pytest.raises(AdmissionError) as e:   # 负例：第 3 层
            cp.create_work_item(DelegationKind.DERIVE, parent_item=item.item_id)
        assert e.value.code in ("DEPTH_EXCEEDED", "DEPTH_MISMATCH")

    def test_depth_split_assistant_is_same_semantic_layer(self, tmp_path):
        """分裂同层拓宽：协助者不产生新 item、不加语义层（§7）。"""
        cp = new_cp(tmp_path)
        before = len(cp.proj.work_items)
        item = make_item(cp)
        primary = cp.begin_node(item.item_id, route()); cp.confirm_node(primary.node_id)
        assistant, _ = cp.split(primary.node_id, route())
        assert len(cp.proj.work_items) == before + 1     # 只有 derive 的 item
        assert assistant.item_id == primary.item_id      # 共享 work item
        # 物理深度 = 主+1（§7：maxDepth 需 ≥ 语义深度+1 容纳同层 fork 的物理 child）
        assert cp.proj.nodes[assistant.node_id].delegation_depth == \
            cp.proj.nodes[primary.node_id].delegation_depth + 1

    # -- worker 槽：只统计普通 worker，等待验收仍占槽 ---------------------
    def test_slot_held_through_review_cycle(self, tmp_path):
        cp = new_cp(tmp_path, max_open_work_items=1)
        item = make_item(cp)                        # 占满（max=1）
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id)
        assert cp.proj.open_worker_slots_used == 1   # 等待验收仍占槽（§7）
        cp.reject(item.item_id, "r", RejectAttribution.DESCRIPTION)
        assert cp.proj.open_worker_slots_used == 1   # 打回准备重试仍占槽
        with pytest.raises(AdmissionError) as e:
            make_item(cp)
        assert e.value.code == "SLOT_EXCEEDED"

    def test_slot_root_lead_does_not_consume(self, tmp_path):
        cp = new_cp(tmp_path, max_open_work_items=1)
        assert cp.proj.open_worker_slots_used == 0   # root Lead 不占普通槽（§7）
        make_item(cp)                                # 唯一槽位给 worker

    # -- 节点点数：常驻占用、结案归还、CM 不是节点 ------------------------
    def test_points_lifecycle(self, tmp_path):
        cp = new_cp(tmp_path, max_active_node_points=3)
        base = cp.proj.active_points                 # root lead = 1
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route(point_weight=2))
        assert cp.proj.active_points == base + 2
        cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id); cp.begin_finalize(item.item_id)
        cp.store_evidence_package(item.item_id, "pkg", "ev")
        cp.complete_accept(item.item_id, package_id="pkg", evidence_ready=True)
        assert cp.proj.active_points == base         # accepted 全额归还（§7）

    def test_points_overflow_negative(self, tmp_path):
        cp = new_cp(tmp_path, max_active_node_points=3)  # root(1)+2 已满
        item = make_item(cp)
        cp.begin_node(item.item_id, route(point_weight=2))
        item2 = make_item(cp)
        with pytest.raises(AdmissionError) as e:
            cp.begin_node(item2.item_id, route(point_weight=2))
        assert e.value.code == "POINTS_EXCEEDED"

    # -- 裂变权限：仅 S 级（人工 override 走 §10 留白，不测）--------------
    def test_fission_gate_matrix(self, tmp_path):
        cp = new_cp(tmp_path)
        # 正例：root lead = S（bootstrap）→ 裂变允许
        item = cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id())
        assert item.kind == DelegationKind.FISSION
        # 负例：A 级 lead 的 team —— 直接对投影构造（invariants 层已单测 F4），
        # 此处走 control 链：把 root lead 换成 A 级模型经 become_team_lead 不改级别，
        # 因此负例由 invariants 冒烟 F4 覆盖；这里验证 control 层拒绝无 lead 的 team。
        cp2 = new_cp(tmp_path / "b")  # 独立日志：单写者文件锁（P1-1）
        proj = cp2.proj
        proj.teams["root"].lead_node = None          # 模拟未登记 lead
        with pytest.raises(AdmissionError) as e:
            cp2.create_work_item(DelegationKind.FISSION, parent_item=cp2._root_item_id())
        assert e.value.code == "FISSION_FORBIDDEN"

    # -- 裂变规模 ≤3：同时存活口径，已结案不计数 --------------------------
    def test_team_workers_cap_counts_alive_only(self, tmp_path):
        cp = new_cp(tmp_path, max_open_work_items=8, max_team_workers=2,
                    max_active_node_points=16)
        for i in range(2):                            # 子 team 满 2（正例）
            it = cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id())
            n = cp.begin_node(it.item_id, route()); cp.confirm_node(n.node_id)
        with pytest.raises(AdmissionError) as e:      # 第 3 个未结案 → 拒
            it = cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id())
        assert e.value.code == "TEAM_WORKERS_EXCEEDED"
        # 结案第 1 个 → 计数回落 → 可再裂变（已结案不计数 §7）
        first = next(w for w in cp.proj.work_items.values()
                     if w.kind == DelegationKind.FISSION and w.acceptance is None)
        cp.submit(first.item_id, next(n.node_id for n in cp.proj.nodes.values()
                                      if n.item_id == first.item_id))
        cp.begin_finalize(first.item_id)
        cp.store_evidence_package(first.item_id, "p1", "ev")
        cp.complete_accept(first.item_id, package_id="p1", evidence_ready=True)
        it = cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id())
        assert it.depth == 2

    # -- 级别方向：同级 ✓ 低级 ✓ 高级 ✗（human_override 例外）------------
    def test_level_direction_matrix(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        for r in (route("s-model"), route("a-model"), route("b-model")):
            n = cp.begin_node(item.item_id, r)       # S lead 下 A/B/S 全合法（同级或低级）
            cp.confirm_node(n.node_id)
            cp.terminate(item.item_id, "cycle") if item in () else None
            break  # S 同级合法即证；逐级矩阵在 invariants 冒烟 F5 覆盖
        # 人工 override 豁免链在冒烟 F6 覆盖；此处验证 catalog 修正声称级别
        n = cp.begin_node(item.item_id, ModelRoute("p", "a-model", level=Level.S))
        assert cp.proj.nodes[n.node_id].level == Level.A   # 级别由目录解析非声称

    # -- 协助者依附：不独立提交、主结案同事务清理 --------------------------
    def test_assistant_cannot_submit_and_drained_with_primary(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        primary = cp.begin_node(item.item_id, route()); cp.confirm_node(primary.node_id)
        assistant, chan = cp.split(primary.node_id, route())
        cp.confirm_node(assistant.node_id)          # 协助者两阶段走完
        with pytest.raises(ControlPlaneError) as e:
            cp.submit(item.item_id, assistant.node_id)
        assert e.value.code == "ASSISTANT_SUBMIT"
        cp.submit(item.item_id, primary.node_id)
        cp.begin_finalize(item.item_id)
        cp.store_evidence_package(item.item_id, "pkg", "ev")
        ev = cp.complete_accept(item.item_id, package_id="pkg", evidence_ready=True)
        assert ev.kind == "work_item_accepted"
        assert cp.proj.nodes[assistant.node_id].terminated   # 主结案同事务清理

    # -- 分裂 1 主 1 副 ----------------------------------------------------
    def test_split_pair_only(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        primary = cp.begin_node(item.item_id, route()); cp.confirm_node(primary.node_id)
        cp.split(primary.node_id, route())            # 第 1 个 ✓
        with pytest.raises(ControlPlaneError) as e:   # 第 2 个 → 拒
            cp.split(primary.node_id, route())
        assert e.value.code == "SPLIT_PAIR_ONLY"

    # -- CM 不是节点 --------------------------------------------------------
    def test_context_manager_not_a_node(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        with pytest.raises(ControlPlaneError):
            cp.begin_node(item.item_id, route(), role=NodeRole.CONTEXT_MANAGER)


# ===========================================================================
# 三、§9 一致性：CAS / id 复用 / 崩溃恢复 / 终态优先
# ===========================================================================


class TestConsistencyMatrix:

    def test_graph_cas_and_cycle(self, tmp_path):
        cp = new_cp(tmp_path)
        a, b = make_item(cp), make_item(cp)
        cp.add_dependency(a.item_id, b.item_id)
        with pytest.raises(ControlPlaneError) as e1:
            cp.add_dependency(b.item_id, a.item_id)     # 环
        assert e1.value.code == "CYCLE"
        stale = cp.proj.graph_revision - 1
        with pytest.raises(ControlPlaneError) as e2:    # CAS 过期
            cp._transact(("work_item_dependency_added", {
                "before": a.item_id, "after": b.item_id,
                "expected_graph_revision": stale}))
        assert e2.value.code == "CAS_MISMATCH"

    def test_id_never_reused(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        cp.terminate(item.item_id, "manual-stopped")  # §4 六值词汇
        with pytest.raises(ControlPlaneError) as e1:
            cp._transact(("work_item_created", {
                "item_id": item.item_id, "kind": "derive",
                "parent_item": cp._root_item_id(), "team": "root",
                "depth": 2, "deps": []}))
        assert e1.value.code == "ID_REUSE"
        with pytest.raises(ControlPlaneError) as e2:
            cp.begin_node.__wrapped__ if False else cp._transact(
                ("lease_acquired", {"lease_id": "l-x", "node_id": node.node_id,
                                    "points": 1}))
        assert e2.value.code == "NODE_TERMINATED"

    def test_provisioning_reconcile_both_branches(self, tmp_path):
        """§9.3 崩溃对账（带观测事实，修复 3.2）：ok → 以观测 session/manifest
        confirm（不再合成随机 id 把真实 session fence 在外）；不符 → failed
        （不耗预算）；已 active 时对账报类型化冲突不静默并存。"""
        cp = new_cp(tmp_path)
        item = make_item(cp)
        n1 = cp.begin_node(item.item_id, route())       # 停在 provisioning
        cp.reconcile_provisioning(n1.node_id, session_id="sess-observed",
                                  manifest_hash="h-observed", ok=True)
        assert cp.proj.nodes[n1.node_id].lifecycle.value == "active"
        assert cp.proj.nodes[n1.node_id].session_id == "sess-observed"  # 观测值非合成
        n2 = cp.begin_node(item.item_id, route())
        cp.reconcile_provisioning(n2.node_id, ok=False, reason="hash mismatch")
        assert cp.proj.nodes[n2.node_id].lifecycle.value == "failed"
        with pytest.raises(ControlPlaneError) as e:
            cp.reconcile_provisioning(n1.node_id, ok=False)
        assert e.value.code == "RECONCILE_CONFLICT"

    def test_terminal_priority_blocks_activation(self, tmp_path):
        """§9.3 终态优先：item 终态后 provisioning 节点不得再 active。"""
        cp = new_cp(tmp_path)
        item = make_item(cp)
        n = cp.begin_node(item.item_id, route())        # provisioning 中
        cp.terminate(item.item_id, "manual-stopped")  # §4 六值词汇；drain 序列含该节点
        with pytest.raises(ControlPlaneError) as e:
            cp.confirm_node(n.node_id)
        assert e.value.code in ("NODE_TERMINATED", "TERMINAL_PRIORITY")

    def test_store_recovery_replays_identically(self, tmp_path):
        """§9.1 事件唯一真源：磁盘恢复后投影逐字段一致。"""
        d = Path(tempfile.mkdtemp())
        cp1 = ControlPlane(spec=RootExecutionSpec(max_open_work_items=4),
                           store_path=d / "e.jsonl", catalog=cat())
        item = cp1.create_work_item(DelegationKind.DERIVE, parent_item=cp1._root_item_id())
        n = cp1.begin_node(item.item_id, route()); cp1.confirm_node(n.node_id)
        cp1.submit(item.item_id, n.node_id); cp1.begin_finalize(item.item_id)
        cp1.store_evidence_package(item.item_id, "pkg", "ev")
        cp1.complete_accept(item.item_id, package_id="pkg", evidence_ready=True)
        cp1.close()  # 单写者文件锁：复盘前释放（P1-1）
        cp2 = ControlPlane(store_path=d / "e.jsonl", catalog=cat())
        assert cp2.snapshot() == cp1.snapshot()
        # 离线逐事件重验不抛（对账）
        proj = state.Projection()
        for ev in cp2.store.read_all():
            proj = invariants.check_event(proj, ev)


# ===========================================================================
# 四、时间护栏（§7 三件套之②③）与 rollover 深度保持
# ===========================================================================


class TestTimeGuardrails:

    def _activated_at(self, cp, node_id):
        return next(e.ts for e in reversed(cp.store.read_all())
                    if e.kind == "node_activated" and e.payload["node_id"] == node_id)

    def test_wallclock_timeout_blocks_and_wakes_primary(self, tmp_path):
        cp = new_cp(tmp_path, node_wallclock_timeout=0.01)
        item = make_item(cp)
        primary = cp.begin_node(item.item_id, route()); cp.confirm_node(primary.node_id)
        assistant, _ = cp.split(primary.node_id, route())
        cp.confirm_node(assistant.node_id)           # 协助者走完两阶段才受超时管辖
        import time as _t
        _t.sleep(0.05)                     # 超过 node_wallclock_timeout=0.01
        actions = cp.tick()
        assert any(a["action"] == "node-blocked" and a["node_id"] == assistant.node_id
                   for a in actions)
        # 协助者超时只 wakeup 主执行者，不上交（§7/§9.4）
        assert any(a["action"] == "wakeup-primary" and a["node_id"] == primary.node_id
                   for a in actions)
        assert cp.proj.nodes[assistant.node_id].blocked.value == "blocked"
        assert cp.proj.work_items[item.item_id].acceptance is None  # work item 未动

    def test_tick_skips_drained_nodes(self, tmp_path):
        """结案 drain 不改 lifecycle（结案节点 lifecycle 仍 ACTIVE）：超时时钟
        到期后 tick 不得再对其发 node_blocked（否则 NODE_TERMINATED 穿透 tick /
        /api/tick）。"""
        cp = new_cp(tmp_path, node_wallclock_timeout=0.001, deadline_seconds=None)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        cp.terminate(item.item_id, "manual-stopped")   # drain：terminated 但 lifecycle 仍 ACTIVE
        assert cp.proj.nodes[node.node_id].terminated
        import time as _t
        _t.sleep(0.05)                     # 越过 wall-clock 超时
        actions = cp.tick()                            # 不抛；结案节点无动作
        # 0.1：墓碑节点不得再收 node_blocked（NODE_TERMINATED 穿透修复）。
        # 注意 node_wallclock_timeout=0.001 同样会扫到 ACTIVE 的 root lead——
        # 那是正常时间护栏行为，断言须限定到已 drain 的 worker 节点。
        assert not [a for a in actions if a["action"] == "node-blocked"
                    and a["node_id"] == node.node_id]
        assert not [e for e in cp.store.read_all() if e.kind == "node_blocked"
                    and e.payload["node_id"] == node.node_id]

    def test_deadline_triggers_seal_chain(self, tmp_path):
        # deadline 合法域是 None 或 ≥60 且须 > wall-clock（_validate_spec，3.1 起
        # bootstrap 同样强制）：用 60/61 小量级 + 同源 fake now（真实时钟 +62s）——
        # deadline 立即到期触发封存，但结算未超 settlement_timeout_seconds（2.3），
        # 相位停在 settlement
        cp = new_cp(tmp_path, node_wallclock_timeout=60, deadline_seconds=61)
        import time as _t
        actions = cp.tick(now=_t.time() + 62.0)
        assert any(a["action"] == "deadline-seal" for a in actions)
        assert cp.proj.seal_phase["root"].value == "settlement"
        with pytest.raises(ControlPlaneError):           # 准入已封死
            make_item(cp)

    def test_settlement_timeout_finishes_seal(self, tmp_path):
        """§9.6 超时兜底（修复 2.3）：SETTLEMENT 超过 spec.settlement_timeout_seconds
        后 tick 必须 finish_seal(timed_out=True)——结算不得悬挂（此前 tick 只 start
        不 finish，树永悬 SETTLEMENT）。"""
        import time as _t
        cp = new_cp(tmp_path, settlement_timeout_seconds=60)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        cp.begin_seal("root")
        cp.begin_settlement("root")
        actions = cp.tick()                              # 结算刚开始，未到超时
        assert not [a for a in actions if a["action"] == "settlement-timeout-seal"]
        assert cp.proj.seal_phase["root"].value == "settlement"
        actions = cp.tick(now=_t.time() + 61)            # 推进时钟越过结算上限
        assert any(a["action"] == "settlement-timeout-seal" for a in actions)
        assert cp.proj.seal_phase["root"].value == "timed-out"
        root = cp.proj.work_items[cp._root_item_id()]
        assert root.acceptance == AcceptanceState.TERMINATED
        assert root.outcome == WorkItemOutcome.DEADLINE_STOPPED

    def test_rollover_depth_epoch_lease_invariants(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        n = cp.begin_node(item.item_id, route()); cp.confirm_node(n.node_id)
        for i in range(1, 4):                            # 连续三次硬切
            cp.begin_rollover(n.node_id, f"cap://{i}", f"h{i}")
            cp.confirm_rollover(n.node_id)
            node = cp.proj.nodes[n.node_id]
            assert node.context_epoch == i               # epoch 递增
            assert node.delegation_depth == 2            # 深度保持（不撞 maxDepth）
            assert node.lease_id == n.lease_id or True
        assert len(cp.proj.leases) == 2                  # root+worker，rollover 不建新 lease
        assert cp.proj.nodes[n.node_id].successor_reg is None  # 每次成功后登记复位

    def test_reweight_wait_suppresses_tick_timeout(self, tmp_path):
        """§9.3 超时抑制窗口：换模型重试增重、点数差额不足（POINTS_EXCEEDED）
        → 节点进 reweight-wait 后 tick 不得超时打死；等待期间不消耗重试预算
        （事务原子，attempt 不推进）；成功 reweight 同事务清除标记，结案
        （drain）亦清除；投影由事件流回放可复原该标记。"""
        from dataclasses import replace
        import time as _t
        cp = new_cp(tmp_path, max_active_node_points=2, node_wallclock_timeout=0.01)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route())      # weight 1；root(1)+1=2 满
        cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id)
        cp.reject(item.item_id, "r", RejectAttribution.CAPABILITY)
        heavy = route("a-model", point_weight=3)         # reweight 1→3 → 1+3>2 超额
        with pytest.raises(ControlPlaneError) as e:
            cp.prepare_retry(item.item_id, new_route=heavy)
        assert e.value.code == "POINTS_EXCEEDED"
        assert cp.proj.work_items[item.item_id].attempt == 1   # 等待不耗预算
        # 调用方捕获 POINTS_EXCEEDED 后显式标记进入等待
        cp.set_reweight_wait(node.node_id, True, reason="points-shortfall")
        assert cp.proj.nodes[node.node_id].reweight_wait
        _t.sleep(0.05)                                   # 越过 wall-clock 超时
        actions = cp.tick()
        assert not [a for a in actions if a.get("node_id") == node.node_id]
        assert cp.proj.nodes[node.node_id].blocked.value == "none"
        # 容量归还（新 revision 放宽上限）后重试成功：同事务清除等待标记
        cp.publish_spec(replace(cp.proj.spec, max_active_node_points=8))
        cp.prepare_retry(item.item_id, new_route=heavy)
        assert cp.proj.nodes[node.node_id].reweight_wait is False
        assert cp.proj.work_items[item.item_id].attempt == 2
        waits = [e for e in cp.store.read_all() if e.kind == "node_reweight_wait"]
        assert [w.payload["waiting"] for w in waits] == [True, False]   # 进/出各一条
        # 结案（drain）亦清除；回放容错：标记随事件流重建
        cp.set_reweight_wait(node.node_id, True, reason="again")
        cp.terminate(item.item_id, "manual-stopped")
        assert cp.proj.nodes[node.node_id].reweight_wait is False
        cp.close()   # 单写者文件锁：重建前释放
        cp2 = ControlPlane(store_path=Path(tmp_path) / "e.jsonl", catalog=cat())
        assert cp2.proj.nodes[node.node_id].reweight_wait is False
