from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest

from modelbench.minimal_value_20260905 import budget as module
from modelbench.minimal_value_20260905.budget import EpisodeBudget, BudgetPersistenceError, LedgerError


def record(call_id, role='lead', total=10):
    return {'call_id': call_id, 'role': role, 'total_tokens': total}


def test_local_denial_does_not_consume_root():
    budget = EpisodeBudget()
    for role, tokens in [('cm', 1), ('selector', 60001)]:
        with pytest.raises(LedgerError):
            budget.scope('selector').reserve('rejected', role, tokens)
        assert budget.summary()['call_count'] == 0
        assert budget.ticket_scopes == {}


def test_concurrent_root_limit_has_one_atomic_winner():
    budget = EpisodeBudget(max_calls=1)
    barrier = threading.Barrier(2)
    def admit(scope):
        barrier.wait(timeout=3)
        try:
            budget.scope(scope).reserve(scope, 'lead', 100)
            return True
        except LedgerError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, ['candidate_1', 'candidate_2']))
    assert sorted(results) == [False, True]
    assert budget.summary()['call_count'] == len(budget.ticket_scopes) == 1
    assert budget.summary()['reserved_tokens'] == 100


def test_nontransferable_work_and_cm_pools_and_single_charge():
    budget = EpisodeBudget()
    one, two = budget.scope('candidate_1'), budget.scope('candidate_2')
    for role, count in [('lead', 12), ('cm', 6)]:
        for index in range(count):
            ticket = f'{role}-{index}'
            one.reserve(ticket, role, 20)
            one.complete(ticket, record(ticket, role))
        with pytest.raises(LedgerError):
            one.reserve(role + '-overflow', role, 1)
    assert budget.summary()['known_subtotal'] == 180
    assert budget.summary()['call_count'] == 18
    assert budget.summary()['remaining_calls'] == 16
    assert two.summary()['remaining_calls'] == 12
    assert two.summary()['remaining_cm_calls'] == 6
    assert one.summary()['known_subtotal'] == 180


def test_unknown_usage_remains_held_across_restore_and_completion_is_idempotent():
    budget = EpisodeBudget()
    one = budget.scope('candidate_1')
    one.reserve('unknown', 'lead', 260000)
    evidence = record('actual-unknown', total=None)
    one.complete('unknown', evidence)
    one.complete('unknown', evidence)
    restored = EpisodeBudget.from_snapshot(budget.snapshot())
    assert restored.scope('candidate_1').summary()['remaining_tokens'] == 10000
    assert restored.summary()['reserved_tokens'] == 260000
    assert restored.summary()['total_tokens'] is None
    assert restored.summary()['call_count'] == 1
    with pytest.raises(LedgerError, match='SCOPE_CONFLICT'):
        restored.scope('candidate_2').reserve('unknown', 'lead', 260000)
    with pytest.raises(LedgerError, match='COMPLETION_CONFLICT'):
        restored.scope('candidate_1').complete('unknown', record('actual-unknown', total=1))
    corrupt = deepcopy(budget.snapshot())
    corrupt['ticket_scopes']['unknown'] = 'selector'
    with pytest.raises(LedgerError, match='SNAPSHOT_CORRUPT'):
        EpisodeBudget.from_snapshot(corrupt)


def test_deadline_and_local_freeze_are_shared_without_freezing_sibling():
    now = [100.0]
    budget = EpisodeBudget(clock=lambda: now[0])
    assert budget.scope('selector').deadline_at == 1900
    budget.scope('candidate_1').freeze()
    with pytest.raises(LedgerError, match='FROZEN'):
        budget.scope('candidate_1').reserve('one', 'lead', 1)
    budget.scope('candidate_2').reserve('two', 'lead', 1)
    now[0] = 1900
    with pytest.raises(LedgerError, match='DEADLINE_EXCEEDED'):
        budget.scope('selector').reserve('late', 'selector', 1)


def test_durable_admission_failure_poisoned_before_transport(tmp_path, monkeypatch):
    budget = EpisodeBudget(path=tmp_path / 'budget.json')
    def broken(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(module, 'atomic_json', broken)
    with pytest.raises(BudgetPersistenceError):
        budget.scope('candidate_1').reserve('held', 'lead', 100)
    assert budget.summary()['frozen'] is True
    assert budget.summary()['reserved_tokens'] == 100
    with pytest.raises(BudgetPersistenceError):
        budget.scope('candidate_2').reserve('never-admitted', 'lead', 100)
    assert budget.summary()['call_count'] == 1


def test_provider_call_id_cannot_be_double_charged_across_scopes():
    budget = EpisodeBudget()
    for scope in ('candidate_1', 'candidate_2'):
        budget.scope(scope).reserve(scope, 'lead', 100)
    budget.scope('candidate_1').complete('candidate_1', record('same-actual'))
    with pytest.raises(LedgerError, match='CALL_ID_REUSED'):
        budget.scope('candidate_2').complete('candidate_2', record('same-actual'))
    assert budget.summary()['known_subtotal'] == 10
    assert budget.summary()['pending_call_count'] == 1


def test_actual_provider_overshoot_is_recorded_and_stops_new_admission():
    budget = EpisodeBudget()
    one = budget.scope('candidate_1')
    one.reserve('first', 'lead', 100)
    one.complete('first', record('actual', total=280000))
    assert one.summary()['over_token_limit'] is True
    with pytest.raises(LedgerError, match='TOKEN_BUDGET_EXHAUSTED'):
        one.reserve('second', 'lead', 1)
    restored = EpisodeBudget.from_snapshot(budget.snapshot())
    assert restored.scope('candidate_1').summary()['known_subtotal'] == 280000
    assert restored.summary()['known_subtotal'] == 280000
