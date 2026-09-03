"""Per-run DPswarm control for a single Lead with optional SWE workers.

This bridge grants no SWE-bench correctness verdict and runs no model/container.
The host owns one shared RunBudget, isolated repositories and patch application.
Only the Lead can adopt a worker's immutable evidence. Levels are equal admission
labels in this experiment, not AA measurements or a production model policy.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4


REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "dpswarm-plugin"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from dpswarm import invariants, state
from dpswarm.control import ControlPlane, ControlPlaneError
from dpswarm.team_runtime.ledger import ExecutionStore, RunBudget, canonical, clone, digest, identifier
from dpswarm.types import (DelegationKind, Level, ModelCatalog, ModelFacts, ModelRoute,
                           RootExecutionSpec, RouteSource, SealPhase)


MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "glm-5.3", "glm-5.3-flash")
TERMINAL = {"adopted", "discarded", "failed", "finished"}


def _error(code, message):
    raise ControlPlaneError(code, message)


def _provider(model):
    return "glm-coding" if model.startswith("glm-") else "codex-chatgpt"


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical(value).encode("utf-8")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class AgentHandle:
    run_id: str
    root_id: str
    instance_id: str
    role: str
    model: str
    provider: str
    item_id: str
    node_id: str
    attempt: int
    session_id: str
    context_epoch: int
    manifest_path: str
    manifest_hash: str


class SweControl:
    """Fresh run only; an existing journal requires explicit recovery tooling.

    delegate accepts a request dict or a single-element list and returns a list
    containing one provisioning handle. The runtime may call it concurrently;
    the lock and real CP leases limit outstanding workers to two. Multi-request
    batches are rejected before writes, rather than partially creating workers.
    """
    def __init__(self, run_dir, instance_id, lead_model="gpt-5.6-sol", max_workers=2, *, run_id=None):
        if lead_model not in MODELS:
            _error("MODEL_UNAVAILABLE", "Lead model is outside the experiment catalog")
        if type(max_workers) is not int or not 1 <= max_workers <= 2:
            _error("WORKER_LIMIT", "This experiment allows one or two outstanding workers")
        self.run_dir = Path(run_dir).resolve()
        self.instance_id = identifier(instance_id, "instance_id")
        self.run_id = identifier(run_id or self.run_dir.name, "run_id")
        self.directory = self.run_dir / "control-plane"
        if self.directory.exists():
            _error("RUN_EXISTS", "Control directory exists; never overwrite or implicitly resume it")
        self.directory.mkdir(parents=True)
        self.max_workers = max_workers
        self._lock = threading.RLock()
        self._agents, self._calls, self._decisions = {}, {}, []
        self._closed, self._finished = False, None
        self.catalog = ModelCatalog(point_policy_version="swe-equal-one-point-unranked-v1")
        for model in MODELS:
            self.catalog.register(ModelFacts(_provider(model), model, Level.B,
                aa_dimensional={}, aa_source="experiment-declared-unranked", context_window=None,
                input_price_per_mtok=None, output_price_per_mtok=None))
        spec = RootExecutionSpec(max_open_work_items=max_workers,
            max_active_node_points=max_workers + 1, max_team_workers=max_workers,
            max_attempts=1, max_depth=2, root_acceptance_mode="lead-patch-selection",
            point_policy_version=self.catalog.point_policy_version)
        self.cp = ControlPlane(spec=spec, store_path=self.directory / "events.jsonl",
                               catalog=self.catalog, root_level=Level.B)
        self.ledger = ExecutionStore(self.directory / "ledger")
        self.root_item_id = self.cp._root_item_id()
        try:
            # Reuse the existing root node/lease. Its bootstrap control identity
            # has made no model call; rollover binds the exact configured Lead
            # route and manifest in the existing two-phase session lifecycle.
            root_node = self.cp.proj.nodes[self.cp.root_lead_node]
            session, path, sha = self._manifest("lead", lead_model, self.root_item_id,
                root_node.context_epoch + 1, {"task": self.instance_id})
            self.cp.begin_rollover(root_node.node_id, str(path), sha,
                new_route=self._route(lead_model, human=True))
            node = self.cp.confirm_rollover(root_node.node_id, session_id=session)
            self.lead = self._handle("lead", lead_model, node, session, path, sha)
            self._agents[node.node_id] = {"handle": self.lead, "status": "active", "artifact": None}
            self._log("lead_registered", handle=asdict(self.lead))
            _write(self.directory / "manifest.json", {
                "run_id": self.run_id, "instance_id": self.instance_id,
                "root_id": self.cp.proj.root_id, "lead": asdict(self.lead),
                "models": list(MODELS), "max_workers": max_workers, "nested_worker": False,
                "level_policy": "equal Level.B admission labels; not AA or a capability ranking",
                "aa_scores": None, "cost_usd": None, "point_policy": "one lease point per agent",
                "official_swe_verdict": "external harness only; CP completion is not correctness",
            })
            self._checkpoint()
        except BaseException:
            self.close("Lead registration failed")
            raise

    def _route(self, model, *, human=False):
        return ModelRoute(_provider(model), model, reasoning_effort="max", level=Level.B,
                          source=RouteSource.ROUTE_HUMAN if human else RouteSource.ROUTE_LEAD,
                          point_weight=1)

    def _manifest(self, role, model, item_id, epoch, request):
        session = "swe-agent-" + uuid4().hex
        path = self.directory / "manifests" / (uuid4().hex + ".json")
        sha = _write(path, {"run_id": self.run_id, "root_id": self.cp.proj.root_id,
            "instance_id": self.instance_id, "role": role, "model": model,
            "provider": _provider(model), "item_id": item_id, "session_id": session,
            "context_epoch": epoch, "request": request})
        return session, path, sha

    def _handle(self, role, model, node, session, path, sha):
        return AgentHandle(self.run_id, self.cp.proj.root_id, self.instance_id, role,
            model, _provider(model), node.item_id, node.node_id,
            self.cp.proj.work_items[node.item_id].attempt, session, node.context_epoch,
            str(path), sha)

    def _log(self, event, **payload):
        self.ledger.append(event, {"run_id": self.run_id, "instance_id": self.instance_id, **payload})

    def _registered(self, handle):
        if self._closed:
            _error("CONTROL_CLOSED", "Run control is closed")
        record = self._agents.get(handle.node_id) if isinstance(handle, AgentHandle) else None
        if record is None or record["handle"] != handle:
            _error("HANDLE_MISMATCH", "Handle was not issued for this exact run/agent/context")
        return record

    def _check(self, handle, allowed=("active",)):
        record = self._registered(handle)
        if record["status"] not in allowed:
            _error("AGENT_STATE", "Agent is " + record["status"])
        node = self.cp.proj.nodes[handle.node_id]
        item = self.cp.proj.work_items[handle.item_id]
        if (node.item_id != handle.item_id or node.context_epoch != handle.context_epoch
                or item.attempt != handle.attempt or node.route.model != handle.model
                or node.route.provider != handle.provider or node.terminated
                or (record["status"] != "provisioning" and node.session_id != handle.session_id)):
            _error("FENCE_VIOLATION", "Persisted agent context no longer matches the handle")
        path = Path(handle.manifest_path)
        if (path.is_symlink() or not path.resolve().is_relative_to(self.directory)
                or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != handle.manifest_hash
                or node.package_ref != str(path) or node.package_hash != handle.manifest_hash):
            _error("MANIFEST_MISMATCH", "Agent manifest is missing or changed")
        return record

    def _lead(self, caller):
        self._check(caller)
        if caller != self.lead or caller.role != "lead":
            _error("CALLER_NOT_LEAD", "Only this run's Lead may delegate or select patches")

    def delegate(self, caller, requests):
        with self._lock:
            self._lead(caller)
            if isinstance(requests, dict):
                requests = [requests]
            if not isinstance(requests, list) or len(requests) != 1:
                _error("BATCH_NOT_SUPPORTED", "Use one request per delegate call; no partial batch admission")
            request = requests[0]
            if not isinstance(request, dict) or set(request) - {"model", "task", "title"}:
                _error("BAD_REQUEST", "Worker request accepts only model, task and optional title")
            if request.get("model") not in MODELS:
                _error("MODEL_UNAVAILABLE", "Worker model must be an exact experiment catalog entry")
            if not isinstance(request.get("task"), str) or not request["task"].strip():
                _error("BAD_REQUEST", "Worker task must be explicit nonempty text")
            if "title" in request and not isinstance(request["title"], str):
                _error("BAD_REQUEST", "Worker title must be text")
            request = clone(request)
            if self.cp.proj.open_worker_slots_used >= self.max_workers:
                _error("WORKER_LIMIT", "All worker slots are occupied, including submitted unreviewed work")
            item = self.cp.create_work_item(DelegationKind.DERIVE, parent_item=self.root_item_id)
            handle = None
            try:
                session, path, sha = self._manifest("worker", request["model"], item.item_id, 0, request)
                node = self.cp.begin_node(item.item_id, self._route(request["model"]),
                                          package_ref=str(path), package_hash=sha)
                handle = self._handle("worker", request["model"], node, session, path, sha)
                self._agents[node.node_id] = {"handle": handle, "status": "provisioning", "artifact": None}
                self._log("worker_reserved", caller=asdict(caller), handle=asdict(handle), request=request)
                self._checkpoint()
                return [handle]
            except BaseException:
                self.cp.terminate(item.item_id, reason="manual-stopped", summary="Worker provisioning failed")
                if handle is not None:
                    self._agents[handle.node_id]["status"] = "failed"
                self._log("worker_reservation_failed", item_id=item.item_id)
                self._checkpoint()
                raise

    def activate(self, handle):
        with self._lock:
            record = self._check(handle, ("provisioning",))
            try:
                self.cp.confirm_node(handle.node_id, session_id=handle.session_id,
                                     manifest_hash=handle.manifest_hash)
                record["status"] = "active"
                self._log("worker_activated", handle=asdict(handle))
                self._checkpoint()
                return handle
            except BaseException:
                self.fail(handle, "Worker activation failed")
                raise

    def record_call(self, handle, record):
        with self._lock:
            self._check(handle)
            if not isinstance(record, dict):
                _error("INVALID_CALL", "Call record must be an object")
            record = clone(record)
            call_id = identifier(record.get("call_id"), "call_id")
            for field, expected in (("model_requested", handle.model), ("role", handle.role),
                    ("run_id", self.run_id), ("task_id", self.instance_id), ("instance_id", self.instance_id)):
                if (field == "model_requested" or field in record) and record.get(field) != expected:
                    _error("CALL_IDENTITY", "Call identity mismatch: " + field)
            usage = RunBudget._usage(record)
            value = {"handle": asdict(handle), "record": record, "usage": usage}
            old = self._calls.get(call_id)
            if old:
                if old["handle"] != value["handle"] or old["record"] != record:
                    _error("CALL_CONFLICT", "Actual call ID already belongs to different evidence")
                return deepcopy(old)
            inp, cache = usage["input_tokens"], usage["cached_input_tokens"]
            mapping = {"input_exclusive": inp - cache if inp is not None and cache is not None else None,
                       "output": usage["output_tokens"], "cache_read": cache, "cache_write": None, "cost_usd": None}
            event = self.cp.record_token_usage(handle.node_id, mapping["input_exclusive"],
                mapping["output"], mapping["cache_read"], None, None)
            if record.get("stop_reason") is not None:
                self.cp.record_stop_reason(handle.node_id, str(record["stop_reason"]))
            value.update(cp_token_event_seq=event.seq, cp_mapping=mapping)
            self._calls[call_id] = value
            self._log("call_recorded", **value)
            self._checkpoint()
            return deepcopy(value)

    def _artifact(self, handle, artifact):
        if not isinstance(artifact, dict):
            _error("INVALID_ARTIFACT", "Artifact must be a JSON object")
        value = clone(artifact)
        patch = value.get("patch_path", value.get("path") if "patch_sha256" in value else None)
        descriptor = None
        if patch is not None:
            if not isinstance(patch, str):
                _error("INVALID_ARTIFACT", "Patch path must be text")
            path = Path(patch)
            if not path.is_absolute():
                path = self.run_dir / path
            if path.is_symlink() or not path.resolve().is_relative_to(self.run_dir) or not path.is_file():
                _error("INVALID_ARTIFACT", "Patch must be a regular file inside this run")
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if value.get("patch_sha256") != sha:
                _error("PATCH_HASH_MISMATCH", "Patch bytes do not match submitted digest")
            descriptor = {"path": str(path.resolve()), "sha256": sha}
        path = self.directory / "artifacts" / (handle.node_id + ".json")
        sha = _write(path, {"handle": asdict(handle), "artifact": value, "verified_patch": descriptor})
        return {"path": str(path), "sha256": sha, "verified_patch": descriptor}

    def _verify_artifact(self, artifact):
        for descriptor in (artifact, artifact.get("verified_patch")):
            if descriptor is None:
                continue
            path = Path(descriptor["path"])
            if (path.is_symlink() or not path.resolve().is_relative_to(self.run_dir) or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != descriptor["sha256"]):
                _error("ARTIFACT_CHANGED", "Submitted artifact/patch changed before Lead adoption")

    def submit(self, handle, artifact):
        with self._lock:
            record = self._check(handle)
            saved = self._artifact(handle, artifact)
            self.cp.submit(handle.item_id, handle.node_id, Path(saved["path"]).read_text(encoding="utf-8"),
                           context_epoch=handle.context_epoch, session_id=handle.session_id)
            record.update(status="submitted", artifact=saved)
            self._log("agent_submitted", handle=asdict(handle), artifact=saved)
            self._checkpoint()
            return deepcopy(saved)

    def _accept(self, handle, evidence, refs, via):
        self.cp.begin_finalize(handle.item_id)
        package_id = "swe-evidence-" + digest(evidence)[:24]
        self.cp.store_evidence_package(handle.item_id, package_id, canonical(evidence), source_refs=refs)
        self.cp.complete_accept(handle.item_id, package_id, evidence_ready=True,
            accepted_by={"via": via, "lead_node_id": self.lead.node_id, "official_resolved": None})

    def validate_decision(self, caller, worker, decision, reason, evidence=None):
        """Read-only preflight before the host applies a proposed patch.

        decide repeats this validation; failure after host application must stop
        that run, not be treated as an ordinary retryable model tool error.
        """
        with self._lock:
            self._lead(caller)
            record = self._check(worker, ("submitted",))
            if worker.role != "worker" or decision not in ("adopt", "discard"):
                _error("INVALID_DECISION", "Lead may adopt/discard a submitted worker only")
            identifier(reason, "reason")
            if evidence is not None and not isinstance(evidence, dict):
                _error("INVALID_DECISION", "Decision evidence must be an object")
            self._verify_artifact(record["artifact"])
            verified_patch = record["artifact"].get("verified_patch")
            if (evidence and "delta_sha256" in evidence and verified_patch is not None
                    and evidence["delta_sha256"] != verified_patch["sha256"]):
                _error("PATCH_HASH_MISMATCH", "Host patch selection differs from submitted worker bytes")
            return deepcopy({"caller": asdict(caller), "worker": asdict(worker), "decision": decision,
                     "reason": reason, "evidence": clone(evidence or {}), "artifact": record["artifact"]})

    def decide(self, caller, worker, decision, reason, evidence=None):
        with self._lock:
            value = self.validate_decision(caller, worker, decision, reason, evidence)
            record = self._agents[worker.node_id]
            if decision == "adopt":
                self._accept(worker, value, [record["artifact"]["path"]], "lead-worker-adoption")
                record["status"] = "adopted"
            else:
                self.cp.terminate(worker.item_id, reason="manual-stopped", summary="Lead discarded: " + reason)
                record["status"] = "discarded"
            self._decisions.append(value)
            self._log("worker_decided", **value)
            self._checkpoint()
            return deepcopy(value)

    def fail(self, handle, reason, evidence=None):
        with self._lock:
            record = self._registered(handle)
            identifier(reason, "reason")
            if evidence is not None and not isinstance(evidence, dict):
                _error("INVALID_ARTIFACT", "Failure evidence must be an object")
            if record["status"] == "failed":
                # activate() already settles a failed provisioning operation;
                # its host exception handler may report that same agent again.
                # Preserve the first evidence and do not publish a second stop.
                return deepcopy(record["failure"])
            if record["status"] in TERMINAL:
                _error("AGENT_STATE", "Agent already settled as " + record["status"])
            value = {"handle": asdict(handle), "reason": reason, "evidence": clone(evidence or {})}
            self.cp.terminate(handle.item_id, reason="manual-stopped", summary=reason)
            record["status"] = "failed"
            record["failure"] = value
            self._log("agent_failed", **value)
            self._checkpoint()
            return value

    def get_usage(self):
        def total(entries):
            result = {"calls": len(entries), "cost_usd": None, "known_subtotals": {}, "unknown_counts": {}}
            for field in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"):
                values = [entry["usage"][field] for entry in entries]
                unknown = sum(value is None for value in values)
                known = sum(value for value in values if value is not None)
                result[field] = None if unknown else known
                result["known_subtotals"][field], result["unknown_counts"][field] = known, unknown
            return result
        with self._lock:
            entries = list(self._calls.values())
            return {"total": total(entries), "by_node": {node: total([
                value for value in entries if value["handle"]["node_id"] == node]) for node in self._agents},
                "input_includes_cached": True, "reasoning_included_in_output": True}

    def _checkpoint(self):
        self.ledger.save_snapshot(self.snapshot())

    def snapshot(self):
        with self._lock:
            return {"run_id": self.run_id, "instance_id": self.instance_id,
                "cp": self.cp.snapshot(), "agents": {node: {**value, "handle": asdict(value["handle"])}
                for node, value in self._agents.items()}, "usage": self.get_usage(),
                "decisions": deepcopy(self._decisions), "finished": self._finished is not None,
                "nested_worker": False, "official_resolved": None}

    def _seal(self):
        phase = self.cp.proj.seal_phase.get("root", SealPhase.OPEN)
        if phase == SealPhase.OPEN:
            self.cp.begin_seal()
            phase = SealPhase.CUTOFF
        if phase == SealPhase.CUTOFF:
            self.cp.begin_settlement()
            phase = SealPhase.SETTLEMENT
        if phase == SealPhase.SETTLEMENT:
            self.cp.finish_seal()

    def _replay(self):
        projection = state.Projection()
        for event in self.cp.store.read_all():
            projection = invariants.check_event(projection, event)
        return True

    def finish(self, caller, artifact):
        with self._lock:
            if self._finished is not None:
                if caller != self.lead or self._finished["artifact_hash"] != digest(artifact):
                    _error("FINISH_CONFLICT", "Final Lead artifact is immutable")
                return deepcopy(self._finished)
            self._lead(caller)
            if any(value["handle"].role == "worker" and value["status"] not in TERMINAL
                   for value in self._agents.values()):
                _error("WORKERS_PENDING", "Lead must review or fail outstanding workers before finalizing")
            saved = self.submit(caller, artifact)
            self._verify_artifact(saved)
            self._accept(caller, {"final_artifact": saved, "worker_decisions": self._decisions},
                         [saved["path"]], "lead-final-artifact")
            self._agents[caller.node_id]["status"] = "finished"
            self._seal()
            self._finished = {"control_completed": True, "official_resolved": None,
                "artifact": saved, "artifact_hash": digest(artifact), "usage": self.get_usage(),
                "invariant_replay_passed": self._replay(), "cp": self.cp.snapshot()}
            self._log("root_finished", result=self._finished)
            self._checkpoint()
            return deepcopy(self._finished)

    def close(self, reason="Runtime closed before final Lead artifact"):
        with self._lock:
            if self._closed:
                return
            try:
                if self._finished is None:
                    # Reverse creation order drains workers before the root.
                    for item in reversed(list(self.cp.proj.work_items.values())):
                        if item.acceptance not in invariants.TERMINAL_ACCEPTANCE:
                            self.cp.terminate(item.item_id, reason="manual-stopped", summary=reason)
                    for record in self._agents.values():
                        if record["status"] not in TERMINAL:
                            record["status"] = "failed"
                    self._seal()
                    self._log("run_closed_without_final", reason=reason, invariant_replay_passed=self._replay())
                    self._checkpoint()
            finally:
                self.cp.close()
                self._closed = True


__all__ = ["SweControl", "AgentHandle", "MODELS"]
