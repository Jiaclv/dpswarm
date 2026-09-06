"""One authorized backfill wave for seven missing A2 cells, preserving all history.

Only the operator contract and accounting projection change. Frozen solver,
prompts, grading, per-episode admission, provider gates and resource guards are
reused without editing any historically hash-bound file. There is no retry loop.
"""
from __future__ import annotations
import argparse
import copy
import importlib.util
import json
import math
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
SELF = Path(__file__).resolve()
REVISION = "a2_backfill_v1"
PENDING = ["a2-05-R2", "a2-05-T", "a2-05-S", "a2-09-D", "a2-09-T", "a2-09-S", "a2-10-D"]
PRIOR_ADMISSION, TOTAL_CAP, PRIOR_VALID, NEW_COUNT = 51_600_000, 55_800_000, 73, 7


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


base = module("_a2_backfill_recovery", HERE / "a2_recovery.py")
controller, recovery, cont = base.controller, base.recovery, base.cont
read, write, sha, utc, require = base.read, base.write, base.sha, base.utc, base.require
BASE_INSPECT, BASE_TERMINAL, BASE_LAUNCH = base.inspect, base.terminal_wave, base.launch


def operator_sources():
    paths = [HERE / name for name in controller.source_identity()]
    paths += [SELF, HERE / "a2_recovery.py", HERE / "a2_continuation.py",
              HERE / "a2_recovery_accounting.py", HERE / "idle_memory.py"]
    return {str(path): sha(path) for path in paths}


def authorization(path):
    value = read(path)
    require(value.get("revision") == REVISION and value.get("user_confirmation") == "你把缺失的补上",
            "Explicit authorization for these seven backfill attempts is missing")
    exact = {"total_admission_cap": TOTAL_CAP, "already_admitted_attempts": 86,
        "already_admitted_tokens": PRIOR_ADMISSION, "remaining_admission_tokens": 4_200_000,
        "new_attempt_count": NEW_COUNT, "new_attempt_token_admission": 600000,
        "unknown_call_count": 10, "legacy_unknown_reserved_tokens": 308099,
        "carried_original_valid_count": PRIOR_VALID, "maximum_logical_valid": 80,
        "cost_stop_usd": 480, "cost_warning_usd": 320,
        "stage_dispatch_seconds": 61 * 3600, "package_max_seconds": 96 * 3600,
        "global_model_slots": 8, "candidate_container_cap": 12, "candidate_memory": "1g",
        "pending_run_ids": PENDING}
    require(all(value.get(k) == v for k, v in exact.items()), "Authorized scope, budget or resource limit changed")
    require(value.get("automatic_additional_retry") is False and value.get("unknown_usage_not_zero") is True
            and value.get("provider_limits") == {"codex_account": 4, "glm_coding": 1, "deepseek": 4},
            "Retry, unknown usage or provider gate contract changed")
    require(math.isclose(float(value["legacy_known_cost_usd"]), 123.57437509977163, rel_tol=0, abs_tol=1e-9),
            "Historical known subtotal changed")
    return value


def bind(refs, path, digest=None):
    path = Path(path).resolve()
    actual = sha(path)
    require(digest is None or actual == digest, "Bound evidence changed: " + str(path))
    refs[str(path)] = actual
    return actual


def qualified_result(refs, item):
    path = Path(item["path"]).resolve()
    bind(refs, path, item["sha256"])
    result = read(path)
    score = result.get("score") or {}
    require(result["run_id"] == item["run_id"] and score.get("completed") is True
            and type(score.get("resolved")) is bool and result.get("quiesced") is True
            and result.get("cleanup_confirmed") is True, "Carried result is not valid and quiescent")
    artifact = result.get("artifact") or result.get("lead_artifact") or {}
    bind(refs, artifact["path"], artifact["sha256"])
    require(score.get("patch_sha256") == artifact["sha256"] and score.get("reports_sha256"),
            "Carried result lacks score-to-patch evidence")
    root = Path(score["grader_dir"]).resolve()
    for relative, digest in score["reports_sha256"].items():
        report = (root / relative).resolve()
        require(report.is_relative_to(root), "Official report escaped grader directory")
        bind(refs, report, digest)


def prepare(batch, *, authorization_path):
    batch, authorization_path = Path(batch).resolve(), Path(authorization_path).resolve()
    require(batch.is_relative_to(EXPERIMENT / "batches") and not batch.exists(),
            "Backfill requires a fresh batch inside this experiment")
    auth = authorization(authorization_path)
    source = Path(auth["source_batch"]).resolve()
    require(source == EXPERIMENT / "batches" / "a2-resume-v3" and source != batch,
            "Backfill source must be the completed A2 resume v3 batch")
    refs = {}
    bind(refs, authorization_path)
    bind(refs, source / "manifest.json", auth["source_manifest_sha256"])
    bind(refs, source / "state.json", auth["source_state_sha256"])
    supervisor = Path(auth["source_supervisor_path"]).resolve()
    require(supervisor == HERE / "a2-resume-auto-v3" / "resume-state.json", "Source supervisor differs")
    bind(refs, supervisor, auth["source_supervisor_sha256"])
    previous = read(supervisor)
    require(previous.get("status") == "scope_complete_original_matrix_incomplete"
            and previous.get("logical_valid_total") == PRIOR_VALID
            and previous.get("cleanup_confirmed") is True, "Historical supervisor is not complete and clean")
    old, state = read(source / "manifest.json"), read(source / "state.json")
    bind(refs, source / "recovery-plan.json", old["recovery_plan_sha256"])
    prior = read(source / "recovery-plan.json")
    require(old["stage"] == "a2" and old["scheduled_episodes"] == 80
            and state["completed_episodes"] == 33 and state["token_admission_sum"] == PRIOR_ADMISSION
            and not state.get("active_episodes") and state["parallel_trial"].get("cleanup_confirmed") is True,
            "Historical completion or admission projection changed")
    require(math.isclose(state["known_cost_usd"], auth["legacy_known_cost_usd"], rel_tol=0, abs_tol=1e-9),
            "Known subtotal does not match historical state")
    require(all(auth[k] == state[s] == prior[k] for k, s in
                (("stage_started_at", "started_at"), ("package_started_at", "package_started_at"))),
            "Original stage/package clocks must not reset")
    schedule = [e["run_id"] for e in old["schedule"]]
    require([rid for rid in schedule if rid in PENDING] == PENDING
            and set(prior["incomplete_runs_not_retried"]) == set(PENDING), "Missing-cell identities differ")
    carried = list(prior["carried_valid"]) + list(state["episodes"])
    require(len(carried) == PRIOR_VALID and len({x["run_id"] for x in carried}) == PRIOR_VALID
            and {x["run_id"] for x in carried} == set(schedule) - set(PENDING),
            "Carried evidence must cover exactly the other 73 logical cells")
    carried.sort(key=lambda item: schedule.index(item["run_id"]))
    for path, digest in prior["references"].items():
        bind(refs, path, digest)
    for item in carried:
        qualified_result(refs, item)
    unknown = prior["legacy_unknown_calls"]
    require(len(unknown) == 10 and len({x["call_id"] for x in unknown}) == 10
            and all(type(x["reserved_tokens"]) is int and x["reserved_tokens"] >= 0 for x in unknown)
            and sum(x["reserved_tokens"] for x in unknown) == 308099, "Historical unknown calls changed")
    require(old["stage_limits"]["token_admission_sum"] == PRIOR_ADMISSION
            and old["stage_limits"]["cost_stop_usd"] == 480
            and old["stage_limits"]["cost_warning_usd"] == 320
            and old["stage_limits"]["dispatch_seconds"] == 61 * 3600, "Historical limits changed")
    value = {"version": 1, "revision": REVISION, "source_batch": str(source),
        "source_manifest_sha256": auth["source_manifest_sha256"], "source_state_sha256": auth["source_state_sha256"],
        "authorization_path": str(authorization_path), "authorization_sha256": sha(authorization_path),
        "carried_valid": carried, "pending_run_ids": PENDING, "incomplete_runs_not_retried": [],
        "legacy_admitted_attempts": 86, "legacy_token_admission_sum": PRIOR_ADMISSION,
        "legacy_known_cost_usd": auth["legacy_known_cost_usd"], "legacy_unknown_calls": unknown,
        "legacy_unknown_reserved_tokens": 308099, "total_admission_cap": TOTAL_CAP,
        "cost_stop_usd": 480, "cost_warning_usd": 320, "stage_started_at": auth["stage_started_at"],
        "package_started_at": auth["package_started_at"], "maximum_logical_valid": 80,
        "new_attempt_count": NEW_COUNT, "references": refs, "overall_cost_computable": False,
        "automatic_additional_retry": False}
    require(base.deadline(value) > utc(), "Original deadline has elapsed")
    # Exclusive byte copies of the existing frozen snapshot, never current checkout code.
    pairs = [(Path(relative), digest) for relative, digest in old["runtime_sources"].items()]
    official = Path("modelbench/minimal_value_20260905/official")
    pairs += [(official / relative, digest) for relative, digest in old["input_artifacts"].items()]
    files = []
    for relative, digest in pairs:
        src, dst = (source / "runtime_snapshot" / relative).resolve(), (batch / "runtime_snapshot" / relative).resolve()
        require(src.is_relative_to(source / "runtime_snapshot") and dst.is_relative_to(batch / "runtime_snapshot"),
                "Snapshot entry escaped source or destination root")
        require(sha(src) == digest, "Frozen snapshot changed before copying")
        files.append((src, dst, digest))
    batch.mkdir(parents=True, exist_ok=False)
    for src, dst, digest in files:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("xb") as stream:
            stream.write(src.read_bytes())
        require(sha(src) == sha(dst) == digest, "Frozen snapshot changed while copying")
    write(batch / "recovery-plan.json", value)
    manifest = copy.deepcopy(old)
    manifest.update(root=str(batch / "runtime_snapshot"), recovery_plan_sha256=sha(batch / "recovery-plan.json"))
    manifest["stage_limits"]["token_admission_sum"] = TOTAL_CAP
    write(batch / "manifest.json", manifest)
    (batch / "manifest.sha256").write_text(sha(batch / "manifest.json") + "\n", encoding="ascii")
    write(batch / "state.json", {"stage": "a2", "started_at": value["stage_started_at"],
        "package_started_at": value["package_started_at"], "completed_episodes": 0, "episodes": [],
        "token_admission_sum": PRIOR_ADMISSION, "known_cost_usd": value["legacy_known_cost_usd"],
        "stop_reason": None, "active_episodes": {}, "recovery": {"legacy_unknown_calls": unknown, "run_groups": {}},
        "carried_valid_episodes": PRIOR_VALID, "new_attempts_scheduled": NEW_COUNT, "maximum_logical_valid": 80,
        "overall_cost_computable": False, "recovery_plan_sha256": sha(batch / "recovery-plan.json")})
    contract(batch, base.CliView(recovery.load_frozen_cli(batch)))
    receipt = {"prepared": True, "batch": str(batch), "manifest_sha256": sha(batch / "manifest.json"),
        "recovery_plan_sha256": sha(batch / "recovery-plan.json"), "carried_valid_count": PRIOR_VALID,
        "new_attempt_count": NEW_COUNT, "maximum_logical_valid": 80, "models_started": 0,
        "snapshot_files_verified": len(files), "references_verified": len(refs)}
    write(batch / "preparation.json", receipt)
    return receipt


def contract(batch, cli, *, allow_stopped=False, full_evidence=True):
    batch = Path(batch).resolve()
    manifest, state, value = cli.load_manifest(batch), read(batch / "state.json"), read(batch / "recovery-plan.json")
    require(value.get("revision") == REVISION and value.get("version") == 1
            and manifest["recovery_plan_sha256"] == sha(batch / "recovery-plan.json"), "Backfill contract changed")
    auth = authorization(value["authorization_path"])
    require(sha(value["authorization_path"]) == value["authorization_sha256"], "Authorization changed")
    for key in ("pending_run_ids", "legacy_known_cost_usd", "stage_started_at", "package_started_at"):
        require(value[key] == auth[key], "Plan changed authorized field: " + key)
    require(value["legacy_token_admission_sum"] == PRIOR_ADMISSION and value["legacy_admitted_attempts"] == 86
            and value["total_admission_cap"] == TOTAL_CAP and value["new_attempt_count"] == NEW_COUNT
            and value["maximum_logical_valid"] == 80 and value["automatic_additional_retry"] is False,
            "Backfill counts, cap or retry contract changed")
    source = Path(value["source_batch"])
    require(source == EXPERIMENT / "batches" / "a2-resume-v3"
            and sha(source / "manifest.json") == value["source_manifest_sha256"]
            and sha(source / "state.json") == value["source_state_sha256"], "Historical batch changed")
    old = read(source / "manifest.json")
    expected = copy.deepcopy(old)
    expected.update(root=str(batch / "runtime_snapshot"), recovery_plan_sha256=sha(batch / "recovery-plan.json"))
    expected["stage_limits"]["token_admission_sum"] = TOTAL_CAP
    require(manifest == expected, "Frozen experiment changed beyond authorized cumulative admission cap")
    require(state["started_at"] == value["stage_started_at"] and state["package_started_at"] == value["package_started_at"],
            "Original stage/package clocks reset")
    prior = read(source / "recovery-plan.json")
    carried = list(prior["carried_valid"]) + list(read(source / "state.json")["episodes"])
    order = {entry["run_id"]: i for i, entry in enumerate(manifest["schedule"])}
    carried.sort(key=lambda item: order[item["run_id"]])
    require(value["carried_valid"] == carried and len(carried) == PRIOR_VALID
            and len({x["run_id"] for x in carried}) == PRIOR_VALID
            and {x["run_id"] for x in carried} == set(order) - set(PENDING), "Carried 73-cell evidence changed")
    require(value["legacy_unknown_calls"] == prior["legacy_unknown_calls"]
            and len(value["legacy_unknown_calls"]) == 10 and value["legacy_unknown_reserved_tokens"] == 308099
            and value["overall_cost_computable"] is False, "Unknown calls cannot be replaced by zero usage")
    require(allow_stopped or not any((batch / marker).exists() for marker in ("STOP", "CANCEL", "TRIP.json")),
            "Backfill is stopped")
    if full_evidence:
        for path, digest in value["references"].items():
            require(sha(path) == digest, "Historical evidence changed")
        cli.freeze.verify_snapshot(batch, manifest)
        cli.validate_schedule(manifest["schedule"], "a2")
    return value, manifest, state


def prefix_audit(batch, cli):
    value, manifest, state = contract(batch, cli)
    count = state["completed_episodes"]
    require(type(count) is int and 0 <= count <= NEW_COUNT, "New valid count is invalid")
    lookup = {entry["run_id"]: entry for entry in manifest["schedule"]}
    mapping = state.get("recovery", {}).get("run_groups", {})
    audited = []
    for rid in PENDING[:count]:
        require(rid in mapping, "New result has no bound wave")
        cont.group_identity(batch, Path(mapping[rid]))
        audited.append(base.audit_attempt(batch, lookup[rid], cli, mapping[rid]))
    expected = [{"run_id": x["entry"]["run_id"], "path": x["path"], "sha256": x["sha256"]} for x in audited]
    require(state["episodes"] == expected, "New valid results differ from audited prefix")
    require(state["token_admission_sum"] == PRIOR_ADMISSION + count * 600000,
            "An interrupted admission cannot be refunded or retried")
    known = value["legacy_known_cost_usd"] + sum(x["known_cost_usd"] for x in audited)
    require(math.isclose(state["known_cost_usd"], known, rel_tol=0, abs_tol=1e-9), "Known subtotal differs")
    root = Path(batch) / "results"
    existing = {p.name for p in root.iterdir()} if root.exists() else set()
    require(existing == set(PENDING[:count]), "An unsettled or unexpected attempt exists")
    return value, manifest, state, expected, known


def public_summary(batch, cli):
    value, _, state = contract(batch, cli, allow_stopped=True)
    count = state["completed_episodes"]
    require(type(count) is int and 0 <= count <= NEW_COUNT, "Invalid result count")
    done = count == NEW_COUNT
    return {"revision": REVISION, "new_valid_episodes": count, "carried_valid_episodes": PRIOR_VALID,
        "logical_valid_total": PRIOR_VALID + count, "historical_admitted_attempts": 86,
        "new_admitted_attempts": (state["token_admission_sum"] - PRIOR_ADMISSION) // 600000,
        "cumulative_token_admission_sum": state["token_admission_sum"],
        "cumulative_known_api_equivalent_usd": state["known_cost_usd"],
        "legacy_unknown_calls": value["legacy_unknown_calls"], "legacy_unknown_reserved_tokens": 308099,
        "total_cost_computable": False, "cost_stop_applies_to_known_subtotal": True,
        "known_cost_warning": state["known_cost_usd"] >= 320, "known_cost_stop_usd": 480,
        "original_matrix_complete": done, "original_matrix_size": 80, "maximum_logical_valid": 80,
        "remaining_missing_run_ids": [rid for rid in PENDING if rid not in {x["run_id"] for x in state["episodes"]}],
        "scope_complete": done, "new_results": state["episodes"], "carried_results": value["carried_valid"],
        "status": "completed" if done else "in_progress_or_stopped", "automatic_additional_retry": False,
        "original_deadline": base.deadline(value).isoformat(), "stop_reason": state.get("stop_reason"), "at": utc().isoformat()}


def inspect(batch, run_dir, *, cli=None):
    result = BASE_INSPECT(batch, run_dir, cli=cli)
    result.update(initial_token_admission_sum=PRIOR_ADMISSION, new_attempt_count=NEW_COUNT,
                  maximum_logical_valid=80, carried_valid_count=PRIOR_VALID)
    return result


def inspect_wave(batch, group_dir, *, policy_path=None, cli=None, now=None, process_check=None, check_previous=True):
    """Same frozen wave guards with the explicit 55.8M cumulative admission cap."""
    batch, group_dir = Path(batch).resolve(), Path(group_dir).resolve()
    cli = cli or base.CliView(recovery.load_frozen_cli(batch))
    value, manifest, state, prefix, known = prefix_audit(batch, cli)
    require(not state.get("active_run_id") and not state.get("active_episodes")
            and not state.get("completed_at") and not state.get("stop_reason"), "Backfill transition is not ready")
    policy_path = Path(policy_path or group_dir / "policy.json").resolve()
    policy = controller.policy_contract(read(policy_path))
    require(policy["max_parallel_episodes"] == 12 and policy["candidate_memory"] == "1g"
            and policy.get("wave_watchdog_policy") == "cancel_owned_inflight_at_finite_group_deadline"
            and policy["global_model_slots"] == 8 and policy["candidate_container_cap"] == 12
            and policy["provider_limits"] == {"codex_account": 4, "glm_coding": 1, "deepseek": 4},
            "Approved provider, resource or watchdog policy changed")
    for root in (batch, group_dir, Path(policy["recovery_run_dir"])):
        require(not any((root / name).exists() for name in ("STOP", "CANCEL", "TRIP.json")), "Backfill admission stopped")
    transition = state.get("parallel_transition") or {}
    record = Path(transition.get("record", "")).resolve()
    require(transition.get("status") == "ready" and transition.get("transition_id") == group_dir.name
            and record.is_file() and record.is_relative_to(group_dir), "Backfill transition is unbound")
    require(read(record)["recovery_plan_sha256"] == sha(batch / "recovery-plan.json"), "Transition plan changed")
    base.prior_stop_guard(group_dir)
    ids = policy["run_ids"]
    require(not prefix and ids == PENDING, "Only one complete seven-attempt wave is authorized")
    require(policy["manifest_sha256"] == sha(batch / "manifest.json"), "Wave manifest differs")
    require(state["token_admission_sum"] + len(ids) * 600000 <= value["total_admission_cap"] == TOTAL_CAP
            and known < value["cost_stop_usd"], "Authorized backfill admission or cost limit reached")
    require(not Path(manifest["resource_lock_path"]).exists(), "Shared stage lease remains")
    previous = None
    if check_previous and (batch / "launch.json").exists():
        previous = (process_check or recovery.previous_controller_status)(batch)
    elif check_previous:
        require(not (batch / "controller.lock").exists(), "Prior controller identity is missing")
        previous = {"never_launched": True}
    lookup = {entry["run_id"]: entry for entry in manifest["schedule"]}
    requirements = controller.candidate_requirements([lookup[rid] for rid in ids])
    timing = controller.wave_time_limits(requirements, policy)
    current = now or utc()
    require((base.deadline(value) - current).total_seconds() >= timing["watchdog_seconds"] + 300,
            "Original stage/package window cannot fit the entire backfill wave")
    return {"version": 1, "batch": str(batch), "group_dir": str(group_dir), "stage": "a2",
        "manifest_sha256": sha(batch / "manifest.json"), "state_sha256": sha(batch / "state.json"),
        "policy_path": str(policy_path), "policy_sha256": sha(policy_path),
        "transition_record_path": str(record), "transition_record_sha256": sha(record),
        "completed_prefix": prefix, "run_ids": ids, "group_episode_count": len(ids),
        "candidate_peak_requirement": sum(requirements.values()), "candidate_requirements": requirements,
        "episode_admission_mode": policy["episode_admission_mode"], "provider_limits": policy["provider_limits"],
        "provider_limits_source": policy["provider_limits_source"], "provider_limits_version": policy["provider_limits_version"],
        "max_parallel_episodes": 12, **timing, "wave_watchdog_policy": policy["wave_watchdog_policy"],
        "memory_admission_mode": policy["memory_admission_mode"], "prior_completed_episodes": len(prefix),
        "prior_token_admission_sum": state["token_admission_sum"], "prior_known_cost_usd": known,
        "original_started_at": state["started_at"], "original_dispatch_deadline": base.deadline(value).isoformat(),
        "remaining_dispatch_seconds": (base.deadline(value) - current).total_seconds(), "previous_controller": previous,
        "global_model_slots": 8, "candidate_container_cap": 12, **controller.profile_binding(policy),
        "recovery_sources": operator_sources(), "recovery_plan_sha256": sha(batch / "recovery-plan.json")}


def prepare_wave(plan, cli, *, headroom=None):
    base.verify_supervisor(plan, cli)
    _, manifest, state, prefix, _ = prefix_audit(plan["batch"], cli)
    require(not prefix and not state.get("active_episodes"), "Backfill wave has already started")
    group = Path(plan["run_dir"]) / "wave-001-007"
    policy = base.policy_for(plan, PENDING)
    policy["scope"] = "Seven explicitly authorized missing cells once; retain 86 old attempts and ten unknown calls"
    lookup = {entry["run_id"]: entry for entry in manifest["schedule"]}
    timing = controller.wave_time_limits(controller.candidate_requirements([lookup[rid] for rid in PENDING]), policy)
    require((base.timestamp(plan["deadline"]) - utc()).total_seconds() >= timing["watchdog_seconds"] + 300,
            "Original window cannot fit this backfill wave")
    group.mkdir(parents=True, exist_ok=False)
    batch = Path(plan["batch"])
    before = sha(batch / "state.json")
    shutil.copyfile(batch / "state.json", group / "previous-state.json")
    require(sha(group / "previous-state.json") == before, "State changed during archival")
    helper = headroom or module("_backfill_idle", HERE / "idle_memory.py").ensure_idle_headroom
    require(helper(group / "idle-memory.json", manifest["resource_lock_path"], minimum_gib=4).get("ready") is True,
            "Idle memory or resource lease not ready")
    write(group / "policy.json", policy)
    write(group / "transition.json", {"status": "ready", "transition_id": group.name,
        "recovery_plan_sha256": plan["recovery_plan_sha256"], "prior_state_sha256": before,
        "new_valid_prefix": [], "legacy_admitted_attempts": 86, "previous_group": None, "at": utc().isoformat()})
    with cli.stage_lease(manifest["resource_lock_path"], batch):
        base.verify_supervisor(plan, cli)
        require(sha(batch / "state.json") == before, "Another writer changed backfill state")
        updated = copy.deepcopy(state)
        updated["recovery"]["run_groups"] = {rid: str(group) for rid in PENDING}
        updated["parallel_transition"] = {"status": "ready", "transition_id": group.name,
            "record": str(group / "transition.json"), "group_dir": str(group), "policy_sha256": sha(group / "policy.json")}
        write(batch / "state.json", updated)
    inspect_wave(batch, group, cli=cli)
    return group


def terminal_wave(plan, group, cli):
    result = BASE_TERMINAL(plan, group, cli)
    require(result["new_valid_episodes"] == NEW_COUNT, "Seven valid official results are required for completion")
    result["logical_valid_total"] = PRIOR_VALID + result["new_valid_episodes"]
    return result


def supervise(plan, *, cli=None, preparer=None, launcher=None, waiter=None, aborter=None):
    cli = cli or base.CliView(recovery.load_frozen_cli(plan["batch"]))
    preparer, launcher, waiter = preparer or prepare_wave, launcher or base.launch_wave, waiter or base.wait_wave
    aborter = aborter or cont.abort_owned
    root, group, tracked, safe = Path(plan["run_dir"]), None, {}, False
    state = {"revision": REVISION, "status": "starting", "waves": [], "deadline": plan["deadline"],
        "legacy_unknown_calls": plan["legacy_unknown_calls"], "automatic_retry_allowed": False,
        "maximum_logical_valid": 80}
    with cont.continuation_lease(plan["batch"], root):
        try:
            base.verify_supervisor(plan, cli)
            group = preparer(plan, cli)
            state.update(status="dispatching", active_group=str(group))
            write(root / "backfill-state.json", state)
            state["controller"] = launcher(plan, group, tracked)
            state["status"] = "running"
            write(root / "backfill-state.json", state)
            result = waiter(plan, group, cli)
            require(result["new_valid_episodes"] == NEW_COUNT and result["cleanup_confirmed"] is True,
                    "Incomplete backfill wave cannot be reported as complete")
            safe = True
            state["waves"].append(result)
            state.update(status="completed", new_valid_episodes=NEW_COUNT, logical_valid_total=80,
                original_matrix_size=80, original_matrix_complete=True, total_admitted_attempts=93,
                active_group=None, completed_at=utc().isoformat(), cleanup_confirmed=True)
        except BaseException as exc:
            state.update(status="stopped", error={"type": type(exc).__name__, "message": str(exc)}, completed_at=utc().isoformat())
            if group is not None and not safe:
                try:
                    state["failure_cleanup"] = aborter(plan, group, cli, descriptor=tracked or None)
                except BaseException as error:
                    state["failure_cleanup"] = {"confirmed": False, "error": str(error)}
        write(root / "backfill-state.json", state)
        return state


def install():
    # Local module wiring only. No frozen or historical file is modified.
    base.SELF, base.REVISION, base.operator_sources = SELF, REVISION, operator_sources
    base.contract, base.prefix_audit, base.public_summary = contract, prefix_audit, public_summary
    base.inspect, base.inspect_wave, base.prepare_wave = inspect, inspect_wave, prepare_wave
    base.terminal_wave, base.supervise = terminal_wave, supervise


install()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "inspect", "launch", "_supervise", "_run"))
    for key in ("batch", "run-dir", "authorization", "plan", "group-dir", "snapshot-root"):
        parser.add_argument("--" + key, type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.batch, authorization_path=args.authorization)
    elif args.command == "inspect":
        result = inspect(args.batch, args.run_dir)
    elif args.command == "launch":
        result = BASE_LAUNCH(args.batch, args.run_dir)
    elif args.command == "_run":
        require(args.snapshot_root == args.batch / "runtime_snapshot", "Snapshot root changed")
        result = base.run_wave(args.batch, args.group_dir)
    else:
        plan = read(args.plan)
        try:
            cont.await_registration(plan)
            result = supervise(plan)
        except BaseException as exc:
            result = {"status": "stopped", "phase": "startup", "error": {"type": type(exc).__name__, "message": str(exc)}}
            write(Path(plan["run_dir"]) / "backfill-state.json", result)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return int(args.command in ("_run", "_supervise") and result.get("status") not in ("completed", "completed_awaiting_review"))


if __name__ == "__main__":
    raise SystemExit(main())
