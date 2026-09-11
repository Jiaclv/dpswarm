"""Fresh A2 pipeline tests: fake process and resource boundaries only."""
from datetime import datetime, timezone, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "a2_wave_pipeline.py"
spec = importlib.util.spec_from_file_location("_a2_wave_pipeline_fixture", SOURCE)
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    experiment = tmp_path / "experiment"
    ops = experiment / "operations"
    a1 = experiment / "batches/a1"
    ops.mkdir(parents=True)
    a1.mkdir(parents=True)
    now = datetime(2026, 9, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(p, "EXPERIMENT", experiment)
    monkeypatch.setattr(p, "HERE", ops)
    monkeypatch.setattr(p, "REPO", tmp_path)
    monkeypatch.setattr(p, "utc", lambda: now)
    p.write(a1 / "manifest.json", {"runtime_sources": {}, "resource_lock_path": str(experiment / "lease")})
    p.write(a1 / "state.json", {
        "completed_episodes": 10, "completed_at": now.isoformat(), "started_at": (now-timedelta(hours=12)).isoformat(),
        "stop_reason": None, "active_episodes": {}})
    source = ops / "bound-source.py"
    source.write_text("# immutable source\n", encoding="utf-8")
    monkeypatch.setattr(p, "sources", lambda sympy=False: {str(source): p.sha(source)})
    definitions, provenance = ops / "definitions.json", ops / "provenance.json"
    p.write(definitions, {"public": "checks"})
    p.write(provenance, {"public": "origin"})
    (ops / "A2_PREPARATION_V3_ZH.md").write_text("Reviewed v3 compatibility scope\n", encoding="utf-8")
    args = dict(run_dir=ops / "pipeline", qualification_dir=ops / "qualification",
                attempt="a2_attempt_02", a1_batch=a1, a2_batch=experiment / "batches/a2",
                group_dir=ops / "group", definitions=definitions, provenance=provenance,
                sympy_native_public_checks=True)
    plan = p.inspect(**args)
    def install():
        p.write(args["run_dir"] / "plan.json", plan)
    def qualified():
        p.write(args["qualification_dir"] / "preparation-state.json", {
            "status": "complete", "all_qualified": True, "qualified_count": 16, "task_count": 16,
            "active_tasks": [], "completed": [{"instance_id": f"task-{i}", "qualified": True,
                                              "cleanup_confirmed": True} for i in range(16)]})
        p.write(args["qualification_dir"] / "preparation-cleanup.json", {"confirmed": True})
    return SimpleNamespace(**locals())


class FakeProcess:
    pid = 876543
    def __init__(self, code=0):
        self.code = code
    def poll(self):
        return self.code


def test_inspect_is_read_only_and_preserves_original_96_hour_anchor(rig):
    assert all(not rig.args[name].exists() for name in ("run_dir", "qualification_dir", "a2_batch", "group_dir"))
    assert p.timestamp(rig.plan["package_deadline"]) == rig.now + timedelta(hours=84)
    assert rig.plan["provider_limits"] == {"codex_account": 4, "glm_coding": 1, "deepseek": 4}
    assert rig.plan["first_wave_count"] == 12 and rig.plan["no_automatic_remaining_waves"]
    assert rig.plan["input_sources"][str(rig.definitions)] == p.sha(rig.definitions)


@pytest.mark.parametrize("defect", ["run_dir", "qualification_dir", "a2_batch", "group_dir", "attempt", "deadline", "a1_stop"])
def test_fresh_pipeline_refuses_existing_or_unavailable_inputs(rig, defect):
    if defect in ("run_dir", "qualification_dir", "a2_batch", "group_dir"):
        rig.args[defect].mkdir(parents=True)
    elif defect == "attempt":
        p.owned_root(rig.args["attempt"]).mkdir(parents=True)
    elif defect == "deadline":
        state = p.read(rig.a1 / "state.json")
        state["started_at"] = (rig.now-timedelta(hours=97)).isoformat()
        p.write(rig.a1 / "state.json", state)
    else:
        (rig.a1 / "STOP").touch()
    with pytest.raises(p.PipelineError):
        p.inspect(**rig.args)


def test_new_sympy_inputs_are_forwarded_only_to_qualification(rig):
    qualify = p.phase_argv(rig.plan, "qualify")
    freeze = p.phase_argv(rig.plan, "freeze")
    assert "--definitions" in qualify and "--provenance" in qualify and "--sympy-native-public-checks" in qualify
    assert "--definitions" not in freeze and "--sympy-native-public-checks" not in freeze
    assert qualify[qualify.index("--attempt")+1] == "a2_attempt_02"


def test_success_hands_off_one_wave_without_waiting_or_launching_other_waves(rig):
    rig.install()
    calls = []
    def phase(plan, name, state):
        calls.append(name)
        return {"successful_phase": name}
    result = p.run(rig.plan, phase_runner=phase)
    assert calls == ["qualify", "freeze", "idle_headroom", "prepare_wave", "dispatch"]
    assert result["status"] == "wave_dispatched" and result["completion_claimed"] is False
    assert result["no_automatic_remaining_waves"]
    assert result["package_deadline"] == rig.plan["package_deadline"]


def test_failed_qualification_cannot_reach_freeze_or_any_model_dispatch(rig):
    rig.install()
    calls = []
    def phase(plan, name, state):
        calls.append(name)
        raise p.PipelineError("one public task failed qualification")
    result = p.run(rig.plan, phase_runner=phase)
    assert calls == ["qualify"]
    assert result["status"] == "failed" and result["no_automatic_retry"]
    assert not rig.args["a2_batch"].exists()


def test_source_drift_is_durable_failure_before_any_child(rig):
    rig.install()
    rig.source.write_text("# changed source\n", encoding="utf-8")
    result = p.run(rig.plan, phase_runner=lambda *args: pytest.fail("no phase may start"))
    assert result["status"] == "failed"
    assert "source/input changed" in result["failure"]["message"]
    assert p.read(rig.args["run_dir"] / "pipeline_state.json")["status"] == "failed"


def test_real_supervisor_validates_saved_qualification_and_hidden_utf8_child(rig):
    rig.install()
    seen = []
    def spawn(argv, **kwargs):
        seen.append(argv)
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        if p.os.name == "nt":
            assert kwargs["creationflags"] == p.subprocess.CREATE_NO_WINDOW
        rig.qualified()
        return FakeProcess(0)
    result = p.run_phase(rig.plan, "qualify", {}, popen=spawn,
                         identify=lambda process: {"pid": process.pid}, observe=lambda _: [])
    assert result["qualified_count"] == 16 and result["cleanup_confirmed"]
    assert len(seen) == 1


@pytest.mark.parametrize("kind", ["exit", "watchdog", "cancel"])
def test_failed_or_cancelled_child_is_stopped_and_only_its_attempt_cleaned(rig, monkeypatch, kind):
    rig.install()
    process = FakeProcess(1 if kind == "exit" else None)
    stopped, cleaned = [], []
    ticks = [0]
    monkeypatch.setitem(p.PHASE_LIMITS, "qualify", 1)
    def sleep(_):
        ticks[0] += 2
        if kind == "cancel":
            p.write(rig.args["run_dir"] / "CANCEL", {"requested": True})
    def stop(child, tracked):
        stopped.append(child.pid)
        child.code = -9
        return {"confirmed": True}
    def clean(plan, phase):
        cleaned.append((phase, plan["qualification_owned_root"]))
        return {"confirmed": True}
    state = {}
    with pytest.raises(p.PipelineError):
        p.run_phase(rig.plan, "qualify", state, popen=lambda *a, **k: process,
                    identify=lambda child: {"pid": child.pid}, observe=lambda _: [],
                    clock=lambda: ticks[0], sleep=sleep, stop=stop, clean=clean)
    assert stopped == [process.pid]
    assert cleaned == [("qualify", rig.plan["qualification_owned_root"])]
    assert (rig.args["qualification_dir"] / "STOP").exists()
    assert not rig.args["a2_batch"].exists()


def test_unconfirmed_process_cleanup_never_claims_container_cleanup(rig):
    rig.install()
    state = {}
    with pytest.raises(p.PipelineError):
        p.run_phase(rig.plan, "qualify", state, popen=lambda *a, **k: FakeProcess(1),
                    identify=lambda child: {"pid": child.pid}, observe=lambda _: [],
                    stop=lambda *args: {"confirmed": False},
                    clean=lambda *args: pytest.fail("do not clean while an owned writer may still run"))
    assert state["failure_cleanup"]["containers"]["confirmed"] is False


def test_dispatch_result_loss_stops_nested_owned_controller(rig):
    rig.install()
    stopped, waves, cleaned = [], [], []
    state = {}
    with pytest.raises(FileNotFoundError):
        p.run_phase(rig.plan, "dispatch", state, popen=lambda *a, **k: FakeProcess(0),
                    identify=lambda child: {"pid": child.pid}, observe=lambda _: [],
                    stop=lambda *a: stopped.append(1) or {"confirmed": True},
                    stop_wave=lambda plan: waves.append(plan["group_dir"]) or {"confirmed": True},
                    clean=lambda *a: cleaned.append(1) or {"confirmed": True})
    assert stopped == [1] and waves == [rig.plan["group_dir"]] and cleaned == [1]
    assert (rig.args["group_dir"] / "CANCEL").exists()


def test_prepare_wave_creates_zero_prefix_and_exact_first_twelve_sidecar(rig, monkeypatch):
    rig.install()
    entries = [{"run_id": f"a2-{i:03d}", "arm": ["S", "L", "D", "T", "R2"][i % 5]} for i in range(80)]
    p.write(rig.args["a2_batch"] / "manifest.json", {"stage": "a2", "scheduled_episodes": 80, "schedule": entries})
    called = []
    monkeypatch.setattr(p.controller, "inspect_trial", lambda *a: called.append(a) or {"passed": True})
    result = p.execute_phase(rig.plan, "prepare_wave")
    state = p.read(rig.args["a2_batch"] / "state.json")
    policy = p.read(rig.args["group_dir"] / "policy.json")
    assert result["prepared"] and len(called) == 1
    assert policy["run_ids"] == [x["run_id"] for x in entries[:12]]
    assert state["token_admission_sum"] == 0 and state["known_cost_usd"] == 0 and state["episodes"] == []
    assert state["package_started_at"] == rig.plan["package_started_at"]
    assert state["parallel_transition"]["status"] == "ready"
    assert policy["candidate_container_cap"] == 12 and policy["candidate_memory"] == "1g"
    assert policy["wave_watchdog_policy"] == "cancel_owned_inflight_at_finite_group_deadline"
    assert not (rig.args["a2_batch"] / "launch.json").exists()


def test_launch_registration_failure_cleans_spawned_supervisor(rig, monkeypatch):
    stopped, cleaned = [], []
    monkeypatch.setattr(p.subprocess, "Popen", lambda *a, **k: FakeProcess(None))
    def identify(_):
        raise OSError("failed to save startup identity")
    monkeypatch.setattr(p, "process_identity", identify)
    monkeypatch.setattr(p.controller, "tracked_descendants", lambda _: [])
    monkeypatch.setattr(p, "stop_process", lambda *a: stopped.append(1) or {"confirmed": True})
    monkeypatch.setattr(p, "stop_dispatched_controller", lambda *a: {"confirmed": True})
    monkeypatch.setattr(p, "cleanup", lambda plan, phase: cleaned.append(phase) or {"confirmed": True})
    with pytest.raises(OSError):
        p.launch(**rig.args)
    assert stopped == [1] and cleaned == ["qualify", "dispatch"]
    assert p.read(rig.args["run_dir"] / "launch-failure.json")["automatic_retry_allowed"] is False


@pytest.mark.parametrize("failure_point", ["after_dispatch", "final_handoff"])
def test_post_dispatch_projection_failure_stops_owned_wave_before_reporting(rig, monkeypatch, failure_point):
    rig.install()
    original_write = p.write
    dispatched, injected, stopped, cleaned = [], [], [], []
    def phase(plan, name, state):
        if name == "dispatch":
            dispatched.append(True)
        return {"phase": name}
    def failing_write(path, value):
        targeted = Path(path).name == "pipeline_state.json" and dispatched and not injected
        if targeted and (failure_point == "after_dispatch" or value.get("status") == "wave_dispatched"):
            injected.append(True)
            raise OSError("lost durable handoff write")
        return original_write(path, value)
    monkeypatch.setattr(p, "write", failing_write)
    monkeypatch.setattr(p, "stop_dispatched_controller", lambda plan: stopped.append(plan["group_dir"]) or {"confirmed": True})
    monkeypatch.setattr(p, "cleanup", lambda plan, name: cleaned.append(name) or {"confirmed": True})
    result = p.run(rig.plan, phase_runner=phase)
    assert injected == [True]
    assert stopped == [rig.plan["group_dir"]] and cleaned == ["dispatch"]
    assert result["status"] == "failed" and result["completion_claimed"] is False
    assert result["handoff_failure_cleanup"]["containers"]["confirmed"] is True
    assert (rig.args["group_dir"] / "CANCEL").exists()
    assert p.read(rig.args["run_dir"] / "pipeline_state.json")["status"] == "failed"


def test_handoff_stop_signal_write_failure_cannot_prevent_process_stop(rig, monkeypatch):
    stopped, cleaned = [], []
    monkeypatch.setattr(p, "signal_stop", lambda *args: (_ for _ in ()).throw(OSError("signal disk error")))
    monkeypatch.setattr(p, "stop_dispatched_controller", lambda plan: stopped.append(1) or {"confirmed": True})
    monkeypatch.setattr(p, "cleanup", lambda *args: cleaned.append(1) or {"confirmed": True})
    result = p.abort_undurable_handoff(rig.plan)
    assert stopped == [1] and cleaned == [1]
    assert result["signal_errors"] and result["containers"]["confirmed"]


def test_v3_revision_binds_adapter_history_and_required_supplement(rig):
    assert rig.plan["pipeline_revision"] == p.PIPELINE_REVISION
    assert rig.plan["sympy_adapter"] == "sympy_public_checks_v3.py"
    assert rig.plan["sympy_historical_counting_baseline"] == "sympy_public_checks.py"
    supplement = rig.ops / "A2_PREPARATION_V3_ZH.md"
    assert rig.plan["input_sources"][str(supplement)] == p.sha(supplement)
    supplement.unlink()
    with pytest.raises(FileNotFoundError):
        p.inspect(**rig.args)


def test_old_plan_revision_cannot_start_new_pipeline_source(rig):
    rig.install()
    old = rig.plan | {"pipeline_revision": "old-v1"}
    result = p.run(old, phase_runner=lambda *args: pytest.fail("No old plan may execute"))
    assert result["status"] == "failed" and "revision differs" in result["failure"]["message"]
