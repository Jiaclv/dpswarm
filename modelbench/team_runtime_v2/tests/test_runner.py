"""Offline runner integration: real control/store, scripted I/O, no grader.

The fixture handoff is derived from the authorized public specification solely
to exercise routing. Neither these tests nor ``OfflineTeamRun`` establish a
TeamBench task pass or an oracle acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.team_runtime_v2 import runner
from dpswarm import invariants, state
from dpswarm.team_runtime.ledger import ExecutionStore


TASK = "SPEC5_config_system"
PUBLIC_TASK = REPO / "modelbench/team_eval20260903/instances" / TASK / "task"


def action(name, arguments, ident=None):
    return {"id": ident or name, "name": name, "arguments": deepcopy(arguments)}


def finish(ident="finish"):
    return action("finish_phase", {"status": "done", "summary": "Offline fixture finished",
                                  "artifacts": [], "unresolved": []}, ident)


def calls(*items):
    return {"calls": list(items)}


def good_handoff(contract):
    # Test data only. The production runner must receive these facts from P.
    return {"task_id": contract.task_id, "source_spec_sha256": contract.source_spec_sha256,
            "source_ref": "spec", "facts": contract.required_facts,
            "assumptions": [], "unresolved": []}


def planner_done(contract):
    return calls(action("submit_handoff", good_handoff(contract)), finish())


def verifier_done():
    return calls(action("write", {"path": "/shared/submission/attestation.json",
                                  "content": json.dumps({"verdict": "pass", "evidence": [
                                      "Offline control-flow fixture; no task validation"]})}), finish())


class ScriptedTransport:
    """Preserve actual caller history while returning a finite response script."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.records = []

    def complete(self, model, messages, *, tools, run_id, role, task_id):
        index = len(self.seen)
        invocation = {"model": model, "messages": deepcopy(messages), "tools": deepcopy(tools),
                      "run_id": run_id, "role": role, "task_id": task_id}
        self.seen.append(invocation)
        if index >= len(self.script):
            raise AssertionError("Unexpected extra model invocation: " + role)
        expected_role, response = self.script[index]
        assert role == expected_role
        if callable(response):
            response = response(invocation)
        if "assistant" in response:
            assistant = deepcopy(response["assistant"])
            text = assistant.get("content") or ""
        elif "calls" in response:
            if model.startswith("glm-"):
                assistant = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": item["id"], "type": "function", "function": {
                        "name": item["name"], "arguments": json.dumps(item["arguments"])}}
                    for item in response["calls"]]}
                text = ""
            else:
                text = json.dumps({"type": "tool_calls", "calls": response["calls"]})
                assistant = {"role": "assistant", "content": text}
        else:
            text = response["text"]
            assistant = {"role": "assistant", "content": text}
        record = {"call_id": f"fixture-call-{index + 1:03d}", "model_requested": model,
                  "role": role, "task_id": task_id, "run_id": run_id,
                  "assistant_message": assistant, "text": text, "error": None,
                  "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                  "cached_input_tokens": 30, "reasoning_tokens": 5,
                  "wall_seconds": 0.01, "stop_reason": "fixture"}
        self.records.append(deepcopy(record))
        return record


class FakeSandbox:
    def __init__(self, task_dir, run_dir, *, image):
        self.task_dir, self.run_dir, self.image = Path(task_dir), Path(run_dir), image
        self.actions = []
        self.started = self.closed = self.frozen = False

    def start(self):
        self.started = True

    def tool(self, role, name, args):
        self.actions.append({"role": role, "name": name, "arguments": deepcopy(args)})
        if name == "write":
            path = args["path"]
            assert path.startswith("/shared/")
            destination = (self.run_dir / path.removeprefix("/shared/")).resolve()
            assert destination.is_relative_to(self.run_dir.resolve())
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(args["content"], encoding="utf-8")
            return {"ok": True}
        if name == "run":
            return {"ok": True, "exit_code": 0, "stdout": "offline fixture", "stderr": ""}
        raise AssertionError("Unscripted sandbox action: " + name)

    def freeze(self):
        self.frozen = True

    def grade(self):
        raise AssertionError("These integration tests must never invoke a grader")

    def close(self):
        self.closed = True


class OfflineTeamRun(runner.TeamRun):
    def grade(self):
        self.scheduler.freeze()
        self.budget.freeze()
        self.data["finished"] = True
        self.checkpoint()
        value = self.result("offline_complete")
        value["test_only"] = "No grader run, no task pass, no oracle acceptance"
        return value


@pytest.fixture
def make_run(tmp_path):
    instance = tmp_path / "instances" / TASK
    (instance / "task").mkdir(parents=True)
    # Deliberately materialize public inputs only: no grader or expected.json.
    for name in ("spec.md", "brief.md"):
        (instance / "task" / name).write_text((PUBLIC_TASK / name).read_text(encoding="utf-8"),
                                              encoding="utf-8")
    (instance / "workspace").mkdir()
    (instance / "workspace" / "fixture.txt").write_text("untouched", encoding="utf-8")
    (instance / "submission").mkdir()
    contract = runner.build_contract(TASK, (instance / "task/spec.md").read_text(encoding="utf-8"))

    def create(script, *, executor="glm-5.3", name="fixture"):
        transport = ScriptedTransport(script)
        entry = {"task_id": TASK, "condition": "team", "executor": executor, "run_id": name}
        team = OfflineTeamRun(tmp_path, entry, {"image_id": "offline-image"},
                              transport=transport, sandbox_factory=FakeSandbox)
        return team, transport, contract

    create.contract = contract
    return create


def successful_result(team, transport, **run_options):
    result = team.run(**run_options)
    assert result["status"] == "offline_complete", result.get("error")
    assert len(transport.seen) == len(transport.script)
    assert isinstance(team.control, runner.ExperimentControl)
    assert isinstance(team.store, ExecutionStore)
    assert team.box.started and team.box.closed
    assert "score" not in result and "control" not in result
    assert result["call_count"] == len(transport.records)
    assert result["total_tokens"] == 120 * result["call_count"]
    assert result["budget"]["completed_call_count"] == result["call_count"]
    assert result["budget"]["pending_call_count"] == 0
    assert team.control.get_usage()["total"]["calls"] == result["call_count"]
    projection = state.Projection()
    for event in team.control.cp.store.read_all():
        projection = invariants.check_event(projection, event)
    # Closing an offline test without an oracle cannot accept any item.
    assert all(item.acceptance.value != "accepted" for item in projection.work_items.values())
    snapshot = ExecutionStore(team.folder / "execution").load_snapshot()
    assert snapshot["pending_events"] == []
    assert snapshot["state"]["inflight"] is None
    return result


def phase(result, name):
    return next(item for item in result["phases"] if item["phase"] == name)


def tool_results(team, name, turn):
    value = json.loads((team.folder / "phases" / name / f"turn_{turn:03d}.json").read_text())
    return value["tool_results"]


def test_incomplete_handoff_cannot_dispatch_executor(make_run):
    invalid = good_handoff(make_run.contract)
    del invalid["facts"]["config"]["debug_mode"]["env_var"]
    team, transport, _ = make_run([
        ("planner", calls(action("submit_handoff", invalid), finish())),
        ("planner", calls(finish())),
    ])
    result = successful_result(team, transport)
    assert [seen["role"] for seen in transport.seen] == ["planner", "planner"]
    assert result["terminal_reason"] == "HANDOFF_FAILED"
    assert result["handoff_validated"] is False
    assert not (team.folder / "handoff.json").exists()
    assert all(handle.role == "planner" for handle in team.control._handles.values())
    assert phase(result, "planner")["protocol_errors"] == 1
    assert tool_results(team, "planner", 2)[0]["result"]["error"] == "HANDOFF_REQUIRED"
    assert team.box.actions == []


def test_wrong_value_feedback_then_valid_package_reaches_executor(make_run):
    invalid = good_handoff(make_run.contract)
    invalid["facts"]["config"]["debug_mode"]["env_var"] = "WEB_DEBUG_MODE"
    expected = good_handoff(make_run.contract)

    def revised_planner(invocation):
        feedback = [json.loads(msg["content"]) for msg in invocation["messages"] if msg["role"] == "tool"]
        assert any(item.get("ok") is False and any("debug_mode.env_var" in e for e in item.get("errors", []))
                   for item in feedback)
        return planner_done(make_run.contract)

    def executor(invocation):
        prompt = next(msg["content"] for msg in invocation["messages"]
                      if msg["role"] == "user" and "Validated handoff:\n" in msg["content"])
        accepted = json.loads(prompt.split("Validated handoff:\n", 1)[1])
        assert accepted["handoff_validated"] is True
        assert accepted["facts"] == expected["facts"]
        assert accepted["source_spec_sha256"] == expected["source_spec_sha256"]
        assert make_run.contract.source_spec_sha256 == accepted["source_spec_sha256"]
        assert (PUBLIC_TASK / "spec.md").read_text(encoding="utf-8") not in prompt
        return calls(finish())

    team, transport, _ = make_run([
        ("planner", calls(action("submit_handoff", invalid), finish())),
        ("planner", revised_planner), ("executor", executor), ("verifier", verifier_done()),
    ])
    result = successful_result(team, transport)
    assert result["handoff_validated"] is True
    assert phase(result, "planner")["calls"] == 2
    first = tool_results(team, "planner", 1)
    assert first[0]["result"]["ok"] is False and first[1]["result"]["error"] == "HANDOFF_REQUIRED"


def test_native_plaintext_no_action_consumes_turn_without_finishing(make_run):
    def resumed_executor(invocation):
        assert any(msg["role"] == "assistant" and msg.get("content") == "DONE" for msg in invocation["messages"])
        assert any("NoAction" in (msg.get("content") or "") for msg in invocation["messages"])
        return calls(action("write", {"path": "/shared/workspace/after_no_action.txt", "content": "executed"}), finish())

    team, transport, _ = make_run([
        ("planner", planner_done(make_run.contract)), ("executor", {"text": "DONE"}),
        ("executor", resumed_executor), ("verifier", verifier_done()),
    ])
    result = successful_result(team, transport)
    executor = phase(result, "executor")
    assert executor["calls"] == 2 and executor["no_actions"] == 1
    assert executor["protocol_errors"] == 0 and executor["status"] == "finished"
    assert (team.folder / "workspace/after_no_action.txt").read_text() == "executed"


def test_native_bad_arguments_reject_whole_batch_and_pair_feedback_before_retry(make_run):
    assistant = {"role": "assistant", "content": "Applying the fixture", "reasoning_content": "fixture reasoning",
                 "tool_calls": [
                     {"id": "valid-but-not-executed", "type": "function", "function": {
                         "name": "write", "arguments": json.dumps({"path": "/shared/workspace/forbidden.txt", "content": "bad"})}},
                     {"id": "broken-arguments", "type": "function", "function": {
                         "name": "write", "arguments": '{"path":'}}]}

    def retry(invocation):
        history = invocation["messages"]
        offset = history.index(assistant)
        feedback = history[offset + 1:offset + 3]
        assert [item["tool_call_id"] for item in feedback] == ["valid-but-not-executed", "broken-arguments"]
        assert all(item["role"] == "tool" and json.loads(item["content"])["protocol_error"] == "invalid_arguments_json"
                   for item in feedback)
        return calls(action("write", {"path": "/shared/workspace/recovered.txt", "content": "ok"}, "recovered"), finish())

    team, transport, _ = make_run([
        ("planner", planner_done(make_run.contract)), ("executor", {"assistant": assistant}),
        ("executor", retry), ("verifier", verifier_done()),
    ])
    result = successful_result(team, transport)
    assert phase(result, "executor")["protocol_errors"] == 1
    assert phase(result, "executor")["calls"] == 2
    assert not (team.folder / "workspace/forbidden.txt").exists()
    assert (team.folder / "workspace/recovered.txt").read_text() == "ok"
    assert tool_results(team, "executor", 1) == []


QUESTION = {"request_id": "fixture-question", "question": "Confirm the public debug env mapping.",
            "missing_fields": ["config.debug_mode.env_var"]}
ANSWER = "The public specification maps debug_mode to WEB_DEBUG."


def clarification_done():
    return calls(action("reply_clarification", {"reply_to": QUESTION["request_id"], "answer": ANSWER}), finish())


def test_clarification_starts_new_planner_and_resumes_same_executor_exactly_once(make_run):
    captured = {}

    def request(invocation):
        captured["first_executor_history"] = deepcopy(invocation["messages"])
        captured["executor_handle"] = deepcopy(team.data["current"]["handle"])
        captured["original_planner_handle"] = deepcopy(phase({"phases": team.data["phases"]}, "planner")["handle"])
        return calls(action("request_clarification", QUESTION))

    def reply(invocation):
        captured["clarifier_handle"] = deepcopy(team.data["current"]["handle"])
        assert team.data["current"]["phase"] == "clarification"
        assert team.data["suspended"]["handle"] == captured["executor_handle"]
        assert team.data["suspended"]["calls"] == 1
        assert team.budget.summary()["call_count"] == 3  # Includes this reserved P call.
        public_spec = (PUBLIC_TASK / "spec.md").read_text(encoding="utf-8")
        assert any(public_spec in (msg.get("content") or "") for msg in invocation["messages"])
        names = [tool["function"]["name"] for tool in invocation["tools"]]
        assert "reply_clarification" in names and "submit_handoff" not in names
        return clarification_done()

    def continue_executor(invocation):
        assert team.data["current"]["handle"] == captured["executor_handle"]
        assert team.data["current"]["calls"] == 2
        before = captured["first_executor_history"]
        assert invocation["messages"][:len(before)] == before
        assert sum(ANSWER in (msg.get("content") or "") for msg in invocation["messages"]) == 1
        assert team.data["suspended"] is None
        # Retrying the same request is idempotent and must not start another P.
        return calls(action("request_clarification", QUESTION, "request-again"), finish())

    team, transport, _ = make_run([
        ("planner", planner_done(make_run.contract)), ("executor", request),
        ("planner", reply), ("executor", continue_executor), ("verifier", verifier_done()),
    ])
    result = successful_result(team, transport)
    assert [seen["role"] for seen in transport.seen] == ["planner", "executor", "planner", "executor", "verifier"]
    for key in ("item_id", "node_id", "session_id"):
        assert captured["clarifier_handle"][key] != captured["original_planner_handle"][key]
    assert len([handle for handle in team.control._handles.values() if handle.role == "executor"]) == 1
    assert phase(result, "executor")["calls"] == 2 and phase(result, "clarification")["calls"] == 1
    request_state = result["clarifications"]["requests"][QUESTION["request_id"]]
    assert request_state["state"] == "RESUMED" and request_state["resume_count"] == 1
    assert request_state["reply_call_count"] == 1
    assert tool_results(team, "executor", 2)[0]["result"] == {"ok": True, "already_resumed": True}


def test_clarification_does_not_reset_global_call_budget(make_run, monkeypatch):
    monkeypatch.setattr(runner, "MAX_CALLS", 3)
    team, transport, _ = make_run([
        ("planner", planner_done(make_run.contract)),
        ("executor", calls(action("request_clarification", QUESTION))),
        ("planner", clarification_done()),
    ])
    result = successful_result(team, transport)
    assert result["call_count"] == 3 and result["budget"]["remaining_calls"] == 0
    assert result["terminal_reason"] == "CALL_BUDGET_EXHAUSTED"
    assert phase(result, "executor")["calls"] == 1
    assert phase(result, "executor")["status"] == "call_budget_exhausted"
    assert result["clarifications"]["requests"][QUESTION["request_id"]]["resume_count"] == 1
    assert len(transport.seen) == 3


def test_pause_has_settled_checkpoint_without_extra_model_or_tool_calls(make_run):
    team, transport, _ = make_run([("planner", planner_done(make_run.contract))])
    try:
        result = team.run(pause_after_calls=1)
        assert result["status"] == "paused"
        assert result["call_count"] == 1 and len(transport.seen) == 1
        assert not team.box.closed
        assert not (team.folder / "result.json").exists()
        snapshot = ExecutionStore(team.folder / "execution").load_snapshot()
        saved = snapshot["state"]
        assert snapshot["pending_events"] == [] and saved["inflight"] is None
        assert "unprocessed_response" not in saved
        assert saved["stage"] == 1 and saved["current"] is None
        assert saved["handoff"]["handoff_validated"] is True
        assert len(saved["calls"]) == 1
        assert len(saved["budget"]["tickets"]) == 1
        assert team.box.actions == []
    finally:
        # run(pause) intentionally retains its sandbox and unsealed CP state.
        team.box.close()


def test_resume_after_executor_batch_before_settle_dispatches_planner_first(make_run, monkeypatch):
    from modelbench.team_runtime_v2 import recovery

    saved = {}

    def resumed_planner(invocation):
        assert restored.data["current"]["phase"] == "clarification"
        assert restored.data["suspended"]["handle"] == saved["executor_handle"]
        assert restored.data["suspended"]["calls"] == 1
        assert restored.budget.summary()["call_count"] == 3
        # Only the system message changes when original P becomes clarifier.
        previous = saved["planner_history"][1:]
        assert invocation["messages"][1:len(previous) + 1] == previous
        return clarification_done()

    def resumed_executor(invocation):
        assert restored.data["current"]["handle"] == saved["executor_handle"]
        assert restored.data["current"]["calls"] == 2
        assert invocation["messages"][:len(saved["executor_history"])] == saved["executor_history"]
        assert sum(ANSWER in (msg.get("content") or "") for msg in invocation["messages"]) == 1
        return calls(finish())

    team, transport, _ = make_run([
        ("planner", planner_done(make_run.contract)),
        ("executor", calls(action("write", {"path": "/shared/workspace/before_checkpoint.txt", "content": "once"}),
                           action("request_clarification", QUESTION))),
        ("planner", resumed_planner), ("executor", resumed_executor), ("verifier", verifier_done()),
    ])
    # Stop at the precise durable boundary: the whole E batch completed, but
    # settle() has not yet created the P clarification phase.
    team.setup()
    assert team.next_phase()
    team.turn()
    team.settle()
    assert team.next_phase()
    team.turn()
    current = team.data["current"]
    assert current["phase"] == "executor" and current["pending_question"] == QUESTION["request_id"]
    assert team.data["suspended"] is None and len(transport.seen) == 2
    saved.update(executor_handle=deepcopy(current["handle"]),
                 executor_history=deepcopy(team.history("executor")),
                 planner_history=deepcopy(team.history("planner")))
    snapshot = ExecutionStore(team.folder / "execution").load_snapshot()
    assert snapshot["pending_events"] == [] and snapshot["state"]["inflight"] is None
    assert "unprocessed_response" not in snapshot["state"]
    original_box = team.box
    team.control.cp.close()  # Release the journal writer without sealing it.

    def reattach(run_dir, image):
        assert run_dir == team.folder and image == "offline-image"
        return original_box

    monkeypatch.setattr(recovery, "reattach_sandbox", reattach)
    restored = OfflineTeamRun(team.root, team.entry, team.manifest, transport=transport,
                             sandbox_factory=FakeSandbox)
    result = successful_result(restored, transport, resume=True)
    assert isinstance(restored.control, recovery.RecoverableControl)
    assert [call["role"] for call in transport.seen] == ["planner", "executor", "planner", "executor", "verifier"]
    assert result["call_ids"] == [record["call_id"] for record in transport.records]
    assert len(set(result["call_ids"])) == 5
    assert result["budget"]["call_count"] == 5
    assert phase(result, "executor")["tool_calls"] == 3
    assert sum(item["arguments"].get("path") == "/shared/workspace/before_checkpoint.txt"
               for item in original_box.actions) == 1
    assert result["clarifications"]["requests"][QUESTION["request_id"]]["resume_count"] == 1


@pytest.mark.parametrize("ending", ["budget_exhausted", "blocked"])
def test_reply_without_finish_done_never_resumes_executor(make_run, ending):
    blocked = action("finish_phase", {"status": "blocked", "summary": "Cannot complete this reply",
                                      "artifacts": [], "unresolved": ["fixture remaining question"]})
    last_reply = {"text": "DONE"} if ending == "budget_exhausted" else calls(blocked)
    team, transport, _ = make_run([
        ("planner", planner_done(make_run.contract)),
        ("executor", calls(action("request_clarification", QUESTION))),
        ("planner", calls(action("reply_clarification", {"reply_to": QUESTION["request_id"], "answer": ANSWER}))),
        ("planner", last_reply), ("verifier", verifier_done()),
    ])
    result = successful_result(team, transport)
    assert [entry["role"] for entry in transport.seen].count("executor") == 1
    assert phase(result, "executor")["status"] == "clarification_failed"
    clarifier = phase(result, "clarification")
    assert clarifier["calls"] == 2
    assert clarifier["status"] == ("phase_budget_exhausted" if ending == "budget_exhausted" else "blocked")
    request = result["clarifications"]["requests"][QUESTION["request_id"]]
    assert request["reply"]["answer"] == ANSWER  # Reply exists, but cannot authorize continuation.
    assert request["state"] == "FAILED" and request["resume_count"] == 0
    assert request["failure_reason"] == "CLARIFIER_NOT_FINISHED"
    assert request["reply_call_count"] == 2
    assert all(ANSWER not in (item.get("content") or "") for item in team.history("executor"))


def test_terminal_checkpoint_resume_returns_saved_result_and_only_cleans_up(make_run, monkeypatch):
    from modelbench.team_runtime_v2 import recovery

    template, transport, _ = make_run([])

    def forbidden(*args, **kwargs):
        pytest.fail("Terminal recovery must not restore active control, call a model, or grade")

    # Exercise the production TeamRun entry point; even its grade method is a
    # tripwire. The final result below is synthetic fixture evidence only.
    monkeypatch.setattr(runner.TeamRun, "grade", forbidden)
    monkeypatch.setattr(recovery.RecoverableControl, "restore", classmethod(forbidden))
    monkeypatch.setattr(recovery, "reattach_sandbox", forbidden)
    monkeypatch.setattr(transport, "complete", forbidden)
    team = runner.TeamRun(template.root, template.entry, template.manifest, transport=transport,
                          sandbox_factory=forbidden, control_factory=forbidden)
    budget = runner.RunBudget(runner.MAX_CALLS, runner.TOKEN_LIMIT, runner.RUN_DEADLINE)
    budget.freeze()
    scheduler = runner.ClarificationScheduler(team.entry["run_id"])
    scheduler.freeze()
    final_result = {**team.entry, "status": "offline_terminal_fixture", "input_tokens": None,
                    "output_tokens": None, "total_tokens": None, "call_ids": [],
                    "test_only": "Synthetic terminal checkpoint; no grader or oracle was run",
                    "nested": {"preserve_null": None, "preserve_value": ["saved", 17]}}
    store = ExecutionStore(team.folder / "execution")
    store.save_snapshot({"entry": deepcopy(team.entry), "manifest_hash": team.manifest_hash(),
                         "budget": budget.snapshot(), "scheduler": scheduler.snapshot(),
                         "inflight": None, "finished": True, "final_result": deepcopy(final_result)})
    before_snapshot = deepcopy(store.load_snapshot())
    result_path = team.folder / "result.json"
    assert not result_path.exists()
    cleanups = []

    class CleanupOnlySandbox:
        close_count = 0

        def close(self):
            self.close_count += 1

    box = CleanupOnlySandbox()

    def cleanup_finished(run_dir, image):
        cleanups.append((run_dir, image))
        return box

    monkeypatch.setattr(recovery, "cleanup_finished_sandbox", cleanup_finished)
    actual = team.run(resume=True)
    assert actual == final_result
    assert json.loads(result_path.read_text(encoding="utf-8")) == final_result
    assert team.control is None and team.box is box
    assert cleanups == [(team.folder, "offline-image")] and box.close_count == 1
    assert transport.seen == [] and transport.records == []
    assert not (team.folder / "control-plane").exists()
    assert ExecutionStore(team.folder / "execution").load_snapshot() == before_snapshot
    # Once output reconstruction succeeded, a repeated resume returns the
    # existing result without repeating the cleanup hook or its close action.
    assert team.run(resume=True) == final_result
    assert len(cleanups) == 1 and box.close_count == 1
