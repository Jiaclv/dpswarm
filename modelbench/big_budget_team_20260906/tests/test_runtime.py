"""Real control plane and inherited generation loop; no network or Docker."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import threading

import pytest

from modelbench.big_budget_team_20260906 import contracts
from modelbench.big_budget_team_20260906.runtime import BigBudgetRun
from modelbench.minimal_value_20260905 import runner as legacy
from modelbench.minimal_value_20260905.budget import EpisodeBudget

PRODUCTION = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n"
REGRESSION = "diff --git a/test_f.py b/test_f.py\n--- a/test_f.py\n+++ b/test_f.py\n@@ -1 +1 @@\n-old\n+test\n"


def entry_for(condition):
    cell = next(row for row in contracts.load_cells() if row["condition_id"] == condition)
    instance = {"instance_id": cell["instance_id"], "repo": "pylint-dev/pylint", "base_commit": "a" * 40,
                "version": "1.0", "problem_statement": "Public fixture issue: distinguish two input sources."}
    return contracts.build_entry(cell, instance, {"public_fixture": "python -m pytest test_f.py -q"})


def action(name, **arguments):
    return {"id": name, "name": name, "arguments": arguments}


class Transport:
    def __init__(self, folder):
        self.records = []
        self.lock = threading.Lock()
        self.run = None

    def complete(self, model, messages, *, role, run_id, task_id, call_id, tools, **kwargs):
        assert role != "cm", "Small fixtures must not invoke CM"
        actor = "lead" if role == "lead" else re.search(r"Agent identity: (worker-[0-9]+);", messages[0]["content"]).group(1)
        calls = []
        if actor == "lead":
            if self.run.entry["arm"] in ("S", "D"):
                calls.append(action("bash", command="edit-production"))
            for worker in self.run.worker_specs:
                calls += [action("collect", worker_id=worker["worker_id"], wait_seconds=5),
                          action("review_worker", worker_id=worker["worker_id"], decision="adopt",
                                 reason="Inspected independent delta and public evidence")]
        else:
            spec = next(spec for spec in self.run.worker_specs if spec["worker_id"] == actor)
            calls.append(action("bash", command="edit-production" if spec["role"] == "implementation" else "edit-regression"))
        calls.append(action("finish", status="completed", summary="Offline fixture evidence"))
        for index, call in enumerate(calls):
            call["id"] += "-" + str(index)
        assistant = {"role": "assistant", "content": None, "tool_calls": [
            {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
            for call in calls]}
        record = {"call_id": call_id, "model_requested": model, "role": role, "run_id": run_id, "task_id": task_id,
                  "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cached_input_tokens": 30,
                  "reasoning_tokens": 5, "wall_seconds": 0.01, "error": None, "protocol_error": None,
                  "assistant_message": assistant, "action": {"kind": "tools", "calls": calls},
                  "transport_attempt_count": 1, "stop_reason": "fixture"}
        with self.lock:
            self.records.append({"actor": actor, "model": model, "max_tokens": kwargs["max_tokens"], "record": deepcopy(record)})
        return record


class Environment:
    def __init__(self, instance, run_dir, **kwargs):
        self.instance, self.run_dir = instance, Path(run_dir)
        self.actor = self.run_dir.parent.name if self.run_dir.parent.name.startswith("worker-") else "lead"
        self.delta, self.baseline = "", kwargs.get("baseline_patch", "")
        self.memory, self.cpus = kwargs.get("memory"), kwargs.get("cpus")
        self.started = self.closed = self.quiesced = False
        self.container_id = None

    def start(self):
        assert not self.started
        assert self.memory == "1g" and self.cpus == 2
        self.started = True
        self.container_id = "offline-" + self.actor
        return self

    def run(self, command, timeout):
        assert self.started and not self.closed and not self.quiesced
        self.delta += {"edit-production": PRODUCTION, "edit-regression": REGRESSION}.get(command, "")
        return {"exit_code": 0, "stdout": "offline", "stderr": ""}

    def observe_worktree(self):
        return {"nonempty_delta": bool(self.delta), "state_sha256": hashlib.sha256(self.delta.encode()).hexdigest(),
                "baseline_sha256": hashlib.sha256(b"").hexdigest(), "changed_files": {}, "measurement": "offline fixture"}

    def snapshot_patch(self, delta=False):
        return self.delta if delta else self.baseline + self.delta

    def quiesce(self):
        self.quiesced = True
        return {"quiesced": True, "container_id": self.actor, "maintenance_started": True}

    def export_patch(self, delta=False):
        assert self.quiesced and not self.closed
        return self.delta if delta else self.baseline + self.delta

    def check_frozen_patch(self, patch):
        assert self.quiesced and not self.closed
        return {"applicable": True, "patch_sha256": legacy.sha(patch), "baseline_commit": self.instance["base_commit"], "exit_code": 0}

    def fork(self, run_dir, *, baseline_patch):
        return Environment(self.instance, run_dir, baseline_patch=baseline_patch, memory=self.memory, cpus=self.cpus)

    def apply_patch(self, patch):
        assert not self.quiesced and not self.closed
        self.delta += patch
        return {"exit_code": 0}

    def close(self):
        self.closed = True
        return {"closed": True, "removed": True, "container_id": self.actor, "errors": []}

    def grade(self, *args, **kwargs):
        raise AssertionError("Generation must never invoke official grading")


@pytest.fixture
def create(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "RESOURCE_FAILURE", threading.Event())
    monkeypatch.setattr(legacy, "MODEL_SLOTS", threading.BoundedSemaphore(4))
    monkeypatch.setattr(legacy, "CONTAINER_SLOTS", threading.BoundedSemaphore(3))
    runs = []

    def make(condition="T11", **kwargs):
        entry = entry_for(condition)
        run = BigBudgetRun(tmp_path / str(len(runs)), entry, transport_factory=Transport,
                           environment_factory=Environment, **kwargs)
        run.transport.run = run
        runs.append(run)
        return run

    yield make
    for run in runs:
        run.cancel.set()
        for worker in run.workers.values():
            worker.cancel.set()
        run.pool.shutdown(wait=True)
        if not run.control._closed:
            run.control.close(reason="Offline test teardown")


@pytest.mark.parametrize("condition", contracts.CONDITIONS)
def test_real_control_and_inherited_generation_preserve_actual_identity(create, condition):
    run = create(condition)
    result = run.run()
    assert result["infrastructure_error"] is None
    assert result["outcome"]["status"] == "completed"
    assert result["score"] is None and result["grading_pending"] is True
    assert result["cleanup_confirmed"] and result["quiesced"]
    assert result["condition_id"] == result["strategy_id"] == condition
    assert result["arm"] == result["base_arm"] == run.entry["base_arm"]
    assert result["effective_limits"] == run.limits
    assert result["lead_model"] == run.control.lead.model == run.entry["lead_model"]
    observed = {record["actor"]: record["model"] for record in run.transport.records}
    assert observed == {"lead": run.lead_model, **{spec["worker_id"]: spec["model"] for spec in run.worker_specs}}
    assert all(worker.handle.model == worker.request["model"] for worker in run.workers.values())
    assert all(record["max_tokens"] == 32768 for record in run.transport.records)
    assert all(worker["review_decision"] == "adopt" for worker in result["workers"])
    assert json.loads((run.folder / "result.json").read_text(encoding="utf-8"))["condition_id"] == condition
    assert result["budget"]["total_tokens"] == 120 * (1 + run.expected_workers)


@pytest.mark.parametrize("condition", ["T00", "T10", "T01", "T11"])
def test_root_and_scope_propagate_mixed_factor_limits(create, condition):
    limits = entry_for(condition)["effective_limits"]
    values = {key: limits[key] for key in ("max_calls", "token_limit", "cm_call_allowance")}
    budget = EpisodeBudget(**values, deadline_seconds=limits["wall_seconds"], scopes={"solver": values})
    run = create(condition, budget=budget.scope("solver"), start_clock=budget.root.created_at, deadline=budget.deadline_at)
    assert run.budget.episode.root is budget.root
    assert run.deadline - run.start_clock == pytest.approx(7200)
    if limits["token_limit"] == 2400000:
        run.budget.reserve("above-old-token-cap", "lead", 700000)
        run.budget.complete("above-old-token-cap", {"call_id": "above-old-token-cap", "input_tokens": 0, "output_tokens": 0})
    else:
        with pytest.raises(legacy.LedgerError, match="TOKEN_BUDGET_EXHAUSTED"):
            run.budget.reserve("above-old-token-cap", "lead", 700000)
    already = run.budget.summary()["call_count"]
    for index in range(already, limits["max_calls"]):
        run.budget.reserve(f"work-{index}", "lead", 1)
    with pytest.raises(legacy.LedgerError, match="CALL_BUDGET_EXHAUSTED"):
        run.budget.reserve("one-extra-work", "lead", 1)
    for index in range(limits["cm_call_allowance"]):
        run.budget.reserve(f"cm-{index}", "cm", 1)
    with pytest.raises(legacy.LedgerError, match="CM_CALL_BUDGET_EXHAUSTED"):
        run.budget.reserve("one-extra-cm", "cm", 1)
    assert budget.summary()["call_count"] == limits["max_calls"] + limits["cm_call_allowance"]


def test_old_root_budget_cannot_silently_limit_high_scope(tmp_path):
    entry = entry_for("T11")
    budget = EpisodeBudget(scopes={"solver": {"max_calls": 160, "token_limit": 2400000, "cm_call_allowance": 64}},
                           deadline_seconds=7200)
    with pytest.raises(ValueError, match="root/scope budget"):
        BigBudgetRun(tmp_path, entry, transport_factory=lambda _: pytest.fail("No transport before validation"),
                     budget=budget.scope("solver"), start_clock=budget.root.created_at, deadline=budget.deadline_at)
    assert not (tmp_path / "results").exists()


def test_wrong_entry_and_wrong_deadline_have_no_side_effects(tmp_path):
    entry = entry_for("F-T")
    entry["worker_specs"][0]["model"] = "gpt-5.6-terra"
    with pytest.raises(ValueError, match="drift"):
        BigBudgetRun(tmp_path, entry, transport_factory=lambda _: pytest.fail("Must not initialize transport"))
    entry = entry_for("F-T")
    with pytest.raises(ValueError, match="wall_seconds"):
        BigBudgetRun(tmp_path, entry, start_clock=1, deadline=1801)
    with pytest.raises(ValueError, match="generation-only"):
        BigBudgetRun(tmp_path, entry, grade_enabled=True)
    assert not (tmp_path / "results").exists()


def test_old_validator_still_rejects_new_flash_route():
    with pytest.raises(ValueError, match="Lead model"):
        legacy.strategy_entry(entry_for("F-T"), legacy.LIMITS)


def test_no_admission_or_delivery_policy_was_overridden():
    for method in ("_call", "_maybe_compress_context", "loop", "_closing_call", "bootstrap_team", "start_workers", "run"):
        assert getattr(BigBudgetRun, method) is getattr(legacy.ValueRun, method)



def test_runtime_waiting_ceiling_is_separately_bound_without_mutating_entry_contract(create):
    from modelbench.big_budget_team_20260906.transport import read_transport_policy
    run = create('T11')
    original = deepcopy(run.entry)
    assert original['effective_limits']['call_timeout'] == 600
    assert run.limits['call_timeout'] == 960
    policy, expected_sha = read_transport_policy()
    assert run.transport_policy == policy and run.transport_policy_sha256 == expected_sha
    assert policy["protocol"] == "big_budget_transport_coding_stream_v4"
    assert policy["stream"] is True and policy["tool_stream"] is True
    result = run.run()
    assert run.entry == original
    assert result['effective_limits']['call_timeout'] == 960
    assert result['configuration_sha256'] == original['configuration_sha256']
    assert result['transport_policy_sha256'] == expected_sha
    assert result['runtime_configuration_sha256'] == contracts.digest({
        'configuration_sha256': original['configuration_sha256'],
        'transport_policy_sha256': expected_sha, 'effective_limits': run.limits})
    events = [json.loads(line) for line in (run.folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    started = next(event for event in events if event['event'] == 'run_started')
    assert started['limits']['call_timeout'] == 960
    assert started['transport_policy_sha256'] == expected_sha


def test_real_runtime_rejects_factory_that_disagrees_with_bound_read_timeout(tmp_path):
    from functools import partial
    from modelbench.big_budget_team_20260906.transport import CodingPlanTransport
    with pytest.raises(ValueError, match='bound transport policy'):
        BigBudgetRun(tmp_path, entry_for('T11'),
                     transport_factory=partial(CodingPlanTransport, glm_read_timeout_seconds=300))
