"""Keep observed usage and artifacts when failures arrive after dispatch."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from modelbench.swe_fixed_team_20260903 import runner, runtime_integrity
from modelbench.swe_fixed_team_20260903.tests.test_runner import LEAD_PATCH, action, make_run


def test_lead_fault_preserves_patch_and_unknown_inflight_reservation(make_run):
    def fail_after_edit(run):
        raise PermissionError('fixture provider artifact write failed after dispatch')
    run, env, _ = make_run(condition='solo', lead=[
        [action('bash', command='edit-lead')], fail_after_edit])
    result = run.run()
    assert result['execution_health']['status'] == 'host_error'
    assert result['lead_artifact']['status'] == 'present'
    assert Path(result['lead_artifact']['path']).read_text() == LEAD_PATCH
    assert result['lead_artifact']['bytes'] == len(LEAD_PATCH.encode())
    assert result['budget']['completed_call_count'] == 1
    assert result['budget']['pending_call_count'] == 1
    assert result['budget']['total_tokens'] is None
    assert env.grade_calls == 0


def test_inflight_responses_still_settle_after_another_snapshot_fails(make_run, monkeypatch):
    run, env, _ = make_run()
    run.lead_env = env(run.instance, run.folder / 'environment').start()
    run.bootstrap_team()
    for child in run.workers.values():
        run.control.activate(child.handle)
    both_dispatched = threading.Barrier(2)
    complete = run.transport.complete
    def transport(*args, **kwargs):
        both_dispatched.wait(timeout=5)
        return complete(*args, **kwargs)
    run.transport.complete = transport
    replace = runtime_integrity.os.replace
    def fail_snapshot(source, target):
        if Path(target).name == 'budget.json' and run.budget.summary()['completed_call_count']:
            raise PermissionError('fixture snapshot failure after both calls dispatched')
        replace(source, target)
    monkeypatch.setattr(runtime_integrity.os, 'replace', fail_snapshot)
    def call(child):
        try:
            run._call(child.handle, [{'role': 'system', 'content': run.prompt(worker=child)}],
                      runner.BASE_TOOLS, child.cancel)
        except runner.HostRuntimeError:
            return 'host_error'
        return 'returned'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, run.workers.values()))
    assert 'host_error' in results
    assert run.budget.summary()['completed_call_count'] == len(run.calls) == 2
    assert run.budget.summary()['pending_call_count'] == 0
    assert run.budget.summary()['total_tokens'] == 240
    assert run.cancel.is_set() and runner.RESOURCE_FAILURE.is_set()
    assert len(run.transport.records) == 2
