import importlib
import importlib.util
import sys
import json
from pathlib import Path

import pytest

from modelbench.minimal_value_20260905.operations import a2_recovery_accounting as audit


def put(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class FakeReporting:
    __file__ = __file__

    @staticmethod
    def accounting(directory, card):
        calls = []
        for path in Path(directory).rglob("metadata.json"):
            value = json.loads(path.read_text("utf-8"))
            calls.append(value | {"metadata_path": str(path), "metadata_sha256": audit.sha(path),
                                  "api_equivalent_usd": .25})
        return {"calls": calls, "cost_computable": True, "protocol_issues": [],
                "api_equivalent_known_subtotal_usd": len(calls) * .25,
                "total_tokens_known_subtotal": sum(c["total_tokens"] for c in calls)}

    @staticmethod
    def audit_accounting(*args):
        return {"passed": True}

    @staticmethod
    def result_status(*args):
        return {}

    @staticmethod
    def stop_reason(*args):
        return None


def attempt(directory, events_path, run_id, count=1, pending=0, reserved=38435, nested=False):
    tickets, events = {}, []
    owner = run_id + "__candidate_1" if nested else run_id
    for i in range(count + pending):
        cid = run_id + "-call-" + str(i)
        target = directory / "results" / owner if nested else directory
        folder = target / "calls" / cid
        record = {"call_id": cid, "run_id": owner, "model_requested": "gpt-5.6-sol",
                  "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        put(folder / "started.json", record)
        event = {"run_id": owner, "call_id": cid, "model": "gpt-5.6-sol", "event": "transport_entered"}
        events.append(event)
        if i < count:
            put(folder / "metadata.json", record)
            events.append(event | {"event": "transport_returned"})
            tickets[cid] = {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                               "total_tokens": 15}, "reserved_tokens": 20}
        else:
            tickets[cid] = {"status": "reserved", "reserved_tokens": reserved}
    put(directory / "episode-budget.json", {"root": {"tickets": tickets, "frozen": not pending},
                                             "persistence_error": None})
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_reporting", lambda: FakeReporting)
    directory = tmp_path / "attempt"
    events = tmp_path / "events.jsonl"
    pricing = tmp_path / "pricing.json"
    put(pricing, {})
    return directory, events, pricing


def check(local):
    directory, events, pricing = local
    return audit.audit_new_attempt(directory, [events], run_id="run", pricing_path=pricing)


def test_complete_attempt_is_deterministic_and_read_only(local):
    d, e, p = local
    attempt(d, e, "run", nested=True)
    before = {str(x): audit.sha(x) for x in d.rglob("*") if x.is_file()}
    result = check(local)
    assert result == check(local)
    assert result["passed"] and result["new_unknown_calls"] == []
    assert result["counts"] == dict.fromkeys(("entered", "returned", "started", "metadata", "tickets"), 1)
    assert before == {str(x): audit.sha(x) for x in d.rglob("*") if x.is_file()}


def test_unknown_call_is_not_zero_or_refunded(local):
    d, e, p = local
    attempt(d, e, "run", pending=1)
    result = check(local)
    assert not result["passed"]
    assert result["new_unknown_calls"] == ["run-call-1"]
    assert result["known_api_equivalent_usd"] == .25
    assert result["reserved_tokens"] == 38435
    assert not result["automatic_retry_allowed"]


def test_other_live_episode_and_its_partial_log_are_ignored(local):
    d, e, p = local
    attempt(d, e, "run")
    other = e.parent / "other.jsonl"
    other.write_text(json.dumps({"event": "transport_entered", "run_id": "other", "call_id": "other-call"})
                     + '\n{"unfinished":', encoding="utf-8")
    result = audit.audit_new_attempt(d, [e, other], run_id="run", pricing_path=p)
    assert result["passed"]
    assert str(other) not in result["references"]


@pytest.mark.parametrize("defect", ["no_entered", "no_returned", "no_started", "no_budget", "duplicate_event",
                                    "duplicate_metadata", "usage", "model", "malformed_owned", "new_foreign_id"])
def test_new_coverage_or_usage_defects_fail_closed(local, defect):
    d, e, p = local
    attempt(d, e, "run")
    rows = [json.loads(x) for x in e.read_text().splitlines()]
    if defect == "no_entered": rows = rows[1:]
    if defect == "no_returned": rows = rows[:1]
    if defect == "duplicate_event": rows.append(rows[0])
    if defect == "model": rows[0]["model"] = "other"
    if defect == "new_foreign_id": rows.append(rows[0] | {"call_id": "unbudgeted"})
    e.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
    if defect == "malformed_owned":
        with e.open("a") as f: f.write('{"partial":')
    if defect == "no_started": next(d.rglob("started.json")).unlink()
    if defect == "no_budget": (d / "episode-budget.json").unlink()
    if defect == "duplicate_metadata": put(d / "duplicate/metadata.json", json.loads(next(d.rglob("metadata.json")).read_text()))
    if defect == "usage":
        bp = d / "episode-budget.json"
        b = json.loads(bp.read_text()); b["root"]["tickets"]["run-call-0"]["usage"]["total_tokens"] = 16
        put(bp, b)
    assert not check(local)["passed"]


@pytest.fixture
def carry(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_reporting", lambda: FakeReporting)
    batch, incident, group = tmp_path / "old", tmp_path / "incident", tmp_path / "group"
    schedule = [{"run_id": f"r-{i:02}-{arm}", "arm": arm,
                 "instance": {"instance_id": f"task-{i}", "base_commit": "base"},
                 "limits_override": {"token_limit": 600000}}
                for i in range(16) for arm in ("D", "L", "R2", "S", "T")]
    snapshot = batch / "runtime_snapshot"
    relative = "modelbench/minimal_value_20260905/reporting.py"
    source = snapshot / relative
    source.parent.mkdir(parents=True); source.write_bytes(Path(__file__).read_bytes())
    pricing = snapshot / "modelbench/minimal_value_20260905/pricing.json"
    put(pricing, {})
    inp = snapshot / "modelbench/minimal_value_20260905/official/input.json"
    put(inp, {})
    manifest = {"stage": "a2", "scheduled_episodes": 80, "schedule": schedule,
                "runtime_sources": {relative: audit.sha(source)}, "input_artifacts": {"input.json": audit.sha(inp)}}
    put(batch / "manifest.json", manifest)
    (batch / "manifest.sha256").write_text(audit.sha(batch / "manifest.json"))
    valid = []
    for entry in schedule[:12]:
        rid = entry["run_id"]; d = batch / "results" / rid
        attempt(d, tmp_path / "valid-events" / (rid + ".jsonl"), rid)
        patch = d / "model.patch"; patch.write_text("patch")
        grader = d / "grade"; rp = grader / "report.json"
        put(rp, {entry["instance"]["instance_id"]: {"resolved": False}})
        result = {"run_id": rid, "arm": entry["arm"], "instance_id": entry["instance"]["instance_id"],
                  "quiesced": True, "cleanup_confirmed": True,
                  "artifact": {"path": str(patch), "sha256": audit.sha(patch)},
                  "score": {"completed": True, "resolved": False, "instance_id": entry["instance"]["instance_id"],
                            "base_commit": "base", "patch_sha256": audit.sha(patch), "grader_dir": str(grader),
                            "reports": ["report.json"], "reports_sha256": {"report.json": audit.sha(rp)}}}
        put(d / "episode_result.json", result)
        valid.append({"run_id": rid, "path": str(d / "episode_result.json"), "sha256": audit.sha(d / "episode_result.json")})
    known = (12 + 45) * .25
    monkeypatch.setattr(audit, "OLD_KNOWN_USD", known)
    state = {"completed_episodes": 12, "episodes": valid, "active_episodes": {}, "completed_at": "done",
             "stop_reason": "parallel_trial_failed", "token_admission_sum": 10800000, "known_cost_usd": known}
    put(batch / "state.json", state)
    put(group / "group.json", {"manifest_sha256": audit.sha(batch / "manifest.json")})
    put(group / "trial-state.json", {"cleanup_confirmed": True, "active_episodes": {}})
    rows, unknown, event_hashes = [], [], {}
    for idx, (entry, count) in enumerate(zip(schedule[12:18], (5, 6, 6, 12, 6, 10))):
        rid = entry["run_id"]; d = batch / "results" / rid
        reserve = {2: 38435, 3: 42982, 5: 36970}.get(idx, 0)
        ep = group / "provider-events" / (rid + ".jsonl")
        attempt(d, ep, rid, count=count, pending=int(bool(reserve)), reserved=reserve)
        account = FakeReporting.accounting(d, {})
        sp = group / "settlements" / (rid + ".json")
        put(sp, {"cleanup_confirmed": True, "engineering_valid": False, "accounting": account})
        rows.append({"run_id": rid, "settlement_sha256": audit.sha(sp),
                     "budget_sha256": audit.sha(d / "episode-budget.json"), "metadata": count,
                     "entered": count + bool(reserve), "reserved_tokens": reserve,
                     "known_tokens": count * 15, "known_api_equivalent_usd": count * .25})
        event_hashes[ep.relative_to(group).as_posix()] = audit.sha(ep)
        if reserve:
            cid = rid + "-call-" + str(count); started = d / "calls" / cid / "started.json"
            unknown.append({"run_id": rid, "call_id": cid, "reserved_tokens": reserve,
                            "started_path": started.relative_to(batch).as_posix(), "started_sha256": audit.sha(started)})
    value = {"batch_dir": str(batch), "group_dir": str(group), "group_sha256": audit.sha(group / "group.json"),
             "runs": rows, "unresolved_calls": unknown, "provider_event_sha256": event_hashes,
             "totals": {"metadata_records": 45, "entered_calls": 48}}
    settlements = {p.stem: json.loads(p.read_text()) for p in (group / "settlements").glob("*.json")}
    state["parallel_trial"] = {"group_dir": str(group), "settlements": settlements}
    put(batch / "state.json", state)
    put(group / "trial-state.json", {"cleanup_confirmed": True, "active_episodes": {}, "settlements": settlements})
    put(incident / "ACCOUNTING_AUDIT.json", value)
    monkeypatch.setattr(audit, "INCIDENT_AUDIT_SHA256", audit.sha(incident / "ACCOUNTING_AUDIT.json"))
    return batch, incident, group


def test_carryover_preserves_invalid_unknown_and_authorized_caps(carry):
    batch, incident, group = carry
    value = audit.build_carryover(batch, incident_dir=incident)
    assert value == audit.build_carryover(batch, incident_dir=incident)
    assert value["carried_valid"] == value["carried_valid12"] == json.loads((batch / "state.json").read_text())["episodes"]
    assert len(value["carried_valid12"]) == 12 and value["old_infra_invalid_count"] == 6
    assert len(value["legacy_unknown_calls"]) == 3 and value["legacy_unknown_reserved_tokens"] == 118387
    assert value["legacy_token_admission_sum"] == 10800000
    assert value["new_attempt_count"] == len(value["remaining_original_entries"]) == 68
    assert value["combined_admission_cap"] == 51600000
    assert not value["overall_cost_computable"] and not value["additional_automatic_retry_allowed"]


@pytest.mark.parametrize("defect", ["refund", "result", "source", "official_report", "audit", "settlement", "state_settlement", "unknown_started"])
def test_carryover_rejects_changed_old_evidence(carry, defect):
    batch, incident, group = carry
    if defect == "refund":
        p = batch / "state.json"; s = json.loads(p.read_text()); s["token_admission_sum"] = 7200000; put(p, s)
    elif defect == "result": next((batch / "results").rglob("episode_result.json")).write_text("{}")
    elif defect == "source": next((batch / "runtime_snapshot").rglob("reporting.py")).write_text("changed")
    elif defect == "official_report": next((batch / "results").rglob("report.json")).write_text("{}")
    elif defect == "audit": (incident / "ACCOUNTING_AUDIT.json").write_text("{}")
    elif defect == "settlement": next((group / "settlements").glob("*.json")).write_text("{}")
    elif defect == "state_settlement":
        p = batch / "state.json"; s = json.loads(p.read_text()); s["parallel_trial"]["settlements"] = {}; put(p, s)
    else:
        info = json.loads((incident / "ACCOUNTING_AUDIT.json").read_text())["unresolved_calls"][0]
        (batch / info["started_path"]).write_text("{}")
    with pytest.raises((audit.RecoveryAccountingError, KeyError)):
        audit.build_carryover(batch, incident_dir=incident)


@pytest.mark.parametrize("module_name", ["_prepare_recovery_accounting", "_a2_recovery_evidence"])
def test_standalone_loader_reuses_selected_reporting_without_path_mutation(module_name):
    selected = importlib.import_module("modelbench.minimal_value_20260905.reporting")
    before = list(sys.path)
    spec = importlib.util.spec_from_file_location(module_name, audit.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._reporting() is selected
    assert sys.path == before
    assert sys.modules["modelbench.minimal_value_20260905.reporting"] is selected
