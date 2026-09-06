"""Offline reporting fixtures: no runtime, provider, grader, or frozen writes."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_fixed_team_20260903 import reporting
from modelbench.swe_fixed_team_20260903.validation.audit_results import Audit


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def fixture(tmp_path, *, schema=12, cm="integrated_on_demand", tasks=1, lead="gpt-5.6-terra"):
    schedule = []
    for index in range(tasks):
        entry = {"run_id": f"run-{index}", "arm": "hetero", "condition": "hetero_team",
                 "worker_model": None, "worker_models": ["gpt-5.6-terra", "glm-5.3"],
                 "lead_model": lead, "instance": {"instance_id": f"task-{index}"}}
        schedule.append(entry)
        result = {"run_id": entry["run_id"], "arm": entry["arm"], "condition": entry["condition"],
                  "instance_id": entry["instance"]["instance_id"], "worker_model": None,
                  "lead_model": lead, "worker_pool": entry["worker_models"], "call_count": 0,
                  "cm_call_count": 0, "score": {"completed": False}, "activation_source": "experiment_protocol",
                  "workers_with_actual_calls": 0, "workers_with_call_records": 0,
                  "workers_with_measured_usage": 0, "workers": []}
        if schema is not None:
            result["schema_version"] = schema
        if schema == 12:
            result.update(execution_health={"status": "ok", "errors": []},
                          lead_worktree={"observed_persisted_change": True})
        if cm is not None:
            result["mechanism_coverage"] = {"derive": "fixed", "split": "not_exposed",
                                            "fission": "not_exposed", "cm": cm}
        write_json(tmp_path / "results" / entry["run_id"] / "result.json", result)
    manifest = {"schedule": schedule, "lead_model": lead, "fixed_workers_per_team": 2,
                "limits": {"cm_model": "glm-5.3-flash"}}
    write_json(tmp_path / "manifest.json", manifest)
    return manifest


def add_call(batch, entry, role, model, *, worker=None):
    folder = batch / "results" / entry["run_id"]
    call_id = entry["run_id"] + "-" + role
    record = {"call_id": call_id, "run_id": entry["run_id"], "task_id": entry["instance"]["instance_id"],
              "role": role, "model_requested": model, "transport_attempt_count": 1,
              "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
              "cached_input_tokens": 0, "reasoning_tokens": 0, "wall_seconds": 0.1}
    write_json(folder / "calls" / hashlib.sha256(call_id.encode()).hexdigest() / "metadata.json", record)
    result_path = folder / "result.json"
    result = json.loads(result_path.read_text())
    result["cm_call_count" if role == "cm" else "call_count"] += 1
    write_json(result_path, result)
    if worker:
        events = [{"event": "worker_admitted", "worker_id": worker, "handle": {
                    "node_id": "node-1", "role": "worker", "model": entry["worker_models"][0]}},
                  {"event": "call_reserved", "call_id": call_id, "handle": {
                    "node_id": "node-1", "role": "worker", "model": entry["worker_models"][0]}}]
        (folder / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return record


def test_report_uses_actual_task_count_lead_and_cm_records(tmp_path):
    manifest = fixture(tmp_path, tasks=3)
    add_call(tmp_path, manifest["schedule"][0], "cm", "glm-5.3-flash")
    summary = reporting.report(tmp_path)
    report = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert summary["scope"]["CM"] == "integrated_on_demand"
    assert summary["scope"]["CM_usage"]["calls"] == 1
    assert summary["scope"]["CM_usage"]["total_tokens"] == 12
    assert summary["task_count"] == 3
    assert "gpt-5.6-terra" in report
    assert "仅两题" not in report and "Sol Lead" not in report and "CM 为 not_integrated" not in report
    assert summary["rows"][0]["worker_pool"] == ["gpt-5.6-terra", "glm-5.3"]


def test_legacy_missing_mechanism_and_health_remain_unknown(tmp_path):
    fixture(tmp_path, schema=None, cm=None)
    summary = reporting.report(tmp_path)
    assert summary["scope"]["CM"] is None
    assert summary["rows"][0]["execution_health"] is None
    assert summary["rows"][0]["lead_worktree"] is None
    legacy = summary["evidence_schema_groups"]["legacy_unversioned"]
    assert legacy["lead_observed_persisted_change_runs"] == 0
    assert legacy["lead_observed_persisted_change_unknown_runs"] == 1


def test_new_and_legacy_execution_evidence_have_separate_denominators(tmp_path):
    fixture(tmp_path, tasks=2)
    path = tmp_path / "results/run-1/result.json"
    legacy = json.loads(path.read_text())
    for field in ("schema_version", "execution_health", "lead_worktree"):
        legacy.pop(field)
    legacy["lead_edit_detected"] = True
    write_json(path, legacy)
    summary = reporting.report(tmp_path)
    groups = summary["evidence_schema_groups"]
    assert groups["schema_12"]["runs"] == 1
    assert groups["schema_12"]["lead_observed_persisted_change_runs"] == 1
    assert groups["legacy_unversioned"]["lead_observed_persisted_change_unknown_runs"] == 1


def test_reversed_result_worker_pool_is_rejected(tmp_path):
    fixture(tmp_path)
    path = tmp_path / "results/run-0/result.json"
    result = json.loads(path.read_text())
    result["worker_pool"].reverse()
    write_json(path, result)
    with pytest.raises(ValueError, match="worker_pool"):
        reporting.report(tmp_path)


def test_heterogeneous_pair_requires_two_distinct_ordered_models(tmp_path):
    manifest = fixture(tmp_path)
    manifest["schedule"][0]["worker_models"] = ["glm-5.3", "glm-5.3"]
    write_json(tmp_path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="hetero"):
        reporting.report(tmp_path)


def test_bound_worker_call_cannot_use_other_workers_model(tmp_path):
    manifest = fixture(tmp_path)
    add_call(tmp_path, manifest["schedule"][0], "worker", "glm-5.3", worker="worker-1")
    with pytest.raises(ValueError, match="worker.*model|model.*worker"):
        reporting.report(tmp_path)


def test_worker_admission_checks_ordinal_instead_of_membership(tmp_path):
    manifest = fixture(tmp_path)
    run = tmp_path / "results/run-0"
    event = {"event": "worker_admitted", "worker_id": "worker-1", "handle": {
             "node_id": "node-1", "model": "glm-5.3", "role": "worker"}}
    (run / "events.jsonl").write_text(json.dumps(event) + "\n")
    with pytest.raises(ValueError, match="worker.*model|model.*worker"):
        reporting.report(tmp_path)


def test_cm_model_uses_per_entry_effective_limits(tmp_path):
    manifest = fixture(tmp_path)
    manifest["schedule"][0]["effective_limits"] = {"cm_model": "glm-5.3"}
    write_json(tmp_path / "manifest.json", manifest)
    add_call(tmp_path, manifest["schedule"][0], "cm", "glm-5.3")
    assert reporting.report(tmp_path)["scope"]["CM_usage"]["calls"] == 1


def test_audit_preserves_legacy_edit_heuristic_but_never_calls_it_persisted_change(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text(json.dumps({"event": "patch_frozen", "lead_edit_detected": True,
                                                "lead_first_edit_ordinal": 2}) + "\n")
    forensics = Audit(tmp_path).forensics(run, {"workers": []})
    assert forensics["lead_edit_detected"] is True
    assert forensics["lead_edit_attempted"] is True
    assert forensics["lead_observed_persisted_change"] is None
    assert forensics["edit_evidence_semantics"] == "legacy_command_attempt_heuristic"


def test_audit_schema12_distinguishes_attempt_from_persisted_change(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text(json.dumps({"event": "patch_frozen", "lead_edit_detected": True}) + "\n")
    result = {"schema_version": 12, "lead_worktree": {"observed_persisted_change": False},
              "execution_health": {"status": "host_error", "errors": [{"phase": "write"}]},
              "workers": [{"worker_id": "worker-1", "edit_attempted": True,
                           "worktree": {"observed_persisted_change": True}}]}
    forensics = Audit(tmp_path).forensics(run, result)
    assert forensics["lead_edit_attempted"] is True
    assert forensics["lead_observed_persisted_change"] is False
    assert forensics["worker_observed_persisted_changes"] == {"worker-1": True}
    assert forensics["execution_health"] == result["execution_health"]


def test_audit_flags_ordered_pool_identity_mismatch(tmp_path):
    manifest = fixture(tmp_path)
    path = tmp_path / "results/run-0/result.json"
    result = json.loads(path.read_text())
    result["worker_pool"].reverse()
    audit = Audit(tmp_path)
    audit.identity(path.parent, manifest["schedule"][0], result, manifest)
    assert any(finding["code"] == "result_route_identity_mismatch" for finding in audit.findings)


def test_audit_checks_activation_pair_order_and_worker_role_direction(tmp_path):
    manifest = fixture(tmp_path)
    entry = manifest["schedule"][0]
    run = tmp_path / "results/run-0"
    handle = {"node_id": "node-1", "model": "glm-5.3", "role": "worker"}
    runtime = [{"event": "team_activation_requested", "source": "experiment_protocol",
                "requested_workers": 2, "mechanism": "derive",
                "worker_models": list(reversed(entry["worker_models"]))},
               {"event": "worker_admitted", "worker_id": "worker-1", "source": "experiment_protocol",
                "handle": handle, "request": {"model": "glm-5.3"}}]
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in runtime))
    ledger = run / "control-plane/ledger/execution.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"kind": "worker_reserved", "payload": {
        "handle": handle, "request": {"model": "glm-5.3"}}}) + "\n")
    result = json.loads((run / "result.json").read_text())
    audit = Audit(tmp_path)
    audit.activation(run, entry, result, [])
    assert any(f["code"] == "bootstrap_request_mismatch" and f.get("field") == "worker_models" for f in audit.findings)
    assert any(f["code"] == "bootstrap_worker_model_mismatch" and f.get("field") == "worker-1" for f in audit.findings)


def test_audit_schema12_requires_health_and_reconciles_host_failure(tmp_path):
    manifest = fixture(tmp_path)
    run = tmp_path / "results/run-0"
    result = json.loads((run / "result.json").read_text())
    result["infrastructure_error"] = {"type": "HostRuntimeError"}
    audit = Audit(tmp_path)
    audit.identity(run, manifest["schedule"][0], result, manifest)
    assert any(f["code"] == "schema12_execution_health_disagreement" for f in audit.findings)
    result.pop("execution_health")
    audit.identity(run, manifest["schedule"][0], result, manifest)
    assert any(f["code"] == "schema12_execution_health_missing_or_invalid" for f in audit.findings)


def test_audit_call_has_no_invented_sol_or_cm_model_when_legacy_declarations_are_absent(tmp_path):
    audit = Audit(tmp_path)
    entry = {"run_id": "legacy", "condition": "solo", "instance": {"instance_id": "task"}}
    record = {"call_id": "legacy-call", "run_id": "legacy", "task_id": "task", "role": "lead",
              "model_requested": "gpt-5.6-terra", "error": "offline transport fixture"}
    path = tmp_path / "results/legacy/calls" / hashlib.sha256(b"legacy-call").hexdigest() / "metadata.json"
    audit.call(path, record, entry)
    assert not any(f["code"] == "fixed_role_model_mismatch" for f in audit.findings)


def test_audit_binds_schema12_worktree_result_to_saved_observation(tmp_path):
    run = tmp_path / "run"
    state = {"state_sha256": "fixture-hash", "current_nonempty_delta": True,
             "observed_persisted_change": True}
    snapshot = {"schema_version": 12, "actor": "lead", "call_id": "call", "ordinal": 2,
                "origin": "tool", **state}
    write_json(run / "lead/worktree/00001.json", snapshot)
    event = {"event": "worktree_observed", "actor": "lead", "evidence_path": "lead/worktree/00001.json", **state}
    (run / "events.jsonl").write_text(json.dumps(event) + "\n")
    result = {"schema_version": 12, "lead_worktree": dict(state), "workers": []}
    audit = Audit(tmp_path)
    verified = audit.worktree_evidence(run, result)
    assert verified["observation_events"] == 1 and audit.findings == []
    result["lead_worktree"]["observed_persisted_change"] = False
    audit.worktree_evidence(run, result)
    assert any(f["code"] == "result_worktree_observation_mismatch" for f in audit.findings)
    assert audit.worktree_evidence(run, {"lead_edit_detected": True}) is None


def test_known_legacy_null_worker_pool_is_warned_without_rewriting_raw(tmp_path):
    manifest = fixture(tmp_path, schema=None)
    path = tmp_path / "results/run-0/result.json"
    result = json.loads(path.read_text())
    result["worker_pool"] = [None]
    write_json(path, result)
    before = path.read_bytes()
    summary = reporting.report(tmp_path)
    assert summary["rows"][0]["worker_pool"] == [None]
    assert any(f["code"] == "legacy_worker_pool_missing_models" for f in summary["findings"])
    audit = Audit(tmp_path)
    audit.identity(path.parent, manifest["schedule"][0], result, manifest)
    assert [(f["severity"], f["code"]) for f in audit.findings] == [("warning", "legacy_worker_pool_missing_models")]
    assert path.read_bytes() == before


def test_legacy_fixed_model_pool_is_distinct_candidates_not_worker_count(tmp_path):
    entry = {"run_id": "old", "condition": "fixed_team", "worker_model": "glm-5.3"}
    result = {"worker_pool": ["glm-5.3"]}
    routes = reporting.validate_result_routes(entry, result, {})
    assert routes["worker_models"] == ["glm-5.3", "glm-5.3"]
    assert routes["compatibility_warnings"] == []
    assert routes["worker_pool_semantics"] == "legacy_model_pool"


def test_known_legacy_sol_label_preserves_declared_and_observed_lead_routes(tmp_path):
    manifest = fixture(tmp_path, schema=None, lead="gpt-5.6-terra")
    path = tmp_path / "results/run-0/result.json"
    result = json.loads(path.read_text())
    result["lead_model"] = "gpt-5.6-sol"
    write_json(path, result)
    add_call(tmp_path, manifest["schedule"][0], "lead", "gpt-5.6-terra")
    summary = reporting.report(tmp_path)
    row = summary["rows"][0]
    assert row["lead_model"] == "gpt-5.6-sol"  # Retain the original label as evidence.
    assert row["declared_lead_model"] == "gpt-5.6-terra"
    assert row["observed_requested_lead_models"] == ["gpt-5.6-terra"]
    assert any(f["code"] == "legacy_hardcoded_lead_model_label" for f in summary["findings"])
    result["schema_version"] = 12
    with pytest.raises(ValueError, match="lead_model"):
        reporting.validate_result_routes(manifest["schedule"][0], result, manifest)
