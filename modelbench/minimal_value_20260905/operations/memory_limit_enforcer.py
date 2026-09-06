"""Bounded external enforcement of a 1 GiB maximum for this group's containers.

Registration precedes Docker discovery. Every mutation rechecks the actual
owner label and uses the immutable container ID. Polling does not promise that
new containers were created with this limit; the original request stays intact.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

GIB = 1024 ** 3
MAX_SECONDS = 9300
POLL_SECONDS = 0.5
OWNER = re.compile(r"[0-9a-f]{32}")
IDENTITY = re.compile(r"[A-Za-z0-9_.-]+")
TERMINAL = {"completed_awaiting_review", "failed"}


class EnforcementError(RuntimeError):
    pass


class GroupDeadline(EnforcementError):
    pass


def utc():
    return datetime.now(timezone.utc)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def group_contract(group_dir):
    group_dir = Path(group_dir).resolve()
    group = read(group_dir / "group.json")
    batch = Path(group["batch"]).resolve()
    manifest = read(batch / "manifest.json")
    if sha(batch / "manifest.json") != group.get("manifest_sha256"):
        raise EnforcementError("Original group manifest changed")
    ids = group.get("run_ids")
    if not isinstance(ids, list) or len(ids) != len(set(ids)) or len(ids) != 4:
        raise EnforcementError("Exactly the existing four group episodes are required")
    declared = {entry["run_id"] for entry in manifest["schedule"]}
    if any(not isinstance(value, str) or not IDENTITY.fullmatch(value) or value not in declared for value in ids):
        raise EnforcementError("Invalid or unscheduled group episode")
    launched = read(group_dir / "controller-launch" / "launch.json")
    started = datetime.fromisoformat(launched["started_at"].replace("Z", "+00:00"))
    if started.tzinfo is None:
        raise EnforcementError("Group start has no timezone")
    return {"group_dir": str(group_dir), "batch": str(batch), "run_ids": ids,
            "owned_roots": [str(batch / "results" / identity) for identity in ids],
            "group_sha256": sha(group_dir / "group.json"), "manifest_sha256": group["manifest_sha256"],
            "group_started_at": launched["started_at"], "deadline": (started + timedelta(seconds=MAX_SECONDS)).isoformat(),
            "memory_cap_bytes": GIB, "swap_policy": "memory_swap_equals_enforced_memory",
            "preserve_lower_existing_limits": True, "poll_target_seconds": POLL_SECONDS,
            "creation_limit_guaranteed": False,
            "limitation": "A newly created container can retain its original limit until discovery and verified update."}


def registrations(roots):
    """Only these episode-owned records can authorize a container owner."""
    registered = {}
    for root in map(Path, roots):
        root = root.resolve()
        if not root.exists():
            continue
        paths = sorted({path for name in ("container-intent.json", "container.json", "request.json")
                        for path in root.rglob(name)})
        for path in paths:
            if not path.resolve().is_relative_to(root):
                raise EnforcementError("Registration escaped its episode")
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                continue
            row = json.loads(raw.decode("utf-8"))
            if not isinstance(row, dict):
                raise EnforcementError("Invalid resource registration")
            if path.name == "request.json" and not row.get("grader_contract"):
                continue
            owner = row.get("owner")
            if owner is None:
                continue
            if not isinstance(owner, str) or not OWNER.fullmatch(owner):
                raise EnforcementError("Invalid registered owner")
            evidence = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                        "declared_memory": row.get("memory"), "record_kind": path.name}
            registered.setdefault(owner, []).append(evidence)
    return registered


def docker_command(args, *, timeout=8):
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def absent(result):
    return result.returncode != 0 and "no such" in (result.stderr or "").lower()


def inspect_owned(identity, owner, docker):
    result = docker(["inspect", identity])
    if absent(result):
        return None
    if result.returncode:
        raise EnforcementError("Docker inspect failed: " + (result.stderr or "").strip()[:1000])
    records = json.loads(result.stdout)
    if not isinstance(records, list) or len(records) != 1:
        raise EnforcementError("Unexpected Docker inspection response")
    row = records[0]
    if (row.get("Config", {}).get("Labels") or {}).get("dpswarm.swe.owner") != owner:
        raise EnforcementError("Container owner label does not match registered episode")
    identity = row.get("Id")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise EnforcementError("Container does not have a complete immutable ID")
    return row


def discover(registered, docker):
    owned = {}
    for owner in sorted(registered):
        # Query exact registered owner, including stopped/created containers.
        result = docker(["ps", "-aq", "--no-trunc", "--filter", "label=dpswarm.swe.owner=" + owner])
        if result.returncode:
            raise EnforcementError("Docker owner discovery failed: " + (result.stderr or "").strip()[:1000])
        for identity in result.stdout.split():
            if not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise EnforcementError("Discovery returned an invalid immutable ID")
            if identity in owned and owned[identity] != owner:
                raise EnforcementError("Container appears under conflicting owners")
            owned[identity] = owner
    return owned


def apply_cap(identity, owner, evidence, *, docker=docker_command, now=utc):
    before = inspect_owned(identity, owner, docker)
    if before is None:
        return {"container_id": identity, "owner": owner, "status": "gone_before_update", "at": now().isoformat()}
    limits = before.get("HostConfig") or {}
    memory, swap = int(limits.get("Memory") or 0), int(limits.get("MemorySwap") or 0)
    target = memory if 0 < memory < GIB else GIB
    event = {"at": now().isoformat(), "container_id": before["Id"], "owner": owner,
             "container_pid": (before.get("State") or {}).get("Pid"),
             "container_created_at": before.get("Created"),
             "oom_killed": (before.get("State") or {}).get("OOMKilled"),
             "registration": evidence, "requested_previous_memory_bytes": memory,
             "requested_previous_swap_bytes": swap,
             "target_memory_bytes": target, "target_swap_bytes": target,
             "enforcer_pid": os.getpid(), "enforcer_sha256": sha(__file__)}
    if memory == target and swap == target:
        return event | {"status": "already_within_cap", "observed_memory_bytes": memory, "observed_swap_bytes": swap}
    result = docker(["update", "--memory", str(target), "--memory-swap", str(target), before["Id"]])
    after = inspect_owned(before["Id"], owner, docker)
    if after is None:
        return event | {"status": "disappeared_during_update", "update_returncode": result.returncode}
    limits = after.get("HostConfig") or {}
    event.update(update_returncode=result.returncode, verified_at=now().isoformat(),
                 observed_memory_bytes=limits.get("Memory"), observed_swap_bytes=limits.get("MemorySwap"))
    if result.returncode or limits.get("Memory") != target or limits.get("MemorySwap") != target:
        event.update(status="enforcement_failed", error=(result.stderr or "").strip()[:1000])
        return event
    return event | {"status": "updated_and_verified"}


def group_finished(group_dir):
    path = Path(group_dir) / "trial-state.json"
    return path.is_file() and read(path).get("status") in TERMINAL


def run(group_dir, *, docker=docker_command, now=utc, sleep=time.sleep):
    group_dir = Path(group_dir).resolve()
    output = group_dir / "memory-1g-enforcer"
    plan = read(output / "plan.json")
    if sha(__file__) != plan["enforcer_sha256"]:
        raise EnforcementError("Enforcer source changed after launch")
    current = group_contract(group_dir)
    if any(current[key] != plan[key] for key in ("group_sha256", "manifest_sha256", "owned_roots", "deadline")):
        raise EnforcementError("Enforcer scope or deadline changed")
    deadline = datetime.fromisoformat(plan["deadline"])
    def bounded_docker(args):
        remaining = (deadline - now()).total_seconds()
        if remaining <= 0:
            raise GroupDeadline("Original group watchdog reached during resource discovery")
        return docker(args, timeout=min(8, remaining))
    seen, update_count, cycles = {}, 0, 0
    state = {"status": "running", "started_at": now().isoformat(), "pid": os.getpid(),
             "scope": current, "verified_update_count": 0}
    write(output / "state.json", state)
    try:
        while now() < deadline:
            if (output / "STOP").exists():
                state["status"] = "stopped_by_operator"
                break
            if sha(group_dir / "group.json") != plan["group_sha256"]:
                raise EnforcementError("Group changed while enforcing limits")
            known = registrations(plan["owned_roots"])
            owned = discover(known, bounded_docker)
            cycle_start = time.monotonic()
            for identity, owner in owned.items():
                event = apply_cap(identity, owner, known[owner], docker=bounded_docker, now=now)
                signature = (event["status"], event.get("observed_memory_bytes"),
                             event.get("observed_swap_bytes"), event.get("oom_killed"))
                if seen.get(identity) != signature or event["status"] == "updated_and_verified":
                    with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    seen[identity] = signature
                if event["status"] == "enforcement_failed":
                    write(output / "ALARM.json", event)
                    raise EnforcementError("Requested memory cap was not verified for " + identity)
                if event["status"] == "updated_and_verified":
                    update_count += 1
            cycles += 1
            state.update(last_cycle_at=now().isoformat(), registered_owner_count=len(known),
                         observed_owned_container_count=len(owned), cycles=cycles,
                         verified_update_count=update_count, last_update_phase_seconds=time.monotonic()-cycle_start,
                         target_poll_seconds=POLL_SECONDS, polling_is_hard_realtime=False)
            write(output / "state.json", state)
            # 'owned' includes non-running containers. No helper exit while any
            # registered container still exists; no parent result is rewritten.
            if group_finished(group_dir) and not owned:
                state["status"] = "group_finished_owned_containers_absent"
                break
            sleep(POLL_SECONDS)
        else:
            state["status"] = "group_watchdog_deadline"
    except GroupDeadline:
        state["status"] = "group_watchdog_deadline"
    except BaseException as exc:
        state.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        if not (output / "ALARM.json").exists():
            write(output / "ALARM.json", state["error"] | {"at": now().isoformat()})
    state["completed_at"] = now().isoformat()
    write(output / "state.json", state)
    return state


def launch(group_dir):
    group_dir = Path(group_dir).resolve()
    plan = group_contract(group_dir)
    if utc() >= datetime.fromisoformat(plan["deadline"]):
        raise EnforcementError("Original group watchdog has elapsed")
    output = group_dir / "memory-1g-enforcer"
    output.mkdir(parents=True, exist_ok=False)
    plan["enforcer_sha256"] = sha(__file__)
    write(output / "plan.json", plan)
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "_run", "--group-dir", str(group_dir)]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        with (output / "stdout.log").open("xb") as out, (output / "stderr.log").open("xb") as err:
            process = subprocess.Popen(argv, cwd=group_dir, env=env, stdout=out, stderr=err, **options)
    except Exception as exc:
        write(output / "ALARM.json", {"at": utc().isoformat(), "type": type(exc).__name__, "message": str(exc)})
        raise
    descriptor = {"pid": process.pid, "argv": argv, "started_at": utc().isoformat(),
                  "enforcer_sha256": plan["enforcer_sha256"], "group_sha256": plan["group_sha256"]}
    write(output / "launch.json", descriptor)
    return descriptor


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("inspect", "launch", "_run"))
    parser.add_argument("--group-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    value = group_contract(args.group_dir) if args.command == "inspect" else (
        launch(args.group_dir) if args.command == "launch" else run(args.group_dir))
    print(json.dumps(value, ensure_ascii=True), flush=True)
    return int(args.command == "_run" and value.get("status") in ("failed", "group_watchdog_deadline"))


if __name__ == "__main__":
    raise SystemExit(main())
