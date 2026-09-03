"""Offline request-level tests of the additive budget-visibility revision.

The original runner and its fixtures are reused without changes. All model and
sandbox responses are scripted; real ControlPlane and ExecutionStore still run.
No task grader, provider request, or oracle acceptance is exercised.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.team_runtime_v2.tests import test_runner as support
from modelbench.team_runtime_v2.revisions.budget_visibility import BudgetAwareTeamRun


# Import the established public-spec materialization fixture, not private data.
make_run = support.make_run


class OfflineBudgetTeamRun(BudgetAwareTeamRun):
    grade = support.OfflineTeamRun.grade


@pytest.fixture
def make_budget_run(make_run):
    def create(script):
        template, transport, contract = make_run(script)
        team = OfflineBudgetTeamRun(template.root, template.entry, template.manifest,
                                     transport=transport, sandbox_factory=support.FakeSandbox)
        return team, transport, contract

    create.contract = make_run.contract
    return create


def assert_notice(invocation, expected):
    notices = [message["content"] for message in invocation["messages"]
               if message["role"] == "user" and message.get("content", "").startswith("Runtime budget:")]
    assert notices, "Every actual request must carry a current runtime budget notice"
    notice = notices[-1]
    found = re.search(r"phase ([a-z]+), model call (\d+)/(\d+)\. "
                      r"After this call: (\d+) additional phase calls and (\d+) global calls remain\.", notice)
    assert found is not None, notice
    actual = (found[1], *(int(found[index]) for index in range(2, 6)))
    assert actual == expected
    assert "Errors and NoAction consume calls" in notice
    return notice


def test_every_request_sees_decreasing_budget_and_last_verifier_call_requires_delivery(make_budget_run):
    team, transport, _ = make_budget_run([
        ("planner", {"text": "Still reading"}),
        ("planner", support.planner_done(make_budget_run.contract)),
        ("executor", {"text": "DONE"}), ("executor", support.calls(support.finish())),
        ("verifier", {"text": "Checking"}), ("verifier", {"text": "Still checking"}),
        ("verifier", {"text": "DONE"}), ("verifier", support.verifier_done()),
    ])
    result = support.successful_result(team, transport)
    expected = [
        ("planner", 1, 2, 1, 19), ("planner", 2, 2, 0, 18),
        ("executor", 1, 6, 5, 17), ("executor", 2, 6, 4, 16),
        ("verifier", 1, 4, 3, 15), ("verifier", 2, 4, 2, 14),
        ("verifier", 3, 4, 1, 13), ("verifier", 4, 4, 0, 12),
    ]
    notices = [assert_notice(invocation, budget) for invocation, budget in zip(transport.seen, expected, strict=True)]
    for index in (1, 7):
        assert "LAST ALLOWED CALL" in notices[index]
        assert "handoff/attestation/artifacts" in notices[index]
        assert "finish_phase in this tool batch" in notices[index]
        assert "finish blocked" in notices[index]
        assert "do not claim unseen tool results passed" in notices[index]
    assert all("LAST ALLOWED CALL" not in notice for index, notice in enumerate(notices) if index not in (1, 7))
    assert support.phase(result, "verifier")["calls"] == 4
    assert support.phase(result, "verifier")["no_actions"] == 3
    assert (team.folder / "submission/attestation.json").is_file()


def test_executor_notice_counter_continues_after_planner_clarification(make_budget_run):
    saved = {}

    def question(invocation):
        saved["executor_handle"] = deepcopy(team.data["current"]["handle"])
        return support.calls(support.action("request_clarification", support.QUESTION))

    def continuation(invocation):
        assert team.data["current"]["handle"] == saved["executor_handle"]
        assert team.data["current"]["calls"] == 2
        return support.calls(support.finish())

    team, transport, _ = make_budget_run([
        ("planner", support.planner_done(make_budget_run.contract)), ("executor", question),
        ("planner", support.clarification_done()), ("executor", continuation),
        ("verifier", support.verifier_done()),
    ])
    result = support.successful_result(team, transport)
    expected = [
        ("planner", 1, 2, 1, 19), ("executor", 1, 6, 5, 18),
        ("clarification", 1, 2, 1, 17), ("executor", 2, 6, 4, 16),
        ("verifier", 1, 4, 3, 15),
    ]
    for invocation, budget in zip(transport.seen, expected, strict=True):
        assert_notice(invocation, budget)
    assert support.phase(result, "executor")["calls"] == 2
    request = result["clarifications"]["requests"][support.QUESTION["request_id"]]
    assert request["resume_count"] == 1 and request["reply_call_count"] == 1
    assert len([handle for handle in team.control._handles.values() if handle.role == "executor"]) == 1


def test_reserved_and_unknown_usage_tickets_reduce_visible_global_calls(make_budget_run):
    team, transport, _ = make_budget_run([("planner", {"text": "Continue reading"})])
    try:
        team.setup()
        # These are accounting fixtures, not model calls or recovery claims.
        # Both an unresolved reservation and completed usage-unknown call must
        # count, regardless of data['calls'] or completed_call_count.
        team.budget.reserve("pending-ticket", "oracle")
        team.budget.reserve("unknown-usage-ticket", "oracle")
        team.budget.complete("unknown-usage-ticket", {
            "call_id": "previous-usage-unknown", "role": "oracle",
            "input_tokens": None, "output_tokens": None, "total_tokens": None,
        })
        before = team.budget.summary()
        assert before["call_count"] == 2 and before["remaining_calls"] == 18
        assert before["pending_call_count"] == 1 and before["unknown_call_count"] == 1
        assert team.data["calls"] == []
        assert team.next_phase()
        team.turn()
        assert len(transport.seen) == 1
        assert_notice(transport.seen[0], ("planner", 1, 2, 1, 17))
        after = team.budget.summary()
        assert after["call_count"] == 3 and after["remaining_calls"] == 17
        assert after["pending_call_count"] == 1 and after["unknown_call_count"] == 1
        assert after["total_tokens"] is None
        assert len(team.data["calls"]) == 1
        assert team.data["current"]["calls"] == 1
    finally:
        if team.control is not None:
            team.control.close()
        if team.box is not None:
            team.box.close()


@pytest.mark.parametrize("path", ["/shared/workspace/config_system.py", "./config_system.py"])
def test_equivalent_deliverable_path_accepts_one_planner_call_and_preserves_raw_arguments(make_budget_run, path):
    payload = support.good_handoff(make_budget_run.contract)
    payload["facts"]["deliverables"] = [path]
    original = deepcopy(payload)
    # The generic finite contract stays strict; only the new representation
    # adapter is allowed to canonicalize the declared workspace path.
    assert make_budget_run.contract.validate(payload)["ok"] is False

    def executor(invocation):
        prompt = next(message["content"] for message in invocation["messages"]
                      if message["role"] == "user" and "Validated handoff:\n" in message["content"])
        package = json.loads(prompt.split("Validated handoff:\n", 1)[1])
        assert package["handoff_validated"] is True
        assert package["facts"] == make_budget_run.contract.required_facts
        assert package["facts"]["deliverables"] == ["config_system.py"]
        return support.calls(support.finish())

    team, transport, _ = make_budget_run([
        ("planner", support.calls(support.action("submit_handoff", payload), support.finish())),
        ("executor", executor), ("verifier", support.verifier_done()),
    ])
    result = support.successful_result(team, transport)
    assert result["handoff_validated"] is True
    assert support.phase(result, "planner")["calls"] == 1
    assert payload == original
    assert make_budget_run.contract.validate(payload)["ok"] is False
    tool = support.tool_results(team, "planner", 1)[0]
    assert tool["call"]["arguments"] == original
    assert tool["result"]["ok"] is True
    changes = [{"field": "facts.deliverables[0]", "from": path, "to": "config_system.py"}]
    assert tool["result"]["representation_normalizations"] == changes
    # Both response evidence and the durable tool request preserve what P
    # actually submitted; only accepted facts and the annotated result change.
    for record in (transport.records[0], team.data["calls"][0]):
        assert json.loads(record["text"])["calls"][0]["arguments"] == original
    operation = team.store.tool_state(transport.records[0]["call_id"] + ":submit_handoff")
    assert operation["request"]["call"]["arguments"] == original
    assert operation["result"]["representation_normalizations"] == changes
    persisted = json.loads((team.folder / "handoff.json").read_text(encoding="utf-8"))
    assert persisted["facts"]["deliverables"] == ["config_system.py"]


@pytest.mark.parametrize(("path", "extra"), [
    ("/shared/workspace/wrong.py", None),
    ("/other/config_system.py", None),
    ("../config_system.py", None),
    ("/shared/workspace/../config_system.py", None),
    (r"/shared/workspace\config_system.py", None),
    ("./config_system.py", "top"),
    ("./config_system.py", "facts"),
])
def test_path_adapter_does_not_accept_wrong_files_escape_paths_or_extra_fields(make_budget_run, path, extra):
    payload = support.good_handoff(make_budget_run.contract)
    payload["facts"]["deliverables"] = [path]
    if extra == "top":
        payload["extra_field"] = "not declared"
    elif extra == "facts":
        payload["facts"]["extra_field"] = "not declared"
    original = deepcopy(payload)
    team, transport, _ = make_budget_run([
        ("planner", support.calls(support.action("submit_handoff", payload), support.finish())),
        ("planner", support.calls(support.finish())),
    ])
    result = support.successful_result(team, transport)
    assert result["handoff_validated"] is False
    assert result["terminal_reason"] == "HANDOFF_FAILED"
    assert not (team.folder / "handoff.json").exists()
    assert all(handle.role == "planner" for handle in team.control._handles.values())
    assert [invocation["role"] for invocation in transport.seen] == ["planner", "planner"]
    assert payload == original
    assert json.loads(transport.records[0]["text"])["calls"][0]["arguments"] == original
    if extra is None:
        submitted = support.tool_results(team, "planner", 1)[0]
        assert submitted["result"]["ok"] is False
        assert any("facts.deliverables[0]" in error for error in submitted["result"]["errors"])
        assert submitted["call"]["arguments"] == original
    else:
        # Unknown fields are rejected by the strict tool schema before the
        # adapter executes; normalization cannot bypass that boundary.
        assert support.phase(result, "planner")["protocol_errors"] == 1
        assert support.tool_results(team, "planner", 1) == []
