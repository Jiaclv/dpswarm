"""Faults and concurrent closing admission against the real local budget ledger."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from modelbench.swe_fixed_team_20260903 import runner
from modelbench.swe_fixed_team_20260903.tests.test_runner import action, done, make_run


def test_journal_failure_after_response_keeps_settlement_and_stops(make_run, monkeypatch):
    run, env, _ = make_run(condition='solo', lead=[[action('bash', command='edit-lead')], [done()]])
    original = Path.open
    def open_file(path, *args, **kwargs):
        if path == run.folder / 'events.jsonl' and run.budget.summary()['completed_call_count']:
            raise PermissionError('fixture event journal failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open_file)
    result = run.run()
    assert result['execution_health']['status'] == 'host_error'
    assert result['budget']['completed_call_count'] == result['call_count'] == 1
    assert result['budget']['pending_call_count'] == 0
    assert run.journal_failed and env.grade_calls == 0
    assert not env.instances[0].delta


def test_host_io_during_tool_is_not_candidate_protocol_feedback(make_run):
    run, env, _ = make_run(condition='solo', lead=[[action('bash', command='edit-lead')], [done()]])
    def fail(self, command, timeout):
        raise PermissionError('fixture host command dispatch failure')
    env.run = fail
    result = run.run()
    assert result['execution_health']['status'] == 'host_error'
    assert len(run.transport.records) == 1 and env.grade_calls == 0
    assert result['protocol_errors'] == 0


@pytest.mark.parametrize('barrier', ['token', 'deadline', 'cancel', 'global_cancel'])
def test_closing_never_bypasses_other_admission_limits(make_run, barrier):
    run, env, _ = make_run(limits_override={'closing_call_reserve_exempt': True})
    run.lead_env = env(run.instance, run.folder / 'environment').start()
    run.bootstrap_team()
    child = run.workers['worker-1']
    run.control.activate(child.handle)
    if barrier == 'token':
        run.budget.reserve('already-held', 'lead', run.limits['token_limit'] - 1)
    elif barrier == 'deadline':
        run.start_clock -= run.limits['wall_seconds'] + 1
    elif barrier == 'cancel':
        child.cancel.set()
    else:
        run.cancel.set()
    before = run.budget.summary()['call_count']
    with pytest.raises((runner.LedgerError, RuntimeError)):
        run._call(child.handle, [{'role': 'system', 'content': run.prompt(worker=child)}],
                  runner.BASE_TOOLS, child.cancel, closing=True)
    assert not run.transport.records and run.budget.summary()['call_count'] == before


def test_two_concurrent_closings_cannot_spend_the_last_ticket_twice(make_run):
    run, env, _ = make_run(limits_override={'closing_call_reserve_exempt': True})
    run.lead_env = env(run.instance, run.folder / 'environment').start()
    run.bootstrap_team()
    for child in run.workers.values():
        run.control.activate(child.handle)
    for i in range(run.limits['max_calls'] - 1):
        ident = 'spent-' + str(i)
        run.budget.reserve(ident, 'lead', 0)
        run.budget.complete(ident, {'call_id': ident, 'role': 'lead', 'total_tokens': 0})
    ready = threading.Barrier(2)
    def close(child):
        ready.wait(timeout=5)
        try:
            run._call(child.handle, [{'role':'system', 'content':run.prompt(worker=child)}],
                      runner.BASE_TOOLS, child.cancel, closing=True)
            return 'settled'
        except runner.LedgerError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(close, run.workers.values()))
    assert sorted(results) == ['CALL_BUDGET_EXHAUSTED', 'settled']
    assert len(run.transport.records) == 1
    assert run.budget.summary()['completed_call_count'] == run.limits['max_calls']
    assert run.budget.summary()['pending_call_count'] == 0


def test_context_package_failure_is_classified_before_workers_start(make_run):
    run, env, _ = make_run(limits_override={'cm_team_memory': True, 'cm_scout_distill': True,
                                           'cm_bootstrap_package': True, 'cm_package_budget': 1})
    result = run.run()
    assert result['execution_health']['status'] == 'host_error'
    assert any(e['phase'] == 'context_assembly' for e in result['execution_health']['errors'])
    assert result['workers_with_actual_calls'] == 0 and env.grade_calls == 0
    assert all(child.future is None for child in run.workers.values())
    assert run.control.cp.proj.open_worker_slots_used == 0
