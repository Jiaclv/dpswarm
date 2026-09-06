"""Revision 11 tests: compression edit-phase curfew (F1), CM output headroom
(F2), closing-call Lead reserve exemption (F3), delivery forensics and worker
death phases (F4), and the neutral no-edit banner field (F5). Offline only."""
import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_fixed_team_20260903 import cli, runner  # runner.py inserts dpswarm-plugin into sys.path
from dpswarm.team_runtime.ledger import LedgerError
from modelbench.swe_fixed_team_20260903.tests.test_runner import (
    PRODUCTION, REGRESSION, action, done, events, make_run, settle)
from modelbench.swe_fixed_team_20260903.validation.audit_results import Audit

EDIT_COMMAND = 'python -c "open(\'f.py\',\'w\').write(\'fix\')"'
INSPECT_COMMAND = 'grep -rn "TODO" /testbed | head -5'
CLOSED_BUT_UNDECLARED = 'undeclared-closing-bash'


# ------------------------------------------------------------------ defaults

def test_rev11_defaults_are_pinned():
    assert runner.LIMITS['cm_edit_curfew'] is True
    assert runner.LIMITS['closing_call_reserve_exempt'] is True
    assert runner.LIMITS['edit_status_banner'] is True
    assert runner.LIMITS['cm_max_tokens'] == 4096  # rev11 F2
    for flag in ('cm_team_memory', 'cm_scout_distill', 'cm_bootstrap_package'):
        assert runner.LIMITS[flag] is False  # rev10 assembly defaults unchanged
    assert cli.GATE_PATH.name == 'gate_revision12_test_evidence_20260905.json'  # Approved prompt changes require a new offline source freeze.


# ----------------------------------------------------- F1 compression curfew

def over_budget_messages():
    return [{'role': 'system', 'content': 's'},
            {'role': 'user', 'content': 'x' * 400},
            {'role': 'assistant', 'content': 'a'},
            {'role': 'user', 'content': 'u2'},
            {'role': 'user', 'content': 'u3'}]


def test_maybe_compress_context_curfew_skips_cm_after_edit(make_run):
    run, env, _ = make_run(limits_override={'cm_context_budget': 10, 'cm_keep_recent': 1})
    run.lead_edit_detected = True
    run.lead_worktree = {"observed_persisted_change": True}
    messages = over_budget_messages()
    run._maybe_compress_context(run.control.lead, messages, None)
    skipped = [e for e in events(run) if e['event'] == 'cm_skipped']
    assert [(e['trigger_role'], e['reason']) for e in skipped] == [('lead', 'edit_phase_curfew')]
    assert not [s for s in run.transport.seen if s['actor'] == 'cm']  # no CM call ever started
    assert len(messages) == 5  # history untouched


def test_maybe_compress_context_without_curfew_flag_compresses(make_run):
    run, env, _ = make_run(limits_override={'cm_context_budget': 10, 'cm_keep_recent': 1,
                                            'cm_edit_curfew': False})
    run.lead_edit_detected = True
    run.lead_worktree = {"observed_persisted_change": True}
    messages = over_budget_messages()
    run._maybe_compress_context(run.control.lead, messages, None)
    assert not [e for e in events(run) if e['event'] == 'cm_skipped']
    assert [s for s in run.transport.seen if s['actor'] == 'cm']
    assert 'Context summary' in messages[1]['content']  # old behavior restored


def test_edit_phase_curfew_holds_compression_off_in_a_solo_run(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    run, env, _ = make_run(condition='solo',
                           lead=[[action('bash', command=EDIT_COMMAND)]] * 2 + [[done()]])
    result = run.run()
    assert result['infrastructure_error'] is None
    skipped = [e for e in events(run) if e['event'] == 'cm_skipped']
    assert skipped and all(e['reason'] == 'edit_phase_curfew' and e['trigger_role'] == 'lead'
                           for e in skipped)
    assert result['cm_call_count'] == 0
    assert not [s for s in run.transport.seen if s['actor'] == 'cm']
    detected = [e for e in events(run) if e['event'] == 'worktree_edit_detected']
    assert len(detected) == 1 and detected[0]['agent'] == 'lead'
    assert detected[0]['pattern'] == 'python_open_write' and detected[0]['local_call'] == 1
    assert result['lead_death_phase'] == 'edited'


def test_edit_phase_curfew_off_restores_on_demand_compression(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    monkeypatch.setitem(runner.LIMITS, 'cm_edit_curfew', False)
    run, env, _ = make_run(condition='solo',
                           lead=[[action('bash', command=EDIT_COMMAND)]] * 2 + [[done()]])
    result = run.run()
    assert result['infrastructure_error'] is None
    assert result['cm_call_count'] >= 1
    compressions = [e for e in events(run) if e['event'] == 'cm_compression']
    assert compressions and all(c['after_est_tokens'] < c['before_est_tokens'] for c in compressions)
    assert not [e for e in events(run) if e['event'] == 'cm_skipped']


# ------------------------------------------------ F3 closing call exemption

def worker_handle(run):
    request = {'model': 'glm-5.3', 'title': 'Production implementation',
               'task': 'Own the production-code fixture task'}
    handle = run.control.delegate(run.control.lead, request)[0]
    run.control.activate(handle)  # worker_run activates before its first call
    return handle


def pinned_remaining_calls(run, monkeypatch, remaining):
    real_summary = run.budget.summary
    monkeypatch.setattr(run.budget, 'summary',
                        lambda *a, **k: {**real_summary(), 'remaining_calls': remaining})


def test_closing_call_spends_lead_reserve_but_normal_worker_call_does_not(make_run, monkeypatch):
    # attempt_counts=None keeps the direct call out of the first-worker-call
    # bookkeeping, which normally runs after a full team bootstrap.
    run, env, _ = make_run(condition='solo', attempt_counts={'worker-1': None})
    handle = worker_handle(run)
    assert handle.role == 'worker'
    pinned_remaining_calls(run, monkeypatch, 1)  # at or below lead_reserve_calls (2)
    messages = [{'role': 'system', 'content': 'Own the production-code fixture prompt'}]
    with pytest.raises(LedgerError) as denied:
        run._call(handle, messages, [], run.cancel)
    assert denied.value.code == 'LEAD_RESERVE'
    record = run._call(handle, messages, [], run.cancel, closing=True)
    assert record['call_id'] and not record['error']
    assert len([e for e in events(run) if e['event'] == 'call_settled']) == 1  # only the closing call


def test_closing_call_reserve_exemption_is_per_arm_disableable(make_run, monkeypatch):
    run, env, _ = make_run(condition='solo', attempt_counts={'worker-1': None},
                           limits_override={'closing_call_reserve_exempt': False})
    handle = worker_handle(run)
    pinned_remaining_calls(run, monkeypatch, 1)
    with pytest.raises(LedgerError) as denied:
        run._call(handle, [{'role': 'system', 'content': 'Own the production-code fixture prompt'}],
                  [], run.cancel, closing=True)
    assert denied.value.code == 'LEAD_RESERVE'


# ------------------------------------------- F4 death phases and forensics

def test_death_phase_adopted_and_patch_frozen_forensics(make_run):
    run, env, _ = make_run()
    result = run.run()
    assert result['infrastructure_error'] is None
    assert {w['worker_id']: w['death_phase'] for w in result['workers']} == {
        'worker-1': 'adopted', 'worker-2': 'adopted'}
    assert result['lead_death_phase'] == 'edited'  # Formal adoption is an observed Lead change.
    assert result['lead_edit_attempted'] is False
    frozen = [e for e in events(run) if e['event'] == 'patch_frozen'][0]
    assert frozen['patch_bytes'] == len((PRODUCTION + REGRESSION).encode())
    assert frozen['lead_edit_detected'] is False and frozen['lead_first_edit_ordinal'] is None
    assert not [e for e in events(run) if e['event'] == 'worker_cleanup_discarded']


def test_death_phase_delivered_not_adopted(make_run):
    lead = [settle('worker-1', decision='discard') + settle('worker-2') + [done()]]
    run, env, _ = make_run(lead=lead)
    result = run.run()
    assert result['infrastructure_error'] is None
    phases = {w['worker_id']: w['death_phase'] for w in result['workers']}
    assert phases == {'worker-1': 'delivered_not_adopted', 'worker-2': 'adopted'}


def test_death_phase_edited_no_delivery(make_run):
    scripts = {'worker-1': [[action('bash', command=EDIT_COMMAND)]] * 8
                            + [[action('bash', command=CLOSED_BUT_UNDECLARED)]]}
    lead = [settle('worker-1', decision='discard') + settle('worker-2') + [done()]]
    run, env, _ = make_run(lead=lead, workers=scripts)
    result = run.run()
    assert result['infrastructure_error'] is None
    assert run.workers['worker-1'].delivery['status'] == 'local_call_limit'
    phases = {w['worker_id']: w['death_phase'] for w in result['workers']}
    assert phases == {'worker-1': 'edited_no_delivery', 'worker-2': 'adopted'}


def test_death_phase_no_edit(make_run):
    scripts = {'worker-1': [[action('bash', command=INSPECT_COMMAND)]] * 8
                            + [[action('bash', command=CLOSED_BUT_UNDECLARED)]]}
    lead = [settle('worker-1', decision='discard') + settle('worker-2') + [done()]]
    run, env, _ = make_run(lead=lead, workers=scripts)
    result = run.run()
    assert result['infrastructure_error'] is None
    phases = {w['worker_id']: w['death_phase'] for w in result['workers']}
    assert phases == {'worker-1': 'no_edit', 'worker-2': 'adopted'}
    assert not [e for e in events(run) if e['event'] == 'worktree_edit_detected']


def test_cleanup_discard_measures_delta_bytes_and_feeds_audit_forensics(make_run):
    def fail_after_workers(run):
        for child in run.workers.values():
            child.future.result(timeout=30)  # both deliveries settle before the Lead fails
        return {'error': 'fixture lead transport failure'}

    lead = [fail_after_workers]  # the fixture already prepends its scout round
    run, env, _ = make_run(lead=lead)
    result = run.run()
    assert result['infrastructure_error'] is None
    cleanup = [e for e in events(run) if e['event'] == 'worker_cleanup_discarded']
    assert {e['worker_id']: e['delta_bytes'] for e in cleanup} == {
        'worker-1': len(PRODUCTION.encode()), 'worker-2': len(REGRESSION.encode())}
    frozen = [e for e in events(run) if e['event'] == 'patch_frozen'][0]
    assert frozen['patch_bytes'] == 0 and frozen['lead_edit_detected'] is False
    assert frozen['lead_first_edit_ordinal'] is None
    phases = {w['worker_id']: w['death_phase'] for w in result['workers']}
    assert set(phases.values()) == {'delivered_not_adopted'}
    forensics = Audit(run.batch_dir).forensics(run.folder, result)
    assert forensics['cleanup_discards'] == [
        {'worker_id': e['worker_id'], 'status': 'completed', 'delta_bytes': e['delta_bytes']}
        for e in cleanup]
    assert forensics['patch_bytes'] == 0 and forensics['lead_edit_detected'] is False
    assert forensics['lead_first_edit_ordinal'] is None
    assert forensics['lead_death_phase'] == 'no_edit'
    assert forensics['worker_death_phases'] == phases
    assert forensics['cm_skipped_count'] == 0


def test_audit_forensics_tolerates_pre_rev11_runs(tmp_path):
    run_dir = tmp_path / 'results' / 'fixture-old'
    run_dir.mkdir(parents=True)
    lines = [json.dumps({'event': 'run_started'}),
             json.dumps({'event': 'worker_cleanup_discarded', 'worker_id': 'worker-1',
                         'status': 'completed'}),
             json.dumps({'event': 'patch_frozen'}),
             json.dumps({'event': 'cm_skipped', 'trigger_role': 'lead',
                         'reason': 'low remaining call budget'})]
    (run_dir / 'events.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    forensics = Audit(tmp_path).forensics(run_dir, {'workers': [{'worker_id': 'worker-1'}]})
    assert forensics['cleanup_discards'] == [
        {'worker_id': 'worker-1', 'status': 'completed', 'delta_bytes': None}]
    assert forensics['patch_bytes'] is None and forensics['lead_edit_detected'] is None
    assert forensics['lead_first_edit_ordinal'] is None
    assert forensics['cm_skipped_count'] == 1
    assert forensics['cm_skipped_reasons'] == {'low remaining call budget': 1}
    assert forensics['lead_death_phase'] is None
    assert forensics['worker_death_phases'] == {'worker-1': None}


# ------------------------------------------------------ F5 no-edit banner

def pin_half_spent_tokens(run, monkeypatch):
    real_summary = run.budget.summary
    monkeypatch.setattr(run.budget, 'summary',
                        lambda *a, **k: {**real_summary(),
                                         'committed_tokens': run.limits['token_limit'] // 2 + 1})


def test_no_edit_banner_states_the_fact_once_tokens_half_spent(make_run, monkeypatch):
    run, env, _ = make_run(condition='solo', lead=[[action('bash', command=INSPECT_COMMAND), done()]])
    pin_half_spent_tokens(run, monkeypatch)
    result = run.run()
    assert result['infrastructure_error'] is None
    assert '"edit_status": "no edits yet"' in run.transport.seen[0]['messages'][1]['content']
    assert not [e for e in events(run) if e['event'] == 'worktree_edit_detected']


def test_no_edit_banner_disappears_after_the_first_edit(make_run, monkeypatch):
    run, env, _ = make_run(condition='solo',
                           lead=[[action('bash', command=EDIT_COMMAND)], [done()]])
    pin_half_spent_tokens(run, monkeypatch)
    result = run.run()
    assert result['infrastructure_error'] is None
    seen = run.transport.seen
    assert '"edit_status": "no edits yet"' in seen[0]['messages'][1]['content']
    assert 'edit_status' not in seen[1]['messages'][-1]['content']
    detected = [e for e in events(run) if e['event'] == 'worktree_edit_detected'][0]
    assert detected['agent'] == 'lead' and detected['local_call'] == 1


def test_no_edit_banner_is_per_arm_disableable(make_run, monkeypatch):
    run, env, _ = make_run(condition='solo', limits_override={'edit_status_banner': False},
                           lead=[[action('bash', command=INSPECT_COMMAND), done()]])
    pin_half_spent_tokens(run, monkeypatch)
    result = run.run()
    assert result['infrastructure_error'] is None
    assert not any('edit_status' in message['content']
                   for seen in run.transport.seen for message in seen['messages'])


def test_lead_edit_ordinal_counts_from_the_first_lead_round(make_run):
    lead = [[action('bash', command=EDIT_COMMAND)] + settle('worker-1')
            + settle('worker-2') + [done()]]
    run, env, _ = make_run(lead=lead)
    result = run.run()
    assert result['infrastructure_error'] is None
    frozen = [e for e in events(run) if e['event'] == 'patch_frozen'][0]
    assert frozen['lead_edit_detected'] is True and frozen['lead_first_edit_ordinal'] == 2
    detected = [e for e in events(run) if e['event'] == 'worktree_edit_detected'][0]
    assert detected['agent'] == 'lead' and detected['local_call'] == 2
    assert result['lead_death_phase'] == 'edited'


# ------------------------------------------------- 20260904C wave manifests

@pytest.fixture
def gate_fixture(tmp_path, monkeypatch):
    gate = {'status': 'PASS', 'runtime_sources': cli.sources(), 'validation_artifacts': {}}
    gate_path = tmp_path / 'gate_fixture.json'
    gate_path.write_text(json.dumps(gate), encoding='utf-8')
    monkeypatch.setattr(cli, 'GATE_PATH', gate_path)
    return gate_path


def no_pair_holds_two_repeats_of_one_arm(schedule):
    """Stagger convention inside one instance block (run() pairs per block).
    A single-arm block has no second arm to interleave with, so its repeats
    pair together by design (xarray/seaborn solo, sphinx heterooff canary)."""
    for _key, entries in cli.instance_blocks(schedule):
        if len({e['arm'] for e in entries}) < 2:
            continue
        ids = [e['run_id'] for e in entries]
        for pair in [ids[i:i + 2] for i in range(0, len(ids), 2)]:
            arms = {e['arm'] for e in entries if e['run_id'] in pair}
            assert len(arms) == len(pair), 'a pair holds two runs of one arm: ' + str(pair)


def test_curfew15_manifest_freezes_hard_tier_wave_and_staggered_reps(tmp_path, gate_fixture):
    batch = tmp_path / 'pilot_fixture_v15'
    manifest = cli.prepare(batch, wave='curfew15')
    schedule = manifest['schedule']
    assert len(schedule) == 14
    ids = [e['run_id'] for e in schedule]
    assert len(set(ids)) == 14
    sphinx = [e for e in schedule if e['instance']['instance_id'] == 'sphinx-doc__sphinx-8035']
    assert [e['arm'] for e in sphinx] == ['solo_gpt-5.6-sol', 'solo_gpt-5.6-sol.nocurf',
                                          'solo_gpt-5.6-sol.cm24'] * 2 + ['solo_gpt-5.6-sol']
    assert [e['rep'] for e in sphinx] == [1, 1, 1, 2, 2, 2, 3]
    assert [key for key, _ in cli.instance_blocks(schedule)] == [
        'sphinx-doc__sphinx-8035', 'pydata__xarray-7229', 'mwaskom__seaborn-3069',
        'scikit-learn__scikit-learn-25232', 'sympy__sympy-16792', 'astropy__astropy-14995']
    no_pair_holds_two_repeats_of_one_arm(schedule)
    for entry in schedule:
        assert entry['condition'] == 'solo' and entry['lead_model'] == 'gpt-5.6-sol'
        effective = entry['effective_limits']
        # rev11 defaults hold everywhere except the single ablated key per arm.
        assert (effective['edit_status_banner'], effective['closing_call_reserve_exempt'],
                effective['cm_max_tokens']) == (True, True, 4096)
        if entry['arm'].endswith('.nocurf'):
            assert entry['limits_override'] == {'cm_edit_curfew': False}
            assert effective['cm_edit_curfew'] is False and effective['cm_call_allowance'] == 12
        elif entry['arm'].endswith('.cm24'):
            assert entry['limits_override'] == {'cm_call_allowance': 24}
            assert effective['cm_call_allowance'] == 24 and effective['cm_edit_curfew'] is True
        else:
            assert 'limits_override' not in entry and effective == {**runner.LIMITS}
    assert manifest['maximum_calls'] == 14 * 28
    assert manifest['sum_token_admission_limits'] == 14 * 600_000
    assert 'Pre-registered' in manifest['selection']['rule'] and 'section 2' in manifest['selection']['rule']


def test_teamhard16_manifest_pins_banner_off_and_assembly_arms(tmp_path, gate_fixture):
    batch = tmp_path / 'pilot_fixture_v16'
    manifest = cli.prepare(batch, wave='teamhard16')
    schedule = manifest['schedule']
    assert len(schedule) == 10
    ids = [e['run_id'] for e in schedule]
    assert len(set(ids)) == 10
    assert [key for key, _ in cli.instance_blocks(schedule)] == [
        'pydata__xarray-7229', 'mwaskom__seaborn-3069', 'sphinx-doc__sphinx-8035']
    for instance_id in ('pydata__xarray-7229', 'mwaskom__seaborn-3069'):
        block = [e for e in schedule if e['instance']['instance_id'] == instance_id]
        assert [e['arm'] for e in block] == ['heterooff_gpt-5.6-terra__glm-5.3',
                                             'hetero_gpt-5.6-terra__glm-5.3'] * 2
        assert [e['rep'] for e in block] == [1, 1, 2, 2]
    assembly = ('cm_team_memory', 'cm_scout_distill', 'cm_bootstrap_package')
    for entry in schedule:
        assert entry['condition'] == 'hetero_team'
        assert entry['worker_models'] == ['gpt-5.6-terra', 'glm-5.3']
        effective = entry['effective_limits']
        # Review revision: the integrating Lead never trips edit detection, so
        # every team arm pins the F5 banner off (plan 20260904C section 3).
        assert entry['limits_override']['edit_status_banner'] is False
        assert effective['edit_status_banner'] is False
        assert effective['cm_edit_curfew'] is True and effective['cm_call_allowance'] == 12
        if entry['arm'].startswith('hetero_'):
            assert all(effective[flag] is True for flag in assembly)
        else:
            assert all(effective[flag] is False for flag in assembly)
    no_pair_holds_two_repeats_of_one_arm(schedule)
    assert manifest['maximum_calls'] == 10 * 28
    assert 'Pre-registered' in manifest['selection']['rule'] and 'section 3' in manifest['selection']['rule']


def test_budget15b_manifest_pins_expanded_budget(tmp_path, gate_fixture):
    batch = tmp_path / 'pilot_fixture_v15b'
    manifest = cli.prepare(batch, wave='budget15b')
    schedule = manifest['schedule']
    assert len(schedule) == 4
    assert len({e['run_id'] for e in schedule}) == 4
    assert [key for key, _ in cli.instance_blocks(schedule)] == [
        'sphinx-doc__sphinx-8035', 'pydata__xarray-7229']
    assert [e['rep'] for e in schedule] == [1, 2, 1, 2]
    for entry in schedule:
        assert entry['condition'] == 'solo' and entry['lead_model'] == 'gpt-5.6-sol'
        assert entry['arm'] == 'solo_gpt-5.6-sol.b900'
        assert entry['limits_override'] == {'max_calls': 40, 'token_limit': 900000}
        effective = entry['effective_limits']
        assert (effective['max_calls'], effective['token_limit']) == (40, 900_000)
        # Only the budget expands; the rev11 mechanism flags stay at defaults.
        assert (effective['cm_edit_curfew'], effective['closing_call_reserve_exempt'],
                effective['edit_status_banner']) == (True, True, True)
        assert effective['cm_call_allowance'] == 12 and effective['cm_max_tokens'] == 4096
    assert manifest['maximum_calls'] == 4 * 40
    assert manifest['sum_token_admission_limits'] == 4 * 900_000
    rule = manifest['selection']['rule']
    assert 'B-branch' in rule and 'section 5' in rule and 'pool-24' in rule
