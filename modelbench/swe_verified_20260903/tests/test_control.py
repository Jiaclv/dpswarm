"""Offline SWE bridge tests: real CP and durable ledger; no model or grader."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
from pathlib import Path
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_verified_20260903.control import SweControl, MODELS
from dpswarm import invariants, state
from dpswarm.control import ControlPlaneError
from dpswarm.team_runtime.ledger import ExecutionStore, LedgerError
from dpswarm.types import DelegationKind, Level, LifecycleState, RouteSource


@pytest.fixture
def controls(tmp_path):
    made = []
    def create(name="run", **kwargs):
        control = SweControl(tmp_path / name, "sympy__sympy-12345", **kwargs)
        made.append(control)
        return control
    yield create
    for control in reversed(made):
        control.close()


def request(model="glm-5.3", **kwargs):
    return {"model": model, "task": "Inspect one explicitly scoped bug", **kwargs}


def call(handle, ident="call-1", **kwargs):
    return {"call_id": ident, "model_requested": handle.model, "role": handle.role,
        "task_id": handle.instance_id, "run_id": handle.run_id,
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        "cached_input_tokens": 30, "reasoning_tokens": 5, "stop_reason": "stop", **kwargs}


def evidence(control):
    return deepcopy((control.snapshot(), [asdict(event) for event in control.cp.store.read_all()],
                     control.ledger.events_since()))


def check_replay(control):
    projection = state.Projection()
    for event in control.cp.store.read_all():
        projection = invariants.check_event(projection, event)
    assert projection.active_points == control.cp.proj.active_points
    assert projection.open_worker_slots_used == control.cp.proj.open_worker_slots_used
    return projection


def worker(control, model="glm-5.3"):
    handle, = control.delegate(control.lead, request(model))
    assert control.activate(handle) == handle
    return handle


def test_default_is_one_real_lead_with_exact_route_and_no_aa_claim(controls):
    control = controls()
    assert len(control.cp.proj.nodes) == len(control.cp.proj.work_items) == 1
    assert control.cp.proj.open_worker_slots_used == 0
    node = control.cp.proj.nodes[control.lead.node_id]
    assert node.node_id == control.cp.root_lead_node
    assert node.lifecycle == LifecycleState.ACTIVE
    assert node.route.model == control.lead.model == "gpt-5.6-sol"
    assert node.route.provider == control.lead.provider == "codex-chatgpt"
    assert node.context_epoch == control.lead.context_epoch
    assert node.session_id == control.lead.session_id
    assert node.package_hash == control.lead.manifest_hash
    assert {facts.model for facts in control.catalog.facts.values()} == set(MODELS)
    assert all(facts.level == Level.B and facts.aa_dimensional == {}
               and facts.input_price_per_mtok is None for facts in control.catalog.facts.values())
    assert control.get_usage()["total"]["calls"] == 0
    result = control.finish(control.lead, {"summary": "Lead produced the final artifact alone"})
    assert result["control_completed"] is True and result["official_resolved"] is None
    assert result["invariant_replay_passed"]
    assert result["cp"]["active_points"] == result["cp"]["open_worker_slots_used"] == 0


def test_runs_have_independent_roots_and_foreign_handle_is_rejected(controls):
    first, second = controls("one"), controls("two")
    assert first.lead.root_id != second.lead.root_id
    assert first.cp.store.path != second.cp.store.path
    before = evidence(second)
    with pytest.raises(ControlPlaneError, match="Handle was not issued"):
        second.delegate(first.lead, request())
    assert evidence(second) == before


@pytest.mark.parametrize("bad", [
    {}, {"task": "scope"}, {"model": "glm-5.3"}, request("unknown"),
    request(source="human"), request(level="S"), request(provider="forged"),
    request(task=" "), request(title={}),
])
def test_invalid_routes_and_authority_claims_have_no_side_effects(controls, bad):
    control = controls()
    before, original = evidence(control), deepcopy(bad)
    with pytest.raises((ControlPlaneError, LedgerError)):
        control.delegate(control.lead, bad)
    assert evidence(control) == before and bad == original


def test_multi_request_batch_is_explicitly_rejected_before_any_item(controls):
    control = controls()
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.delegate(control.lead, [request(), request("gpt-5.6-terra")])
    assert exc.value.code == "BATCH_NOT_SUPPORTED"
    assert evidence(control) == before


def test_concurrent_reservations_use_two_real_slots_and_reject_third(controls):
    control = controls()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(control.delegate, control.lead, request(model))
                   for model in ("glm-5.3", "gpt-5.6-terra")]
        handles = [future.result()[0] for future in futures]
    assert len({handle.item_id for handle in handles}) == 2
    assert len({handle.session_id for handle in handles}) == 2
    assert [handle.attempt for handle in handles] == [1, 1]
    assert control.cp.proj.open_worker_slots_used == 2 and control.cp.proj.active_points == 3
    for handle in handles:
        node = control.cp.proj.nodes[handle.node_id]
        assert node.lifecycle == LifecycleState.PROVISIONING
        assert node.route.source == RouteSource.ROUTE_LEAD
        assert node.route.model == handle.model
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.delegate(control.lead, request())
    assert exc.value.code == "WORKER_LIMIT" and evidence(control) == before
    check_replay(control)


def test_reserved_worker_cannot_record_call_until_activation(controls):
    control = controls()
    handle, = control.delegate(control.lead, [request()])
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.record_call(handle, call(handle))
    assert exc.value.code == "AGENT_STATE" and evidence(control) == before
    control.activate(handle)
    control.record_call(handle, call(handle))
    assert control.get_usage()["total"]["calls"] == 1


@pytest.mark.parametrize("field,value", [("model", "gpt-5.6-terra"), ("role", "lead"),
    ("session_id", "forged-session"), ("context_epoch", 9), ("attempt", 2)])
def test_forged_handle_cannot_record_or_delegate(controls, field, value):
    control = controls()
    handle = worker(control)
    forged = replace(handle, **{field: value})
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.record_call(forged, call(forged))
    assert exc.value.code == "HANDLE_MISMATCH"
    assert evidence(control) == before


def test_worker_cannot_delegate_or_adopt_peer(controls):
    control = controls()
    handle, peer = worker(control), worker(control, "gpt-5.6-luna")
    control.submit(peer, {"summary": "Evidence"})
    before = evidence(control)
    for function in (lambda: control.delegate(handle, request()),
                     lambda: control.decide(handle, peer, "adopt", "unauthorized")):
        with pytest.raises(ControlPlaneError) as exc:
            function()
        assert exc.value.code == "CALLER_NOT_LEAD"
        assert evidence(control) == before


def test_manifest_tampering_and_real_epoch_change_are_fenced(controls):
    control = controls()
    handle = worker(control)
    path = Path(handle.manifest_path)
    original = path.read_bytes()
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ControlPlaneError) as exc:
        control.record_call(handle, call(handle))
    assert exc.value.code == "MANIFEST_MISMATCH"
    path.write_bytes(original)
    control.cp.begin_rollover(handle.node_id, handle.manifest_path, handle.manifest_hash)
    with pytest.raises(ControlPlaneError) as exc:
        control.record_call(handle, call(handle))
    assert exc.value.code == "FENCE_VIOLATION"
    assert control.get_usage()["total"]["calls"] == 0


def test_provisioning_failure_terminates_created_item_and_returns_slot(controls, monkeypatch):
    control = controls()
    with monkeypatch.context() as patch:
        patch.setattr(control, "_manifest", lambda *args: (_ for _ in ()).throw(OSError("disk fixture")))
        with pytest.raises(OSError, match="disk fixture"):
            control.delegate(control.lead, request())
    children = [item for item in control.cp.proj.work_items.values() if item.kind == DelegationKind.DERIVE]
    assert len(children) == 1 and children[0].acceptance.value == "terminated"
    assert control.cp.proj.open_worker_slots_used == 0 and control.cp.proj.active_points == 1
    worker(control)
    check_replay(control)


def test_activation_failure_releases_provisioning_lease(controls, monkeypatch):
    control = controls()
    handle, = control.delegate(control.lead, request())
    with monkeypatch.context() as patch:
        patch.setattr(control.cp, "confirm_node", lambda *args, **kw:
                      (_ for _ in ()).throw(ControlPlaneError("FIXTURE", "activation failed")))
        with pytest.raises(ControlPlaneError):
            control.activate(handle)
    assert control.cp.proj.nodes[handle.node_id].terminated
    assert control.cp.proj.open_worker_slots_used == 0 and control.cp.proj.active_points == 1
    assert any(event["kind"] == "agent_failed" for event in control.ledger.events_since())
    before = evidence(control)
    failed = control.fail(handle, "Host caught the already settled activation failure")
    assert failed["reason"] == "Worker activation failed"
    assert evidence(control) == before
    check_replay(control)


def test_submission_is_immutable_and_only_lead_decision_releases_slot(controls):
    control = controls()
    handle = worker(control)
    saved = control.submit(handle, {"summary": "Proposed solution"})
    original = Path(saved["path"]).read_bytes()
    assert control.cp.proj.work_items[handle.item_id].acceptance.value == "submitted"
    assert control.cp.proj.open_worker_slots_used == 1
    with pytest.raises(ControlPlaneError):
        control.submit(handle, {"summary": "replacement"})
    assert Path(saved["path"]).read_bytes() == original
    with pytest.raises(ControlPlaneError) as exc:
        control.finish(control.lead, {"summary": "premature"})
    assert exc.value.code == "WORKERS_PENDING"
    decision = control.decide(control.lead, handle, "discard", "Lead chose its own implementation")
    assert decision["decision"] == "discard"
    assert control.cp.proj.open_worker_slots_used == 0
    assert control.cp.proj.work_items[handle.item_id].acceptance.value == "terminated"


def test_patch_digest_bound_at_submission_and_rechecked_before_adoption(controls):
    control = controls()
    handle = worker(control)
    patch = control.run_dir / "worker.patch"
    patch.write_text("diff fixture\n", encoding="utf-8")
    sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    artifact = {"summary": "proposed patch", "patch_path": str(patch), "patch_sha256": sha}
    saved = control.submit(handle, artifact)
    assert saved["verified_patch"]["sha256"] == sha
    patch.write_text("changed patch\n", encoding="utf-8")
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.decide(control.lead, handle, "adopt", "Use worker patch")
    assert exc.value.code == "ARTIFACT_CHANGED" and evidence(control) == before
    patch.write_text("diff fixture\n", encoding="utf-8")
    control.decide(control.lead, handle, "adopt", "Reviewed patch and supporting checks")
    assert control.cp.proj.work_items[handle.item_id].acceptance.value == "accepted"
    result = control.finish(control.lead, artifact)
    assert result["control_completed"] and result["official_resolved"] is None
    assert result["cp"]["active_points"] == result["cp"]["open_worker_slots_used"] == 0
    assert control.finish(control.lead, artifact) == result
    with pytest.raises(ControlPlaneError) as exc:
        control.finish(control.lead, {"summary": "changed final artifact"})
    assert exc.value.code == "FINISH_CONFLICT"


def test_wrong_patch_hash_rejected_before_cp_submission(controls):
    control = controls()
    handle = worker(control)
    patch = control.run_dir / "patch.diff"
    patch.write_text("patch", encoding="utf-8")
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.submit(handle, {"patch_path": str(patch), "patch_sha256": "0" * 64})
    assert exc.value.code == "PATCH_HASH_MISMATCH" and evidence(control) == before


def test_readonly_decision_preflight_rejects_reason_and_pins_selected_patch(controls):
    control = controls()
    handle = worker(control)
    patch = control.run_dir / "worker.patch"
    patch.write_bytes(b"diff fixture\n")
    sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    control.submit(handle, {"patch_path": str(patch), "patch_sha256": sha})
    before = evidence(control)
    with pytest.raises(LedgerError):
        control.validate_decision(control.lead, handle, "adopt", "")
    assert evidence(control) == before
    with pytest.raises(ControlPlaneError) as exc:
        control.validate_decision(control.lead, handle, "adopt", "Checked", {"delta_sha256": "0" * 64})
    assert exc.value.code == "PATCH_HASH_MISMATCH" and evidence(control) == before
    value = control.validate_decision(control.lead, handle, "adopt", "Checked", {"delta_sha256": sha})
    assert value["artifact"]["verified_patch"]["sha256"] == sha
    assert evidence(control) == before
    patch.write_bytes(b"changed after preflight\n")
    with pytest.raises(ControlPlaneError) as exc:
        control.decide(control.lead, handle, "adopt", "Checked", {"delta_sha256": sha})
    assert exc.value.code == "ARTIFACT_CHANGED" and evidence(control) == before


def test_decision_preflight_requires_original_lead_and_submitted_worker(controls):
    control = controls()
    handle = worker(control)
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.validate_decision(control.lead, handle, "adopt", "Not submitted")
    assert exc.value.code == "AGENT_STATE" and evidence(control) == before
    control.submit(handle, {"summary": "evidence"})
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.validate_decision(handle, handle, "adopt", "Self adoption")
    assert exc.value.code == "AGENT_STATE" and evidence(control) == before


def test_call_accounting_is_once_nullable_and_keeps_cache_dimension_separate(controls):
    control = controls()
    handle = worker(control)
    known = call(control.lead, "known")
    first = control.record_call(control.lead, known)
    before = evidence(control)
    assert control.record_call(control.lead, known) == first
    assert evidence(control) == before
    control.record_call(handle, call(handle, "unknown", input_tokens=None, total_tokens=None,
                                     cached_input_tokens=None, reasoning_tokens=None))
    total = control.get_usage()["total"]
    assert total["calls"] == 2 and total["total_tokens"] is None and total["input_tokens"] is None
    assert total["output_tokens"] == 40 and total["known_subtotals"]["total_tokens"] == 120
    assert total["cached_input_tokens"] is None and total["known_subtotals"]["cached_input_tokens"] == 30
    assert total["unknown_counts"]["input_tokens"] == 1 and total["cost_usd"] is None
    token_events = [event for event in control.cp.store.read_all() if event.kind == "token_usage_recorded"]
    assert len(token_events) == 2
    assert token_events[0].payload["input"] == 70 and token_events[0].payload["cache_read"] == 30
    assert token_events[1].payload["input"] is None and token_events[1].payload["cache_read"] is None
    assert all(event.payload["cost"] is None for event in token_events)
    reloaded = ExecutionStore(control.directory / "ledger")
    records = [event for event in reloaded.events_since() if event["kind"] == "call_recorded"]
    assert len(records) == 2 and records[1]["payload"]["record"]["input_tokens"] is None


@pytest.mark.parametrize("change,code", [
    ({"model_requested": "gpt-5.6-luna"}, "CALL_IDENTITY"),
    ({"run_id": "another-run"}, "CALL_IDENTITY"),
    ({"role": "worker"}, "CALL_IDENTITY"),
    ({"task_id": "another-task"}, "CALL_IDENTITY"),
])
def test_call_identity_mismatch_has_no_accounting_effect(controls, change, code):
    control = controls()
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.record_call(control.lead, call(control.lead, **change))
    assert exc.value.code == code and evidence(control) == before


def test_conflicting_call_id_and_invalid_usage_do_not_add_events(controls):
    control = controls()
    control.record_call(control.lead, call(control.lead))
    before = evidence(control)
    with pytest.raises(ControlPlaneError) as exc:
        control.record_call(control.lead, call(control.lead, output_tokens=21))
    assert exc.value.code == "CALL_CONFLICT"
    with pytest.raises(LedgerError):
        control.record_call(control.lead, call(control.lead, "negative", input_tokens=-1))
    assert evidence(control) == before


def test_fail_and_close_settle_all_workers_without_correctness_claim(controls):
    control = controls()
    active = worker(control)
    reserved, = control.delegate(control.lead, request("gpt-5.6-terra"))
    control.fail(active, "transport failure", {"usage": None})
    assert control.cp.proj.nodes[active.node_id].terminated
    assert control.cp.proj.open_worker_slots_used == 1
    control.close("Host stopped before final patch")
    assert control.cp.proj.nodes[reserved.node_id].terminated
    assert control.cp.proj.active_points == control.cp.proj.open_worker_slots_used == 0
    projection = check_replay(control)
    assert all(item.acceptance.value == "terminated" for item in projection.work_items.values())
    assert control.snapshot()["official_resolved"] is None
    assert control.snapshot()["cp"]["seal_phase"]["root"] == "completed"
    control.close()  # Idempotent cleanup.


def test_existing_run_cannot_be_overwritten(controls):
    control = controls()
    original = (control.directory / "events.jsonl").read_bytes()
    with pytest.raises(ControlPlaneError) as exc:
        SweControl(control.run_dir, control.instance_id)
    assert exc.value.code == "RUN_EXISTS"
    assert (control.directory / "events.jsonl").read_bytes() == original
