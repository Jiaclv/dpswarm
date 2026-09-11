"""Four-episode supervisor contract tests; no real process, model or container."""
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_parallel_controller_fixture", HERE / "parallel_controller.py")
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)
base_spec = importlib.util.spec_from_file_location("_recovery_base_fixture", Path(__file__).with_name("test_recovery_controller.py"))
base = importlib.util.module_from_spec(base_spec)
base_spec.loader.exec_module(base)
rig = base.rig
NOW, write = base.NOW, base.write


class FakeGuard:
    def __init__(self):
        self.calls = []
    def tick(self):
        self.calls.append("tick")
        return {"owned_memory_bytes": 0}
    def start(self):
        self.calls.append("start")
        return self
    def stop(self):
        self.calls.append("stop")
        return {"stopped": True, "tripped": False}


@pytest.fixture
def trial(rig, monkeypatch):
    for entry in rig.schedule[1:4]:
        rig.complete(entry)
    state = p.read(rig.batch / "state.json")
    state.update(completed_episodes=4, token_admission_sum=2400000,
                 known_cost_usd=base.FIRST_COST + .75,
                 episodes=[{"run_id": entry["run_id"],
                            "path": str(rig.batch / "results" / entry["run_id"] / "episode_result.json"),
                            "sha256": p.sha(rig.batch / "results" / entry["run_id"] / "episode_result.json")}
                           for entry in rig.schedule[:4]])
    group_dir = rig.batch.parent / "parallel-trial-4-v1"
    record = group_dir / "transition.json"
    write(record, {"status": "ready"})
    state["parallel_transition"] = {"status": "ready", "transition_id": group_dir.name, "record": str(record)}
    write(rig.batch / "state.json", state)
    policy = {"version": 1, "max_parallel_episodes": 4, "global_model_slots": 8,
              "candidate_container_cap": 7, "candidate_memory": "3g", "candidate_cpus": 2,
              "run_ids": [entry["run_id"] for entry in rig.schedule[4:8]],
              "manifest_sha256": p.sha(rig.batch / "manifest.json")}
    write(group_dir / "policy.json", policy)
    monkeypatch.setattr(p, "source_identity", lambda: {"fixture": "unchanged"})
    guard = FakeGuard()
    prior_cost = state["known_cost_usd"]
    processes, stopped = {}, []
    def inspect(**kwargs):
        return p.inspect_trial(rig.batch, group_dir, cli=rig.cli,
                               now=kwargs.pop("now", NOW), process_check=lambda _: {"stopped": True}, **kwargs)
    def prepare():
        plan = inspect()
        plan["operations_sources"] = p.source_identity()
        group = {"version": 1, "batch": str(rig.batch), "run_ids": plan["run_ids"],
                 "manifest_sha256": plan["manifest_sha256"], "operations_sources": plan["operations_sources"],
                 "global_model_slots": 8, "candidate_container_cap": 7}
        write(group_dir / "group.json", group)
        plan["group_sha256"] = p.sha(group_dir / "group.json")
        write(group_dir / "controller-launch" / "plan.json", plan)
        return plan
    def spawn(argv, **kwargs):
        run_id = argv[argv.index("--run-id") + 1]
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        assert "_episode" not in argv and str(p.WRAPPER) in argv
        process = base.FakeProcess(None)
        process.pid = 500000 + len(processes)
        processes[run_id] = process
        rig.complete(next(e for e in rig.schedule if e["run_id"] == run_id))
        write(group_dir / "candidates" / (run_id + ".json"),
              {"run_id": run_id, "group_sha256": p.sha(group_dir / "group.json"),
               "phase": "candidate_closed", "cleanup_confirmed": True})
        if len(processes) == 4:
            for item in processes.values():
                item.code = 0
        return process
    def stop_tree(process, tracked, cli):
        stopped.append(process.pid)
        process.code = process.code if process.code is not None else -9
        return {"confirmed": True, "errors": []}
    def run(**kwargs):
        return p.run_group(rig.batch, group_dir, cli=rig.cli, now=kwargs.pop("now", lambda: NOW),
                           clock=kwargs.pop("clock", lambda: 0), sleep=kwargs.pop("sleep", lambda _: None),
                           popen=kwargs.pop("popen", spawn),
                           guard_factory=kwargs.pop("guard_factory", lambda _: guard),
                           observe_tree=kwargs.pop("observe_tree", lambda _: []),
                           stop_tree=kwargs.pop("stop_tree", stop_tree), **kwargs)
    return SimpleNamespace(**locals())


def test_read_only_inspect_locks_next_four_and_preserves_original_totals(trial):
    before = (trial.rig.batch / "state.json").read_bytes()
    plan = trial.inspect()
    assert plan["run_ids"] == [e["run_id"] for e in trial.rig.schedule[4:8]]
    assert plan["prior_token_admission_sum"] == 2400000
    assert plan["prior_known_cost_usd"] == trial.prior_cost
    assert plan["remaining_dispatch_seconds"] == 8*3600
    assert (trial.rig.batch / "state.json").read_bytes() == before


@pytest.mark.parametrize("defect", ["active", "stopped", "marker", "token", "cost", "prefix", "existing_remaining", "lease"])
def test_ambiguous_transition_cannot_admit(trial, defect):
    state = p.read(trial.rig.batch / "state.json")
    if defect == "active":
        state["active_episodes"] = {"unfinished": {}}
    elif defect == "stopped":
        state["stop_reason"] = "stop_requested"
    elif defect == "marker":
        state["parallel_transition"]["status"] = "unapproved"
    elif defect == "token":
        state["token_admission_sum"] += 600000
    elif defect == "cost":
        state["known_cost_usd"] = 0
    elif defect == "prefix":
        state["episodes"][0]["sha256"] = "changed"
    elif defect == "existing_remaining":
        (trial.rig.batch / "results" / trial.rig.schedule[4]["run_id"]).mkdir()
    else:
        Path(trial.rig.manifest["resource_lock_path"]).touch()
    write(trial.rig.batch / "state.json", state)
    with pytest.raises(p.ParallelError):
        trial.inspect()
    assert not trial.processes


@pytest.mark.parametrize("key,value", [("max_parallel_episodes", 5), ("global_model_slots", 16),
                                      ("candidate_container_cap", 8), ("candidate_memory", "4g"),
                                      ("candidate_cpus", 3), ("run_ids", ["different"]),
                                      ("manifest_sha256", "changed")])
def test_policy_cannot_change_trial_scope_or_caps(trial, key, value):
    policy = trial.policy | {key: value}
    write(trial.group_dir / "policy.json", policy)
    with pytest.raises(p.ParallelError):
        trial.inspect()


def test_original_nine_hour_window_remains_binding(trial):
    with pytest.raises(p.ParallelError, match="deadline"):
        trial.inspect(now=NOW + timedelta(hours=8))


def test_four_children_overlap_and_then_stop_at_eight_without_touching_other_episodes(trial):
    plan = trial.prepare()
    original = {e["run_id"]: (trial.rig.batch / "results" / e["run_id"] / "episode_result.json").read_bytes()
                for e in trial.rig.schedule[:4]}
    result = trial.run()
    state = p.read(trial.rig.batch / "state.json")
    assert list(trial.processes) == plan["run_ids"]
    assert len(trial.processes) == 4
    assert result["status"] == "completed_awaiting_review" and result["terminal_episodes"] == 4
    assert result["no_automatic_continuation"] and result["cleanup_confirmed"]
    assert state["completed_episodes"] == 8 and state["token_admission_sum"] == 4800000
    assert state["known_cost_usd"] == pytest.approx(trial.prior_cost + 1)
    assert state["started_at"] == trial.state["started_at"]
    assert state["stop_reason"] == "parallel_trial_complete_review_required" and not state["active_episodes"]
    assert [item["run_id"] for item in state["episodes"]] == [e["run_id"] for e in trial.rig.schedule[:8]]
    assert trial.guard.calls == ["tick", "start", "stop"]
    for entry in trial.rig.schedule[8:]:
        assert not (trial.rig.batch / "results" / entry["run_id"]).exists()
    for identity, before in original.items():
        assert (trial.rig.batch / "results" / identity / "episode_result.json").read_bytes() == before


def test_resource_preflight_failure_starts_zero_children(trial):
    trial.prepare()
    trial.guard.tick = lambda: {"sample_error": "no resource evidence"}
    result = trial.run()
    state = p.read(trial.rig.batch / "state.json")
    assert not trial.processes and state["token_admission_sum"] == 2400000
    assert result["status"] == "failed" and (trial.group_dir / "CANCEL").exists()


def test_generation_failure_cancels_all_and_keeps_every_known_cost(trial):
    trial.prepare()
    saved = {}
    def spawn(argv, **kwargs):
        process = trial.spawn(argv, **kwargs)
        run_id = argv[argv.index("--run-id")+1]
        path = trial.rig.batch / "results" / run_id / "episode_result.json"
        if len(trial.processes) == 1:
            value = p.read(path)
            value["infrastructure_error"] = {"type": "ProviderFailure"}
            write(path, value)
            saved["path"], saved["bytes"] = path, path.read_bytes()
        return process
    result = trial.run(popen=spawn)
    state = p.read(trial.rig.batch / "state.json")
    assert result["status"] == "failed" and len(trial.stopped) == 4
    assert len(result["settlements"]) == 4
    assert state["known_cost_usd"] == pytest.approx(trial.prior_cost + 1)
    assert state["token_admission_sum"] == 4800000
    assert saved["path"].read_bytes() == saved["bytes"]
    assert (trial.group_dir / "CANCEL").exists()


def test_unknown_cost_is_explicit_and_does_not_erase_other_calls(trial):
    trial.prepare()
    def spawn(argv, **kwargs):
        process = trial.spawn(argv, **kwargs)
        if len(trial.processes) == 1:
            run_id = argv[argv.index("--run-id")+1]
            account_path = trial.rig.batch / "results" / run_id / "account.json"
            account = p.read(account_path) | {"cost_computable": False}
            write(account_path, account)
        return process
    result = trial.run(popen=spawn)
    state = p.read(trial.rig.batch / "state.json")
    first = result["settlements"][next(iter(trial.processes))]
    assert result["status"] == "failed" and first["cost_computable"] is False
    assert state["known_cost_usd"] == pytest.approx(trial.prior_cost + 1)


def test_resource_trip_cancels_and_settles_all_owned_children(trial):
    trial.prepare()
    def spawn(argv, **kwargs):
        process = trial.spawn(argv, **kwargs)
        if len(trial.processes) == 4:
            write(trial.group_dir / "TRIP.json", {"reason": "owned_memory_threshold"})
        return process
    result = trial.run(popen=spawn)
    assert result["status"] == "failed" and len(trial.stopped) == 4
    assert len(result["settlements"]) == 4
    assert not p.read(trial.rig.batch / "state.json")["active_episodes"]


def test_unconfirmed_cleanup_retains_global_lease_and_identity(trial):
    trial.prepare()
    trial.rig.cli.cleanup_owned_episode = lambda _: {"confirmed": False}
    result = trial.run()
    state = p.read(trial.rig.batch / "state.json")
    assert result["status"] == "failed" and result["cleanup_confirmed"] is False
    assert trial.rig.leases[0]["retain"] is True and len(state["active_episodes"]) == 4


def test_popen_failure_retains_admission_and_stops_already_started_children(trial):
    trial.prepare()
    def spawn(argv, **kwargs):
        if len(trial.processes) == 2:
            raise OSError("cannot start third wrapper")
        return trial.spawn(argv, **kwargs)
    result = trial.run(popen=spawn)
    state = p.read(trial.rig.batch / "state.json")
    assert result["status"] == "failed" and len(trial.stopped) == 2
    assert state["token_admission_sum"] == 4200000
    assert len(result["settlements"]) == 3
    assert state["known_cost_usd"] == pytest.approx(trial.prior_cost + .5)
    assert not state["active_episodes"]


def test_changed_group_after_launch_blocks_all_child_admission(trial):
    trial.prepare()
    group = p.read(trial.group_dir / "group.json")
    group["global_model_slots"] = 99
    write(trial.group_dir / "group.json", group)
    with pytest.raises(p.ParallelError, match="Group contract changed"):
        trial.run()
    assert not trial.processes


def test_marker_projection_failure_cannot_double_count_settlement(trial, monkeypatch):
    trial.prepare()
    marker_calls = []
    def spawn(argv, **kwargs):
        process = trial.spawn(argv, **kwargs)
        run_id = argv[argv.index("--run-id") + 1]
        (trial.group_dir / "candidates" / (run_id + ".json")).unlink()
        return process
    original = p.failed_marker
    def fail_once(*args, **kwargs):
        marker_calls.append(args[1])
        if len(marker_calls) == 1:
            raise OSError("marker projection failed after complete audit")
        return original(*args, **kwargs)
    monkeypatch.setattr(p, "failed_marker", fail_once)
    result = trial.run(popen=spawn)
    state = p.read(trial.rig.batch / "state.json")
    assert result["status"] == "failed"
    assert len(result["settlements"]) == 4
    assert state["known_cost_usd"] == pytest.approx(trial.prior_cost + 1)
    assert state["completed_episodes"] == 8
    assert len({item["run_id"] for item in state["episodes"]}) == 8
    assert marker_calls.count(next(iter(trial.processes))) == 1


def test_resource_abort_stops_all_children_before_first_container_cleanup(trial):
    trial.prepare()
    original = trial.rig.cli.cleanup_owned_episode
    def cleanup(directory):
        assert len(trial.stopped) == 4, "No slow container cleanup may delay stopping another model process"
        return original(directory)
    trial.rig.cli.cleanup_owned_episode = cleanup
    def spawn(argv, **kwargs):
        process = trial.spawn(argv, **kwargs)
        if len(trial.processes) == 4:
            write(trial.group_dir / "TRIP.json", {"reason": "resource limit"})
        return process
    result = trial.run(popen=spawn)
    assert result["status"] == "failed"
    assert result["cleanup_confirmed"]


def test_late_monitor_trip_never_reports_success(trial):
    trial.prepare()
    trial.guard.stop = lambda: {"stopped": True, "tripped": True}
    result = trial.run()
    assert result["status"] == "failed"
    assert p.read(trial.rig.batch / "state.json")["stop_reason"] == "parallel_trial_failed"


def test_finite_group_watchdog_stops_all_four(trial):
    trial.prepare()
    tick = [0]
    def clock():
        tick[0] += 1
        return 0 if tick[0] <= 5 else p.GROUP_WATCHDOG_SECONDS + 1
    def spawn(argv, **kwargs):
        process = trial.spawn(argv, **kwargs)
        for item in trial.processes.values():
            item.code = None
        return process
    result = trial.run(popen=spawn, clock=clock)
    assert result["status"] == "failed" and len(trial.stopped) == 4
    assert "watchdog" in result["failure"]["message"]



def one_gib_policy(trial, monkeypatch, run_ids):
    profile_dir = trial.group_dir / "profile-fixture"
    write(profile_dir / "resource-defaults.json", {
        "version": 1, "candidate_memory": "1g", "evaluation_memory": "1g",
        "memory_swap_equals_memory": True})
    (profile_dir / "memory_profile.py").write_text("# offline profile fixture\n", encoding="utf-8")
    monkeypatch.setattr(p, "HERE", profile_dir)
    policy = trial.policy | {"candidate_memory": "1g", "grader_memory": "1g",
                             "memory_admission_mode": "bounded_container_limits",
                             "resource_profile": "resource-defaults.json", "run_ids": run_ids}
    write(trial.group_dir / "policy.json", policy)
    return profile_dir


def test_final_a1_pair_preserves_eight_results_budget_and_original_time(trial, monkeypatch):
    for entry in trial.rig.schedule[4:8]:
        trial.rig.complete(entry)
    state = p.read(trial.rig.batch / "state.json")
    state.update(completed_episodes=8, token_admission_sum=4800000,
                 known_cost_usd=trial.prior_cost + 1,
                 episodes=[{"run_id": e["run_id"],
                            "path": str(trial.rig.batch / "results" / e["run_id"] / "episode_result.json"),
                            "sha256": p.sha(trial.rig.batch / "results" / e["run_id"] / "episode_result.json")}
                           for e in trial.rig.schedule[:8]])
    write(trial.rig.batch / "state.json", state)
    ids = [e["run_id"] for e in trial.rig.schedule[8:]]
    profile = one_gib_policy(trial, monkeypatch, ids)
    plan = trial.prepare()
    assert plan["prior_completed_episodes"] == 8 and plan["run_ids"] == ids
    assert plan["group_episode_count"] == 2 and plan["watchdog_seconds"] == 5700
    assert plan["profile_sha256"] == p.sha(profile / "resource-defaults.json")
    before = {entry["run_id"]: (trial.rig.batch / "results" / entry["run_id"] / "episode_result.json").read_bytes()
              for entry in trial.rig.schedule[:8]}
    def spawn(argv, **kwargs):
        process = trial.spawn(argv, **kwargs)
        if len(trial.processes) == 2:
            for item in trial.processes.values():
                item.code = 0
        return process
    result = trial.run(popen=spawn)
    final = p.read(trial.rig.batch / "state.json")
    assert list(trial.processes) == ids and result["completed_count"] == 2
    assert result["status"] == "completed_awaiting_review" and result["stage_complete"]
    assert final["completed_episodes"] == 10 and final["token_admission_sum"] == 6000000
    assert final["known_cost_usd"] == pytest.approx(state["known_cost_usd"] + .5)
    assert final["stop_reason"] is None and final["started_at"] == state["started_at"]
    assert [item["run_id"] for item in final["episodes"]] == [e["run_id"] for e in trial.rig.schedule]
    for identity, data in before.items():
        assert (trial.rig.batch / "results" / identity / "episode_result.json").read_bytes() == data


def test_profile_change_after_plan_blocks_before_any_admission(trial, monkeypatch):
    directory = one_gib_policy(trial, monkeypatch, [e["run_id"] for e in trial.rig.schedule[4:8]])
    trial.prepare()
    profile = p.read(directory / "resource-defaults.json")
    profile["unapproved_change"] = True
    write(directory / "resource-defaults.json", profile)
    with pytest.raises(p.ParallelError, match="profile_sha256"):
        trial.run()
    assert not trial.processes


def test_a2_fresh_empty_prefix_is_admissible_without_prior_launch(trial, monkeypatch):
    batch = trial.rig.batch.parent / "fresh-a2"
    group = trial.rig.batch.parent / "a2-group-01"
    entries = [{"run_id": f"a2-{n+1:03d}", "arm": ["S","L","D","T","R2"][n%5],
                "instance": {"instance_id": f"public-{n//5}"}} for n in range(80)]
    manifest = {"stage": "a2", "scheduled_episodes": 80, "schedule": entries,
                "stage_limits": {"dispatch_seconds": 61*3600, "episode_limit": 80,
                                 "token_admission_sum": 48000000, "cost_stop_usd": 480, "cost_warning_usd": 320},
                "resource_lock_path": str(batch.parent / "fresh-stage.lock")}
    write(batch / "manifest.json", manifest)
    write(group / "transition.json", {"stage": "a2"})
    write(batch / "state.json", {"stage": "a2", "started_at": NOW.isoformat(),
          "completed_episodes": 0, "episodes": [], "known_cost_usd": 0, "token_admission_sum": 0,
          "parallel_transition": {"status": "ready", "transition_id": group.name,
                                  "record": str(group / "transition.json")}})
    one_gib_policy(trial, monkeypatch, [e["run_id"] for e in entries[:4]])
    policy = p.read(trial.group_dir / "policy.json") | {"manifest_sha256": p.sha(batch / "manifest.json")}
    write(group / "policy.json", policy)
    plan = p.inspect_trial(batch, group, cli=trial.rig.cli, now=NOW,
                            process_check=lambda _: pytest.fail("fresh stage has no process"))
    assert plan["stage"] == "a2" and plan["prior_completed_episodes"] == 0
    assert plan["completed_prefix"] == [] and plan["previous_controller"] == {"never_launched": True}
    assert plan["remaining_dispatch_seconds"] == 61*3600
    assert len(plan["run_ids"]) == 4


@pytest.fixture
def wave12(trial, monkeypatch):
    """Real controller scheduling with fake processes/containers and an empty A2 prefix."""
    batch = trial.rig.batch.parent / "fresh-wave12"
    group_dir = trial.rig.batch.parent / "wave12"
    arms = ["T", "T", "T", "D", "T", "S", "L", "R2", "S", "D", "L", "R2"]
    entries = [{"run_id": f"a2-{i+1:03d}", "arm": arms[i % len(arms)],
                "instance": {"instance_id": f"public-{i//5}"}} for i in range(80)]
    manifest = {"stage": "a2", "scheduled_episodes": 80, "schedule": entries,
                "stage_limits": {"dispatch_seconds": 61*3600, "episode_limit": 80,
                                 "token_admission_sum": 48000000, "cost_stop_usd": 480},
                "resource_lock_path": str(batch.parent / "wave-stage.lock")}
    write(batch / "manifest.json", manifest)
    write(group_dir / "transition.json", {"stage": "a2", "status": "ready"})
    state = {"stage": "a2", "started_at": NOW.isoformat(), "completed_episodes": 0,
             "episodes": [], "known_cost_usd": 0, "token_admission_sum": 0,
             "parallel_transition": {"status": "ready", "transition_id": group_dir.name,
                                     "record": str(group_dir / "transition.json")}}
    write(batch / "state.json", state)
    one_gib_policy(trial, monkeypatch, [e["run_id"] for e in entries[:12]])
    policy = p.read(trial.group_dir / "policy.json") | {
        "manifest_sha256": p.sha(batch / "manifest.json"), "max_parallel_episodes": 12,
        "candidate_container_cap": 12, "episode_admission_mode": p.ATOMIC_ADMISSION,
        "provider_limits": {"codex_account": 4, "glm_coding": 1, "deepseek": 4},
        "provider_limits_source": "operator_configured", "provider_limits_version": 1,
        "wave_watchdog_policy": "cancel_owned_inflight_at_finite_group_deadline",
    }
    write(group_dir / "policy.json", policy)
    cli = trial.rig.cli
    plan = p.inspect_trial(batch, group_dir, cli=cli, now=NOW)
    plan["operations_sources"] = p.source_identity()
    group = {key: plan[key] for key in (
        "batch", "run_ids", "manifest_sha256", "operations_sources", "global_model_slots",
        "candidate_container_cap", "candidate_requirements", "episode_admission_mode",
        "max_parallel_episodes", "provider_limits", "provider_limits_source", "provider_limits_version",
        "wait_timeout_seconds", "grader_wait_timeout_seconds")}
    group["version"] = 1
    write(group_dir / "group.json", group)
    plan["group_sha256"] = p.sha(group_dir / "group.json")
    write(group_dir / "controller-launch/plan.json", plan)
    processes, live, stopped, generation_batches, receipts = {}, set(), [], [], []
    ticks = [0]
    guard = FakeGuard()

    def complete(entry):
        directory = batch / "results" / entry["run_id"]
        directory.mkdir(parents=True, exist_ok=True)
        patch = directory / "frozen.patch"
        patch.write_text("immutable fake result\n", encoding="utf-8")
        account = {"cost_computable": True, "api_equivalent_known_subtotal_usd": .25,
                   "api_equivalent_usd": .25, "calls": [{"call_id": entry["run_id"]}],
                   "usage_unknown_calls": 0, "protocol_issues": []}
        write(directory / "account.json", account)
        write(directory / "episode_result.json", {
            "run_id": entry["run_id"], "arm": entry["arm"], "instance_id": entry["instance"]["instance_id"],
            "artifact": {"path": str(patch), "sha256": p.sha(patch)}, "quiesced": True,
            "cleanup_confirmed": True, "accounting": account, "score": {"completed": True, "resolved": True},
            "budget": {"unknown_call_count": 0, "pending_call_count": 0}})

    def spawn(argv, **kwargs):
        run_id = argv[argv.index("--run-id")+1]
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        receipt = p.read(group_dir / "admissions" / (run_id + ".json"))
        assert receipt["status"] == "admitted" and receipt["group_sha256"] == plan["group_sha256"]
        assert receipt["candidate_quota"] == plan["candidate_requirements"][run_id]
        receipts.append(receipt)
        process = base.FakeProcess(None)
        process.pid = 800000 + len(processes)
        processes[run_id] = process
        live.add(run_id)
        assert sum(plan["candidate_requirements"][key] for key in live) <= 12
        return process

    def sleep(_):
        if live:
            generation_batches.append(list(live))
        for run_id in list(live):
            write(group_dir / "candidates" / (run_id + ".json"), {
                "run_id": run_id, "group_sha256": plan["group_sha256"],
                "phase": "candidate_closed", "cleanup_confirmed": True})
            live.remove(run_id)
        ticks[0] += 10
        if len(list((group_dir / "candidate-releases").glob("*.json"))) == 12:
            for entry in entries[:12]:
                complete(entry)
                processes[entry["run_id"]].code = 0

    def absent(cli, directory):
        assert directory.name not in live, "Live writers cannot release a quota"
        return []

    def stop(process, tracked, cli):
        stopped.append(process.pid)
        process.code = process.code if process.code is not None else -9
        return {"confirmed": True, "errors": []}

    def run(**kwargs):
        return p.run_group(batch, group_dir, cli=cli, now=lambda: NOW,
                           clock=lambda: ticks[0], sleep=kwargs.pop("sleep", sleep),
                           popen=kwargs.pop("popen", spawn), guard_factory=lambda _: guard,
                           observe_tree=lambda _: [], stop_tree=stop,
                           candidate_absence=kwargs.pop("candidate_absence", absent), **kwargs)
    return SimpleNamespace(**locals())


def test_twelve_wave_atomically_reserves_whole_episodes_and_stops_after_twelve(wave12):
    w = wave12
    result = w.run()
    final = p.read(w.batch / "state.json")
    assert result["status"] == "completed_awaiting_review"
    assert len(w.processes) == 12 and final["completed_episodes"] == 12
    assert final["token_admission_sum"] == 7200000 and final["known_cost_usd"] == pytest.approx(3)
    assert final["started_at"] == w.state["started_at"]
    assert result["candidate_quota_peak"] <= 12 and result["candidate_reservations"] == {}
    assert len(result["candidate_releases"]) == 12 and not result["unadmitted_run_ids"]
    assert result["no_automatic_continuation"] and final["stop_reason"] == "parallel_trial_complete_review_required"
    assert list(w.processes) == [e["run_id"] for e in w.entries[:12]]
    assert [x["run_id"] for x in final["episodes"]] == list(w.processes)
    # The first four use 11 slots. The next T needs 3; the S behind it must
    # not jump the FIFO queue just because its 1 slot would fit.
    assert set(w.generation_batches[0]) == set(e["run_id"] for e in w.entries[:4])
    assert all(x["queue_seconds"] == 0 for x in w.receipts[:4])
    assert all(x["queue_seconds"] > 0 for x in w.receipts[4:])
    for entry in w.entries[12:]:
        assert not (w.batch / "results" / entry["run_id"]).exists()


def test_twelve_wave_live_container_cannot_release_or_admit_next_episode(wave12):
    w = wave12
    def absence(*_):
        raise p.ParallelError("owned candidate remains")
    result = w.run(candidate_absence=absence)
    state = p.read(w.batch / "state.json")
    assert result["status"] == "failed"
    assert len(w.processes) == 4 and len(w.stopped) == 4
    assert state["token_admission_sum"] == 2400000
    assert len(result["unadmitted_run_ids"]) == 8
    assert not list((w.group_dir / "candidate-releases").glob("*.json"))


def test_twelve_wave_cancel_while_queued_never_charges_unstarted_episodes(wave12):
    w = wave12
    def sleep(_):
        write(w.group_dir / "CANCEL", {"reason": "operator"})
    result = w.run(sleep=sleep)
    state = p.read(w.batch / "state.json")
    assert len(w.processes) == 4 and len(w.stopped) == 4
    assert state["token_admission_sum"] == 2400000
    assert len(result["unadmitted_run_ids"]) == 8
    assert all(e["run_id"] not in result["settlements"] for e in w.entries[4:12])


def test_twelve_wave_rejects_forged_release_marker_before_next_admission(wave12):
    w = wave12
    def sleep(_):
        w.sleep(0)
        first = w.entries[0]["run_id"]
        marker = w.group_dir / "candidates" / (first + ".json")
        write(marker, p.read(marker) | {"group_sha256": "different"})
    result = w.run(sleep=sleep)
    assert result["status"] == "failed" and len(w.processes) == 4
    assert "release marker" in result["failure"]["message"]


def test_twelve_wave_time_bound_covers_fifo_batches_and_every_grader(wave12):
    plan = wave12.plan
    assert plan["candidate_peak_requirement"] == 24
    assert plan["generation_batches_upper_bound"] == 3
    assert plan["wait_timeout_seconds"] == 5700
    assert plan["grader_wait_timeout_seconds"] == 21900
    assert plan["watchdog_seconds"] == 27900
    assert plan["previous_controller"] == {"never_launched": True}


def test_controller_launch_sidecar_loads_in_real_wrapper_without_a_real_process(wave12, monkeypatch):
    """Exercise the producer/consumer schema rather than matching separate fake dicts."""
    w = wave12
    group_dir = w.group_dir.parent / "launch-contract-wave"
    write(group_dir / "transition.json", {"stage": "a2", "status": "ready"})
    state = p.read(w.batch / "state.json")
    state["parallel_transition"] = {"status": "ready", "transition_id": group_dir.name,
                                   "record": str(group_dir / "transition.json")}
    write(w.batch / "state.json", state)
    original_policy = p.read(HERE / "resume-a1-1g-v1/policy.json")
    policy = original_policy | w.policy
    write(group_dir / "policy.json", policy)
    monkeypatch.setattr(p, "HERE", HERE)
    source_names = ("parallel_controller.py", "recovery_controller.py", "parallel_episode.py",
                    "parallel_resources.py", "memory_profile.py", "resource-defaults.json",
                    "provider_limits.py")
    monkeypatch.setattr(p, "source_identity", lambda: {name: p.sha(HERE / name) for name in source_names})
    monkeypatch.setattr(p.recovery, "load_frozen_cli", lambda _: w.cli)
    launches = []
    def fake_launch(argv, **kwargs):
        launches.append(argv)
        assert "_run" in argv and str(w.batch) in argv
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        return base.FakeProcess(0)
    monkeypatch.setattr(p.subprocess, "Popen", fake_launch)
    descriptor = p.launch(w.batch, group_dir)
    assert len(launches) == 1 and descriptor["pid"] == base.FakeProcess.pid
    group = p.read(group_dir / "group.json")
    run_id = group["run_ids"][0]
    write(group_dir / "admissions" / (run_id + ".json"), {
        "run_id": run_id, "group_sha256": p.sha(group_dir / "group.json"),
        "status": "admitted", "candidate_quota": group["candidate_requirements"][run_id]})
    wrapper = p.local_module("_controller_wrapper_contract_test", HERE / "parallel_episode.py")
    observed, digest, observed_policy = wrapper.load_group(group_dir, w.batch, run_id)
    assert len(observed["operations_sources"]) == 7 and observed["max_parallel_episodes"] == 12
    assert observed["candidate_container_cap"] == 12
    assert observed["provider_limits"] == {"codex_account": 4, "glm_coding": 1, "deepseek": 4}
    assert observed["episode_admission_mode"] == p.ATOMIC_ADMISSION
    assert digest == p.sha(group_dir / "group.json") and observed_policy == policy


def test_atomic_projection_retries_only_transient_replace_denial(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write(path, {"old": True})
    original = p.os.replace
    attempts = []
    monkeypatch.setattr(p.time, "sleep", lambda _: None)
    def replace(source, target):
        attempts.append(str(source))
        if len(attempts) < 3:
            raise PermissionError("temporary Windows reader")
        return original(source, target)
    monkeypatch.setattr(p.os, "replace", replace)
    p.write(path, {"new": True})
    assert p.read(path) == {"new": True} and len(attempts) == 3
    assert len(set(attempts)) == 1, "retry the same frozen bytes, not the operation"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_projection_permanent_denial_is_bounded_and_preserves_previous(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write(path, {"old": True})
    attempts = []
    monkeypatch.setattr(p.time, "sleep", lambda _: None)
    def replace(source, target):
        attempts.append(source)
        raise PermissionError("permanent")
    monkeypatch.setattr(p.os, "replace", replace)
    with pytest.raises(PermissionError):
        p.write(path, {"new": True})
    assert len(attempts) == 5 and p.read(path) == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_queued_wave_requires_room_for_entire_watchdog_before_admission(wave12):
    w = wave12
    with pytest.raises(p.ParallelError, match="Insufficient original stage dispatch window"):
        p.inspect_trial(w.batch, w.group_dir, cli=w.cli, now=NOW + timedelta(hours=60))
    assert not w.processes
    assert p.read(w.batch / "state.json")["token_admission_sum"] == 0
