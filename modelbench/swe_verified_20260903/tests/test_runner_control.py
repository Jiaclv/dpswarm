"""Runner/real-control integration using scripted transport and fake environments.

The grade method below is a terminal-order spy, not an SWE evaluator. These
tests establish no repository correctness or benchmark score.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import threading

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_verified_20260903 import runner
from modelbench.swe_verified_20260903.control import SweControl
from dpswarm.control import ControlPlaneError


PATCH = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n"


def action(name, **arguments):
    return {"id": name, "name": name, "arguments": arguments}


def done(status="completed"):
    return action("finish", status=status, summary="Offline fixture evidence only")


def delegation():
    return action("delegate", model="glm-5.3", task="Inspect and repair f.py", title="bounded fix")


def collect():
    return action("collect", worker_id="worker-1", wait_seconds=5)


def review(reason="Checked worker evidence", decision="adopt"):
    return action("review_worker", worker_id="worker-1", decision=decision, reason=reason)


@pytest.fixture
def make_run(tmp_path):
    created = []

    def create(lead_script, worker_script=(), *, condition="dpswarm", failure=None):
        references = {}

        class Transport:
            def __init__(self, folder):
                self.script = {"lead": list(lead_script), "worker": list(worker_script)}
                self.seen, self.records = [], []
                self.lock = threading.Lock()

            def complete(self, model, messages, *, role, run_id, task_id, call_id, **options):
                with self.lock:
                    assert self.script[role], "Unexpected extra call for " + role
                    response = self.script[role].pop(0)
                    self.seen.append({"model": model, "role": role, "messages": deepcopy(messages),
                                      "run_id": run_id, "task_id": task_id, "call_id": call_id})
                if callable(response):
                    response = response(references["run"])
                error = response.get("error") if isinstance(response, dict) else None
                calls = [] if error else deepcopy(response)
                # Tool IDs must be unique within a real batch, including repeats.
                for index, call in enumerate(calls):
                    call["id"] += "-" + str(index)
                assistant = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"], "arguments": json.dumps(call["arguments"])}} for call in calls]}
                record = {"call_id": call_id, "model_requested": model, "role": role,
                    "run_id": run_id, "task_id": task_id, "input_tokens": 100,
                    "output_tokens": 20, "total_tokens": 120, "cached_input_tokens": 30,
                    "reasoning_tokens": 5, "wall_seconds": 0.01, "error": error,
                    "assistant_message": assistant, "action": {"kind": "tools", "calls": calls},
                    "protocol_error": None, "stop_reason": "fixture"}
                with self.lock:
                    self.records.append(deepcopy(record))
                return record

        class Environment:
            instances = []
            grade_calls = 0

            def __init__(self, instance, run_dir, *, image=None, cpus=2, memory="3g"):
                self.instance, self.run_dir = instance, Path(run_dir)
                self.baseline, self.delta = "", ""
                self.started = self.closed = False
                self.applied = []
                self.instances.append(self)

            def start(self):
                self.started = True
                return self

            def run(self, command, timeout):
                assert self.started and not self.closed
                if command == "edit":
                    self.delta = PATCH
                return {"exit_code": 0, "stdout": "offline", "stderr": ""}

            def export_patch(self, delta=False):
                assert self.started
                return self.delta if delta else self.baseline + self.delta

            def fork(self, run_dir, *, baseline_patch):
                if failure == "fork":
                    raise OSError("fixture fork failed")
                child = Environment(self.instance, run_dir).start()
                child.baseline = baseline_patch
                return child

            def apply_patch(self, patch):
                assert self.started and not self.closed
                self.applied.append(patch)
                self.delta += patch
                return {"exit_code": 0}

            def close(self):
                is_lead = self is Environment.instances[0]
                if (failure == "lead_close" and is_lead) or (failure == "worker_close" and not is_lead):
                    raise OSError("fixture " + ("lead" if is_lead else "worker") + " close failed")
                self.closed = True

            def grade(self, patch, model_name):
                # No grader implementation, tests, or subprocess is invoked.
                assert all(env.closed for env in self.instances)
                assert references["run"].control._closed
                assert references["run"].control.cp.proj.active_points == 0
                Environment.grade_calls += 1
                return {"status": "offline_terminal_spy", "resolved": None, "test_only": True}

        def control_factory(*args, **kwargs):
            control = SweControl(*args, **kwargs)
            if failure == "activate":
                def failed_activation(*args, **kwargs):
                    raise ControlPlaneError("FIXTURE_ACTIVATION", "fixture activation failed")
                control.cp.confirm_node = failed_activation
            if failure == "decide":
                def failed_commit(*args, **kwargs):
                    raise OSError("fixture CP commit failed")
                control.decide = failed_commit
            return control

        instance = {"instance_id": "sympy__sympy-12345", "repo": "sympy/sympy",
                    "problem_statement": "Offline integration fixture; no real task answer"}
        entry = {"run_id": instance["instance_id"] + "__" + condition,
                 "condition": condition, "instance": instance}
        run = runner.SweRun(tmp_path, entry, transport_factory=Transport,
            environment_factory=Environment, control_factory=control_factory)
        references["run"] = run
        created.append(run)
        return run, Environment

    yield create
    for run in created:
        run.cancel.set()
        for child in run.workers.values():
            child.cancel.set()
        run.pool.shutdown(wait=True)
        run.control.close("offline fixture cleanup")


def assert_clean(run, result, expected_calls):
    assert result["infrastructure_error"] is None
    assert result["call_count"] == result["budget"]["completed_call_count"] == expected_calls
    assert result["budget"]["pending_call_count"] == 0
    assert result["cp_result"]["usage"]["total"]["calls"] == expected_calls
    assert result["cp_result"]["official_resolved"] is None
    assert result["score"]["resolved"] is None and result["score"]["test_only"] is True
    assert run.control.cp.proj.active_points == run.control.cp.proj.open_worker_slots_used == 0
    assert all(record["run_id"] == run.run_id for record in run.transport.records)
    assert run.control.run_id == run.folder.name == run.run_id


def test_solo_lead_record_and_final_multiline_patch_match_real_cp(make_run):
    run, env = make_run([[action("bash", command="edit"), done()]], condition="solo")
    result = run.run()
    assert_clean(run, result, 1)
    assert result["delegations"] == 0 and len(env.instances) == 1
    assert (run.folder / "model.patch").read_bytes() == PATCH.encode("utf-8")
    assert result["patch_sha256"] == hashlib.sha256(PATCH.encode()).hexdigest()
    assert [call["role"] for call in run.transport.seen] == ["lead"]
    assert env.grade_calls == 1


def test_optional_worker_is_adopted_by_lead_and_final_patch_bytes_stay_bound(make_run):
    run, env = make_run([[delegation(), collect(), review(), done()]],
                        [[action("bash", command="edit"), done()]])
    result = run.run()
    assert_clean(run, result, 2)
    assert result["delegations"] == 1 and result["workers"][0]["reviewed"]
    assert env.instances[0].applied == [PATCH]
    assert (run.folder / "worker-1/delta.patch").read_bytes() == PATCH.encode()
    assert (run.folder / "model.patch").read_bytes() == PATCH.encode()
    assert run.control._decisions[0]["decision"] == "adopt"
    assert run.control._decisions[0]["caller"]["node_id"] == run.control.lead.node_id
    assert run.workers["worker-1"].handle.attempt == 1
    assert set(call["role"] for call in run.transport.seen) == {"lead", "worker"}


@pytest.mark.parametrize("failure", ["fork", "activate"])
def test_worker_start_failure_is_reported_once_and_cleanup_remains_usable(make_run, failure):
    run, env = make_run([[delegation(), collect(), review(decision="discard"), done()]], failure=failure)
    result = run.run()
    assert_clean(run, result, 1)
    assert result["workers"][0]["status"] == "worker_error"
    assert result["workers"][0]["reviewed"]
    child = run.workers["worker-1"]
    assert run.control.cp.proj.work_items[child.handle.item_id].acceptance.value == "terminated"
    failures = [event for event in run.control.ledger.events_since() if event["kind"] == "agent_failed"]
    assert len(failures) == 1
    assert env.instances[0].applied == []


def test_empty_reason_is_rejected_before_apply_then_valid_review_can_continue(make_run):
    def corrected_review(run):
        assert run.lead_env.applied == []
        child = run.workers["worker-1"]
        assert run.control.cp.proj.work_items[child.handle.item_id].acceptance.value == "submitted"
        return [review(), done()]
    run, env = make_run([[delegation(), collect(), review(reason="")], corrected_review],
                        [[action("bash", command="edit"), done()]])
    result = run.run()
    assert_clean(run, result, 3)
    assert run.lead_env.applied == [PATCH]
    assert len(run.control._decisions) == 1


def test_cp_failure_after_apply_stops_run_and_never_invokes_grade_spy(make_run):
    run, env = make_run([[delegation(), collect(), review(), done()]],
                        [[action("bash", command="edit"), done()]], failure="decide")
    result = run.run()
    assert result["infrastructure_error"]["type"] == "FatalRuntimeError"
    assert "CP adoption failed" in result["infrastructure_error"]["message"]
    assert result["score"] is None and env.grade_calls == 0
    assert run.lead_env.applied == [PATCH]
    assert run.control.cp.proj.active_points == run.control.cp.proj.open_worker_slots_used == 0
    assert len(run.transport.seen) == 2


def test_failed_worker_patch_is_never_applied_and_lead_can_finish(make_run):
    run, env = make_run([[delegation(), collect(), review(), review(decision="discard"), done()]],
                        [[action("bash", command="edit"), done("blocked")]])
    result = run.run()
    assert_clean(run, result, 2)
    assert result["workers"][0]["status"] == "blocked"
    assert run.lead_env.applied == []
    assert (run.folder / "model.patch").read_bytes() == b""
    assert run.control._decisions == []


def test_lead_transport_failure_discards_unreviewed_delivery_without_adopting(make_run):
    run, env = make_run([[delegation(), collect()], {"error": "offline transport failure"}],
                        [[action("bash", command="edit"), done()]])
    result = run.run()
    assert_clean(run, result, 3)
    assert result["outcome"]["status"] == "transport_error"
    assert run.lead_env.applied == [] and (run.folder / "model.patch").read_bytes() == b""
    assert run.control._decisions[0]["decision"] == "discard"
    assert run.control._decisions[0]["evidence"]["automatic_cleanup"] is True
    assert env.grade_calls == 1  # Spy only: grading uses the actual frozen partial candidate.


def assert_cleanup_failure_is_terminal(run, env, result, resource_failure, slots, failed_role):
    """A failed physical cleanup must not become permission to grade or leak CP."""
    assert result["score"] is None and env.grade_calls == 0
    assert result["infrastructure_error"] is not None
    assert result["cleanup_errors"]
    assert "fixture " + failed_role + " close failed" in json.dumps(result["cleanup_errors"])
    assert resource_failure.is_set()
    assert run.control._closed
    assert run.control.cp.proj.active_points == run.control.cp.proj.open_worker_slots_used == 0
    assert json.loads((run.folder / "result.json").read_text(encoding="utf-8")) == result
    events = [json.loads(line) for line in (run.folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any("cleanup" in event["event"] and "close failed" in json.dumps(event) for event in events)
    # Logical slots are returned, but the global failure fence stays set because
    # the failed fake container deliberately still reports itself as unclosed.
    acquired = [slots.acquire(blocking=False) for _ in range(4)]
    assert all(acquired) and not slots.acquire(blocking=False)
    for _ in acquired:
        slots.release()


def test_lead_close_failure_still_closes_cp_persists_result_and_blocks_grade(make_run, monkeypatch):
    resource_failure, slots = threading.Event(), threading.BoundedSemaphore(4)
    monkeypatch.setattr(runner, "RESOURCE_FAILURE", resource_failure)
    monkeypatch.setattr(runner, "CONTAINER_SLOTS", slots)
    run, env = make_run([[action("bash", command="edit"), done()]], condition="solo", failure="lead_close")
    result = run.run()
    assert_cleanup_failure_is_terminal(run, env, result, resource_failure, slots, "lead")
    assert not env.instances[0].closed and len(run.transport.seen) == 1


def test_worker_close_failure_still_closes_cp_persists_result_and_blocks_grade(make_run, monkeypatch):
    resource_failure, slots = threading.Event(), threading.BoundedSemaphore(4)
    monkeypatch.setattr(runner, "RESOURCE_FAILURE", resource_failure)
    monkeypatch.setattr(runner, "CONTAINER_SLOTS", slots)
    run, env = make_run([[delegation(), collect(), review(decision="discard"), done()]],
                        [[action("bash", command="edit"), done()]], failure="worker_close")
    result = run.run()
    assert_cleanup_failure_is_terminal(run, env, result, resource_failure, slots, "worker")
    assert env.instances[0].closed and not env.instances[1].closed
    assert len(run.transport.seen) == 2
