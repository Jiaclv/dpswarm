from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from modelbench.minimal_value_20260905.operations import prepare_a2 as module
from modelbench.minimal_value_20260905 import cli, environment, freeze


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    operations, official = tmp_path / "operations", tmp_path / "official"
    operations.mkdir()
    official.mkdir()
    monkeypatch.setattr(module, "OPERATIONS", operations)
    monkeypatch.setattr(module, "OFFICIAL", official)
    monkeypatch.setattr(module, "STAGING", operations / "staging_a2")
    rows = [{"instance_id": f"org__repo-{i}", "repo": "org/repo", "base_commit": "a" * 40,
             "problem_statement": "Public issue " + str(i), "version": "1"} for i in range(18)]
    a1_ids, d1_ids = [row["instance_id"] for row in rows[:2]], [row["instance_id"] for row in rows[2:]]
    module.data.freeze_json(official / "selection.json", {"a1": a1_ids, "d1": d1_ids})
    module.data.freeze_json(official / "selected_public.json", rows)
    module.data.freeze_json(official / "public_checks.json", {i: {"old_check": "python -m pytest tests/test_public.py"} for i in a1_ids})
    module.data.freeze_json(official / "public_checks_provenance.json", {i: {"source": "frozen A1 public file"} for i in a1_ids})
    definitions, provenance = {}, {}
    for row in rows[2:]:
        iid = row["instance_id"]
        command = "python -m pytest tests/test_public.py -q"
        definitions[iid] = {"public_test": command}
        provenance[iid] = {"repo": row["repo"], "base_commit": row["base_commit"],
            "problem_statement_sha256": hashlib.sha256(row["problem_statement"].encode()).hexdigest(),
            "checks": {"public_test": {"command": command, "timeout_seconds": 120, "rationale": "public issue scope",
                "sources": [{"path": "tests/test_public.py", "sha256": "b" * 64,
                    "url": f"https://raw.githubusercontent.com/{row['repo']}/{row['base_commit']}/tests/test_public.py"}]}}}
    module.data.freeze_json(operations / "public_check_definitions.json", definitions)
    module.data.freeze_json(operations / "public_check_provenance.json", provenance)
    monkeypatch.setattr(environment, "_docker", lambda *a, **k: pytest.fail("offline test attempted Docker"))
    return {"operations": operations, "official": official, "rows": rows, "a1_ids": a1_ids, "d1_ids": d1_ids}


@pytest.fixture
def approved(inputs, monkeypatch, tmp_path):
    stage_result = module.stage_a2_inputs()
    evidence = {"passed": True, "batch": str(tmp_path / "a1"), "manifest_sha256": "c" * 64, "state_sha256": "d" * 64}
    manifest = {"resource_lock_path": str(tmp_path / "active-stage.lock")}
    monkeypatch.setattr(module, "_approved_a1", lambda _: (evidence, manifest, tmp_path / "snapshot"))
    monkeypatch.setattr(freeze, "verify_snapshot", lambda *a: tmp_path / "snapshot")
    return {**inputs, "evidence": evidence, "manifest": manifest, "stage": Path(stage_result["manifest_path"]).parent}


def test_staging_merges_18_public_tasks_without_changing_official(inputs):
    official = inputs["official"]
    before = {name: (official / name).read_bytes() for name in module.CHECK_FILES}
    result = module.stage_a2_inputs()
    assert result["staged"] and result["task_count"] == 16
    merged = module._read(Path(result["manifest_path"]).parent / "public_checks.json")
    assert set(merged) == {row["instance_id"] for row in inputs["rows"]}
    assert {name: (official / name).read_bytes() for name in module.CHECK_FILES} == before
    assert module.stage_a2_inputs() == result


def test_wrong_public_commit_is_rejected_before_staging(inputs):
    path = inputs["operations"] / "public_check_provenance.json"
    value = module._read(path)
    value[inputs["d1_ids"][0]]["base_commit"] = "f" * 40
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="frozen public task"):
        module.stage_a2_inputs()
    assert not (module.STAGING / "manifest.json").exists()


def test_stage_output_mutation_is_rejected(inputs):
    module.stage_a2_inputs()
    (module.STAGING / "public_checks.json").write_text("{}")
    with pytest.raises(ValueError, match="staged public checks changed"):
        module.stage_a2_inputs()


def test_publish_requires_completed_a1_before_mutating_official(inputs, monkeypatch):
    module.stage_a2_inputs()
    before = {name: (inputs["official"] / name).read_bytes() for name in module.CHECK_FILES}
    def denied(_):
        raise ValueError("A1 must finish")
    monkeypatch.setattr(module, "_approved_a1", denied)
    with pytest.raises(ValueError, match="A1 must finish"):
        module.publish_a2_inputs("unfinished-a1")
    assert {name: (inputs["official"] / name).read_bytes() for name in module.CHECK_FILES} == before


def test_publish_backs_up_a1_and_is_idempotent(approved):
    official, stage = approved["official"], approved["stage"]
    old = {name: (official / name).read_bytes() for name in module.CHECK_FILES}
    result = module.publish_a2_inputs("a1")
    assert result["published"]
    for name in module.CHECK_FILES:
        assert (stage / "publication" / "a1_backups" / name).read_bytes() == old[name]
        assert (official / name).read_bytes() == (stage / name).read_bytes()
    assert module.publish_a2_inputs("a1") == result


def test_partial_publish_is_uncommitted_then_resumes_only_recorded_hashes(approved, monkeypatch):
    original = module._atomic_bytes
    failed = False
    def interrupted(path, content):
        nonlocal failed
        if Path(path) == approved["official"] / "public_checks_provenance.json" and not failed:
            failed = True
            raise OSError("simulated second-file crash")
        original(path, content)
    monkeypatch.setattr(module, "_atomic_bytes", interrupted)
    with pytest.raises(OSError, match="second-file crash"):
        module.publish_a2_inputs("a1")
    assert (approved["stage"] / "publication" / "intent.json").exists()
    assert not (approved["stage"] / "publication" / "publication.json").exists()
    with pytest.raises(FileNotFoundError):
        module._publication_record(approved["stage"])
    assert module.publish_a2_inputs("a1")["published"]


def test_existing_resource_lease_blocks_publication_without_changes(approved):
    Path(approved["manifest"]["resource_lock_path"]).write_text("active process")
    before = (approved["official"] / "public_checks.json").read_bytes()
    with pytest.raises(FileExistsError):
        module.publish_a2_inputs("a1")
    assert (approved["official"] / "public_checks.json").read_bytes() == before


@pytest.mark.parametrize(("command", "stdout", "stderr", "code", "executed"), [
    ("python -m pytest tests/test_a.py", "3 passed, 1 skipped in 0.02s", "", 0, 3),
    ("python -m pytest tests/test_a.py", "1 failed, 2 passed in 0.02s", "", 1, 3),
    ("python -m pytest tests/test_a.py", "3 skipped in 0.02s", "", 0, 0),
    ("python -m pytest tests/test_a.py", "2 passed, 1 error in 0.02s", "", 1, 0),
    ("python -m pytest tests/test_a.py", "no tests ran in 0.02s", "", 5, 0),
    ("python -m unittest test_requests.Case.test_a", "", "Ran 3 tests in 0.010s\n\nOK", 0, 3),
    ("python -m unittest missing", "", "Ran 1 test in 0.010s\n\nFAILED (errors=1)", 1, 0),
    ("python tests/runtests.py tests", "", "Ran 2 tests in 0.010s\n\nOK (skipped=2)", 0, 0),
    ("python tests/runtests.py tests", "", "Ran 3 tests in 0.010s\n\nFAILED (failures=1)", 1, 3),
])
def test_public_test_execution_rejects_skips_import_errors_and_no_collection(command, stdout, stderr, code, executed):
    result = {"stdout": stdout, "stderr": stderr, "exit_code": code, "timed_out": False}
    assert module.public_check_execution(command, result)["executed"] == executed


def _passing_qualification(iid, contract):
    return {"instance_id": iid, "grader_contract": contract, "qualified": True, "cleanup_confirmed": True,
        "candidate_probe": {"quiesced": True, "detached_writer_stopped": True, "patch_applicable": True,
                            "public_checks": {"public_test": {"exit_code": 0, "timed_out": False, "duration_seconds": 0.01}}},
        "baseline": {"completed": True, "resolved": False, "counts": {"parsed": True,
            "FAIL_TO_PASS": {"total": 1, "missing": 0, "passed": 0, "failed": 1},
            "PASS_TO_PASS": {"total": 1, "missing": 0, "passed": 1, "failed": 0}}},
        "reference": {"completed": True, "resolved": True, "counts": {"parsed": True,
            "FAIL_TO_PASS": {"total": 1, "missing": 0, "passed": 1, "failed": 0},
            "PASS_TO_PASS": {"total": 1, "missing": 0, "passed": 1, "failed": 0}}}}


def test_real_cli_gate_reads_all_16_projected_qualification_summaries(approved, monkeypatch):
    inputs = approved
    module.publish_a2_inputs("a1")
    contract = {"scope": "test-contract"}
    monkeypatch.setattr(environment, "capture_grader_contract", lambda: contract)
    for iid in inputs["d1_ids"]:
        module._project_qualification(iid, "a2_attempt_01", _passing_qualification(iid, contract))
    evidence = cli.qualification_evidence("a2", inputs["official"])
    assert set(evidence) == set(inputs["d1_ids"])
    assert all("attempt_01" in value["path"] and "a2_attempt_01" not in value["path"] for value in evidence.values())


def test_cached_failure_also_gets_gate_visible_projection(inputs):
    instance = inputs["rows"][2]
    root = module.qualification_owned_root()
    contract = {"scope": "test-contract"}
    failed = {"instance_id": instance["instance_id"], "grader_contract": contract,
              "qualified": False, "cleanup_confirmed": True, "error_type": "FixtureFailure"}
    module.data.freeze_json(root / instance["instance_id"] / "qualification.json", failed)
    result = module._qualify_one(instance, contract=contract, checks={}, owned_root=root,
        attempt="a2_attempt_01", deadline=0, stop_file=None)
    projected = inputs["official"] / "grader/preflight" / instance["instance_id"] / "attempt_01/qualification.json"
    assert result == failed and module._read(projected) == failed


def test_parser_intent_is_visible_to_real_cli_and_cleanup_removes_owned_container(inputs, monkeypatch):
    contract = {"scope": "test-contract", "input_artifacts": {"test": "hash"}}
    module.data.freeze_json(inputs["official"] / "grader_controller.json", {"image_id": "sha256:" + "f" * 64})
    directory = inputs["official"] / "grader/preflight/a2_runs/a2_attempt_01/one/baseline"
    state = {"exists": False, "owner": None, "name": None, "removed": False}
    def docker(args, **kwargs):
        if args[0] == "create":
            state.update(exists=True, name=args[args.index("--name") + 1], owner=args[args.index("--label") + 1].split("=")[1])
            return subprocess.CompletedProcess(args, 0, "owned-parser-id", "")
        if args[0] == "start":
            owned = cli._recorded_containers(directory / "counts")
            assert owned == {state["name"]: state["owner"]}
            return subprocess.CompletedProcess(args, 0, json.dumps({"parsed": True, "observed_test_count": 1}), "")
        if args[0] == "rm":
            state.update(exists=False, removed=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args[0] == "inspect"
        if not state["exists"]:
            return subprocess.CompletedProcess(args, 1, "", "No such container")
        return subprocess.CompletedProcess(args, 0, json.dumps([{"Config": {"Labels": {"dpswarm.swe.owner": state["owner"]}}}]), "")
    monkeypatch.setattr(environment, "_docker", docker)
    result = module._official_counts(inputs["d1_ids"][0], directory / "job", directory, contract)
    assert result["parsed"] and state["removed"] and not state["exists"]


def test_qualification_stops_after_first_failure_without_trying_other_images(approved, monkeypatch):
    module.publish_a2_inputs("a1")
    contract = {"scope": "fixture-contract"}
    monkeypatch.setattr(environment, "capture_grader_contract", lambda: contract)
    calls = []
    def fail(instance, **kwargs):
        calls.append(instance["instance_id"])
        return {"qualified": False, "cleanup_confirmed": True, "error_type": "FixtureFailure"}
    monkeypatch.setattr(module, "_qualify_one", fail)
    result = module.qualify_a2("a1")
    assert result["stopped"] and result["failure_kind"] == "FixtureFailure"
    assert calls == approved["d1_ids"][:1]
    assert result["owned_root"] == str(module.qualification_owned_root())


def test_qualification_does_not_admit_work_after_stop_file(approved, monkeypatch):
    module.publish_a2_inputs("a1")
    monkeypatch.setattr(environment, "capture_grader_contract", lambda: {"scope": "fixture-contract"})
    monkeypatch.setattr(module, "_qualify_one", lambda *a, **k: pytest.fail("must not admit a task"))
    stop = approved["operations"] / "STOP"
    stop.touch()
    result = module.qualify_a2("a1", stop_file=stop)
    assert result["stopped"] and result["qualified_count"] == 0
