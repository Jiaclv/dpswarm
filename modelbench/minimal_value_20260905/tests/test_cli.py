"""Offline CLI gates, terminal grading and owned cancellation; no providers or Docker."""
from pathlib import Path
import json
import subprocess
import sys
import threading

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.minimal_value_20260905 import cli
from modelbench.minimal_value_20260905.contracts import arm_entry, schedule
from modelbench.minimal_value_20260905.tests.test_strategies import INSTANCE, CHECKS

OWNER = "a" * 32


def manifest_at(batch, entries, stage="a1"):
    batch.mkdir()
    cli.atomic_json(batch / "manifest.json", {"stage": stage, "schedule": entries,
                    "scheduled_episodes": len(entries), "runtime_sources": {"source": "hash"}})
    (batch / "manifest.sha256").write_text(cli.sha(batch / "manifest.json"), encoding="ascii")


def artifact(directory, content=b"frozen candidate"):
    directory.mkdir(parents=True, exist_ok=True)
    patch = directory / "model.patch"
    patch.write_bytes(content)
    return {"artifact": {"status": "present", "path": str(patch), "sha256": cli.sha(patch),
                         "bytes": len(content), "applicable": True},
            "quiesced": True, "cleanup_confirmed": True, "infrastructure_error": None}


@pytest.mark.parametrize("xml,passed", [
    ("<testsuites><testsuite><testcase name='ran'/></testsuite></testsuites>", True),
    ("<testsuite tests='999'/>", False),
    ("<testsuite><testcase><failure/></testcase></testsuite>", False),
    ("<testsuite><testcase><error/></testcase></testsuite>", False),
    ("<testsuite><testcase><skipped/></testcase></testsuite>", False),
])
def test_junit_requires_actual_passing_cases(tmp_path, xml, passed):
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    assert cli.junit_evidence(path)["passed"] is passed


def test_manifest_binding_and_existing_launch_refuse_mutation(tmp_path):
    batch = tmp_path / "batch"
    manifest_at(batch, [])
    assert cli.load_manifest(batch)["schedule"] == []
    path = batch / "manifest.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="manifest identity"):
        cli.load_manifest(batch)
    (batch / "results").mkdir()
    with pytest.raises(ValueError, match="implicit resume"):
        cli.launch(batch)


@pytest.mark.parametrize("kind", ["quiesced", "cleanup_confirmed"])
def test_grade_rejects_unready_candidate_without_call(tmp_path, kind):
    result = artifact(tmp_path / "episode")
    result[kind] = False
    with pytest.raises(ValueError, match="cannot enter grading"):
        cli.grade_frozen(result, {}, tmp_path / "episode",
                         grader=lambda *args, **kwargs: pytest.fail("grader must not run"))


def test_grader_retries_once_on_identical_frozen_bytes(tmp_path):
    directory = tmp_path / "episode"
    result = artifact(directory)
    calls = []
    entry = {"instance": INSTANCE, "arm": "S", "grader_contract": {}}

    def grade(instance, path, expected, run_dir, **kwargs):
        calls.append((path.read_bytes(), expected, run_dir))
        return {"completed": len(calls) == 2, "resolved": False}

    score, attempts = cli.grade_frozen(result, entry, directory, grader=grade,
                                       cleanup=lambda path: {"confirmed": True})
    assert score["completed"] and len(calls) == len(attempts) == 2
    assert calls[0][:2] == calls[1][:2]
    assert calls[0][2] != calls[1][2]


def test_grader_never_retries_candidate_failure_or_changed_patch(tmp_path):
    directory = tmp_path / "episode"
    result = artifact(directory)
    entry = {"instance": INSTANCE, "arm": "S", "grader_contract": {}}
    calls = []

    def empty(*args, **kwargs):
        calls.append(1)
        return {"completed": False, "resolved": False, "failure_kind": "candidate_empty_patch"}

    cli.grade_frozen(result, entry, directory, grader=empty, cleanup=lambda p: {"confirmed": True})
    assert len(calls) == 1

    def mutate(instance, path, *args, **kwargs):
        path.write_bytes(b"changed")
        return {"completed": False}

    with pytest.raises(ValueError, match="changed during"):
        cli.grade_frozen(result, entry, directory, grader=mutate, cleanup=lambda p: {"confirmed": True})


def test_failed_grader_cleanup_prevents_retry(tmp_path):
    directory = tmp_path / "episode"
    result = artifact(directory)
    calls = []
    def grade(*args, **kwargs):
        calls.append(1)
        return {"completed": False}
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        cli.grade_frozen(result, {"instance": INSTANCE, "arm": "D", "grader_contract": {}}, directory,
                         grader=grade, cleanup=lambda p: {"confirmed": False})
    assert len(calls) == 1


def test_owned_cleanup_supports_precreate_intent_and_checks_label(tmp_path):
    cli.atomic_json(tmp_path / "container-intent.json",
                    {"owner": OWNER, "name": "dpswarm-swe-" + OWNER[:16]})
    calls, removed = [], set()
    def docker(args, **kwargs):
        calls.append(args)
        if args[0] == "rm":
            removed.add(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[-1] in removed:
            return subprocess.CompletedProcess(args, 1, "", "No such container")
        return subprocess.CompletedProcess(args, 0,
            json.dumps([{"Config": {"Labels": {"dpswarm.swe.owner": OWNER}}}]), "")
    result = cli.cleanup_owned_episode(tmp_path, docker=docker)
    assert result["confirmed"] and len(result["removed"]) == 1
    assert [call[0] for call in calls] == ["inspect", "rm", "inspect"]

    def mismatch(args, **kwargs):
        assert args[0] != "rm"
        return subprocess.CompletedProcess(args, 0, json.dumps([{"Config": {"Labels": {}}}]), "")
    assert not cli.cleanup_owned_episode(tmp_path, docker=mismatch)["confirmed"]


def test_cancel_kills_only_owned_child_then_cleans_and_does_not_continue(tmp_path):
    (tmp_path / "CANCEL").touch()
    events = []
    class Process:
        returncode = None
        def poll(self):
            return self.returncode
    proc = Process()
    def kill(process):
        assert process is proc
        events.append("kill")
        process.returncode = -1
        return {"confirmed": True}
    def clean(directory):
        events.append("clean")
        return {"confirmed": False}
    result = cli.supervise(proc, tmp_path, tmp_path / "episode", kill=kill, cleanup=clean,
                           sleep=lambda duration: pytest.fail("cancel should act immediately"))
    assert events == ["kill", "clean"]
    assert result["reason"] == "cancel_requested" and not result["cleanup_confirmed"]


def test_stop_allows_active_child_to_finish(tmp_path):
    (tmp_path / "STOP").touch()
    class Process:
        returncode = 0
        def poll(self):
            return 0
    result = cli.supervise(Process(), tmp_path, tmp_path / "episode",
                           kill=lambda p: pytest.fail("STOP does not kill active episode"))
    assert result["reason"] is None


def test_episode_generates_once_then_grades_and_refuses_resume(tmp_path):
    batch = tmp_path / "batch"
    entry = arm_entry("S", INSTANCE, run_id="episode", public_checks=CHECKS, grader_contract={})
    manifest_at(batch, [entry])
    calls = []
    class Run:
        def __init__(self, batch_dir, received, **kwargs):
            assert received == entry
            assert kwargs["grade_enabled"] is False and kwargs["deadline"] > kwargs["start_clock"]
            self.directory = batch_dir / "results" / received["run_id"]
            self.directory.mkdir(parents=True)
            self.cancel = threading.Event()
        def run(self):
            calls.append("generate")
            return artifact(self.directory) | {"budget": {"unknown_call_count": 0, "pending_call_count": 0}}
    def grade(*args, **kwargs):
        calls.append("grade")
        return {"completed": True, "resolved": False}
    account = lambda *args: {"api_equivalent_usd": 0.1, "api_equivalent_known_subtotal_usd": 0.1,
                            "cost_computable": True, "calls": []}
    result = cli.episode(batch, "episode", run_factory=Run, grader=grade, account_fn=account,
                         cleanup=lambda p: {"confirmed": True})
    assert calls == ["generate", "grade"]
    assert result["official_resolved"] is False and result["artifact_execution_integrity"]
    assert (batch / "results/episode/episode_result.json").is_file()
    with pytest.raises(ValueError, match="already exists"):
        cli.episode(batch, "episode", run_factory=Run, grader=grade, account_fn=account)
    assert calls == ["generate", "grade"]


def test_accounting_error_stays_unknown_and_saves_terminal_fault(tmp_path):
    entry = arm_entry("S", INSTANCE, run_id="episode", public_checks=CHECKS)
    result = artifact(tmp_path / "episode")
    def broken(*args):
        raise ValueError("raw/normalized discrepancy")
    saved = cli.save_episode_result(tmp_path / "episode", entry, result, account_fn=broken)
    assert saved["api_equivalent_usd"] is None
    assert saved["infrastructure_error"]["type"] == "AccountingError"
    assert not saved["artifact_execution_integrity"]


def test_stage_lease_is_shared_across_batches_and_released(tmp_path):
    path = tmp_path / "shared" / "active-stage.lock"
    with cli.stage_lease(path, tmp_path / "batch-one"):
        with pytest.raises(FileExistsError):
            with cli.stage_lease(path, tmp_path / "batch-two"):
                pytest.fail("second batch admitted")
    assert not path.exists()
    with cli.stage_lease(path, tmp_path / "batch-two"):
        assert path.exists()


def canary_batch(tmp_path, monkeypatch):
    from modelbench.minimal_value_20260905.contracts import A1_TASKS
    instances = [INSTANCE | {"instance_id": identity} for identity in A1_TASKS]
    entries = schedule("a1", instances, checks={i: CHECKS for i in A1_TASKS})
    batch = tmp_path / "canary"
    manifest_at(batch, entries)
    cli.atomic_json(batch / "state.json", {"completed_episodes": 10, "stop_reason": None})
    monkeypatch.setattr(cli.freeze, "verify_snapshot", lambda *args: batch / "runtime_snapshot")
    for entry in entries:
        directory = batch / "results" / entry["run_id"]
        result = artifact(directory) | {"artifact_execution_integrity": True,
            "budget": {"unknown_call_count": 0, "pending_call_count": 0},
            "score": {"completed": True, "resolved": False}}
        if entry["arm"] in ("D", "T"):
            specs = entry["worker_specs"]
            result.update(workers_with_actual_calls=len(specs), workers=[
                {"status": "completed", "worker_role": spec["role"], "delta_status": "present"} for spec in specs])
        if entry["arm"] == "R2":
            result.update(candidates={scope: artifact(directory / scope) for scope in ("candidate_1", "candidate_2")},
                          selector={"status": "selected", "calls": [{"transport_attempt_count": 1}],
                                    "verifications": [{"candidate_id": "candidate_1", "check_id": "focused",
                                                       "exit_code": 1, "timed_out": False, "official_grading": False}]},
                          selection_source="selector",
                          selection={"selected": "candidate_1", "sha256": result["artifact"]["sha256"]})
        cli.atomic_json(directory / "episode_result.json", result)
    return batch, entries


def test_a1_requires_real_t_workers_and_nonfallback_verified_selection(tmp_path, monkeypatch):
    batch, entries = canary_batch(tmp_path, monkeypatch)
    assert cli.validate_a1(batch)["passed"]
    team = next(e for e in entries if e["arm"] == "T")
    path = batch / "results" / team["run_id"] / "episode_result.json"
    row = cli.read(path)
    row["workers_with_actual_calls"] = 1
    cli.atomic_json(path, row)
    with pytest.raises(ValueError, match="T did not start"):
        cli.validate_a1(batch)


@pytest.mark.parametrize("defect", ["all_fallback", "no_verification", "reserved_only", "wrong_selected_hash"])
def test_a1_r2_path_coverage_cannot_be_fabricated(tmp_path, monkeypatch, defect):
    batch, entries = canary_batch(tmp_path, monkeypatch)
    for entry in entries:
        if entry["arm"] != "R2":
            continue
        path = batch / "results" / entry["run_id"] / "episode_result.json"
        row = cli.read(path)
        if defect == "all_fallback":
            row["selection_source"] = "fallback"
        elif defect == "no_verification":
            row["selector"]["verifications"] = []
        elif defect == "reserved_only":
            row["selector"]["calls"] = [{"transport_attempt_count": 0}]
        else:
            row["selection"]["sha256"] = "wrong"
        cli.atomic_json(path, row)
    with pytest.raises(ValueError, match="R2"):
        cli.validate_a1(batch)


def test_audit_failure_preserves_already_known_cost_and_calls(tmp_path, monkeypatch):
    from modelbench.minimal_value_20260905 import reporting
    entry = arm_entry("S", INSTANCE, run_id="episode", public_checks=CHECKS)
    account = {"calls": [{"call_id": "observed"}], "api_equivalent_usd": 1.2,
               "api_equivalent_known_subtotal_usd": 1.2, "cost_computable": True}
    monkeypatch.setattr(reporting, "accounting", lambda *args: account)
    monkeypatch.setattr(reporting, "audit_accounting", lambda *args: {"passed": False, "errors": ["missing ledger"]},
                        raising=False)
    saved = cli.save_episode_result(tmp_path / "episode", entry, artifact(tmp_path / "episode"))
    assert saved["api_equivalent_known_subtotal_usd"] == 1.2
    assert saved["accounting"]["calls"] == [{"call_id": "observed"}]
    assert saved["infrastructure_error"]["type"] == "AccountingIntegrityError"


def test_gate_runs_junit_and_binds_current_sources(tmp_path, monkeypatch):
    from modelbench.minimal_value_20260905 import environment
    from types import ModuleType
    identity = ModuleType("modelbench.minimal_value_20260905.transport_identity")
    identity.transport_identity = lambda: {"offline": "transport"}
    identity.assert_transport_identity = lambda expected: expected
    monkeypatch.setitem(sys.modules, identity.__name__, identity)
    monkeypatch.setattr(cli.freeze, "runtime_sources", lambda: {"cli.py": "source-hash"})
    monkeypatch.setattr(cli.freeze, "input_files", lambda *args: {"public.json": "input-hash"})
    qualifiers = {"fixture": {"grader_contract": {}}}
    monkeypatch.setattr(cli, "qualification_evidence", lambda *args: qualifiers)
    monkeypatch.setattr(environment, "capture_grader_contract", lambda: {})
    def run(argv, **kwargs):
        assert "pytest" in argv and str(cli.HERE / "tests") in argv
        path = Path(argv[argv.index("--junitxml") + 1])
        path.write_text("<testsuite><testcase name='actually-ran'/></testsuite>")
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(cli.subprocess, "run", run)
    target = tmp_path / "gate"
    gate = cli.gate("a1", official=tmp_path, destination=target)
    assert gate["status"] == "PASS"
    assert cli.verify_gate(target / "gate.json", "a1", tmp_path)["live_run_admission"]
    Path(gate["junit"]["path"]).write_text("<testsuite tests='1'/>")
    with pytest.raises(ValueError, match="JUnit"):
        cli.verify_gate(target / "gate.json", "a1", tmp_path)


def qualification_files(tmp_path, monkeypatch):
    from modelbench.minimal_value_20260905 import data, environment
    from modelbench.minimal_value_20260905.contracts import A1_TASKS
    rows = [INSTANCE | {"instance_id": identity} for identity in A1_TASKS]
    monkeypatch.setattr(data, "load_public", lambda *args: rows)
    monkeypatch.setattr(environment, "capture_grader_contract", lambda: {"frozen": "contract"})
    cli.atomic_json(tmp_path / "public_checks.json", {i: CHECKS for i in A1_TASKS})
    baseline = {"completed": True, "resolved": False, "counts": {
        "parsed": True, "FAIL_TO_PASS": {"total": 1, "missing": 0, "failed": 1, "passed": 0},
        "PASS_TO_PASS": {"total": 1, "missing": 0, "failed": 0, "passed": 1}}}
    reference = {"completed": True, "resolved": True, "counts": {
        "parsed": True, "FAIL_TO_PASS": {"total": 1, "missing": 0, "failed": 0, "passed": 1},
        "PASS_TO_PASS": {"total": 1, "missing": 0, "failed": 0, "passed": 1}}}
    paths = []
    for identity in A1_TASKS:
        path = tmp_path / "grader/preflight" / identity / "attempt_03/qualification.json"
        cli.atomic_json(path, {"instance_id": identity, "grader_contract": {"frozen": "contract"},
            "qualified": True, "baseline": baseline, "reference": reference,
            "candidate_probe": {"quiesced": True, "detached_writer_stopped": True, "patch_applicable": True,
                                "public_checks": {"focused": {"exit_code": 1, "timed_out": False}}}})
        paths.append(path)
    return paths


def test_qualification_uses_latest_current_contract_without_fallback(tmp_path, monkeypatch):
    paths = qualification_files(tmp_path, monkeypatch)
    evidence = cli.qualification_evidence("a1", tmp_path)
    assert all(row["attempt"] == 3 for row in evidence.values())
    later = paths[0].parent.parent / "attempt_04/qualification.json"
    cli.atomic_json(later, cli.read(paths[0]) | {"qualified": False})
    with pytest.raises(ValueError, match="Latest task qualification failed"):
        cli.qualification_evidence("a1", tmp_path)


@pytest.mark.parametrize("public_result", [
    {"exit_code": 2, "timed_out": False}, {"exit_code": 5, "timed_out": False},
    {"exit_code": 0, "timed_out": True}, {"exit_code": 0}, {},
])
def test_qualification_rejects_unexecuted_or_unusable_public_check(tmp_path, monkeypatch, public_result):
    paths = qualification_files(tmp_path, monkeypatch)
    row = cli.read(paths[0])
    row["candidate_probe"]["public_checks"]["focused"] = public_result
    cli.atomic_json(paths[0], row)
    with pytest.raises(ValueError, match="qualification failed"):
        cli.qualification_evidence("a1", tmp_path)


def test_watchdog_is_finite_and_failed_kill_is_not_confirmation(tmp_path):
    times = iter([0, cli.EPISODE_WATCHDOG_SECONDS + 1])
    class Process:
        returncode = None
        def poll(self):
            return None
    def kill(process):
        raise RuntimeError("owned process could not be killed")
    result = cli.supervise(Process(), tmp_path, tmp_path / "episode", clock=lambda: next(times),
                           kill=kill, cleanup=lambda p: {"confirmed": True})
    assert result["reason"] == "episode_watchdog"
    assert not result["cleanup_confirmed"]


def test_retained_resource_lease_blocks_later_batch(tmp_path):
    path = tmp_path / "active-stage.lock"
    with cli.stage_lease(path, tmp_path / "one") as lease:
        lease["retain"] = True
    assert path.exists()
    with pytest.raises(FileExistsError):
        with cli.stage_lease(path, tmp_path / "two"):
            pytest.fail("unconfirmed resource reused")
