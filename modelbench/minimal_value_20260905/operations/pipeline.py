"""Finite first-package orchestration: observe existing A1, then at most one A2."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[3]
OPERATIONS = Path(__file__).resolve().parent
MAX_TOTAL_SECONDS = 96 * 3600
OPERATION_LIMITS = {"stage": 3600, "publish": 600, "qualify": 16 * 3600 + 600,
                    "gate": 1200, "prepare": 1200, "launch": 120}
MODULE = "modelbench.minimal_value_20260905.operations.pipeline"


class PipelineError(RuntimeError):
    pass


class PipelineStopped(PipelineError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    from ..runtime_integrity import atomic_json
    atomic_json(path, value)


def original_runtime(a1_batch):
    """Never import a snapshot package to publish inputs in the mutable checkout."""
    from .. import cli, freeze
    from ..transport_identity import transport_identity
    manifest = cli.load_manifest(a1_batch)
    lock = Path(manifest["resource_lock_path"]).resolve()
    expected_repo = lock.parents[3]
    if REPO.resolve() != expected_repo or "runtime_snapshot" in REPO.parts:
        raise PipelineError("Pipeline must execute from the original admitted checkout")
    current_sources = freeze.runtime_sources()
    current_transport = transport_identity()
    if manifest["runtime_sources"] != current_sources:
        raise PipelineError("Original runtime differs from the A1 frozen runtime")
    if manifest["transport_environment"] != current_transport:
        raise PipelineError("External transport differs from A1")
    return manifest, current_sources, current_transport


def controller_alive(batch):
    """Read identity of an existing controller; never infer ownership from PID alone."""
    import psutil
    launch = read(Path(batch) / "launch.json")
    pid = launch.get("pid")
    if type(pid) is not int or pid <= 0:
        raise PipelineError("Invalid controller launch identity")
    try:
        process = psutil.Process(pid)
        started = datetime.fromisoformat(launch["started_at"].replace("Z", "+00:00")).timestamp()
        if abs(process.create_time() - started) > 30:
            return False
        command = process.cmdline()
        if "_run" not in command or str(Path(batch).resolve()) not in command:
            return False
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, KeyError, ValueError) as exc:
        raise PipelineError("Cannot verify controller process identity") from exc


def stop_requested(run_dir):
    roots = {Path(run_dir).resolve(), (OPERATIONS / "pipeline-run").resolve()}
    return any((root / marker).exists() for root in roots for marker in ("STOP", "CANCEL"))


def check_stop(run_dir):
    if stop_requested(run_dir):
        raise PipelineStopped("pipeline_stop_requested")


def terminal_stage(batch, expected, *, alive=controller_alive):
    """False means still running; an absent/inconsistent terminal result is an error."""
    batch = Path(batch)
    if any((batch / marker).exists() for marker in ("STOP", "CANCEL")):
        raise PipelineStopped("stage_stop_or_cancel_marker")
    state_path = batch / "state.json"
    state = read(state_path) if state_path.is_file() else {}
    if state.get("stop_reason"):
        raise PipelineError("Stage stopped: " + str(state["stop_reason"]))
    running = alive(batch)
    terminal = bool(state.get("completed_at")) and not state.get("active_run_id")
    if terminal:
        if state.get("completed_episodes") != expected:
            raise PipelineError("Terminal stage has the wrong episode count")
        if running:
            return None
        manifest = read(batch / "manifest.json")
        lock_path = Path(manifest["resource_lock_path"])
        if lock_path.exists():
            raise PipelineError("Controller exited while its shared resource lease remains")
        return state
    if not running:
        raise PipelineError("Controller exited without a normal terminal state")
    return None


def request_stage_stop(batch):
    batch = Path(batch)
    if (batch / "launch.json").is_file():
        # Graceful stage STOP: finish the currently admitted episode, admit no next one.
        with (batch / "STOP").open("a", encoding="utf-8") as stream:
            stream.write("Requested by finite first-package pipeline\n")


def stop_started_stages(batches, *, alive=controller_alive):
    pending = []
    for batch in map(Path, batches):
        if not (batch / "launch.json").is_file():
            continue
        try:
            state = read(batch / "state.json") if (batch / "state.json").is_file() else {}
            manifest = read(batch / "manifest.json")
            terminal = (bool(state.get("completed_at")) and not state.get("active_run_id")
                        and not alive(batch) and not Path(manifest["resource_lock_path"]).exists())
            error = None
        except Exception as exc:
            terminal, error = False, type(exc).__name__ + ": " + str(exc)
        if not terminal:
            request_stage_stop(batch)
            pending.append({"batch": str(batch.resolve()), "stop_requested": True,
                            "terminal_cleanup_confirmed": False, "identity_error": error})
    return pending


def wait_stage(batch, expected, run_dir, deadline, state, *, clock=time.monotonic,
               sleep=time.sleep, alive=controller_alive):
    while True:
        check_stop(run_dir)
        if clock() >= deadline:
            request_stage_stop(batch)
            raise PipelineStopped("pipeline_total_deadline")
        done = terminal_stage(batch, expected, alive=alive)
        state.update(last_checked_at=utc(), active_stage=str(Path(batch).resolve()))
        write(Path(run_dir) / "pipeline_state.json", state)
        if done is not None:
            return done
        sleep(min(10, max(.01, deadline - clock())))


def child_environment():
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(REPO), str(REPO / "dpswarm-plugin")))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Inherited by frozen controllers and their episode children on Windows.
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def operation_result_valid(name, result):
    if name == "stage":
        return result.get("staged") is True and result.get("task_count") == 16
    if name == "publish":
        return result.get("published") is True
    if name == "qualify":
        return (result.get("all_qualified") is True and result.get("qualified_count") == 16
                and result.get("total") == 16 and not result.get("stopped") and not result.get("failure_kind"))
    if name == "gate":
        return result.get("status") == "PASS" and result.get("live_run_admission") is True
    if name == "prepare":
        return result.get("stage") == "a2" and result.get("scheduled_episodes") == 80
    if name == "launch":
        return type(result.get("pid")) is int and result["pid"] > 0
    return False


def run_operation(name, a1_batch, a2_batch, run_dir, deadline, state, *,
                  clock=time.monotonic, sleep=time.sleep, popen=subprocess.Popen):
    """Each operation runs in an original-checkout child with a finite watchdog."""
    from ..cli import kill_owned_process_tree, cleanup_owned_episode
    run_dir, a1_batch, a2_batch = map(lambda p: Path(p).resolve(), (run_dir, a1_batch, a2_batch))
    check_stop(run_dir)
    remaining = deadline - clock()
    if remaining <= 180:
        raise PipelineStopped("pipeline_total_deadline_cleanup_reserve")
    directory = run_dir / name
    directory.mkdir(exist_ok=False)
    operation_deadline = clock() + min(OPERATION_LIMITS[name], remaining - 180)
    argv = [sys.executable, "-B", "-m", MODULE, "_operation", "--name", name,
            "--a1-batch", str(a1_batch), "--a2-batch", str(a2_batch), "--run-dir", str(run_dir),
            "--remaining-seconds", str(max(1, min(remaining, OPERATION_LIMITS[name]) - 300))]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with (directory / "stdout.log").open("xb") as out, (directory / "stderr.log").open("xb") as err:
        process = popen(argv, cwd=REPO, env=child_environment(), stdout=out, stderr=err, **options)
        state.update(active_operation={"name": name, "pid": process.pid, "started_at": utc(),
                                       "maximum_seconds": min(OPERATION_LIMITS[name], remaining)})
        write(run_dir / "pipeline_state.json", state)
        stopped = False
        while process.poll() is None:
            stopped |= stop_requested(run_dir)
            if stopped:
                (run_dir / "STOP").touch(exist_ok=True)
            if stopped and name == "launch":
                request_stage_stop(a2_batch)
            if clock() >= operation_deadline:
                killed = kill_owned_process_tree(process)
                cleanup = {"confirmed": True, "not_required": name not in ("qualify", "launch")}
                if name == "qualify":
                    from .prepare_a2 import qualification_owned_root
                    cleanup = cleanup_owned_episode(qualification_owned_root("a2_attempt_01"))
                elif name == "launch":
                    request_stage_stop(a2_batch)
                    cleanup = (cleanup_owned_episode(a2_batch / "results") if killed.get("confirmed") is True else
                               {"confirmed": False, "reason": "Launch process tree did not stop"})
                state["operation_failure"] = {"name": name, "kind": "watchdog",
                                               "process_cleanup": killed, "container_cleanup": cleanup}
                write(run_dir / "pipeline_state.json", state)
                raise PipelineError("Operation watchdog expired: " + name)
            sleep(min(.5, max(.01, operation_deadline - clock())))
    state["operation_exit"] = {"name": name, "returncode": process.returncode, "completed_at": utc()}
    state.pop("active_operation", None)
    write(run_dir / "pipeline_state.json", state)
    if stopped:
        raise PipelineStopped("pipeline_stop_requested")
    output = directory / "result.json"
    if process.returncode != 0 or not output.is_file():
        if name == "qualify":
            from .prepare_a2 import qualification_owned_root
            state["operation_failure_cleanup"] = cleanup_owned_episode(qualification_owned_root("a2_attempt_01"))
            write(run_dir / "pipeline_state.json", state)
        if name == "launch":
            request_stage_stop(a2_batch)
        raise PipelineError("Operation exited without successful result: " + name)
    result = read(output)
    if not operation_result_valid(name, result):
        raise PipelineError("Operation failed its result contract: " + name)
    return result


def run(a1_batch, a2_batch, run_dir, *, total_seconds=MAX_TOTAL_SECONDS, package_started_at=None,
        clock=time.monotonic, sleep=time.sleep, alive=controller_alive,
        operation=run_operation, runtime=original_runtime):
    """One invocation, no resume, no model rerun; only the 10 + 80 first package."""
    from .. import cli, reporting
    if type(total_seconds) not in (int, float) or not 0 < total_seconds <= MAX_TOTAL_SECONDS:
        raise ValueError("Pipeline total limit must be positive and at most 96 hours")
    a1_batch, a2_batch, run_dir = map(lambda p: Path(p).resolve(), (a1_batch, a2_batch, run_dir))
    run_dir.mkdir(parents=True, exist_ok=True)
    if (run_dir / "pipeline_state.json").exists():
        raise PipelineError("Existing pipeline state is never implicitly resumed")
    with (run_dir / "controller.lock").open("x", encoding="utf-8") as stream:
        json.dump({"pid": os.getpid(), "started_at": utc()}, stream)
    elapsed_before_start = 0.0
    if package_started_at is not None:
        original = datetime.fromisoformat(package_started_at.replace("Z", "+00:00"))
        if original.tzinfo is None:
            raise PipelineError("Package start must include a timezone")
        elapsed_before_start = max(0.0, (datetime.now(timezone.utc) - original).total_seconds())
    deadline = clock() + max(0.0, total_seconds - elapsed_before_start)
    state = {"status": "running", "phase": "waiting_a1", "started_at": utc(),
             "package_started_at": package_started_at, "elapsed_before_start_seconds": elapsed_before_start,
             "a1_batch": str(a1_batch), "a2_batch": str(a2_batch), "maximum_seconds": total_seconds,
             "first_package_only": True, "episode_limits": {"a1": 10, "a2": 80, "total": 90},
             "operations": [], "success": False}
    write(run_dir / "pipeline_state.json", state)
    try:
        check_stop(run_dir)
        if a2_batch.exists():
            raise PipelineError("A2 directory already exists; no automatic model rerun or resume")
        if not (a1_batch / "launch.json").is_file():
            raise PipelineError("A1 has not been launched; pipeline never starts A1")
        manifest, sources, transport = runtime(a1_batch)
        if manifest["stage"] != "a1" or manifest["scheduled_episodes"] != 10:
            raise PipelineError("Input batch is not the finite A1 canary")
        wait_stage(a1_batch, 10, run_dir, deadline, state, clock=clock, sleep=sleep, alive=alive)
        state["a1_validation"] = cli.validate_a1(a1_batch, expected_sources=sources, expected_transport=transport)
        for name in ("stage", "publish", "qualify", "gate", "prepare", "launch"):
            check_stop(run_dir)
            if clock() >= deadline:
                raise PipelineStopped("pipeline_total_deadline")
            state["phase"] = name + "_a2"
            write(run_dir / "pipeline_state.json", state)
            result = operation(name, a1_batch, a2_batch, run_dir, deadline, state, clock=clock, sleep=sleep)
            if not operation_result_valid(name, result):
                raise PipelineError("Operation failed its result contract: " + name)
            state["operations"].append({"name": name, "completed_at": utc(),
                                       "result_path": str(run_dir / name / "result.json")})
            write(run_dir / "pipeline_state.json", state)
        state["phase"] = "waiting_a2"
        wait_stage(a2_batch, 80, run_dir, deadline, state, clock=clock, sleep=sleep, alive=alive)
        runtime(a1_batch)  # Detect original source/executor drift before the final interpretation.
        summary = reporting.write_report(a2_batch)
        if not summary.get("stage_complete") or summary.get("completed_episodes") != 80 or "selection" not in summary:
            raise PipelineError("A2 ended without a complete report and development selection")
        state.update(status="complete", phase="first_package_complete", success=True,
                     summary_path=str(a2_batch / "summary.json"), selection=summary["selection"])
    except PipelineStopped as exc:
        state["pending_stage_cleanup"] = stop_started_stages((a1_batch, a2_batch), alive=alive)
        state.update(status="stopped", reason=str(exc), success=False)
    except Exception as exc:
        state["pending_stage_cleanup"] = stop_started_stages((a1_batch, a2_batch), alive=alive)
        state.update(status="failed", reason=type(exc).__name__ + ": " + str(exc), success=False)
    state.pop("active_operation", None)
    pending = state.get("pending_stage_cleanup") or []
    if pending:
        state["active_stage"] = pending[0]["batch"]
    else:
        state.pop("active_stage", None)
    state["started_stages_terminal_confirmed"] = not pending
    state["package_terminal_confirmed"] = state["status"] == "complete" and not pending
    state["completion_scope"] = "pipeline_controller_only"
    state["completed_at"] = utc()
    write(run_dir / "pipeline_state.json", state)
    return state


def execute_operation(name, a1_batch, a2_batch, run_dir, remaining_seconds):
    from .. import cli
    from .prepare_a2 import stage_a2_inputs, publish_a2_inputs, qualify_a2
    run_dir, a1_batch, a2_batch = map(lambda p: Path(p).resolve(), (run_dir, a1_batch, a2_batch))
    check_stop(run_dir)
    _, sources, transport = original_runtime(a1_batch)
    cli.validate_a1(a1_batch, expected_sources=sources, expected_transport=transport)
    staging = run_dir / "staged-inputs"
    if name == "stage":
        result = stage_a2_inputs(staging_dir=staging)
    elif name == "publish":
        result = publish_a2_inputs(a1_batch, staging_dir=staging)
    elif name == "qualify":
        result = qualify_a2(a1_batch, attempt="a2_attempt_01", stop_file=run_dir / "STOP",
                            wall_seconds=min(57600, remaining_seconds), staging_dir=staging)
    elif name == "gate":
        result = cli.gate("a2", destination=run_dir / "a2-gate")
    elif name == "prepare":
        result = cli.prepare("a2", a2_batch, run_dir / "a2-gate/gate.json", a1_batch=a1_batch)
    elif name == "launch":
        check_stop(run_dir)
        result = cli.launch(a2_batch)
    else:
        raise PipelineError("Unknown operation")
    write(run_dir / name / "result.json", result)
    if not operation_result_valid(name, result):
        raise PipelineError("Operation returned an unsuccessful contract: " + name)
    return result


def launch(a1_batch, a2_batch, run_dir):
    """Explicit caller action only; constructing/importing this module starts nothing."""
    a1_batch, a2_batch, run_dir = map(lambda p: Path(p).resolve(), (a1_batch, a2_batch, run_dir))
    if run_dir.exists():
        raise PipelineError("Pipeline launch directory already exists; no implicit restart")
    if not (a1_batch / "launch.json").is_file():
        raise PipelineError("A1 has not been launched; pipeline never starts A1")
    check_stop(run_dir)
    original_runtime(a1_batch)
    package_started_at = read(a1_batch / "state.json")["started_at"]
    original = datetime.fromisoformat(package_started_at.replace("Z", "+00:00"))
    if original.tzinfo is None or (datetime.now(timezone.utc) - original).total_seconds() >= MAX_TOTAL_SECONDS:
        raise PipelineError("Original first-package deadline is unavailable or elapsed")
    run_dir.mkdir(parents=True)
    argv = [sys.executable, "-B", "-m", MODULE, "run", "--a1-batch", str(a1_batch),
            "--a2-batch", str(a2_batch), "--run-dir", str(run_dir), "--package-started-at", package_started_at]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with (run_dir / "stdout.log").open("xb") as out, (run_dir / "stderr.log").open("xb") as err:
        process = subprocess.Popen(argv, cwd=REPO, env=child_environment(), stdout=out, stderr=err, **options)
    value = {"pid": process.pid, "argv": argv, "started_at": utc(), "maximum_seconds": MAX_TOTAL_SECONDS,
             "package_started_at": package_started_at}
    write(run_dir / "launch.json", value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("launch", "run", "_operation"):
        p = commands.add_parser(command)
        p.add_argument("--a1-batch", required=True, type=Path)
        p.add_argument("--a2-batch", required=True, type=Path)
        p.add_argument("--run-dir", type=Path, default=OPERATIONS / "pipeline-run")
        if command == "run":
            p.add_argument("--package-started-at")
        if command == "_operation":
            p.add_argument("--name", choices=OPERATION_LIMITS, required=True)
            p.add_argument("--remaining-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    if args.command == "launch":
        result = launch(args.a1_batch, args.a2_batch, args.run_dir)
    elif args.command == "run":
        result = run(args.a1_batch, args.a2_batch, args.run_dir, package_started_at=args.package_started_at)
    else:
        result = execute_operation(args.name, args.a1_batch, args.a2_batch, args.run_dir, args.remaining_seconds)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result.get("status") in {"stopped", "failed"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
