import copy
import json
from pathlib import Path

import pytest

from modelbench.minimal_value_20260905.operations import a1_engineering_gate as gate


def ticket(name, role, reserved, actual, start, end):
    return {"ticket_id": name, "call_id": name, "role": role, "status": "completed",
            "reserved_tokens": reserved, "reserved_at": start, "completed_at": end,
            "usage": {"total_tokens": actual}}


def snapshot(tickets, scopes=None, owners=None):
    return {"root": {"frozen": True, "created_at": 0, "deadline_at": 1800,
                "max_calls": 28, "token_limit": 600000, "cm_call_allowance": 12,
                "tickets": {t["ticket_id"]: t for t in tickets}},
            "scopes": scopes or {"solver": {"max_calls": 28, "token_limit": 600000, "cm_call_allowance": 12}},
            "ticket_scopes": owners or {t["ticket_id"]: "solver" for t in tickets},
            "persistence_error": None}


def test_actual_final_overshoot_is_kept_and_legal_admission_passes():
    value = snapshot([ticket("old", "lead", 532200, 532200, 1, 2),
                      ticket("last", "lead", 56682, 69916, 3, 4)])
    result = gate.admission_replay(value)
    assert result["total_tokens"] == 602116
    assert result["root_overshoot_tokens"] == 2116
    assert result["tickets"][-1]["levels"]["root"]["committed_after_reserve"] == 588882


def test_overshoot_must_block_a_later_provider_admission():
    value = snapshot([ticket("old", "lead", 532200, 532200, 1, 2),
                      ticket("last", "lead", 56682, 69916, 3, 4),
                      ticket("illegal", "lead", 1, 1, 5, 6)])
    with pytest.raises(gate.GateError, match="Token quota"):
        gate.admission_replay(value)


def test_pending_reservations_count_at_concurrent_admission():
    value = snapshot([ticket("a", "lead", 400000, 100, 1, 5),
                      ticket("b", "lead", 210000, 100, 2, 6)])
    with pytest.raises(gate.GateError, match="Token quota"):
        gate.admission_replay(value)


def test_component_quota_cannot_borrow_unused_root_or_sibling():
    value = snapshot([ticket("s1", "selector", 50000, 59000, 1, 2),
                      ticket("s2", "selector", 2000, 100, 3, 4)],
        {"selector": {"max_calls": 4, "token_limit": 60000, "cm_call_allowance": 0}},
        {"s1": "selector", "s2": "selector"})
    with pytest.raises(gate.GateError, match="selector"):
        gate.admission_replay(value)


@pytest.mark.parametrize("defect", ["pending", "unknown", "deadline", "frozen", "persistence", "cm"])
def test_budget_contract_defects_reject(defect):
    value = snapshot([ticket("a", "lead", 1, 1, 1, 2)])
    item = value["root"]["tickets"]["a"]
    if defect == "pending": item["status"] = "reserved"
    if defect == "unknown": item["usage"]["total_tokens"] = None
    if defect == "deadline": item.update(reserved_at=1800, completed_at=1801)
    if defect == "frozen": value["root"]["frozen"] = False
    if defect == "persistence": value["persistence_error"] = "disk failure"
    if defect == "cm":
        item["role"] = "cm"
        value["scopes"]["solver"]["cm_call_allowance"] = 0
    with pytest.raises(gate.GateError): gate.admission_replay(value)


def call():
    return {"call_id": "c", "error": None, "transport_attempt_count": 1, "attempt_count": 1,
            "retry_attempted": False, "reconnect_detected": False, "history_continuation_safe": True,
            "protocol_error": {"code": "schema_validation_failed", "message": "timeout out of range"}}


def test_rejected_model_output_is_retained_without_whole_stage_failure():
    result = gate.protocol_audit([call()], [])
    assert result["rejected_model_outputs"][0]["classification"] == "rejected_model_output"
    assert result["rejected_model_outputs"][0]["usage_charged"] is True


@pytest.mark.parametrize("defect", ["executed", "unsafe", "transport", "retry", "new_protocol"])
def test_real_protocol_or_accounting_defect_is_not_whitelisted(defect):
    row, events = call(), []
    if defect == "executed": events = [{"event": "tool_started", "call_id": "c"}]
    if defect == "unsafe": row["history_continuation_safe"] = False
    if defect == "transport": row["error"] = {"code": "unauthorized"}
    if defect == "retry": row["retry_attempted"] = True
    if defect == "new_protocol": row["protocol_error"]["code"] = "unknown_envelope"
    with pytest.raises(gate.GateError): gate.protocol_audit([row], events)


def test_batch_coverage_accepts_blocked_second_task_and_never_counts_it_as_success():
    rows = [{"run_id": "d1", "arm": "D", "adopted_roles": ["test"]},
            {"run_id": "t1", "arm": "T", "adopted_roles": ["implementation", "test"]},
            {"run_id": "t2", "arm": "T", "adopted_roles": [], "official_resolved": False},
            {"run_id": "r1", "arm": "R2", "r2_isolation": True, "verified_selection": True},
            {"run_id": "r2", "arm": "R2", "r2_isolation": True, "budget_fallback": True}]
    result = gate.coverage_check(rows)
    assert all(x["status"] == "observed" for x in result.values())
    assert result["T_both_roles_handoff_adopt"]["run_ids"] == ["t1"]
    assert rows[2]["official_resolved"] is False


def test_all_fallback_or_no_adoption_does_not_cover_necessary_path():
    result = gate.coverage_check([{"run_id": "r", "arm": "R2", "budget_fallback": True}])
    assert result["R2_verified_selection"]["status"] == "not-covered"
    assert result["T_both_roles_handoff_adopt"]["status"] == "not-covered"


def test_cleanup_claim_requires_actual_matching_close_evidence():
    result = {"quiesced": True, "cleanup_confirmed": True,
              "quiescence": {"lead": {"quiesced": True, "candidate_execution_fenced": True,
                    "stopped_running": False, "stopped_pid": 0, "container_id": "owned"}},
              "environment_closures": {"lead": {"closed": True, "removed": True,
                    "container_id": "owned", "errors": []}}}
    assert gate.closure_check(result)["passed"]
    result["environment_closures"]["lead"]["removed"] = False
    with pytest.raises(gate.GateError): gate.closure_check(result)


def test_immutable_evidence_detects_patch_change_and_outside_path(tmp_path):
    batch = tmp_path / "batch"; batch.mkdir()
    path = batch / "patch"; path.write_text("old")
    evidence = gate.Evidence(batch)
    evidence.bind(path)
    path.write_text("changed")
    with pytest.raises(gate.GateError): evidence.unchanged()
    outside = tmp_path / "outside"; outside.write_text("data")
    with pytest.raises(gate.GateError): evidence.bind(outside)


def test_new_report_is_not_allowed_to_overwrite_previous_report(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "evaluate", lambda *a, **k: {"passed": False, "failures": ["not covered"], "coverage": {}})
    path = tmp_path / "existing-directory" / "a1-engineering-gate.json"
    path.parent.mkdir()
    result = gate.write_report(tmp_path, report_path=path)
    saved = path.read_bytes()
    assert result["passed"] is False
    with pytest.raises(gate.GateError): gate.write_report(tmp_path, report_path=path)
    assert path.read_bytes() == saved


def test_compatibility_validator_returns_stable_evidence_and_does_not_install_itself(monkeypatch, tmp_path):
    from modelbench.minimal_value_20260905 import cli
    original = cli.validate_a1
    expected = {"passed": True, "revision": gate.REVISION, "report_sha256": "fixed"}
    calls = []
    def verify(path, batch, **kwargs):
        calls.append((path, batch, kwargs))
        return copy.deepcopy(expected)
    monkeypatch.setattr(gate, "verify_report", verify)
    assert gate.validate_a1(tmp_path, expected_sources={"a": "b"}) == gate.validate_a1(tmp_path, expected_sources={"a": "b"})
    assert calls[0] == calls[1]
    assert cli.validate_a1 is original
