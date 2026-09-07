from copy import deepcopy
import json
from pathlib import Path

import pytest

from modelbench.big_budget_team_20260906 import contracts


def inputs():
    official = Path(contracts.HERE).parent / "minimal_value_20260905" / "official"
    public = {row["instance_id"]: row for row in json.loads((official / "selected_public.json").read_text(encoding="utf-8"))}
    checks = json.loads((official / "public_checks.json").read_text(encoding="utf-8"))
    return public, checks


def example(condition="T11"):
    cell = next(row for row in contracts.load_cells() if row["condition_id"] == condition)
    public, checks = inputs()
    return contracts.build_entry(cell, public[cell["instance_id"]], checks[cell["instance_id"]],
                                 "offline-image@sha256:" + "a" * 64, {"scope": "offline-grader"})


def test_saved_matrix_produces_all_180_new_entries():
    plan, cells = contracts.load_plan(), contracts.load_cells()
    public, checks = inputs()
    entries = [contracts.build_entry(cell, public[cell["instance_id"]], checks[cell["instance_id"]],
                                     "offline-image", {"scope": "offline-grader"}, plan=plan) for cell in cells]
    assert contracts.validate_schedule(entries, plan=plan)
    assert sum(entry["effective_limits"]["token_limit"] for entry in entries) == 334800000
    assert sum(entry["effective_limits"]["max_calls"] for entry in entries) == 21672
    assert sum(entry["effective_limits"]["cm_call_allowance"] for entry in entries) == 8712
    assert {entry["condition_id"] for entry in entries} == set(contracts.CONDITIONS)
    assert all(entry["effective_limits"]["memory"] == "1g" and entry["effective_limits"]["cpus"] == 2 for entry in entries)
    assert all(entry["effective_limits"]["wall_seconds"] == 7200 for entry in entries)


@pytest.mark.parametrize("condition,token,calls,workers,cm", [
    ("T00", 600000, 28, 8, 12), ("T10", 2400000, 28, 8, 12),
    ("T01", 600000, 160, 48, 64), ("T11", 2400000, 160, 48, 64),
])
def test_token_and_call_bundle_are_independent(condition, token, calls, workers, cm):
    entry = example(condition)
    limits = entry["effective_limits"]
    assert [limits[k] for k in ("token_limit", "max_calls", "worker_calls", "cm_call_allowance")] == [token, calls, workers, cm]
    assert contracts.validate_entry(entry) == entry


@pytest.mark.parametrize("condition,lead,workers,base", [
    ("D00", "gpt-5.6-sol", ["glm-5.3"], "D"),
    ("F-S", "glm-5.3-flash", [], "S"),
    ("F-T", "glm-5.3-flash", ["glm-5.3-flash", "glm-5.3-flash"], "T"),
    ("S-FF", "gpt-5.6-sol", ["glm-5.3-flash", "glm-5.3-flash"], "T"),
])
def test_actual_model_roster_and_base_topology(condition, lead, workers, base):
    entry = example(condition)
    assert entry["condition_id"] == entry["strategy_id"] == condition
    assert entry["arm"] == entry["base_arm"] == base
    assert entry["lead_model"] == lead and entry["worker_models"] == workers
    assert entry["expected_workers"] == len(workers)


@pytest.mark.parametrize("key,value", [
    ("lead_model", "gpt-5.6-luna"), ("condition_id", "T00"), ("arm", "S"),
    ("expected_workers", 1), ("rep", 2), ("configuration_sha256", "0" * 64),
    ("run_id", "../../outside"), ("unexpected", "extra data"),
])
def test_entry_drift_rejected_even_with_recomputed_hash(key, value):
    entry = example()
    entry[key] = value
    if key != "configuration_sha256":
        entry["configuration_sha256"] = contracts.digest({k: v for k, v in entry.items() if k != "configuration_sha256"})
    with pytest.raises(ValueError):
        contracts.validate_entry(entry)


@pytest.mark.parametrize("field", ["token_limit", "worker_calls", "cm_call_allowance", "memory", "cm_edit_curfew"])
def test_effective_limits_cannot_drift(field):
    entry = example()
    entry["effective_limits"][field] = "3g" if field == "memory" else 1
    with pytest.raises(ValueError):
        contracts.validate_entry(entry)


@pytest.mark.parametrize("field", sorted(contracts.FORBIDDEN))
def test_hidden_candidate_fields_rejected(field):
    cell = contracts.load_cells()[0]
    public, checks = inputs()
    instance = deepcopy(public[cell["instance_id"]])
    instance[field] = "hidden material"
    with pytest.raises(ValueError, match="public fields"):
        contracts.build_entry(cell, instance, checks[cell["instance_id"]])


def test_cell_and_plan_changes_fail_before_execution(tmp_path):
    cell = contracts.load_cells()[0]
    public, _ = inputs()
    cell["max_calls"] = 999
    with pytest.raises(ValueError, match="unchanged member"):
        contracts.build_entry(cell, public[cell["instance_id"]])
    copied = tmp_path / "cells.csv"
    copied.write_bytes((contracts.HERE / "planned_cells.csv").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source hash"):
        contracts.load_cells(copied)
    plan = contracts.load_plan()
    plan["arms"][0]["limits_override"]["max_calls"] = 999
    with pytest.raises(ValueError, match="budget drift"):
        contracts.load_cells(plan=plan)


def test_duplicate_missing_and_unequal_public_inputs_rejected():
    public, checks = inputs()
    entries = [contracts.build_entry(cell, public[cell["instance_id"]], checks[cell["instance_id"]])
               for cell in contracts.load_cells()]
    with pytest.raises(ValueError, match="180"):
        contracts.validate_schedule(entries[:-1])
    repeated = deepcopy(entries)
    repeated[-1] = repeated[0]
    with pytest.raises(ValueError, match="ordering"):
        contracts.validate_schedule(repeated)
    changed = deepcopy(entries)
    changed[1]["public_checks"] = {"public_other": "different command"}
    changed[1]["configuration_sha256"] = contracts.digest({k: v for k, v in changed[1].items() if k != "configuration_sha256"})
    with pytest.raises(ValueError, match="unequal public"):
        contracts.validate_schedule(changed)
