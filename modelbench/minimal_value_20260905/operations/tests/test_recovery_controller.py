"""Recovery orchestration fixtures; never start a provider, grader or container."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "recovery_controller.py"
spec = importlib.util.spec_from_file_location("recovery_fixture_module", SOURCE)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
NOW = datetime(2026, 9, 5, 5, 0, tzinfo=timezone.utc)
FIRST_COST = 2.3334739373975615


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class FakeProcess:
    pid = 987654
    def __init__(self, code=0):
        self.code = code
    def poll(self):
        return self.code


@pytest.fixture
def rig(tmp_path):
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "runtime_snapshot").mkdir()
    here = tmp_path / "frozen-runtime"
    write(here / "pricing.json", {})
    schedule = [
        {"run_id": f"a1-{n:02d}-{['D','S','L','T','R2'][(n-1)%5]}",
         "arm": ['D','S','L','T','R2'][(n-1)%5],
         "instance": {"instance_id": f"task-{(n-1)//5}"}}
        for n in range(1, 11)
    ]
    manifest = {
        "stage": "a1", "scheduled_episodes": 10, "schedule": schedule,
        "resource_lock_path": str(tmp_path / "shared-stage.lock"),
        "stage_limits": {"dispatch_seconds": 9*3600, "episode_limit": 10,
                         "token_admission_sum": 6000000, "cost_stop_usd": 60,
                         "cost_warning_usd": 40},
    }
    write(batch / "manifest.json", manifest)
    record = batch / "recoveries" / r.DEFAULT_RECOVERY_ID / "recovery.json"
    write(record, {"restored": True})
    cleanups, reports, leases, spawned = [], [], [], []

    def complete(entry, cost=0.25, *, invalid=False):
        directory = batch / "results" / entry["run_id"]
        directory.mkdir(parents=True, exist_ok=True)
        patch = directory / "frozen.patch"
        patch.write_text("an immutable fake patch\n", encoding="utf-8")
        account = {"cost_computable": True, "api_equivalent_known_subtotal_usd": cost,
                   "api_equivalent_usd": cost, "calls": [{"call_id": entry["run_id"]}],
                   "usage_unknown_calls": 0, "protocol_issues": []}
        write(directory / "account.json", account)
        result = {"run_id": entry["run_id"], "arm": entry["arm"],
                  "instance_id": entry["instance"]["instance_id"],
                  "artifact": {"path": str(patch), "sha256": r.sha(patch)},
                  "quiesced": True, "cleanup_confirmed": True,
                  "accounting": account, "score": {"completed": True, "resolved": True},
                  "budget": {"unknown_call_count": 0, "pending_call_count": 0}}
        if invalid:
            result["infrastructure_error"] = {"type": "RealFailure"}
        write(directory / "episode_result.json", result)
        return directory

    first = complete(schedule[0], FIRST_COST)
    state = {
        "stage": "a1", "started_at": (NOW - timedelta(hours=1)).isoformat(),
        "completed_episodes": 1, "token_admission_sum": 600000, "known_cost_usd": FIRST_COST,
        "stop_reason": None,
        "episodes": [{"run_id": schedule[0]["run_id"],
                      "path": str(first / "episode_result.json"), "sha256": r.sha(first / "episode_result.json")}],
        "recovery": {"recovery_id": r.DEFAULT_RECOVERY_ID,
                     "status": "evidence_restored_awaiting_explicit_resume", "record": str(record)},
    }
    write(batch / "state.json", state)
    write(batch / "launch.json", {"pid": 123456, "started_at": NOW.isoformat()})

    def accounting(directory, card):
        path = Path(directory) / "account.json"
        return r.read(path) if path.is_file() else {
            "cost_computable": False, "api_equivalent_known_subtotal_usd": 0, "calls": []}
    def audit(directory, result, account):
        if result.get("accounting") != account:
            raise ValueError("ledger/call mismatch")
        return {"passed": True, "completed_calls": 1, "pending_calls": 0, "unknown_calls": 0}
    def result_status(result, entry):
        art = result["artifact"]
        valid = r.sha(art["path"]) == art["sha256"]
        return {"artifact_execution_integrity": valid}
    def stop_reason(result):
        if result.get("infrastructure_error"):
            return "infrastructure_failure"
        if not result["artifact_execution_integrity"]:
            return "artifact_integrity_failure"
        if result.get("accounting", {}).get("protocol_issues"):
            return "transport_protocol_or_usage_unreconciled"
        if not result.get("score", {}).get("completed"):
            return "grading_unavailable"
    def report(batch):
        reports.append(str(batch))
        value = r.read(batch / "state.json")
        return {"stage_complete": value["completed_episodes"] == 10 and not value.get("stop_reason"),
                "completed_episodes": value["completed_episodes"], "stop_reason": value.get("stop_reason")}
    @contextmanager
    def lease(path, batch):
        value = {"retain": False}
        leases.append(value)
        yield value
    def cleanup(directory):
        cleanups.append(str(directory))
        return {"confirmed": True}
    cli = SimpleNamespace(
        HERE=here, load_manifest=lambda b: r.read(b / "manifest.json"),
        validate_schedule=lambda s, stage: None,
        freeze=SimpleNamespace(verify_snapshot=lambda b, m: b / "runtime_snapshot",
                               credential_environment=lambda **kwargs: {"LOCAL_CONFIG_LOADED": "1"}),
        reporting=SimpleNamespace(accounting=accounting, audit_accounting=audit,
                                  result_status=result_status, stop_reason=stop_reason, write_report=report),
        stage_lease=lease, atomic_json=write, cleanup_owned_episode=cleanup,
        kill_owned_process_tree=lambda process: {"confirmed": True},
        validate_a1=lambda batch: {"passed": True})
    def inspect(**kwargs):
        return r.inspect_recovery(batch, cli=cli, now=kwargs.pop("now", NOW),
                                  process_check=lambda b: {"pid": 123456, "stopped": True}, **kwargs)
    def prepare_run():
        plan = inspect()
        plan["controller_source_sha256"] = r.sha(SOURCE)
        directory = batch / "recoveries" / r.DEFAULT_RECOVERY_ID / "controller-launch"
        write(directory / "plan.json", plan)
        return directory
    def spawn(argv, **kwargs):
        run_id = argv[argv.index("--run-id") + 1]
        spawned.append((run_id, argv, kwargs["env"]))
        complete(next(entry for entry in schedule if entry["run_id"] == run_id))
        return FakeProcess()
    return SimpleNamespace(**locals())


def test_prefix_is_audited_and_cost_start_and_admission_are_carried_once(rig):
    before = (rig.batch / "state.json").read_bytes()
    plan = rig.inspect()
    assert plan["completed_episodes"] == 1
    assert len(plan["remaining_run_ids"]) == 9
    assert plan["known_cost_usd"] == FIRST_COST and plan["token_admission_sum"] == 600000
    assert plan["remaining_dispatch_seconds"] == 8 * 3600
    assert plan["original_started_at"] == rig.state["started_at"]
    assert (rig.batch / "state.json").read_bytes() == before


@pytest.mark.parametrize("key,value", [
    ("completed_episodes", 0), ("token_admission_sum", 1200000),
    ("known_cost_usd", 0), ("episodes", [])])
def test_prefix_state_accounting_mismatch_blocks_without_reexecution(rig, key, value):
    rig.state[key] = value
    write(rig.batch / "state.json", rig.state)
    with pytest.raises(r.RecoveryError):
        rig.inspect()
    assert not rig.spawned


@pytest.mark.parametrize("marker", ["STOP", "CANCEL"])
def test_stop_markers_block_admission(rig, marker):
    (rig.batch / marker).touch()
    with pytest.raises(r.RecoveryError, match="STOP/CANCEL"):
        rig.inspect()


def test_expired_original_deadline_cannot_reset_at_recovery(rig):
    with pytest.raises(r.RecoveryError, match="deadline has elapsed"):
        rig.inspect(now=NOW + timedelta(hours=8))


@pytest.mark.parametrize("defect", ["partial", "hole", "patch_tamper", "ledger_tamper", "missing_recovery"])
def test_partial_or_untrusted_prefix_is_never_skipped(rig, defect):
    if defect == "partial":
        (rig.batch / "results" / rig.schedule[1]["run_id"]).mkdir()
    elif defect == "hole":
        rig.complete(rig.schedule[2])
    elif defect == "patch_tamper":
        (rig.first / "frozen.patch").write_text("changed", encoding="utf-8")
    elif defect == "ledger_tamper":
        write(rig.first / "account.json", {"cost_computable": True, "api_equivalent_known_subtotal_usd": 0})
    else:
        rig.record.unlink()
    with pytest.raises((r.RecoveryError, ValueError)):
        rig.inspect()


@pytest.mark.parametrize("value", ["../escape", r"..\escape", "a:b", "", "a/b"])
def test_recovery_id_cannot_escape_owned_directory(rig, value):
    with pytest.raises(r.RecoveryError, match="safe directory"):
        rig.inspect(recovery_id=value)


def test_only_pipe_encoding_changes_and_snapshot_pythonpath(rig, monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    env = r.child_environment(rig.cli, rig.batch / "runtime_snapshot")
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert "PYTHONUTF8" not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert str(rig.batch / "runtime_snapshot") in env["PYTHONPATH"]


def test_known_post_commit_stdout_error_preserves_completed_result(rig):
    before = (rig.first / "episode_result.json").read_bytes()
    stderr = rig.batch / "known.stderr"
    stderr.write_bytes(b'File "cli.py"\n print(json.dumps(result, ensure_ascii=False), flush=True)\nUnicodeEncodeError')
    audited, explanation = r.child_result(
        rig.batch, rig.schedule[0], {"reason": "episode_process_failed", "returncode": 1}, stderr, rig.cli)
    assert explanation["kind"] == "verified_post_commit_stdout_encoding_failure"
    assert audited["known_cost_usd"] == FIRST_COST
    assert (rig.first / "episode_result.json").read_bytes() == before


@pytest.mark.parametrize("reason,returncode", [("episode_process_failed", 1), ("cancel_requested", 1), (None, 1)])
def test_other_abnormal_exit_cannot_be_silently_accepted_or_rewrite(rig, reason, returncode):
    before = (rig.first / "episode_result.json").read_bytes()
    stderr = rig.batch / "real.stderr"
    stderr.write_bytes(b"RealFailure")
    with pytest.raises(r.RecoveryError, match="abnormally"):
        r.child_result(rig.batch, rig.schedule[0], {"reason": reason, "returncode": returncode}, stderr, rig.cli)
    assert (rig.first / "episode_result.json").read_bytes() == before


def test_only_remaining_nine_dispatch_with_original_deadline_and_total_accounting(rig):
    rig.prepare_run()
    first_bytes = (rig.first / "episode_result.json").read_bytes()
    result = r.run_remaining(rig.batch, cli=rig.cli, now=lambda: NOW,
                             popen=rig.spawn, supervise=lambda *args: {"reason": None, "returncode": 0})
    state = r.read(rig.batch / "state.json")
    assert result["stage_complete"]
    assert [item[0] for item in rig.spawned] == [e["run_id"] for e in rig.schedule[1:]]
    assert state["completed_episodes"] == 10 and state["token_admission_sum"] == 6000000
    assert state["known_cost_usd"] == pytest.approx(FIRST_COST + 9 * 0.25)
    assert state["started_at"] == rig.state["started_at"]
    assert state["a1_mechanism_gate"]["passed"]
    assert len(state["episodes"]) == 10 and not state.get("active_run_id")
    assert all(item[2]["PYTHONIOENCODING"] == "utf-8" for item in rig.spawned)
    assert (rig.first / "episode_result.json").read_bytes() == first_bytes


@pytest.mark.parametrize("stop_kind", ["STOP", "CANCEL", "deadline"])
def test_midstage_stop_does_not_admit_more_episodes(rig, stop_kind):
    rig.prepare_run()
    clock = [NOW]
    def supervise(*args):
        if stop_kind == "deadline":
            clock[0] = NOW + timedelta(hours=8)
        else:
            (rig.batch / stop_kind).touch()
        return {"reason": None, "returncode": 0}
    result = r.run_remaining(rig.batch, cli=rig.cli, now=lambda: clock[0],
                             popen=rig.spawn, supervise=supervise)
    state = r.read(rig.batch / "state.json")
    assert len(rig.spawned) == 1 and state["completed_episodes"] == 2
    assert state["token_admission_sum"] == 1200000
    assert not result["stage_complete"] and state["stop_reason"]
    assert not state.get("active_run_id")


@pytest.mark.parametrize("running", [False, True])
def test_supervisor_exception_preserves_known_cost_and_cleans_even_exited_child(rig, running):
    rig.prepare_run()
    saved = {}
    def spawn(argv, **kwargs):
        process = rig.spawn(argv, **kwargs)
        run_id = argv[argv.index("--run-id") + 1]
        path = rig.batch / "results" / run_id / "episode_result.json"
        saved["path"], saved["bytes"] = path, path.read_bytes()
        process.code = None if running else 0
        return process
    def supervise(*args):
        raise RuntimeError("supervisor record failed after calls")
    result = r.run_remaining(rig.batch, cli=rig.cli, now=lambda: NOW, popen=spawn, supervise=supervise)
    state = r.read(rig.batch / "state.json")
    assert result["stop_reason"] == "recovery_controller_failure"
    assert state["known_cost_usd"] == pytest.approx(FIRST_COST + 0.25)
    assert state["completed_episodes"] == 1 and state["token_admission_sum"] == 1200000
    assert state["failed_attempt_accounting"]["api_equivalent_known_subtotal_usd"] == 0.25
    assert rig.cleanups and saved["path"].read_bytes() == saved["bytes"]
    assert not rig.leases[0]["retain"] and len(rig.spawned) == 1


def test_real_episode_failure_is_not_overwritten_and_cost_not_counted_twice(rig):
    directory = rig.prepare_run()
    before = {}
    def spawn(argv, **kwargs):
        process = rig.spawn(argv, **kwargs)
        run_id = argv[argv.index("--run-id") + 1]
        entry = next(e for e in rig.schedule if e["run_id"] == run_id)
        path = rig.complete(entry, invalid=True) / "episode_result.json"
        before.update(path=path, data=path.read_bytes())
        return process
    result = r.run_remaining(rig.batch, cli=rig.cli, now=lambda: NOW, popen=spawn,
                             supervise=lambda *args: {"reason": "episode_process_failed", "returncode": 1})
    state = r.read(rig.batch / "state.json")
    assert state["known_cost_usd"] == pytest.approx(FIRST_COST + 0.25)
    assert state["completed_episodes"] == 1 and not result["stage_complete"]
    assert before["path"].read_bytes() == before["data"]
    assert (directory / (rig.schedule[1]["run_id"] + "-failure.json")).is_file()


def test_unconfirmed_cleanup_retains_lease_and_active_identity(rig):
    rig.prepare_run()
    rig.cli.cleanup_owned_episode = lambda directory: {"confirmed": False}
    def supervise(*args):
        raise RuntimeError("unknown child")
    r.run_remaining(rig.batch, cli=rig.cli, now=lambda: NOW, popen=rig.spawn, supervise=supervise)
    state = r.read(rig.batch / "state.json")
    assert rig.leases[0]["retain"] is True
    assert state["active_run_id"] == rig.schedule[1]["run_id"]


def test_changed_wrapper_after_launch_blocks_before_child_or_lock(rig):
    directory = rig.prepare_run()
    plan = r.read(directory / "plan.json")
    plan["controller_source_sha256"] = "changed"
    write(directory / "plan.json", plan)
    with pytest.raises(r.RecoveryError, match="source changed"):
        r.run_remaining(rig.batch, cli=rig.cli, now=lambda: NOW, popen=rig.spawn)
    assert not rig.spawned and not (rig.batch / "controller.lock").exists()


def test_launch_archives_prior_lock_and_uses_standalone_utf8_command(rig, monkeypatch):
    write(rig.batch / "controller.lock", {"pid": 123456})
    old_launch = (rig.batch / "launch.json").read_bytes()
    old_lock = (rig.batch / "controller.lock").read_bytes()
    monkeypatch.setattr(r, "load_frozen_cli", lambda batch: rig.cli)
    original = r.inspect_recovery
    monkeypatch.setattr(r, "inspect_recovery", lambda batch, **kwargs: original(
        batch, **kwargs, now=NOW, process_check=lambda batch: {"pid": 123456, "stopped": True}))
    created = []
    def popen(argv, **kwargs):
        created.append((argv, kwargs))
        return FakeProcess()
    monkeypatch.setattr(r.subprocess, "Popen", popen)
    value = r.launch(rig.batch)
    directory = rig.batch / "recoveries" / r.DEFAULT_RECOVERY_ID / "controller-launch"
    assert (directory / "previous-launch.json").read_bytes() == old_launch
    assert (directory / "previous-controller.lock").read_bytes() == old_lock
    assert not (rig.batch / "controller.lock").exists()
    argv, options = created[0]
    assert "_run" in argv and str(rig.batch.resolve()) in argv
    assert "-m" not in argv and str(SOURCE) in argv
    assert options["env"]["PYTHONIOENCODING"] == "utf-8"
    assert value["pid"] == FakeProcess.pid
    assert r.read(directory / "plan.json")["controller_source_sha256"] == r.sha(SOURCE)
