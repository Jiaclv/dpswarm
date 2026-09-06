"""Host fault injection and observed-file contracts. No paid calls or containers."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from modelbench.swe_fixed_team_20260903 import runner, runtime_integrity
from modelbench.swe_fixed_team_20260903.tests.test_runner import (
    PRODUCTION, action, done, events, make_run, settle)
from modelbench.swe_verified_20260903.worktree_probe import PROBE_SCRIPT


def test_atomic_snapshot_retries_only_replace_and_keeps_unique_names(tmp_path, monkeypatch):
    seen = []
    original = runtime_integrity.os.replace
    def replace(source, target):
        seen.append(Path(source))
        if len(seen) < 3:
            raise PermissionError('temporary Windows replace refusal')
        original(source, target)
    monkeypatch.setattr(runtime_integrity.os, 'replace', replace)
    runtime_integrity.atomic_json(tmp_path / 'budget.json', {'settled': 4})
    assert json.loads((tmp_path / 'budget.json').read_text()) == {'settled': 4}
    assert len(seen) == 3 and len(set(seen)) == 1
    assert seen[0].name != 'budget.json.tmp'
    assert not list(tmp_path.glob('*.tmp'))


def test_permanent_snapshot_failure_settles_response_then_stops_run(make_run, monkeypatch):
    run, env, _ = make_run(condition='solo', lead=[[action('bash', command='edit-lead')], [done()]])
    original = runtime_integrity.os.replace
    attempts = []
    def replace(source, target):
        if Path(target).name == 'budget.json' and run.budget.summary()['completed_call_count'] > 0:
            attempts.append(str(source))
            raise PermissionError('persistent snapshot refusal')
        original(source, target)
    monkeypatch.setattr(runtime_integrity.os, 'replace', replace)
    result = run.run()
    assert result['execution_health']['status'] == 'host_error'
    assert result['infrastructure_error'] and result['score'] is None
    assert result['budget']['completed_call_count'] == result['call_count'] == 1
    assert result['budget']['pending_call_count'] == 0
    assert env.grade_calls == 0 and len(run.transport.records) == 1
    assert json.loads((run.folder / 'result.json').read_text(encoding='utf-8')) == result
    assert (run.folder / 'host-errors.jsonl').exists()
    assert len(attempts) >= 3


def test_worker_host_error_preserves_actual_delta_and_health(make_run):
    def fail_after_edit(run):
        raise PermissionError('fixture worker host write failure')
    workers = {'worker-1': [[action('bash', command='edit-production')], fail_after_edit]}
    run, env, _ = make_run(workers=workers,
        lead=[settle('worker-1', decision='discard') + settle('worker-2') + [done()]])
    result = run.run()
    delivery = run.workers['worker-1'].delivery
    assert result['infrastructure_error'] and result['execution_health']['status'] == 'host_error'
    assert delivery['delta_status'] == 'present'
    assert (run.folder / 'worker-1/delta.patch').read_text() == PRODUCTION
    assert delivery['patch_sha256'] == hashlib.sha256(PRODUCTION.encode()).hexdigest()
    assert env.grade_calls == 0


def test_scout_declarations_match_phase_and_unknown_tool_never_executes(make_run):
    enabled = {'cm_team_memory': True, 'cm_scout_distill': True, 'cm_bootstrap_package': True}
    run, env, trace = make_run(limits_override=enabled)
    original = run.transport.complete
    declarations = []
    def complete(*args, **kwargs):
        record = original(*args, **kwargs)
        if kwargs['role'] == 'lead' and not declarations:
            declarations.extend(d['function']['name'] for d in kwargs['tools'])
            record['action']['calls'].append(action('collect', worker_id='worker-1'))
        return record
    run.transport.complete = complete
    result = run.run()
    assert declarations == ['bash']
    assert result['infrastructure_error'] is None
    rejected = [e for e in events(run) if e.get('result', {}).get('error') == 'UNDECLARED_PHASE_TOOL']
    assert len(rejected) == 1 and rejected[0]['result']['executed'] is False
    assert not any(e.get('result', {}).get('error') == 'SCOUT_TOOL_UNAVAILABLE' for e in events(run))
    assert trace.index(('bash', 'lead')) < trace.index(('start', 'worker-1'))


def test_edit_attempt_without_write_does_not_trigger_curfew(make_run):
    command = 'python -c "open(\'missing.py\',\'w\').write(\'attempt\')"'
    run, env, _ = make_run(condition='solo', lead=[[action('bash', command=command)], [done()]])
    result = run.run()
    assert result['infrastructure_error'] is None
    assert run.lead_edit_detected is True  # Historical regex/attempt field remains explicit.
    assert run.lead_worktree['observed_persisted_change'] is False
    assert run._agent_edit_detected(None) is False


def test_edit_then_revert_keeps_ever_changed_separate_from_current(make_run):
    run, env, _ = make_run(condition='solo', lead=[
        [action('bash', command='edit-lead')], [action('bash', command='revert')], [done()]])
    original = env.run
    def execute(self, command, timeout):
        if command == 'revert':
            self.delta = ''
        return original(self, command, timeout)
    env.run = execute
    result = run.run()
    state = result['lead_worktree']
    assert state['observed_persisted_change'] is True and state['current_nonempty_delta'] is False
    assert state['first_persisted_change_ordinal'] == 1 and state['first_change_origin'] == 'tool'
    assert len(list((run.folder / 'lead/worktree').glob('*.json'))) == 3


def test_adoption_records_lead_observed_change_and_ordered_model_metadata(make_run):
    run, env, _ = make_run(condition='hetero_team', worker_models=['gpt-5.6-terra', 'glm-5.3'])
    result = run.run()
    assert result['lead_worktree']['first_change_origin'] == 'adoption'
    assert result['lead_worktree']['current_nonempty_delta'] is True
    assert result['worker_pool'] == ['gpt-5.6-terra', 'glm-5.3']
    assert result['mechanism_coverage']['cm'] == result['cm_status'] == 'integrated_on_demand'
    assert all(w['worktree']['observed_persisted_change'] for w in result['workers'])


def test_missing_delta_does_not_report_measured_zero(make_run):
    run, _, _ = make_run()
    run.bootstrap_team = lambda: None  # Only the measurement function is exercised.
    child = type('Child', (), {'worker_id': 'not-started'})()
    assert run.worker_delta_bytes(child) is None


def test_direct_file_probe_tracks_untracked_binary_delete_and_revert(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'tracked.py').write_text('old')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'tracked.py'], check=True)
    def observe():
        return json.loads(subprocess.check_output([sys.executable, '-I', '-c', PROBE_SCRIPT], cwd=tmp_path))
    before = observe()
    # Candidate repository modules cannot replace observer standard-library imports.
    (tmp_path / 'json.py').write_text("raise RuntimeError('repository shadow module executed')")
    assert 'json.py' in observe()
    (tmp_path / 'json.py').unlink()
    (tmp_path / 'untracked.bin').write_bytes(b'\x00\xff')
    (tmp_path / 'tracked.py').unlink()
    changed = observe()
    assert 'tracked.py' not in changed and changed['untracked.bin']['bytes'] == 2
    (tmp_path / 'untracked.bin').unlink()
    (tmp_path / 'tracked.py').write_text('old')
    assert observe() == before
    assert subprocess.check_output(['git', '-C', str(tmp_path), 'diff', '--cached', '--name-only']).strip() == b'tracked.py'


@pytest.mark.parametrize('remaining,closing,exempt,admitted', [
    (3, False, False, True), (2, False, True, False), (1, False, True, False),
    (2, True, True, True), (1, True, True, True), (0, True, True, False),
    (2, True, False, False), (1, True, False, False)])
def test_closing_uses_real_budget_tickets(make_run, remaining, closing, exempt, admitted):
    run, env, _ = make_run(limits_override={'closing_call_reserve_exempt': exempt})
    run.lead_env = env(run.instance, run.folder / 'environment').start()
    run.bootstrap_team()
    for i in range(run.limits['max_calls'] - remaining):
        ident = 'fixture-spent-' + str(i)
        run.budget.reserve(ident, 'lead', 0)
        run.budget.complete(ident, {'call_id': ident, 'role': 'lead', 'total_tokens': 0})
    child = run.workers['worker-1']
    run.control.activate(child.handle)
    before = len(run.transport.records)
    try:
        run._call(child.handle, [{'role':'system','content':run.prompt(worker=child)}], runner.BASE_TOOLS,
                  child.cancel, closing=closing)
    except runner.LedgerError:
        assert not admitted
    else:
        assert admitted
    assert len(run.transport.records) - before == int(admitted)
