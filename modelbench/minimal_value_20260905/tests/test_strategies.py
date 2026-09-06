"""A0 protocol tests with the real control plane, scripted transport and no Docker."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.minimal_value_20260905 import runner
from modelbench.minimal_value_20260905.contracts import arm_entry, worker_specs
from modelbench.minimal_value_20260905.budget import EpisodeBudget
from modelbench.minimal_value_20260905.environment import ValueEnvironment

PRODUCTION = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n"
REGRESSION = "diff --git a/test_f.py b/test_f.py\n--- a/test_f.py\n+++ b/test_f.py\n@@ -1 +1 @@\n-old\n+test\n"
INSTANCE = {"instance_id": "sympy__sympy-12345", "repo": "sympy/sympy",
            "base_commit": "a" * 40, "version": "1.0",
            "problem_statement": "Public issue: distinguish two different input sources."}
CHECKS = {"focused": "python -m pytest test_f.py -q"}


def action(name, **arguments):
    return {"id": name, "name": name, "arguments": arguments}


def done():
    return action("finish", status="completed", summary="Offline fixture evidence")


def settle(worker_id):
    return [action("collect", worker_id=worker_id, wait_seconds=5),
            action("review_worker", worker_id=worker_id, decision="adopt",
                   reason="Inspected independent delta and baseline evidence")]


@pytest.fixture
def make_run(tmp_path, monkeypatch):
    created = []
    monkeypatch.setattr(runner, "RESOURCE_FAILURE", threading.Event())
    monkeypatch.setattr(runner, "MODEL_SLOTS", threading.BoundedSemaphore(4))
    monkeypatch.setattr(runner, "CONTAINER_SLOTS", threading.BoundedSemaphore(3))

    def create(arm="T", *, lead=None, workers=None, unknown_actor=None,
               budget=None, deadline=None, start_clock=None, failure=None, fork_started=False):
        trace, references = [], {}
        specs = worker_specs("S" if arm == "R2-candidate" else arm)
        lead_calls = [action("bash", command="edit-production")] if arm in {"S", "L", "D", "R2-candidate"} else []
        for spec in specs:
            lead_calls += settle(spec["worker_id"])
        scripts = {"lead": list(lead if lead is not None else [lead_calls + [done()]])}
        for spec in specs:
            command = "edit-production" if spec["role"] == "implementation" else "edit-regression"
            scripts[spec["worker_id"]] = [[action("bash", command=command), done()]]
        scripts.update(workers or {})

        class Transport:
            def __init__(self, folder):
                self.seen, self.records, self.lock = [], [], threading.Lock()

            def complete(self, model, messages, *, role, run_id, task_id, call_id, tools, **kwargs):
                assert role != "cm", "Small scripted fixtures must not trigger CM"
                prompt = messages[0]["content"]
                actor = "lead" if role == "lead" else re.search(r"Agent identity: (worker-[0-9]+);", prompt).group(1)
                with self.lock:
                    assert len(references["run"].workers) == len(specs)
                    assert scripts[actor], "Unexpected model request for " + actor
                    response = scripts[actor].pop(0)
                    self.seen.append({"actor": actor, "model": model, "messages": deepcopy(messages),
                                      "tools": deepcopy(tools)})
                    trace.append(("model", actor))
                if callable(response):
                    response = response(references["run"])
                calls = deepcopy(response)
                for index, call in enumerate(calls):
                    call["id"] += "-" + str(index)
                assistant = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": json.dumps(call["arguments"])}} for call in calls]}
                unknown = actor == unknown_actor
                record = {"call_id": call_id, "model_requested": model, "role": role,
                          "run_id": run_id, "task_id": task_id,
                          "input_tokens": None if unknown else 100,
                          "output_tokens": None if unknown else 20,
                          "total_tokens": None if unknown else 120,
                          "cached_input_tokens": None if unknown else 30,
                          "reasoning_tokens": None if unknown else 5,
                          "wall_seconds": .01, "error": None, "protocol_error": None,
                          "assistant_message": assistant, "action": {"kind": "tools", "calls": calls},
                          "transport_attempt_count": 1, "stop_reason": "fixture"}
                with self.lock:
                    self.records.append(deepcopy(record))
                return record

        class Environment:
            instances = []
            grade_calls = 0

            def __init__(self, instance, run_dir, **kwargs):
                self.instance, self.run_dir = instance, Path(run_dir)
                self.actor = self.run_dir.parent.name if self.run_dir.parent.name.startswith("worker-") else "lead"
                self.delta, self.baseline = "", kwargs.get("baseline_patch", "")
                self.image, self.cpus, self.memory = kwargs.get("image"), kwargs.get("cpus", 2), kwargs.get("memory", "3g")
                self.command_timeout, self.grader_contract = kwargs.get("command_timeout", 120), kwargs.get("grader_contract")
                self.container_id = None
                self.started = self.closed = self.quiesced = False
                self.instances.append(self)

            def start(self):
                assert not self.started, "Runner double-started an already running fork"
                self.started = True
                self.container_id = "offline-" + self.actor
                trace.append(("start", self.actor))
                return self

            def run(self, command, timeout):
                assert self.started and not self.closed and not self.quiesced
                self.delta += {"edit-production": PRODUCTION, "edit-regression": REGRESSION}.get(command, "")
                trace.append(("bash", self.actor))
                return {"exit_code": 0, "stdout": "offline", "stderr": ""}

            def observe_worktree(self):
                assert self.started and self.container_id and not self.closed, "environment is not running"
                trace.append(("observe", self.actor))
                return {"nonempty_delta": bool(self.delta),
                        "state_sha256": hashlib.sha256(self.delta.encode()).hexdigest(),
                        "baseline_sha256": hashlib.sha256(b"").hexdigest(), "changed_files": {},
                        "measurement": "offline fixture"}

            def snapshot_patch(self, delta=False):
                assert self.started and not self.closed
                trace.append(("snapshot", self.actor))
                return self.delta if delta else self.baseline + self.delta

            def quiesce(self):
                if failure == "quiesce-" + self.actor:
                    return {"quiesced": False}
                assert self.started and not self.closed
                self.quiesced = True
                trace.append(("quiesce", self.actor))
                return {"quiesced": True, "container_id": self.actor, "maintenance_started": True}

            def export_patch(self, delta=False):
                assert self.quiesced and not self.closed
                trace.append(("export", self.actor))
                return self.delta if delta else self.baseline + self.delta

            def check_frozen_patch(self, patch):
                assert self.quiesced and not self.closed
                trace.append(("applicability", self.actor))
                return {"applicable": True, "patch_sha256": runner.sha(patch),
                        "baseline_commit": self.instance["base_commit"], "method": "offline fixture", "exit_code": 0}

            def fork(self, run_dir, *, baseline_patch):
                # Exercise the actual new adapter fork implementation without Docker.
                child = ValueEnvironment.fork(self, run_dir, baseline_patch=baseline_patch)
                assert child.container_id is None and not child.started
                return child.start() if fork_started else child

            def apply_patch(self, patch):
                assert not self.quiesced and not self.closed
                self.delta += patch
                trace.append(("apply", self.actor))
                return {"exit_code": 0}

            def close(self):
                self.closed = True
                trace.append(("close", self.actor))
                if failure == "close-" + self.actor:
                    return {"closed": False, "removed": False, "errors": ["offline removal failure"]}
                return {"closed": True, "removed": True, "container_id": self.actor, "errors": []}

            def grade(self, *args, **kwargs):
                Environment.grade_calls += 1
                raise AssertionError("ValueRun must not grade")

        entry = arm_entry("S" if arm == "R2-candidate" else arm, INSTANCE,
                          run_id="fixture-" + str(len(created)), public_checks=CHECKS)
        if arm == "R2-candidate":
            entry.update(arm=arm, strategy_id=arm)
        run = runner.ValueRun(tmp_path, entry, transport_factory=Transport, environment_factory=Environment,
                              budget=budget, deadline=deadline, start_clock=start_clock)
        references["run"] = run
        created.append(run)
        return run, Environment, trace

    yield create
    for run in created:
        run.cancel.set()
        for child in run.workers.values():
            child.cancel.set()
        run.pool.shutdown(wait=True)
        if not run.control._closed:
            run.control.close(reason="offline test teardown")


@pytest.mark.parametrize("arm,count,lead_model", [
    ("S", 0, "gpt-5.6-sol"), ("L", 0, "gpt-5.6-luna"),
    ("R2-candidate", 0, "gpt-5.6-sol"), ("D", 1, "gpt-5.6-sol"), ("T", 2, "gpt-5.6-sol")])
def test_topology_freeze_and_no_automatic_grade(make_run, arm, count, lead_model):
    run, env, trace = make_run(arm)
    result = run.generate()
    assert result["infrastructure_error"] is None
    assert result["outcome"]["status"] == "completed"
    assert result["expected_workers"] == result["bootstrap_admitted_workers"] == count
    assert result["workers_with_actual_calls"] == count
    assert result["lead_model"] == lead_model
    assert result["delegations"] == count
    assert result["score"] is None and result["grading_pending"]
    assert result["quiesced"] and result["cleanup_confirmed"]
    assert result["artifact"]["applicable"] is True
    assert result["artifact"]["sha256"] == hashlib.sha256(Path(result["patch_path"]).read_bytes()).hexdigest()
    assert env.grade_calls == 0
    assert all(e.closed for e in env.instances)
    assert run.control._closed and run.control.cp.proj.active_points == 0
    assert [w["worker_role"] for w in result["workers"]] == [s["role"] for s in run.worker_specs]
    for actor in ["lead"] + [s["worker_id"] for s in run.worker_specs]:
        assert trace.index(("quiesce", actor)) < trace.index(("export", actor)) < trace.index(("close", actor))
    if count:
        assert trace.index(("snapshot", "lead")) < min(i for i, item in enumerate(trace) if item[0] == "model")
        assert all(w["review_decision"] == "adopt" for w in result["workers"])


def test_q1_and_public_checks_equal_across_solver_arms(make_run):
    prompts = []
    for arm in ("S", "L", "D", "T"):
        run, _, _ = make_run(arm)
        result = run.run()
        assert result["infrastructure_error"] is None
        prompt = next(x["messages"][0]["content"] for x in run.transport.seen if x["actor"] == "lead")
        prompts.append(prompt)
        assert "counterexample" in prompt and "wrong implementation" in prompt
        assert "unchanged production baseline" in prompt and "exact commands" in prompt
        assert json.dumps(CHECKS, ensure_ascii=False) in prompt
        assert chr(92) + "n" not in prompt.split("Already admitted roster:")[0]
    common_prefix = [p.split("You are the sole solver")[0].split("The protocol admitted")[0] for p in prompts]
    assert all(INSTANCE["problem_statement"] in p for p in common_prefix)


def test_test_worker_blindness_enforced_in_tools_and_reply_dispatch(make_run):
    run, _, _ = make_run("T")
    result = run.run()
    assert result["infrastructure_error"] is None
    test_worker = run.workers["worker-2"]
    prod_worker = run.workers["worker-1"]
    names = lambda worker: {d["function"]["name"] for d in run.tool_declarations(worker)}
    assert names(test_worker) == {"bash", "finish"}
    assert "ask_lead" in names(prod_worker)
    with pytest.raises(ValueError, match="not available"):
        run.execute_tool("ask_lead", {"question": "Show production patch"}, test_worker.handle, None, test_worker)
    run.questions["injected"] = {"worker_id": test_worker.worker_id, "answer": None,
                                "deadline_clock": time.monotonic() + 5, "event": threading.Event()}
    with pytest.raises(ValueError, match="cannot receive"):
        run.execute_tool("reply_worker", {"question_id": "injected", "answer": "patch"},
                         run.control.lead, None, None)
    prompt = next(x["messages"][0]["content"] for x in run.transport.seen if x["actor"] == "worker-2")
    assert "no clarification channel" in prompt.lower()
    assert "never receive the production patch" in prompt
    assert PRODUCTION not in prompt
    assert json.dumps(CHECKS, ensure_ascii=False) in prompt


def test_duo_does_not_expose_reply_and_finish_requires_worker_review(make_run):
    run, _, _ = make_run("D", lead=[[done()] , settle("worker-1") + [done()]])
    result = run.run()
    assert result["infrastructure_error"] is None
    assert result["outcome"]["status"] == "completed"
    assert len([x for x in run.transport.seen if x["actor"] == "lead"]) == 2
    assert "reply_worker" not in {d["function"]["name"] for d in run.tool_declarations()}
    assert "WORKERS_UNSETTLED" in (run.folder / "lead" / "history.json").read_text()


def test_injected_scope_and_deadline_are_shared_not_reset(make_run, tmp_path):
    budget = EpisodeBudget(path=tmp_path / "root-budget.json", clock=time.monotonic)
    scope = budget.scope("candidate_1")
    start = time.monotonic() - 20
    deadline = time.monotonic() + 100
    run, _, _ = make_run("R2-candidate", budget=scope, start_clock=start, deadline=deadline)
    assert run.budget is scope and run.start_clock == start and run.deadline == deadline
    result = run.run()
    assert result["infrastructure_error"] is None
    assert budget.summary()["call_count"] == 1
    assert scope.summary()["call_count"] == 1
    assert not budget.summary()["frozen"]
    assert result["wall_seconds"] >= 20


def test_unknown_usage_retains_reservation(make_run):
    run, _, _ = make_run("S", unknown_actor="lead")
    result = run.run()
    assert result["budget"]["unknown_call_count"] == 1
    assert result["budget"]["total_tokens"] is None
    assert result["budget"]["reserved_tokens"] > 32768


@pytest.mark.parametrize("failure", ["quiesce-lead", "close-lead"])
def test_failed_freeze_or_cleanup_blocks_success(make_run, failure):
    run, env, _ = make_run("S", failure=failure)
    result = run.run()
    assert result["infrastructure_error"]
    assert env.grade_calls == 0
    if failure == "quiesce-lead":
        assert not result["quiesced"] and result["artifact"]["status"] == "missing"
    else:
        assert not result["cleanup_confirmed"] and runner.RESOURCE_FAILURE.is_set()


@pytest.mark.parametrize("change", [
    {"arm": "typo"}, {"expected_workers": 2}, {"lead_model": "gpt-5.6-terra"},
    {"worker_models": ["gpt-5.6-terra"]},
    {"limits_override": {"cm_edit_curfew": True}},
    {"limits_override": {"edit_status_banner": True}},
    {"limits_override": {"cm_team_memory": True}},
    {"limits_override": {"closing_call_reserve_exempt": False}},
    {"limits_override": {"container_concurrency": 4}},
])
def test_invalid_topology_or_frozen_switch_rejected_before_allocation(tmp_path, change):
    entry = arm_entry("D", INSTANCE, run_id="invalid", public_checks=CHECKS)
    entry.update(change)
    with pytest.raises(ValueError):
        runner.ValueRun(tmp_path, entry, environment_factory=lambda *args, **kwargs: None)
    assert not (tmp_path / "results" / "invalid").exists()


def test_grade_opt_in_is_rejected_before_allocation(tmp_path):
    entry = arm_entry("S", INSTANCE, run_id="invalid", public_checks=CHECKS)
    with pytest.raises(ValueError, match="generation-only"):
        runner.ValueRun(tmp_path, entry, grade_enabled=True)
    assert not (tmp_path / "results" / "invalid").exists()


def test_closing_call_is_charged_and_only_finishes(make_run):
    scripts = [[action("bash", command="inspect")] for _ in range(8)] + [
        [action("bash", command="edit-production"), done()]]
    run, _, _ = make_run("D", workers={"worker-1": scripts})
    result = run.run()
    assert result["infrastructure_error"] is None
    assert result["workers"][0]["status"] == "completed"
    worker_calls = [x for x in run.transport.seen if x["actor"] == "worker-1"]
    assert len(worker_calls) == 9
    assert {d["function"]["name"] for d in worker_calls[-1]["tools"]} == {"finish"}
    assert result["workers"][0]["delta_bytes"] == 0
    assert result["budget"]["call_count"] == 10


def test_expired_shared_deadline_admits_no_model_or_container(make_run):
    run, env, _ = make_run("S", deadline=time.monotonic() - 1)
    result = run.run()
    assert result["infrastructure_error"]
    assert not run.transport.seen and not env.instances
    assert result["artifact"]["status"] == "missing"
    assert result["budget"]["call_count"] == 0


@pytest.mark.parametrize("arm", ["D", "T"])
@pytest.mark.parametrize("fork_started", [False, True])
def test_worker_fork_starts_exactly_once_before_observation_and_model(make_run, arm, fork_started):
    run, environments, trace = make_run(arm, fork_started=fork_started)
    result = run.run()
    assert result["infrastructure_error"] is None
    assert result["outcome"]["status"] == "completed"
    assert result["workers_with_actual_calls"] == len(run.worker_specs)
    assert all(worker["status"] == "completed" for worker in result["workers"])
    assert result["quiesced"] and result["cleanup_confirmed"]
    for environment in environments.instances:
        actor = environment.actor
        assert trace.count(("start", actor)) == 1
        assert trace.index(("start", actor)) < trace.index(("observe", actor)) < trace.index(("model", actor))
