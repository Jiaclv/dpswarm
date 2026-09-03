"""Offline loop contracts: real protocol, shared budget and CP; no providers/Docker."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import time

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.swe_verified_20260903 import runner, transport
from dpswarm.team_runtime.protocol import ProtocolError, parse_native_response, parse_text_response
from dpswarm.team_runtime.ledger import LedgerError, RunBudget
from dpswarm.control import ControlPlaneError


def call(ident, name, arguments):
    return {'id': ident, 'name': name, 'arguments': arguments}


def reply(*calls, native=True, usage=True, error=None):
    if native:
        assistant = {'role': 'assistant', 'content': None, 'reasoning_content': 'fixture reasoning retained verbatim',
            'tool_calls': [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'],
                'arguments': c['arguments'] if isinstance(c['arguments'], str) else json.dumps(c['arguments'])}}
                for c in calls]}
    else:
        assistant = {'role': 'assistant', 'content': json.dumps({'type': 'tool_calls', 'calls': list(calls)})}
    return {'assistant': assistant, 'usage': usage, 'error': error}


class ScriptedTransport:
    def __init__(self, script):
        self.script, self.requests, self.records = list(script), [], []
        self.lock = threading.Lock()
        self.after_response = None

    def complete(self, model, messages, **kwargs):
        with self.lock:
            self.requests.append({'model': model, 'messages': deepcopy(messages), **kwargs})
            item = self.script.pop(0)
        assistant = deepcopy(item['assistant'])
        native = model.startswith('glm-')
        record = {k: kwargs[k] for k in ('call_id', 'run_id', 'role', 'task_id')}
        record.update(model_requested=model, model_reported=model if native else None,
            assistant_message=assistant, text=assistant.get('content'), error=item['error'], protocol_error=None,
            action=None, history_continuation_safe=transport._native_history_safe(assistant) if native else True,
            wall_seconds=.01, stop_reason='tool_calls' if native else 'completed',
            input_tokens=130 if item['usage'] else None, output_tokens=40 if item['usage'] else None,
            cached_input_tokens=100 if item['usage'] else None, reasoning_tokens=30 if item['usage'] else None,
            total_tokens=170 if item['usage'] else None)
        if item['error']:
            record['assistant_message'] = None
        else:
            try:
                record['action'] = (parse_native_response(assistant, kwargs['tools']) if native
                    else parse_text_response(assistant['content'], kwargs['tools']))
            except ProtocolError as exc:
                record['protocol_error'] = {'code': exc.code, 'message': str(exc)}
        with self.lock:
            self.records.append(record)
        if self.after_response:
            self.after_response()
        return record


class FakeEnvironment:
    def __init__(self, *args, **kwargs):
        self.commands, self.applied, self.grades = [], [], []
        self.patch, self.closed, self.after_command = '', False, None

    def start(self):
        return self

    def run(self, command, **kwargs):
        self.commands.append(command)
        if self.after_command:
            self.after_command()
        return {'exit_code': 0, 'stdout': 'fixture only', 'stderr': ''}

    def apply_patch(self, patch):
        self.applied.append(patch)
        self.patch += patch
        return {'exit_code': 0}

    def export_patch(self, **kwargs):
        return self.patch

    def close(self):
        self.closed = True

    def grade(self, **kwargs):
        self.grades.append(kwargs)
        return {'status': 'fixture', 'resolved': False}


@pytest.fixture
def make_run(tmp_path):
    runs = []
    def make(script=(), condition='dpswarm'):
        scripted, env = ScriptedTransport(script), FakeEnvironment()
        entry = {'run_id': 'fixture-' + str(len(runs)), 'condition': condition,
                 'instance': {'instance_id': 'fixture__repo-1', 'repo': 'fixture/repo',
                              'problem_statement': 'Offline protocol fixture.'}}
        team = runner.SweRun(tmp_path, entry, transport_factory=lambda _: scripted,
                             environment_factory=lambda *a, **k: env)
        runs.append(team)
        return team, scripted, env
    yield make
    for team in runs:
        team.cancel.set()
        for child in team.workers.values():
            child.cancel.set()
        team.pool.shutdown(wait=True)
        team.control.close()


def worker(team, model='glm-5.3', *, submitted=False):
    handle = team.control.delegate(team.control.lead, {'model': model, 'task': 'Bounded offline fixture'})[0]
    team.control.activate(handle)
    child = runner.Worker('worker-' + str(len(team.workers) + 1), handle, {'task': 'Bounded offline fixture'}, '')
    team.workers[child.worker_id] = child
    if submitted:
        patch = 'diff --git a/a.py b/a.py\nfixture patch\n'
        patch_path = team.folder / (child.worker_id + '.patch')
        patch_path.write_bytes(patch.encode('utf-8'))
        team.control.submit(handle, {'status': 'completed', 'patch_path': str(patch_path),
                                     'patch_sha256': runner.sha(patch)})
        child.delivery = {'status': 'completed', 'patch': patch}
        child.future = Future()
        child.future.set_result(child.delivery)
    return child


def history(team, child):
    return json.loads((team.folder / child.worker_id / 'history.json').read_text(encoding='utf-8'))


def test_native_invalid_batch_executes_nothing_and_preserves_paired_history_and_usage(make_run):
    team, scripted, env = make_run([
        reply(call('inspect', 'bash', {'command': 'must not execute'}), call('broken', 'bash', '{invalid')),
        reply(call('done', 'finish', {'status': 'completed', 'summary': 'fixture done'})),
    ])
    child = worker(team)
    outcome = team.loop(child.handle, env, worker=child)
    assert outcome['status'] == 'completed'
    assert env.commands == [] and team.protocol_errors == 1
    second_history = scripted.requests[1]['messages']
    assert scripted.records[0]['assistant_message'] in second_history
    results = [m for m in second_history if m['role'] == 'tool']
    assert [m['tool_call_id'] for m in results] == ['inspect', 'broken']
    assert all(json.loads(m['content'])['executed'] is False for m in results)
    assert team.budget.summary()['total_tokens'] == 340
    assert team.control.get_usage()['total']['total_tokens'] == 340
    assert team.control.get_usage()['by_node'][child.handle.node_id]['cached_input_tokens'] == 200
    assert len({r['call_id'] for r in team.calls}) == 2


@pytest.mark.parametrize('ident', [None, '', '   '])
def test_unpairable_native_history_is_terminal_without_repair_call(make_run, ident):
    team, scripted, env = make_run([reply(call(ident, 'bash', {'command': 'never'}))])
    child = worker(team)
    assert team.loop(child.handle, env, worker=child)['status'] == 'protocol_terminal'
    assert len(scripted.requests) == 1 and env.commands == []
    saved = history(team, child)
    assert saved[-1] == scripted.records[0]['assistant_message']
    assert not any(message['role'] == 'tool' for message in saved)
    assert team.budget.summary()['completed_call_count'] == 1


@pytest.mark.parametrize('stop_when', ['after_response', 'after_first_tool', 'deadline_after_first_tool'])
def test_cancel_or_deadline_stops_later_batch_effects_but_pairs_all_ids(make_run, stop_when):
    team, scripted, env = make_run([reply(
        call('one', 'bash', {'command': 'first'}), call('two', 'bash', {'command': 'second'}),
        call('done', 'finish', {'status': 'completed', 'summary': 'must not finish'}))])
    child = worker(team)
    if stop_when == 'after_response':
        scripted.after_response = child.cancel.set
    elif stop_when == 'after_first_tool':
        env.after_command = child.cancel.set
    else:
        env.after_command = lambda: setattr(team, 'remaining_time', lambda: 0)
    assert team.loop(child.handle, env, worker=child)['status'] == 'cancelled_or_deadline'
    assert env.commands == ([] if stop_when == 'after_response' else ['first'])
    paired = [m for m in history(team, child) if m['role'] == 'tool']
    assert [m['tool_call_id'] for m in paired] == ['one', 'two', 'done']
    assert all(json.loads(m['content']).get('executed') is False for m in paired[len(env.commands):])
    assert len(scripted.requests) == 1
    assert team.budget.summary()['total_tokens'] == team.control.get_usage()['total']['total_tokens'] == 170


def test_two_workers_cannot_race_away_lead_call_reserve(make_run):
    team, scripted, _ = make_run([reply(call('done', 'finish', {'status': 'completed', 'summary': 'fixture'}))])
    children = [worker(team), worker(team, 'glm-5.3-flash')]
    class RacingSummaryBudget(RunBudget):
        def summary(self):
            value = super().summary()
            # Capturing before yielding reproduces the former guard/reserve TOCTOU.
            if threading.current_thread().name.startswith('reserve-race'):
                time.sleep(.02)
            return value
    team.budget = RacingSummaryBudget(max_calls=3, token_limit=600000)
    start = threading.Barrier(2)
    def attempt(child):
        start.wait(timeout=5)
        try:
            return team._call(child.handle, [{'role': 'user', 'content': 'fixture'}], runner.BASE_TOOLS, child.cancel)
        except LedgerError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='reserve-race') as pool:
        results = list(pool.map(attempt, children))
    assert results.count('LEAD_RESERVE') == 1
    assert len(scripted.requests) == 1
    assert team.budget.summary()['remaining_calls'] == 2
    assert team.control.get_usage()['total']['calls'] == 1


@pytest.mark.parametrize('usage', [True, False])
def test_failed_transport_usage_is_recorded_once_and_unknown_usage_stays_unknown(make_run, usage):
    team, scripted, env = make_run([reply(usage=usage, error={'code': 'cancelled', 'message': 'fixture interruption'})])
    child = worker(team)
    assert team.loop(child.handle, env, worker=child)['status'] == 'transport_error'
    record = scripted.records[0]
    team.control.record_call(child.handle, record)  # Idempotent repeat does not create another charge.
    assert len(team.calls) == team.control.get_usage()['total']['calls'] == 1
    budget = team.budget.summary()
    assert budget['completed_call_count'] == 1 and budget['pending_call_count'] == 0
    if usage:
        assert budget['total_tokens'] == 170
        assert team.control.get_usage()['total']['cached_input_tokens'] == 100
    else:
        assert budget['total_tokens'] is None and budget['unknown_call_count'] == 1
        assert budget['reserved_tokens'] > 32768
        assert team.control.get_usage()['total']['total_tokens'] is None
        assert team.control.get_usage()['total']['unknown_counts']['total_tokens'] == 1
    assert env.commands == []


@pytest.mark.parametrize('reason', ['', '   '])
def test_invalid_adoption_reason_is_rejected_before_applying_patch(make_run, reason):
    team, _, env = make_run()
    child = worker(team, submitted=True)
    args = {'worker_id': child.worker_id, 'decision': 'adopt', 'reason': reason}
    with pytest.raises(ProtocolError):
        parse_text_response(json.dumps({'type': 'tool_calls', 'calls': [call('adopt', 'review_worker', args)]}), runner.LEAD_TOOLS)
    # Public CP preflight also protects callers that bypass the text parser.
    with pytest.raises(LedgerError):
        team.execute_tool('review_worker', args, team.control.lead, env, None)
    assert env.applied == [] and not child.reviewed
    assert team.control.snapshot()['agents'][child.handle.node_id]['status'] == 'submitted'


def test_changed_worker_patch_is_rejected_before_application(make_run):
    team, _, env = make_run()
    child = worker(team, submitted=True)
    child.delivery['patch'] += 'changed after submission\n'
    with pytest.raises(ControlPlaneError) as caught:
        team.execute_tool('review_worker', {'worker_id': child.worker_id, 'decision': 'adopt',
            'reason': 'fixture adoption'}, team.control.lead, env, None)
    assert caught.value.code == 'PATCH_HASH_MISMATCH'
    assert env.applied == [] and not child.reviewed


def test_cp_failure_after_apply_stops_batch_and_prohibits_grading(make_run, monkeypatch):
    team, scripted, env = make_run()
    child = worker(team, submitted=True)
    scripted.script.append(reply(
        call('adopt', 'review_worker', {'worker_id': child.worker_id, 'decision': 'adopt', 'reason': 'fixture'}),
        call('later', 'bash', {'command': 'must never execute'}),
        call('done', 'finish', {'status': 'completed', 'summary': 'must not complete'}), native=False))
    def fail_commit(*args, **kwargs):
        raise RuntimeError('fixture CP commit failure after preflight')
    monkeypatch.setattr(team.control, 'decide', fail_commit)
    result = team.run()
    assert len(env.applied) == 1 and env.commands == [] and env.grades == []
    assert result['infrastructure_error']['type'] == 'FatalRuntimeError'
    assert result['score'] is result['cp_result'] is None
    assert result['call_count'] == 1 and result['budget']['total_tokens'] == 170
    assert not child.reviewed and env.closed
    events = [json.loads(line) for line in (team.folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert not any(event['event'] == 'patch_frozen' for event in events)
    assert not any(event.get('tool_call', {}).get('id') == 'later' for event in events)


def test_unadopted_worker_cleanup_is_explicit_and_grades_only_actual_lead_patch(make_run):
    team, scripted, env = make_run()
    child = worker(team, submitted=True)
    team.budget = RunBudget(max_calls=0, token_limit=600000)
    result = team.run()
    assert result['outcome']['status'] == 'budget_exhausted'
    assert result['infrastructure_error'] is None and result['cp_result']['control_completed']
    assert env.applied == [] and len(env.grades) == 1 and env.grades[0]['patch'] == ''
    assert child.reviewed and len(scripted.requests) == 0
    snapshot = team.control.snapshot()
    assert snapshot['agents'][child.handle.node_id]['status'] == 'discarded'
    events = [json.loads(line) for line in (team.folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    cleanups = [event for event in events if event['event'] == 'worker_cleanup_discarded']
    assert len(cleanups) == 1 and cleanups[0]['model_reviewed'] is False
