"""Finite fresh A2 qualification -> freeze -> first-wave handoff.

Import and inspect do not execute qualification or models. A launch owns only its
fresh preparation attempt and first wave; wave_dispatched is not task completion.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
REPO = HERE.parents[2]
PREPARATION_MODULE = "modelbench.minimal_value_20260905.operations.a2_parallel_prepare"
PHASE_LIMITS = {"qualify": 58500, "freeze": 1800, "idle_headroom": 180,
                "prepare_wave": 180, "dispatch": 180}
PACKAGE_SECONDS = 96 * 3600
CLEANUP_RESERVE = 300
PIPELINE_REVISION = "a2_wave_pipeline_v2_sympy_public_checks_v3"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


controller = module("_a2_wave_controller_helpers", HERE / "parallel_controller.py")
read, write, sha = controller.read, controller.write, controller.sha


class PipelineError(RuntimeError):
    pass


def utc():
    return datetime.now(timezone.utc)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise PipelineError("An original time anchor must include its timezone")
    return result


def sources(sympy=False):
    names = list(controller.source_identity()) + [
        "a2_parallel_prepare.py", "prepare_a2.py", "a1_engineering_gate.py", "idle_memory.py", Path(__file__).name]
    if sympy:
        # Bind the independent v3 adapter and retain the prior counting baseline
        # as audit provenance; v3 does not import the historical module at runtime.
        names.extend(("sympy_public_checks.py", "sympy_public_checks_v3.py"))
    return {str((HERE / name).resolve()): sha(HERE / name) for name in names}


def owned_root(attempt):
    if not isinstance(attempt, str) or not re.fullmatch(r"a2_attempt_[0-9]{2,}", attempt):
        raise PipelineError("Qualification attempt must be a fresh a2_attempt_NN identity")
    return EXPERIMENT / "official/grader/preflight/a2_runs" / attempt


def stop_markers(plan):
    roots = [Path(plan[name]) for name in ("run_dir", "qualification_dir", "a2_batch", "group_dir")]
    return [str(root / marker) for root in roots for marker in ("STOP", "CANCEL", "TRIP.json")
            if (root / marker).exists()]


def verify_plan(plan):
    if plan.get("pipeline_revision") != PIPELINE_REVISION:
        raise PipelineError("Pipeline revision differs; old plans cannot be resumed by new source")
    for path, expected in (plan["sources"] | plan["input_sources"]).items():
        if sha(path) != expected:
            raise PipelineError("Bound source/input changed: " + Path(path).name)
    for relative, expected in plan["runtime_sources"].items():
        path = (REPO / relative).resolve()
        if not path.is_relative_to(REPO) or sha(path) != expected:
            raise PipelineError("Original runtime differs from the A1 source identity: " + relative)
    if sha(Path(plan["a1_batch"]) / "manifest.json") != plan["a1_manifest_sha256"]:
        raise PipelineError("Predecessor manifest changed")
    if sha(Path(plan["a1_batch"]) / "state.json") != plan["a1_state_sha256"]:
        raise PipelineError("Predecessor state changed")
    if stop_markers(plan):
        raise PipelineError("Pipeline STOP/CANCEL/TRIP blocks continuation")
    if utc() >= timestamp(plan["package_deadline"]):
        raise PipelineError("Original 96-hour package deadline reached")


def inspect(*, run_dir, qualification_dir, attempt, a1_batch, a2_batch, group_dir,
            definitions=None, provenance=None, sympy_native_public_checks=False, now=None):
    paths = {name: Path(value).resolve() for name, value in {
        "run_dir": run_dir, "qualification_dir": qualification_dir, "a1_batch": a1_batch,
        "a2_batch": a2_batch, "group_dir": group_dir}.items()}
    if len(set(paths.values())) != len(paths):
        raise PipelineError("Pipeline, qualification, group and batch directories must be distinct")
    for name, path in paths.items():
        if not path.is_relative_to(EXPERIMENT):
            raise PipelineError("Pipeline paths must remain inside this experiment: " + name)
    for name in ("run_dir", "qualification_dir", "group_dir"):
        if not paths[name].is_relative_to(HERE):
            raise PipelineError("Operation outputs must remain inside operations: " + name)
    for name in ("run_dir", "qualification_dir", "a2_batch", "group_dir"):
        if paths[name].exists():
            raise PipelineError("Fresh pipeline never resumes or overwrites an existing directory: " + name)
    owned = owned_root(attempt)
    if owned.exists():
        raise PipelineError("Qualification attempt already exists; no implicit rerun")
    a1 = paths["a1_batch"]
    state, manifest = read(a1 / "state.json"), read(a1 / "manifest.json")
    if (state.get("completed_episodes") != 10 or state.get("active_run_id") or state.get("active_episodes")
            or not state.get("completed_at") or state.get("stop_reason")):
        raise PipelineError("A1 must have its complete clean terminal projection")
    if any((a1 / marker).exists() for marker in ("STOP", "CANCEL")):
        raise PipelineError("Predecessor STOP/CANCEL blocks this fresh pipeline")
    start = timestamp(state["started_at"])
    deadline = start + timedelta(seconds=PACKAGE_SECONDS)
    current = now or utc()
    if (deadline - current).total_seconds() <= CLEANUP_RESERVE:
        raise PipelineError("No time remains under the original 96-hour package anchor")
    definitions = Path(definitions or HERE / "public_check_definitions.json").resolve()
    provenance = Path(provenance or HERE / "public_check_provenance.json").resolve()
    supplemental_inputs = [HERE / name for name in (
        "API_CONCURRENCY_RESEARCH_20260905.json", "A2_PARALLEL_12_PLAN_ZH.md") if (HERE / name).is_file()]
    if sympy_native_public_checks:
        supplemental_inputs.append(HERE / "A2_PREPARATION_V3_ZH.md")
    plan = {"version": 1, "pipeline_revision": PIPELINE_REVISION,
            **{k: str(v) for k, v in paths.items()}, "attempt": attempt,
            "qualification_owned_root": str(owned.resolve()),
            "definitions": str(definitions), "provenance": str(provenance),
            "sympy_native_public_checks": bool(sympy_native_public_checks),
            "sympy_adapter": "sympy_public_checks_v3.py" if sympy_native_public_checks else None,
            "sympy_historical_counting_baseline": "sympy_public_checks.py" if sympy_native_public_checks else None,
            "sources": sources(sympy_native_public_checks),
            "input_sources": {str(p): sha(p) for p in (definitions, provenance, *supplemental_inputs)},
            "runtime_sources": manifest["runtime_sources"],
            "a1_manifest_sha256": sha(a1 / "manifest.json"), "a1_state_sha256": sha(a1 / "state.json"),
            "package_started_at": start.isoformat(), "package_deadline": deadline.isoformat(),
            "phase_limits_seconds": PHASE_LIMITS, "created_at": current.isoformat(),
            "first_wave_count": 12, "no_automatic_remaining_waves": True,
            "provider_limits": {"codex_account": 4, "glm_coding": 1, "deepseek": 4},
            "provider_limits_source": "operator_configured", "provider_limits_version": 1,
            "global_model_slots": 8, "candidate_container_cap": 12, "candidate_memory": "1g"}
    verify_plan(plan)
    return plan


def environment():
    result = os.environ.copy()
    result["PYTHONPATH"] = os.pathsep.join((str(REPO), str(REPO / "dpswarm-plugin")))
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    result["PYTHONIOENCODING"] = "utf-8"
    return result


def process_identity(process):
    import psutil
    try:
        return {"pid": process.pid, "created_at": psutil.Process(process.pid).create_time()}
    except psutil.NoSuchProcess:
        if process.poll() is None:
            raise
        return {"pid": process.pid, "already_exited": True}


def original_cli():
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "dpswarm-plugin"))
    from modelbench.minimal_value_20260905 import cli
    return cli


def stop_process(process, tracked):
    return controller.cleanup_process(process, tracked, original_cli())


def cleanup(plan, phase):
    cli = original_cli()
    roots = ([Path(plan["qualification_owned_root"])] if phase == "qualify" else
             [Path(plan["a2_batch"]) / "results" / run_id
              for run_id in read(Path(plan["group_dir"]) / "group.json")["run_ids"]]
             if phase == "dispatch" and (Path(plan["group_dir"]) / "group.json").is_file() else [])
    evidence = {str(root): cli.cleanup_owned_episode(root) for root in roots}
    return {"confirmed": all(item.get("confirmed") is True for item in evidence.values()), "owned_roots": evidence}


def signal_stop(plan, phase):
    paths = [Path(plan["qualification_dir"]) / "STOP"]
    if phase == "dispatch":
        paths += [Path(plan["a2_batch"]) / "CANCEL", Path(plan["group_dir"]) / "CANCEL"]
    errors = []
    for path in paths:
        try:
            write(path, {"source": "a2_wave_pipeline", "at": utc().isoformat()})
        except BaseException as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))
    if errors:
        raise PipelineError("Some owned stop signals could not be persisted: " + "; ".join(errors))


def stop_dispatched_controller(plan):
    """A dispatch child can exit after starting its own controller; verify that exact PID."""
    import psutil
    launch_path = Path(plan["a2_batch"]) / "launch.json"
    if not launch_path.exists():
        return {"confirmed": True, "not_launched": True}
    value = read(launch_path)
    if Path(value.get("parallel_group_dir", "")).resolve() != Path(plan["group_dir"]):
        raise PipelineError("Refuse to stop a controller outside the owned group")
    if value.get("operations_sources") != controller.source_identity():
        raise PipelineError("Refuse to stop a controller with unbound source identity")
    try:
        process = psutil.Process(value["pid"])
        if abs(process.create_time() - timestamp(value["started_at"]).timestamp()) > 30:
            raise PipelineError("Controller PID was reused; its identity cannot be claimed")
        command = process.cmdline()
        if "_run" not in command or plan["a2_batch"] not in command or plan["group_dir"] not in command:
            raise PipelineError("Controller command line does not match the owned wave")
        targets = process.children(recursive=True) + [process]
        for item in targets:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(targets, timeout=30)
        return {"confirmed": not alive, "owned_pids": [x.pid for x in targets]}
    except psutil.NoSuchProcess:
        return {"confirmed": True, "already_exited": True}


def phase_result(plan, name):
    qualification = Path(plan["qualification_dir"])
    if name == "qualify":
        value = read(qualification / "preparation-state.json")
        cleaned = read(qualification / "preparation-cleanup.json")
        if (value.get("status") != "complete" or value.get("all_qualified") is not True
                or value.get("qualified_count") != 16 or value.get("task_count") != 16
                or len(value.get("completed", [])) != 16 or value.get("active_tasks")
                or any(x.get("qualified") is not True or x.get("cleanup_confirmed") is not True for x in value["completed"])
                or cleaned.get("confirmed") is not True):
            raise PipelineError("Qualification did not finish all 16 tasks with confirmed cleanup")
        return {"qualified": True, "qualified_count": 16, "cleanup_confirmed": True,
                "result_sha256": sha(qualification / "preparation-state.json")}
    if name == "freeze":
        value = read(qualification / "freeze/result.json")
        manifest = read(Path(plan["a2_batch"]) / "manifest.json")
        if (value.get("prepared") is not True or value.get("scheduled_episodes") != 80
                or manifest.get("stage") != "a2" or manifest.get("scheduled_episodes") != 80
                or value.get("manifest_sha256") != sha(Path(plan["a2_batch"]) / "manifest.json")):
            raise PipelineError("Freeze output does not bind a complete 80-episode manifest")
        return value
    path = Path(plan["run_dir"]) / name / "result.json"
    value = read(path)
    if name == "idle_headroom":
        if value.get("ready") is not True:
            raise PipelineError("Idle memory headroom was not confirmed")
    elif name == "prepare_wave":
        if value.get("prepared") is not True or len(value.get("run_ids", [])) != 12:
            raise PipelineError("First-wave preparation is incomplete")
    elif name == "dispatch":
        saved = read(Path(plan["a2_batch"]) / "launch.json")
        if value.get("pid") != saved.get("pid") or type(value.get("pid")) is not int or value["pid"] <= 0:
            raise PipelineError("Dispatch result does not match the saved controller")
        if Path(saved.get("parallel_group_dir", "")).resolve() != Path(plan["group_dir"]):
            raise PipelineError("Dispatched controller belongs to a different group")
    return value


def execute_phase(plan, name):
    verify_plan(plan)
    if name not in ("idle_headroom", "prepare_wave", "dispatch"):
        raise PipelineError("Preparation operations must use their own original module entrypoint")
    if name == "idle_headroom":
        helper = module("_a2_idle_memory", HERE / "idle_memory.py")
        manifest = read(Path(plan["a2_batch"]) / "manifest.json")
        value = helper.ensure_idle_headroom(
            Path(plan["run_dir"]) / name / "receipt.json", manifest["resource_lock_path"], minimum_gib=4)
        if value.get("ready") is not True:
            raise PipelineError("Idle memory helper did not confirm sufficient headroom")
    elif name == "prepare_wave":
        a2, group = Path(plan["a2_batch"]), Path(plan["group_dir"])
        if group.exists() or any((a2 / n).exists() for n in ("state.json", "launch.json", "controller.lock", "results")):
            raise PipelineError("Wave preparation requires a fresh frozen batch")
        manifest = read(a2 / "manifest.json")
        if manifest.get("stage") != "a2" or manifest.get("scheduled_episodes") != 80:
            raise PipelineError("Unexpected A2 manifest")
        entries = manifest["schedule"][:12]
        policy = {
            "version": 3, "execution_mode": "parallel_pilot", "max_parallel_episodes": 12,
            "global_model_slots": 8, "per_episode_model_slots": 4, "candidate_container_cap": 12,
            "candidate_memory": "1g", "candidate_cpus": 2, "grader_memory": "1g",
            "grader_max_containers": 2, "grader_memory_per_container": "1g", "auxiliary_memory": "768m",
            "grading_mode": "all_candidates_quiescent_then_serial",
            "resource_profile": "resource-defaults.json", "memory_admission_mode": "bounded_container_limits",
            "monitor_is_hard_limit": False, "episode_admission_mode": controller.ATOMIC_ADMISSION,
            "wave_watchdog_policy": "cancel_owned_inflight_at_finite_group_deadline",
            "provider_limits": plan["provider_limits"], "provider_limits_source": plan["provider_limits_source"],
            "provider_limits_version": plan["provider_limits_version"],
            "run_ids": [entry["run_id"] for entry in entries], "manifest_sha256": sha(a2 / "manifest.json"),
            "softguard": {"sample_seconds": 2, "owned_memory_limit_gib": 8, "host_available_min_gib": 2,
                          "docker_available_min_gib": 3, "max_consecutive_sample_failures": 2},
            "scope": "Exactly the original first 12 A2 episodes, then stop for review",
            "pipeline_revision": plan["pipeline_revision"],
            "admission_order": "Original manifest FIFO; fixed arm peak quota; no grade-driven priority",
            "package_started_at": plan["package_started_at"], "package_deadline": plan["package_deadline"],
            "pipeline_plan_path": str(Path(plan["run_dir"]) / "plan.json"),
            "pipeline_plan_sha256": sha(Path(plan["run_dir"]) / "plan.json"),
            "interpretation": "External resource sidecar; original serial manifest and frozen runtime unchanged",
        }
        limits = controller.wave_time_limits(controller.candidate_requirements(entries), policy)
        if (timestamp(plan["package_deadline"]) - utc()).total_seconds() < limits["watchdog_seconds"] + CLEANUP_RESERVE:
            raise PipelineError("Original package window cannot fit this complete first wave")
        group.mkdir(parents=True, exist_ok=False)
        write(group / "policy.json", policy)
        record = {"status": "ready", "transition_id": group.name, "fresh_batch": True,
                  "completed_prefix": [], "prior_token_admission_sum": 0, "prior_known_cost_usd": 0,
                  "pipeline_plan_sha256": sha(Path(plan["run_dir"]) / "plan.json"),
                  "policy_sha256": sha(group / "policy.json"), "at": utc().isoformat()}
        write(group / "transition.json", record)
        write(a2 / "state.json", {
            "stage": "a2", "started_at": utc().isoformat(), "completed_episodes": 0,
            "episodes": [], "known_cost_usd": 0, "token_admission_sum": 0,
            "stop_reason": None, "package_started_at": plan["package_started_at"],
            "parallel_transition": {"status": "ready", "transition_id": group.name,
                "record": str(group / "transition.json"), "group_dir": str(group),
                "policy_sha256": sha(group / "policy.json")}})
        evidence = controller.inspect_trial(a2, group)
        value = {"prepared": True, "run_ids": policy["run_ids"], "inspection": evidence}
    else:
        value = controller.launch(plan["a2_batch"], plan["group_dir"])
    write(Path(plan["run_dir"]) / name / "result.json", value)
    return value


def phase_argv(plan, name):
    if name in ("qualify", "freeze"):
        argv = [sys.executable, "-B", "-m", PREPARATION_MODULE, name,
                "--a1-batch", plan["a1_batch"], "--run-dir", plan["qualification_dir"],
                "--attempt", plan["attempt"]]
        if name == "qualify":
            argv += ["--definitions", plan["definitions"], "--provenance", plan["provenance"]]
            if plan["sympy_native_public_checks"]:
                argv.append("--sympy-native-public-checks")
        else:
            argv += ["--a2-batch", plan["a2_batch"]]
        return argv
    return [sys.executable, "-B", str(Path(__file__).resolve()), "_phase",
            "--plan", str(Path(plan["run_dir"]) / "plan.json"), "--phase", name]


def run_phase(plan, name, state, *, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep,
              identify=process_identity, observe=controller.tracked_descendants,
              stop=stop_process, clean=cleanup, stop_wave=stop_dispatched_controller):
    verify_plan(plan)
    remaining = (timestamp(plan["package_deadline"]) - utc()).total_seconds() - CLEANUP_RESERVE
    if remaining <= 0:
        raise PipelineError("Original package cleanup reserve reached")
    limit = min(PHASE_LIMITS[name], remaining)
    directory = Path(plan["run_dir"]) / name
    directory.mkdir(parents=True, exist_ok=False)
    argv = phase_argv(plan, name)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process, tracked = None, {}
    started = clock()
    try:
        with (directory / "stdout.log").open("xb") as out, (directory / "stderr.log").open("xb") as err:
            process = popen(argv, cwd=REPO, env=environment(), stdout=out, stderr=err, **options)
            state["active_operation"] = {"name": name, **identify(process), "argv": argv,
                                         "started_at": utc().isoformat(), "maximum_seconds": limit}
            write(directory / "process.json", state["active_operation"])
            write(Path(plan["run_dir"]) / "pipeline_state.json", state)
            while process.poll() is None:
                for item in observe(process):
                    tracked[(item["pid"], item["created_at"])] = item
                if stop_markers(plan):
                    raise PipelineError("Stop/cancel requested during " + name)
                if clock() - started >= limit:
                    raise PipelineError("Finite operation watchdog expired: " + name)
                sleep(.5)
            state.setdefault("operation_exits", {})[name] = {"returncode": process.poll(), "at": utc().isoformat()}
            if process.poll() != 0:
                raise PipelineError("Operation exited unsuccessfully: " + name)
            verify_plan(plan)
            result = phase_result(plan, name)
            state.pop("active_operation", None)
            return result
    except BaseException as exc:
        errors = []
        try:
            signal_stop(plan, name)
        except BaseException as signal_error:
            errors.append(str(signal_error))
        stopped = {"confirmed": process is None}
        wave_stopped = {"confirmed": True, "not_required": name != "dispatch"}
        if process is not None:
            try:
                stopped = stop(process, list(tracked.values()))
            except BaseException as failure:
                errors.append(str(failure))
                stopped = {"confirmed": False}
        if name == "dispatch":
            try:
                wave_stopped = stop_wave(plan)
            except BaseException as failure:
                errors.append(str(failure))
                wave_stopped = {"confirmed": False}
        if stopped.get("confirmed") is True and wave_stopped.get("confirmed") is True:
            try:
                cleaned = clean(plan, name)
            except BaseException as failure:
                errors.append(str(failure))
                cleaned = {"confirmed": False}
        else:
            cleaned = {"confirmed": False, "reason": "Owned writer termination is unconfirmed"}
        state["failure_cleanup"] = {"phase": name, "process": stopped, "wave_controller": wave_stopped,
                                    "containers": cleaned, "errors": errors}
        raise


def abort_undurable_handoff(plan):
    """Do not leave a paid controller running when its durable handoff was lost."""
    errors = []
    try:
        signal_stop(plan, "dispatch")
    except BaseException as exc:
        errors.append("stop_signals: " + str(exc))
    try:
        stopped = stop_dispatched_controller(plan)
    except BaseException as exc:
        stopped = {"confirmed": False, "error": str(exc)}
    if stopped.get("confirmed") is True:
        try:
            cleaned = cleanup(plan, "dispatch")
        except BaseException as exc:
            cleaned = {"confirmed": False, "error": str(exc)}
    else:
        cleaned = {"confirmed": False, "reason": "Owned wave writer termination is unconfirmed"}
    return {"phase": "durable_handoff", "wave_controller": stopped,
            "containers": cleaned, "signal_errors": errors}


def run(plan, *, phase_runner=run_phase):
    run_dir = Path(plan["run_dir"])
    state = {"status": "running", "pipeline_revision": PIPELINE_REVISION,
             "started_at": utc().isoformat(), "package_started_at": plan["package_started_at"],
             "package_deadline": plan["package_deadline"], "plan_sha256": sha(run_dir / "plan.json"),
             "sources": plan["sources"], "first_wave_count": 12, "completed_phases": [],
             "no_automatic_remaining_waves": True, "results": {}}
    write(run_dir / "pipeline_state.json", state)
    dispatch_attempted = False
    try:
        for name in PHASE_LIMITS:
            verify_plan(plan)
            state["phase"] = name
            write(run_dir / "pipeline_state.json", state)
            dispatch_attempted |= name == "dispatch"
            state["results"][name] = phase_runner(plan, name, state)
            state["completed_phases"].append(name)
            write(run_dir / "pipeline_state.json", state)
        state.update(status="wave_dispatched", phase="handed_off",
                     wave_completion_owned_by="parallel_controller", completion_claimed=False)
        state["finished_at"] = utc().isoformat()
        # Only this durable success projection transfers responsibility. Every
        # earlier exception, including projection I/O, still owns the wave.
        write(run_dir / "pipeline_state.json", state)
        return state
    except BaseException as exc:
        if dispatch_attempted:
            # Cleanup never depends on whether another status write succeeds.
            state["handoff_failure_cleanup"] = abort_undurable_handoff(plan)
        state.update(status="failed", failure={"type": type(exc).__name__, "message": str(exc)},
                     no_automatic_retry=True, completion_claimed=False, finished_at=utc().isoformat())
        try:
            write(run_dir / "pipeline_state.json", state)
        except BaseException as persistence_error:
            state["failure_persistence_error"] = str(persistence_error)
            try:
                write(run_dir / "pipeline-failure.json", state)
            except BaseException:
                pass
        return state


def launch(**kwargs):
    plan = inspect(**kwargs)
    verify_plan(plan)
    run_dir = Path(plan["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=False)
    write(run_dir / "plan.json", plan)
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "_run", "--plan", str(run_dir / "plan.json")]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process = None
    try:
        with (run_dir / "stdout.log").open("xb") as out, (run_dir / "stderr.log").open("xb") as err:
            process = subprocess.Popen(argv, cwd=REPO, env=environment(), stdout=out, stderr=err, **options)
            descriptor = {**process_identity(process), "argv": argv, "at": utc().isoformat(),
                          "plan_sha256": sha(run_dir / "plan.json"), "sources": plan["sources"]}
            write(run_dir / "launch.json", descriptor)
        return descriptor
    except BaseException as exc:
        failure = {"type": type(exc).__name__, "message": str(exc),
                   "at": utc().isoformat(), "automatic_retry_allowed": False}
        if process is not None:
            try:
                signal_stop(plan, "dispatch")
            except BaseException as signal_error:
                failure["signal_error"] = str(signal_error)
            try:
                failure["process_cleanup"] = stop_process(process, controller.tracked_descendants(process))
                failure["wave_cleanup"] = stop_dispatched_controller(plan)
                if failure["process_cleanup"].get("confirmed") is True and failure["wave_cleanup"].get("confirmed") is True:
                    failure["qualification_cleanup"] = cleanup(plan, "qualify")
                    failure["candidate_cleanup"] = cleanup(plan, "dispatch")
            except BaseException as cleanup_error:
                failure["cleanup_error"] = str(cleanup_error)
                failure["cleanup_confirmed"] = False
        write(run_dir / "launch-failure.json", failure)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("inspect", "launch", "_run", "_phase"))
    for name in ("run-dir", "qualification-dir", "a1-batch", "a2-batch", "group-dir", "definitions", "provenance", "plan"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--attempt", default="a2_attempt_02")
    parser.add_argument("--sympy-native-public-checks", action="store_true")
    parser.add_argument("--phase", choices=tuple(PHASE_LIMITS))
    args = vars(parser.parse_args(argv))
    command, plan_path, phase = args.pop("command"), args.pop("plan"), args.pop("phase")
    if command in ("_run", "_phase"):
        if plan_path is None:
            parser.error("--plan required")
        value = run(read(plan_path)) if command == "_run" else execute_phase(read(plan_path), phase)
    else:
        if any(args[name] is None for name in ("run_dir", "qualification_dir", "a1_batch", "a2_batch", "group_dir")):
            parser.error("All five explicit pipeline/qualification/batch/group directories are required")
        value = inspect(**args) if command == "inspect" else launch(**args)
    print(json.dumps(value, ensure_ascii=True), flush=True)
    return int(command == "_run" and value.get("status") != "wave_dispatched")


if __name__ == "__main__":
    raise SystemExit(main())
