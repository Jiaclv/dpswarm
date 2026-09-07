"""Independent supervisor failure probes; no subprocess, model, or Docker starts."""
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from modelbench.big_budget_team_20260906 import execution as e
from modelbench.big_budget_team_20260906.contracts import build_entry, load_cells


class Process:
    pid = 12345

    def __init__(self, code=0, *, wait_error=None):
        self.returncode = code
        self.wait_error = wait_error
        self.polls = 0

    def poll(self):
        self.polls += 1
        assert self.polls <= 20, "Supervisor repeatedly polled a process whose termination was unconfirmed"
        return self.returncode

    def wait(self, timeout=None):
        assert timeout is not None and 0 < timeout <= 2100
        if self.wait_error:
            raise self.wait_error
        return self.returncode


@pytest.fixture
def harness(tmp_path, monkeypatch):
    cell = load_cells()[0]
    old = e.read(e.REPO / "modelbench/minimal_value_20260905/batches/a2-v1/manifest.json")
    ref = next(row for row in old["schedule"] if row["instance"]["instance_id"] == cell["instance_id"])
    entry = build_entry(cell, ref["instance"], ref["public_checks"], ref["image"], ref["grader_contract"])
    batch = tmp_path / "batch"
    (batch / "group").mkdir(parents=True)
    e.atomic_json(batch / "manifest.json", {})
    e.atomic_json(batch / "pricing.json", {})
    e.atomic_json(batch / "group/group.json", {})
    e.atomic_json(batch / "group/policy.json", {})
    lock = tmp_path / "execution.lock"
    manifest = {"schedule": [entry], "dispatch_seconds": 7 * 86400,
                "final_drain_seconds": 48 * 3600, "resource_lock_path": str(lock),
                "token_admission_sum_limit": 334800000,
                "generation_watchdog_seconds": 7500, "grading_watchdog_seconds": 2100}
    monkeypatch.setattr(e, "verify", lambda _: (manifest, tmp_path))
    monkeypatch.setattr(e.freeze, "assert_import_origins", lambda _: True)
    monkeypatch.setattr(e.time, "sleep", lambda _: None)
    monitor = SimpleNamespace(started=False, stopped=False)
    monitor.start = lambda: setattr(monitor, "started", True)
    monitor.stop = lambda: setattr(monitor, "stopped", True)
    monkeypatch.setattr(e.resources, "ResourceMonitor", lambda *args, **kwargs: monitor)
    monkeypatch.setattr(e, "cleanup_owned_episode", lambda _: {"confirmed": True})
    monkeypatch.setattr(e.reporting, "accounting", lambda *args: {
        "calls": [], "call_count": 0, "api_equivalent_usd": 0.0,
        "api_equivalent_known_subtotal_usd": 0.0, "usage_unknown_calls": 0,
        "total_tokens_known_subtotal": 0})
    started = []

    def generation():
        directory = batch / "results" / entry["run_id"]
        directory.mkdir(parents=True, exist_ok=True)
        patch = directory / "model.patch"
        patch.write_text("offline frozen patch", encoding="utf-8")
        result = {"run_id": entry["run_id"], "condition_id": entry["condition_id"],
                  "instance_id": entry["instance"]["instance_id"], "cleanup_confirmed": True,
                  "quiesced": True, "grading_pending": True, "official_resolved": None,
                  "artifact": {"path": str(patch), "sha256": e.sha(patch), "status": "present"},
                  "accounting": {}, "outcome": {"status": "completed"}}
        e.atomic_json(directory / "generation_result.json", result)
        e.atomic_json(directory / "episode_result.json", result)
        return result

    return SimpleNamespace(batch=batch, lock=lock, entry=entry, manifest=manifest, monitor=monitor,
                           generation=generation, started=started,
                           directory=batch / "results" / entry["run_id"], monkeypatch=monkeypatch)


def test_current_cleanup_failure_overrides_stale_child_success_and_forbids_grading(harness):
    h = harness

    def child(batch, snapshot, command, rid):
        h.started.append(command)
        assert command == "generate", "Grading started after controller cleanup was unconfirmed"
        h.generation()  # Child claims clean; the controller's independent cleanup disagrees.
        return Process()

    h.monkeypatch.setattr(e, "start_child", child)
    h.monkeypatch.setattr(e, "cleanup_owned_episode", lambda _: {"confirmed": False, "remaining": ["owned-container"]})
    result = e.run(h.batch)
    assert result["status"] == "stopped_incomplete"
    assert result["stop_reason"] == "cleanup_unconfirmed" and result["scored"] == 0
    assert h.started == ["generate"] and h.lock.exists() and h.monitor.stopped
    assert e.read(h.directory / "generation_result.json")["cleanup_confirmed"] is True


def test_process_still_alive_after_kill_exits_bounded_and_retains_lock(harness):
    h = harness
    process, kills = Process(None), []
    h.manifest["generation_watchdog_seconds"] = 0.1
    tick = [0.0]

    def clock():
        tick[0] += 1.0
        return tick[0]

    def child(batch, snapshot, command, rid):
        h.started.append(command)
        assert command == "generate"
        h.generation()
        return process

    def kill(value):
        kills.append(value.pid)
        return {"confirmed": True}  # A positive kill receipt is insufficient while poll still says alive.

    h.monkeypatch.setattr(e.time, "monotonic", clock)
    h.monkeypatch.setattr(e, "start_child", child)
    h.monkeypatch.setattr(e, "kill_owned_process_tree", kill)
    with pytest.raises(RuntimeError, match="termination unconfirmed"):
        e.run(h.batch)
    state = e.read(h.batch / "state.json")
    assert state["status"] == "controller_failed" and state["scored"] == 0
    assert 1 <= len(kills) <= 2 and process.polls < 20
    assert h.started == ["generate"] and h.lock.exists() and h.monitor.stopped


def test_incomplete_official_score_is_recorded_and_stops_controller(harness):
    h = harness
    attempts = []

    def frozen(result, entry, directory, *, grader):
        attempts.append(entry["run_id"])
        return {"completed": False, "resolved": None, "failure_kind": "harness_error"}, [{"completed": False}]

    def child(batch, snapshot, command, rid):
        h.started.append(command)
        if command == "generate":
            h.generation()
        else:
            e.grade(batch, rid)
        return Process()

    h.monkeypatch.setattr(e, "grade_frozen", frozen)
    h.monkeypatch.setattr(e, "start_child", child)
    result = e.run(h.batch)
    terminal = e.read(h.directory / "episode_result.json")
    assert terminal["grading_error"]["type"] == "OfficialScoringIncomplete"
    assert terminal["official_resolved"] is None and terminal["grading_pending"] is False
    assert result["status"] == "stopped_incomplete" and result["stop_reason"] == "grading_or_evidence_error"
    assert h.started == ["generate", "grade"] and len(attempts) == 1
    assert h.monitor.stopped and not h.lock.exists()


@pytest.mark.parametrize("grader_cleanup_confirmed", [True, False])
def test_nonzero_grader_exit_always_cleans_and_retains_lock_if_needed(harness, grader_cleanup_confirmed):
    h = harness
    cleanups = []

    def cleanup(directory):
        cleanups.append(Path(directory))
        return {"confirmed": True if len(cleanups) == 1 else grader_cleanup_confirmed}

    def child(batch, snapshot, command, rid):
        h.started.append(command)
        if command == "generate":
            h.generation()
        return Process(0 if command == "generate" else 7)

    h.monkeypatch.setattr(e, "cleanup_owned_episode", cleanup)
    h.monkeypatch.setattr(e, "start_child", child)
    result = e.run(h.batch)
    assert result["status"] == "stopped_incomplete" and result["stop_reason"] == "grading_process_failed"
    assert result["scored"] == 0 and h.started == ["generate", "grade"]
    assert cleanups == [h.directory, h.directory]
    assert h.lock.exists() is (not grader_cleanup_confirmed)
    receipt = e.read(h.batch / "logs" / (h.entry["run_id"] + ".grading-failure-cleanup.json"))
    assert receipt["confirmed"] is grader_cleanup_confirmed and h.monitor.stopped


def test_exception_while_waiting_for_grader_kills_and_cleans_before_exit(harness):
    h = harness
    grader = Process(None, wait_error=OSError("offline wait failed"))
    kills, cleanups = [], []

    def child(batch, snapshot, command, rid):
        h.started.append(command)
        if command == "generate":
            h.generation()
            return Process()
        return grader

    def kill(process):
        assert process is grader
        kills.append(process.pid)
        process.returncode = -1
        return {"confirmed": True}

    def cleanup(directory):
        cleanups.append(Path(directory))
        return {"confirmed": len(cleanups) == 1}

    h.monkeypatch.setattr(e, "start_child", child)
    h.monkeypatch.setattr(e, "kill_owned_process_tree", kill)
    h.monkeypatch.setattr(e, "cleanup_owned_episode", cleanup)
    with pytest.raises(OSError, match="offline wait failed"):
        e.run(h.batch)
    assert kills == [grader.pid] and cleanups == [h.directory, h.directory]
    assert h.lock.exists() and h.monitor.stopped
    assert e.read(h.batch / "state.json")["status"] == "controller_failed"
    receipt = e.read(h.batch / "logs" / (h.entry["run_id"] + ".grader-exception-cleanup.json"))
    assert receipt["process"]["confirmed"] is True and receipt["containers"]["confirmed"] is False


def test_grading_function_exception_persists_error_and_cleanup_evidence(harness):
    h = harness
    before = h.generation()
    cleanups = []

    def frozen(*args, **kwargs):
        raise RuntimeError("offline scorer failure")

    def cleanup(directory):
        cleanups.append(Path(directory))
        return {"confirmed": False, "remaining": ["owned-grader"]}

    h.monkeypatch.setattr(e, "grade_frozen", frozen)
    h.monkeypatch.setattr(e, "cleanup_owned_episode", cleanup)
    outcome = e.grade(h.batch, h.entry["run_id"])
    terminal = e.read(h.directory / "episode_result.json")
    assert outcome["official_resolved"] is None
    assert terminal["grading_error"]["message"] == "offline scorer failure"
    assert terminal["grading_cleanup"]["confirmed"] is False
    assert terminal["infrastructure_error"]["type"] == "GradingCleanupError"
    assert terminal["artifact"] == before["artifact"] and cleanups == [h.directory]
    assert e.read(h.directory / "generation_result.json") == before


def test_accounting_exception_preserves_generation_patch_and_unknown_ledger(harness):
    from modelbench.big_budget_team_20260906 import provider, runtime

    h = harness
    e.atomic_json(h.batch / "admissions" / (h.entry["run_id"] + ".json"), {
        "manifest_sha256": e.sha(h.batch / "manifest.json"), "status": "admitted"})
    h.monkeypatch.setattr(provider, "install", lambda *args, **kwargs: None)
    artifact = {}

    class FakeRun:
        def __init__(self, batch, entry, *, budget, **kwargs):
            self.folder = Path(batch) / "results" / entry["run_id"]
            self.folder.mkdir(parents=True)
            self.cancel, self.budget = threading.Event(), budget

        def run(self):
            patch = self.folder / "model.patch"
            patch.write_text("retained offline patch", encoding="utf-8")
            artifact.update(path=str(patch), sha256=e.sha(patch), status="present")
            self.budget.reserve("unknown-call", "lead", 12345)
            self.budget.complete("unknown-call", {"call_id": "unknown-call", "input_tokens": None, "output_tokens": None})
            return {"cleanup_confirmed": True, "quiesced": True, "outcome": {"status": "completed"},
                    "artifact": deepcopy(artifact)}

    def broken_accounting(*args, **kwargs):
        raise ValueError("offline malformed accounting source")

    h.monkeypatch.setattr(runtime, "BigBudgetRun", FakeRun)
    h.monkeypatch.setattr(e.reporting, "accounting", broken_accounting)
    outcome = e.generate(h.batch, h.entry["run_id"])
    generated = e.read(h.directory / "generation_result.json")
    terminal = e.read(h.directory / "episode_result.json")
    assert outcome["generation_finished"] is True and generated == terminal
    assert generated["artifact"] == artifact and e.sha(artifact["path"]) == artifact["sha256"]
    assert generated["root_budget_snapshot"]["root"]["frozen"] is True
    assert generated["budget"]["unknown_call_count"] == 1
    assert generated["budget"]["reserved_tokens"] == 12345
    assert generated["budget"]["total_tokens"] is None
    assert generated["accounting_error"]["message"] == "offline malformed accounting source"
    assert generated["infrastructure_error"]["type"] == "AccountingError"
    assert generated["accounting"]["api_equivalent_usd"] is None
    assert generated["accounting"]["total_tokens_known_subtotal"] is None
    assert generated["official_resolved"] is None and generated["grading_pending"] is True
