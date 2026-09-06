"""Trusted DSH binding and failure settlement without model calls or HTTP."""
from copy import deepcopy

import pytest

from dpswarm import invariants, state
from dpswarm.control import ControlPlaneError
from dpswarm.server import PanelState


@pytest.fixture
def panel(tmp_path):
    panel = PanelState(tmp_path / "panel")
    yield panel
    panel.cp.close()


def delegated(panel):
    assert panel.bind_execution_root({"parent_session_id": "host-root", "delegation_depth": 0, "provider": "mock", "model": "b-kimi"})[0]
    ok, response = panel.delegate({"kind": "derive", "subtasks": [{
        "provider": "mock", "model": "b-kimi", "title": "fixture", "prompt": "offline"}]})
    assert ok, response
    return response["items"][0]


def binding(item, session="host-child"):
    return {"item_id": item["item_id"], "node_id": item["node_id"], "attempt": item["attempt"],
            "context_epoch": item["context_epoch"], "reservation_session_id": item["session_id"],
            "execution_session_id": session, "parent_session_id": "host-root", "execution_provider": "spawn"}


def snapshot(panel):
    return panel.cp.store.last_seq, deepcopy(panel.cp.snapshot())


def test_root_owner_conflicts_fail_without_rebinding(panel):
    first = panel.bind_execution_root({"parent_session_id": "host-root", "delegation_depth": 0, "provider": "mock", "model": "b-kimi"})
    assert first[0]
    before = snapshot(panel)
    ok, result = panel.bind_execution_root({"parent_session_id": "other-root", "delegation_depth": 0, "provider": "mock", "model": "b-kimi"})
    assert not ok and result["error"] == "ROOT_EXECUTION_CONFLICT"
    assert snapshot(panel) == before
    assert panel.bind_execution_root({"parent_session_id": "host-root", "delegation_depth": 0, "provider": "mock", "model": "b-kimi"})[0]
    assert snapshot(panel) == before


def test_nested_or_missing_root_identity_never_binds(panel):
    before = snapshot(panel)
    for request in ({}, {"parent_session_id": "host-child", "delegation_depth": 1},
                    {"parent_session_id": "host-root", "delegation_depth": False}):
        assert not panel.bind_execution_root(request)[0]
    assert snapshot(panel) == before


def test_true_session_replaces_reservation_and_rejects_old_submit_fence(panel):
    item = delegated(panel)
    assert panel.bind_execution(binding(item))[0]
    node = panel.cp.proj.nodes[item["node_id"]]
    assert node.session_id == "host-child"
    before = snapshot(panel)
    ok, result = panel.submit_output({**item, "output": "old", "stop_reason": "completed"})
    assert not ok and result["error"] == "FENCE_VIOLATION"
    assert snapshot(panel) == before
    assert panel.submit_output({**item, "session_id": "host-child", "output": "actual",
                                "token_usage": {"input_tokens": None, "output_tokens": None}})[0]
    assert panel.review({"item_id": item["item_id"], "verdict": "accept"})[0]
    usage = [event for event in panel.cp.store.read_all() if event.kind == "token_usage_recorded"][-1]
    assert all(usage.payload[key] is None for key in ("input", "output", "cache_read", "cache_write", "cost"))


def test_binding_is_immutable_and_parent_must_match_root(panel):
    item = delegated(panel)
    bad = binding(item)
    bad["parent_session_id"] = "foreign-parent"
    before = snapshot(panel)
    assert panel.bind_execution(bad)[1]["error"] == "ROOT_EXECUTION_CONFLICT"
    assert snapshot(panel) == before
    assert panel.bind_execution(binding(item))[0]
    before = snapshot(panel)
    assert panel.bind_execution(binding(item, "replaced"))[1]["error"] == "EXECUTION_ALREADY_BOUND"
    assert snapshot(panel) == before


def test_same_run_identity_cannot_bind_two_nodes(panel):
    a, b = delegated(panel), delegated(panel)
    assert panel.bind_execution(binding(a))[0]
    before = snapshot(panel)
    assert panel.bind_execution(binding(b))[1]["error"] == "EXECUTION_REUSED"
    assert snapshot(panel) == before


def test_failure_settlement_is_fenced_atomic_idempotent_and_retains_unknown_usage(panel):
    item = delegated(panel)
    request = binding(item)
    assert panel.bind_execution(request)[0]
    before = snapshot(panel)
    assert panel.fail_execution({**request, "attempt": 9})[1]["error"] == "FENCE_VIOLATION"
    assert snapshot(panel) == before
    ok, result = panel.fail_execution({**request, "code": "TRANSPORT_FAILURE", "error": "original failure",
        "details": {"disposalError": "cleanup fault"}, "physical_cleanup_confirmed": False})
    assert ok and result["outcome"] == "terminated"
    assert result["failure"]["message"] == "original failure"
    assert result["failure"]["details"]["disposalError"] == "cleanup fault"
    assert not result["failure"]["physical_cleanup_confirmed"]
    assert panel.cp.proj.active_points == 1 and panel.cp.proj.open_worker_slots_used == 0
    before = snapshot(panel)
    assert panel.fail_execution(request)[1]["already_terminal"]
    assert snapshot(panel) == before
    replay = state.Projection()
    for event in panel.cp.store.read_all():
        replay = invariants.check_event(replay, event)
    assert replay.nodes[item["node_id"]].session_id == "host-child"
    assert replay.active_points == 1


def test_failure_before_publication_releases_reserved_node(panel):
    item = delegated(panel)
    ok, result = panel.fail_execution({**binding(item), "execution_session_id": None,
                                      "code": "START_FAILED", "error": "provider start failed"})
    assert ok and result["outcome"] == "terminated"
    assert panel.cp.proj.open_worker_slots_used == 0


def test_late_failure_cannot_terminate_a_retried_item(panel):
    item = delegated(panel)
    request = binding(item)
    assert panel.bind_execution(request)[0]
    assert panel.submit_output({**item, "session_id": "host-child", "output": "retry"})[0]
    assert panel.review({"item_id": item["item_id"], "verdict": "reject", "attribution": "description"})[0]
    ok, result = panel.delegate({"item_id": item["item_id"], "subtask": {
        "provider": "mock", "model": "b-kimi", "title": "retry", "prompt": "retry"}})
    assert ok, result
    before = snapshot(panel)
    assert panel.fail_execution(request)[1]["error"] == "FENCE_VIOLATION"
    assert snapshot(panel) == before


def test_unknown_usage_remains_null_in_report_with_known_subtotals(panel):
    from dpswarm.observation import summarize_events
    item = delegated(panel)
    request = binding(item)
    assert panel.bind_execution(request)[0]
    panel.cp.record_token_usage(item["node_id"], 10, 2, 0, 0, 0.1)
    assert panel.fail_execution({**request, "error": "failure"})[0]
    report = summarize_events(panel.cp.store.read_all())
    assert report["token_totals"]["input"] is None
    assert report["token_totals"]["cost"] is None
    assert report["token_known_subtotals"]["input"] == 10
    assert report["token_known_subtotals"]["cost"] == 0.1
    assert report["token_unknown_counts"]["input"] == 2  # root session and failed child are independently unknown
    assert report["economics"]["worker_tokens"] is None
    assert report["delegations"] == 0  # execution failure is not a fabricated model capability record
    assert report["failures"] == 1


def test_root_requested_model_uses_catalog_level_and_does_not_inherit_placeholder_s(panel):
    from dpswarm.types import Level
    ok, response = panel.bind_execution_root({"parent_session_id": "host-root", "delegation_depth": 0,
                                             "provider": "mock", "model": "b-kimi"})
    assert ok, response
    root = panel.cp.proj.nodes[panel.cp.root_lead_node]
    assert root.route.provider == "mock" and root.route.model == "b-kimi"
    assert root.level == Level.B
    ok, result = panel.delegate({"kind": "fission", "subtasks": [{
        "provider": "mock", "model": "b-kimi", "title": "fixture", "prompt": "offline"}]})
    assert not ok
    assert "FISSION" in result["error"] or "LEVEL" in result["error"]


def test_unverified_root_model_never_claims_a_session(panel):
    before = snapshot(panel)
    ok, result = panel.bind_execution_root({"parent_session_id": "host-root", "delegation_depth": 0,
                                             "provider": "unknown-provider", "model": "unknown-model"})
    assert not ok
    assert snapshot(panel) == before
    assert panel.cp.proj.nodes[panel.cp.root_lead_node].execution_binding is None


def test_unpublished_start_failure_does_not_seal_other_work(panel):
    item = delegated(panel)
    ok, response = panel.fail_execution({**binding(item), "execution_session_id": None,
                                        "published": False, "error": "start failed"})
    assert ok, response
    assert panel.cp.proj.seal_phase["root"].value == "open"
    assert delegated(panel)["item_id"] != item["item_id"]


def test_dsh_root_reservation_cannot_submit_before_real_publication(panel):
    item = delegated(panel)
    before = snapshot(panel)
    ok, result = panel.submit_output({**item, "output": "unpublished synthetic delivery"})
    assert not ok and result["error"] == "EXECUTION_NOT_BOUND"
    assert snapshot(panel) == before
