"""Finite A2 continuation; inspect is read-only, launch is explicit.

The original 80-episode manifest, solver code, budgets, and scores are immutable.
This supervisor waits for an existing wave and then dispatches contiguous waves
through the already frozen controller. It never retries a started episode.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
from types import SimpleNamespace
from uuid import uuid4

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
REPO = EXPERIMENT.parents[1]
REVISION = "a2_continuation_v1"
MAX_EPISODES = 80
CLEANUP_RESERVE = 300
DOC = HERE / "A2_ASYNC_CONTINUATION_20260905.md"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


controller = module("_continuation_controller", HERE / "parallel_controller.py")
read, write, sha, utc = controller.read, controller.write, controller.sha, controller.utc
recovery = controller.recovery


class ContinuationError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise ContinuationError(message)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "A timezone is required")
    return result


def sources():
    result = {str(HERE / name): digest for name, digest in controller.source_identity().items()}
    for path in (Path(__file__).resolve(), HERE / "idle_memory.py"):
        result[str(path)] = sha(path)
    return result


def stop_paths(plan, group=None):
    roots = [Path(plan["run_dir"]), Path(plan["batch"])]
    if group:
        roots.append(Path(group))
    return [root / marker for root in roots for marker in ("STOP", "CANCEL", "TRIP.json")]


def verify_plan(plan):
    require(plan.get("revision") == REVISION, "Continuation revision changed")
    for path, digest in {**plan["sources"], **plan["inputs"]}.items():
        require(sha(path) == digest, "Bound continuation source/input changed: " + Path(path).name)
    batch = Path(plan["batch"])
    require(sha(batch / "manifest.json") == plan["manifest_sha256"], "Original manifest changed")
    state = read(batch / "state.json")
    require(state["started_at"] == plan["stage_started_at"], "Original A2 start changed")
    require(state["package_started_at"] == plan["package_started_at"], "Original package start changed")
    require(not any(path.exists() for path in stop_paths(plan)), "STOP/CANCEL/TRIP blocks continuation")
    require(utc() < timestamp(plan["deadline"]), "Original stage/package deadline elapsed")
    return state


def group_identity(batch, group, *, expected_sources=None):
    batch, group = Path(batch).resolve(), Path(group).resolve()
    require(group.is_relative_to(HERE), "Wave is outside the operation directory")
    value = read(group / "group.json")
    require(Path(value["batch"]).resolve() == batch, "Wave batch identity changed")
    require(value["manifest_sha256"] == sha(batch / "manifest.json"), "Wave manifest changed")
    require(value["operations_sources"] == (expected_sources or controller.source_identity()),
            "Wave operations sources changed")
    require(sha(value["policy_path"]) == value["policy_sha256"], "Wave policy changed")
    policy = controller.policy_contract(read(value["policy_path"]))
    require(policy["max_parallel_episodes"] == 12 and policy["candidate_memory"] == "1g",
            "Only the agreed 12-slot / 1 GiB continuation is supported")
    require(value["run_ids"] == policy["run_ids"], "Wave task identities changed")
    return value, policy


def controller_process(batch, group, *, descriptor=None):
    import psutil
    value = descriptor or read(Path(batch) / "launch.json")
    require(Path(value.get("parallel_group_dir", "")).resolve() == Path(group).resolve(),
            "Launch does not belong to the expected wave")
    require(value.get("operations_sources") == controller.source_identity(), "Controller source identity changed")
    try:
        process = psutil.Process(value["pid"])
        expected = value.get("created_at")
        if expected is None:
            expected = timestamp(value["started_at"]).timestamp()
        require(abs(process.create_time() - expected) <= (0.01 if "created_at" in value else 30),
                "Controller PID identity is ambiguous")
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        command = process.cmdline()
        require("_run" in command and str(Path(batch).resolve()) in command
                and str(Path(group).resolve()) in command, "Controller command does not match owned wave")
        return process
    except psutil.NoSuchProcess:
        return None


def check_idle(cli, batch, group, value):
    """No writer or shared slot from the previous wave may survive the handoff."""
    require(not Path(cli.load_manifest(batch)["resource_lock_path"]).exists(), "Shared stage lease remains")
    require(not list(Path(value["global_lock_dir"]).rglob("slot-*.lock")), "Previous wave still owns resource slots")
    require(not list(Path(group).glob("provider-trip-*.json")), "Previous wave has a provider rate-limit trip")
    for run_id in value["run_ids"]:
        controller.confirm_candidate_absence(cli, Path(batch) / "results" / run_id)


def audit_provider_events(group, expected_call_ids):
    """Require every accounted transport to have one closed provider envelope."""
    from collections import Counter
    group = Path(group)
    digest = sha(group / "group.json")
    entered, returned, acquired, released = Counter(), Counter(), Counter(), Counter()
    require(not list(group.glob("provider-trip-*.json")), "Provider trip blocks continuation")
    for path in sorted((group / "provider-events").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            require(event.get("group_sha256") == digest, "Provider event has another group identity")
            kind = event.get("event")
            require(kind != "group_admission_stopped" and not event.get("rate_limit_signal")
                    and not event.get("admission_stopped"), "Provider rate limit or admission stop observed")
            if kind in ("transport_entered", "transport_returned"):
                (entered if kind == "transport_entered" else returned)[event["call_id"]] += 1
            elif kind in ("provider_acquired", "provider_released"):
                key = (event["pid"], event["request_token"])
                (acquired if kind == "provider_acquired" else released)[key] += 1
    require(entered == returned == Counter(expected_call_ids), "Provider transports differ from accounted calls")
    require(acquired == released and sum(acquired.values()) >= sum(entered.values()),
            "Provider leases did not close")
    return {"transport_count": sum(returned.values()), "provider_leases": sum(acquired.values()),
            "rate_limit_signals": 0, "passed": True}


def audit_terminal(plan, group, cli):
    state = verify_plan(plan)
    batch, group = Path(plan["batch"]), Path(group)
    require(not any(path.exists() for path in stop_paths(plan, group)), "Previous wave has a stop or rate-limit trip")
    value, policy = group_identity(batch, group)
    require(controller_process(batch, group) is None, "Previous controller has not exited")
    trial = state.get("parallel_trial") or {}
    require(Path(trial.get("group_dir", "")).resolve() == group.resolve(), "Stage points at another wave")
    require(trial.get("status") == "completed_awaiting_review" and trial.get("cleanup_confirmed") is True,
            "Previous wave did not complete with confirmed cleanup")
    require(not trial.get("report_error"), "Previous wave reporting failed")
    require(not state.get("active_run_id") and not state.get("active_episodes") and state.get("completed_at"),
            "Previous wave still has an active or incomplete projection")
    require(state.get("stop_reason") in (None, "parallel_trial_complete_review_required"),
            "A real stage stop cannot be overridden")
    require(trial.get("terminal_episodes") == len(value["run_ids"])
            and trial.get("completed_count") == len(value["run_ids"]), "Wave is not engineering-complete")
    manifest = cli.load_manifest(batch)
    cli.freeze.verify_snapshot(batch, manifest)
    cli.validate_schedule(manifest["schedule"], "a2")
    count = state["completed_episodes"]
    require(type(count) is int and plan["initial_completed"] <= count <= MAX_EPISODES, "Invalid completed prefix")
    prefix = [recovery.audit_episode(batch, entry, cli) for entry in manifest["schedule"][:count]]
    expected = [{"run_id": item["entry"]["run_id"], "path": item["path"], "sha256": item["sha256"]}
                for item in prefix]
    require(state["episodes"] == expected, "Episode SHA projection differs from the audited prefix")
    require(state["token_admission_sum"] == count * 600000, "Unexplained token admission or partial attempt")
    require(math.isclose(state["known_cost_usd"], sum(item["known_cost_usd"] for item in prefix),
                         rel_tol=0, abs_tol=1e-9), "Known cost differs from the audited prefix")
    remaining = manifest["schedule"][count:]
    require(not any((batch / "results" / entry["run_id"]).exists() for entry in remaining),
            "A remaining episode has already started; no implicit rerun")
    require(value["run_ids"] == [entry["run_id"] for entry in manifest["schedule"][count-len(value["run_ids"]):count]],
            "Previous wave is not the end of the completed prefix")
    check_idle(cli, batch, group, value)
    recent = prefix[-len(value["run_ids"]):]
    provider = audit_provider_events(group, [call["call_id"] for item in recent
                                            for call in item["accounting"]["calls"]])
    return {"provider_audit": provider,"state_sha256": sha(batch / "state.json"), "completed_episodes": count,
            "known_cost_usd": state["known_cost_usd"], "token_admission_sum": state["token_admission_sum"],
            "episodes": expected, "previous_group": str(group), "previous_group_sha256": sha(group / "group.json"),
            "cleanup_confirmed": True, "at": utc().isoformat()}


def inspect(batch, run_dir, *, cli=None):
    """May attach a running wave; this function never writes the stage or launches."""
    batch, run_dir = Path(batch).resolve(), Path(run_dir).resolve()
    require(batch.is_relative_to(EXPERIMENT / "batches") and run_dir.is_relative_to(HERE),
            "Continuation paths must stay within this experiment")
    require(not run_dir.exists(), "Continuation directory exists; no implicit resume")
    require(not (batch / "continuation.lock").exists(), "Another continuation owns this batch")
    cli = cli or recovery.load_frozen_cli(batch)
    manifest, state = cli.load_manifest(batch), read(batch / "state.json")
    require(manifest.get("stage") == "a2" and manifest.get("scheduled_episodes") == MAX_EPISODES,
            "Only the existing frozen A2 schedule is authorized")
    cli.freeze.verify_snapshot(batch, manifest)
    require(12 <= state["completed_episodes"] < MAX_EPISODES, "Expected the original partially completed A2 batch")
    launch = read(batch / "launch.json")
    group = Path(launch["parallel_group_dir"]).resolve()
    value, policy = group_identity(batch, group)
    require(DOC.is_file(), "The explicit asynchronous continuation plan is missing")
    stage_deadline = recovery.dispatch_deadline(state, manifest)
    package_deadline = timestamp(state["package_started_at"]) + timedelta(hours=96)
    plan = {"revision": REVISION, "batch": str(batch), "run_dir": str(run_dir),
            "manifest_sha256": sha(batch / "manifest.json"), "sources": sources(), "inputs": {str(DOC): sha(DOC)},
            "stage_started_at": state["started_at"], "stage_deadline": stage_deadline.isoformat(),
            "package_started_at": state["package_started_at"], "package_deadline": package_deadline.isoformat(),
            "deadline": min(stage_deadline, package_deadline).isoformat(),
            "initial_group": str(group), "initial_group_sha256": sha(group / "group.json"),
            "initial_completed": state["completed_episodes"], "maximum_total_episodes": MAX_EPISODES,
            "maximum_wave_episodes": 12, "policy_template": policy,
            "policy_template_sha256": value["policy_sha256"],
            "automatic_continuation_authorized": True, "rerun_started_episodes": False,
            "created_at": utc().isoformat()}
    verify_plan(plan)
    alive = controller_process(batch, group)
    if alive is None:
        plan["initial_audit"] = audit_terminal(plan, group, cli)
    else:
        require(not any(path.exists() for path in stop_paths(plan, group)), "Active wave has a stop/trip")
        plan["observed_active_controller"] = {"pid": alive.pid, "created_at": alive.create_time()}
    return plan


@contextmanager
def continuation_lease(batch, run_dir):
    path = Path(batch) / "continuation.lock"
    token = uuid4().hex
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"pid": os.getpid(), "run_dir": str(run_dir), "token": token, "at": utc().isoformat()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        yield
    finally:
        if path.exists() and read(path).get("token") == token:
            path.unlink()


def prepare_next_wave(plan, *, cli=None, audit=None, headroom=None):
    """Under the continuation lease, archive then clear only the completed-wave stop."""
    batch, run_dir = Path(plan["batch"]), Path(plan["run_dir"])
    cli = cli or recovery.load_frozen_cli(batch)
    state = verify_plan(plan)
    group = Path(read(batch / "launch.json")["parallel_group_dir"])
    audit = audit or audit_terminal(plan, group, cli)
    require(sha(batch / "state.json") == audit["state_sha256"], "Stage changed after its terminal audit")
    count = audit["completed_episodes"]
    require(count < MAX_EPISODES, "All original A2 episodes are already complete")
    manifest = cli.load_manifest(batch)
    selected = manifest["schedule"][count:count+12]
    next_group = run_dir / ("wave-%03d-%03d" % (count+1, count+len(selected)))
    require(not next_group.exists(), "Next wave directory already exists; no retry")
    policy = dict(plan["policy_template"])
    policy.update(run_ids=[entry["run_id"] for entry in selected],
                  scope="Continue only the original frozen 80-episode schedule; audit between sequential waves",
                  continuation_revision=REVISION, continuation_plan_path=str(run_dir / "plan.json"),
                  continuation_plan_sha256=sha(run_dir / "plan.json"),
                  previous_group=str(group), previous_group_sha256=sha(group / "group.json"))
    policy.pop("pipeline_plan_path", None)
    policy.pop("pipeline_plan_sha256", None)
    limits = controller.wave_time_limits(controller.candidate_requirements(selected), policy)
    require((timestamp(plan["deadline"])-utc()).total_seconds() >= limits["watchdog_seconds"] + CLEANUP_RESERVE,
            "Original stage/package window cannot fit another complete bounded wave")
    require(state["token_admission_sum"]+len(selected)*600000 <= manifest["stage_limits"]["token_admission_sum"],
            "Original token admission cap would be exceeded")
    require(state["known_cost_usd"] < manifest["stage_limits"]["cost_stop_usd"], "Original cost stop reached")
    next_group.mkdir(parents=True, exist_ok=False)
    before = next_group / "before"
    before.mkdir()
    archived = {}
    for name in ("state.json", "launch.json", "controller.lock", "report.json", "report.md"):
        path = batch / name
        if path.is_file():
            digest = sha(path)
            target = before / name
            shutil.copyfile(path, target)
            require(sha(path) == sha(target) == digest, "Previous evidence changed during archival")
            archived[name] = {"path": str(target), "sha256": digest}
    write(next_group / "previous-audit.json", audit)
    helper = headroom or module("_continuation_idle", HERE / "idle_memory.py").ensure_idle_headroom
    receipt = helper(next_group / "idle-memory.json", manifest["resource_lock_path"], minimum_gib=4)
    require(receipt.get("ready") is True, "Idle resource headroom was not confirmed")
    verify_plan(plan)
    require(not any(path.exists() for path in stop_paths(plan, group)), "Previous wave STOP arrived during preparation")
    require(sha(batch / "state.json") == audit["state_sha256"], "Stage changed while preparing the wave")
    write(next_group / "policy.json", policy)
    record = {"status": "ready", "transition_id": next_group.name, "fresh_batch": False,
              "continuation_plan_path": str(run_dir / "plan.json"), "continuation_plan_sha256": sha(run_dir / "plan.json"),
              "prior_state_sha256": audit["state_sha256"], "completed_prefix": state["episodes"],
              "prior_token_admission_sum": state["token_admission_sum"], "prior_known_cost_usd": state["known_cost_usd"],
              "original_started_at": state["started_at"], "package_started_at": state["package_started_at"],
              "policy_sha256": sha(next_group / "policy.json"), "archived": archived, "at": utc().isoformat()}
    write(next_group / "transition.json", record)
    # The frozen controller obtains this same shared lease when its process starts.
    with cli.stage_lease(manifest["resource_lock_path"], batch):
        require(not any(path.exists() for path in stop_paths(plan, group)), "STOP arrived before transition commit")
        require(sha(batch / "state.json") == audit["state_sha256"], "Another writer changed the stage")
        updated = dict(state)
        updated.pop("completed_at", None)
        updated["stop_reason"] = None
        updated["parallel_transition"] = {"status": "ready", "transition_id": next_group.name,
            "record": str(next_group / "transition.json"), "group_dir": str(next_group),
            "policy_sha256": sha(next_group / "policy.json")}
        write(batch / "state.json", updated)
    evidence = controller.inspect_trial(batch, next_group, cli=cli)
    write(next_group / "inspection.json", evidence)
    return {"group_dir": str(next_group), "run_ids": policy["run_ids"], "inspection": evidence}


def child_environment():
    env = os.environ.copy()
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return env


def launch_wave(plan, group, tracked, *, popen=subprocess.Popen):
    """Capture the process before the frozen launcher's own descriptor writes."""
    verify_plan(plan)
    previous = read(Path(group) / "policy.json")["previous_group"]
    require(not any(path.exists() for path in stop_paths(plan, previous)), "Previous wave STOP blocks dispatch")
    original = controller.subprocess
    def capture(argv, **kwargs):
        verify_plan(plan)
        require(not any(path.exists() for path in stop_paths(plan, previous)), "STOP arrived during launch inspection")
        import psutil
        process = popen(argv, **kwargs)
        tracked.update(pid=process.pid, argv=argv, started_at=utc().isoformat(),
                       parallel_group_dir=str(group), operations_sources=controller.source_identity())
        try:
            tracked["created_at"] = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            require(process.poll() is not None, "Cannot establish launched controller identity")
        # tracked is in memory before any I/O that might fail.
        write(Path(group) / "continuation-launch-intent.json", tracked)
        return process
    controller.subprocess = SimpleNamespace(Popen=capture,
        CREATE_NO_WINDOW=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return controller.launch(plan["batch"], group)
    finally:
        controller.subprocess = original


def abort_owned(plan, group, cli, *, descriptor=None):
    """Stop only this wave; preserve submitted results and record partial accounting."""
    import psutil
    batch, group = Path(plan["batch"]), Path(group)
    evidence = {"at": utc().isoformat(), "signal_errors": [], "cleanup": {}, "partial_accounting": {}}
    for path in (batch / "CANCEL", group / "CANCEL"):
        try:
            write(path, {"source": REVISION, "at": utc().isoformat()})
        except BaseException as exc:
            evidence["signal_errors"].append(str(exc))
    process = controller_process(batch, group, descriptor=descriptor)
    targets = process.children(recursive=True) + [process] if process else []
    # A controller may have died while its episode children survived. Receipts
    # bind each PID to this wave's exact argv and observed launch time.
    for path in sorted((group / "children").glob("*/process.json")):
        item = read(path)
        try:
            child = psutil.Process(item["pid"])
            require(abs(child.create_time()-timestamp(item["at"]).timestamp()) <= 30
                    and child.cmdline() == item["argv"], "Owned episode PID was reused or changed")
            targets.extend(child.children(recursive=True) + [child])
        except psutil.NoSuchProcess:
            pass
    for item in plan.get("_observed_descendants", {}).get(str(group), {}).values():
        try:
            child = psutil.Process(item["pid"])
            if abs(child.create_time()-item["created_at"]) < .01:
                targets.append(child)
        except psutil.NoSuchProcess:
            pass
    targets = list({item.pid: item for item in targets}.values())
    for item in targets:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=30)
    evidence["processes_stopped"] = not alive
    require(evidence["processes_stopped"], "Owned processes did not stop; container cleanup cannot be claimed")
    policy = read(group / "policy.json")
    card = read(Path(cli.HERE) / "pricing.json")
    for run_id in policy["run_ids"]:
        directory = batch / "results" / run_id
        try:
            evidence["cleanup"][run_id] = cli.cleanup_owned_episode(directory)
        except BaseException as exc:
            evidence["cleanup"][run_id] = {"confirmed": False, "error": str(exc)}
        if directory.exists():
            try:
                reporting = cli.reporting if hasattr(cli, "reporting") else __import__(
                    "modelbench.minimal_value_20260905.reporting", fromlist=["reporting"])
                evidence["partial_accounting"][run_id] = reporting.accounting(directory, card)
            except BaseException as exc:
                evidence["partial_accounting"][run_id] = {"cost_computable": False, "error": str(exc)}
    evidence["confirmed"] = all(item.get("confirmed") is True for item in evidence["cleanup"].values())
    return evidence


def wait_wave(plan, group, cli, *, sleep=time.sleep, clock=time.monotonic):
    value, _ = group_identity(plan["batch"], group)
    launch = read(Path(plan["batch"]) / "launch.json")
    deadline = min(timestamp(plan["deadline"]),
        timestamp(launch["started_at"]) + timedelta(seconds=value["watchdog_seconds"]+CLEANUP_RESERVE))
    start = clock()
    while True:
        verify_plan(plan)
        require(not any(path.exists() for path in stop_paths(plan, group)), "Wave stopped: STOP/CANCEL/TRIP")
        process = controller_process(plan["batch"], group)
        if process is not None:
            import psutil
            tracked = plan.setdefault("_observed_descendants", {}).setdefault(str(group), {})
            for child in process.children(recursive=True):
                try:
                    tracked[child.pid] = {"pid": child.pid, "created_at": child.create_time()}
                except psutil.NoSuchProcess:
                    pass
        if process is None:
            return audit_terminal(plan, group, cli)
        require(utc() < deadline and clock()-start <= value["watchdog_seconds"]+CLEANUP_RESERVE,
                "Finite wave/original package watchdog expired")
        sleep(2)


def run(plan, *, cli=None, waiter=wait_wave, preparer=prepare_next_wave, launcher=launch_wave,
        aborter=abort_owned):
    cli = cli or recovery.load_frozen_cli(plan["batch"])
    run_dir = Path(plan["run_dir"])
    state = {"revision": REVISION, "status": "starting", "started_at": utc().isoformat(),
             "deadline": plan["deadline"], "waves": [], "automatic_retry_allowed": False}
    group, tracked, safe_group = Path(plan["initial_group"]), {}, None
    with continuation_lease(plan["batch"], run_dir):
        try:
            verify_plan(plan)
            require(sha(group / "group.json") == plan["initial_group_sha256"], "Initial wave changed")
            while True:
                state.update(status="waiting_wave", active_group=str(group))
                write(run_dir / "continuation_state.json", state)
                audit = waiter(plan, group, cli)
                safe_group = str(group)
                state["waves"].append({"group_dir": str(group), **audit})
                state["completed_episodes"] = audit["completed_episodes"]
                write(run_dir / "continuation_state.json", state)
                if audit["completed_episodes"] == MAX_EPISODES:
                    state.update(status="completed", active_group=None, completed_at=utc().isoformat(),
                                 cleanup_confirmed=True)
                    write(run_dir / "continuation_state.json", state)
                    return state
                prepared = preparer(plan, cli=cli, audit=audit)
                group, tracked = Path(prepared["group_dir"]), {}
                state.update(status="dispatching", active_group=str(group), next_run_ids=prepared["run_ids"])
                write(run_dir / "continuation_state.json", state)
                descriptor = launcher(plan, group, tracked)
                state["active_controller"] = descriptor
                state["status"] = "waiting_wave"
                write(run_dir / "continuation_state.json", state)
        except BaseException as exc:
            state.update(status="stopped", error={"type": type(exc).__name__, "message": str(exc)},
                         completed_at=utc().isoformat(), active_group=str(group))
            # Even persistence failure after Popen must stop the just-launched wave.
            try:
                active = controller_process(plan["batch"], group, descriptor=tracked or None)
                stage = read(Path(plan["batch"]) / "state.json")
                if active is not None or stage.get("active_episodes") or safe_group != str(group):
                    state["failure_cleanup"] = aborter(plan, group, cli, descriptor=tracked or None)
                else:
                    state["failure_cleanup"] = {"confirmed": True, "no_active_controller": True}
            except BaseException as cleanup_error:
                state["failure_cleanup"] = {"confirmed": False, "error": str(cleanup_error)}
            try:
                write(run_dir / "continuation_state.json", state)
            except BaseException:
                # Raw stderr is an independent final evidence path if filesystem writes fail.
                print(json.dumps(state, ensure_ascii=True), file=sys.stderr, flush=True)
            return state


def launch(batch, run_dir, *, popen=subprocess.Popen):
    plan = inspect(batch, run_dir)
    run_dir = Path(plan["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=False)
    write(run_dir / "plan.json", plan)
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "_run", "--plan", str(run_dir / "plan.json")]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process = None
    try:
        with (run_dir / "stdout.log").open("xb") as out, (run_dir / "stderr.log").open("xb") as err:
            process = popen(argv, cwd=REPO, env=child_environment(), stdout=out, stderr=err, **options)
            import psutil
            descriptor = {"pid": process.pid, "created_at": psutil.Process(process.pid).create_time(),
                          "argv": argv, "plan_sha256": sha(run_dir / "plan.json"),
                          "started_at": utc().isoformat(), "sources": plan["sources"]}
            write(run_dir / "launch.json", descriptor)
        return descriptor
    except BaseException:
        if process is not None:
            # _run cannot dispatch before this launcher publishes its descriptor.
            # Kill the owned process tree independently of filesystem stop signals.
            import psutil
            try:
                owned = psutil.Process(process.pid)
                require(owned.cmdline() == argv
                        and abs(owned.create_time()-timestamp(plan["created_at"]).timestamp()) <= 120,
                        "Supervisor PID identity changed; refuse to kill an unrelated process")
                targets = owned.children(recursive=True) + [owned]
                for item in targets:
                    try:
                        item.kill()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs(targets, timeout=30)
                require(not alive, "Supervisor registration failed and owned process exit is unconfirmed")
            except psutil.NoSuchProcess:
                pass
            current = read(Path(plan["batch"]) / "launch.json")
            current_group = Path(current["parallel_group_dir"]).resolve()
            # Registration may have committed just before the outer I/O failed.
            # Reconcile only a new wave belonging to this continuation directory.
            if current_group.is_relative_to(run_dir):
                abort_owned(plan, current_group, recovery.load_frozen_cli(plan["batch"]))
        raise


def await_registration(plan, *, sleep=time.sleep, clock=time.monotonic):
    import psutil
    path = Path(plan["run_dir"]) / "launch.json"
    until = clock() + 30
    while not path.exists():
        require(clock() < until, "Durable supervisor registration never arrived")
        sleep(.1)
    identity = read(path)
    require(identity.get("pid") == os.getpid()
            and abs(identity["created_at"] - psutil.Process(os.getpid()).create_time()) < .01
            and identity.get("plan_sha256") == sha(Path(plan["run_dir"]) / "plan.json"),
            "Supervisor registration identity differs")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("inspect", "launch", "_run"))
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args(argv)
    if args.command == "_run":
        require(args.plan is not None, "--plan is required")
        plan = read(args.plan)
        try:
            await_registration(plan)
            value = run(plan)
        except BaseException as exc:
            value = {"status": "stopped", "phase": "startup", "completed_at": utc().isoformat(),
                     "error": {"type": type(exc).__name__, "message": str(exc)},
                     "automatic_retry_allowed": False}
            try:
                write(Path(plan["run_dir"]) / "continuation_state.json", value)
            except BaseException:
                print(json.dumps(value, ensure_ascii=True), file=sys.stderr, flush=True)
    else:
        require(args.batch is not None and args.run_dir is not None, "--batch and --run-dir are required")
        value = inspect(args.batch, args.run_dir) if args.command == "inspect" else launch(args.batch, args.run_dir)
    print(json.dumps(value, ensure_ascii=True), flush=True)
    return int(args.command == "_run" and value.get("status") != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
