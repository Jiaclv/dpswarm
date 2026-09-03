"""CM (context manager) integration tests: code-triggered compression, real
call accounting, attribution by event, and silent degradation. Offline only."""
import json
from pathlib import Path
import sys
import threading
from copy import deepcopy

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_fixed_team_20260903 import runner
from modelbench.swe_fixed_team_20260903.tests.test_runner import (
    action, done, events, make_run, settle)

CM_TEXT = '## 目标\nzero-loss fixture summary\n## 已验收决定与不变量\n- fact preserved'


def install_cm_transport(run, *, error=None, text=CM_TEXT, seen_cm=None):
    """Route role='cm' calls to a fixture record; everything else unchanged."""
    original = run.transport.complete

    def complete(model, messages, *, role, call_id, **kwargs):
        if role != 'cm':
            return original(model, messages, role=role, call_id=call_id, **kwargs)
        if seen_cm is not None:
            seen_cm.append({'call_id': call_id, 'messages': deepcopy(messages),
                            'max_tokens': kwargs.get('max_tokens')})
        return {'call_id': call_id, 'model_requested': model, 'run_id': kwargs['run_id'],
                'role': 'cm', 'task_id': kwargs['task_id'],
                'input_tokens': 900, 'output_tokens': 100, 'total_tokens': 1000,
                'cached_input_tokens': 0, 'reasoning_tokens': 10,
                'error': error, 'protocol_error': None, 'wall_seconds': 0.01,
                'transport_attempt_count': 1, 'stop_reason': 'fixture',
                'assistant_message': None if error else {'role': 'assistant', 'content': text},
                'action': None if error else {'kind': 'no_action', 'calls': [], 'text': text}}

    run.transport.complete = complete


def cm_events(run, name):
    return [e for e in events(run) if e['event'] == name]


def test_cm_not_triggered_below_threshold(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_scout_distill', False)  # old fully-parallel protocol
    monkeypatch.setitem(runner.LIMITS, 'cm_team_memory', False)
    monkeypatch.setitem(runner.LIMITS, 'cm_bootstrap_package', False)
    run, env, _ = make_run()
    result = run.run()
    assert result['infrastructure_error'] is None
    assert result['cm_call_count'] == 0
    assert not [e for e in events(run) if e['event'].startswith('cm_')]
    assert run.cm_calls == []


def test_cm_compresses_over_budget_history_without_breaking_the_loop(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    scripts = {'worker-1': [[action('bash', command='edit-production')]] * 2 + [[done()]]}
    run, env, _ = make_run(workers=scripts)
    seen_cm = []
    install_cm_transport(run, seen_cm=seen_cm)
    result = run.run()
    assert result['infrastructure_error'] is None
    # Revision 7: scout distill + possible Lead compression join worker compressions.
    started = cm_events(run, 'cm_call_started')
    settled = cm_events(run, 'cm_call_settled')
    compression = cm_events(run, 'cm_compression')
    assert len(run.cm_calls) == result['cm_call_count'] == len(settled) == len(started)
    worker_started = [e for e in started if e['trigger']['role'] == 'worker']
    assert worker_started and all(e['trigger']['agent'] == 'worker-1' for e in worker_started)
    assert any(e['trigger'].get('phase') == 'scout_distill' for e in started)
    assert len(compression) >= 1 and all(c['after_est_tokens'] < c['before_est_tokens'] for c in compression)
    assert len(cm_events(run, 'scout_distilled')) == 1
    # CM call is in the budget but not in any agent's call list.
    assert result['cm_call_ids'][0] not in [cid for usage in result['agent_usage'].values()
                                            for cid in usage['call_ids']]
    assert result['budget']['completed_call_count'] == result['call_count'] + result['cm_call_count']
    # The compressed summary replaced the middle of the history for later calls.
    later = [s for s in run.transport.seen if s['actor'] != 'lead']
    assert any('Context summary' in json.dumps(s['messages'], ensure_ascii=False) for s in later)
    assert seen_cm and seen_cm[0]['max_tokens'] == runner.LIMITS['cm_max_tokens'] == 2048
    worker1_delivery = run.workers['worker-1'].delivery
    assert worker1_delivery['status'] == 'completed'


def test_cm_failure_degrades_silently_to_uncompressed_history(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    scripts = {'worker-1': [[action('bash', command='edit-production')]] * 2 + [[done()]]}
    run, env, _ = make_run(workers=scripts)
    install_cm_transport(run, error={'type': 'TransportError', 'code': 'socket_timeout', 'message': 'fixture'})
    result = run.run()
    assert result['infrastructure_error'] is None
    assert result['cm_call_count'] >= 1  # failed CM calls are still accounted
    assert cm_events(run, 'cm_compression') == []
    assert len(cm_events(run, 'cm_call_failed')) >= 1
    later = json.dumps([s for s in run.transport.seen], ensure_ascii=False)
    assert 'Context summary' not in later
    assert run.workers['worker-1'].delivery['status'] == 'completed'


def test_cm_skips_when_call_budget_is_low(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'cm_context_budget', 200)
    monkeypatch.setitem(runner.LIMITS, 'cm_keep_recent', 2)
    scripts = {'worker-1': [[action('bash', command='edit-production')]] * 2 + [[done()]]}
    run, env, _ = make_run(workers=scripts)
    install_cm_transport(run)
    real_summary = run.budget.summary
    monkeypatch.setattr(run.budget, 'summary',
                        lambda *a, **k: {**real_summary(), 'remaining_calls': 3})
    result = run.run()
    assert result['infrastructure_error'] is None
    assert cm_events(run, 'cm_compression') == []
    assert len(cm_events(run, 'cm_skipped')) >= 1
