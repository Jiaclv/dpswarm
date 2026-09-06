import pytest

from modelbench.swe_fixed_team_20260903 import runner
from modelbench.swe_fixed_team_20260903.tests.test_runner import make_run


@pytest.mark.parametrize('barrier', ['draining', 'cancel', 'resource_failure', 'deadline'])
def test_cm_rechecks_admission_after_capacity_wait(make_run, barrier):
    run, _, _ = make_run(condition='solo')
    acquire = run._acquire
    def after_wait(semaphore, cancel):
        acquire(semaphore, cancel)
        if barrier == 'draining':
            run.draining = True
        elif barrier == 'cancel':
            run.cancel.set()
        elif barrier == 'resource_failure':
            runner.RESOURCE_FAILURE.set()
        else:
            run.start_clock -= run.limits['wall_seconds'] + 1
    run._acquire = after_wait
    with pytest.raises(runner.LedgerError):
        run._cm_call('cm-fixture', run.control.lead, [], {})
    assert not run.transport.records and run.budget.summary()['call_count'] == 0


def test_cm_reserves_the_configured_output_cap_and_estimation_slack(make_run):
    run, _, _ = make_run(condition='solo', limits_override={
        'cm_max_tokens': 16000, 'cm_reservation_slack': 20000})
    seen = {}
    complete = run.transport.complete
    def transport(*args, **kwargs):
        seen.update(run.budget.snapshot()['tickets']['cm-fixture'])
        seen['max_tokens'] = kwargs['max_tokens']
        return complete(*args, **kwargs)
    run.transport.complete = transport
    run._cm_call('cm-fixture', run.control.lead, [{'role': 'user', 'content': 'fixture'}], {})
    assert seen['reserved_tokens'] > 36000 and seen['max_tokens'] == 16000
    assert run.budget.summary()['completed_call_count'] == 1


def test_cm_host_error_keeps_pending_usage_and_stops_admission(make_run):
    run, _, _ = make_run(condition='solo')
    def transport(*args, **kwargs):
        raise PermissionError('fixture CM response metadata write failure')
    run.transport.complete = transport
    with pytest.raises(runner.HostRuntimeError):
        run._cm_call('cm-fixture', run.control.lead, [], {})
    assert run.cancel.is_set() and runner.RESOURCE_FAILURE.is_set()
    assert run.budget.summary()['pending_call_count'] == 1
    assert run.budget.summary()['total_tokens'] is None
    assert run.host_errors[-1]['phase'] == 'cm_execution'


def test_cm_does_not_continue_for_a_cancelled_worker(make_run):
    run, env, _ = make_run()
    run.lead_env = env(run.instance, run.folder / 'environment').start()
    run.bootstrap_team()
    child = run.workers['worker-1']
    child.cancel.set()
    with pytest.raises(RuntimeError):
        run._cm_call('cm-worker-fixture', child.handle, [], {})
    assert not run.transport.records and run.budget.summary()['call_count'] == 0
