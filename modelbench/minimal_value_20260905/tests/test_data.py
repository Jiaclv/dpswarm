import json
from collections import Counter

import pytest

from modelbench.minimal_value_20260905 import data


def population():
    return [{"instance_id": f"org{i}__repo-{j}", "repo": f"org{i}/repo"}
            for i in range(8) for j in range(6)]


def test_selection_is_deterministic_balanced_and_excludes_exposure():
    rows = population()
    exposed = {row["instance_id"] for row in rows[:4]}
    result = data.select_metadata(rows, exposed)
    assert result == data.select_metadata(list(reversed(rows)), exposed)
    assert len(result) == 16
    counts = Counter(row["repo"] for row in result)
    assert len(counts) >= 6 and max(counts.values()) <= 3
    assert not exposed.intersection(row["instance_id"] for row in result)


def test_selection_does_not_read_private_fields():
    class PublicOnly(dict):
        def __getitem__(self, key):
            if key not in {"repo", "instance_id"}:
                raise AssertionError("attempt to read non-selection data")
            return super().__getitem__(key)

    assert len(data.select_metadata([PublicOnly(row) for row in population()])) == 16


def test_selection_rejects_insufficient_strata_and_duplicate_ids():
    with pytest.raises(ValueError, match="strata"):
        data.select_metadata(population()[:30])
    with pytest.raises(ValueError, match="duplicate"):
        data.select_metadata(population() + [population()[0]])
    with pytest.raises(ValueError, match="too few unexposed instances"):
        data.select_metadata(population(), count=25)


def test_frozen_json_cannot_be_overwritten(tmp_path):
    path = tmp_path / "selection.json"
    data.freeze_json(path, {"ids": [1, 2]})
    original = path.read_bytes()
    data.freeze_json(path, {"ids": [1, 2]})
    with pytest.raises(ValueError, match="frozen artifact"):
        data.freeze_json(path, {"ids": [3]})
    assert path.read_bytes() == original


def test_historical_exposure_reads_public_metadata_and_task_paths(tmp_path):
    old = tmp_path / "swe_fixed_team_20260903"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps({"schedule": [{"instance": {"instance_id": "org__repo-42"}}]}))
    (old / "org__repo-77__solo").mkdir()
    # Deliberately invalid/private files must not be opened by the scanner.
    (old / "selected.json").write_text("PRIVATE NOT JSON")
    (old / "keys.local.json").write_text("NOT JSON")
    result = data.exposure_inventory(tmp_path)
    assert result["instance_ids"] == ["org__repo-42", "org__repo-77"]
    assert len(result["sources"]) == 1


def test_load_public_keeps_frozen_stage_order(tmp_path):
    data.freeze_json(tmp_path / "selection.json", {"a1": ["b", "a"], "d1": ["c"]})
    data.freeze_json(tmp_path / "selected_public.json", [{"instance_id": value} for value in "abc"])
    assert [row["instance_id"] for row in data.load_public("a1", tmp_path)] == ["b", "a"]
    assert data.load_public("a2", tmp_path) == [{"instance_id": "c"}]

def test_qualification_rejects_missing_tests_as_apparent_baseline_failure():
    baseline = {"completed": True, "resolved": False, "counts": {"parsed": True,
        "FAIL_TO_PASS": {"total": 1, "missing": 0, "passed": 0, "failed": 1},
        "PASS_TO_PASS": {"total": 2, "missing": 0, "passed": 2, "failed": 0}}}
    reference = {"completed": True, "resolved": True, "counts": {"parsed": True,
        "FAIL_TO_PASS": {"total": 1, "missing": 0, "passed": 1, "failed": 0},
        "PASS_TO_PASS": {"total": 2, "missing": 0, "passed": 2, "failed": 0}}}
    assert data.qualification_passes(baseline, reference)
    baseline["counts"]["FAIL_TO_PASS"].update(missing=1, failed=0)
    assert not data.qualification_passes(baseline, reference)
