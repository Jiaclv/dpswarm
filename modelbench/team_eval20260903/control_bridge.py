"""Experiment bridge to the real DPswarm ControlPlane, without core changes.

The external TeamBench loop owns ordering and containers. This exercises the
ControlPlane lifecycle, fencing, accounting and evidence acceptance; it does not
exercise dpswarm.orchestrator.Orchestrator or native provider sessions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any
from uuid import uuid4

if __package__:
    from .transports import MODELS, _redact
else:  # python run_experiment.py imports this module from its script directory.
    from transports import MODELS, _redact

_REPO = Path(__file__).resolve().parents[2]
_PLUGIN = _REPO / "dpswarm-plugin"
_FROZEN_SOURCE_COMMIT = "d185aef1916fd86a9ba554d581fd256319a973af"
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))
from dpswarm.control import ControlPlane, ControlPlaneError
from dpswarm import invariants, state
from dpswarm.types import (AcceptanceState, DelegationKind, Level, ModelCatalog,
                           ModelFacts, ModelRoute, RootExecutionSpec, RouteSource,
                           SealPhase)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, value: Any) -> str:
    content = _json(_redact(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _sha(content)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True)
class RoleHandle:
    role: str
    phase: str
    model: str
    item_id: str
    node_id: str
    session_id: str
    context_epoch: int
    manifest_path: str
    manifest_hash: str


class ExperimentControl:
    """One run's real CP journal plus rich, nullable per-role usage evidence.

    ``score`` in finish() should be the complete sandbox.grade() result, including
    raw_score_path, raw_score, grade_sha256, exit_code and timed_out. Role handles
    are logical external-loop sessions, not physical Codex CLI sessions.
    """

    def __init__(self, run_dir: Path, models_by_role: dict[str, str], task_id: str):
        if not models_by_role or any(model not in MODELS for model in models_by_role.values()):
            raise ValueError("models_by_role must contain only supported experiment models")
        self.run_dir = Path(run_dir).resolve()
        self.directory = self.run_dir / "control-plane"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.task_id = task_id
        self.models_by_role = dict(models_by_role)
        self._lock = threading.RLock()
        self._closed = False
        self._finished: dict[str, Any] | None = None
        self._handles: dict[str, RoleHandle] = {}
        self._calls: dict[str, dict[str, Any]] = {}
        self._submissions: dict[str, dict[str, str]] = {}
        self.catalog = ModelCatalog(point_policy_version="experiment-equal-one-point-unranked")
        for model in sorted(set(models_by_role.values())):
            # These levels are declared admission bookkeeping, not measured
            # capability ranks. No price or AA score is inferred or reported.
            self.catalog.register(ModelFacts(self._provider(model), model, Level.S,
                                             aa_source="experiment-declared-unranked",
                                             context_window=None,
                                             input_price_per_mtok=None,
                                             output_price_per_mtok=None))
        journal = self.directory / "events.jsonl"
        if journal.exists():
            raise ValueError("Use a fresh run_dir; existing CP journals must be replayed, not overwritten")
        spec = RootExecutionSpec(max_open_work_items=8, max_active_node_points=9,
                                 max_team_workers=8, max_attempts=1,
                                 deadline_seconds=14400, node_wallclock_timeout=7200,
                                 root_acceptance_mode="external-oracle-evidence",
                                 point_policy_version=self.catalog.point_policy_version)
        self.cp = ControlPlane(spec=spec, store_path=journal, catalog=self.catalog, root_level=Level.S)
        self.root_item_id = next(item.item_id for item in self.cp.proj.work_items.values()
                                 if item.kind == DelegationKind.ROOT)
        _write(self.directory / "manifest.json", {
            "task_id": task_id, "models_by_role": models_by_role,
            "root_id": self.cp.proj.root_id, "root_item_id": self.root_item_id,
            "root_control_node_id": self.cp.root_lead_node,
            "native_orchestrator_exercised": False,
            "control_scope": "real CP lifecycle, fence, accounting, evidence acceptance and replay",
            "role_session_kind": "external AgentLoop logical session; provider calls are separately logged",
            "level_policy": "all S for declared equal admission; no model ranking inferred",
            "point_policy": "one point per node; not monetary cost",
            "cost_usd": None,
            "dependencies": "role ordering is external; CP items are peers and remain unaccepted until final oracle",
            "accounting": "rich ledger input includes cache; CP input excludes known cache reads",
            "core_observation_warning": "Core ObservationSink defaults missing aggregate cost to zero; use rich ledger for unknowns",
        })
        self._snapshot("started")

    @staticmethod
    def _provider(model: str) -> str:
        return "glm-coding" if model.startswith("glm-") else "codex-chatgpt"

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ExperimentControl is closed")

    def _log(self, event: str, **data: Any) -> None:
        with (self.directory / "rich-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(_json(_redact({"event": event, "timestamp": datetime.now(timezone.utc).isoformat(), **data})) + "\n")

    def _snapshot(self, stage: str) -> dict[str, Any]:
        result = {"stage": stage, "task_id": self.task_id,
                  "native_orchestrator_exercised": False, "cp": self.cp.snapshot(),
                  "handles": {key: asdict(handle) for key, handle in self._handles.items()},
                  "usage": self.get_usage()}
        _write(self.directory / "snapshot.json", result)
        return result

    def start_role(self, role: str, phase: str) -> RoleHandle:
        with self._lock:
            self._ensure_open()
            if self._finished is not None:
                raise RuntimeError("Experiment is already finished")
            if role not in self.models_by_role:
                raise ValueError(f"Unknown experiment role: {role}")
            if not isinstance(phase, str) or not phase:
                raise ValueError("phase must be a nonempty string")
            model = self.models_by_role[role]
            item = self.cp.create_work_item(DelegationKind.DERIVE, parent_item=self.root_item_id)
            session_id = "external-role-" + uuid4().hex
            manifest_path = self.directory / "role-manifests" / (uuid4().hex + ".json")
            manifest_hash = _write(manifest_path, {
                "task_id": self.task_id, "root_id": self.cp.proj.root_id,
                "item_id": item.item_id, "role": role, "phase": phase,
                "model_requested": model, "effort_requested": "max",
                "service_tier_requested": "fast" if model.startswith("gpt-") else None,
                "session_id": session_id, "context_epoch": 0,
                "session_kind": "external AgentLoop logical role session",
                "runtime": "experiment-container-loop", "native_orchestrator_exercised": False,
            })
            try:
                node = self.cp.begin_node(item.item_id,
                    ModelRoute(self._provider(model), model, reasoning_effort="max", level=Level.S,
                               source=RouteSource.ROUTE_HUMAN, point_weight=1),
                    package_ref=str(manifest_path), package_hash=manifest_hash)
                if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_hash:
                    raise ValueError("Role manifest hash changed before activation")
                node = self.cp.confirm_node(node.node_id, session_id=session_id, manifest_hash=manifest_hash)
                handle = RoleHandle(role, phase, model, item.item_id, node.node_id,
                                    session_id, node.context_epoch, str(manifest_path), manifest_hash)
                self._handles[node.node_id] = handle
            except Exception:
                self.cp.terminate(item.item_id, reason="manual-stopped", summary="Role provisioning failed")
                raise
            self._log("role_started", handle=asdict(handle))
            self._snapshot("role_started")
            return handle

    def _check_handle(self, handle: RoleHandle) -> None:
        self._ensure_open()
        if self._handles.get(handle.node_id) != handle:
            raise ValueError("Handle is not registered with this experiment")
        node = self.cp.proj.nodes[handle.node_id]
        if node.session_id != handle.session_id or node.context_epoch != handle.context_epoch:
            raise ControlPlaneError("FENCE_VIOLATION", "Stale role session or context epoch")
        if node.terminated:
            raise ControlPlaneError("NODE_TERMINATED", "Role has already terminated")
        if hashlib.sha256(Path(handle.manifest_path).read_bytes()).hexdigest() != handle.manifest_hash:
            raise ControlPlaneError("MANIFEST_MISMATCH", "Role manifest has changed")

    def record_call(self, handle: RoleHandle, record: dict[str, Any]) -> None:
        with self._lock:
            self._check_handle(handle)
            call_id = record.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("Transport call must have a call_id")
            if record.get("model_requested") != handle.model or record.get("role") != handle.role:
                raise ValueError("Transport model/role does not match its control handle")
            if record.get("task_id") != self.task_id:
                raise ValueError("Transport task_id does not match its control run")
            safe = _redact(record)
            if call_id in self._calls:
                if self._calls[call_id]["record"] != safe:
                    raise ValueError("Conflicting duplicate call_id")
                return
            inp, cached, out = (_count(record.get(key)) for key in
                                ("input_tokens", "cached_input_tokens", "output_tokens"))
            if inp is not None and cached is not None and cached > inp:
                raise ValueError("Cached input exceeds inclusive input token count")
            exclusive_input = inp - cached if inp is not None and cached is not None else None
            token_event = self.cp.record_token_usage(handle.node_id,
                input_tokens=exclusive_input, output_tokens=out, cache_read_tokens=cached,
                cache_write_tokens=None, cost_usd=None)
            if record.get("stop_reason") is not None:
                self.cp.record_stop_reason(handle.node_id, str(record["stop_reason"]))
            entry = {"handle": asdict(handle), "record": safe,
                     "cp_token_event_seq": token_event.seq,
                     "cp_mapping": {"input_exclusive": exclusive_input, "output": out,
                                    "cache_read": cached, "cache_write": None, "cost_usd": None},
                     "unknown_fields_preserved": True}
            self._calls[call_id] = entry
            self._log("call_recorded", **entry)
            self._snapshot("call_recorded")

    def submit_role(self, handle: RoleHandle, artifact: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._check_handle(handle)
            if handle.item_id in self._submissions:
                raise ValueError("Role was already submitted; its evidence is immutable")
            if not isinstance(artifact, dict):
                raise ValueError("Role artifact must be an object")
            artifact_path = self.directory / "role-artifacts" / (handle.node_id + ".json")
            artifact_hash = _write(artifact_path, {"handle": asdict(handle), "artifact": artifact})
            output = artifact_path.read_text(encoding="utf-8")
            self.cp.submit(handle.item_id, handle.node_id, output=output,
                           context_epoch=handle.context_epoch, session_id=handle.session_id)
            self._submissions[handle.item_id] = {"path": str(artifact_path), "sha256": artifact_hash}
            self._log("role_submitted", handle=asdict(handle), artifact=self._submissions[handle.item_id],
                      acceptance=self.cp.proj.work_items[handle.item_id].acceptance.value)
            self._snapshot("role_submitted")
            return dict(self._submissions[handle.item_id])

    def get_usage(self) -> dict[str, Any]:
        """Sum every call, preserving unknown values and partial measured sums."""
        fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
        def aggregate(entries: list[dict[str, Any]]) -> dict[str, Any]:
            row: dict[str, Any] = {"calls": len(entries),
                "failed_calls": sum(entry["record"].get("error") is not None for entry in entries),
                "cost_usd": None, "known_sums": {}, "unknown_counts": {}}
            for field in fields:
                values = [_count(entry["record"].get(field)) for entry in entries]
                unknown = sum(value is None for value in values)
                known = sum(value for value in values if value is not None)
                row[field] = None if unknown else known
                row["known_sums"][field] = known
                row["unknown_counts"][field] = unknown
            row["wall_seconds"] = sum(entry["record"].get("wall_seconds") or 0 for entry in entries)
            return row
        entries = list(self._calls.values())
        return {"total": aggregate(entries),
                "by_role": {role: aggregate([e for e in entries if e["handle"]["role"] == role])
                            for role in self.models_by_role},
                "input_tokens_includes_cached": True,
                "cost_usd": None, "source": "rich-events.jsonl call_recorded; each call_id once"}

    def _oracle_evidence(self, score: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        grade = score if "raw_score" in score else result.get("grade", {})
        evidence: dict[str, Any] = {"verified": False, "pass": None, "reason": None}
        if not isinstance(grade, dict) or "raw_score" not in grade:
            evidence["reason"] = "Full sandbox grader result was not supplied"
            return evidence
        if grade.get("timed_out") is not False or grade.get("exit_code") != 0 or grade.get("score_parse_error"):
            evidence["reason"] = "Grader did not complete cleanly with parseable evidence"
            return evidence
        raw = grade.get("raw_score")
        if not isinstance(raw, dict) or not isinstance(raw.get("pass"), bool):
            evidence["reason"] = "Oracle score lacks a boolean pass verdict"
            return evidence
        path_value = grade.get("raw_score_path")
        if not isinstance(path_value, str):
            evidence["reason"] = "Oracle raw score path is missing"
            return evidence
        path = Path(path_value).resolve()
        if not path.is_relative_to(self.run_dir) or not path.is_file():
            evidence["reason"] = "Oracle raw score is not a readable artifact inside this run"
            return evidence
        payload = path.read_bytes()
        if json.loads(payload) != raw:
            evidence["reason"] = "Oracle raw score artifact does not match supplied score"
            return evidence
        script = self.run_dir / "task" / "grade.sh"
        if script.is_symlink() or not script.resolve().is_relative_to(self.run_dir) or not script.is_file():
            evidence["reason"] = "Materialized run grader script is unavailable"
            return evidence
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        if grade.get("grade_sha256") != digest:
            evidence["reason"] = "Grader hash does not match the run's materialized grade.sh"
            return evidence
        vendor = Path(__file__).resolve().parent / "TeamBench"
        blob = subprocess.run(["git", "-C", str(vendor), "show",
            _FROZEN_SOURCE_COMMIT + ":tasks/" + self.task_id + "/grade.sh"],
            capture_output=True, check=True, timeout=30).stdout
        canonical_hash = hashlib.sha256(blob).hexdigest()
        if digest != canonical_hash:
            evidence["reason"] = "Materialized grader hash does not match the frozen canonical Git blob"
            return evidence
        return {"verified": True, "pass": raw["pass"], "reason": None,
                "score_path": str(path), "score_sha256": hashlib.sha256(payload).hexdigest(),
                "grade_sha256": digest, "canonical_grade_sha256": canonical_hash,
                "source_commit": _FROZEN_SOURCE_COMMIT, "raw_score": raw}

    def _accept(self, item_id: str, content: dict[str, Any], source_refs: list[str]) -> None:
        self.cp.begin_finalize(item_id)
        text = _json(content)
        package_id = "experiment-evidence-" + _sha(text)[:24]
        self.cp.store_evidence_package(item_id, package_id, text, source_refs=source_refs)
        self.cp.complete_accept(item_id, package_id, evidence_ready=True,
            accepted_by={"via": "external-oracle-grader", "scope": "run-level evidence",
                         "individual_role_capability_verdict": False})

    def _stop_unaccepted(self, reason: str) -> None:
        terminal = invariants.TERMINAL_ACCEPTANCE
        for item in list(self.cp.proj.work_items.values()):
            if item.acceptance not in terminal:
                self.cp.terminate(item.item_id, reason="manual-stopped", summary=reason)

    def _seal(self) -> None:
        phase = self.cp.proj.seal_phase.get("root", SealPhase.OPEN)
        if phase == SealPhase.OPEN:
            self.cp.begin_seal()
            phase = SealPhase.CUTOFF
        if phase == SealPhase.CUTOFF:
            self.cp.begin_settlement()
            phase = SealPhase.SETTLEMENT
        if phase == SealPhase.SETTLEMENT:
            self.cp.finish_seal()

    def finish(self, score: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            if self._finished is not None:
                return self._finished
            score_path = self.directory / "oracle-input.json"
            result_path = self.directory / "run-result-input.json"
            _write(score_path, score)
            _write(result_path, result)
            try:
                oracle = self._oracle_evidence(score, result)
                role_evidence_complete = all(handle.item_id in self._submissions for handle in self._handles.values())
                for entry in self._submissions.values():
                    path = Path(entry["path"])
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                        role_evidence_complete = False
                accepted = oracle["verified"] and oracle["pass"] is True and role_evidence_complete
                decision = {"oracle": oracle, "task_pass": oracle["pass"],
                            "role_evidence_complete": role_evidence_complete,
                            "control_accepted": bool(accepted), "native_orchestrator_exercised": False}
                if accepted:
                    for handle in self._handles.values():
                        self._accept(handle.item_id, {"handle": asdict(handle),
                            "submission": self._submissions[handle.item_id], "decision": decision},
                            [self._submissions[handle.item_id]["path"], oracle["score_path"]])
                    root_node = self.cp.proj.nodes[self.cp.root_lead_node]
                    self.cp.submit(self.root_item_id, root_node.node_id,
                        output=_json({"decision": decision, "result_path": str(result_path), "usage": self.get_usage()}),
                        context_epoch=root_node.context_epoch, session_id=root_node.session_id)
                    self._accept(self.root_item_id, {"decision": decision, "result_path": str(result_path),
                                                  "usage": self.get_usage()},
                                 [str(score_path), str(result_path), oracle["score_path"]])
                else:
                    # A task-level failure does not identify which role was at
                    # fault. Terminate pending role evidence without inventing
                    # CP's mandatory per-role rejection attribution.
                    self._stop_unaccepted("Oracle did not authorize acceptance: " + _json(decision))
                self._seal()
                checked = state.Projection()
                for event in self.cp.store.read_all():
                    checked = invariants.check_event(checked, event)
                decision["invariant_replay_passed"] = True
                self._finished = {**decision, "usage": self.get_usage(), "cp": self.cp.snapshot(),
                                  "journal_path": str(self.directory / "events.jsonl")}
            except Exception as exc:
                self._log("finish_error", error={"type": type(exc).__name__, "message": str(exc)})
                self._stop_unaccepted("Control bridge finish failed; no acceptance claim")
                self._seal()
                self._finished = {"task_pass": None, "control_accepted": False,
                                  "native_orchestrator_exercised": False,
                                  "error": {"type": type(exc).__name__, "message": str(exc)},
                                  "usage": self.get_usage(), "cp": self.cp.snapshot()}
            self._log("finished", result=self._finished)
            _write(self.directory / "result.json", self._finished)
            self._snapshot("finished")
            return self._finished

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._finished is None:
                    self._stop_unaccepted("Experiment closed before final oracle acceptance")
                    self._seal()
                    self._snapshot("closed_without_oracle_acceptance")
                    self._log("closed_without_oracle_acceptance")
            finally:
                self.cp.close()
                self._closed = True


__all__ = ["ExperimentControl", "RoleHandle"]
