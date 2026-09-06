"""Revision 9 tests: per-arm limits_override, CM call allowance pool,
assembly-ablation arm, and budget snapshot compatibility. Offline only."""
import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_fixed_team_20260903 import cli, runner  # runner.py inserts dpswarm-plugin into sys.path
from dpswarm.team_runtime.ledger import LedgerError, RunBudget, digest
from modelbench.swe_fixed_team_20260903.tests.test_runner import action, done, events, make_run

ABLATION = {'cm_team_memory': False, 'cm_scout_distill': False, 'cm_bootstrap_package': False}


def call_record(call_id, inp=100, out=20):
    return {'call_id': call_id, 'role': 'lead', 'input_tokens': inp, 'output_tokens': out,
            'total_tokens': inp + out, 'cached_input_tokens': 0, 'reasoning_tokens': 0}


# ---------------------------------------------------------------- ledger pool

def test_cm_allowance_moves_cm_tickets_out_of_max_calls():
    budget = RunBudget(2, 10_000, cm_call_allowance=2)
    budget.reserve('w1', 'worker', 1000)
    budget.reserve('w2', 'lead', 1000)
    assert budget.summary()['remaining_calls'] == 0
    budget.reserve('cm1', 'cm', 500)  # still admitted: own pool, shared tokens
    budget.complete('cm1', call_record('cm1') | {'role': 'cm'})
    status = budget.summary()
    assert status['cm_call_count'] == 1 and status['remaining_cm_calls'] == 1
    assert status['remaining_calls'] == 0  # CM spent no max_calls slot
    assert status['remaining_tokens'] == 10_000 - 1000 - 1000 - 120  # held 2x1000 + settled cm 120
    budget.reserve('cm2', 'cm', 500)
    with pytest.raises(LedgerError) as exhausted:
        budget.reserve('cm3', 'cm', 100)
    assert exhausted.value.code == 'CM_CALL_BUDGET_EXHAUSTED'


def test_without_allowance_cm_still_spends_a_max_calls_slot():
    budget = RunBudget(1, 10_000)
    budget.reserve('w1', 'worker', 1000)
    with pytest.raises(LedgerError) as exhausted:
        budget.reserve('cm1', 'cm', 100)
    assert exhausted.value.code == 'CALL_BUDGET_EXHAUSTED'
    status = budget.summary()
    assert status['cm_call_allowance'] is None and status['cm_call_count'] == 0
    assert status['remaining_cm_calls'] is None


def test_v2_snapshot_roundtrip_and_v1_snapshot_stay_compatible():
    budget = RunBudget(3, 10_000, cm_call_allowance=5)
    budget.reserve('w1', 'worker', 1000)
    budget.complete('w1', call_record('a-1') | {'role': 'worker'})
    budget.reserve('cm1', 'cm', 500)
    budget.complete('cm1', call_record('a-2') | {'role': 'cm'})
    restored = RunBudget.from_snapshot(budget.snapshot())
    assert restored.summary() == budget.summary()
    assert restored.summary()['cm_call_count'] == 1 and restored.summary()['remaining_calls'] == 2
    # A version-1 snapshot (any pre-rev9 batch) counts CM tickets against
    # max_calls; restoring it must keep that historical accounting.
    legacy = {'version': 1, 'max_calls': 2, 'token_limit': 10_000, 'created_at': 1.0,
              'deadline_at': None, 'frozen': True,
              'tickets': {'w1': {'ticket_id': 'w1', 'role': 'worker', 'reserved_tokens': 1000,
                                 'status': 'completed', 'reserved_at': 1.0, 'call_id': 'a-1'},
                          'cm1': {'ticket_id': 'cm1', 'role': 'cm', 'reserved_tokens': 500,
                                  'status': 'completed', 'reserved_at': 1.0, 'call_id': 'a-2'}}}
    for ticket, call_id, role in (('w1', 'a-1', 'worker'), ('cm1', 'a-2', 'cm')):
        record = call_record(call_id) | {'role': role}
        legacy['tickets'][ticket]['record'] = record
        legacy['tickets'][ticket]['record_hash'] = digest(record)
        legacy['tickets'][ticket]['usage'] = RunBudget._usage(record)
    legacy = {**legacy, 'snapshot_hash': digest(legacy)}
    old = RunBudget.from_snapshot(legacy)
    assert old.cm_call_allowance is None
    assert old.summary()['remaining_calls'] == 0  # legacy: CM spent a max_calls slot
    assert old.summary()['cm_call_count'] == 0


# ------------------------------------------------------------------- runner

def test_limits_override_merges_into_run_and_journals_effective_limits(make_run):
    override = {'worker_calls': 3, 'max_calls': 12, 'lead_reserve_calls': 1, 'cm_call_allowance': 4}
    run, env, _ = make_run(limits_override=override)
    assert run.limits == {**runner.LIMITS, **override}
    started = [e for e in events(run) if e['event'] == 'run_started'][0]
    assert started['limits'] == run.limits
    result = run.run()
    assert result['infrastructure_error'] is None
    worker_prompt = [s for s in run.transport.seen if s['actor'] == 'worker-1'][0]['messages'][0]['content']
    assert 'at most 3 model calls' in worker_prompt
    assert result['budget']['cm_call_allowance'] == 4  # runner wired the pool through


def test_unknown_limit_key_is_rejected_before_any_run_resources(tmp_path):
    entry = {'run_id': 'fixture-bad', 'condition': 'solo', 'instance': {'instance_id': 'sympy__sympy-1',
             'repo': 'sympy/sympy', 'problem_statement': 'x'}, 'arm': 'solo', 'worker_model': None,
             'worker_models': [], 'lead_model': 'gpt-5.6-sol',
             'limits_override': {'not_a_limit': 1}}
    with pytest.raises(ValueError, match='limits_override'):
        runner.SweRun(tmp_path, entry)
    assert list(tmp_path.iterdir()) == []


def test_lead_reserve_default_and_limit_key_are_pinned():
    assert runner.LIMITS['lead_reserve_calls'] == 2
    assert runner.LIMITS['cm_call_allowance'] == 12


def test_ablation_arm_cold_starts_workers_and_keeps_on_demand_cm(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)  # force on-demand compressions
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    run, env, _ = make_run(condition='hetero_team', worker_models=['gpt-5.6-terra', 'glm-5.3'],
                           workers={'worker-1': [[action('bash', command='edit-production')] * 3 + [done()]]},
                           limits_override=ABLATION)
    result = run.run()
    assert result['infrastructure_error'] is None
    kinds = [e['event'] for e in events(run)]
    for absent in ('scout_round_completed', 'scout_distilled', 'cm_assembly'):
        assert absent not in kinds, absent + ' must not run on the ablation arm'
    assert not (run.folder / 'memory.jsonl').exists()
    assert not (run.folder / 'worker-1' / 'context_package').exists()
    # Cold start: the worker's first non-system message is a runtime banner,
    # not an assembled context package.
    first = [s for s in run.transport.seen if s['actor'] == 'worker-1'][0]
    assert first['messages'][1]['content'].startswith('Runtime status')
    # On-demand compression still works and now spends the independent pool.
    assert result['cm_status'] == 'integrated_on_demand'
    compressions = [e for e in events(run) if e['event'] == 'cm_compression']
    assert compressions and result['cm_call_count'] >= 1
    assert result['budget']['cm_call_count'] == result['cm_call_count']
    assert result['budget']['remaining_calls'] == run.limits['max_calls'] - result['call_count']


def test_cm_allowance_exhaustion_degrades_silently(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    run, env, _ = make_run(condition='hetero_team',
                           workers={'worker-1': [[action('bash', command='edit-production')] * 3 + [done()]]},
                           limits_override={**ABLATION, 'cm_call_allowance': 0})
    result = run.run()
    assert result['infrastructure_error'] is None
    stream = events(run)
    denied = [e for e in stream if e['event'] == 'cm_call_not_admitted']
    assert denied and all('CM_CALL_BUDGET_EXHAUSTED' in e['reason'] for e in denied)
    assert result['cm_call_count'] == 0
    assert run.workers['worker-1'].delivery['status'] == 'completed'
    assert result['team_execution_status'] == 'workers_completed'


# ---------------------------------------------------------------------- cli

@pytest.mark.parametrize('wave,expected', [
    ('ablation9', {'runs': 9, 'arms': ['solo_gpt-5.6-sol', 'hetero_gpt-5.6-terra__glm-5.3',
                                       'heterooff_gpt-5.6-terra__glm-5.3']}),
    ('budget11', {'runs': 7, 'arms': ['fixed_glm-5.3', 'hetero_gpt-5.6-terra__glm-5.3',
                                      'fixed_gpt-5.6-sol', 'fixed_deepseek-v4-flash']}),
])
def test_rev9_wave_manifests_freeze_overrides_and_reps(tmp_path, monkeypatch, wave, expected):
    gate = {'status': 'PASS', 'runtime_sources': cli.sources(), 'validation_artifacts': {}}
    gate_path = tmp_path / 'gate_fixture.json'
    gate_path.write_text(json.dumps(gate), encoding='utf-8')
    monkeypatch.setattr(cli, 'GATE_PATH', gate_path)
    batch = tmp_path / ('pilot_fixture_' + wave)
    manifest = cli.prepare(batch, only_instances=['sphinx-doc__sphinx-8035'], wave=wave)
    schedule = manifest['schedule']
    assert len(schedule) == expected['runs']
    assert list(dict.fromkeys(e['arm'] for e in schedule)) == expected['arms']
    ids = [e['run_id'] for e in schedule]
    assert len(set(ids)) == len(ids)
    reps = {}
    for entry in schedule:
        reps[entry['arm']] = reps.get(entry['arm'], 0) + 1
        assert entry['effective_limits'] == {**runner.LIMITS, **(entry.get('limits_override') or {})}
        if entry['arm'].startswith('heterooff_'):
            assert entry['condition'] == 'hetero_team' and len(entry['worker_models']) == 2
            assert (entry.get('limits_override') or {}) == cli.ABLATION9_OVERRIDE
    if wave == 'ablation9':
        assert reps == {'solo_gpt-5.6-sol': 3, 'hetero_gpt-5.6-terra__glm-5.3': 3,
                        'heterooff_gpt-5.6-terra__glm-5.3': 3}
        assert manifest['maximum_calls'] == 9 * 28 and manifest['sum_token_admission_limits'] == 9 * 600_000
    else:
        assert reps == {'fixed_glm-5.3': 2, 'hetero_gpt-5.6-terra__glm-5.3': 2,
                        'fixed_gpt-5.6-sol': 2, 'fixed_deepseek-v4-flash': 1}
        assert manifest['maximum_calls'] == 7 * 40 and manifest['sum_token_admission_limits'] == 7 * 900_000
        for entry in schedule:
            effective = entry['effective_limits']
            assert (effective['max_calls'], effective['token_limit'],
                    effective['worker_calls'], effective['cm_call_allowance']) == (40, 900_000, 16, 12)
    # Same-arm repeats never share a pair (staggered execution, plan §4.4).
    pairs = [set(ids[i:i + 2]) for i in range(0, len(ids), 2)]
    for pair in pairs:
        assert len({next(e['arm'] for e in schedule if e['run_id'] == i) for i in pair}) == len(pair), \
            'a pair contains two runs of one arm'
