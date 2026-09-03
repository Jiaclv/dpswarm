"""Scripted integration checks; real CP/store, no model, Docker, or grader."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.team_runtime_v2.tests import test_runner as support
from modelbench.team_runtime_v2.revisions.review_fixes import ReviewFixedTeamRun
from modelbench.team_runtime_v2 import recovery
from modelbench.team_runtime_v2 import runner
from modelbench.team_runtime_v2.paths import RecoveryRequired
from dpswarm.team_runtime.ledger import ExecutionStore


make_run = support.make_run


class OfflineReviewTeamRun(ReviewFixedTeamRun):
    def grade(self):
        # Before offline cleanup, every created role has submitted evidence but
        # none is accepted. A finish tool or this fixture cannot grant success.
        handles = list(self.control._handles.values())
        assert set(self.control._submissions) == {handle.item_id for handle in handles}
        assert all(self.control.cp.proj.work_items[h.item_id].acceptance.value == 'submitted'
                   for h in handles)
        assert self.data['inflight'] is None and self.data['suspended'] is None
        return support.OfflineTeamRun.grade(self)


@pytest.fixture
def make_review_run(make_run):
    def create(script):
        template, transport, contract = make_run(script)
        team = OfflineReviewTeamRun(template.root, template.entry, template.manifest,
                                    transport=transport, sandbox_factory=support.FakeSandbox)
        return team, transport, contract
    create.contract = make_run.contract
    return create


def verifier_fail():
    return support.calls(support.action('write', {
        'path': '/shared/submission/attestation.json',
        'content': json.dumps({'verdict': 'fail', 'evidence': ['offline fixture']}),
    }), support.finish())


def test_live_clarification_keeps_same_executor_and_bounded_accounting(make_review_run):
    saved = {}

    def ask(invocation):
        saved['handle'] = deepcopy(team.data['current']['handle'])
        return support.calls(support.action('request_clarification', support.QUESTION))

    def resumed(invocation):
        assert team.data['current']['handle'] == saved['handle']
        assert sum(support.ANSWER in (message.get('content') or '')
                   for message in invocation['messages']) == 1
        return support.calls(support.finish())

    team, transport, _ = make_review_run([
        ('planner', support.planner_done(make_review_run.contract)), ('executor', ask),
        ('planner', support.clarification_done()), ('executor', resumed),
        ('verifier', support.verifier_done()),
    ])
    result = support.successful_result(team, transport)
    assert result['review_fixes']['clarification_failures'] == []
    assert result['review_fixes']['terminal_roles'] == {}
    assert result['budget']['call_count'] == result['call_count'] == 5
    request = result['clarifications']['requests'][support.QUESTION['request_id']]
    assert request['state'] == 'RESUMED' and request['resume_count'] == 1
    assert support.phase(result, 'executor')['calls'] == 2


@pytest.mark.parametrize('when', ['before_admission', 'after_cp_creation'])
def test_expired_admission_submits_created_items_and_settles_same_executor(make_review_run, when):
    saved = {}
    team, transport, _ = make_review_run([
        ('planner', support.planner_done(make_review_run.contract)),
        ('executor', support.calls(support.action('request_clarification', support.QUESTION))),
        ('verifier', support.verifier_done()),
    ])
    original_settle = team.settle

    def settle():
        phase = team.data['current']
        if phase and phase.get('pending_question'):
            saved['executor_handle'] = deepcopy(phase['handle'])
            if when == 'before_admission':
                expires = team.scheduler.get(phase['pending_question'])['expires_at']
                team.scheduler.clock = lambda: expires + 1
        return original_settle()

    team.settle = settle
    original_start = team.start_phase

    def start(role, name, prompt, *, request_id=None):
        if name == 'clarification' and when == 'after_cp_creation':
            admit = team.scheduler.admit

            def expire_then_admit(ident, context):
                assert team.data['current']['phase'] == 'clarification'
                expires = team.scheduler.get(ident)['expires_at']
                team.scheduler.clock = lambda: expires + 1
                return admit(ident, context)

            team.scheduler.admit = expire_then_admit
        return original_start(role, name, prompt, request_id=request_id)

    team.start_phase = start
    result = support.successful_result(team, transport)
    executor = support.phase(result, 'executor')
    assert executor['handle'] == saved['executor_handle']
    assert executor['status'] == 'clarification_failed' and executor['calls'] == 1
    assert executor['pending_question'] is None
    request = result['clarifications']['requests'][support.QUESTION['request_id']]
    assert request['state'] == 'FAILED' and request['failure_reason'] == 'expired'
    assert request['resume_count'] == 0 and request['reply_call_count'] == 0
    assert result['terminal_reason'] == 'CLARIFICATION_FAILED'
    assert [call['role'] for call in transport.seen] == ['planner', 'executor', 'verifier']
    failure, = result['review_fixes']['clarification_failures']
    assert failure['code'] == 'REQUEST_EXPIRED'
    clarifiers = [phase for phase in result['phases'] if phase['phase'] == 'clarification']
    if when == 'after_cp_creation':
        clarifier, = clarifiers
        assert clarifier['status'] == 'clarification_admission_failed' and clarifier['calls'] == 0
        assert failure['clarifier_item_id'] == clarifier['handle']['item_id']
    else:
        assert clarifiers == [] and failure['clarifier_item_id'] is None
    assert len(team.control._submissions) == (4 if when == 'after_cp_creation' else 3)


def malformed_response(kind):
    call = {'id': 'same-id', 'type': 'function', 'function': {
        'name': 'write', 'arguments': json.dumps({'path': '/shared/workspace/forbidden.txt', 'content': 'never'})}}
    values = [call]
    if kind == 'missing_id':
        del call['id']
    elif kind == 'whitespace_id':
        call['id'] = '   '
    elif kind == 'duplicate_id':
        values.append(deepcopy(call))
    elif kind == 'invalid_list':
        values = {}
    return {'role': 'assistant', 'content': None, 'reasoning_content': 'fixture reasoning', 'tool_calls': values}


@pytest.mark.parametrize('kind', ['missing_id', 'duplicate_id', 'invalid_list', 'whitespace_id'])
def test_unpairable_native_history_never_executes_or_reaches_repair(make_review_run, kind):
    assistant = malformed_response(kind)
    team, transport, _ = make_review_run([
        ('planner', support.planner_done(make_review_run.contract)),
        ('executor', {'assistant': assistant}), ('verifier', verifier_fail()),
        ('verifier', support.verifier_done()),
    ])
    result = support.successful_result(team, transport)
    assert [call['role'] for call in transport.seen] == ['planner', 'executor', 'verifier', 'verifier']
    assert support.phase(result, 'executor')['status'] == 'protocol_terminal'
    assert support.phase(result, 'executor')['tool_calls'] == 0
    assert not any(phase['phase'] == 'repair' for phase in result['phases'])
    assert not (team.folder / 'workspace/forbidden.txt').exists()
    record = transport.records[1]
    assert record['assistant_message'] == assistant
    step = json.loads((team.folder / 'phases/executor/turn_001.json').read_text(encoding='utf-8'))
    assert step['record']['assistant_message'] == assistant and step['tool_results'] == []
    assert team.history('executor')[-1] == assistant
    assert not any(message['role'] == 'tool' for message in team.history('executor'))
    blocked = result['review_fixes']['terminal_roles']['executor']
    assert blocked['call_id'] == record['call_id'] and blocked['reason'] == 'unpairable_native_history'
    assert result['review_fixes']['skipped_phases'] == [{
        'role': 'executor', 'phase': 'repair', 'reason': 'unpairable_native_history',
        'source_call_id': record['call_id'],
    }]
    assert result['call_ids'] == [entry['call_id'] for entry in transport.records]


def test_valid_ids_keep_existing_protocol_retry_and_execute_once(make_review_run):
    assistant = malformed_response('valid_ids')
    assistant['tool_calls'][0]['function']['arguments'] = '{"path":'
    team, transport, _ = make_review_run([
        ('planner', support.planner_done(make_review_run.contract)),
        ('executor', {'assistant': assistant}),
        ('executor', support.calls(support.action('write', {
            'path': '/shared/workspace/recovered.txt', 'content': 'one write'}), support.finish())),
        ('verifier', support.verifier_done()),
    ])
    result = support.successful_result(team, transport)
    assert result['review_fixes']['terminal_roles'] == {}
    assert support.phase(result, 'executor')['calls'] == 2
    assert len([action for action in team.box.actions if action['arguments'].get('path') ==
                '/shared/workspace/recovered.txt']) == 1
    history = transport.seen[2]['messages']
    offset = history.index(assistant)
    assert history[offset + 1]['tool_call_id'] == 'same-id'
    assert json.loads(history[offset + 1]['content'])['protocol_error'] == 'invalid_arguments_json'


def test_terminal_role_marker_survives_safe_resume_without_replaying_bad_history(make_review_run, monkeypatch):
    assistant = malformed_response('missing_id')
    team, transport, _ = make_review_run([
        ('planner', support.planner_done(make_review_run.contract)),
        ('executor', {'assistant': assistant}), ('verifier', verifier_fail()),
        ('verifier', support.verifier_done()),
    ])
    paused = team.run(pause_after_calls=2)
    assert paused['status'] == 'paused' and paused['call_count'] == 2
    snapshot = ExecutionStore(team.folder / 'execution').load_snapshot()
    assert snapshot['pending_events'] == [] and snapshot['state']['inflight'] is None
    assert snapshot['state']['review_fixes']['terminal_roles']['executor']['call_id'] == transport.records[1]['call_id']
    box = team.box
    monkeypatch.setattr(recovery, 'reattach_sandbox', lambda run_dir, image: box)
    restored = OfflineReviewTeamRun(team.root, team.entry, team.manifest, transport=transport,
                                    sandbox_factory=support.FakeSandbox)
    result = support.successful_result(restored, transport, resume=True)
    assert result['call_count'] == 4 and len(set(result['call_ids'])) == 4
    assert [call['role'] for call in transport.seen].count('executor') == 1
    assert restored.history('executor')[-1] == assistant
    assert not (team.folder / 'workspace/forbidden.txt').exists()


@pytest.mark.parametrize('when', ['before_admission', 'after_cp_creation'])
def test_expired_checkpoint_resume_keeps_executor_until_loop_settles(make_review_run, monkeypatch, when):
    team, transport, _ = make_review_run([
        ('planner', support.planner_done(make_review_run.contract)),
        ('executor', support.calls(support.action('write', {
            'path': '/shared/workspace/before_expiry.txt', 'content': 'once'}),
            support.action('request_clarification', support.QUESTION))),
        ('verifier', support.verifier_done()),
    ])
    # Stop at a real safe checkpoint after the complete E tool batch, before
    # settle dispatches the clarifier. A new instance restores the CP/store.
    team.setup()
    assert team.next_phase()
    team.turn()
    team.settle()
    assert team.next_phase()
    if when == 'before_admission':
        # Date the request in the past; restore uses the real default clock.
        # No sleep and no persisted evidence is edited to manufacture expiry.
        team.scheduler.clock = lambda: time.time() - runner.CLARIFICATION_DEADLINE - 10
    team.turn()
    source_handle = deepcopy(team.data['current']['handle'])
    snapshot = ExecutionStore(team.folder / 'execution').load_snapshot()
    assert snapshot['pending_events'] == [] and snapshot['state']['inflight'] is None
    assert 'unprocessed_response' not in snapshot['state']
    assert snapshot['state']['current']['pending_question'] == support.QUESTION['request_id']
    box = team.box
    team.control.cp.close()
    monkeypatch.setattr(recovery, 'reattach_sandbox', lambda run_dir, image: box)
    restored = OfflineReviewTeamRun(team.root, team.entry, team.manifest, transport=transport,
                                    sandbox_factory=support.FakeSandbox)
    if when == 'after_cp_creation':
        start = restored.start_phase

        def expire_during_start(role, name, prompt, *, request_id=None):
            if name == 'clarification':
                admit = restored.scheduler.admit

                def expired_admit(ident, context):
                    expires = restored.scheduler.get(ident)['expires_at']
                    restored.scheduler.clock = lambda: expires + 1
                    return admit(ident, context)

                restored.scheduler.admit = expired_admit
            return start(role, name, prompt, request_id=request_id)

        restored.start_phase = expire_during_start
    result = support.successful_result(restored, transport, resume=True)
    assert isinstance(restored.control, recovery.RecoverableControl)
    assert [call['role'] for call in transport.seen] == ['planner', 'executor', 'verifier']
    assert result['call_count'] == result['budget']['call_count'] == 3
    assert len(set(result['call_ids'])) == 3
    source = support.phase(result, 'executor')
    assert source['handle'] == source_handle and source['status'] == 'clarification_failed'
    assert source['calls'] == 1 and source['tool_calls'] == 2 and source['pending_question'] is None
    assert sum(action['arguments'].get('path') == '/shared/workspace/before_expiry.txt'
               for action in box.actions) == 1
    failure, = result['review_fixes']['clarification_failures']
    assert failure['source_item_id'] == source_handle['item_id']
    assert (failure['clarifier_item_id'] is None) == (when == 'before_admission')
    request = result['clarifications']['requests'][support.QUESTION['request_id']]
    assert request['state'] == 'FAILED' and request['resume_count'] == 0
    assert request['reply_call_count'] == 0
    assert len(restored.control._submissions) == (3 if when == 'before_admission' else 4)


def test_direct_dispatch_guard_does_not_allocate_another_cp_item(make_review_run):
    team, transport, _ = make_review_run([
        ('planner', support.planner_done(make_review_run.contract)),
        ('executor', {'assistant': malformed_response('missing_id')}),
    ])
    try:
        team.setup()
        team.next_phase(); team.turn(); team.settle()
        team.next_phase(); team.turn()
        before = len(team.control._handles)
        calls_before = len(transport.seen)
        with pytest.raises(RecoveryRequired, match='unusable native history'):
            team.start_phase('executor', 'repair', 'Must not be dispatched')
        assert len(team.control._handles) == before
        team.turn()
        assert len(transport.seen) == calls_before
    finally:
        if team.control is not None:
            team.control.close()
        if team.box is not None:
            team.box.close()
