"""Admission failure compensation against the real durable control plane."""
from __future__ import annotations

import json

import pytest

from dpswarm.control import ControlPlaneError
from dpswarm.server import PanelState
from dpswarm.types import AcceptanceState, DelegationKind, Level, LifecycleState, ModelFacts


@pytest.fixture()
def panel(tmp_path):
    state = PanelState(tmp_path / "panel")
    # Exact failing session revision 2: rework allowed six direct workers while
    # retaining the user's four work-item slots and eight active points.
    assert state.publish_spec({"max_team_workers": 6})[0]
    yield state
    state.cp.close()


def request(*subtasks, kind="derive"):
    return {"kind": kind, "subtasks": list(subtasks or [task()])}


def task(**extra):
    return {"provider": "mock", "model": "b-kimi", "title": "offline task",
            "prompt": "no model requests are made", **extra}


def admit(panel, count):
    rows = []
    for _ in range(count):
        ok, response = panel.delegate(request())
        assert ok, response
        rows.extend(response["items"])
    return rows


def assert_capacity(panel, slots, points):
    assert panel.cp.proj.open_worker_slots_used == slots
    assert panel.cp.proj.active_points == points
    assert panel.cp.proj.spec.max_open_work_items == 4
    assert panel.cp.proj.spec.max_active_node_points == 8


def test_fourth_worker_points_denial_releases_only_new_cold_item_and_replays(panel):
    existing = admit(panel, 3)
    nodes_before = set(panel.cp.proj.nodes)
    assert_capacity(panel, 3, 7)
    failed_ids = []
    for _ in range(3):
        ok, response = panel.delegate(request())
        assert not ok and response["error"] == "POINTS_EXCEEDED", response
        assert_capacity(panel, 3, 7)  # Original implementation leaks a fourth slot here.
        assert response["partial"] == response["started"] == []
        assert response["cleanup_complete"] is True
        item_id = response["created"][0]["item_id"]
        failed_ids.append(item_id)
        assert response["cleanup"] == [{"item_id": item_id, "subtask_index": 0,
                "node_ids": [], "status": "terminated", "reason": "admission-failed-before-node"}]
        assert response["pending"][0]["cleanup_status"] == "terminated"
        assert panel.cp.proj.work_items[item_id].acceptance == AcceptanceState.TERMINATED
        assert_capacity(panel, 3, 7)
        assert set(panel.cp.proj.nodes) == nodes_before
    audits = [e for e in panel.cp.store.read_all() if e.kind == "work_item_terminated"]
    assert [e.payload["item_id"] for e in audits] == failed_ids
    for event in audits:
        assert event.payload["reason"] == "manual-stopped"
        assert json.loads(event.payload["summary"]) == {
            "phase": "delegate-admission-compensation", "error": "POINTS_EXCEEDED",
            "subtask_index": 0, "created_in_this_request": True, "execution_started": False}
    panel.cp.close()
    replay = PanelState(panel.workspace)
    try:
        assert_capacity(replay, 3, 7)
        assert all(replay.cp.proj.work_items[row["item_id"]].acceptance is None for row in existing)
        assert all(replay.cp.proj.work_items[item].acceptance == AcceptanceState.TERMINATED
                   for item in failed_ids)
        # Existing work settles through the ordinary protocol; the failed request
        # leaves no hidden slot to prevent a subsequent fresh admission.
        replay.cp.terminate(existing[0]["item_id"])
        assert admit(replay, 1)
        assert_capacity(replay, 3, 7)
    finally:
        replay.cp.close()


def test_multi_subtask_partial_start_keeps_started_nodes_and_cleans_unstarted(panel):
    existing = admit(panel, 1)
    ok, response = panel.delegate(request(task(), task(), task()))
    assert not ok and response["error"] == "POINTS_EXCEEDED", response
    assert_capacity(panel, 3, 7)
    assert len(response["created"]) == 3
    assert len(response["partial"]) == len(response["started"]) == 2
    assert [row["status"] for row in response["cleanup"]] == ["retained", "retained", "terminated"]
    assert response["cleanup_complete"] is False
    assert_capacity(panel, 3, 7)
    for row in existing + response["started"]:
        node = panel.cp.proj.nodes[row["node_id"]]
        assert node.lifecycle == LifecycleState.ACTIVE
        assert panel.cp.proj.leases[node.lease_id].active
        assert panel.cp.proj.work_items[row["item_id"]].acceptance is None


def test_slot_failure_mid_creation_compensates_already_created_item(panel):
    admit(panel, 3)
    ok, response = panel.delegate(request(task(), task()))
    assert not ok and response["error"] == "SLOT_EXCEEDED", response
    assert_capacity(panel, 3, 7)
    assert len(response["created"]) == 1
    assert response["started"] == [] and response["cleanup_complete"]
    assert_capacity(panel, 3, 7)


def test_pending_dag_and_later_unvisited_items_are_not_left_occupying_slots(panel, monkeypatch):
    admit(panel, 1)
    def refuse(*args, **kwargs):
        raise ControlPlaneError("POINTS_EXCEEDED", "forced admission failure after DAG creation")
    monkeypatch.setattr(panel.cp, "begin_node", refuse)
    ok, response = panel.delegate(request(task(deps=[1]), task(), task()))
    assert not ok and response["error"] == "POINTS_EXCEEDED"
    assert len(response["created"]) == len(response["pending"]) == 3
    assert response["pending"][0]["waiting_on"] == [response["created"][1]["item_id"]]
    assert response["cleanup_complete"]
    assert all(row["status"] == "terminated" for row in response["cleanup"])
    assert_capacity(panel, 1, 3)


def test_confirm_failure_reports_provisioning_node_even_when_partial_is_empty(panel, monkeypatch):
    def refuse(*args, **kwargs):
        raise ControlPlaneError("CONFIRM_FAILED", "activation could not be confirmed")
    monkeypatch.setattr(panel.cp, "confirm_node", refuse)
    ok, response = panel.delegate(request())
    assert not ok and response["error"] == "CONFIRM_FAILED"
    assert response["partial"] == []
    assert len(response["started"]) == 1
    assert response["started"][0]["lifecycle"] == "provisioning"
    assert response["cleanup"][0]["status"] == "retained"
    assert response["cleanup_complete"] is False
    assert_capacity(panel, 1, 3)
    node = panel.cp.proj.nodes[response["started"][0]["node_id"]]
    assert panel.cp.proj.leases[node.lease_id].active


def test_split_assistant_denial_reports_primary_without_terminating_existing_root(panel):
    admit(panel, 2)
    root_id = panel.cp._root_item_id()
    ok, response = panel.delegate(request(kind="split"))
    assert not ok and response["error"] == "POINTS_EXCEEDED", response
    assert response["partial"] == response["created"] == []
    assert len(response["started"]) == 1
    assert response["started"][0]["item_id"] == root_id
    assert response["cleanup"][0]["status"] == "retained"
    assert response["cleanup_complete"] is False
    assert panel.cp.proj.work_items[root_id].acceptance is None
    assert_capacity(panel, 2, 7)


def test_failed_compensation_is_explicit_and_does_not_claim_resource_release(panel, monkeypatch):
    admit(panel, 3)
    def refuse(*args, **kwargs):
        raise ControlPlaneError("STORAGE_FAULT", "injected compensation failure")
    monkeypatch.setattr(panel.cp, "terminate", refuse)
    ok, response = panel.delegate(request())
    assert not ok and response["error"] == "POINTS_EXCEEDED"
    assert response["cleanup_complete"] is False
    assert response["cleanup"][0]["status"] == "failed"
    assert response["cleanup"][0]["error"] == "STORAGE_FAULT"
    assert response["pending"][0]["cleanup_status"] == "failed"
    assert_capacity(panel, 4, 7)


def test_unconfirmed_physical_cleanup_is_never_compensated(panel, monkeypatch):
    # A conservative guard remains valid even if an external execution failure
    # is observed between reservation and the failure report.
    item = panel.cp.create_work_item(DelegationKind.DERIVE, parent_item=panel.cp._root_item_id())
    monkeypatch.setattr(panel, "_unconfirmed_execution_cleanups",
                        lambda: {(item.item_id, "unknown-child"): {"published": True}})
    response = panel._failed_delegation(ControlPlaneError("POINTS_EXCEEDED", "denied"),
                                       [(0, item)], set(panel.cp.proj.nodes), [], "derive")
    assert response["cleanup_complete"] is False
    assert response["cleanup"][0]["status"] == "retained"
    assert panel.cp.proj.work_items[item.item_id].acceptance is None
    assert_capacity(panel, 1, 1)


def test_successful_dag_keeps_pending_work_and_existing_response_contract(panel):
    ok, response = panel.delegate(request(task(), task(deps=[0])))
    assert ok, response
    assert len(response["items"]) == len(response["pending"]) == 1
    assert set(response) == {"ok", "items", "pending"}
    assert response["pending"][0]["waiting_on"] == [response["items"][0]["item_id"]]
    assert_capacity(panel, 2, 3)


def test_preexisting_cold_item_is_not_swept_by_another_requests_failure(panel):
    existing = panel.cp.create_work_item(DelegationKind.DERIVE, parent_item=panel.cp._root_item_id())
    admit(panel, 2)
    panel.cp.catalog.register(ModelFacts("mock", "heavy", Level.B, point_weight=4))
    ok, response = panel.delegate(request(task(model="heavy")))
    assert not ok and response["error"] == "POINTS_EXCEEDED"
    assert_capacity(panel, 3, 5)
    assert panel.cp.proj.work_items[existing.item_id].acceptance is None
    assert existing.item_id not in {row["item_id"] for row in response["cleanup"]}
    assert response["cleanup_complete"]


@pytest.mark.parametrize("sibling", [False, True])
def test_admission_settlement_rechecks_sole_node_under_control_lock(panel, sibling):
    row = admit(panel, 1)[0]
    if sibling:
        assistant, _ = panel.cp.split(row["node_id"], panel._route_from_subtask(task())[1])
        panel.cp.confirm_node(assistant.node_id)
    before = panel.cp.proj.open_worker_slots_used
    ok, result = panel.fail_execution({"item_id": row["item_id"], "node_id": row["node_id"],
        "attempt": row["attempt"], "context_epoch": row["context_epoch"],
        "reservation_session_id": row["session_id"], "execution_session_id": None,
        "published": False, "physical_cleanup_confirmed": True, "admission_cleanup": True})
    if sibling:
        assert not ok and result["error"] == "ADMISSION_CLEANUP_CONFLICT"
        assert panel.cp.proj.open_worker_slots_used == before
        assert panel.cp.proj.work_items[row["item_id"]].acceptance is None
    else:
        assert ok and result["outcome"] == "terminated"
        assert panel.cp.proj.open_worker_slots_used == 0
