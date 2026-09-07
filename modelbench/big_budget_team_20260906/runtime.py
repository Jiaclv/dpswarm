"""New identity and budget admission; inherit A2 generation and delivery behavior."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import math
from pathlib import Path
import threading
import time

from modelbench.minimal_value_20260905 import runner as legacy
from .contracts import digest, validate_entry
from .transport import CodingPlanTransport, read_transport_policy


def _validate_budget(budget, limits, deadline):
    if budget is None:
        return
    budgets = [budget]
    episode = getattr(budget, "episode", None)
    if episode is not None:
        if getattr(budget, "scope_id", None) != "solver" or set(episode.scopes) != {"solver"}:
            raise ValueError("Big-budget generation requires a single solver scope")
        budgets.append(episode.root)
    for item in budgets:
        for key in ("max_calls", "token_limit", "cm_call_allowance"):
            if getattr(item, key, None) != limits[key]:
                raise ValueError("Supplied root/scope budget disagrees with entry: " + key)
        stamp = getattr(item, "deadline_at", None)
        if stamp is None or not math.isclose(stamp, deadline, rel_tol=0, abs_tol=1):
            raise ValueError("Supplied budget deadline disagrees with entry")
    if budget.summary()["call_count"] != 0:
        raise ValueError("A new planned episode cannot reuse an already admitted budget")


class BigBudgetRun(legacy.ValueRun):
    """Generation only, with actual model routes selected by the new frozen plan.

    The small constructor is independent because ValueRun's constructor enforces
    the old five-arm model catalog. Its execution methods are inherited unchanged.
    Run one episode per OS process: the inherited resource gates are process-wide.
    """

    def __init__(self, batch_dir, entry, *, transport_factory=CodingPlanTransport,
                 environment_factory=None, control_factory=legacy.SweControl,
                 budget=None, deadline=None, start_clock=None, grade_enabled=False):
        if grade_enabled is not False:
            raise ValueError("BigBudgetRun is generation-only; the controller owns grading")
        entry = validate_entry(entry)
        self.transport_policy, self.transport_policy_sha256 = read_transport_policy()
        effective = dict(entry["effective_limits"])
        # The protocol (including GLM streaming) is separately frozen and hashed.
        # The old loop and its drain use one waiting ceiling. Transport admission
        # then clips each model to its own frozen cap and remaining episode time.
        effective["call_timeout"] = max(self.transport_policy[key] for key in (
            "glm_total_timeout_seconds", "other_model_total_timeout_seconds", "cm_total_timeout_seconds"))
        self.runtime_configuration_sha256 = digest({
            "configuration_sha256": entry["configuration_sha256"],
            "transport_policy_sha256": self.transport_policy_sha256,
            "effective_limits": effective})
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
        if environment_factory is None:
            from modelbench.minimal_value_20260905.environment import ValueEnvironment
            environment_factory = ValueEnvironment
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
        if isinstance(self.transport, CodingPlanTransport) and (
                self.transport.transport_policy_sha256 != self.transport_policy_sha256
                or self.transport.glm_read_timeout_seconds != self.transport_policy["glm_read_timeout_seconds"]
                or self.transport.glm_cm_read_timeout_seconds != self.transport_policy["cm_read_timeout_seconds"]):
            raise ValueError("Runtime transport must use the bound transport policy")
        self.environment_factory = environment_factory
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
        self.memory = legacy.MemoryService(sink=self._memory_event) if self.limits["cm_team_memory"] else None
        self.pool = ThreadPoolExecutor(max_workers=max(1, self.expected_workers), thread_name_prefix="value-worker")
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
                   runtime_configuration_sha256=self.runtime_configuration_sha256)

    def persist(self, path, value):
        # Enrich the first terminal write, not a later non-atomic rewrite.
        if Path(path) == self.folder / "result.json" and isinstance(value, dict):
            value.update({key: self.entry[key] for key in (
                "condition_id", "base_arm", "rep", "phase", "block_id", "position", "priority",
                "token_factor", "call_bundle_factor", "candidate_container_slots",
                "configuration_sha256", "plan_sha256")})
            value["effective_limits"] = dict(self.limits)
            value["transport_policy"] = dict(self.transport_policy)
            value["transport_policy_sha256"] = self.transport_policy_sha256
            value["runtime_configuration_sha256"] = self.runtime_configuration_sha256
        return super().persist(path, value)
