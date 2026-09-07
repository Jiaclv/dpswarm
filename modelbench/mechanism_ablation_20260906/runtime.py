"""C1-only CM ablation and observational review evidence; historical runs stay frozen."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import math
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from modelbench.minimal_value_20260905 import runner as legacy
from modelbench.big_budget_team_20260906.runtime import BigBudgetRun, _validate_budget
from .contracts import digest, validate_entry
from .environment import AblationEnvironment
from .transport import AblationTransport, read_transport_policy


class AblationRun(BigBudgetRun):
    """Use the frozen C1 roster and budget with the inherited generation loop."""

    def __init__(self, batch_dir, entry, *, transport_factory=AblationTransport,
                 environment_factory=None, control_factory=legacy.SweControl,
                 budget=None, deadline=None, start_clock=None, grade_enabled=False):
        if grade_enabled is not False:
            raise ValueError("AblationRun is generation-only; the controller owns grading")
        entry = validate_entry(entry)
        self.transport_policy, self.transport_policy_sha256 = read_transport_policy()
        effective = dict(entry["effective_limits"])
        if any(effective[key] for key in ("cm_team_memory", "cm_scout_distill", "cm_bootstrap_package")):
            raise ValueError("C1 forbids alternate memory/summary channels")
        if not effective["cm_enabled"] and effective["cm_call_allowance"] != 0:
            raise ValueError("CM-off requires zero CM admission allowance")
        effective["call_timeout"] = max(self.transport_policy[key] for key in (
            "glm_total_timeout_seconds", "other_model_total_timeout_seconds", "cm_total_timeout_seconds"))
        self.runtime_configuration_sha256 = digest({
            "configuration_sha256": entry["configuration_sha256"],
            "transport_policy_sha256": self.transport_policy_sha256,
            "effective_limits": effective,
            "ablation_runtime_protocol": "c1-cm-review-pipefail-v1"})
        now = time.monotonic()
        self.start_clock = now if start_clock is None else start_clock
        if type(self.start_clock) not in (int, float) or not math.isfinite(self.start_clock):
            raise ValueError("start_clock must be a finite monotonic timestamp")
        self.deadline = self.start_clock + effective["wall_seconds"] if deadline is None else deadline
        if (type(self.deadline) not in (int, float) or not math.isfinite(self.deadline)
                or not math.isclose(self.deadline - self.start_clock, effective["wall_seconds"], rel_tol=0, abs_tol=1)
                or self.deadline <= now):
            raise ValueError("Generation deadline must match the saved wall_seconds")
        _validate_budget(budget, effective, self.deadline)
        self.grade_enabled = False
        self.lead_model = entry["lead_model"]
        self.worker_specs = entry["worker_specs"]
        self.expected_workers = len(self.worker_specs)
        self.batch_dir, self.entry = Path(batch_dir), entry
        self.run_id, self.instance = entry["run_id"], entry["instance"]
        self.limits = effective
        self.fixed_team_requested = self.expected_workers > 0
        self.worker_models = [spec["model"] for spec in self.worker_specs]
        self.bootstrap_admitted = False
        self.activation_source = "experiment_protocol" if self.fixed_team_requested else None
        self.folder = self.batch_dir / "results" / self.run_id
        if self.folder.exists():
            raise RuntimeError("Refusing to overwrite existing run: " + self.run_id)
        self.folder.mkdir(parents=True)
        self.transport = transport_factory(self.folder)
        if isinstance(self.transport, AblationTransport) and (
                self.transport.transport_policy_sha256 != self.transport_policy_sha256
                or self.transport.glm_read_timeout_seconds != self.transport_policy["glm_read_timeout_seconds"]
                or self.transport.glm_cm_read_timeout_seconds != self.transport_policy["cm_read_timeout_seconds"]):
            raise ValueError("Runtime transport must use the bound transport policy")
        self.environment_factory = environment_factory or AblationEnvironment
        self.control = control_factory(self.folder, self.instance["instance_id"],
                                       lead_model=self.lead_model, max_workers=max(1, self.expected_workers))
        self.budget = budget if budget is not None else legacy.RunBudget(
            self.limits["max_calls"], self.limits["token_limit"],
            max(0.000001, self.deadline - time.monotonic()),
            cm_call_allowance=self.limits["cm_call_allowance"], clock=time.monotonic)
        self.lock, self.cancel = threading.RLock(), threading.Event()
        self.draining = False
        self.workers, self.questions, self.calls = {}, {}, []
        self.cm_calls = []
        self.call_agents = {}
        self.team_scope = "team:" + self.run_id
        self.memory = None
        self.pool = ThreadPoolExecutor(max_workers=max(1, self.expected_workers), thread_name_prefix="c1-worker")
        self.lead_env = None
        self.started_at = legacy.utc()
        self.quiescence = {}
        self.environment_closures = {}
        self.protocol_errors = 0
        self.lead_edit_detected = False
        self.lead_first_edit_ordinal = None
        self.cleanup_errors = []
        self.host_errors = []
        self.journal_failed = False
        self.lead_worktree = {}
        self.observation_ordinals = {}
        self.event("run_started", entry=entry, limits=self.limits, root_handle=asdict(self.control.lead),
                   transport_policy=self.transport_policy, transport_policy_sha256=self.transport_policy_sha256,
                   runtime_configuration_sha256=self.runtime_configuration_sha256,
                   ablation_runtime_protocol="c1-cm-review-pipefail-v1")

    def _maybe_compress_context(self, handle, messages, worker):
        if not self.limits["cm_enabled"]:
            return
        return super()._maybe_compress_context(handle, messages, worker)

    def _cm_call(self, call_id, handle, prompt_messages, trigger):
        if not self.limits["cm_enabled"]:
            self.event("cm_disabled_call_rejected", call_id=call_id, trigger=trigger)
            raise legacy.LedgerError("CM_DISABLED", "C1 CM-off prohibits all summary calls")
        return super()._cm_call(call_id, handle, prompt_messages, trigger)

    def _cm_complete_fn(self, call_id, handle, trigger):
        complete = super()._cm_complete_fn(call_id, handle, trigger)

        def checked(route, prompt_messages):
            result = complete(route, prompt_messages)
            record = result.record
            reasons = []
            if record.get("error") or record.get("protocol_error"):
                reasons.append("response_error")
            if record.get("stop_reason") != "stop":
                reasons.append("response_not_complete")
            if record.get("usage_complete") is not True or any(
                    type(record.get(key)) is not int or record[key] < 0
                    for key in ("input_tokens", "output_tokens", "total_tokens")):
                reasons.append("usage_incomplete")
            if record.get("model_requested") != self.limits["cm_model"]:
                reasons.append("cm_model_mismatch")
            action = record.get("action") or {}
            if action.get("calls") or not isinstance(result.text, str) or not result.text.strip():
                reasons.append("not_a_text_summary")
            if reasons:
                # _cm_call already settled observed/unknown usage. Reject only
                # adoption; the inherited caller preserves the original history.
                self.event("cm_response_rejected", call_id=call_id, reasons=reasons,
                           model=record.get("model_requested"), stop_reason=record.get("stop_reason"),
                           usage_complete=record.get("usage_complete"))
                raise ValueError("CM response rejected: " + ",".join(reasons))
            return result

        return checked

    def _review_snapshot(self, env, folder, label):
        observed = {"observed_at": legacy.utc(), "quiesced": bool(getattr(env, "_quiesced", False))}
        try:
            patch = env.snapshot_patch()
            path = folder / (label + ".patch")
            self._write_review_patch(path, patch)
            observed.update(patch_path=str(path), patch_sha256=legacy.sha(patch),
                            patch_bytes=len(patch.encode("utf-8")), worktree=env.observe_worktree())
        except Exception as exc:
            observed["observation_error"] = type(exc).__name__ + ": " + str(exc)
        return observed

    @staticmethod
    def _write_review_patch(path, patch):
        # persist() is JSON-only. These are exact raw patches, never JSON strings.
        with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(patch)
            stream.flush()
            os.fsync(stream.fileno())

    def execute_tool(self, name, args, handle, env, worker):
        if name != "review_worker" or worker is not None:
            return super().execute_tool(name, args, handle, env, worker)
        folder = self.folder / "review_observations" / uuid4().hex
        folder.mkdir(parents=True)
        record = {"protocol": "c1-live-review-observation-v1", "run_id": self.run_id,
                  "worker_id": args.get("worker_id"), "decision_requested": args.get("decision"),
                  "reason": args.get("reason"),
                  "snapshot_semantics": "live_nonterminal_observations_not_quiesced_counterfactuals",
                  "pre": self._review_snapshot(env, folder, "lead_pre")}
        child = self.workers.get(args.get("worker_id"))
        if child is not None and child.future.done():
            try:
                delivery = child.future.result()
                delta = delivery.get("patch", "")
                self._write_review_patch(folder / "worker_delta.patch", delta)
                record["delivery"] = {"status": delivery.get("status"), "patch_sha256": legacy.sha(delta),
                                      "patch_path": str(folder / "worker_delta.patch"),
                                      "patch_bytes": len(delta.encode("utf-8"))}
            except Exception as exc:
                record["delivery_observation_error"] = type(exc).__name__ + ": " + str(exc)
        self.persist(folder / "before.json", record)
        try:
            result = super().execute_tool(name, args, handle, env, worker)
            record["result"] = result
            return result
        except Exception as exc:
            record["decision_error"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            record["post"] = self._review_snapshot(env, folder, "lead_post")
            self.persist(folder / "review.json", record)
            self.event("review_observed", worker_id=args.get("worker_id"), decision=args.get("decision"),
                       observation_path=str(folder / "review.json"),
                       pre_patch_sha256=record["pre"].get("patch_sha256"),
                       post_patch_sha256=record["post"].get("patch_sha256"),
                       delta_sha256=record.get("delivery", {}).get("patch_sha256"))
