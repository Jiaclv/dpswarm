from copy import deepcopy

import pytest

from dpswarm.team_runtime.ledger import RunBudget
from dpswarm.team_runtime.scheduler import ClarificationError, ClarificationScheduler


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def ctx(role="executor", **changes):
    return {"run_id": "run", "item_id": role + "-item", "attempt": 0,
            "node_id": role + "-node", "session_id": role + "-session", "context_epoch": 0,
            "role": role, **changes}


def assert_code(code, operation):
    with pytest.raises(ClarificationError) as exc:
        operation()
    assert exc.value.code == code


def request(scheduler, request_id="q1", **changes):
    return scheduler.request(**{"source_context": ctx(), "target_role": "planner",
        "question": "What is the interval boundary?", "missing_fields": ["interval_boundary"],
        "request_id": request_id, "contract_revision": 2, **changes})


def replying(scheduler):
    request(scheduler)
    scheduler.admit("q1", ctx("planner"))
    return scheduler.mark_reply_started("q1", "reply-call-1")


def reply(scheduler, **changes):
    return scheduler.reply(**{"reply_to": "q1", "responder_context": ctx("planner"),
        "current_source_context": ctx(), "answer": "The interval is closed on the left.",
        "patch": {"interval_boundary": "left_closed"}, "contract_revision": 2, **changes})


def test_question_admission_answer_resume_keeps_executor_call_count():
    clock = Clock()
    scheduler = ClarificationScheduler("run", clock=clock)
    budget = RunBudget(max_calls=3, token_limit=100, clock=clock)
    budget.reserve("e1", "executor", 30)
    budget.complete("e1", {"call_id": "actual-e1", "total_tokens": 10})
    assert request(scheduler)["state"] == "E_WAITING_REPLY"
    assert scheduler.admit("q1", ctx("planner"))["state"] == "ADMITTED"
    budget.reserve("p1", "planner", 30)
    assert scheduler.mark_reply_started("q1", "p1")["state"] == "P_REPLYING"
    budget.complete("p1", {"call_id": "actual-p1", "total_tokens": 10})
    assert reply(scheduler)["state"] == "REPLY_READY"
    assert scheduler.resume("q1", ctx())["state"] == "RESUMED"
    assert budget.summary()["call_count"] == 2
    assert budget.summary()["remaining_calls"] == 1
    assert budget.tickets["e1"]["call_id"] == "actual-e1"
    budget.reserve("e2", "executor", 30)
    assert budget.summary()["call_count"] == 3


def test_exact_duplicate_requests_and_replies_are_idempotent():
    scheduler = ClarificationScheduler("run")
    asked = request(scheduler)
    assert request(scheduler) == asked
    admitted = scheduler.admit("q1", ctx("planner"))
    assert scheduler.admit("q1", ctx("planner")) == admitted
    started = scheduler.mark_reply_started("q1", "p1")
    assert scheduler.mark_reply_started("q1", "p1") == started
    assert started["reply_call_count"] == 1
    answered = reply(scheduler)
    assert reply(scheduler) == answered
    scheduler.resume("q1", ctx())
    assert_code("ALREADY_RESUMED", lambda: scheduler.resume("q1", ctx()))


def test_changed_duplicate_payload_is_rejected():
    scheduler = ClarificationScheduler("run")
    replying(scheduler)
    assert_code("REQUEST_CONFLICT", lambda: request(scheduler, question="Different question"))
    assert_code("ADMISSION_CONFLICT", lambda: scheduler.admit("q1", ctx("planner", session_id="new")))
    reply(scheduler)
    assert_code("REPLY_CONFLICT", lambda: reply(scheduler, answer="Changed answer"))


def test_finishing_call_after_reply_keeps_immutable_answer_and_uses_same_cap():
    scheduler = ClarificationScheduler("run", max_reply_calls=2)
    replying(scheduler)
    answered = reply(scheduler)
    finishing = scheduler.mark_reply_started("q1", "finish-call")
    assert finishing["state"] == "REPLY_READY"
    assert finishing["reply"] == answered["reply"]
    assert finishing["reply_call_count"] == 2
    assert scheduler.mark_reply_started("q1", "finish-call") == finishing
    assert_code("REPLY_CONFLICT", lambda: reply(scheduler, answer="Replacement answer"))
    assert scheduler.resume("q1", ctx())["state"] == "RESUMED"


@pytest.mark.parametrize("field,value", [("run_id", "another-run"), ("item_id", "another-item"),
    ("attempt", 1), ("node_id", "another-node"), ("session_id", "another-session"),
    ("context_epoch", 1), ("role", "verifier")])
def test_changed_requesting_context_cannot_accept_or_resume_reply(field, value):
    scheduler = ClarificationScheduler("run")
    replying(scheduler)
    stale = ctx(**{field: value})
    assert_code("STALE_SOURCE", lambda: reply(scheduler, current_source_context=stale))
    assert scheduler.get("q1")["state"] == "P_REPLYING"
    reply(scheduler)
    assert_code("STALE_SOURCE", lambda: scheduler.resume("q1", stale))
    assert scheduler.get("q1")["resume_count"] == 0


@pytest.mark.parametrize("field,value", [("run_id", "another-run"), ("item_id", "another-item"),
    ("attempt", 1), ("node_id", "another-node"), ("session_id", "another-session"),
    ("context_epoch", 1), ("role", "verifier")])
def test_responder_must_match_admitted_fence(field, value):
    scheduler = ClarificationScheduler("run")
    replying(scheduler)
    wrong = ctx("planner")
    wrong[field] = value
    assert_code("WRONG_RESPONDER", lambda: reply(scheduler, responder_context=wrong))


def test_wrong_role_run_revision_and_unstarted_replies_are_rejected():
    scheduler = ClarificationScheduler("run")
    assert_code("RUN_MISMATCH", lambda: request(scheduler, source_context=ctx(run_id="other")))
    request(scheduler)
    assert_code("WRONG_TARGET", lambda: scheduler.admit("q1", ctx("verifier")))
    assert_code("WRONG_TARGET", lambda: scheduler.admit("q1", ctx("planner", run_id="other")))
    scheduler.admit("q1", ctx("planner"))
    assert_code("NOT_REPLYING", lambda: reply(scheduler))
    scheduler.mark_reply_started("q1", "p1")
    assert_code("CONTRACT_MISMATCH", lambda: reply(scheduler, contract_revision=3))
    assert_code("UNKNOWN_REQUEST", lambda: reply(scheduler, reply_to="q-other"))


def test_request_and_reply_call_caps_bound_cycles():
    scheduler = ClarificationScheduler("run", max_requests=1, max_reply_calls=2)
    replying(scheduler)
    scheduler.mark_reply_started("q1", "p2")
    assert_code("REPLY_BUDGET_EXHAUSTED", lambda: scheduler.mark_reply_started("q1", "p3"))
    assert scheduler.get("q1")["state"] == "FAILED"
    assert scheduler.get("q1")["failure_reason"] == "reply_call_budget_exhausted"
    assert scheduler.get("q1")["reply_call_count"] == 2
    assert_code("REQUEST_BUDGET_EXHAUSTED", lambda: request(scheduler, request_id="q2"))


def test_no_capacity_is_an_explicit_durable_failure():
    scheduler = ClarificationScheduler("run")
    request(scheduler)
    failed = scheduler.fail("q1", "no_capacity")
    assert scheduler.fail("q1", "no_capacity") == failed
    assert_code("FAILURE_CONFLICT", lambda: scheduler.fail("q1", "other"))
    restored = ClarificationScheduler.from_snapshot(scheduler.snapshot())
    assert restored.get("q1")["failure_reason"] == "no_capacity"
    assert_code("REQUEST_TERMINAL", lambda: restored.admit("q1", ctx("planner")))


def test_same_executor_cannot_queue_parallel_questions():
    scheduler = ClarificationScheduler("run", max_requests=2)
    request(scheduler)
    assert_code("SOURCE_ALREADY_WAITING", lambda: request(scheduler, request_id="q2"))
    assert_code("SELF_REQUEST", lambda: request(scheduler, request_id="q2", target_role="executor"))


def test_reply_ticket_cannot_be_reused_across_requests():
    scheduler = ClarificationScheduler("run", max_requests=2)
    replying(scheduler)
    request(scheduler, request_id="q2", source_context=ctx(node_id="other-executor"))
    scheduler.admit("q2", ctx("planner", node_id="other-planner"))
    assert_code("TICKET_REUSED", lambda: scheduler.mark_reply_started("q2", "reply-call-1"))


def test_deadline_is_not_reset_by_snapshot_restore():
    clock = Clock()
    scheduler = ClarificationScheduler("run", deadline_seconds=3, clock=clock)
    asked = request(scheduler)
    clock.now += 3
    restored = ClarificationScheduler.from_snapshot(scheduler.snapshot(), clock=clock)
    assert restored.get("q1")["expires_at"] == asked["expires_at"]
    assert_code("REQUEST_EXPIRED", lambda: restored.admit("q1", ctx("planner")))
    assert restored.get("q1")["failure_reason"] == "expired"


def test_reply_ready_can_expire_before_executor_resumes():
    clock = Clock()
    scheduler = ClarificationScheduler("run", deadline_seconds=3, clock=clock)
    replying(scheduler)
    reply(scheduler)
    clock.now += 3
    assert scheduler.expire() == ["q1"]
    assert scheduler.expire() == []
    assert_code("REPLY_NOT_READY", lambda: scheduler.resume("q1", ctx()))


def test_restored_ready_reply_authorizes_exactly_one_continuation():
    scheduler = ClarificationScheduler("run")
    replying(scheduler)
    reply(scheduler)
    restored = ClarificationScheduler.from_snapshot(scheduler.snapshot())
    assert restored.resume("q1", ctx())["state"] == "RESUMED"
    twice = ClarificationScheduler.from_snapshot(restored.snapshot())
    assert_code("ALREADY_RESUMED", lambda: twice.resume("q1", ctx()))


def test_freeze_is_idempotent_and_blocks_all_scheduling_mutations():
    scheduler = ClarificationScheduler("run")
    replying(scheduler)
    reply(scheduler)
    scheduler.freeze()
    scheduler.freeze()
    for operation in [lambda: request(scheduler), lambda: scheduler.admit("q1", ctx("planner")),
                      lambda: scheduler.mark_reply_started("q1", "p2"), lambda: reply(scheduler),
                      lambda: scheduler.resume("q1", ctx()), lambda: scheduler.fail("q1", "stopped"),
                      scheduler.expire]:
        assert_code("FROZEN", operation)
    restored = ClarificationScheduler.from_snapshot(scheduler.snapshot())
    assert_code("FROZEN", lambda: restored.resume("q1", ctx()))
    assert restored.get("q1")["state"] == "REPLY_READY"


def test_context_and_return_value_mutation_does_not_change_fences():
    scheduler = ClarificationScheduler("run")
    source = ctx()
    original = deepcopy(source)
    result = request(scheduler, source_context=source)
    source["context_epoch"] = 99
    result["source_context"]["context_epoch"] = 88
    result["missing_fields"].append("untracked")
    assert scheduler.get("q1")["source_context"] == original
    assert scheduler.get("q1")["missing_fields"] == ["interval_boundary"]


def test_corrupt_snapshot_cannot_change_context_or_resume_counter():
    scheduler = ClarificationScheduler("run")
    replying(scheduler)
    snapshot = scheduler.snapshot()
    snapshot["requests"]["q1"]["source_context"]["context_epoch"] = 99
    assert_code("SNAPSHOT_CORRUPT", lambda: ClarificationScheduler.from_snapshot(snapshot))


def test_unbounded_clarification_deadline_is_rejected():
    assert_code("INVALID_DEADLINE", lambda: ClarificationScheduler("run", deadline_seconds=None))
