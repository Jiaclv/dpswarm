import json

import pytest

from dpswarm.team_runtime.ledger import ExecutionStore, LedgerError, RunBudget, canonical, digest


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def record(call_id="actual-1", **usage):
    return {"call_id": call_id, **usage}


def assert_code(code, operation):
    with pytest.raises(LedgerError) as exc:
        operation()
    assert exc.value.code == code


def rehash(snapshot):
    value = {k: v for k, v in snapshot.items() if k != "snapshot_hash"}
    return {**value, "snapshot_hash": digest(value)}


def test_inclusive_usage_is_counted_once_and_call_id_is_distinct_from_ticket():
    budget = RunBudget(token_limit=1000)
    budget.reserve("logical-1", "executor", 500)
    completed = budget.complete("logical-1", record(input_tokens=100, output_tokens=20,
        cached_input_tokens=80, reasoning_tokens=15, total_tokens=120))
    assert completed["call_id"] == "actual-1"
    assert completed["usage"]["total_tokens"] == 120
    assert budget.summary()["known_subtotal"] == 120
    assert budget.summary()["reserved_tokens"] == 0
    assert budget.summary()["total_tokens"] == 120


@pytest.mark.parametrize("usage,expected,source", [
    ({"input_tokens": 8, "output_tokens": 2}, 10, "input_tokens + output_tokens"),
    ({"input_tokens": 8, "output_tokens": 2, "total_tokens": 99}, 10, "input_tokens + output_tokens"),
    ({"total_tokens": 10}, 10, "reported_total_tokens"),
    ({"input_tokens": 8}, None, None),
    ({}, None, None),
])
def test_known_and_unknown_usage_semantics(usage, expected, source):
    budget = RunBudget(token_limit=100)
    budget.reserve("t", "executor", 50)
    completed = budget.complete("t", record(**usage))
    assert completed["usage"]["total_tokens"] == expected
    assert completed["usage"]["total_source"] == source
    assert budget.summary()["total_tokens"] == expected
    assert budget.summary()["unknown_call_count"] == (expected is None)
    assert budget.summary()["reserved_tokens"] == (50 if expected is None else 0)
    assert completed["usage"]["reported_total_mismatch"] == (usage.get("total_tokens") == 99)


def test_unknown_usage_keeps_reservation_and_known_subtotal():
    budget = RunBudget(token_limit=100)
    budget.reserve("known", "planner", 30)
    budget.complete("known", record("a", input_tokens=10, output_tokens=10))
    budget.reserve("unknown", "executor", 80)
    budget.complete("unknown", record("b"))
    summary = budget.summary()
    assert summary["total_tokens"] is None
    assert summary["known_subtotal"] == 20
    assert summary["unknown_call_count"] == 1
    assert summary["committed_tokens"] == 100
    assert_code("TOKEN_BUDGET_EXHAUSTED", lambda: budget.reserve("more", "planner", 1))


def test_reservation_and_completion_are_idempotent_but_conflicts_rejected():
    budget = RunBudget(token_limit=100)
    reserved = budget.reserve("t", "executor", 20)
    assert budget.reserve("t", "executor", 20) == reserved
    assert budget.summary()["call_count"] == 1
    assert_code("TICKET_CONFLICT", lambda: budget.reserve("t", "planner", 20))
    assert_code("TICKET_CONFLICT", lambda: budget.reserve("t", "executor", 21))
    evidence = record("actual", input_tokens=4, output_tokens=3)
    completed = budget.complete("t", evidence)
    assert budget.complete("t", evidence) == completed
    assert_code("COMPLETION_CONFLICT", lambda: budget.complete("t", record("actual", total_tokens=8)))
    budget.reserve("t2", "executor", 20)
    assert_code("CALL_ID_REUSED", lambda: budget.complete("t2", evidence))
    assert_code("ROLE_MISMATCH", lambda: budget.complete("t2", record("other", role="planner")))


def test_call_limit_counts_started_reservations_even_when_no_result_arrives():
    budget = RunBudget(max_calls=1, token_limit=100)
    budget.reserve("t", "executor", 50)
    assert budget.summary()["pending_call_count"] == 1
    assert budget.summary()["total_tokens"] is None
    assert_code("CALL_BUDGET_EXHAUSTED", lambda: budget.reserve("t2", "executor", 1))


def test_global_deadline_and_freeze_deny_new_calls_but_keep_late_evidence():
    clock = Clock()
    budget = RunBudget(token_limit=100, deadline_seconds=5, clock=clock)
    budget.reserve("t", "executor", 30)
    clock.now += 5
    assert_code("DEADLINE_EXCEEDED", lambda: budget.reserve("other", "planner", 1))
    budget.freeze()
    budget.freeze()
    assert_code("FROZEN", lambda: budget.reserve("t", "executor", 30))
    budget.complete("t", record(total_tokens=10))
    assert budget.summary()["total_tokens"] == 10


def test_expired_snapshot_restores_without_replaying_admission():
    clock = Clock()
    budget = RunBudget(token_limit=100, deadline_seconds=1, clock=clock)
    budget.reserve("t", "executor", 50)
    budget.complete("t", record(total_tokens=10))
    snapshot = budget.snapshot()
    clock.now += 10
    restored = RunBudget.from_snapshot(snapshot, clock=clock)
    assert restored.snapshot() == snapshot
    assert restored.summary()["deadline_exceeded"] is True
    assert_code("DEADLINE_EXCEEDED", lambda: restored.reserve("new", "executor", 1))


def test_overshoot_snapshot_restores_all_previously_admitted_tickets():
    budget = RunBudget(token_limit=100)
    # Both are admitted before the first actual usage exceeds its reservation.
    budget.reserve("z-pending", "planner", 50)
    budget.reserve("a-completed", "executor", 50)
    budget.complete("a-completed", record(total_tokens=150))
    snapshot = json.loads(canonical(budget.snapshot()))
    restored = RunBudget.from_snapshot(snapshot)
    assert restored.snapshot() == snapshot
    assert restored.summary()["committed_tokens"] == 200
    assert restored.summary()["over_token_limit"] is True
    assert_code("TOKEN_BUDGET_EXHAUSTED", lambda: restored.reserve("more", "executor", 0))
    restored.complete("z-pending", record("late", total_tokens=1))
    assert restored.summary()["total_tokens"] == 151


def test_snapshot_validates_hash_and_usage_evidence():
    budget = RunBudget(token_limit=100)
    budget.reserve("t", "executor", 50)
    budget.complete("t", record(total_tokens=10))
    snapshot = budget.snapshot()
    snapshot["token_limit"] += 1
    assert_code("SNAPSHOT_CORRUPT", lambda: RunBudget.from_snapshot(snapshot))
    snapshot = budget.snapshot()
    snapshot["tickets"]["t"]["usage"]["total_tokens"] = 0
    assert_code("SNAPSHOT_CORRUPT", lambda: RunBudget.from_snapshot(rehash(snapshot)))


@pytest.mark.parametrize("usage", [{"input_tokens": -1}, {"total_tokens": True},
    {"input_tokens": 2, "cached_input_tokens": 3}, {"output_tokens": 2, "reasoning_tokens": 3}])
def test_bad_usage_does_not_destroy_reservation(usage):
    budget = RunBudget(token_limit=100)
    budget.reserve("t", "executor", 50)
    with pytest.raises(LedgerError):
        budget.complete("t", record(**usage))
    assert budget.summary()["pending_call_count"] == 1
    assert budget.summary()["reserved_tokens"] == 50


def test_completed_tool_result_replays_after_restore_without_execution(tmp_path):
    store = ExecutionStore(tmp_path)
    request = {"role": "executor", "name": "write", "arguments": {"path": "a", "content": "one"}}
    store.plan_tool("write-1", request)
    store.start_tool("write-1")
    side_effects = ["one"]  # The runner executes only after a durable running event.
    result = {"stdout": "", "stderr": "", "exit_code": 0}
    store.complete_tool("write-1", result)
    recovered = ExecutionStore(tmp_path)
    assert recovered.plan_tool("write-1", request)["status"] == "completed"
    assert recovered.replay_completed("write-1") == result
    assert_code("TOOL_ALREADY_COMPLETED", lambda: recovered.start_tool("write-1"))
    assert side_effects == ["one"]
    assert len(recovered.events_since()) == 3


def test_running_tool_after_crash_cannot_be_automatically_retried(tmp_path):
    store = ExecutionStore(tmp_path)
    request = {"name": "write", "arguments": {"content": "may-have-written"}}
    store.plan_tool("write-1", request)
    store.start_tool("write-1")
    recovered = ExecutionStore(tmp_path)
    assert recovered.plan_tool("write-1", request)["status"] == "running"
    assert_code("TOOL_OUTCOME_UNKNOWN", lambda: recovered.start_tool("write-1"))
    assert_code("TOOL_OUTCOME_UNKNOWN", lambda: recovered.replay_completed("write-1"))
    assert len(recovered.events_since()) == 2


def test_tool_operations_reject_conflicting_replays_without_extra_events(tmp_path):
    store = ExecutionStore(tmp_path)
    planned = store.plan_tool("op", {"name": "read"})
    assert store.plan_tool("op", {"name": "read"}) == planned
    assert_code("TOOL_CONFLICT", lambda: store.plan_tool("op", {"name": "write"}))
    store.start_tool("op")
    complete = store.complete_tool("op", {"stdout": "result"})
    assert store.complete_tool("op", {"stdout": "result"}) == complete
    assert_code("TOOL_CONFLICT", lambda: store.complete_tool("op", {"stdout": "changed"}))
    assert len(store.events_since()) == 3


def test_checkpoint_and_following_events_recover_role_history(tmp_path):
    store = ExecutionStore(tmp_path)
    state = {"histories": {"executor": [{"role": "user", "content": "task"}]}, "message_cursor": 2,
             "phase": "E_WAITING_REPLY"}
    saved = store.save_snapshot(state)
    state["histories"]["executor"].append({"role": "assistant", "content": "not saved"})
    event = store.append("message.delivered", {"cursor": 3, "content": "clarification"})
    recovered = ExecutionStore(tmp_path).load_snapshot()
    assert recovered["state"] == saved["state"]
    assert recovered["event_seq"] == saved["event_seq"]
    assert recovered["pending_events"] == [event]


@pytest.mark.parametrize("missing", [False, True])
def test_latest_durable_checkpoint_survives_missing_or_stale_atomic_snapshot(tmp_path, missing):
    store = ExecutionStore(tmp_path)
    store.save_snapshot({"phase": "before"})
    # Emulate crash after checkpoint fsync but before atomic snapshot replacement.
    store.append("execution.checkpoint", {"state": {"phase": "after"}})
    if missing:
        store.snapshot_path.unlink()
    loaded = ExecutionStore(tmp_path).load_snapshot()
    assert loaded["state"] == {"phase": "after"}
    assert loaded["event_seq"] == 2
    assert loaded["pending_events"] == []


def test_stale_writer_is_rejected_instead_of_overwriting_other_events(tmp_path):
    first, stale = ExecutionStore(tmp_path), ExecutionStore(tmp_path)
    first.append("event", {"n": 1})
    assert_code("STALE_WRITER", lambda: stale.append("event", {"n": 2}))
    assert len(ExecutionStore(tmp_path).events_since()) == 1


@pytest.mark.parametrize("mutation", ["truncated", "content", "sequence", "previous_hash"])
def test_corrupted_journal_is_not_silently_repaired(tmp_path, mutation):
    store = ExecutionStore(tmp_path)
    store.append("event", {"n": 1})
    if mutation == "truncated":
        store.journal.write_bytes(store.journal.read_bytes()[:-1])
        code = "JOURNAL_TRUNCATED"
    else:
        event = json.loads(store.journal.read_text(encoding="utf-8"))
        if mutation == "content":
            event["payload"]["n"] = 9
        elif mutation == "sequence":
            event["seq"] = 2
        else:
            event["prev_hash"] = "f" * 64
        store.journal.write_text(canonical(event) + "\n", encoding="utf-8")
        code = "JOURNAL_CORRUPT"
    assert_code(code, lambda: ExecutionStore(tmp_path))


def test_corrupt_atomic_snapshot_is_rejected(tmp_path):
    store = ExecutionStore(tmp_path)
    store.save_snapshot({"phase": "saved"})
    value = json.loads(store.snapshot_path.read_text(encoding="utf-8"))
    value["state"]["phase"] = "changed"
    store.snapshot_path.write_text(canonical(value), encoding="utf-8")
    assert_code("SNAPSHOT_CORRUPT", lambda: ExecutionStore(tmp_path).load_snapshot())
