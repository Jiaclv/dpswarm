"""Finite pipeline orchestration tests. No child processes, providers or containers."""
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.minimal_value_20260905.operations import pipeline as p
from modelbench.minimal_value_20260905 import cli, reporting


def stage(batch, count, *, active=None, reason=None, launched=True):
    batch.mkdir(parents=True, exist_ok=True)
    p.write(batch / "state.json", {"completed_at": "now", "completed_episodes": count,
                                  "active_run_id": active, "stop_reason": reason})
    p.write(batch / "manifest.json", {"resource_lock_path": str(batch.parent / "shared-stage.lock")})
    if launched:
        p.write(batch / "launch.json", {"pid": 123, "started_at": "now"})


@pytest.fixture
def rig(tmp_path, monkeypatch):
    a1, a2, run_dir = tmp_path / "a1", tmp_path / "a2", tmp_path / "pipeline"
    stage(a1, 10)
    monkeypatch.setattr(p, "OPERATIONS", tmp_path / "operations")
    monkeypatch.setattr(cli, "validate_a1", lambda *args, **kwargs: {"passed": True})
    monkeypatch.setattr(reporting, "write_report", lambda batch: {
        "stage_complete": True, "completed_episodes": 80, "selection": {"status": "not_qualified"}})
    observed = []
    def runtime(batch):
        assert batch == a1
        return {"stage": "a1", "scheduled_episodes": 10}, {"source": "frozen"}, {"transport": "frozen"}
    def operation(name, *args, **kwargs):
        observed.append(name)
        values = {
            "stage": {"staged": True, "task_count": 16},
            "publish": {"published": True},
            "qualify": {"all_qualified": True, "qualified_count": 16, "total": 16, "stopped": False},
            "gate": {"status": "PASS", "live_run_admission": True},
            "prepare": {"stage": "a2", "scheduled_episodes": 80},
            "launch": {"pid": 456},
        }
        if name == "launch":
            stage(a2, 80)
        return values[name]
    return a1, a2, run_dir, observed, runtime, operation


def test_first_package_completes_exact_chain_once(rig):
    a1, a2, run_dir, observed, runtime, operation = rig
    result = p.run(a1, a2, run_dir, runtime=runtime, operation=operation,
                   alive=lambda batch: False, sleep=lambda _: pytest.fail("unexpected wait"))
    assert result["success"] and result["status"] == "complete"
    assert result["episode_limits"] == {"a1": 10, "a2": 80, "total": 90}
    assert observed == ["stage", "publish", "qualify", "gate", "prepare", "launch"]
    assert result["selection"]["status"] == "not_qualified"
    with pytest.raises(p.PipelineError, match="never implicitly resumed"):
        p.run(a1, a2, run_dir, runtime=runtime, operation=operation)


def test_unlaunched_a1_fails_fast_without_runtime_or_operations(rig):
    a1, a2, run_dir, observed, runtime, operation = rig
    (a1 / "launch.json").unlink()
    result = p.run(a1, a2, run_dir,
                   runtime=lambda _: pytest.fail("must fail before identity lookup"),
                   operation=lambda *args, **kwargs: pytest.fail("must not launch"))
    assert result["status"] == "failed" and "never starts A1" in result["reason"]
    assert result["package_terminal_confirmed"] is False
    assert not observed


@pytest.mark.parametrize("defect", ["stop", "cancel", "failure", "incomplete", "active_at_exit"])
def test_a1_abnormal_completion_prevents_all_a2_work(rig, defect):
    a1, a2, run_dir, observed, runtime, operation = rig
    if defect in ("stop", "cancel"):
        (a1 / defect.upper()).touch()
    elif defect == "failure":
        stage(a1, 3, reason="infrastructure_failure")
    elif defect == "incomplete":
        stage(a1, 9)
    else:
        stage(a1, 10, active="unfinished")
    result = p.run(a1, a2, run_dir, runtime=runtime, operation=operation, alive=lambda batch: False)
    assert not result["success"] and result["status"] in ("failed", "stopped")
    assert observed == [] and not a2.exists()


def test_terminal_state_waits_for_controller_and_released_lease(tmp_path):
    batch = tmp_path / "a1"
    stage(batch, 10)
    assert p.terminal_stage(batch, 10, alive=lambda _: True) is None
    lock = tmp_path / "shared-stage.lock"
    lock.touch()
    with pytest.raises(p.PipelineError, match="lease remains"):
        p.terminal_stage(batch, 10, alive=lambda _: False)


def test_controller_exit_without_state_is_failure(tmp_path):
    batch = tmp_path / "batch"
    batch.mkdir()
    with pytest.raises(p.PipelineError, match="without a normal terminal"):
        p.terminal_stage(batch, 10, alive=lambda _: False)


def test_failed_a1_validation_stops_before_preparation(rig, monkeypatch):
    a1, a2, run_dir, observed, runtime, operation = rig
    def reject(*args, **kwargs):
        raise ValueError("selector had no verified selection")
    monkeypatch.setattr(cli, "validate_a1", reject)
    result = p.run(a1, a2, run_dir, runtime=runtime, operation=operation, alive=lambda _: False)
    assert result["status"] == "failed" and "selector" in result["reason"] and not observed


def test_pipeline_stop_after_qualification_prevents_gate_prepare_launch(rig):
    a1, a2, run_dir, observed, runtime, operation = rig
    def stop_after(name, *args, **kwargs):
        result = operation(name, *args, **kwargs)
        if name == "qualify":
            (run_dir / "STOP").touch()
        return result
    result = p.run(a1, a2, run_dir, runtime=runtime, operation=stop_after, alive=lambda _: False)
    assert result["status"] == "stopped" and not result["success"]
    assert observed == ["stage", "publish", "qualify"]


def test_unqualified_task_prevents_paid_launch(rig):
    a1, a2, run_dir, observed, runtime, operation = rig
    def failure(name, *args, **kwargs):
        result = operation(name, *args, **kwargs)
        return result | {"all_qualified": False, "qualified_count": 15} if name == "qualify" else result
    result = p.run(a1, a2, run_dir, runtime=runtime, operation=failure, alive=lambda _: False)
    assert result["status"] == "failed"
    assert observed == ["stage", "publish", "qualify"] and not a2.exists()


def test_total_deadline_is_finite_and_stops_future_admission(rig):
    a1, a2, run_dir, observed, runtime, operation = rig
    times = iter([0, 2])
    result = p.run(a1, a2, run_dir, runtime=runtime, operation=operation,
                   total_seconds=1, clock=lambda: next(times), alive=lambda _: True)
    assert result["status"] == "stopped" and result["reason"] == "pipeline_total_deadline"
    assert (a1 / "STOP").exists() and not observed


def test_report_missing_selection_is_not_success(rig, monkeypatch):
    a1, a2, run_dir, observed, runtime, operation = rig
    monkeypatch.setattr(reporting, "write_report", lambda _: {"stage_complete": True, "completed_episodes": 80})
    result = p.run(a1, a2, run_dir, runtime=runtime, operation=operation, alive=lambda _: False)
    assert result["status"] == "failed" and not result["success"]


def test_original_runtime_drift_at_final_report_is_not_ignored(rig):
    a1, a2, run_dir, observed, runtime, operation = rig
    seen = []
    def drift(batch):
        seen.append(1)
        if len(seen) > 1:
            raise p.PipelineError("external executor changed")
        return runtime(batch)
    result = p.run(a1, a2, run_dir, runtime=drift, operation=operation, alive=lambda _: False)
    assert result["status"] == "failed" and "executor changed" in result["reason"]


def test_global_stop_blocks_custom_pipeline_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "OPERATIONS", tmp_path / "ops")
    (tmp_path / "ops/pipeline-run").mkdir(parents=True)
    (tmp_path / "ops/pipeline-run/STOP").touch()
    with pytest.raises(p.PipelineStopped):
        p.check_stop(tmp_path / "custom")


def test_launch_is_explicit_and_refuses_unstarted_a1_without_popen(tmp_path, monkeypatch):
    a1 = tmp_path / "prepared-a1"
    a1.mkdir()
    monkeypatch.setattr(p.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not spawn"))
    with pytest.raises(p.PipelineError, match="never starts A1"):
        p.launch(a1, tmp_path / "a2", tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_operation_watchdog_records_failure_and_kills_only_created_child(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "OPERATIONS", tmp_path / "operations")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls = []
    class Process:
        pid = 777
        returncode = None
        def poll(self):
            return self.returncode
    process = Process()
    def kill(child):
        assert child is process
        calls.append("kill")
        child.returncode = -1
        return {"confirmed": True}
    monkeypatch.setattr(cli, "kill_owned_process_tree", kill)
    times = iter([0, 0, 1300])
    with pytest.raises(p.PipelineError, match="watchdog"):
        p.run_operation("gate", tmp_path / "a1", tmp_path / "a2", run_dir, 10000, {},
                         clock=lambda: next(times), popen=lambda *args, **kwargs: process,
                         sleep=lambda _: pytest.fail("watchdog should already expire"))
    assert calls == ["kill"]
    assert p.read(run_dir / "pipeline_state.json")["operation_failure"]["process_cleanup"]["confirmed"]


def test_qualification_receives_same_staging_as_stage_and_publish(tmp_path, monkeypatch):
    from modelbench.minimal_value_20260905.operations import prepare_a2
    run_dir = tmp_path / "run"
    (run_dir / "qualify").mkdir(parents=True)
    monkeypatch.setattr(p, "OPERATIONS", tmp_path / "operations")
    monkeypatch.setattr(p, "original_runtime", lambda _: ({}, {}, {}))
    monkeypatch.setattr(cli, "validate_a1", lambda *args, **kwargs: {"passed": True})
    calls = []
    def qualify(batch, **kwargs):
        calls.append(kwargs)
        return {"all_qualified": True, "qualified_count": 16, "total": 16, "stopped": False}
    monkeypatch.setattr(prepare_a2, "qualify_a2", qualify)
    result = p.execute_operation("qualify", tmp_path / "a1", tmp_path / "a2", run_dir, 5000)
    assert result["all_qualified"]
    assert calls[0]["staging_dir"] == run_dir / "staged-inputs"
    assert calls[0]["stop_file"] == run_dir / "STOP" and calls[0]["wall_seconds"] == 5000


def test_pipeline_stop_stops_a1_admission_and_retains_pending_controller(rig):
    a1, a2, run_dir, observed, runtime, operation = rig
    run_dir.mkdir()
    (run_dir / "STOP").touch()
    result = p.run(a1, a2, run_dir, runtime=runtime, operation=operation, alive=lambda _: True)
    assert result["status"] == "stopped" and not result["success"]
    assert (a1 / "STOP").exists() and not observed
    assert result["active_stage"] == str(a1.resolve())
    assert result["pending_stage_cleanup"][0]["terminal_cleanup_confirmed"] is False
    assert result["package_terminal_confirmed"] is False
    assert result["completion_scope"] == "pipeline_controller_only"
