"""Fail-closed recovery of the existing CP journal and owned role containers.

Recovery does not recreate tools, replay actions, repair logs, or accept work.
The caller must separately reconcile its in-flight execution ledger. CP restore
acquires the existing journal's single-writer lock; it writes no new CP events.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import threading

from .paths import LEGACY, RecoveryRequired
from control_bridge import ExperimentControl, RoleHandle, MODELS
from sandbox import RoleSandbox, ROLES, _call
from dpswarm.control import ControlPlane
from dpswarm.events import Event
from dpswarm import invariants, state
from dpswarm.types import DelegationKind, Level, ModelCatalog, ModelFacts, SealPhase


def _recovery_boundary(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RecoveryRequired:
            raise
        except Exception as exc:
            raise RecoveryRequired("Recovery evidence failed structural validation") from exc
    return guarded


def _require(condition, message):
    if not condition:
        raise RecoveryRequired(message)


def _inside(path, root):
    path = Path(path)
    _require(not path.is_symlink() and path.resolve().is_relative_to(root.resolve()) and path.is_file(),
             "Recovery artifact is missing, symlinked, or outside the run: " + str(path))
    return path.resolve()


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecoveryRequired("Unreadable recovery JSON: " + str(path)) from exc


def _lines(path):
    try:
        raw = Path(path).read_bytes()
        _require(not raw or raw.endswith(b"\n"), "Incomplete journal tail: " + str(path))
        values = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        _require(all(isinstance(v, dict) for v in values), "Non-object journal entry")
        return raw, values
    except (OSError, UnicodeError, ValueError) as exc:
        raise RecoveryRequired("Unreadable recovery journal: " + str(path)) from exc


class RecoverableControl(ExperimentControl):
    @classmethod
    @_recovery_boundary
    def restore(cls, run_dir, models_by_role, task_id):
        """Restore a nonterminal CP plus its exact rich ledger; never repair gaps."""
        obj = cls.__new__(cls)
        obj.run_dir = Path(run_dir).resolve()
        obj.directory = obj.run_dir / "control-plane"
        obj.task_id, obj.models_by_role = task_id, dict(models_by_role)
        obj._lock, obj._closed, obj._finished = threading.RLock(), False, None
        obj._handles, obj._calls, obj._submissions = {}, {}, {}
        _require(models_by_role and all(m in MODELS for m in models_by_role.values()), "Unsupported recovery models")
        manifest = _load(_inside(obj.directory / "manifest.json", obj.run_dir))
        _require(manifest.get("task_id") == task_id and manifest.get("models_by_role") == models_by_role,
                 "Persisted task/model identity does not match recovery request")
        journal = _inside(obj.directory / "events.jsonl", obj.run_dir)
        journal_raw, transactions = _lines(journal)
        rich_path = obj.directory / "rich-events.jsonl"
        rich_raw, rich = _lines(rich_path) if rich_path.exists() else (b"", [])
        events, projection = [], state.Projection()
        try:
            for transaction in transactions:
                batch = transaction.get("events", [transaction])
                _require(isinstance(batch, list) and batch, "Invalid CP transaction")
                for value in batch:
                    event = Event.from_dict(value)
                    _require(event.seq == len(events), "Noncontiguous CP sequence")
                    projection = invariants.check_event(projection, event)
                    events.append(event)
        except Exception as exc:
            raise RecoveryRequired("CP journal does not pass invariant replay") from exc
        _require(events and projection.root_id == manifest.get("root_id"), "CP root identity mismatch")
        _require(projection.seal_phase.get("root", SealPhase.OPEN) == SealPhase.OPEN,
                 "Run is sealing or terminal; active recovery is not permitted")
        obj.catalog = ModelCatalog(point_policy_version="experiment-equal-one-point-unranked")
        for model in sorted(set(models_by_role.values())):
            obj.catalog.register(ModelFacts(obj._provider(model), model, Level.S,
                aa_source="experiment-declared-unranked", context_window=None,
                input_price_per_mtok=None, output_price_per_mtok=None))
        obj.root_item_id = manifest.get("root_item_id")
        try:
            obj.cp = ControlPlane(store_path=journal, catalog=obj.catalog, root_level=Level.S)
            _require(journal.read_bytes() == journal_raw and (rich_path.read_bytes() if rich_path.exists() else b"") == rich_raw,
                     "Recovery journal changed during lock acquisition")
            roots = [it.item_id for it in obj.cp.proj.work_items.values() if it.kind == DelegationKind.ROOT]
            _require(roots == [obj.root_item_id] and obj.cp.root_lead_node == manifest.get("root_control_node_id"),
                     "Persisted root item/node identity mismatch")
            token_events = {ev.seq: ev for ev in events if ev.kind == "token_usage_recorded"}
            consumed_token_events, expected_stops = set(), Counter()
            for row in rich:
                kind = row.get("event")
                _require(kind in ("role_started", "call_recorded", "role_submitted"),
                         "Unsupported or terminal rich event: " + str(kind))
                handle = RoleHandle(**row["handle"])
                if kind == "role_started":
                    _require(handle.node_id not in obj._handles, "Duplicate role start")
                    _require(obj.models_by_role.get(handle.role) == handle.model, "Role model identity mismatch")
                    path = _inside(handle.manifest_path, obj.directory)
                    _require(hashlib.sha256(path.read_bytes()).hexdigest() == handle.manifest_hash, "Role manifest hash mismatch")
                    role_manifest = _load(path)
                    for key, expected in {"task_id": task_id, "root_id": obj.cp.proj.root_id,
                        "item_id": handle.item_id, "role": handle.role, "phase": handle.phase,
                        "model_requested": handle.model, "session_id": handle.session_id,
                        "context_epoch": handle.context_epoch}.items():
                        _require(role_manifest.get(key) == expected, "Role manifest identity mismatch: " + key)
                    node = obj.cp.proj.nodes.get(handle.node_id)
                    _require(node is not None and node.item_id == handle.item_id and node.route.model == handle.model,
                             "Role handle does not match CP node")
                    _require(node.package_ref == handle.manifest_path and node.package_hash == handle.manifest_hash,
                             "CP package identity mismatch")
                    obj._handles[handle.node_id] = handle
                    obj._check_handle(handle)  # unchanged frozen fencing guards
                    continue
                obj._check_handle(handle)
                if kind == "call_recorded":
                    record, seq = row["record"], row["cp_token_event_seq"]
                    cid = record.get("call_id")
                    _require(isinstance(cid, str) and cid and cid not in obj._calls, "Duplicate/missing rich call ID")
                    _require(record.get("task_id") == task_id and record.get("role") == handle.role and record.get("model_requested") == handle.model,
                             "Recorded call identity mismatch")
                    _require(seq in token_events and seq not in consumed_token_events, "Rich/CP token event mismatch")
                    counts = [record.get(k) if type(record.get(k)) is int and record[k] >= 0 else None
                              for k in ("input_tokens", "cached_input_tokens", "output_tokens")]
                    inp, cache, output = counts
                    _require(inp is None or cache is None or cache <= inp, "Invalid cached token count")
                    mapping = {"input_exclusive": inp - cache if inp is not None and cache is not None else None,
                               "output": output, "cache_read": cache, "cache_write": None, "cost_usd": None}
                    expected = {"node_id": handle.node_id, "input": mapping["input_exclusive"],
                                "output": output, "cache_read": cache, "cache_write": None, "cost": None}
                    _require(row.get("cp_mapping") == mapping and token_events[seq].payload == expected,
                             "Token measurements differ between rich ledger and CP")
                    consumed_token_events.add(seq)
                    if record.get("stop_reason") is not None:
                        expected_stops[(handle.node_id, str(record["stop_reason"]))] += 1
                    obj._calls[cid] = {key: row[key] for key in ("handle", "record", "cp_token_event_seq", "cp_mapping", "unknown_fields_preserved")}
                else:
                    _require(handle.item_id not in obj._submissions, "Duplicate role submission")
                    artifact = row["artifact"]
                    path = _inside(artifact["path"], obj.directory)
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    _require(digest == artifact["sha256"] and _load(path).get("handle") == asdict(handle), "Submission artifact mismatch")
                    item = obj.cp.proj.work_items[handle.item_id]
                    _require(item.acceptance.value == "submitted" and item.submission_sha256 == digest,
                             "Submission state/hash differs from CP")
                    obj._submissions[handle.item_id] = dict(artifact)
            _require(consumed_token_events == set(token_events), "Unpaired CP token event; accounting commit may be incomplete")
            actual_stops = Counter((ev.payload["node_id"], ev.payload["stop_reason"]) for ev in events if ev.kind == "stop_reason_recorded")
            _require(expected_stops == actual_stops, "Unpaired stop-reason event")
            role_items = {it.item_id for it in obj.cp.proj.work_items.values() if it.kind != DelegationKind.ROOT}
            _require(role_items == {h.item_id for h in obj._handles.values()}, "Role provisioning commit is incomplete")
            _require({n for n in obj.cp.proj.nodes if n != obj.cp.root_lead_node} == set(obj._handles), "Unpaired CP role node")
            submitted = {ev.payload["item_id"] for ev in events if ev.kind == "work_item_submitted"}
            _require(submitted == set(obj._submissions), "Role submission commit is incomplete")
            return obj
        except Exception as exc:
            if hasattr(obj, "cp"):
                obj.cp.close()
            if isinstance(exc, RecoveryRequired):
                raise
            raise RecoveryRequired("Control recovery failed validation") from exc


@_recovery_boundary
def reattach_sandbox(run_dir, image, *, allow_frozen=False):
    """Read-only docker inspection, then attach only the exact existing IDs."""
    run_dir = Path(run_dir).resolve()
    logs = run_dir / "sandbox"
    manifest = _load(_inside(logs / "containers.json", run_dir))
    _require(Path(manifest.get("run_dir", "")).resolve() == run_dir and
             Path(manifest.get("task_dir", "")).resolve() == run_dir / "task", "Sandbox path identity mismatch")
    _require(manifest.get("closed") is False, "Sandbox is closed or ambiguous")
    if not allow_frozen:
        _require(manifest.get("freeze_started") is False and manifest.get("frozen") is False and
                 manifest.get("frozen_roles") == [], "Sandbox is frozen or ambiguous")
    prefix, containers = manifest.get("prefix"), manifest.get("containers")
    _require(isinstance(prefix, str) and re.fullmatch(r"dpswarm-tb-[a-f0-9]{12}", prefix), "Invalid sandbox owner")
    allowed = set(ROLES) | ({"grader"} if allow_frozen else set())
    _require(isinstance(containers, dict) and set(ROLES) <= set(containers) <= allowed, "Incomplete role container catalog")
    _require(len(set(containers.values())) == len(containers) and all(isinstance(cid, str) and re.fullmatch(r"[a-f0-9]{64}", cid) for cid in containers.values()), "Invalid container IDs")
    old_images = _load(_inside(logs / "image.inspect.json", run_dir))
    old_roles = _load(_inside(logs / "roles.inspect.json", run_dir))
    _require(isinstance(old_images, list) and len(old_images) == 1, "Missing original image identity")
    image_id = old_images[0].get("Id")
    _require(manifest.get("image") in (image, image_id), "Requested image differs from saved sandbox")
    def inspect(args):
        result = _call(args)
        _require(result.returncode == 0, "Docker inspection failed")
        value = json.loads(result.stdout)
        _require(isinstance(value, list), "Invalid Docker inspection response")
        return value
    try:
        live_image = inspect(["docker", "image", "inspect", image])
        _require(len(live_image) == 1 and live_image[0].get("Id") == image_id, "Docker image ID changed")
        current = inspect(["docker", "inspect", *containers.values()])
        _require(isinstance(old_roles, list) and len(old_roles) == len(ROLES) and len(current) == len(containers), "Role inspection is incomplete")
        previous = {item["Id"]: item for item in old_roles}
        live = {item["Id"]: item for item in current}
        _require(set(previous) == {containers[role] for role in ROLES} and set(live) == set(containers.values()), "Container identity changed")
        for role, cid in containers.items():
            item, old = live[cid], previous.get(cid)
            _require(item.get("Image") == image_id and item.get("Name") == "/" + prefix + "-" + role, "Container image/name mismatch")
            config, host, status = item.get("Config", {}), item.get("HostConfig", {}), item.get("State", {})
            _require(config.get("Labels", {}).get("dpswarm.teambench.owner") == prefix, "Container ownership label mismatch")
            if not allow_frozen:
                _require(status.get("Running") is True and status.get("Paused") is False and not status.get("Restarting") and not status.get("Dead"), "Container is not actively running")
            else:
                _require(type(status.get("Running")) is bool and type(status.get("Paused")) is bool and
                         not status.get("Restarting"), "Container state is ambiguous for cleanup")
            if old is not None:
                _require(config == old.get("Config") and host == old.get("HostConfig") and item.get("Mounts") == old.get("Mounts"), "Container configuration or mounts changed")
            else:
                # Frozen grade() did not save a grader inspect snapshot. Its
                # base isolation/config is the executor's, with only its
                # generated hostname and fixed five bind mounts differing.
                executor = previous[containers["executor"]]
                _require({k: v for k, v in config.items() if k != "Hostname"} ==
                         {k: v for k, v in executor["Config"].items() if k != "Hostname"} and
                         {k: v for k, v in host.items() if k != "Mounts"} ==
                         {k: v for k, v in executor["HostConfig"].items() if k != "Mounts"},
                         "Grader configuration differs from frozen sandbox isolation")
            _require(host.get("NetworkMode") == "none" and host.get("ReadonlyRootfs") is True and
                     host.get("Privileged") is False and config.get("User") == "10001:10001" and
                     "ALL" in host.get("CapDrop", []) and "no-new-privileges=true" in host.get("SecurityOpt", []), "Sandbox isolation contract mismatch")
            expected = {"/task/brief.md": (run_dir / "task/brief.md", False)}
            if role != "executor":
                expected["/task/spec.md"] = (run_dir / "task/spec.md", False)
            if role != "planner":
                expected.update({"/shared/workspace": (run_dir / "workspace", role != "verifier"),
                                 "/shared/reports": (run_dir / "reports", role != "verifier")})
            if role in ("verifier", "oracle"):
                expected["/shared/submission"] = (run_dir / "submission", True)
            if role == "grader":
                expected = {"/task": (run_dir / "task", False),
                            "/shared/workspace": (run_dir / "workspace", True),
                            "/shared/reports": (run_dir / "reports", True),
                            "/shared/submission": (run_dir / "submission", False),
                            "/grader": (run_dir / "grader", False)}
            mounts = item.get("Mounts", [])
            _require(len(mounts) == len(expected), "Unexpected mount count")
            _require({m.get("Destination") for m in mounts} == set(expected), "Duplicate/missing mount target")
            for mount in mounts:
                target = mount.get("Destination")
                _require(target in expected and mount.get("Type") == "bind", "Unexpected mount target/type")
                source, writable = expected[target]
                _require(source.exists() and source.resolve() == Path(mount["Source"]).resolve() and
                         source.resolve().is_relative_to(run_dir) and mount.get("RW") is writable,
                         "Role mount source or access mode mismatch")
        sandbox = RoleSandbox(run_dir / "task", run_dir, image=image)
        sandbox.prefix, sandbox.containers = prefix, dict(containers)
        if allow_frozen:
            # An object recovered for final cleanup must never regain candidate
            # tool execution, even if a paused container is later unpaused.
            sandbox._freeze_started = True
            sandbox._frozen = bool(manifest.get("frozen"))
            sandbox._frozen_roles = list(manifest.get("frozen_roles") or [])
        return sandbox
    except RecoveryRequired:
        raise
    except Exception as exc:
        raise RecoveryRequired("Sandbox recovery failed validation") from exc


@_recovery_boundary
def cleanup_finished_sandbox(run_dir, image):
    """Return an owned cleanup-only sandbox, or None if cleanup is recorded done.

    The caller must first verify its durable terminal checkpoint. This function
    never calls close(), unpauses containers, runs candidate tools, or grades.
    """
    root = Path(run_dir).resolve()
    manifest = _load(_inside(root / "sandbox/containers.json", root))
    _require(Path(manifest.get("run_dir", "")).resolve() == root and
             Path(manifest.get("task_dir", "")).resolve() == root / "task", "Sandbox path identity mismatch")
    if manifest.get("closed") is True:
        saved_image = _load(_inside(root / "sandbox/image.inspect.json", root))
        _require(isinstance(saved_image, list) and len(saved_image) == 1 and
                 (manifest.get("image") == image or saved_image[0].get("Id") == image),
                 "Closed sandbox image identity mismatch")
        return None
    return reattach_sandbox(root, image, allow_frozen=True)
