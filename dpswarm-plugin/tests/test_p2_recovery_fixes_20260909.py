"""P2 持久化/恢复缺陷修复回归（2026-09-09，测试先行）。

三个已确诊缺陷的钉死测试（实现前红、实现后绿）：
- P2-2 证据文件持久化次序倒置：write_artifact 的 artifacts/<sha>.txt 必须
  fsync 落盘，且完成于引用它的事件事务（package_stored/work_item_submitted
  envelope）fsync 之前——否则崩溃后事件先于证据可读，review accept 时
  EVIDENCE_NOT_READABLE。
- P2-1 seal 断点不可续：begin_seal 与 begin_settlement 是两个独立事务，
  进程在间隙崩溃后 root 停在 CUTOFF，重试 seal 应能从断点续走到
  SETTLEMENT→COMPLETED；严格单向不变（终态后重入仍拒 SEAL_ORDER）。
- P2-3 cleanup 确认单向门：宿主对已终止 item 补交 physical_cleanup_confirmed
  应落账为迟到确认观测（同事务单向解除清理类 cutoff），delegate 准入门按
  每个 (item, node) 执行会话的**最新**失败观测判定；身份不符仍拒绝。

PanelState / ControlPlane 直接调用风格（同 test_dsh_execution_binding /
test_control），控制面事务全走真链、真事件目录。
"""
from __future__ import annotations

import hashlib
import os
from copy import deepcopy

import pytest

from dpswarm import invariants, state as state_module
from dpswarm.control import ControlPlane, ControlPlaneError
from dpswarm.events import EventStore
from dpswarm.server import PanelState
from dpswarm.types import (
    AcceptanceState,
    DelegationKind,
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
    SealPhase,
)


def make_catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


def route_b() -> ModelRoute:
    return ModelRoute("p", "b-model", level=Level.B)


@pytest.fixture()
def cp(tmp_path):
    return ControlPlane(spec=RootExecutionSpec(max_open_work_items=2,
                                               max_active_node_points=6,
                                               max_attempts=3),
                        store_path=tmp_path / "events.jsonl",
                        catalog=make_catalog())


def replay_checked(events):
    """全量回放过 check_event（invariant 层与事件流一致性）。"""
    proj = state_module.Projection()
    for ev in events:
        proj = invariants.check_event(proj, ev)
    return proj


# ---------------------------------------------------------------------------
# P2-2 证据文件持久化次序：artifact fsync 先于事件事务落盘
# ---------------------------------------------------------------------------


class TestArtifactFsyncOrdering:
    def test_write_artifact_fsyncs_file_before_return(self, cp, tmp_path, monkeypatch):
        synced = []
        real_fsync = os.fsync
        monkeypatch.setattr(os, "fsync",
                            lambda fd: (synced.append(fd), real_fsync(fd))[1])
        cp.write_artifact("init-baseline")  # 先让 bootstrap/写者锁产生基线 fsync
        baseline = len(synced)
        sha = cp.write_artifact("崩溃窗口内的证据正文")
        assert sha == hashlib.sha256("崩溃窗口内的证据正文".encode("utf-8")).hexdigest()
        # 证据文件写入路径上必须有 fsync（write_bytes 无 fsync 是缺陷现状）
        assert len(synced) > baseline
        assert (tmp_path / "artifacts" / f"{sha}.txt").read_bytes() == \
            "崩溃窗口内的证据正文".encode("utf-8")

    def test_submit_evidence_artifact_fsync_precedes_referencing_event_txn(
            self, tmp_path, monkeypatch):
        """submit 带 output：引用证据的事件 envelope fsync 之前，证据文件必须
        已 fsync 完成——按 fd 的文件标识（st_ino）区分 fsync 目标，断言
        artifacts/<sha>.txt 的 fsync 先于 work_item_submitted 事务落盘。"""
        timeline = []  # ("fsync", st_ino) | ("txn", envelope 尾事件 kind)
        real_fsync = os.fsync

        def fsync_spy(fd):
            timeline.append(("fsync", os.fstat(fd).st_ino))
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync_spy)
        real_append = EventStore.append_txn

        def append_txn_spy(self, events):
            result = real_append(self, events)   # marker 在 envelope fsync 完成后
            timeline.append(("txn", events[-1].kind))
            return result

        monkeypatch.setattr(EventStore, "append_txn", append_txn_spy)
        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=2,
                                                 max_active_node_points=6),
                          store_path=tmp_path / "events.jsonl",
                          catalog=make_catalog())
        content = "证据正文：fsync 次序回归"
        cp.submit(cp._root_item_id(), cp.root_lead_node, content)

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        artifact = tmp_path / "artifacts" / f"{digest}.txt"
        with open(artifact, "rb") as f:
            artifact_ino = os.fstat(f.fileno()).st_ino
        with open(tmp_path / "events.jsonl", "rb") as f:
            events_ino = os.fstat(f.fileno()).st_ino
        assert artifact_ino != events_ino

        artifact_syncs = [i for i, entry in enumerate(timeline)
                          if entry == ("fsync", artifact_ino)]
        submit_txn = timeline.index(("txn", "work_item_submitted"))
        # 证据文件 fsync 存在，且全部先于引用它的事件事务 envelope 落盘完成
        assert artifact_syncs, timeline
        assert max(artifact_syncs) < submit_txn, timeline


# ---------------------------------------------------------------------------
# P2-1 seal 断点续走（CUTOFF/SETTLEMENT 崩溃停留相位的幂等恢复）
# ---------------------------------------------------------------------------


class TestSealResumeAfterCrash:
    def _stranded_item(self, cp) -> str:
        """滞留 item：create + 两阶段启动、无终态——finish_seal 应回收。"""
        item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
        node = cp.begin_node(item.item_id, route_b())
        cp.confirm_node(node.node_id)
        return item.item_id

    def test_seal_retry_from_cutoff_completes_and_recycles_stranded_item(self, tmp_path):
        path = tmp_path / "events.jsonl"
        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=2,
                                                 max_active_node_points=6),
                          store_path=path, catalog=make_catalog())
        stranded = self._stranded_item(cp)
        cp.begin_seal("root")   # begin_seal 已提交、begin_settlement 前崩溃
        assert cp.proj.seal_phase["root"] == SealPhase.CUTOFF
        cp.close()              # 模拟重启：释放写者锁、保留事件目录

        resumed = ControlPlane(store_path=path, catalog=make_catalog())
        try:
            assert resumed.proj.seal_phase["root"] == SealPhase.CUTOFF
            resumed.begin_seal("root")         # 断点续走：不再抛 SEAL_ORDER
            resumed.begin_settlement("root")   # CUTOFF → SETTLEMENT
            resumed.finish_seal("root")        # SETTLEMENT → COMPLETED
            assert resumed.proj.seal_phase["root"] == SealPhase.COMPLETED
            assert resumed.proj.work_items[stranded].acceptance == \
                AcceptanceState.TERMINATED     # 滞留 item 被 finish_seal 回收
            cutoffs = [e for e in resumed.store.read_all()
                       if e.kind == "seal_admission_cutoff"]
            assert len(cutoffs) == 1           # 幂等：不重复发射 cutoff
            assert replay_checked(resumed.store.read_all()).seal_phase["root"] == \
                SealPhase.COMPLETED
        finally:
            resumed.close()

    def test_seal_retry_from_settlement_completes_without_duplicate_events(self, tmp_path):
        path = tmp_path / "events.jsonl"
        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=2,
                                                 max_active_node_points=6),
                          store_path=path, catalog=make_catalog())
        self._stranded_item(cp)
        cp.begin_seal("root")
        cp.begin_settlement("root")   # settlement 已提交、finish_seal 前崩溃
        cp.close()

        resumed = ControlPlane(store_path=path, catalog=make_catalog())
        try:
            resumed.begin_seal("root")         # 已过 cutoff：幂等放行
            resumed.begin_settlement("root")   # 已在 SETTLEMENT：幂等放行
            resumed.finish_seal("root")
            assert resumed.proj.seal_phase["root"] == SealPhase.COMPLETED
            events = resumed.store.read_all()
            assert [e.kind for e in events].count("seal_admission_cutoff") == 1
            assert [e.kind for e in events].count("seal_settlement_started") == 1
            assert replay_checked(events).seal_phase["root"] == SealPhase.COMPLETED
        finally:
            resumed.close()

    def test_completed_seal_reentry_is_rejected(self, cp):
        cp.begin_seal("root")
        cp.begin_settlement("root")
        cp.finish_seal("root")
        with pytest.raises(ControlPlaneError) as ei:
            cp.begin_seal("root")
        assert ei.value.code == "SEAL_ORDER"
        with pytest.raises(ControlPlaneError) as ei:
            cp.begin_settlement("root")
        assert ei.value.code == "SEAL_ORDER"

    def test_http_seal_retry_after_restart_completes(self, tmp_path):
        """server.seal() 三事务间隙崩溃（停在 CUTOFF）→ 同事件目录重建
        PanelState → 再次 POST /api/seal 语义重试 → completed。"""
        ws = tmp_path / "ws"
        panel = PanelState(ws)
        try:
            ok, resp = panel.delegate({"kind": "derive", "subtasks": [
                {"provider": "mock", "model": "b-kimi", "title": "t", "prompt": "p"}]})
            assert ok, resp
            item_id = resp["items"][0]["item_id"]
            panel.cp.begin_seal("root")   # 崩溃前仅 cutoff 事务落盘
        finally:
            panel.cp.close()

        panel2 = PanelState(ws)
        try:
            r = panel2.seal({"team": "root"})
            assert r["ok"], r
            assert r["phase"] == "completed"
            assert panel2.cp.proj.seal_phase["root"] == SealPhase.COMPLETED
            assert panel2.cp.proj.work_items[item_id].acceptance == \
                AcceptanceState.TERMINATED
        finally:
            panel2.cp.close()


# ---------------------------------------------------------------------------
# P2-3 cleanup 确认单向门：迟到确认落账、门按最新观测、身份不符仍拒
# ---------------------------------------------------------------------------


def _child(panel: PanelState) -> dict:
    assert panel.bind_execution_root({"parent_session_id": "host-root",
                                      "delegation_depth": 0,
                                      "provider": "mock", "model": "b-kimi"})[0]
    ok, resp = panel.delegate({"kind": "derive", "subtasks": [
        {"provider": "mock", "model": "b-kimi", "title": "fixture", "prompt": "offline"}]})
    assert ok, resp
    return resp["items"][0]


def _fail_request(item: dict, session: str = "host-child") -> dict:
    return {"item_id": item["item_id"], "node_id": item["node_id"],
            "attempt": item["attempt"], "context_epoch": item["context_epoch"],
            "reservation_session_id": item["session_id"],
            "execution_session_id": session, "parent_session_id": "host-root",
            "execution_provider": "spawn"}


def _derive_body(title="next"):
    return {"kind": "derive", "subtasks": [
        {"provider": "mock", "model": "b-kimi", "title": title, "prompt": "offline"}]}


def _fail_unconfirmed(panel, item, request):
    ok, result = panel.fail_execution({**request, "code": "DISPOSAL_PENDING",
                                       "error": "child failed; disposal incomplete",
                                       "physical_cleanup_confirmed": False})
    assert ok and result["outcome"] == "terminated", result
    return result


class TestLateCleanupConfirmation:
    def test_unconfirmed_failure_seals_then_confirmation_reopens_admission(self, tmp_path):
        panel = PanelState(tmp_path / "ws")
        try:
            item = _child(panel)
            request = _fail_request(item)
            assert panel.bind_execution(request)[0]
            _fail_unconfirmed(panel, item, request)
            assert panel.cp.proj.seal_phase["root"] == SealPhase.CUTOFF

            ok, r = panel.delegate(_derive_body())
            assert not ok and r["error"] == "EXECUTION_CLEANUP_UNCONFIRMED"

            # 迟到确认：同身份补 physical_cleanup_confirmed=true → 落账为
            # 确认观测，同事务单向解除清理类 cutoff
            ok, r = panel.fail_execution({**request, "physical_cleanup_confirmed": True})
            assert ok, r
            assert r["outcome"] == "cleanup-confirmed"
            assert panel.cp.proj.seal_phase["root"] == SealPhase.OPEN
            confirmations = [e for e in panel.cp.store.read_all()
                             if e.kind == "observation_recorded"
                             and e.payload.get("cleanup_confirmation")]
            assert len(confirmations) == 1
            assert confirmations[0].payload["execution_failure"][
                "physical_cleanup_confirmed"] is True
            assert replay_checked(panel.cp.store.read_all()).seal_phase["root"] == \
                SealPhase.OPEN

            ok, r = panel.delegate(_derive_body())   # 同会话恢复委派
            assert ok, r
        finally:
            panel.cp.close()

    def test_confirmation_without_flag_keeps_silent_early_return(self, tmp_path):
        """cleanup 仍为 false 的重报保持原早返回：不追加任何事件。"""
        panel = PanelState(tmp_path / "ws")
        try:
            item = _child(panel)
            request = _fail_request(item)
            assert panel.bind_execution(request)[0]
            _fail_unconfirmed(panel, item, request)
            seq = panel.cp.store.last_seq
            ok, r = panel.fail_execution({**request, "physical_cleanup_confirmed": False})
            assert ok and r["already_terminal"]
            assert panel.cp.store.last_seq == seq
        finally:
            panel.cp.close()

    def test_confirmation_with_wrong_identity_is_rejected_and_gate_stays(self, tmp_path):
        panel = PanelState(tmp_path / "ws")
        try:
            item = _child(panel)
            request = _fail_request(item)
            assert panel.bind_execution(request)[0]
            _fail_unconfirmed(panel, item, request)
            seq = panel.cp.store.last_seq
            for bad in ({"attempt": item["attempt"] + 1},
                        {"execution_session_id": "foreign-session"}):
                ok, r = panel.fail_execution({**request, **bad,
                                              "physical_cleanup_confirmed": True})
                assert not ok and r["error"] == "FENCE_VIOLATION", (bad, r)
            assert panel.cp.store.last_seq == seq   # 确认未落账
            assert panel.cp.proj.seal_phase["root"] == SealPhase.CUTOFF
            ok, r = panel.delegate(_derive_body())
            assert not ok and r["error"] == "EXECUTION_CLEANUP_UNCONFIRMED"
        finally:
            panel.cp.close()

    def test_partial_confirmation_keeps_admission_sealed(self, tmp_path):
        """两个未确认失败：确认其一仍封；全部确认后才恢复 OPEN。"""
        panel = PanelState(tmp_path / "ws")
        try:
            first, second = _child(panel), _child(panel)
            req_a, req_b = _fail_request(first, "child-a"), _fail_request(second, "child-b")
            assert panel.bind_execution(req_a)[0]
            assert panel.bind_execution(req_b)[0]
            _fail_unconfirmed(panel, first, req_a)
            _fail_unconfirmed(panel, second, req_b)

            ok, r = panel.fail_execution({**req_a, "physical_cleanup_confirmed": True})
            assert ok, r
            assert panel.cp.proj.seal_phase["root"] == SealPhase.CUTOFF  # 仍有残留
            ok, r = panel.delegate(_derive_body())
            assert not ok and r["error"] == "EXECUTION_CLEANUP_UNCONFIRMED"

            ok, r = panel.fail_execution({**req_b, "physical_cleanup_confirmed": True})
            assert ok, r
            assert panel.cp.proj.seal_phase["root"] == SealPhase.OPEN
            ok, r = panel.delegate(_derive_body())
            assert ok, r
        finally:
            panel.cp.close()

    def test_double_confirmation_is_idempotent_no_new_events(self, tmp_path):
        panel = PanelState(tmp_path / "ws")
        try:
            item = _child(panel)
            request = _fail_request(item)
            assert panel.bind_execution(request)[0]
            _fail_unconfirmed(panel, item, request)
            ok, _ = panel.fail_execution({**request, "physical_cleanup_confirmed": True})
            assert ok
            seq = panel.cp.store.last_seq
            ok, r = panel.fail_execution({**request, "physical_cleanup_confirmed": True})
            assert ok and r["already_terminal"]
            assert panel.cp.store.last_seq == seq
        finally:
            panel.cp.close()

    def test_unpublished_failure_has_nothing_to_confirm(self, tmp_path):
        """未发布（未公布子进程）失败无清理义务：补确认保持早返回、不落账。"""
        panel = PanelState(tmp_path / "ws")
        try:
            item = _child(panel)
            request = {**_fail_request(item), "execution_session_id": None}
            ok, r = panel.fail_execution({**request, "error": "start failed",
                                          "physical_cleanup_confirmed": False})
            assert ok and r["outcome"] == "terminated"
            assert panel.cp.proj.seal_phase["root"] == SealPhase.OPEN
            seq = panel.cp.store.last_seq
            ok, r = panel.fail_execution({**request, "physical_cleanup_confirmed": True})
            assert ok and r["already_terminal"]
            assert panel.cp.store.last_seq == seq
        finally:
            panel.cp.close()

    def test_confirmation_survives_restart_and_gate_rereads_event_log(self, tmp_path):
        """确认与解除均以事件为源：重启（同事件目录重建 PanelState）后相位
        仍为 open、delegate 门仍放行。"""
        ws = tmp_path / "ws"
        panel = PanelState(ws)
        try:
            item = _child(panel)
            request = _fail_request(item)
            assert panel.bind_execution(request)[0]
            _fail_unconfirmed(panel, item, request)
            ok, _ = panel.fail_execution({**request, "physical_cleanup_confirmed": True})
            assert ok
        finally:
            panel.cp.close()

        panel2 = PanelState(ws)
        try:
            assert panel2.cp.proj.seal_phase["root"] == SealPhase.OPEN
            ok, r = panel2.delegate(_derive_body())
            assert ok, r
        finally:
            panel2.cp.close()

    def test_manual_cutoff_is_not_undone_by_confirmation(self, tmp_path):
        """单向语义边界：非清理类 cutoff（手动 begin_seal，无 reason）不被
        迟到确认回退——确认仍落账（事件账可见），相位保持 CUTOFF；随后
        seal 断点续走（P2-1）仍可正常推进收尾。"""
        panel = PanelState(tmp_path / "ws")
        try:
            item = _child(panel)
            request = _fail_request(item)
            assert panel.bind_execution(request)[0]
            panel.cp.begin_seal("root")   # 先手动 cutoff（无清理 reason）
            _fail_unconfirmed(panel, item, request)
            ok, r = panel.fail_execution({**request, "physical_cleanup_confirmed": True})
            assert ok and r["outcome"] == "cleanup-confirmed"
            assert panel.cp.proj.seal_phase["root"] == SealPhase.CUTOFF
            assert [e for e in panel.cp.store.read_all()
                    if e.kind == "seal_admission_resumed"] == []
            # seal 断点续走不受影响：cutoff → settlement → completed
            panel.cp.begin_settlement("root")
            panel.cp.finish_seal("root")
            assert panel.cp.proj.seal_phase["root"] == SealPhase.COMPLETED
        finally:
            panel.cp.close()

    def test_confirmation_during_settlement_records_without_resume(self, tmp_path):
        """单向语义边界：cutoff 已推进到 SETTLEMENT 后确认只落账、不回退
        （SETTLEMENT 及之后永不回退）。"""
        panel = PanelState(tmp_path / "ws")
        try:
            item = _child(panel)
            request = _fail_request(item)
            assert panel.bind_execution(request)[0]
            _fail_unconfirmed(panel, item, request)
            panel.cp.begin_seal("root")
            panel.cp.begin_settlement("root")
            ok, r = panel.fail_execution({**request, "physical_cleanup_confirmed": True})
            assert ok and r["outcome"] == "cleanup-confirmed"
            assert panel.cp.proj.seal_phase["root"] == SealPhase.SETTLEMENT
            assert [e for e in panel.cp.store.read_all()
                    if e.kind == "seal_admission_resumed"] == []
            panel.cp.finish_seal("root")
            assert panel.cp.proj.seal_phase["root"] == SealPhase.COMPLETED
        finally:
            panel.cp.close()

