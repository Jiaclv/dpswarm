"""Offline contract checks for the isolated, model-free salvage operator."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "salvage_scoring.py"
spec = importlib.util.spec_from_file_location("_salvage_under_test", SOURCE)
salvage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(salvage)


def valid_result():
    return {"quiesced": True, "cleanup_confirmed": True,
            "patch_frozen_at": "2026-09-05T14:21:09.990300+00:00",
            "completed_at": "2026-09-05T14:21:10.817354+00:00"}


TRIP = "2026-09-05T14:21:10.716505+00:00"


def test_post_trip_bookkeeping_with_pre_trip_generation_is_eligible():
    result = valid_result()
    old = deepcopy(result)
    outcome = salvage.qualify_generation(result, {"passed": True}, [
        {"event": "transport_returned", "at": "2026-09-05T14:20:00+00:00"}], TRIP)
    assert outcome["eligible"] and outcome["post_trip_bookkeeping"]
    assert result == old


@pytest.mark.parametrize("change", [
    {"quiesced": False}, {"cleanup_confirmed": False},
    {"patch_frozen_at": "2026-09-05T14:21:10.716506+00:00"},
    {"infrastructure_error": {"message": "cancelled"}},
    {"execution_health": {"status": "host_error"}},
])
def test_incomplete_or_post_stop_generation_rejected(change):
    result = valid_result()
    result.update(change)
    with pytest.raises(ValueError):
        salvage.qualify_generation(result, {"passed": True}, [], TRIP)


def test_unknown_call_rejected_even_when_patch_exists():
    with pytest.raises(ValueError, match="accounting"):
        salvage.qualify_generation(valid_result(), {"passed": False}, [], TRIP)


def test_provider_call_after_trip_rejected():
    with pytest.raises(ValueError, match="Provider activity"):
        salvage.qualify_generation(valid_result(), {"passed": True}, [
            {"event": "transport_entered", "at": "2026-09-05T14:21:11+00:00"}], TRIP)


def test_timestamps_require_timezone():
    with pytest.raises(ValueError, match="timezone"):
        salvage.timestamp("2026-09-05T14:21:10")


def test_evidence_is_write_once_and_source_hashes_are_checked(tmp_path):
    path = tmp_path / "result.json"
    salvage.write(path, {"actual_tokens": None})
    original = salvage.sha(path)
    with pytest.raises(FileExistsError):
        salvage.write(path, {"actual_tokens": 0})
    assert salvage.read(path)["actual_tokens"] is None
    salvage.unchanged({str(path): original})
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Original evidence changed"):
        salvage.unchanged({str(path): original})


@pytest.mark.parametrize("resolved", [True, False])
def test_score_only_uses_one_gib_and_unresolved_is_valid(tmp_path, monkeypatch, resolved):
    directory = tmp_path / "results" / "a2-05-D"
    directory.mkdir(parents=True)
    patch = directory / "model.patch"
    patch.write_text("diff --git a/a b/a\n", encoding="utf-8")
    patch_sha = salvage.sha(patch)
    result = valid_result()
    result.update(artifact={"path": "old.patch", "sha256": patch_sha}, budget={"frozen": False})
    salvage.write(directory / "source-result.json", result)
    original_sha = salvage.sha(directory / "source-result.json")
    requests = []

    def rootgrader(*args, **kwargs):
        requests.append(kwargs)
        return {"completed": True, "resolved": resolved, "patch_sha256": patch_sha,
                "reports_sha256": {}, "grader_dir": str(directory / "grade-1")}

    monkeypatch.setitem(sys.modules, "modelbench.minimal_value_20260905.environment",
                        SimpleNamespace(rootgrade_terminal=rootgrader))
    monkeypatch.setattr(salvage, "reporting", lambda: SimpleNamespace(result_status=lambda r, e: {}))

    def grade_frozen(value, entry, where, *, grader):
        assert Path(value["artifact"]["path"]) == patch.resolve()
        return grader("task", patch, patch_sha, where, grader_contract={}), []

    item = {"run_id": "a2-05-D", "entry": {}, "source_result": "original.json",
            "source_result_sha256": "source_hash", "patch_sha256": patch_sha,
            "root_budget_snapshot": {"frozen": True}, "eligibility": {"eligible": True},
            "accounting_integrity": {"passed": True},
            "accounting": {"api_equivalent_known_subtotal_usd": 1.25,
                           "total_tokens_known_subtotal": 100}}
    record = salvage.score_one(SimpleNamespace(grade_frozen=grade_frozen), item, tmp_path)
    assert requests == [{"grader_contract": {}, "memory": "1g"}]
    assert record["official_resolved"] is resolved
    assert record["known_tokens"] == 100
    assert salvage.read(record["path"])["budget"]["frozen"] is True
    assert salvage.sha(directory / "source-result.json") == original_sha
    assert salvage.sha(patch) == patch_sha


def test_no_solver_launch_in_salvage_entrypoint():
    source = SOURCE.read_text(encoding="utf-8")
    assert "cli.episode(" not in source
    assert "component.run(" not in source
    assert "transport_factory" not in source
