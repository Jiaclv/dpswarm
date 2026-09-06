"""Explicit finite continuation using the unchanged A1 snapshot and accounting.

Execute this file as a standalone script. Importing it starts nothing. Recovery
metadata lives outside runtime_snapshot; no submitted episode is rewritten.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE_REPO = Path(__file__).resolve().parents[3]
MODULE = "modelbench.minimal_value_20260905.cli"
DEFAULT_RECOVERY_ID = "stdout-utf8-20260905"


class RecoveryError(RuntimeError):
    pass


def utc():
    return datetime.now(timezone.utc)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def recovery_directory(batch, recovery_id):
    if not isinstance(recovery_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", recovery_id):
        raise RecoveryError("Recovery identifier must be one safe directory component")
    return Path(batch).resolve() / "recoveries" / recovery_id


def load_frozen_cli(batch):
    """A fresh standalone process loads modelbench exclusively from the snapshot."""
    batch = Path(batch).resolve()
    manifest = read(batch / "manifest.json")
    if (batch / "manifest.sha256").read_text(encoding="ascii").strip() != sha(batch / "manifest.json"):
        raise RecoveryError("Manifest identity changed")
    snapshot = batch / "runtime_snapshot"
    for relative, expected in manifest["runtime_sources"].items():
        if sha(snapshot / relative) != expected:
            raise RecoveryError("Frozen runtime changed: " + relative)
    for name, module in sys.modules.copy().items():
        if name == "modelbench" or name.startswith("modelbench.") or name == "dpswarm" or name.startswith("dpswarm."):
            origin = getattr(module, "__file__", None)
            if origin and not Path(origin).resolve().is_relative_to(snapshot):
                raise RecoveryError("Use the standalone recovery script, not an imported mutable package")
    sys.path.insert(0, str(snapshot))
    sys.path.insert(0, str(snapshot / "dpswarm-plugin"))
    from modelbench.minimal_value_20260905 import cli
    from modelbench.minimal_value_20260905.transport_identity import assert_transport_identity
    cli.freeze.verify_snapshot(batch, manifest)
    # The snapshot intentionally excludes local credentials. Read the same
    # original local configuration into process memory before endpoint checks.
    os.environ.update(cli.freeze.credential_environment(source_repo=SOURCE_REPO))
    assert_transport_identity(manifest["transport_environment"])
    cli.freeze.assert_import_origins(snapshot)
    return cli


def previous_controller_status(batch):
    import psutil
    launch = read(Path(batch) / "launch.json")
    pid = launch.get("pid")
    if type(pid) is not int or pid <= 0:
        raise RecoveryError("Previous launch PID is missing or invalid")
    lock = Path(batch) / "controller.lock"
    if lock.is_file() and read(lock).get("pid") != pid:
        raise RecoveryError("Previous controller lock and launch identity disagree")
    try:
        process = psutil.Process(pid)
        started = datetime.fromisoformat(launch["started_at"].replace("Z", "+00:00")).timestamp()
        reused = abs(process.create_time() - started) > 30
        if not reused and process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
            raise RecoveryError("Previous controller is still running")
        return {"pid": pid, "stopped": True, "pid_reused": reused}
    except psutil.NoSuchProcess:
        return {"pid": pid, "stopped": True, "pid_reused": False}


def dispatch_deadline(state, manifest):
    started = datetime.fromisoformat(state["started_at"].replace("Z", "+00:00"))
    if started.tzinfo is None:
        raise RecoveryError("Original stage start must have a timezone")
    return started + timedelta(seconds=manifest["stage_limits"]["dispatch_seconds"])


def audit_episode(batch, entry, cli):
    directory = Path(batch) / "results" / entry["run_id"]
    path = directory / "episode_result.json"
    if not path.is_file():
        raise RecoveryError("Missing completed episode: " + entry["run_id"])
    result = read(path)
    if result.get("run_id") != entry["run_id"] or result.get("arm") != entry["arm"]:
        raise RecoveryError("Episode identity disagrees with the original schedule")
    if result.get("instance_id") != entry["instance"]["instance_id"]:
        raise RecoveryError("Episode task disagrees with the original schedule")
    if result.get("quiesced") is not True or result.get("cleanup_confirmed") is not True:
        raise RecoveryError("Episode is not frozen and cleaned")
    artifact = result.get("artifact") or result.get("lead_artifact") or {}
    if not artifact.get("path") or not Path(artifact["path"]).resolve().is_relative_to(directory.resolve()):
        raise RecoveryError("Episode patch is outside its owned directory")
    before = sha(path)
    reporting = cli.reporting if hasattr(cli, "reporting") else __import__(
        "modelbench.minimal_value_20260905.reporting", fromlist=["reporting"])
    card = read(Path(cli.HERE) / "pricing.json")
    account = reporting.accounting(directory, card)
    integrity = reporting.audit_accounting(directory, result, account)
    if integrity.get("passed") is not True or account.get("cost_computable") is not True:
        raise RecoveryError("Episode accounting is not complete")
    measured = reporting.result_status(result, entry)
    reason = reporting.stop_reason({**result, "accounting": account, **measured})
    if reason:
        raise RecoveryError("Episode failed its frozen contract: " + reason)
    if sha(path) != before:
        raise RecoveryError("Episode changed during audit")
    return {"entry": entry, "result": result, "accounting": account, "audit": integrity,
            "path": str(path.resolve()), "sha256": before,
            "known_cost_usd": account["api_equivalent_known_subtotal_usd"]}


def inspect_recovery(batch, *, recovery_id=DEFAULT_RECOVERY_ID, cli=None,
                     now=None, process_check=previous_controller_status, check_previous=True):
    """Read-only admissibility check; only a complete audited prefix may be skipped."""
    batch = Path(batch).resolve()
    recovery_root = recovery_directory(batch, recovery_id)
    cli = cli or load_frozen_cli(batch)
    manifest, state = cli.load_manifest(batch), read(batch / "state.json")
    if manifest.get("stage") != "a1" or manifest.get("scheduled_episodes") != 10:
        raise RecoveryError("Only the existing 10-episode A1 may be continued")
    cli.validate_schedule(manifest["schedule"], "a1")
    cli.freeze.verify_snapshot(batch, manifest)
    if any((batch / marker).exists() for marker in ("STOP", "CANCEL")):
        raise RecoveryError("Existing STOP/CANCEL marker blocks recovery")
    if state.get("active_run_id") or state.get("completed_at") or state.get("stop_reason"):
        raise RecoveryError("Root must explicitly restore the stopped state before continuation")
    recovery = state.get("recovery") or {}
    if recovery.get("recovery_id") != recovery_id or recovery.get("status") != "evidence_restored_awaiting_explicit_resume":
        raise RecoveryError("Explicit matching evidence-restoration record is required")
    record = Path(recovery.get("record", "")).resolve()
    if not record.is_relative_to(recovery_root) or not record.is_file():
        raise RecoveryError("Recovery evidence record is missing or outside this recovery")
    completed, remaining, gap = [], [], False
    for entry in manifest["schedule"]:
        directory = batch / "results" / entry["run_id"]
        if directory.exists():
            if gap:
                raise RecoveryError("Existing episodes are not an ordered completed prefix")
            completed.append(audit_episode(batch, entry, cli))
        else:
            gap = True
            remaining.append(entry)
    if not completed or not remaining:
        raise RecoveryError("Continuation requires an audited prefix and unstarted remaining episodes")
    if state.get("completed_episodes") != len(completed):
        raise RecoveryError("State completed count differs from the audited prefix")
    if state.get("token_admission_sum") != len(completed) * 600000:
        raise RecoveryError("Prior admissions include an unexplained or partial episode")
    total_cost = sum(item["known_cost_usd"] for item in completed)
    if not math.isclose(state.get("known_cost_usd", -1), total_cost, rel_tol=0, abs_tol=1e-9):
        raise RecoveryError("Prior known cost differs from the audited prefix")
    expected = [{"run_id": item["entry"]["run_id"], "path": item["path"], "sha256": item["sha256"]} for item in completed]
    if state.get("episodes") != expected:
        raise RecoveryError("State episode identities/hashes differ from audited results")
    deadline = dispatch_deadline(state, manifest)
    current = now or utc()
    if deadline <= current:
        raise RecoveryError("Original 9-hour dispatch deadline has elapsed")
    previous = process_check(batch) if check_previous else None
    return {"recovery_id": recovery_id, "batch": str(batch), "manifest_sha256": sha(batch / "manifest.json"),
            "state_sha256": sha(batch / "state.json"), "recovery_record_sha256": sha(record),
            "completed_prefix": expected, "remaining_run_ids": [e["run_id"] for e in remaining],
            "completed_episodes": len(completed), "known_cost_usd": total_cost,
            "token_admission_sum": state["token_admission_sum"], "original_started_at": state["started_at"],
            "original_dispatch_deadline": deadline.isoformat(), "previous_controller": previous,
            "remaining_dispatch_seconds": (deadline - current).total_seconds()}


def child_environment(cli, snapshot, *, credential_source=None):
    env = (cli.freeze.credential_environment(source_repo=credential_source)
           if credential_source is not None else os.environ.copy())
    env["PYTHONPATH"] = os.pathsep.join((str(snapshot), str(Path(snapshot) / "dpswarm-plugin")))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def launch(batch, *, recovery_id=DEFAULT_RECOVERY_ID):
    """Explicit caller action; archive old launch/lock before creating a new controller."""
    batch = Path(batch).resolve()
    cli = load_frozen_cli(batch)
    plan = inspect_recovery(batch, recovery_id=recovery_id, cli=cli)
    directory = recovery_directory(batch, recovery_id) / "controller-launch"
    directory.mkdir(parents=True, exist_ok=False)
    archived = {}
    for name in ("launch.json", "controller.lock"):
        path = batch / name
        if path.is_file():
            expected = sha(path)
            target = directory / ("previous-" + name)
            shutil.copyfile(path, target)
            if sha(target) != expected or sha(path) != expected:
                raise RecoveryError("Previous controller record changed during archival")
            archived[name] = {"path": str(target), "sha256": expected}
    plan["archived_controller_records"] = archived
    plan["controller_source_sha256"] = sha(__file__)
    cli.atomic_json(directory / "plan.json", plan)
    lock = batch / "controller.lock"
    if lock.is_file():
        if sha(lock) != archived["controller.lock"]["sha256"]:
            raise RecoveryError("Controller lock changed after inspection")
        lock.unlink()
    snapshot = batch / "runtime_snapshot"
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "_run", "--batch", str(batch),
            "--recovery-id", recovery_id, "--snapshot-root", str(snapshot)]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        with (directory / "stdout.log").open("xb") as out, (directory / "stderr.log").open("xb") as err:
            process = subprocess.Popen(argv, cwd=snapshot,
                env=child_environment(cli, snapshot, credential_source=SOURCE_REPO), stdout=out, stderr=err, **options)
    except Exception as exc:
        cli.atomic_json(directory / "launch-failure.json",
                        {"at": utc().isoformat(), "type": type(exc).__name__, "message": str(exc),
                         "automatic_retry_allowed": False})
        raise
    descriptor = {"pid": process.pid, "argv": argv, "snapshot_root": str(snapshot), "started_at": utc().isoformat(),
                  "recovery_id": recovery_id, "previous_records": archived, "controller_source_sha256": sha(__file__)}
    cli.atomic_json(batch / "launch.json", descriptor)
    cli.atomic_json(directory / "launch.json", descriptor)
    return descriptor


def known_stdout_failure(path):
    data = Path(path).read_bytes() if Path(path).is_file() else b""
    return (b"UnicodeEncodeError" in data and b"cli.py" in data
            and b"print(json.dumps(result, ensure_ascii=False), flush=True)" in data)


def child_result(batch, entry, supervision, stderr_path, cli):
    """Preserve committed bytes even when the child exits nonzero after saving."""
    audited = audit_episode(batch, entry, cli)
    reason = supervision.get("reason")
    if reason is None and supervision.get("returncode") in (None, 0):
        return audited, None
    if reason == "episode_process_failed" and known_stdout_failure(stderr_path):
        return audited, {"kind": "verified_post_commit_stdout_encoding_failure",
                         "returncode": supervision.get("returncode"), "stderr_sha256": sha(stderr_path),
                         "committed_episode_sha256": audited["sha256"], "accounting_audit": audited["audit"]}
    raise RecoveryError("Child exited abnormally despite a preserved result: " + str(reason))


def run_remaining(batch, *, recovery_id=DEFAULT_RECOVERY_ID, cli=None, now=utc,
                  popen=subprocess.Popen, supervise=None):
    batch = Path(batch).resolve()
    cli = cli or load_frozen_cli(batch)
    directory = recovery_directory(batch, recovery_id) / "controller-launch"
    plan = read(directory / "plan.json")
    if plan.get("controller_source_sha256") != sha(__file__):
        raise RecoveryError("Recovery controller source changed after explicit launch")
    if sha(batch / "state.json") != plan["state_sha256"]:
        raise RecoveryError("Restored state changed between recovery launch and admission")
    current = inspect_recovery(batch, recovery_id=recovery_id, cli=cli, now=now(), check_previous=False)
    if current["completed_prefix"] != plan["completed_prefix"] or current["remaining_run_ids"] != plan["remaining_run_ids"]:
        raise RecoveryError("Recovery prefix or remaining schedule changed")
    manifest, state = cli.load_manifest(batch), read(batch / "state.json")
    snapshot = batch / "runtime_snapshot"
    deadline = dispatch_deadline(state, manifest)
    reporting = cli.reporting if hasattr(cli, "reporting") else __import__(
        "modelbench.minimal_value_20260905.reporting", fromlist=["reporting"])
    limits = manifest["stage_limits"]
    result_summary = None
    process = None
    active_directory = None
    active_cost_recorded = False
    with cli.stage_lease(manifest["resource_lock_path"], batch) as lease:
        with (batch / "controller.lock").open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "started_at": now().isoformat(), "recovery_id": recovery_id}, stream)
        state["recovery"] = {**state["recovery"], "status": "resuming",
                             "resume_started_at": now().isoformat(), "original_dispatch_deadline": deadline.isoformat()}
        cli.atomic_json(batch / "state.json", state)
        try:
            for entry in manifest["schedule"][len(plan["completed_prefix"]):]:
                if any((batch / name).exists() for name in ("STOP", "CANCEL")):
                    state["stop_reason"] = "cancel_requested" if (batch / "CANCEL").exists() else "stop_requested"
                    break
                if now() >= deadline:
                    state["stop_reason"] = "stage_dispatch_deadline"
                    break
                if (state["completed_episodes"] >= limits["episode_limit"]
                        or state["token_admission_sum"] + 600000 > limits["token_admission_sum"]
                        or state["known_cost_usd"] >= limits["cost_stop_usd"]):
                    state["stop_reason"] = "stage_episode_token_or_cost_limit"
                    break
                active_directory = batch / "results" / entry["run_id"]
                if active_directory.exists():
                    raise RecoveryError("Remaining episode directory already exists; never implicitly rerun")
                state["active_run_id"] = entry["run_id"]
                active_cost_recorded = False
                state["token_admission_sum"] += 600000
                cli.atomic_json(batch / "state.json", state)
                logs = batch / "controller-logs"
                logs.mkdir(exist_ok=True)
                stderr_path = logs / (entry["run_id"] + ".stderr.log")
                argv = [sys.executable, "-B", "-m", MODULE, "_episode", "--batch", str(batch),
                        "--run-id", entry["run_id"], "--snapshot-root", str(snapshot)]
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                with (logs / (entry["run_id"] + ".stdout.log")).open("xb") as out, stderr_path.open("xb") as err:
                    process = popen(argv, cwd=snapshot, env=child_environment(cli, snapshot), stdout=out, stderr=err, **options)
                    cli.atomic_json(logs / (entry["run_id"] + ".process.json"),
                                    {"pid": process.pid, "argv": argv, "at": now().isoformat(), "recovery_id": recovery_id})
                    observed = (supervise or cli.supervise)(process, batch, active_directory)
                try:
                    audited, explanation = child_result(batch, entry, observed, stderr_path, cli)
                except Exception:
                    cleaned = observed.get("container_cleanup") or cli.cleanup_owned_episode(active_directory)
                    state["supervision"] = {**observed, "container_cleanup": cleaned}
                    if not cleaned.get("confirmed") or observed.get("cleanup_confirmed") is False:
                        lease["retain"] = True
                    # Known costs from a failed attempt remain visible without inventing a completed result.
                    account = reporting.accounting(active_directory, read(Path(cli.HERE) / "pricing.json"))
                    state["known_cost_usd"] += account.get("api_equivalent_known_subtotal_usd") or 0
                    active_cost_recorded = True
                    cli.atomic_json(directory / (entry["run_id"] + "-failure.json"),
                                    {"supervision": state["supervision"], "accounting": account,
                                     "existing_episode_preserved": (active_directory / "episode_result.json").is_file()})
                    raise
                if explanation:
                    cli.atomic_json(directory / (entry["run_id"] + "-exit-explanation.json"), explanation)
                state["completed_episodes"] += 1
                state["known_cost_usd"] += audited["known_cost_usd"]
                active_cost_recorded = True
                state["episodes"].append({key: audited[key] for key in ("path", "sha256")} | {"run_id": entry["run_id"]})
                state["cost_warning"] = state["known_cost_usd"] >= limits["cost_warning_usd"]
                state.pop("active_run_id", None)
                cli.atomic_json(batch / "state.json", state)
                reporting.write_report(batch)
                process = None
        except BaseException as exc:
            if active_directory is not None and state.get("active_run_id"):
                try:
                    killed = (cli.kill_owned_process_tree(process)
                              if process is not None and process.poll() is None else {"confirmed": True})
                    cleaned = cli.cleanup_owned_episode(active_directory)
                    state["supervision"] = {**state.get("supervision", {}),
                                           "process_cleanup": killed, "container_cleanup": cleaned}
                    if not killed.get("confirmed") or not cleaned.get("confirmed"):
                        lease["retain"] = True
                except BaseException as cleanup_error:
                    lease["retain"] = True
                    state["cleanup_error"] = {"type": type(cleanup_error).__name__, "message": str(cleanup_error)}
                if not active_cost_recorded:
                    try:
                        account = reporting.accounting(active_directory, read(Path(cli.HERE) / "pricing.json"))
                        state["known_cost_usd"] += account.get("api_equivalent_known_subtotal_usd") or 0
                        state["failed_attempt_accounting"] = account
                    except Exception as accounting_error:
                        state["failed_attempt_accounting"] = {"cost_computable": False,
                                                              "error": str(accounting_error)}
            state["stop_reason"] = state.get("stop_reason") or "recovery_controller_failure"
            state["recovery_error"] = {"type": type(exc).__name__, "message": str(exc)}
        if not lease["retain"]:
            state.pop("active_run_id", None)
        state["completed_at"] = now().isoformat()
        state["recovery"]["status"] = "finished" if not state.get("stop_reason") else "stopped"
        cli.atomic_json(batch / "state.json", state)
        if state["completed_episodes"] == 10 and not state.get("stop_reason"):
            try:
                state["a1_mechanism_gate"] = cli.validate_a1(batch)
            except Exception as exc:
                state["stop_reason"] = "a1_mechanism_gate_failed"
                state["a1_mechanism_gate"] = {"passed": False, "error": str(exc)}
                state["recovery"]["status"] = "stopped"
            cli.atomic_json(batch / "state.json", state)
        try:
            result_summary = reporting.write_report(batch)
        except Exception as exc:
            state["stop_reason"] = state.get("stop_reason") or "recovery_report_failure"
            state["recovery"]["status"] = "stopped"
            state["report_error"] = {"type": type(exc).__name__, "message": str(exc)}
            cli.atomic_json(batch / "state.json", state)
            result_summary = {"stage_complete": False, "completed_episodes": state["completed_episodes"],
                              "stop_reason": state["stop_reason"], "report_error": state["report_error"]}
    return result_summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("inspect", "launch", "_run"))
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--recovery-id", default=DEFAULT_RECOVERY_ID)
    parser.add_argument("--snapshot-root", type=Path)
    args = parser.parse_args(argv)
    if args.snapshot_root is not None and args.snapshot_root.resolve() != args.batch.resolve() / "runtime_snapshot":
        raise RecoveryError("Recovery snapshot path disagrees with original batch")
    if args.command == "inspect":
        result = inspect_recovery(args.batch, recovery_id=args.recovery_id)
    elif args.command == "launch":
        result = launch(args.batch, recovery_id=args.recovery_id)
    else:
        result = run_remaining(args.batch, recovery_id=args.recovery_id)
    # This status summary must remain printable even under legacy Windows pipes.
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 1 if args.command == "_run" and result.get("stop_reason") else 0


if __name__ == "__main__":
    raise SystemExit(main())
