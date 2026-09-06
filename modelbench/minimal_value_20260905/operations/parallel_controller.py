"""Explicit bounded concurrency group; import/inspect never dispatches work.

This standalone controller uses the original immutable stage runtime for each child.
Only operations metadata and the existing stage projection are written on launch.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "parallel_episode.py"
RESOURCES = HERE / "parallel_resources.py"
HELPER = HERE / "recovery_controller.py"
COUNT = 12
GROUP_WATCHDOG_SECONDS = 9300
CANDIDATE_REQUIREMENTS = {"S": 1, "L": 1, "D": 2, "T": 3, "R2": 2}
ATOMIC_ADMISSION = "atomic_episode_candidate_quota"


def local_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


recovery = local_module("_parallel_recovery_helpers", HELPER)
ParallelError = recovery.RecoveryError
read, sha, utc = recovery.read, recovery.sha, recovery.utc


def write(path, value):
    """Durable single-writer projection, independent of mutable package imports."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/indexers can briefly deny replacement of an existing
        # state projection. Retry only that atomic rename, never the admission,
        # write, settlement or child process that produced it.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def source_identity():
    paths = (Path(__file__), HELPER, WRAPPER, RESOURCES, HERE / "memory_profile.py",
             HERE / "resource-defaults.json", HERE / "provider_limits.py")
    return {path.name: sha(path) for path in paths}


def policy_contract(policy):
    expanded = policy.get("max_parallel_episodes") == COUNT
    required = ({"max_parallel_episodes": COUNT, "global_model_slots": 8,
                 "candidate_container_cap": 12, "candidate_cpus": 2} if expanded else
                {"max_parallel_episodes": 4, "global_model_slots": 8,
                 "candidate_container_cap": 7, "candidate_cpus": 2})
    for key, expected in required.items():
        if policy.get(key) != expected:
            raise ParallelError("Bounded group policy differs: " + key)
    if policy.get("candidate_memory") not in ("1g", "3g"):
        raise ParallelError("Unreviewed candidate memory limit")
    if policy["candidate_memory"] == "1g":
        if policy.get("memory_admission_mode") != "bounded_container_limits" or policy.get("resource_profile") != "resource-defaults.json":
            raise ParallelError("1 GiB groups require the explicit resource profile")
        if policy.get("grader_memory") != "1g":
            raise ParallelError("1 GiB group grader limit differs")
    if expanded:
        if policy["candidate_memory"] != "1g" or policy.get("episode_admission_mode") != ATOMIC_ADMISSION:
            raise ParallelError("Twelve-episode waves require atomic 1 GiB candidate quota admission")
        if policy.get("provider_limits") != {"codex_account": 4, "glm_coding": 1, "deepseek": 4}:
            raise ParallelError("Per-provider operator concurrency contract differs")
        if policy.get("provider_limits_source") != "operator_configured" or policy.get("provider_limits_version") != 1:
            raise ParallelError("Provider limits must identify their operator-configured provenance")
    return policy


def candidate_requirements(entries):
    try:
        return {entry["run_id"]: CANDIDATE_REQUIREMENTS[entry["arm"]] for entry in entries}
    except KeyError as exc:
        raise ParallelError("Unknown arm cannot obtain a candidate quota") from exc


def wave_time_limits(requirements, policy):
    """Bound FIFO generation by complete batches, without charging queue time to a solver."""
    if policy.get("episode_admission_mode") != ATOMIC_ADMISSION:
        return {"generation_batches_upper_bound": 1, "wait_timeout_seconds": 2100,
                "grader_wait_timeout_seconds": 7500,
                "watchdog_seconds": 1800 + len(requirements) * 1800 + 300}
    cap = policy["candidate_container_cap"]
    batches, used = 1, 0
    for amount in requirements.values():
        if amount > cap:
            raise ParallelError("One episode cannot fit its complete candidate quota")
        if used + amount > cap:
            batches, used = batches + 1, 0
        used += amount
    generation = batches * 1800 + 300
    grading = len(requirements) * 1800 + 300
    return {"generation_batches_upper_bound": batches,
            "wait_timeout_seconds": generation, "grader_wait_timeout_seconds": grading,
            "watchdog_seconds": generation + grading + 300}


def profile_binding(policy):
    if policy["candidate_memory"] != "1g":
        return {"profile_path": None, "profile_sha256": None}
    path = HERE / "resource-defaults.json"
    profile = read(path)
    if (profile.get("candidate_memory") != "1g" or profile.get("evaluation_memory") != "1g"
            or profile.get("memory_swap_equals_memory") is not True):
        raise ParallelError("Resource defaults do not enforce 1 GiB")
    if not (HERE / "memory_profile.py").is_file():
        raise ParallelError("Memory profile adapter is missing")
    return {"profile_path": str(path.resolve()), "profile_sha256": sha(path)}


def inspect_trial(batch, group_dir, *, policy_path=None, cli=None, now=None,
                  process_check=recovery.previous_controller_status, check_previous=True):
    batch, group_dir = Path(batch).resolve(), Path(group_dir).resolve()
    policy_path = Path(policy_path or group_dir / "policy.json").resolve()
    policy = policy_contract(read(policy_path))
    cli = cli or recovery.load_frozen_cli(batch)
    manifest, state = cli.load_manifest(batch), read(batch / "state.json")
    stage = manifest.get("stage")
    if stage not in ("a1", "a2") or manifest.get("scheduled_episodes") != {"a1": 10, "a2": 80}.get(stage):
        raise ParallelError("Only the original finite A1/A2 schedule may be used")
    cli.freeze.verify_snapshot(batch, manifest)
    cli.validate_schedule(manifest["schedule"], stage)
    if any((root / marker).exists() for root in (batch, group_dir) for marker in ("STOP", "CANCEL", "TRIP.json")):
        raise ParallelError("STOP/CANCEL/TRIP blocks new trial admission")
    if state.get("active_run_id") or state.get("active_episodes") or state.get("completed_at") or state.get("stop_reason"):
        raise ParallelError("Previous stage must finish and root must explicitly prepare transition")
    transition = state.get("parallel_transition") or {}
    if transition.get("status") != "ready" or transition.get("transition_id") != group_dir.name:
        raise ParallelError("Matching explicit parallel_transition is required")
    record = Path(transition.get("record", "")).resolve()
    if not record.is_file() or not record.is_relative_to(group_dir):
        raise ParallelError("Transition record is missing or outside this trial")
    prefix = []
    for entry in manifest["schedule"]:
        directory = batch / "results" / entry["run_id"]
        if not directory.exists():
            break
        prefix.append(recovery.audit_episode(batch, entry, cli))
    expected = [{"run_id": item["entry"]["run_id"], "path": item["path"], "sha256": item["sha256"]}
                for item in prefix]
    if state.get("completed_episodes") != len(prefix) or state.get("episodes") != expected:
        raise ParallelError("Prior episode projection differs from audited prefix")
    if state.get("token_admission_sum") != len(prefix) * 600000:
        raise ParallelError("Prior token admissions contain an unexplained attempt")
    cost = sum(item["known_cost_usd"] for item in prefix)
    if not math.isclose(state.get("known_cost_usd", -1), cost, rel_tol=0, abs_tol=1e-9):
        raise ParallelError("Prior known cost differs from audited prefix")
    remaining = manifest["schedule"][len(prefix):]
    ids = policy.get("run_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= policy["max_parallel_episodes"]:
        raise ParallelError("A group exceeds its finite original-episode wave limit")
    if policy["candidate_memory"] == "3g" and len(ids) != 4:
        raise ParallelError("Historical 3 GiB trial is fixed to four episodes")
    selected = remaining[:len(ids)]
    if ids != [entry["run_id"] for entry in selected]:
        raise ParallelError("Policy does not bind the exact next remaining episode identities")
    if policy.get("manifest_sha256") != sha(batch / "manifest.json"):
        raise ParallelError("Policy manifest identity differs")
    if any((batch / "results" / entry["run_id"]).exists() for entry in remaining):
        raise ParallelError("Remaining episode already started; no implicit reexecution")
    limits = manifest["stage_limits"]
    if state["token_admission_sum"] + len(selected) * 600000 > limits["token_admission_sum"]:
        raise ParallelError("Group admissions exceed original stage token cap")
    if state["known_cost_usd"] >= limits["cost_stop_usd"]:
        raise ParallelError("Original stage cost stop already reached")
    deadline = recovery.dispatch_deadline(state, manifest)
    current = now or utc()
    if deadline <= current:
        raise ParallelError("Original stage dispatch deadline has elapsed")
    lock = Path(manifest["resource_lock_path"])
    if lock.exists():
        raise ParallelError("Shared stage lease has not been released")
    previous = None
    if check_previous:
        if (batch / "launch.json").exists():
            previous = process_check(batch)
        elif not prefix and not (batch / "controller.lock").exists():
            previous = {"never_launched": True}
        else:
            raise ParallelError("Prior completed stage is missing its launch identity")
    profile = profile_binding(policy)
    requirements = candidate_requirements(selected)
    time_limits = wave_time_limits(requirements, policy)
    if policy.get("episode_admission_mode") == ATOMIC_ADMISSION:
        if policy.get("wave_watchdog_policy") != "cancel_owned_inflight_at_finite_group_deadline":
            raise ParallelError("Atomic waves require an explicit finite whole-group watchdog policy")
        if (deadline - current).total_seconds() < time_limits["watchdog_seconds"]:
            raise ParallelError("Insufficient original stage dispatch window for the complete bounded wave")
    return {
        "version": 1, "batch": str(batch), "group_dir": str(group_dir),
        "manifest_sha256": sha(batch / "manifest.json"), "state_sha256": sha(batch / "state.json"),
        "policy_path": str(policy_path), "policy_sha256": sha(policy_path),
        "transition_record_path": str(record), "transition_record_sha256": sha(record),
        "completed_prefix": expected, "run_ids": [entry["run_id"] for entry in selected],
        "stage": stage, "group_episode_count": len(selected),
        "candidate_peak_requirement": sum(requirements.values()),
        "candidate_requirements": requirements,
        "episode_admission_mode": policy.get("episode_admission_mode", "historical_immediate"),
        "provider_limits": policy.get("provider_limits"),
        "provider_limits_source": policy.get("provider_limits_source"),
        "provider_limits_version": policy.get("provider_limits_version"),
        "max_parallel_episodes": policy["max_parallel_episodes"],
        **time_limits,
        "wave_watchdog_policy": policy.get("wave_watchdog_policy"),
        "memory_admission_mode": policy.get("memory_admission_mode"),
        "prior_completed_episodes": len(prefix), "prior_token_admission_sum": state["token_admission_sum"],
        "prior_known_cost_usd": cost, "original_started_at": state["started_at"],
        "original_dispatch_deadline": deadline.isoformat(), "remaining_dispatch_seconds": (deadline-current).total_seconds(),
        "global_model_slots": policy["global_model_slots"], "candidate_container_cap": policy["candidate_container_cap"],
        "previous_controller": previous, **profile,
    }


def launch(batch, group_dir, *, policy_path=None):
    batch, group_dir = Path(batch).resolve(), Path(group_dir).resolve()
    cli = recovery.load_frozen_cli(batch)
    plan = inspect_trial(batch, group_dir, policy_path=policy_path, cli=cli)
    directory = group_dir / "controller-launch"
    directory.mkdir(parents=True, exist_ok=False)
    archived = {}
    for name in ("launch.json", "controller.lock"):
        source = batch / name
        if source.is_file():
            digest = sha(source)
            target = directory / ("previous-" + name)
            shutil.copyfile(source, target)
            if sha(source) != digest or sha(target) != digest:
                raise ParallelError("Previous controller record changed during archival")
            archived[name] = {"path": str(target), "sha256": digest}
    plan["operations_sources"] = source_identity()
    plan["archived_controller_records"] = archived
    write(directory / "plan.json", plan)
    group = {
        "version": 1, "batch": str(batch), "run_ids": plan["run_ids"],
        "manifest_sha256": plan["manifest_sha256"], "global_model_slots": plan["global_model_slots"],
        "candidate_container_cap": plan["candidate_container_cap"], "global_lock_dir": str(group_dir / "global-locks"),
        "wait_timeout_seconds": plan["wait_timeout_seconds"],
        "grader_wait_timeout_seconds": plan["grader_wait_timeout_seconds"],
        "max_parallel_episodes": plan["max_parallel_episodes"],
        "candidate_requirements": plan["candidate_requirements"],
        "episode_admission_mode": plan["episode_admission_mode"],
        "provider_limits": plan["provider_limits"], "provider_limits_source": plan["provider_limits_source"],
        "provider_limits_version": plan["provider_limits_version"],
        "wave_watchdog_policy": plan["wave_watchdog_policy"],
        "watchdog_seconds": plan["watchdog_seconds"],
        "policy_path": plan["policy_path"], "policy_sha256": plan["policy_sha256"],
        "operations_sources": plan["operations_sources"],
        "profile_path": plan["profile_path"], "profile_sha256": plan["profile_sha256"],
    }
    if (group_dir / "group.json").exists():
        raise ParallelError("Group already prepared; no implicit resume")
    write(group_dir / "group.json", group)
    plan["group_sha256"] = sha(group_dir / "group.json")
    write(directory / "plan.json", plan)
    lock = batch / "controller.lock"
    if lock.is_file():
        if sha(lock) != archived["controller.lock"]["sha256"]:
            raise ParallelError("Controller lock changed after archival")
        lock.unlink()
    snapshot = batch / "runtime_snapshot"
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "_run",
            "--batch", str(batch), "--group-dir", str(group_dir), "--snapshot-root", str(snapshot)]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        with (directory / "stdout.log").open("xb") as out, (directory / "stderr.log").open("xb") as err:
            process = subprocess.Popen(argv, cwd=snapshot,
                env=recovery.child_environment(cli, snapshot, credential_source=recovery.SOURCE_REPO),
                stdout=out, stderr=err, **options)
    except Exception as exc:
        write(directory / "launch-failure.json", {"type": type(exc).__name__, "message": str(exc),
                                                 "automatic_retry_allowed": False, "at": utc().isoformat()})
        raise
    descriptor = {"pid": process.pid, "argv": argv, "started_at": utc().isoformat(),
                  "snapshot_root": str(snapshot), "parallel_group_dir": str(group_dir),
                  "operations_sources": plan["operations_sources"], "previous_records": archived}
    write(batch / "launch.json", descriptor)
    write(directory / "launch.json", descriptor)
    return descriptor


def tracked_descendants(process):
    import psutil
    try:
        return [{"pid": item.pid, "created_at": item.create_time()}
                for item in psutil.Process(process.pid).children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def cleanup_process(process, tracked, cli):
    """Kill only this child and descendants previously observed under its PID."""
    import psutil
    result = cli.kill_owned_process_tree(process)
    errors = list(result.get("errors") or [])
    pending = []
    for identity in tracked:
        try:
            item = psutil.Process(identity["pid"])
            if abs(item.create_time() - identity["created_at"]) > 0.01:
                continue
            if item.is_running() and item.status() != psutil.STATUS_ZOMBIE:
                item.kill()
                pending.append(item)
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))
    if pending:
        _, alive = psutil.wait_procs(pending, timeout=15)
        if alive:
            errors.append("Previously observed owned descendants remain alive")
    return {"confirmed": result.get("confirmed") is True and not errors, "errors": errors}


def failed_marker(group_dir, run_id, *, cleaned, reason):
    path = Path(group_dir) / "candidates" / (run_id + ".json")
    if not path.exists():
        write(path, {"run_id": run_id, "group_sha256": sha(Path(group_dir) / "group.json"),
                     "phase": "candidate_failed", "cleanup_confirmed": cleaned,
                     "source": "parallel_supervisor", "reason": reason, "at": utc().isoformat()})


def default_guard(group_dir):
    resources = local_module("_parallel_guard_runtime", RESOURCES)
    group = read(Path(group_dir) / "group.json")
    roots = [Path(group["batch"]) / "results" / run_id for run_id in group["run_ids"]]
    return resources.monitor_from_group(group_dir, roots)


def confirm_candidate_absence(cli, directory):
    wrapper = local_module("_parallel_candidate_absence_runtime", WRAPPER)
    return wrapper.confirm_absent(cli, directory)


def run_group(batch, group_dir, *, cli=None, now=utc, clock=time.monotonic, sleep=time.sleep,
              popen=subprocess.Popen, guard_factory=default_guard,
              observe_tree=tracked_descendants, stop_tree=cleanup_process,
              candidate_absence=confirm_candidate_absence):
    batch, group_dir = Path(batch).resolve(), Path(group_dir).resolve()
    cli = cli or recovery.load_frozen_cli(batch)
    plan = read(group_dir / "controller-launch" / "plan.json")
    group = read(group_dir / "group.json")
    if sha(group_dir / "group.json") != plan.get("group_sha256"):
        raise ParallelError("Group contract changed after explicit launch")
    if plan["operations_sources"] != source_identity() or group["operations_sources"] != plan["operations_sources"]:
        raise ParallelError("Trial operations code changed after launch")
    if sha(batch / "state.json") != plan["state_sha256"]:
        raise ParallelError("Transition state changed between launch and admission")
    current = inspect_trial(batch, group_dir, policy_path=plan["policy_path"], cli=cli,
                            now=now(), check_previous=False)
    for key in ("run_ids", "completed_prefix", "manifest_sha256", "policy_sha256", "transition_record_sha256",
                "profile_path", "profile_sha256", "watchdog_seconds", "candidate_requirements",
                "episode_admission_mode", "provider_limits", "provider_limits_source", "provider_limits_version",
                "max_parallel_episodes", "wait_timeout_seconds", "grader_wait_timeout_seconds",
                "wave_watchdog_policy"):
        if current[key] != plan[key]:
            raise ParallelError("Trial input identity changed: " + key)
    if group["run_ids"] != plan["run_ids"] or group["manifest_sha256"] != plan["manifest_sha256"]:
        raise ParallelError("Group and bound plan disagree")
    manifest, state = cli.load_manifest(batch), read(batch / "state.json")
    entries = {entry["run_id"]: entry for entry in manifest["schedule"]}
    order = {entry["run_id"]: i for i, entry in enumerate(manifest["schedule"])}
    reporting = cli.reporting if hasattr(cli, "reporting") else __import__(
        "modelbench.minimal_value_20260905.reporting", fromlist=["reporting"])
    snapshot = batch / "runtime_snapshot"
    started_clock = clock()
    deadline = recovery.dispatch_deadline(state, manifest)
    children, settled = {}, {}
    pending = list(plan["run_ids"])
    atomic_admission = plan["episode_admission_mode"] == ATOMIC_ADMISSION
    reservations, released = {}, {}
    if atomic_admission:
        for key in ("candidate_requirements", "episode_admission_mode", "max_parallel_episodes",
                    "provider_limits", "provider_limits_source", "provider_limits_version",
                    "wait_timeout_seconds", "grader_wait_timeout_seconds", "candidate_container_cap"):
            if group.get(key) != plan[key]:
                raise ParallelError("Atomic admission group and plan differ: " + key)
    guard = None
    fatal = None
    state["active_episodes"] = {}
    state["parallel_transition"]["status"] = "running"
    state["parallel_trial"] = {
        "group_dir": str(group_dir), "run_ids": plan["run_ids"], "started_at": now().isoformat(),
        "original_dispatch_deadline": deadline.isoformat(), "status": "running", "settlements": {},
        "resource_policy_sha256": plan["policy_sha256"],
        "resource_profile_sha256": plan["profile_sha256"],
        "overcommit_trial": plan["memory_admission_mode"] == "monitored_overcommit",
        "group_episode_count": len(plan["run_ids"]),
        "candidate_peak_requirement": plan["candidate_peak_requirement"],
        "episode_admission_mode": plan["episode_admission_mode"],
        "candidate_reservations": reservations, "candidate_releases": released,
        "candidate_quota_peak": 0, "queued_run_ids": list(pending),
        "queue_is_outside_episode_deadline": True,
    }

    def persist():
        write(batch / "state.json", state)
        write(group_dir / "trial-state.json", state["parallel_trial"] | {
            "active_episodes": state["active_episodes"], "stage_completed_episodes": state["completed_episodes"],
            "stage_known_cost_usd": state["known_cost_usd"], "stage_token_admission_sum": state["token_admission_sum"]})

    def settle(run_id, *, abort_reason=None):
        if run_id in settled:
            return settled[run_id]
        child = children[run_id]
        directory = batch / "results" / run_id
        process = child["process"]
        errors = []
        try:
            stopped = (child["forced_stop"] if "forced_stop" in child else
                       stop_tree(process, list(child["descendants"].values()), cli))
        except BaseException as exc:
            stopped = {"confirmed": False, "errors": [str(exc)]}
        try:
            cleaned = (child["forced_cleanup"] if "forced_cleanup" in child else
                       cli.cleanup_owned_episode(directory))
        except BaseException as exc:
            cleaned = {"confirmed": False, "errors": [str(exc)]}
        complete = stopped.get("confirmed") is True and cleaned.get("confirmed") is True
        if not complete:
            lease["retain"] = True
            errors.append("Owned process/container cleanup is unconfirmed")
        account = None
        try:
            account = reporting.accounting(directory, read(Path(cli.HERE) / "pricing.json"))
        except Exception as exc:
            account = {"cost_computable": False, "api_equivalent_known_subtotal_usd": None,
                       "error": type(exc).__name__ + ": " + str(exc)}
        audited = None
        explanation = None
        try:
            supervision = {"reason": None if process.poll() == 0 else "episode_process_failed",
                           "returncode": process.poll()}
            audited, explanation = recovery.child_result(batch, entries[run_id], supervision, child["stderr"], cli)
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))
        if abort_reason:
            errors.append(abort_reason)
        if account.get("cost_computable") is not True:
            errors.append("Incomplete or unknown cost evidence")
        known = account.get("api_equivalent_known_subtotal_usd")
        record = {
            "run_id": run_id, "returncode": process.poll(), "at": now().isoformat(),
            "process_cleanup": stopped, "container_cleanup": cleaned, "cleanup_confirmed": complete,
            "accounting": account, "known_cost_usd": known, "cost_computable": account.get("cost_computable") is True,
            "errors": errors, "saved_result_preserved": (directory / "episode_result.json").is_file(),
            "engineering_valid": audited is not None and complete,
        }
        if audited is not None:
            record["episode_result_sha256"] = audited["sha256"]
            record["accounting_audit"] = audited["audit"]
            record["episode_result_path"] = audited["path"]
        if explanation:
            record["post_commit_exit_explanation"] = explanation
        # Register once BEFORE any marker/report I/O, then rebuild totals from
        # the immutable prefix and these unique settlement records. Retrying a
        # failing projection can never count a call or an episode twice.
        settled[run_id] = record
        state["known_cost_usd"] = plan["prior_known_cost_usd"] + sum(
            item["known_cost_usd"] or 0 for item in settled.values())
        valid_episodes = [{"run_id": key, "path": item["episode_result_path"],
                           "sha256": item["episode_result_sha256"]}
                          for key, item in settled.items() if item["engineering_valid"]]
        state["episodes"] = sorted(plan["completed_prefix"] + valid_episodes, key=lambda item: order[item["run_id"]])
        state["completed_episodes"] = len(state["episodes"])
        state["parallel_trial"]["settlements"][run_id] = record
        if complete:
            state["active_episodes"].pop(run_id, None)
            reservations.pop(run_id, None)
        else:
            state["active_episodes"][run_id]["phase"] = "cleanup_unconfirmed"
        if not (group_dir / "candidates" / (run_id + ".json")).exists():
            failed_marker(group_dir, run_id, cleaned=complete, reason="child_terminal_without_candidate_marker")
        write(group_dir / "settlements" / (run_id + ".json"), record)
        persist()
        return record

    with cli.stage_lease(manifest["resource_lock_path"], batch) as lease:
        with (batch / "controller.lock").open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "started_at": now().isoformat(), "parallel_group_dir": str(group_dir)}, stream)
        persist()
        try:
            guard = guard_factory(group_dir)
            initial_resources = guard.tick()  # Require one usable pre-admission probe.
            if initial_resources.get("sample_error"):
                raise ParallelError("Initial resource probe unavailable; no episode admitted")
            guard.start()
            def admit(run_id):
                if any((root / marker).exists() for root in (batch, group_dir)
                       for marker in ("STOP", "CANCEL", "TRIP.json")):
                    raise ParallelError("Cancellation/stop/resource trip before all group admissions")
                if now() >= deadline:
                    raise ParallelError("Original stage dispatch deadline reached before admission")
                if state["known_cost_usd"] >= manifest["stage_limits"]["cost_stop_usd"]:
                    raise ParallelError("Original stage known cost stop reached before admission")
                directory = batch / "results" / run_id
                if directory.exists():
                    raise ParallelError("Trial episode already exists; never reexecute")
                required = plan["candidate_requirements"][run_id]
                if atomic_admission:
                    if sum(reservations.values()) + required > plan["candidate_container_cap"]:
                        raise ParallelError("Atomic candidate quota would over-admit")
                    reservations[run_id] = required
                    state["parallel_trial"]["candidate_quota_peak"] = max(
                        state["parallel_trial"]["candidate_quota_peak"], sum(reservations.values()))
                state["token_admission_sum"] += 600000
                state["active_episodes"][run_id] = {"phase": "starting", "pid": None, "at": now().isoformat()}
                persist()
                logs = group_dir / "children" / run_id
                logs.mkdir(parents=True, exist_ok=False)
                stderr = logs / "stderr.log"
                admitted_clock = clock()
                queue_seconds = max(0.0, admitted_clock - started_clock)
                if atomic_admission:
                    write(group_dir / "admissions" / (run_id + ".json"), {
                        "run_id": run_id, "group_sha256": plan["group_sha256"],
                        "candidate_quota": required, "status": "admitted",
                        "admitted_at": now().isoformat(), "queue_seconds": queue_seconds,
                        "queue_is_outside_episode_deadline": True,
                    })
                argv = [sys.executable, "-B", str(WRAPPER), "--batch", str(batch), "--run-id", run_id,
                        "--group-dir", str(group_dir), "--snapshot-root", str(snapshot)]
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                with (logs / "stdout.log").open("xb") as out, stderr.open("xb") as err:
                    process = popen(argv, cwd=snapshot, env=recovery.child_environment(cli, snapshot),
                                    stdout=out, stderr=err, **options)
                    # Register ownership before closing pipe files can raise.
                    children[run_id] = {"process": process, "stderr": stderr, "descendants": {},
                                        "started_clock": admitted_clock}
                state["active_episodes"][run_id] = {
                    "phase": "running", "pid": process.pid, "at": now().isoformat(),
                    "candidate_quota": required, "queue_seconds": queue_seconds,
                }
                write(logs / "process.json", {"pid": process.pid, "argv": argv, "at": now().isoformat()})
                persist()

            def release_candidate_quota(run_id):
                if not atomic_admission or run_id in released:
                    return
                marker = group_dir / "candidates" / (run_id + ".json")
                if not marker.is_file():
                    return
                value = read(marker)
                if (value.get("run_id") != run_id or value.get("group_sha256") != plan["group_sha256"]
                        or value.get("phase") != "candidate_closed" or value.get("cleanup_confirmed") is not True):
                    raise ParallelError("Candidate release marker failed identity or cleanup: " + run_id)
                # Grading waits for every release receipt, so this live absence
                # check cannot race with a grader created after the final marker.
                absent = candidate_absence(cli, batch / "results" / run_id)
                receipt = {"run_id": run_id, "group_sha256": plan["group_sha256"],
                           "candidate_quota": reservations[run_id], "status": "released",
                           "released_at": now().isoformat(), "owned_absence_verified": absent}
                write(group_dir / "candidate-releases" / (run_id + ".json"), receipt)
                released[run_id] = receipt
                del reservations[run_id]
                if run_id in state["active_episodes"]:
                    state["active_episodes"][run_id]["phase"] = "candidate_closed_waiting_grade"
                persist()

            while pending or len(settled) < len(children):
                if any((root / marker).exists() for root in (batch, group_dir)
                       for marker in ("CANCEL", "TRIP.json")):
                    raise ParallelError("Trial cancelled or resource guard tripped")
                if pending and any((root / "STOP").exists() for root in (batch, group_dir)):
                    raise ParallelError("Stop requested while original wave episodes remain queued")
                # Admission is FIFO and independent of solutions or grade outcomes.
                # An episode acquires its entire peak allowance before its process
                # and the frozen 1800-second generation clock are created.
                while pending and len(children) - len(settled) < plan["max_parallel_episodes"]:
                    run_id = pending[0]
                    if atomic_admission and (sum(reservations.values()) + plan["candidate_requirements"][run_id]
                                             > plan["candidate_container_cap"]):
                        break
                    admit(run_id)
                    pending.pop(0)
                    state["parallel_trial"]["queued_run_ids"] = list(pending)
                    persist()
                if any((root / marker).exists() for root in (batch, group_dir)
                       for marker in ("CANCEL", "TRIP.json")):
                    raise ParallelError("Trial cancelled or resource guard tripped")
                if clock() - started_clock >= plan["watchdog_seconds"]:
                    raise ParallelError("Finite group watchdog reached")
                for run_id, child in children.items():
                    if run_id in settled:
                        continue
                    for item in observe_tree(child["process"]):
                        child["descendants"][(item["pid"], item["created_at"])] = item
                    release_candidate_quota(run_id)
                    if child["process"].poll() is not None:
                        if atomic_admission and run_id not in released:
                            raise ParallelError("Child exited before verified candidate quota release: " + run_id)
                        record = settle(run_id)
                        if record["errors"]:
                            raise ParallelError("Episode failed trial contract: " + run_id)
                sleep(0.2)
        except BaseException as exc:
            fatal = {"type": type(exc).__name__, "message": str(exc), "at": now().isoformat()}
            write(group_dir / "CANCEL", fatal)
            state["parallel_trial"]["failure"] = fatal
            pending_children = {key: child for key, child in children.items() if key not in settled}
            # Stop every process tree before any slow accounting. Container
            # cleanup then runs concurrently per owned episode, not behind the
            # first episode's Docker operations or accounting audit.
            if pending_children:
                with ThreadPoolExecutor(max_workers=max(1, len(pending_children)), thread_name_prefix="parallel-trial-stop") as pool:
                    stops = {key: pool.submit(stop_tree, child["process"],
                                              list(child["descendants"].values()), cli)
                             for key, child in pending_children.items()}
                    for key, future in stops.items():
                        try:
                            pending_children[key]["forced_stop"] = future.result()
                        except BaseException as stop_error:
                            pending_children[key]["forced_stop"] = {"confirmed": False, "errors": [str(stop_error)]}
                    cleanups = {key: pool.submit(cli.cleanup_owned_episode, batch / "results" / key)
                                for key in pending_children}
                    for key, future in cleanups.items():
                        try:
                            pending_children[key]["forced_cleanup"] = future.result()
                        except BaseException as cleanup_error:
                            pending_children[key]["forced_cleanup"] = {"confirmed": False, "errors": [str(cleanup_error)]}
            for run_id in children:
                if run_id not in settled:
                    try:
                        settle(run_id, abort_reason="group_aborted_after_failure")
                    except BaseException as settlement_error:
                        lease["retain"] = True
                        state["parallel_trial"].setdefault("settlement_errors", {})[run_id] = {
                            "type": type(settlement_error).__name__, "message": str(settlement_error)}
                        # Cleanup happens before accounting/persistence in settle.
                        # Other owned children must still be stopped on this path.
            # Admissions that failed before Popen are retained, never silently refunded.
            for run_id in list(state["active_episodes"]):
                if run_id not in children:
                    directory = batch / "results" / run_id
                    try:
                        cleaned = cli.cleanup_owned_episode(directory)
                    except BaseException as cleanup_error:
                        cleaned = {"confirmed": False, "errors": [str(cleanup_error)]}
                    if not cleaned.get("confirmed"):
                        lease["retain"] = True
                    else:
                        state["active_episodes"].pop(run_id, None)
                        reservations.pop(run_id, None)
                    record = {"run_id": run_id, "launched": False, "cleanup_confirmed": cleaned.get("confirmed"),
                              "known_cost_usd": 0, "cost_computable": True, "reason": "dispatch_failed_before_child"}
                    state["parallel_trial"]["settlements"][run_id] = record
                    failed_marker(group_dir, run_id, cleaned=cleaned.get("confirmed") is True, reason=record["reason"])
        finally:
            if guard is not None:
                try:
                    state["parallel_trial"]["resource_guard"] = guard.stop()
                    if state["parallel_trial"]["resource_guard"].get("tripped"):
                        fatal = fatal or {"type": "ResourceMonitorTripped", "message": "resource guard trip before final settlement"}
                    if state["parallel_trial"]["resource_guard"].get("stopped") is not True:
                        fatal = fatal or {"type": "ResourceMonitorStopUnconfirmed", "message": "monitor did not stop"}
                except BaseException as exc:
                    fatal = fatal or {"type": type(exc).__name__, "message": str(exc)}
                    state["parallel_trial"]["guard_shutdown_error"] = str(exc)
            safe = not lease["retain"] and not state["active_episodes"]
            state["parallel_trial"].update(
                status="completed_awaiting_review" if not fatal and len(settled) == len(plan["run_ids"]) and safe else "failed",
                terminal_episodes=len(settled),
                completed_count=sum(item["engineering_valid"] for item in settled.values()),
                stage_completed_episodes=state["completed_episodes"],
                cleanup_confirmed=safe, cleanup_status="confirmed" if safe else "unconfirmed",
                completed_at=now().isoformat(), no_new_admissions=True,
                unadmitted_run_ids=[run_id for run_id in pending
                                   if run_id not in state["parallel_trial"]["settlements"]],
                no_automatic_continuation=True)
            state["parallel_transition"]["status"] = state["parallel_trial"]["status"]
            group_completed = state["parallel_trial"]["status"] == "completed_awaiting_review"
            stage_completed = group_completed and state["completed_episodes"] == manifest["scheduled_episodes"]
            state["parallel_trial"]["stage_complete"] = stage_completed
            # Completing a stage exposes its real unchanged gate to the outer
            # observer. Intermediate groups remain explicitly stopped.
            state["stop_reason"] = (None if stage_completed else "parallel_trial_complete_review_required"
                                    if group_completed else "parallel_trial_failed")
            state["completed_at"] = now().isoformat()
            persist()
            try:
                reporting.write_report(batch)
            except Exception as exc:
                state["parallel_trial"]["report_error"] = str(exc)
                persist()
    return state["parallel_trial"]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("inspect", "launch", "_run"))
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--group-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--snapshot-root", type=Path)
    args = parser.parse_args(argv)
    if args.snapshot_root is not None and args.snapshot_root.resolve() != args.batch.resolve() / "runtime_snapshot":
        raise ParallelError("Snapshot root differs from original batch")
    if args.command == "inspect":
        result = inspect_trial(args.batch, args.group_dir, policy_path=args.policy)
    elif args.command == "launch":
        result = launch(args.batch, args.group_dir, policy_path=args.policy)
    else:
        result = run_group(args.batch, args.group_dir)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return int(args.command == "_run" and result.get("status") != "completed_awaiting_review")


if __name__ == "__main__":
    raise SystemExit(main())
