import json
from pathlib import Path

import pytest

from modelbench.minimal_value_20260905 import reporting as r
from modelbench.minimal_value_20260905.budget import RunBudget
from modelbench.minimal_value_20260905.contracts import ARMS, schedule, validate_schedule


def rows():
    return [{"arm": arm, "instance_id": f"task-{i}", "repo": f"repo-{i % 8}",
             "end_to_end_success": int(i < 8), "api_equivalent_usd": 1.,
             "inference_wall_seconds": 100.} for arm in ARMS for i in range(16)]


def test_quality_qualifies_each_team_before_selecting():
    values = rows()
    for row in values:
        if row["arm"] in ("D", "T") and row["instance_id"] in ("task-8", "task-9"):
            row["end_to_end_success"] = 1
        if row["arm"] == "T":
            row["api_equivalent_usd"] = 2.
    selected = r.choose(values)
    assert selected["C_star"] == "D"
    assert selected["qualifications"] == {"D": "quality", "T": "not_qualified"}


def test_unknown_cost_skips_whole_solo_tiebreak_layer():
    values = rows()
    for row in values:
        if row["arm"] == "L":
            row.update(api_equivalent_usd=None, inference_wall_seconds=50.)
        if row["arm"] in ("D", "T"):
            row["inference_wall_seconds"] = 50.
    selection = r.choose(values)
    assert selection["S_star"] == "L"
    assert selection["status"] == "undecidable"


def test_zero_success_cannot_advance_on_cheap_failure():
    values = rows()
    for row in values:
        row["end_to_end_success"] = 0
        if row["arm"] in ("D", "T"):
            row["api_equivalent_usd"] = .1
    assert r.choose(values)["status"] == "not_qualified"


@pytest.mark.parametrize("change", ["duplicate", "unpaired", "repo"])
def test_task_pairing_is_required(change):
    values = rows()
    target = next(row for row in values if row["arm"] == "D")
    target["instance_id" if change != "repo" else "repo"] = {
        "duplicate": "task-1", "unpaired": "elsewhere", "repo": "elsewhere"}[change]
    assert r.choose(values)["status"] == "unpaired_or_duplicate_tasks"


def card():
    return r.read(Path(r.__file__).with_name("pricing.json"))


def test_cost_uses_inclusive_output_once_and_cache_splits():
    call = {"model_requested": "gpt-5.6-sol", "input_tokens": 1000,
            "cached_input_tokens": 200, "cache_write_input_tokens": 100,
            "output_tokens": 50, "reasoning_tokens": 40, "service_tier_requested": "fast"}
    quoted = r.quote(call, card())
    assert quoted["api_equivalent_usd"] == pytest.approx((700 * 8 + 200 * .8 + 100 * 10 + 50 * 40) / 1e6)
    call.pop("cache_write_input_tokens")
    assert r.quote(call, card())["cost_status"] == "cache_write_split_unknown"


def settled(tmp_path):
    record = {"call_id": "c1", "role": "lead", "model_requested": "glm-5.3",
              "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20,
              "total_tokens": 120}
    folder = tmp_path / "calls" / "c1"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps(record), encoding="utf-8")
    budget = RunBudget(28, 600000, cm_call_allowance=12)
    budget.reserve("ticket1", "lead", 4096)
    budget.complete("ticket1", record)
    budget.freeze()
    (tmp_path / "budget.json").write_text(json.dumps(budget.snapshot()), encoding="utf-8")
    return {"budget": budget.summary()}, folder


def test_audit_matches_root_tickets_and_metadata(tmp_path):
    result, _ = settled(tmp_path)
    assert r.audit_accounting(tmp_path, result, r.accounting(tmp_path, card()))["passed"]


def test_deleting_metadata_cannot_make_call_free(tmp_path):
    result, folder = settled(tmp_path)
    (folder / "metadata.json").unlink()
    with pytest.raises(ValueError, match="call IDs differ"):
        r.audit_accounting(tmp_path, result, r.accounting(tmp_path, card()))


def test_mutating_metadata_cannot_change_settled_cost(tmp_path):
    result, folder = settled(tmp_path)
    path = folder / "metadata.json"
    record = r.read(path)
    record.update(input_tokens=0, output_tokens=0, total_tokens=0)
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="settled call record"):
        r.audit_accounting(tmp_path, result, r.accounting(tmp_path, card()))


def test_unknown_price_is_separate_from_solution_correctness(tmp_path):
    patch = tmp_path / "patch.diff"
    patch.write_text("example patch", encoding="utf-8")
    result = {"artifact": {"path": str(patch), "sha256": r.sha(patch)}, "cleanup_confirmed": True,
              "budget": {"unknown_call_count": 0, "pending_call_count": 0},
              "score": {"completed": True, "resolved": True}, "api_equivalent_usd": None}
    assert r.result_status(result, {"expected_workers": 0})["end_to_end_success"] == 1


def test_balanced_contracts_reject_private_fields_and_drift():
    public = [{"instance_id": f"task-{i}", "repo": f"repo-{i % 8}", "base_commit": "base",
               "problem_statement": "public issue", "version": "1"} for i in range(16)]
    entries = schedule("a2", public)
    assert validate_schedule(entries, "a2")
    assert all(e["limits_override"]["token_limit"] == 600000 for e in entries)
    entries[0]["limits_override"]["max_calls"] += 1
    with pytest.raises(ValueError, match="drift"):
        validate_schedule(entries, "a2")
    public[0]["patch"] = "PRIVATE"
    with pytest.raises(ValueError, match="public fields"):
        schedule("a2", public)
