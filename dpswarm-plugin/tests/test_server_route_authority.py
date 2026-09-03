"""Agent route requests select models; server facts alone grant authority.

All tests use the real control plane and event store, with no model calls.
"""
from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dpswarm.aa import AASnapshot
from dpswarm.control import ControlPlane
from dpswarm.server import Handler, PanelState, default_catalog
from dpswarm.types import Level, ModelFacts, RouteSource


@pytest.fixture()
def state(tmp_path):
    panel = PanelState(tmp_path / "panel")
    panel.cp.store.close()
    panel.cp = ControlPlane(store_path=tmp_path / "events.jsonl",
                            catalog=default_catalog(), root_level=Level.B)
    panel.aa = None
    yield panel
    panel.cp.store.close()


def subtask(**changes):
    return {"provider": "mock", "model": "b-kimi", "title": "bounded task",
            "prompt": "Do the task", **changes}


def evidence(state):
    return copy.deepcopy((state.cp.snapshot(), state.events(10000),
                          state.cp.catalog.facts))


def assert_rejected_without_effects(state, body, error):
    before, original = evidence(state), copy.deepcopy(body)
    ok, response = state.delegate(body)
    assert not ok and response["error"] == error, response
    assert evidence(state) == before
    assert body == original
    return response


@pytest.mark.parametrize("kind", ["derive", "fission", "split"])
def test_agent_cannot_claim_human_source(state, kind):
    response = assert_rejected_without_effects(
        state, {"kind": kind, "subtasks": [subtask(model="s-grok", source="human")]},
        "BAD_SUBTASK")
    assert "source" in response["errors"]


@pytest.mark.parametrize("field,value", [
    ("level", "S"), ("level", "D"), ("level_hint", "S"),
    ("aa_coding", 99), ("human_override", True), ("unexpected", "value"),
])
def test_server_owned_and_unknown_fields_are_rejected(state, field, value):
    response = assert_rejected_without_effects(
        state, {"kind": "derive", "subtasks": [subtask(**{field: value})]}, "BAD_SUBTASK")
    assert field in response["errors"]


@pytest.mark.parametrize("route", [
    {}, {"model": "b-kimi"}, {"provider": "mock"},
    {"provider": "", "model": "b-kimi"}, {"provider": "mock", "model": "  "},
    {"provider": ["mock"], "model": "b-kimi"},
    {"provider": "mock", "model": "b-kimi", "reasoning_effort": {}},
    None, "not an object",
])
def test_explicit_well_typed_route_is_required(state, route):
    assert_rejected_without_effects(state, {"kind": "derive", "subtasks": [route]},
                                    "BAD_SUBTASK")


def test_unknown_model_is_not_registered_from_agent_request(state):
    assert_rejected_without_effects(
        state, {"kind": "derive", "subtasks": [subtask(provider="new", model="unknown")]},
        "MODEL_UNAVAILABLE")
    assert state.cp.catalog.resolve("new", "unknown") is None


def test_catalog_level_is_enforced_before_creating_items(state):
    assert_rejected_without_effects(
        state, {"kind": "derive", "subtasks": [subtask(), subtask(model="s-grok")]},
        "LEVEL_DIRECTION")


def test_unavailable_catalog_entry_is_rejected_before_creating_items(state):
    state.cp.catalog.register(ModelFacts("manual", "disabled", Level.C, available=False))
    assert_rejected_without_effects(
        state, {"kind": "derive", "subtasks": [subtask(provider="manual", model="disabled")]},
        "MODEL_UNAVAILABLE")


def test_trusted_manual_catalog_route_keeps_level_but_source_is_lead(state):
    state.cp.catalog.register(ModelFacts("manual", "configured", Level.C, aa_source="declared"))
    body = {"kind": "derive", "subtasks": [
        subtask(provider="manual", model="configured", source="lead")]}
    original = copy.deepcopy(body)
    ok, response = state.delegate(body)
    assert ok, response
    node = state.cp.proj.nodes[response["items"][0]["node_id"]]
    assert node.route.level == Level.C
    assert node.route.source == RouteSource.ROUTE_LEAD
    started = [e for e in state.cp.store.read_all()
               if e.kind == "node_provisioning" and e.payload["node_id"] == node.node_id]
    assert len(started) == 1 and started[0].payload["human_override"] is False
    assert body == original


def test_trusted_aa_entry_can_supply_new_model_facts(state):
    state.aa = AASnapshot({"snapshot_date": "fixture", "models": {
        "aa-model": {"overall": 35, "coding": 34}}})
    ok, response = state.delegate({"kind": "derive", "subtasks": [
        subtask(provider="configured-provider", model="aa-model")]})
    assert ok, response
    facts = state.cp.catalog.resolve("configured-provider", "aa-model")
    assert facts.level == Level.C
    assert facts.aa_source == "aa@fixture"
    assert facts.aa_dimensional == {"overall": 35.0, "coding": 34.0}
    node = state.cp.proj.nodes[response["items"][0]["node_id"]]
    assert node.route.source == RouteSource.ROUTE_LEAD


def test_invalid_later_route_does_not_register_earlier_aa_entry(state):
    state.aa = AASnapshot({"models": {"aa-model": {"overall": 35}}})
    assert_rejected_without_effects(state, {"kind": "derive", "subtasks": [
        subtask(provider="new", model="aa-model"), subtask(source="human")]}, "BAD_SUBTASK")


@pytest.mark.parametrize("replacement,error", [
    (subtask(source="human"), "BAD_SUBTASK"),
    (subtask(level="S"), "BAD_SUBTASK"),
    (subtask(model="s-grok"), "LEVEL_DIRECTION"),
    (subtask(provider="new", model="unknown"), "MODEL_UNAVAILABLE"),
    ({"model": "b-kimi"}, "BAD_SUBTASK"),
])
def test_rerun_route_rejection_does_not_spend_attempt_or_retire_nodes(state, replacement, error):
    ok, response = state.delegate({"kind": "derive", "subtasks": [subtask()]})
    assert ok, response
    item = response["items"][0]
    ok, response = state.submit_output({**item, "output": "first result"})
    assert ok, response
    ok, response = state.review({"item_id": item["item_id"], "verdict": "reject",
                                 "attribution": "capability", "reason": "needs correction"})
    assert ok, response
    assert_rejected_without_effects(state, {"item_id": item["item_id"], "subtask": replacement},
                                    error)
    assert state.cp.proj.work_items[item["item_id"]].attempt == 1
    assert not state.cp.proj.nodes[item["node_id"]].terminated


@pytest.mark.parametrize("deps,error", [
    ([[1], [0]], "CYCLE"),
    ([[1], [2], [0]], "CYCLE"),
    ([[], [0, 0]], "BAD_DEPS"),
    ([[], [True]], "BAD_DEPS"),
    ([[], [2]], "BAD_DEPS"),
    ([[], ["0"]], "BAD_DEPS"),
    ([[], "0"], "BAD_SUBTASK"),
    ([[], {"0": True}], "BAD_SUBTASK"),
    ([[], None], "BAD_SUBTASK"),
])
def test_invalid_dependency_graph_has_no_control_plane_side_effects(state, deps, error):
    assert_rejected_without_effects(state, {
        "kind": "derive", "subtasks": [subtask(deps=values) for values in deps]}, error)


def test_valid_forward_dependency_still_starts_only_ready_item(state):
    ok, response = state.delegate({"kind": "derive", "subtasks": [
        subtask(deps=[1]), subtask()]})
    assert ok, response
    assert [item["subtask_index"] for item in response["items"]] == [1]
    assert len(response["pending"]) == 1


def test_human_directive_channel_still_publishes_and_records(state):
    ok, response = state.directive({"kind": "config", "spec": {"max_active_node_points": 32}})
    assert ok and response["revision"] == 2
    ok, response = state.directive({"kind": "immediate", "payload": {
        "op": "wakeup", "node_id": state.cp.root_lead_node}})
    assert ok, response
    ok, response = state.directive({"kind": "terminal", "payload": {"scope": "root"}})
    assert ok, response
    assert {"human_directive", "node_wakeup", "spec_published"}.issubset(
        {e.kind for e in state.cp.store.read_all()})


def test_http_rejects_forged_source_with_structured_error(state):
    class TestHandler(Handler):
        pass
    TestHandler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = evidence(state)
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/api/delegate",
        data=json.dumps({"kind": "derive", "subtasks": [subtask(source="human")]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {state.token}"})
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 400
        response = json.loads(error.value.read())
        assert response["error"] == "BAD_SUBTASK" and "source" in response["errors"]
        assert evidence(state) == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
