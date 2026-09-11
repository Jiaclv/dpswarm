"""Offline scope and launch contracts for the 56-entry resume revision."""
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "a2_resume.py"
spec = importlib.util.spec_from_file_location("_a2_resume_fixture", SOURCE)
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
    for entry in schedule[:12]:
        path = old / "results" / entry["run_id"] / "episode_result.json"
        p.write(path, {"original": True})
        carried.append({"run_id": entry["run_id"], "path": str(path), "sha256": p.sha(path)})
    unknown = [{"call_id": f"unknown-{i}", "reserved_tokens": n}
               for i, n in enumerate((40000, 40000, 38387, 5270, 23624, 22678, 42805))]
    carry_path = old / "carryover.json"
    p.write(carry_path, {"references": {}})
    old_plan = {"carried_valid": carried, "legacy_unknown_calls": unknown[:3],
                "carryover_path": str(carry_path), "carryover_sha256": p.sha(carry_path)}
    p.write(old / "recovery-plan.json", old_plan)
    manifest = {"stage": "a2", "scheduled_episodes": 80, "schedule": schedule,
                "runtime_sources": {}, "input_artifacts": {},
                "resource_lock_path": str(exp / "stage.lock"),
                "stage_limits": {"token_admission_sum": 51_600_000, "cost_stop_usd": 480},
                "recovery_plan_sha256": p.sha(old / "recovery-plan.json")}
    p.write(old / "manifest.json", manifest)
    p.write(old / "state.json", {"token_admission_sum": 18_000_000})
    prior_group = ops / "old-auto/wave-001-012"
    p.write(old / "launch.json", {"parallel_group_dir": str(prior_group)})
    supervisor = prior_group.parent / "recovery-state.json"
    p.write(supervisor, {"status": "stopped"})
    auth = {"revision": p.REVISION, "user_confirmation": "ok",
            "total_admission_cap": 51_600_000, "already_admitted_attempts": 30,
            "already_admitted_tokens": 18_000_000, "remaining_admission_tokens": 33_600_000,
            "new_attempt_count": 56, "new_attempt_token_admission": 600000, "unknown_call_count": 7,
            "salvage_candidate_count": 9, "carried_original_valid_count": 12,
            "maximum_logical_valid_if_all_salvage_qualify": 77, "cost_stop_usd": 480,
            "cost_warning_usd": 320, "stage_dispatch_seconds": 61 * 3600, "package_max_seconds": 96 * 3600,
            "global_model_slots": 8, "candidate_container_cap": 12, "candidate_memory": "1g",
            "automatic_additional_retry": False, "unknown_usage_not_zero": True,
            "provider_limits": {"codex_account": 4, "glm_coding": 1, "deepseek": 4},
            "source_batch": str(old), "source_manifest_sha256": p.sha(old / "manifest.json"),
            "source_state_sha256": p.sha(old / "state.json"), "source_supervisor_sha256": p.sha(supervisor),
            "legacy_known_cost_usd": 38.281241811161465,
            "pending_run_ids": [e["run_id"] for e in schedule[24:]],
            "incomplete_runs_not_retried": [e["run_id"] for e in schedule[21:24]],
            "stage_started_at": (moment - timedelta(hours=5)).isoformat(),
            "package_started_at": (moment - timedelta(hours=13)).isoformat()}
    auth_path, report_path = ops / "authorization.json", ops / "salvage/SALVAGE_REPORT.json"
    p.write(auth_path, auth)
    salvaged = []
    for entry in schedule[12:21]:
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
              "source_manifest_sha256": auth["source_manifest_sha256"], "overall_unknown_count": 7,
              "overall_cost_computable": False, "accounting": {
                  "combined_known_api_equivalent_usd": auth["legacy_known_cost_usd"]},
              "salvaged_valid": salvaged, "legacy_unknown_calls": unknown[:3],
              "new_unknown_calls": unknown[3:], "references": {}}
    p.write(report_path, report)
    raw_cli = SimpleNamespace(reporting=SimpleNamespace(),
        load_manifest=lambda directory: p.read(Path(directory) / "manifest.json"),
        freeze=SimpleNamespace(verify_snapshot=lambda *a: None), validate_schedule=lambda *a: None)
    cli = p.base.CliView(raw_cli)
    monkeypatch.setattr(p.recovery, "load_frozen_cli", lambda _: raw_cli)
    def prepare():
        return p.prepare(batch, authorization_path=auth_path, salvage_report=report_path)
    return SimpleNamespace(**locals())


def test_prepare_preserves_30_admissions_7_unknown_and_only_56_unstarted(rig):
    receipt = rig.prepare()
    value, _, state = p.contract(rig.batch, rig.cli)
    assert receipt["new_attempt_count"] == 56 and receipt["carried_valid_count"] == 21
    assert value["pending_run_ids"] == [e["run_id"] for e in rig.schedule[24:]]
    assert state["token_admission_sum"] == 18_000_000
    assert len(value["legacy_unknown_calls"]) == 7 and value["legacy_unknown_reserved_tokens"] == 212764
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


def test_scope_completion_remains_77_of_80_with_unknowns(rig):
    rig.prepare()
    state = p.read(rig.batch / "state.json")
    state.update(completed_episodes=56, token_admission_sum=51_600_000)
    p.write(rig.batch / "state.json", state)
    result = p.public_summary(rig.batch, rig.cli)
    assert result["scope_complete"] is True and result["logical_valid_total"] == 77
    assert result["original_matrix_complete"] is False and result["original_matrix_size"] == 80
    assert result["total_cost_computable"] is False and len(result["legacy_unknown_calls"]) == 7


def test_stop_prevents_real_launcher_dispatch(rig):
    rig.prepare()
    (rig.batch / "STOP").touch()
    called = []
    with pytest.raises(p.cont.ContinuationError, match="stopped"):
        p.BASE_LAUNCH(rig.batch, rig.ops / "resume-auto", popen=lambda *a, **k: called.append(a))
    assert not called and not (rig.ops / "resume-auto").exists()


def test_real_base_launch_binds_new_entrypoint_and_56_plan(rig, monkeypatch):
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
    assert plan["revision"] == p.REVISION and plan["new_attempt_count"] == 56
    assert plan["initial_token_admission_sum"] == 18_000_000
    assert plan["sources"][str(SOURCE)] == p.sha(SOURCE)
    assert not (rig.batch / "results").exists()


def test_supervisor_stops_after_56_without_a_sixth_wave(rig, monkeypatch):
    rig.prepare()
    plan = p.inspect(rig.batch, rig.ops / "resume-auto", cli=rig.cli)
    p.write(Path(plan["run_dir"]) / "plan.json", plan)
    completed = iter((12, 24, 36, 48, 56))
    dispatched = []
    def prepare(plan, cli):
        group = Path(plan["run_dir"]) / f"fake-{len(dispatched)}"
        group.mkdir()
        return group
    result = p.supervise(plan, cli=rig.cli, preparer=prepare,
        launcher=lambda plan, group, tracked: dispatched.append(str(group)) or {"pid": 42},
        waiter=lambda *a: {"new_valid_episodes": next(completed), "cleanup_confirmed": True})
    assert len(dispatched) == 5
    assert result["status"] == "scope_complete_original_matrix_incomplete"
    assert result["logical_valid_total"] == 77 and result["original_matrix_complete"] is False
    assert result["total_admitted_attempts"] == 86
