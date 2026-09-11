"""Offline scope and launch contracts for the 33-entry resume revision."""
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "a2_resume_v3.py"
spec = importlib.util.spec_from_file_location("_a2_resume_v3_fixture", SOURCE)
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    exp = tmp_path / "experiment"
    ops, old, batch = exp / "operations", exp / "batches/old", exp / "batches/new"
    ops.mkdir(parents=True)
    old.mkdir(parents=True)
    moment = datetime(2026, 9, 6, tzinfo=timezone.utc)
    for owner in (p, p.base):
        monkeypatch.setattr(owner, "HERE", ops)
        monkeypatch.setattr(owner, "EXPERIMENT", exp)
        monkeypatch.setattr(owner, "utc", lambda: moment)
        monkeypatch.setattr(owner, "operator_sources", lambda: {str(SOURCE): p.sha(SOURCE)})
    schedule = [{"run_id": f"a2-{i:03d}", "arm": "S",
                 "instance": {"instance_id": f"task-{i // 5}"}} for i in range(80)]
    carried = []
    for entry in schedule[:21]:
        path = old / "results" / entry["run_id"] / "episode_result.json"
        p.write(path, {"original": True})
        carried.append({"run_id": entry["run_id"], "path": str(path), "sha256": p.sha(path)})
    unknown = [{"call_id": f"unknown-{i}", "reserved_tokens": n}
               for i, n in enumerate((40000, 40000, 38387, 5270, 23624, 22678, 42805, 30000, 30000, 35335))]
    old_plan = {"carried_valid": carried, "legacy_unknown_calls": unknown[:7],
                "references": {}, "incomplete_runs_not_retried": [e["run_id"] for e in schedule[21:24]]}
    p.write(old / "recovery-plan.json", old_plan)
    manifest = {"stage": "a2", "scheduled_episodes": 80, "schedule": schedule,
                "runtime_sources": {}, "input_artifacts": {},
                "resource_lock_path": str(exp / "stage.lock"),
                "stage_limits": {"token_admission_sum": 51_600_000, "cost_stop_usd": 480},
                "recovery_plan_sha256": p.sha(old / "recovery-plan.json")}
    p.write(old / "manifest.json", manifest)
    completed = []
    for entry in schedule[24:36]:
        path = old / "results" / entry["run_id"] / "episode_result.json"
        p.write(path, {"new_original": True})
        completed.append({"run_id": entry["run_id"], "path": str(path), "sha256": p.sha(path)})
    p.write(old / "state.json", {"token_admission_sum": 31_800_000,
                               "completed_episodes": 12, "episodes": completed})
    prior_group = ops / "old-auto/wave-001-012"
    p.write(old / "launch.json", {"parallel_group_dir": str(prior_group)})
    supervisor = prior_group.parent / "resume-state.json"
    p.write(supervisor, {"status": "stopped"})
    auth = {"revision": p.REVISION, "user_confirmation": "那就继续啊",
            "total_admission_cap": 51_600_000, "already_admitted_attempts": 53,
            "already_admitted_tokens": 31_800_000, "remaining_admission_tokens": 19_800_000,
            "new_attempt_count": 33, "new_attempt_token_admission": 600000, "unknown_call_count": 10,
            "salvage_candidate_count": 7, "carried_original_valid_count": 33,
            "maximum_logical_valid_if_all_salvage_qualify": 73, "cost_stop_usd": 480,
            "cost_warning_usd": 320, "stage_dispatch_seconds": 61 * 3600, "package_max_seconds": 96 * 3600,
            "global_model_slots": 8, "candidate_container_cap": 12, "candidate_memory": "1g",
            "automatic_additional_retry": False, "unknown_usage_not_zero": True,
            "provider_limits": {"codex_account": 4, "glm_coding": 1, "deepseek": 4},
            "source_batch": str(old), "source_manifest_sha256": p.sha(old / "manifest.json"),
            "source_state_sha256": p.sha(old / "state.json"), "source_supervisor_sha256": p.sha(supervisor),
            "legacy_known_cost_usd": 73.83606440367053,
            "pending_run_ids": [e["run_id"] for e in schedule[47:]],
            "incomplete_runs_not_retried": [e["run_id"] for e in schedule[21:24]+schedule[43:47]],
            "stage_started_at": (moment - timedelta(hours=5)).isoformat(),
            "package_started_at": (moment - timedelta(hours=13)).isoformat()}
    auth_path, report_path = ops / "authorization.json", ops / "salvage/SALVAGE_REPORT.json"
    p.write(auth_path, auth)
    salvaged = []
    for entry in schedule[36:43]:
        directory = ops / "salvage/results" / entry["run_id"]
        patch = directory / "model.patch"
        directory.mkdir(parents=True)
        patch.write_text("immutable patch\n", encoding="utf-8")
        raw = directory / "source-result.json"
        p.write(raw, {"source": entry["run_id"]})
        official = directory / "grade/report.json"
        p.write(official, {"official": "completed unresolved"})
        scored = directory / "episode_result.json"
        p.write(scored, {"quiesced": True, "cleanup_confirmed": True,
                        "artifact": {"path": str(patch), "sha256": p.sha(patch)},
                        "score": {"completed": True, "resolved": False, "patch_sha256": p.sha(patch),
                                  "grader_dir": str(official.parent), "reports_sha256": {official.name: p.sha(official)}}})
        salvaged.append({"run_id": entry["run_id"], "path": str(scored), "sha256": p.sha(scored),
                         "source_result_path": str(raw), "source_result_sha256": p.sha(raw),
                         "patch_sha256": p.sha(patch), "official_resolved": False})
    report = {"status": "PASS", "cleanup_confirmed": True, "source_batch": str(old),
              "source_manifest_sha256": auth["source_manifest_sha256"], "overall_unknown_count": 10,
              "overall_cost_computable": False, "accounting": {
                  "combined_known_api_equivalent_usd": auth["legacy_known_cost_usd"]},
              "salvaged_valid": salvaged, "legacy_unknown_calls": unknown[:7],
              "new_unknown_calls": unknown[7:], "references": {}, "carried_valid": carried+completed}
    p.write(report_path, report)
    raw_cli = SimpleNamespace(reporting=SimpleNamespace(),
        load_manifest=lambda directory: p.read(Path(directory) / "manifest.json"),
        freeze=SimpleNamespace(verify_snapshot=lambda *a: None), validate_schedule=lambda *a: None)
    cli = p.base.CliView(raw_cli)
    monkeypatch.setattr(p.recovery, "load_frozen_cli", lambda _: raw_cli)
    def prepare():
        return p.prepare(batch, authorization_path=auth_path, salvage_report=report_path)
    return SimpleNamespace(**locals())


def test_prepare_preserves_53_admissions_10_unknown_and_only_33_unstarted(rig):
    receipt = rig.prepare()
    value, _, state = p.contract(rig.batch, rig.cli)
    assert receipt["new_attempt_count"] == 33 and receipt["carried_valid_count"] == 40
    assert value["pending_run_ids"] == [e["run_id"] for e in rig.schedule[47:]]
    assert state["token_admission_sum"] == 31_800_000
    assert len(value["legacy_unknown_calls"]) == 10 and value["legacy_unknown_reserved_tokens"] == 308099
    assert state["known_cost_usd"] == rig.auth["legacy_known_cost_usd"]
    assert state["overall_cost_computable"] is False
    assert not (rig.batch / "results").exists()


@pytest.mark.parametrize("change", [{"new_attempt_count": 59}, {"automatic_additional_retry": True}])
def test_authorization_expansion_rejected_before_creating_batch(rig, change):
    p.write(rig.auth_path, {**rig.auth, **change})
    with pytest.raises(p.cont.ContinuationError): rig.prepare()
    assert not rig.batch.exists()


@pytest.mark.parametrize("defect", ["unknown_zero", "double_charge"])
def test_salvage_accounting_changes_are_not_accepted(rig, defect):
    report = p.read(rig.report_path)
    if defect == "unknown_zero": report["new_unknown_calls"][0]["reserved_tokens"] = 0
    else: report["accounting"]["combined_known_api_equivalent_usd"] += 10
    p.write(rig.report_path, report)
    with pytest.raises(p.cont.ContinuationError): rig.prepare()
    assert not rig.batch.exists()


def test_bound_authorization_edit_rejected_after_prepare(rig):
    rig.prepare()
    p.write(rig.auth_path, {**rig.auth, "note": "changed after binding"})
    with pytest.raises(p.cont.ContinuationError, match="authorization changed"):
        p.contract(rig.batch, rig.cli)


def test_state_cost_cannot_refund_or_double_charge_salvage(rig):
    rig.prepare()
    state = p.read(rig.batch / "state.json")
    state["known_cost_usd"] += 1
    p.write(rig.batch / "state.json", state)
    with pytest.raises(p.cont.ContinuationError, match="Known cost"):
        p.prefix_audit(rig.batch, rig.cli)


def test_allegedly_unstarted_with_existing_evidence_rejected(rig):
    (rig.old / "results" / rig.auth["pending_run_ids"][0]).mkdir(parents=True)
    with pytest.raises(p.cont.ContinuationError, match="already has evidence"):
        rig.prepare()
    assert not rig.batch.exists()


def test_scope_completion_remains_73_of_80_with_unknowns(rig):
    rig.prepare()
    state = p.read(rig.batch / "state.json")
    state.update(completed_episodes=33, token_admission_sum=51_600_000)
    p.write(rig.batch / "state.json", state)
    result = p.public_summary(rig.batch, rig.cli)
    assert result["scope_complete"] is True and result["logical_valid_total"] == 73
    assert result["original_matrix_complete"] is False and result["original_matrix_size"] == 80
    assert result["total_cost_computable"] is False and len(result["legacy_unknown_calls"]) == 10


def test_stop_prevents_real_launcher_dispatch(rig):
    rig.prepare()
    (rig.batch / "STOP").touch()
    called = []
    with pytest.raises(p.cont.ContinuationError, match="stopped"):
        p.BASE_LAUNCH(rig.batch, rig.ops / "resume-auto", popen=lambda *a, **k: called.append(a))
    assert not called and not (rig.ops / "resume-auto").exists()


def test_real_base_launch_binds_new_entrypoint_and_33_plan(rig, monkeypatch):
    rig.prepare()
    monkeypatch.setattr(p.cont, "child_environment", lambda: {"PYTHONIOENCODING": "utf-8"})
    seen = []
    def spawn(argv, **kwargs):
        seen.append(list(argv))
        return SimpleNamespace(pid=os.getpid())
    descriptor = p.BASE_LAUNCH(rig.batch, rig.ops / "resume-auto", popen=spawn)
    assert seen == [descriptor["argv"]]
    assert seen[0][2:4] == [str(SOURCE), "_supervise"]
    plan = p.read(rig.ops / "resume-auto/plan.json")
    assert plan["revision"] == p.REVISION and plan["new_attempt_count"] == 33
    assert plan["initial_token_admission_sum"] == 31_800_000
    assert plan["sources"][str(SOURCE)] == p.sha(SOURCE)
    assert not (rig.batch / "results").exists()


def test_supervisor_stops_after_33_without_a_fourth_wave(rig, monkeypatch):
    rig.prepare()
    plan = p.inspect(rig.batch, rig.ops / "resume-auto", cli=rig.cli)
    p.write(Path(plan["run_dir"]) / "plan.json", plan)
    completed = iter((12, 24, 33))
    dispatched = []
    def prepare(plan, cli):
        group = Path(plan["run_dir"]) / f"fake-{len(dispatched)}"
        group.mkdir()
        return group
    result = p.supervise(plan, cli=rig.cli, preparer=prepare,
        launcher=lambda plan, group, tracked: dispatched.append(str(group)) or {"pid": 42},
        waiter=lambda *a: {"new_valid_episodes": next(completed), "cleanup_confirmed": True})
    assert len(dispatched) == 3
    assert result["status"] == "scope_complete_original_matrix_incomplete"
    assert result["logical_valid_total"] == 73 and result["original_matrix_complete"] is False
    assert result["total_admitted_attempts"] == 86


@pytest.mark.parametrize("defect", ["duplicate_unknown", "grade_hash", "history_hash", "not_pass", "missing_salvage"])
def test_bound_evidence_rejected_before_batch_creation(rig, defect):
    report = p.read(rig.report_path)
    if defect == "duplicate_unknown": report["new_unknown_calls"][0]["call_id"] = report["legacy_unknown_calls"][0]["call_id"]
    elif defect == "grade_hash":
        result = p.read(report["salvaged_valid"][0]["path"])
        Path(result["score"]["grader_dir"], "report.json").write_text("changed")
    elif defect == "history_hash": Path(rig.carried[0]["path"]).write_text("changed")
    elif defect == "not_pass": report["status"] = "RUNNING"
    else: report["salvaged_valid"].pop()
    p.write(rig.report_path, report)
    with pytest.raises(p.cont.ContinuationError): rig.prepare()
    assert not rig.batch.exists()


def test_snapshot_path_escape_is_rejected_before_any_copy(rig):
    escaped = rig.old / "escape.py"
    escaped.write_text("outside snapshot")
    manifest = p.read(rig.old / "manifest.json")
    manifest["runtime_sources"] = {"../escape.py": p.sha(escaped)}
    p.write(rig.old / "manifest.json", manifest)
    digest = p.sha(rig.old / "manifest.json")
    p.write(rig.auth_path, {**rig.auth, "source_manifest_sha256": digest})
    p.write(rig.report_path, {**rig.report, "source_manifest_sha256": digest})
    with pytest.raises(p.cont.ContinuationError, match="escaped"):
        rig.prepare()
    assert not rig.batch.exists()


def test_source_state_drift_blocks_fast_admission_check(rig):
    rig.prepare()
    state = p.read(rig.old / "state.json")
    state["changed"] = True
    p.write(rig.old / "state.json", state)
    with pytest.raises(p.cont.ContinuationError, match="Source manifest or state"):
        p.contract(rig.batch, rig.cli, full_evidence=False)


def test_unknown_new_attempt_stops_without_retry_and_cleans_owned_wave(rig):
    rig.prepare()
    plan = p.inspect(rig.batch, rig.ops / "resume-auto", cli=rig.cli)
    p.write(Path(plan["run_dir"]) / "plan.json", plan)
    calls = []; cleanups = []
    def preparer(plan, cli):
        group = Path(plan["run_dir"]) / "wave-001-012"
        group.mkdir()
        return group
    def launcher(plan, group, tracked):
        calls.append(str(group)); tracked.update(pid=42)
        return dict(tracked)
    def waiter(*a): raise p.cont.ContinuationError("New attempt unknown accounting")
    result = p.supervise(plan, cli=rig.cli, preparer=preparer, launcher=launcher, waiter=waiter,
        aborter=lambda *a, **k: cleanups.append(k["descriptor"]) or {"confirmed": True})
    assert result["status"] == "stopped" and len(calls) == 1
    assert cleanups == [{"pid": 42}]
    assert result["automatic_retry_allowed"] is False


def test_dispatch_persistence_failure_stops_recorded_controller(rig, monkeypatch):
    rig.prepare()
    plan = p.inspect(rig.batch, rig.ops / "resume-auto", cli=rig.cli)
    p.write(Path(plan["run_dir"]) / "plan.json", plan)
    original = p.write; saved = []; killed = []
    def write(path, value):
        if Path(path).name == "resume-state.json" and value.get("status") == "running":
            raise OSError("injected disk write failure")
        original(path, value)
    monkeypatch.setattr(p, "write", write)
    def prepare(plan, cli):
        group = Path(plan["run_dir"]) / "wave-001-012"; group.mkdir(); return group
    def launch(plan, group, tracked):
        tracked.update(pid=123); saved.append(123); return dict(tracked)
    result = p.supervise(plan, cli=rig.cli, preparer=prepare, launcher=launch,
        waiter=lambda *a: pytest.fail("must not wait after lost state"),
        aborter=lambda *a, **k: killed.append(k["descriptor"]["pid"]) or {"confirmed": True})
    assert saved == killed == [123] and result["status"] == "stopped"
