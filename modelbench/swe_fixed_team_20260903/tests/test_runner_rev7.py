"""Revision 7 tests: team CM (Lead->worker one-way assembly), scout ordering,
hetero worker models and solo model routing. Offline only."""
import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_fixed_team_20260903 import runner
from modelbench.swe_fixed_team_20260903.tests.test_runner import action, done, events, make_run


def test_scout_distilled_before_workers_and_packages_assembled(make_run):
    run, env, _ = make_run()
    result = run.run()
    assert result['infrastructure_error'] is None
    sequence = [e['event'] for e in events(run)]
    assert sequence.index('scout_round_completed') < sequence.index('scout_distilled')
    assert sequence.index('scout_distilled') < sequence.index('workers_started_after_scout')
    assert sequence.index('workers_started_after_scout') < sequence.index('worker_activated')
    memory_lines = [json.loads(l) for l in (run.folder / 'memory.jsonl').read_text(encoding='utf-8').splitlines()]
    kinds = [m['event'] for m in memory_lines]
    assert 'memory_candidate' in kinds and 'memory_promoted' in kinds
    assert all(str(m.get('scope', '')).startswith('team:' + run.run_id) for m in memory_lines)
    for worker_id in ('worker-1', 'worker-2'):
        package_dir = run.folder / worker_id / 'context_package'
        manifests = list(package_dir.glob('*.manifest.json'))
        assert manifests, worker_id + ' has no assembled context package'
        manifest = json.loads(manifests[0].read_text(encoding='utf-8'))
        assert manifest['entries'] and manifest['content_sha256']
        body = manifests[0].with_name(manifest['content_file']).read_text(encoding='utf-8')
        assert 'scout note' in body  # the distilled Lead note reached the worker
    # The package is the first thing each worker sees after its system prompt.
    worker_first = [s for s in run.transport.seen if s['actor'] == 'worker-1'][0]
    assert 'scout note' in worker_first['messages'][1]['content']


def test_team_memory_is_one_way_lead_writes_workers_never_do(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)  # force compressions
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    run, env, _ = make_run(workers={'worker-1': [[action('bash', command='edit-production')]] * 2 + [[done()]]})
    result = run.run()
    assert result['infrastructure_error'] is None
    memory_lines = [json.loads(l) for l in (run.folder / 'memory.jsonl').read_text(encoding='utf-8').splitlines()]
    candidates = [m for m in memory_lines if m['event'] == 'memory_candidate']
    assert candidates and all(m.get('accepted_by') == 'lead' for m in candidates), \
        'only the Lead may promote team memory (one-way)'


def test_worker_compression_pulls_unseen_team_memory(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    run, env, _ = make_run(workers={'worker-1': [[action('bash', command='edit-production')]] * 3 + [[done()]]})
    result = run.run()
    assert result['infrastructure_error'] is None
    compressions = [e for e in events(run) if e['event'] == 'cm_compression'
                    and e['trigger'].get('agent') == 'worker-1']
    assert compressions
    later = json.dumps([s for s in run.transport.seen if s['actor'] == 'worker-1'], ensure_ascii=False)
    assert 'Context summary' in later


def test_hetero_team_assigns_each_worker_its_model(make_run):
    run, env, _ = make_run(condition='hetero_team', worker_models=['glm-5.3', 'gpt-5.6-terra'])
    result = run.run()
    assert result['infrastructure_error'] is None
    admitted = {e['worker_id']: e['handle']['model'] for e in events(run) if e['event'] == 'worker_admitted'}
    assert admitted == {'worker-1': 'glm-5.3', 'worker-2': 'gpt-5.6-terra'}
    worker_models_seen = {s['actor']: s['model'] for s in run.transport.seen if s['actor'].startswith('worker-')}
    assert set(worker_models_seen.values()) == {'glm-5.3', 'gpt-5.6-terra'}
    assert result['team_execution_status'] == 'workers_completed'


def test_solo_run_can_use_any_catalog_model(make_run):
    run, env, _ = make_run(condition='solo', model='glm-5.3', lead_model='glm-5.3',
                           lead=[[action('bash', command='edit-lead'), done()]])
    result = run.run()
    assert result['infrastructure_error'] is None
    lead_seen = [s for s in run.transport.seen if s['actor'] == 'lead']
    assert lead_seen and all(s['model'] == 'glm-5.3' for s in lead_seen)
    assert result['fixed_team_requested'] is False and result['cm_call_count'] == 0
