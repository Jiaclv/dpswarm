"""Finite stage controller; only the frozen episode child may generate candidates."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from .contracts import STAGES, schedule, validate_schedule
from .runtime_integrity import atomic_json
from . import freeze

HERE = Path(__file__).resolve().parent
EPISODE_WATCHDOG_SECONDS = 1800 + 2 * 900 + 300
CLEANUP_SECONDS = 120


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_manifest(batch):
    batch = Path(batch)
    path = batch / "manifest.json"
    binding = batch / "manifest.sha256"
    if not binding.is_file() or binding.read_text(encoding="ascii").strip() != sha(path):
        raise ValueError("Frozen manifest identity changed or is missing")
    return read(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def junit_evidence(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = sum(bool(list(case.iter("failure"))) for case in cases)
    errors = sum(bool(list(case.iter("error"))) for case in cases)
    skipped = sum(bool(list(case.iter("skipped"))) for case in cases)
    # Require observed passing cases, not only a hand-set suite total.
    passed = len(cases) > 0 and failures == errors == skipped == 0
    return {"path": str(Path(path).resolve()), "sha256": sha(path), "tests": len(cases),
            "failures": failures, "errors": errors, "skipped": skipped, "passed": passed}


def qualification_evidence(stage, official):
    from .data import load_public, qualification_passes
    from .environment import capture_grader_contract
    official = Path(official)
    contract = capture_grader_contract()
    checks = read(official / "public_checks.json")
    evidence = {}
    for instance in load_public(stage, official):
        instance_id = instance["instance_id"]
        parent = official / "grader" / "preflight" / instance_id
        current = []
        for path in parent.rglob("qualification.json"):
            row = read(path)
            if row.get("grader_contract") == contract:
                match = re.fullmatch(r"attempt_([0-9]+)", path.parent.name)
                current.append((int(match.group(1)) if match else 1, path, row))
        if not current:
            raise ValueError("No task qualification for current contract: " + instance_id)
        latest = max(attempt for attempt, _, _ in current)
        latest_rows = [(path, row) for attempt, path, row in current if attempt == latest]
        if len(latest_rows) != 1:
            raise ValueError("Ambiguous latest task qualification: " + instance_id)
        path, row = latest_rows[0]
        probe = row.get("candidate_probe") or {}
        executed = probe.get("public_checks") or {}
        required = checks.get(instance_id) or {}
        valid_checks = bool(required) and all(
            check_id in executed and type(executed[check_id].get("exit_code")) is int
            and executed[check_id]["exit_code"] in (0, 1)
            and executed[check_id].get("timed_out") is False for check_id in required)
        try:
            valid = (row.get("instance_id") == instance_id and row.get("qualified") is True
                     and probe.get("quiesced") is True and probe.get("detached_writer_stopped") is True
                     and probe.get("patch_applicable") is True and valid_checks
                     and qualification_passes(row.get("baseline") or {}, row.get("reference") or {}))
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError("Latest task qualification failed: " + instance_id)
        evidence[instance_id] = {"path": str(path.resolve()), "sha256": sha(path),
                                 "grader_contract": row["grader_contract"], "attempt": latest}
    if len(evidence) != STAGES[stage]["task_count"]:
        raise ValueError("Missing task qualification")
    return evidence


def gate(stage, *, official=None, destination=None):
    official = Path(official or HERE / "official")
    destination = Path(destination or HERE / "validation" / ("gate-" + stage))
    destination.mkdir(parents=True, exist_ok=False)
    from .transport_identity import transport_identity
    transport_before = transport_identity()
    sources_before, inputs_before = freeze.runtime_sources(), freeze.input_files(official)
    tests = {str(p.relative_to(freeze.REPO)).replace("\\", "/"): sha(p)
             for p in sorted((HERE / "tests").glob("test_*.py"))}
    junit = destination / "junit.xml"
    argv = [sys.executable, "-B", "-m", "pytest", str(HERE / "tests"), "-q",
            "-p", "no:cacheprovider", "--junitxml", str(junit),
            "--basetemp", str(Path(tempfile.gettempdir()) / ("minimum-value-gate-" + os.urandom(8).hex()))]
    with (destination / "pytest.stdout.log").open("wb") as out, (destination / "pytest.stderr.log").open("wb") as err:
        result = subprocess.run(argv, cwd=freeze.REPO, stdout=out, stderr=err, timeout=900)
    check = junit_evidence(junit) if junit.is_file() else {"passed": False}
    qualifiers, error = {}, None
    try:
        qualifiers = qualification_evidence(stage, official)
        from .environment import capture_grader_contract
        contract = capture_grader_contract()
        if any(row["grader_contract"] != contract for row in qualifiers.values()):
            raise ValueError("Task qualifications do not match the current grader/source contract")
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    unchanged = (sources_before == freeze.runtime_sources() and inputs_before == freeze.input_files(official)
                 and tests == {str(p.relative_to(freeze.REPO)).replace("\\", "/"): sha(p)
                               for p in sorted((HERE / "tests").glob("test_*.py"))})
    unchanged = unchanged and transport_before == transport_identity()
    passed = result.returncode == 0 and check["passed"] and unchanged and not error
    report = {"status": "PASS" if passed else "FAIL", "stage": stage, "created_at": utc(),
              "live_run_admission": passed, "runtime_sources": sources_before,
              "transport_environment": transport_before,
              "input_artifacts": inputs_before, "test_sources": tests, "junit": check,
              "pytest_exit_code": result.returncode, "qualification": qualifiers,
              "unchanged_during_gate": unchanged, "error": error}
    atomic_json(destination / "gate.json", report)
    return report


def verify_gate(gate_path, stage, official):
    value = read(gate_path)
    from .transport_identity import assert_transport_identity
    assert_transport_identity(value["transport_environment"])
    if (value.get("status") != "PASS" or value.get("live_run_admission") is not True
            or value.get("stage") != stage or value.get("pytest_exit_code") != 0
            or value.get("runtime_sources") != freeze.runtime_sources()
            or value.get("input_artifacts") != freeze.input_files(official)):
        raise ValueError("Gate does not admit this exact stage, source and input set")
    evidence = junit_evidence(value["junit"]["path"])
    if not evidence["passed"] or evidence != value["junit"]:
        raise ValueError("Actual passing JUnit evidence changed")
    for relative, digest in value["test_sources"].items():
        if sha(freeze.REPO / relative) != digest:
            raise ValueError("Test source changed after gate")
    if qualification_evidence(stage, official) != value["qualification"]:
        raise ValueError("Qualification evidence changed after gate")
    return value


def validate_a1(batch, *, expected_sources=None, expected_transport=None):
    batch = Path(batch)
    manifest = load_manifest(batch)
    if manifest["stage"] != "a1":
        raise ValueError("A1 predecessor required")
    freeze.verify_snapshot(batch, manifest)
    if expected_sources is not None and manifest["runtime_sources"] != expected_sources:
        raise ValueError("A1 exercised another runtime; repeat canary after protocol changes")
    if expected_transport is not None and manifest.get("transport_environment") != expected_transport:
        raise ValueError("A1 exercised another external transport environment")
    state = read(batch / "state.json")
    if state.get("stop_reason") or state.get("completed_episodes") != 10:
        raise ValueError("A1 did not complete all 10 episodes cleanly")
    observed_selection = False
    for entry in manifest["schedule"]:
        result = read(batch / "results" / entry["run_id"] / "episode_result.json")
        from .reporting import stop_reason
        if stop_reason(result):
            raise ValueError("A1 episode failed its engineering contract")
        if entry["arm"] == "D":
            workers = result.get("workers") or []
            if (result.get("workers_with_actual_calls") != 1 or len(workers) != 1
                    or workers[0].get("status") != "completed"
                    or workers[0].get("worker_role") != "test"
                    or workers[0].get("delta_status") != "present"):
                raise ValueError("A1 D did not start and deliver its test worker")
        if entry["arm"] == "T":
            workers = result.get("workers") or []
            if (result.get("workers_with_actual_calls") != 2 or len(workers) != 2
                    or {w.get("worker_role") for w in workers} != {"implementation", "test"}
                    or any(w.get("status") != "completed" or w.get("delta_status") != "present"
                           for w in workers)):
                raise ValueError("A1 T did not start and deliver both workers")
        if entry["arm"] == "R2":
            candidates = result.get("candidates") or {}
            selector = result.get("selector") or {}
            calls = sum(type(c.get("transport_attempt_count")) is int and c["transport_attempt_count"] > 0
                        for c in selector.get("calls", []))
            if (set(candidates) != {"candidate_1", "candidate_2"} or calls < 1
                    or any((c.get("artifact") or {}).get("status") != "present"
                           or c.get("quiesced") is not True or c.get("cleanup_confirmed") is not True
                           for c in candidates.values())):
                raise ValueError("A1 R2 did not freeze both candidates and call its selector")
            selection = result.get("selection") or {}
            selected = candidates.get(selection.get("selected")) or {}
            chosen = selected.get("artifact") or {}
            verified = any(
                v.get("candidate_id") in candidates and v.get("check_id") in entry.get("public_checks", {})
                and type(v.get("exit_code")) is int and v.get("timed_out") is False
                and v.get("official_grading") is False for v in selector.get("verifications", []))
            observed_selection |= bool(
                result.get("selection_source") == "selector" and selector.get("status") == "selected"
                and chosen.get("sha256") and selection.get("sha256") == chosen["sha256"]
                and (result.get("artifact") or {}).get("sha256") == chosen["sha256"]
                and verified)
    if not observed_selection:
        raise ValueError("A1 R2 never completed verified non-fallback selection")
    return {"passed": True, "batch": str(batch.resolve()), "manifest_sha256": sha(batch / "manifest.json"),
            "state_sha256": sha(batch / "state.json")}


def prepare(stage, batch, gate_path, *, official=None, a1_batch=None):
    from .data import load_public
    from .environment import capture_grader_contract
    official, batch = Path(official or HERE / "official"), Path(batch).resolve()
    if batch.exists():
        raise ValueError("Existing batch is never implicitly resumed or overwritten")
    admission = verify_gate(gate_path, stage, official)
    predecessor = None
    if stage == "a2":
        if a1_batch is None:
            raise ValueError("A2 requires the completed A1 batch")
        predecessor = validate_a1(a1_batch, expected_sources=admission["runtime_sources"],
                                  expected_transport=admission["transport_environment"])
    public = load_public(stage, official)
    checks = read(official / "public_checks.json")
    images = {}
    for row in public:
        identity = row["instance_id"]
        image = read(official / "images" / (identity + ".json"))["image_id"]
        if not isinstance(image, str) or not image.startswith("sha256:"):
            raise ValueError("Candidate image must be pinned by image ID")
        if not isinstance(checks.get(identity), dict) or not checks[identity]:
            raise ValueError("Each task requires public checks before admission")
        images[identity] = image
    entries = schedule(stage, public, checks=checks, images=images, grader_contract=capture_grader_contract())
    validate_schedule(entries, stage)
    batch.mkdir(parents=True)
    frozen = freeze.build_snapshot(batch, admission, official=official)
    manifest = {"protocol": "minimum_value_20260905_v1", "stage": stage, "created_at": utc(),
                "scheduled_episodes": len(entries), "schedule": entries, **frozen,
                "transport_environment": admission["transport_environment"],
                "gate_path": str(Path(gate_path).resolve()), "gate_sha256": sha(gate_path),
                "stage_limits": STAGES[stage], "parallel_episodes": 1,
                "candidate_container_slots": 3, "model_slots": 4, "a1_predecessor": predecessor,
                "pricing_sha256": sha(HERE / "pricing.json"),
                "resource_lock_path": str((HERE / "validation" / "active-stage.lock").resolve())}
    atomic_json(batch / "manifest.json", manifest)
    (batch / "manifest.sha256").write_text(sha(batch / "manifest.json") + "\n", encoding="ascii")
    atomic_json(batch / "gate.json", admission)
    freeze.verify_snapshot(batch, manifest)
    return manifest


def launch(batch):
    batch = Path(batch).resolve()
    if any((batch / name).exists() for name in ("launch.json", "controller.lock", "state.json", "results")):
        raise ValueError("Existing launch/results require explicit investigation, never implicit resume")
    if any((batch / name).exists() for name in ("STOP", "CANCEL")):
        raise ValueError("Batch has a stop/cancel marker")
    manifest = load_manifest(batch)
    from .transport_identity import assert_transport_identity
    assert_transport_identity(manifest["transport_environment"])
    return freeze.launch_stage(batch)


def _recorded_containers(episode_dir):
    episode_dir = Path(episode_dir).resolve()
    owned = {}
    for path in episode_dir.rglob("*.json"):
        if not path.resolve().is_relative_to(episode_dir):
            raise ValueError("Resource record escaped its episode directory")
        if path.name in ("container.json", "container-intent.json"):
            row = read(path)
            owner, identity = row.get("owner"), row.get("id") or row.get("name")
            if owner and identity:
                owned[identity] = owner
        elif path.name == "request.json":
            row = read(path)
            if row.get("owner") and row.get("grader_contract"):
                for name in ("controller.json", "containers.json"):
                    if (path.parent / name).is_file():
                        for identity in read(path.parent / name):
                            owned[identity] = row["owner"]
    for identity, owner in owned.items():
        if (not re.fullmatch(r"[a-zA-Z0-9_.-]+", identity)
                or not re.fullmatch(r"[0-9a-f]{32}", owner)):
            raise ValueError("Invalid owned resource identity")
    return owned


def cleanup_owned_episode(episode_dir, *, docker=None, clock=time.monotonic):
    if docker is None:
        from .environment import _docker as docker
    deadline, errors, removed, confirmed = clock() + CLEANUP_SECONDS, [], [], []
    try:
        owned = _recorded_containers(episode_dir)
    except Exception as exc:
        return {"confirmed": False, "errors": [str(exc)], "removed": []}
    for identity, owner in owned.items():
        try:
            if clock() >= deadline:
                raise TimeoutError("Episode cleanup deadline elapsed")
            inspect = docker(["inspect", identity], timeout=max(1, min(15, deadline - clock())), check=False)
            if inspect.returncode:
                if "no such" not in inspect.stderr.lower():
                    raise RuntimeError("Cannot verify container absence: " + identity)
                confirmed.append(identity)
                continue
            info = json.loads(inspect.stdout)[0]
            if info.get("Config", {}).get("Labels", {}).get("dpswarm.swe.owner") != owner:
                raise RuntimeError("Container ownership mismatch: " + identity)
            result = docker(["rm", "-f", identity], timeout=max(1, min(20, deadline - clock())), check=False)
            if result.returncode:
                raise RuntimeError("Owned container removal failed: " + identity)
            verify = docker(["inspect", identity], timeout=max(1, min(15, deadline - clock())), check=False)
            if verify.returncode == 0 or "no such" not in verify.stderr.lower():
                raise RuntimeError("Owned container deletion unconfirmed: " + identity)
            removed.append(identity)
            confirmed.append(identity)
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))
    return {"confirmed": not errors and len(confirmed) == len(owned),
            "owned": owned, "removed": removed, "errors": errors}


def kill_owned_process_tree(process):
    """The process object is a child created by this controller, never a searched PID."""
    import psutil
    errors = []
    try:
        parent = psutil.Process(process.pid)
        members = parent.children(recursive=True) + [parent]
        for member in reversed(members):
            try:
                member.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(members, timeout=15)
        if alive:
            errors.append("Owned process tree still alive")
        process.wait(timeout=5)
    except (psutil.NoSuchProcess, ProcessLookupError):
        process.wait(timeout=5)
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    return {"confirmed": process.poll() is not None and not errors, "errors": errors}


def supervise(process, batch, episode_dir, *, clock=time.monotonic, sleep=time.sleep,
              kill=kill_owned_process_tree, cleanup=cleanup_owned_episode):
    deadline = clock() + EPISODE_WATCHDOG_SECONDS
    while process.poll() is None:
        reason = "cancel_requested" if (Path(batch) / "CANCEL").exists() else (
            "episode_watchdog" if clock() >= deadline else None)
        if reason:
            try:
                killed = kill(process)
            except Exception as exc:
                killed = {"confirmed": False, "errors": [str(exc)]}
            try:
                cleaned = cleanup(episode_dir)
            except Exception as exc:
                cleaned = {"confirmed": False, "errors": [str(exc)]}
            return {"reason": reason, "returncode": process.poll(),
                    "process_cleanup": killed, "container_cleanup": cleaned,
                    "cleanup_confirmed": killed["confirmed"] and cleaned["confirmed"]}
        sleep(.25)
    return {"reason": None if process.returncode == 0 else "episode_process_failed",
            "returncode": process.returncode}


def grade_frozen(result, entry, episode_dir, *, grader=None, cleanup=cleanup_owned_episode):
    from .environment import rootgrade_terminal
    grader = grader or rootgrade_terminal
    artifact = result.get("artifact") or result.get("lead_artifact") or {}
    if (result.get("quiesced") is not True or result.get("cleanup_confirmed") is not True
            or result.get("infrastructure_error") or artifact.get("status") != "present"):
        raise ValueError("Unquiesced, unclean or failed candidate cannot enter grading")
    path, expected = Path(artifact["path"]).resolve(), artifact["sha256"]
    if not path.is_relative_to(Path(episode_dir).resolve()) or sha(path) != expected:
        raise ValueError("Frozen patch identity/path changed before grading")
    attempts = []
    for index in (1, 2):
        if sha(path) != expected:
            raise ValueError("Frozen patch changed between grading attempts")
        try:
            score = grader(entry["instance"], path, expected, Path(episode_dir) / ("grade-" + str(index)),
                           grader_contract=entry["grader_contract"], image=entry.get("image"),
                           model_name=entry["arm"], timeout=900)
        except Exception as exc:
            score = {"completed": False, "resolved": None, "failure_kind": "grader_exception",
                     "infrastructure_error": type(exc).__name__ + ": " + str(exc)}
        if sha(path) != expected:
            raise ValueError("Frozen patch changed during grading")
        attempts.append(score)
        cleaned = cleanup(episode_dir)
        if not cleaned["confirmed"]:
            raise RuntimeError("Grader owned-resource cleanup unconfirmed")
        if score.get("completed") or score.get("failure_kind") == "candidate_empty_patch":
            break
    return attempts[-1], attempts


def episode(batch, run_id, *, run_factory=None, grader=None, account_fn=None, cleanup=cleanup_owned_episode):
    from . import reporting
    from .budget import EpisodeBudget
    from .runner import ValueRun
    from .r2 import R2Run
    batch = Path(batch).resolve()
    manifest = load_manifest(batch)
    matches = [e for e in manifest["schedule"] if e["run_id"] == run_id]
    if len(matches) != 1:
        raise ValueError("Episode is not uniquely present in the frozen schedule")
    entry = matches[0]
    directory = batch / "results" / run_id
    if directory.exists():
        raise ValueError("Episode directory already exists; no implicit rerun")
    if (batch / "CANCEL").exists():
        raise ValueError("Cancelled before episode admission")
    component, result = None, {}
    started = time.monotonic()
    try:
        factory = run_factory or (R2Run if entry["arm"] == "R2" else ValueRun)
        if entry["arm"] == "R2":
            component = factory(batch, entry)
        else:
            budget = EpisodeBudget(scopes={"solver": {"max_calls": 28, "token_limit": 600000,
                                                      "cm_call_allowance": 12}})
            component = factory(batch, entry, budget=budget.scope("solver"),
                                start_clock=started, deadline=started + 1800, grade_enabled=False)
            budget.path = directory / "episode-budget.json"
            budget._persist()
        result = component.run()
        if entry["arm"] != "R2":
            budget.freeze()
            result["budget"] = budget.summary()
            result["root_budget_snapshot"] = budget.snapshot()
        if result.get("infrastructure_error"):
            raise ValueError("Candidate reported infrastructure failure")
        score, attempts = grade_frozen(result, entry, directory, grader=grader, cleanup=cleanup)
        result.update(score=score, grading_attempts=attempts, grading_pending=False)
    except Exception as exc:
        if component is not None:
            component.cancel.set()
        result.update(infrastructure_error={"type": type(exc).__name__, "message": str(exc)})
        cleaned = cleanup(directory)
        result["cleanup_confirmed"] = result.get("cleanup_confirmed") is True and cleaned["confirmed"]
        result["controller_cleanup"] = cleaned
    directory.mkdir(parents=True, exist_ok=True)
    result["episode_wall_seconds"] = time.monotonic() - started
    return save_episode_result(directory, entry, result, account_fn=account_fn)


def save_episode_result(directory, entry, result, *, account_fn=None):
    from . import reporting
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        account = (account_fn or reporting.accounting)(directory, read(HERE / "pricing.json"))
    except Exception as exc:
        result["infrastructure_error"] = {"type": "AccountingError", "message": str(exc)}
        account = {"calls": [], "call_count": None, "api_equivalent_usd": None,
                   "api_equivalent_known_subtotal_usd": None, "cost_computable": False,
                   "usage_unknown_calls": None, "error": str(exc)}
    if account_fn is None and not account.get("error"):
        try:
            integrity = reporting.audit_accounting(directory, result, account)
            result["accounting_integrity"] = integrity
            if integrity.get("passed") is not True:
                raise ValueError("Canonical ledger and call evidence disagree")
        except Exception as exc:
            result["infrastructure_error"] = {"type": "AccountingIntegrityError", "message": str(exc)}
            result.setdefault("accounting_integrity", {"passed": False, "error": str(exc)})
    result.update(run_id=entry["run_id"], arm=entry["arm"], instance_id=entry["instance"]["instance_id"],
                  accounting=account, api_equivalent_usd=account["api_equivalent_usd"],
                  api_equivalent_known_subtotal_usd=account["api_equivalent_known_subtotal_usd"],
                  controller_completed_at=utc())
    result.update(reporting.result_status(result, entry))
    atomic_json(directory / "episode_result.json", result)
    return result


@contextmanager
def stage_lease(path, batch):
    """One active stage across new CLI batches; stale locks require investigation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = os.urandom(16).hex()
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"pid": os.getpid(), "batch": str(Path(batch).resolve()), "token": token, "at": utc()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    lease = {"retain": False}
    try:
        yield lease
    finally:
        if not lease["retain"] and path.is_file() and read(path).get("token") == token:
            path.unlink()


def run_stage(batch, snapshot_root):
    manifest = load_manifest(batch)
    path = manifest.get("resource_lock_path")
    if not path or not Path(path).is_absolute():
        raise ValueError("Shared stage resource lock is not bound")
    with stage_lease(path, batch) as lease:
        try:
            result = _run_stage_with_lease(batch, snapshot_root)
            state_path = Path(batch) / "state.json"
            state = read(state_path) if state_path.is_file() else {}
            supervision = state.get("supervision") or {}
            if supervision.get("cleanup_confirmed") is False or (
                    supervision.get("container_cleanup") or {}).get("confirmed") is False:
                lease["retain"] = True
            return result
        except BaseException:
            # An interrupted controller cannot prove that a model child stopped.
            lease["retain"] = True
            raise


def _run_stage_with_lease(batch, snapshot_root):
    from . import reporting
    batch, snapshot_root = Path(batch).resolve(), Path(snapshot_root).resolve()
    manifest = load_manifest(batch)
    if freeze.verify_snapshot(batch, manifest).resolve() != snapshot_root:
        raise ValueError("Controller snapshot identity mismatch")
    validate_schedule(manifest["schedule"], manifest["stage"])
    from .transport_identity import assert_transport_identity
    assert_transport_identity(manifest["transport_environment"])
    imports = freeze.assert_import_origins(snapshot_root)
    with (batch / "controller.lock").open("x", encoding="utf-8") as lock:
        json.dump({"pid": os.getpid(), "started_at": utc()}, lock)
    if (batch / "state.json").exists() or (batch / "results").exists():
        raise ValueError("Existing stage execution is not implicitly resumed")
    state = {"stage": manifest["stage"], "started_at": utc(), "completed_episodes": 0,
             "stop_reason": None, "imports": imports, "episodes": [],
             "known_cost_usd": 0.0, "token_admission_sum": 0}
    start = time.monotonic()
    limits = STAGES[manifest["stage"]]
    for entry in manifest["schedule"]:
        if (batch / "CANCEL").exists() or (batch / "STOP").exists():
            state["stop_reason"] = "cancel_requested" if (batch / "CANCEL").exists() else "stop_requested"
            break
        if time.monotonic() - start >= limits["dispatch_seconds"]:
            state["stop_reason"] = "stage_dispatch_deadline"
            break
        if state["token_admission_sum"] + 600000 > limits["token_admission_sum"]:
            state["stop_reason"] = "stage_token_admission_limit"
            break
        if state["completed_episodes"] >= limits["episode_limit"] or state["known_cost_usd"] >= limits["cost_stop_usd"]:
            state["stop_reason"] = "stage_episode_or_cost_limit"
            break
        state["active_run_id"] = entry["run_id"]
        state["token_admission_sum"] += 600000
        atomic_json(batch / "state.json", state)
        directory = batch / "results" / entry["run_id"]
        argv = [sys.executable, "-B", "-m", "modelbench.minimal_value_20260905.cli", "_episode",
                "--batch", str(batch), "--run-id", entry["run_id"], "--snapshot-root", str(snapshot_root)]
        logs = batch / "controller-logs"
        logs.mkdir(exist_ok=True)
        with (logs / (entry["run_id"] + ".stdout.log")).open("xb") as out, (
                logs / (entry["run_id"] + ".stderr.log")).open("xb") as err:
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            process = None
            try:
                process = subprocess.Popen(argv, cwd=snapshot_root, env=os.environ.copy(), stdout=out, stderr=err, **options)
                atomic_json(logs / (entry["run_id"] + ".process.json"), {"pid": process.pid, "argv": argv, "at": utc()})
                supervision = supervise(process, batch, directory)
            except Exception as exc:
                killed = kill_owned_process_tree(process) if process is not None and process.poll() is None else {"confirmed": True}
                cleaned = cleanup_owned_episode(directory)
                supervision = {"reason": "episode_supervisor_error", "error": str(exc),
                               "process_cleanup": killed, "container_cleanup": cleaned,
                               "cleanup_confirmed": killed["confirmed"] and cleaned["confirmed"]}
        path = directory / "episode_result.json"
        if supervision["reason"] or not path.is_file():
            cleanup = supervision.get("container_cleanup") or cleanup_owned_episode(directory)
            state["stop_reason"] = supervision["reason"] or "episode_result_missing"
            state["supervision"] = {**supervision, "container_cleanup": cleanup}
            raw = directory / "result.json"
            result = read(raw) if raw.is_file() else {}
            result.update(infrastructure_error={"type": "EpisodeProcessError", "message": state["stop_reason"]},
                          cleanup_confirmed=cleanup["confirmed"], controller_cleanup=cleanup,
                          supervision=supervision)
            result = save_episode_result(directory, entry, result)
        else:
            result = read(path)
        state["completed_episodes"] += 1
        state["known_cost_usd"] += result.get("api_equivalent_known_subtotal_usd") or 0
        state["episodes"].append({"run_id": entry["run_id"], "path": str(path), "sha256": sha(path)})
        state["stop_reason"] = state["stop_reason"] or reporting.stop_reason(result)
        if (result.get("accounting") or {}).get("cost_computable") is not True:
            state["stop_reason"] = state["stop_reason"] or "cost_uncomputable"
        state["cost_warning"] = state["known_cost_usd"] >= limits["cost_warning_usd"]
        atomic_json(batch / "state.json", state)
        reporting.write_report(batch)
        if state["stop_reason"]:
            break
    state.pop("active_run_id", None)
    state["completed_at"] = utc()
    atomic_json(batch / "state.json", state)
    if state["completed_episodes"] == 10 and manifest["stage"] == "a1" and not state["stop_reason"]:
        try:
            state["a1_mechanism_gate"] = validate_a1(batch)
        except Exception as exc:
            state["stop_reason"] = "a1_mechanism_gate_failed"
            state["a1_mechanism_gate"] = {"passed": False, "error": str(exc)}
        atomic_json(batch / "state.json", state)
    return reporting.write_report(batch)


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("gate")
    p.add_argument("--stage", choices=STAGES, required=True)
    p.add_argument("--output", type=Path)
    p = commands.add_parser("prepare")
    p.add_argument("--stage", choices=STAGES, required=True)
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--gate", type=Path, required=True)
    p.add_argument("--a1-batch", type=Path)
    for name in ("launch", "report", "_run", "_episode"):
        p = commands.add_parser(name)
        p.add_argument("--batch", type=Path, required=True)
        if name in ("_run", "_episode"):
            p.add_argument("--snapshot-root", type=Path, required=True)
        if name == "_episode":
            p.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "gate":
        result = gate(args.stage, destination=args.output)
    elif args.command == "prepare":
        result = prepare(args.stage, args.batch, args.gate, a1_batch=args.a1_batch)
    elif args.command == "launch":
        result = launch(args.batch)
    elif args.command == "report":
        from .reporting import write_report
        result = write_report(args.batch)
    elif args.command == "_run":
        result = run_stage(args.batch, args.snapshot_root)
    else:
        manifest = load_manifest(args.batch)
        snapshot = freeze.verify_snapshot(args.batch, manifest)
        if snapshot.resolve() != args.snapshot_root.resolve():
            raise ValueError("Episode snapshot identity mismatch")
        from . import environment, runner, r2
        from .transport_identity import assert_transport_identity
        assert_transport_identity(manifest["transport_environment"])
        freeze.assert_import_origins(snapshot)
        result = episode(args.batch, args.run_id)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.command == "gate" and result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
