"""控制面核心缺陷修复测试（机制文档↔代码审查六项修复的回归锚点）。

对应《DPswarm-机制架构.md》：
- §4        解锁后继：DAG 依赖从"查询"升为准入硬门禁（新建节点专用分支）
- §7 时间护栏① 超时重试计预算 + 释放滞留 lease（retry_timeout 单事务）
- §7 lease 保守持有：fail_node 收紧为 PROVISIONING 专用；mark_crashed 不放 lease
- §8        重试预算口径：超时重试与打回重试共用 max_attempts
- §9.1      断点续跑：新事件 work_item_timeout_retried 逐事件重验通过
- §2.1/§7   默认 deadline 4h > 单节点 wall-clock 2h（两道时间闸量级关系）

机制注释风格与 test_scenarios.py 一致：每条断言标 § 条文来源。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from dpswarm import invariants, state
from dpswarm.control import AdmissionError, ControlPlane, ControlPlaneError
from dpswarm.events import Event
from dpswarm.types import (
    AcceptanceState,
    BlockState,
    DelegationKind,
    Level,
    LifecycleState,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RejectAttribution,
    RootExecutionSpec,
    WorkItemOutcome,
)


def catalog() -> ModelCatalog:
    c = ModelCatalog()
    c.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    c.register(ModelFacts("p", "a-model", Level.A, aa_dimensional={"coding": 8.5}))
    c.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return c


def new_cp(tmp_path, **kw) -> ControlPlane:
    spec_kw = dict(max_open_work_items=4, max_active_node_points=8)
    spec_kw.update(kw)
    return ControlPlane(spec=RootExecutionSpec(**spec_kw),
                        store_path=Path(tmp_path) / "e.jsonl", catalog=catalog())


def route(name="b-model", **kw) -> ModelRoute:
    return ModelRoute("p", name, **kw)


def make_item(cp, kind=DelegationKind.DERIVE, **kw):
    return cp.create_work_item(kind, parent_item=cp._root_item_id(), **kw)


def run_and_accept(cp, item, pkg="pkg", text="evidence"):
    """准入 → 两阶段 → 提交 → 验收的最短链（依赖解锁用）。"""
    node = cp.begin_node(item.item_id, route())
    cp.confirm_node(node.node_id)
    cp.submit(item.item_id, node.node_id)
    cp.begin_finalize(item.item_id)
    cp.store_evidence_package(item.item_id, pkg, text)
    cp.complete_accept(item.item_id, package_id=pkg, evidence_ready=True)
    return node


# ===========================================================================
# 一、DAG 依赖准入硬门禁（§4：accepted 才解锁后继）
# ===========================================================================


class TestDepsGate:
    def test_begin_node_blocked_until_deps_accepted(self, tmp_path):
        cp = new_cp(tmp_path)
        a = make_item(cp)
        b = cp.create_work_item(DelegationKind.DERIVE,
                                parent_item=cp._root_item_id(), deps=[a.item_id])
        # a 未验收：后继 item 的节点启动被硬门禁拒绝（§4；root item deps 空不受影响）
        with pytest.raises(AdmissionError) as e:
            cp.begin_node(b.item_id, route())
        assert e.value.code == "DEPS_NOT_READY"
        # invariant 层同口径：绕过 control 门禁的裸事件也被拒（append 前校验 §9.1）
        cp._transact(("lease_acquired", {"lease_id": "lease-x", "node_id": "node-x",
                                         "points": 1}))
        with pytest.raises(ControlPlaneError) as e2:
            cp._transact(("node_provisioning", {
                "node_id": "node-x", "item_id": b.item_id, "role": "worker",
                "start_type": "new", "lease_id": "lease-x",
                "delegation_depth": 2, "team": "root"}))
        assert e2.value.code == "DEPS_NOT_READY"
        # rejected ≠ 解锁：a 打回后 b 仍不可启动
        na = cp.begin_node(a.item_id, route()); cp.confirm_node(na.node_id)
        cp.submit(a.item_id, na.node_id)
        cp.reject(a.item_id, "返工", RejectAttribution.DESCRIPTION)
        with pytest.raises(AdmissionError) as e3:
            cp.begin_node(b.item_id, route())
        assert e3.value.code == "DEPS_NOT_READY"
        # a 归因重试（REJECTED→None）→ 重新提交并 accepted → 后继解锁（§4）
        cp.prepare_retry(a.item_id)
        cp.submit(a.item_id, na.node_id)
        cp.begin_finalize(a.item_id)
        cp.store_evidence_package(a.item_id, "pkg-a", "上游产物")
        cp.complete_accept(a.item_id, package_id="pkg-a", evidence_ready=True)
        node = cp.begin_node(b.item_id, route())
        assert cp.proj.nodes[node.node_id].lifecycle == LifecycleState.PROVISIONING

    def test_rollover_exempt_from_deps_gate(self, tmp_path):
        """rollover/resume 不查依赖（§4 门禁只管新建）：依赖在首启时已判定，
        accepted 终态不可逆，续接窗口不被已满足过的依赖卡住。"""
        cp = new_cp(tmp_path)
        a = make_item(cp)
        b = make_item(cp)                                # 先启动（此时无依赖边）
        node = cp.begin_node(b.item_id, route()); cp.confirm_node(node.node_id)
        cp.add_dependency(a.item_id, b.item_id)          # 执行途中补依赖边（§7 拓扑全程可做）
        assert not cp.proj.item_ready(b.item_id)         # a 未验收 → b 现在不可"新建启动"
        with pytest.raises(AdmissionError) as e:
            cp.begin_node(b.item_id, route())            # 新建分支仍被门禁拦
        assert e.value.code == "DEPS_NOT_READY"
        cp.begin_rollover(node.node_id, "cap://1", "h1")  # 既有节点 rollover 不受影响
        cp.confirm_rollover(node.node_id)
        assert cp.proj.nodes[node.node_id].context_epoch == 1


# ===========================================================================
# 二、超时重试计预算 + 释放滞留 lease（§7 时间护栏①）
# ===========================================================================


class TestTimeoutRetry:
    def _timeout_once(self, cp, item):
        """启动 → active → 越过 wall-clock 超时 → tick 转 blocked（§7 确定性动作）。"""
        node = cp.begin_node(item.item_id, route())
        cp.confirm_node(node.node_id)
        time.sleep(0.05)                     # > node_wallclock_timeout=0.001
        actions = cp.tick()
        assert any(a["action"] == "node-blocked" and a["node_id"] == node.node_id
                   for a in actions)
        return node

    def test_retry_timeout_counts_budget_and_releases_stranded_lease(self, tmp_path):
        cp = new_cp(tmp_path, node_wallclock_timeout=0.001,
                    deadline_seconds=None, max_attempts=3)
        item = make_item(cp)
        node = self._timeout_once(cp, item)
        n = cp.proj.nodes[node.node_id]
        assert n.blocked == BlockState.BLOCKED
        assert n.blocked_reason == "wallclock-timeout"     # 阻塞原因已记账
        assert cp.proj.active_points == 2                  # root lead + 滞留 lease
        it = cp.retry_timeout(item.item_id)
        assert it.attempt == 2                             # 计预算（§7 重试计预算）
        assert it.acceptance is None                       # 超时发生在执行中，无裁决
        assert cp.proj.nodes[node.node_id].terminated      # blocked 节点 drained
        assert cp.proj.leases[node.lease_id].active is False
        assert cp.proj.active_points == 1                  # 滞留 lease 已释放
        # 重试启动新节点不双占点（此前旧 lease 永久滞留会造成同 item 双 lease）
        n2 = cp.begin_node(item.item_id, route()); cp.confirm_node(n2.node_id)
        assert cp.proj.active_points == 2

    def test_retry_timeout_budget_exhausted_then_terminate(self, tmp_path):
        cp = new_cp(tmp_path, node_wallclock_timeout=0.001,
                    deadline_seconds=None, max_attempts=2)
        item = make_item(cp)
        self._timeout_once(cp, item)
        cp.retry_timeout(item.item_id)                     # attempt 2 = max（§8 预算）
        self._timeout_once(cp, item)                       # 第二次超时
        with pytest.raises(ControlPlaneError) as e:
            cp.retry_timeout(item.item_id)
        assert e.value.code == "ATTEMPT_EXHAUSTED"
        # 预算耗尽不硬磕：终止结案，滞留 lease 归还（§4 六值词汇：manual-stopped）
        cp.terminate(item.item_id, "manual-stopped")
        assert cp.proj.active_points == 1
        assert cp.proj.work_items[item.item_id].acceptance == AcceptanceState.TERMINATED

    def test_timeout_retry_invariant_negatives(self, tmp_path):
        """凭空刷 attempt 被拒：无超时 blocked 节点 / attempt 不连续。"""
        cp = new_cp(tmp_path, node_wallclock_timeout=0.001,
                    deadline_seconds=None, max_attempts=3)
        item = make_item(cp)
        with pytest.raises(ControlPlaneError) as e1:
            cp._transact(("work_item_timeout_retried",
                          {"item_id": item.item_id, "attempt": 2}))
        assert e1.value.code == "NO_TIMEOUT_BLOCKED"
        self._timeout_once(cp, item)
        with pytest.raises(ControlPlaneError) as e2:
            cp._transact(("work_item_timeout_retried",
                          {"item_id": item.item_id, "attempt": 3}))   # 应为 2
        assert e2.value.code == "ATTEMPT_MISMATCH"


# ===========================================================================
# 三、fail_node 收紧 + mark_crashed（§7：去向未定前 lease 继续占用）
# ===========================================================================


class TestFailNodeAndCrash:
    def test_fail_node_only_for_provisioning(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        # ACTIVE 节点 fail 立即放 lease 违反"去向未定前 lease 继续占用"（§7）→ 拒
        with pytest.raises(ControlPlaneError) as e:
            cp.fail_node(node.node_id, "api lost")
        assert e.value.code == "NOT_PROVISIONING"
        # PROVISIONING 语义不变：启动事务失败不耗预算 + 配对释放（§8/§9.3）
        n2 = cp.begin_node(item.item_id, route())
        cp.fail_node(n2.node_id, "spawn failed")
        assert cp.proj.nodes[n2.node_id].lifecycle == LifecycleState.FAILED
        assert cp.proj.leases[n2.lease_id].active is False

    def test_mark_crashed_keeps_lease_and_wakes_primary(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        primary = cp.begin_node(item.item_id, route()); cp.confirm_node(primary.node_id)
        assistant, _chan = cp.split(primary.node_id, route())
        cp.confirm_node(assistant.node_id)
        points_before = cp.proj.active_points
        cp.mark_crashed(assistant.node_id, "session lost")
        n = cp.proj.nodes[assistant.node_id]
        assert n.lifecycle == LifecycleState.FAILED and n.terminated   # ACTIVE→FAILED
        assert cp.proj.leases[n.lease_id].active is True   # §7 崩溃后去向未定，lease 保守持有
        assert cp.proj.active_points == points_before
        # §9.4 协助者崩溃 → 控制面 wakeup 主执行者（通知不带状态，只报"有变化"）
        wakes = [ev for ev in cp.store.read_all()
                 if ev.kind == "node_wakeup" and ev.payload["node_id"] == primary.node_id]
        assert wakes and wakes[-1].payload["about"] == "assistant-crashed"
        # mark_crashed 只对 ACTIVE：PROVISIONING 崩溃走 fail_node（§9.3 启动协议）
        n3 = cp.begin_node(item.item_id, route())
        with pytest.raises(ControlPlaneError) as e:
            cp.mark_crashed(n3.node_id, "still starting")
        assert e.value.code == "NOT_ACTIVE"


# ===========================================================================
# 四、crashed lease 的结案清理（§4/§7：结案释放 item 名下全部 active lease）
# ===========================================================================


class TestCrashedLeaseCleanup:
    def test_terminate_after_crash_releases_stranded_lease(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        cp.mark_crashed(node.node_id, "provider outage")
        assert cp.proj.active_points == 2                  # crashed lease 滞留（§7 保守持有）
        cp.terminate(item.item_id, "manual-stopped")  # §4 六值词汇（崩溃放弃重试）
        assert cp.proj.active_points == 1                  # 滞留 lease 随结案归还
        # 全量校验：磁盘重放投影与内存一致（半截态会在 replay 暴露，§9.1）
        cp.close()  # 单写者文件锁：复盘前释放（P1-1）
        cp2 = ControlPlane(store_path=cp.store.path, catalog=catalog())
        assert cp2.snapshot() == cp.snapshot()

    def test_accept_after_assistant_crash_releases_stranded_lease(self, tmp_path):
        """主执行者收拢单干：complete_accept 同事务清掉 crashed 协助者滞留的 lease
        （此前清理循环只扫"未终止 PROV/ACTIVE"节点，crashed lease 会漏掉并在
        结案 post 校验 LEASE_NOT_RELEASED 爆炸）。"""
        cp = new_cp(tmp_path)
        item = make_item(cp)
        primary = cp.begin_node(item.item_id, route()); cp.confirm_node(primary.node_id)
        assistant, _chan = cp.split(primary.node_id, route())
        cp.confirm_node(assistant.node_id)
        cp.mark_crashed(assistant.node_id, "crashed mid-flight")
        cp.submit(item.item_id, primary.node_id)
        cp.begin_finalize(item.item_id)
        cp.store_evidence_package(item.item_id, "pkg-c", "主单干完成")
        ev = cp.complete_accept(item.item_id, package_id="pkg-c", evidence_ready=True)
        assert ev.kind == "work_item_accepted"
        assert cp.proj.active_points == 1                  # root lead 之外全归还

    def test_retire_item_nodes_releases_all_active_leases(self, tmp_path):
        """再执行前的旧节点退役：drain 非终态节点 + 释放 item 名下全部 active lease
        （含 crashed 滞留），随后可无残留地重新 begin_node。"""
        cp = new_cp(tmp_path)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        cp.mark_crashed(node.node_id, "provider outage")
        cp.retire_item_nodes(item.item_id)
        assert cp.proj.nodes[node.node_id].terminated
        assert cp.proj.leases[node.lease_id].active is False
        assert cp.proj.active_points == 1
        n2 = cp.begin_node(item.item_id, route()); cp.confirm_node(n2.node_id)
        assert cp.proj.active_points == 2                  # 干净重启，无双占点


# ===========================================================================
# 五、断点续跑一致性（§9.1：事件先于状态，逐事件重验）
# ===========================================================================


class TestReplayConsistency:
    def test_timeout_retry_stream_survives_per_event_revalidation(self, tmp_path):
        """含 work_item_timeout_retried 的完整事件流：cmd_replay 式逐事件重验
        通过 + 磁盘恢复投影一致（新事件不破坏断点续跑对账）。"""
        cp = new_cp(tmp_path, node_wallclock_timeout=0.001,
                    deadline_seconds=None, max_attempts=3)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        time.sleep(0.05)
        cp.tick()                                          # 超时转 blocked
        cp.retry_timeout(item.item_id)                     # attempt 2 + 释放滞留 lease
        n2 = cp.begin_node(item.item_id, route()); cp.confirm_node(n2.node_id)
        cp.submit(item.item_id, n2.node_id)
        cp.begin_finalize(item.item_id)
        cp.store_evidence_package(item.item_id, "pkg-r", "重试后完成")
        cp.complete_accept(item.item_id, package_id="pkg-r", evidence_ready=True)
        events = cp.store.read_all()
        assert any(e.kind == "work_item_timeout_retried" for e in events)
        # 逐事件重验（append 前校验同款逻辑跑全量日志，§9.1/§9.2 全图重验）
        proj = state.Projection()
        for ev in events:
            proj = invariants.check_event(proj, ev)
        assert proj.work_items[item.item_id].attempt == 2
        # 磁盘恢复：重放投影与内存快照逐字段一致
        cp.close()  # 单写者文件锁：复盘前释放（P1-1）
        cp2 = ControlPlane(store_path=cp.store.path, catalog=catalog())
        assert cp2.snapshot() == cp.snapshot()
        assert cp2.proj.work_items[item.item_id].acceptance == AcceptanceState.ACCEPTED


# ===========================================================================
# 六、默认值张力（§2.1/§7：两道时间闸量级关系）
# ===========================================================================


def test_default_deadline_outlives_node_wallclock():
    """树级 deadline（4h）必须晚于单节点 wall-clock 超时（2h）：单节点超时先兜
    "节点无声卡死"（转 blocked 走 Lead 归因），树级 deadline 才兜"整棵树不收敛"
    （§9.6 封存三段式）——顺序颠倒会让第一道闸形同虚设。"""
    spec = RootExecutionSpec()
    assert spec.node_wallclock_timeout == 7200.0
    assert spec.deadline_seconds == 14400.0
    assert spec.deadline_seconds > spec.node_wallclock_timeout


# ===========================================================================
# 七、terminate 原因六值词汇（§4：落盘前 invariant 校验，回放保持容错）
# ===========================================================================


class TestTerminateReasonVocabulary:
    def test_terminate_default_reason_is_manual_stopped(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        cp.terminate(item.item_id)                       # 缺省 reason
        assert (cp.proj.work_items[item.item_id].outcome
                == WorkItemOutcome.MANUAL_STOPPED)

    def test_finish_seal_timed_out_reason_is_deadline_stopped(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        node = cp.begin_node(item.item_id, route()); cp.confirm_node(node.node_id)
        cp.begin_seal("root"); cp.begin_settlement("root")
        cp.finish_seal("root", timed_out=True)
        root = cp.proj.work_items[cp._root_item_id()]
        assert root.acceptance == AcceptanceState.TERMINATED
        assert root.outcome == WorkItemOutcome.DEADLINE_STOPPED  # 不再 seal-timed-out

    def test_unknown_reason_rejected_pre_write_but_replay_tolerant(self, tmp_path):
        cp = new_cp(tmp_path)
        item = make_item(cp)
        with pytest.raises(ControlPlaneError) as e:
            cp.terminate(item.item_id, "degenerate")     # 非六值词汇 → 落盘前拒
        assert e.value.code == "BAD_PAYLOAD"
        assert cp.proj.work_items[item.item_id].acceptance is None  # 未落盘
        # 回放容错（§9.1）：旧日志里的越词 reason 不崩 replay，仅 outcome 无法归类
        forged = Event(seq=999, kind="work_item_terminated",
                       payload={"item_id": item.item_id, "reason": "degenerate"})
        proj = state.replay(cp.store.read_all() + [forged])
        assert proj.work_items[item.item_id].acceptance == AcceptanceState.TERMINATED
        assert proj.work_items[item.item_id].outcome is None


# ===========================================================================
# 八、裂变建队与 lead 登记原子化（§7：同事务，失败不留半截 team）
# ===========================================================================


class TestFissionLeadAtomicity:
    def test_fission_on_terminated_lead_rejected_without_partial_writes(self, tmp_path):
        """lead 已终止时裂变整体拒绝：node_role_changed 的不变量（NODE_TERMINATED）
        在同一事务内拦下 work_item_created——teams/work_items/事件序全部不变
        （修复前 lead 登记在独立的 try/except pass 里被吞，留下 lead_node=None 的
        半截 team，后续 fission 永卡 FISSION_FORBIDDEN）。"""
        cp = new_cp(tmp_path)
        cp.proj.nodes[cp.root_lead_node].terminated = True  # 直接改投影（test_logic 先例）
        teams_before = set(cp.proj.teams)
        items_before = set(cp.proj.work_items)
        seq_before = cp.store.last_seq
        with pytest.raises(AdmissionError) as e:
            cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id())
        assert e.value.code == "NODE_TERMINATED"
        assert set(cp.proj.teams) == teams_before
        assert set(cp.proj.work_items) == items_before
        assert cp.store.last_seq == seq_before
