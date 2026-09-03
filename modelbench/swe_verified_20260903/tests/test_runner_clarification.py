"""Short offline clarification integration checks; real CP, no model or Docker."""
from concurrent.futures import Future, ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.swe_verified_20260903 import runner
from modelbench.swe_verified_20260903.tests.test_runner_protocol import (
    call, history, make_run, reply, worker,
)


def wait_questions(team, count=1):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        questions = team.pending_questions()
        if len(questions) == count:
            return questions
        time.sleep(.005)
    pytest.fail('Expected pending clarification did not appear within the fixture deadline')


def events(team, name):
    return [event for line in (team.folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()
            if (event := json.loads(line))['event'] == name]


def answer(team, env, qid, text):
    return team.execute_tool('reply_worker', {'question_id': qid, 'answer': text},
                             team.control.lead, env, None)


def test_worker_continues_same_native_history_and_lead_next_boundary_sees_question(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'question_timeout', 2)
    team, scripted, env = make_run([
        reply(call('before', 'bash', {'command': 'before-question'}),
              call('clarify', 'ask_lead', {'question': 'Which fixture option?'})),
        reply(native=False, error={'code': 'fixture_stop', 'message': 'Observe Lead banner only'}),
        reply(call('after', 'bash', {'command': 'after-answer'}),
              call('done', 'finish', {'status': 'completed', 'summary': 'Offline evidence'})),
    ])
    child = worker(team)
    child.future = team.pool.submit(team.loop, child.handle, env, worker=child)
    q = wait_questions(team)[0]
    assert q['worker_id'] == child.worker_id and not child.future.done()
    assert env.commands == ['before-question']

    assert team.loop(team.control.lead, env)['status'] == 'transport_error'
    lead_request = next(request for request in scripted.requests if request['role'] == 'lead')
    banner = json.loads(lead_request['messages'][-1]['content'].split('\n', 1)[1])
    assert banner['pending_questions'] == [q]
    assert answer(team, env, q['question_id'], 'Use option A')['delivered'] is True
    assert child.future.result(timeout=2)['status'] == 'completed'
    assert answer(team, env, q['question_id'], 'Do not deliver twice') == {'error': 'QUESTION_NOT_PENDING'}

    requests = [request for request in scripted.requests if request['role'] == 'worker']
    assert len(requests) == 2
    original_assistant = scripted.records[0]['assistant_message']
    assert original_assistant in requests[1]['messages']
    assert original_assistant['reasoning_content'] == 'fixture reasoning retained verbatim'
    feedback = [message for message in requests[1]['messages']
                if message['role'] == 'tool' and message['tool_call_id'] == 'clarify']
    assert len(feedback) == 1
    assert json.loads(feedback[0]['content']) == {
        'question_id': q['question_id'], 'answer': 'Use option A', 'status': 'answered'}
    assert env.commands == ['before-question', 'after-answer']
    assert len(events(team, 'clarification_requested')) == len(events(team, 'clarification_answered')) == 1
    assert events(team, 'clarification_expired') == []
    assert team.pending_questions() == []
    assert team.budget.summary()['completed_call_count'] == team.control.get_usage()['total']['calls'] == 3
    assert len({record['call_id'] for record in team.calls}) == 3
    assert len([m for m in history(team, child) if m.get('tool_call_id') == 'clarify']) == 1


def test_collect_already_waiting_wakes_early_for_new_question(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'question_timeout', 2)
    team, _, env = make_run()
    child = worker(team)
    child.future = Future()  # The worker delivery has not settled.
    entered_wait = threading.Event()
    real_sleep, real_monotonic, real_time = time.sleep, time.monotonic, time.time
    def brief_sleep(seconds):
        entered_wait.set()
        real_sleep(min(seconds, .01))
    monkeypatch.setattr(runner, 'time', SimpleNamespace(
        monotonic=real_monotonic, time=real_time, sleep=brief_sleep))
    with ThreadPoolExecutor(max_workers=2) as pool:
        began = time.monotonic()
        collected = pool.submit(team.execute_tool, 'collect',
            {'worker_id': child.worker_id, 'wait_seconds': 5}, team.control.lead, env, None)
        assert entered_wait.wait(timeout=1)
        asked = pool.submit(team.execute_tool, 'ask_lead', {'question': 'Need bounded context'},
                            child.handle, env, child)
        q = wait_questions(team)[0]
        result = collected.result(timeout=1)
        assert time.monotonic() - began < 2  # It did not consume the requested 5-second collect wait.
        assert result == {'worker_id': child.worker_id, 'status': 'running', 'pending_questions': [q]}
        assert not asked.done() and not child.future.done()
        answer(team, env, q['question_id'], 'Fixture context')
        assert asked.result(timeout=1)['answer'] == 'Fixture context'
    assert len(events(team, 'clarification_answered')) == 1


def test_two_workers_receive_only_their_own_reply_and_repeat_is_rejected(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'question_timeout', 2)
    team, _, env = make_run()
    first, second = worker(team), worker(team, 'glm-5.3-flash')
    for child in (first, second):
        child.future = team.pool.submit(team.execute_tool, 'ask_lead',
            {'question': 'Context for ' + child.worker_id}, child.handle, env, child)
    by_worker = {q['worker_id']: q for q in wait_questions(team, 2)}
    one, two = by_worker[first.worker_id], by_worker[second.worker_id]
    assert one['question_id'] != two['question_id']
    answer(team, env, one['question_id'], 'FIRST_ONLY')
    assert first.future.result(timeout=1) == {
        'question_id': one['question_id'], 'answer': 'FIRST_ONLY', 'status': 'answered'}
    assert not second.future.done()
    assert team.questions[two['question_id']]['answer'] is None
    assert team.pending_questions() == [two]
    answer(team, env, two['question_id'], 'SECOND_ONLY')
    assert second.future.result(timeout=1) == {
        'question_id': two['question_id'], 'answer': 'SECOND_ONLY', 'status': 'answered'}
    assert answer(team, env, one['question_id'], 'DUPLICATE') == {'error': 'QUESTION_NOT_PENDING'}
    delivered = events(team, 'clarification_answered')
    assert {(event['worker_id'], event['answer']) for event in delivered} == {
        (first.worker_id, 'FIRST_ONLY'), (second.worker_id, 'SECOND_ONLY')}
    assert len(delivered) == 2 and team.pending_questions() == [] and env.commands == []


def test_expired_reply_is_rejected_without_resuming_worker_or_delivering_answer(make_run, monkeypatch):
    monkeypatch.setitem(runner.LIMITS, 'question_timeout', .01)
    team, _, env = make_run()
    child = worker(team)
    result = team.execute_tool('ask_lead', {'question': 'Will expire'}, child.handle, env, child)
    qid = result['question_id']
    assert result == {'question_id': qid, 'error': 'CLARIFICATION_EXPIRED', 'answer': None}
    assert team.pending_questions() == []
    assert answer(team, env, qid, 'Too late') == {'error': 'QUESTION_NOT_PENDING'}
    assert team.questions[qid]['answer'] is None
    assert not team.questions[qid]['event'].is_set()
    assert len(events(team, 'clarification_expired')) == 1
    assert events(team, 'clarification_answered') == []


def test_reply_accepted_at_wait_boundary_is_not_misreported_expired(make_run, monkeypatch):
    """Deterministically interleave accepted reply between timed wait and expiry check."""
    team, _, env = make_run()
    child = worker(team)
    monkeypatch.setitem(runner.LIMITS, 'question_timeout', .02)
    monkeypatch.setattr(team, 'remaining_time', lambda: 100)
    clock = {'now': 10.0}
    accepted = []
    class BoundaryEvent:
        def __init__(self):
            self.set_count = 0

        def set(self):
            self.set_count += 1

        def wait(self, timeout):
            qid = next(iter(team.questions))
            clock['now'] = 10.019
            accepted.append(answer(team, env, qid, 'Accepted before expiry'))
            clock['now'] = 10.021
            # A timed wait can have returned False just before the reply wakes
            # this thread; the next loop condition now sees an elapsed deadline.
            return False

    monkeypatch.setattr(runner, 'threading', SimpleNamespace(Event=BoundaryEvent))
    monkeypatch.setattr(runner, 'time', SimpleNamespace(
        monotonic=lambda: clock['now'], time=time.time, sleep=time.sleep))
    result = team.execute_tool('ask_lead', {'question': 'Boundary fixture'}, child.handle, env, child)
    qid = result['question_id']
    assert accepted == [{'question_id': qid, 'delivered': True}]
    assert result == {'question_id': qid, 'answer': 'Accepted before expiry', 'status': 'answered'}
    assert team.questions[qid]['event'].set_count == 1
    assert len(events(team, 'clarification_answered')) == 1
    assert events(team, 'clarification_expired') == []
