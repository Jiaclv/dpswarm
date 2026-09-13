"""Native acceptance adversarial regressions; no model/provider calls."""
import base64
import copy
import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from dpswarm.acceptance import AcceptanceService, parse_report
from dpswarm.control import ControlPlaneError
from dpswarm.server import PanelState
from dpswarm.session_server import create_server
from dpswarm.types import AcceptanceState


@pytest.fixture
def panel(tmp_path):
    st = PanelState(tmp_path / 'state')
    ok, result = st.publish_spec({'max_open_work_items': 12, 'max_team_workers': 8, 'max_active_node_points': 32})
    assert ok, result
    ok, result = st.bind_execution_root({'parent_session_id': 'native-root', 'delegation_depth': 0,
                                       'provider': 'mock', 'model': 'b-kimi'})
    assert ok, result
    yield st
    st.cp.close()


def source(message='user-1', content='Create a correct cycling animation'):
    return {'source': 'dph-user-message', 'session_id': 'native-root', 'message_id': message, 'content': content}


def requirement(rid='motion', message='user-1', mandatory=True):
    return {'id': rid, 'description': rid, 'mandatory': mandatory, 'source_refs': [message]}


def bind(st, reviewer='reviewer'):
    ok, result = st.acceptance({'action': 'bind', 'request_id': 'bind-1', 'payload': {
        'task_lineage': 'cycle-task', 'source': source(), 'requirements': [requirement()], 'reviewer_id': reviewer}})
    assert ok, result
    return result


def mutate(st, state, action, payload, request_id=None):
    ok, result = st.acceptance({'action': action, 'contract_id': state['contract_id'],
        'expected_revision': state['revision'], 'request_id': request_id or f'{action}-{state["revision"]}', 'payload': payload})
    assert ok, result
    return result


def actor(st, content):
    ok, value = st.delegate({'kind': 'derive', 'subtasks': [{'title': 'offline', 'prompt': 'fixture',
                            'provider': 'mock', 'model': 'b-kimi'}]})
    assert ok, value
    row = value['items'][0]
    item, node = st.cp.proj.work_items[row['item_id']], st.cp.proj.nodes[row['node_id']]
    ok, result = st.bind_execution({'item_id': item.item_id, 'node_id': node.node_id,
        'attempt': item.attempt, 'context_epoch': node.context_epoch, 'reservation_session_id': node.session_id,
        'execution_session_id': 'host-' + node.node_id, 'parent_session_id': 'native-root', 'execution_provider': 'dsh'})
    assert ok, result
    result = {'item_id': item.item_id, 'node_id': node.node_id, 'context_epoch': node.context_epoch, 'session_id': node.session_id}
    ok, response = st.submit_output({**result, 'output': content})
    assert ok, response
    return {**result, 'package_id': item.submission_package_id}


def snapshot(content=b'<html>candidate</html>', **extra):
    return {'schema': 'dpswarm-candidate-v1', 'kind': 'files', 'binding': {}, 'entry_paths': ['index.html'],
        'changed_paths': ['index.html'], 'candidate_files': [{'path': 'index.html', 'operation': 'file',
            'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content), 'content_base64': base64.b64encode(content).decode()}],
        'unknown_dependencies': [], 'dependency_complete': True, 'consumed_manifest_refs': [], **extra}


def candidate(st, state, implementation, cid='candidate-1', generation=0, roster=None, snap=None):
    return mutate(st, state, 'candidate', {'candidate_id': cid, 'generation': generation,
        'requirement_revision': state['requirement_revision'], 'candidate_item_ids': [implementation['item_id']],
        'verification_roster': roster if roster is not None else [{'id': 'tester', 'role': 'tester'}, {'id': 'reviewer', 'role': 'reviewer'}],
        'snapshot': snap or snapshot()})


def finding(state='open', classification='defect'):
    return {'id': 'far-leg', 'observation': 'Far knee bends in the wrong direction', 'requirement_ids': ['motion'],
            'paths': ['index.html'], 'classification': classification, 'state': state,
            'evidence': ['phase-90deg'], 'reason': 'Cycling geometry checked against the requirement'}


def report(state, findings=None, verdict='pass', requirements=None, **extra):
    record = {'schema': 'dpswarm-review-v1', 'candidate_id': state['candidate']['candidate_id'],
              'manifest_digest': state['candidate']['manifest_digest'], 'requirement_revision': state['requirement_revision'],
              'evidence_revision': state['evidence_revision'],
              'verdict': verdict, 'requirements': requirements or [{'id': 'motion', 'result': 'met', 'evidence': ['geometry-check']}],
              'findings': findings or [], 'unavailable_checks': [], **extra}
    return 'Review narrative\n```dpswarm-review-v1\n' + json.dumps(record) + '\n```\n'


def evidence(st, state, who, role='tester', **extra):
    return mutate(st, state, 'evidence', {'candidate_id': state['candidate']['candidate_id'], 'roster_id': role, **who, **extra})


def review(st, state, who, **extra):
    return mutate(st, state, 'review', {'candidate_id': state['candidate']['candidate_id'],
        'evidence_revision': state['evidence_revision'], 'roster_id': 'reviewer', **who, **extra})


def accept(st, state, implementation):
    return st.review({'item_id': implementation['item_id'], 'verdict': 'accept',
        'contract_id': state['contract_id'], 'candidate_id': state['candidate']['candidate_id'],
        'review_id': state['review']['review_id'], 'expected_revision': state['revision']})


def prepared(st, findings=None):
    state = bind(st)
    implementation = actor(st, 'Implementation output')
    state = candidate(st, state, implementation)
    tester = actor(st, report(state, findings))
    state = evidence(st, state, tester)
    return state, implementation, tester


def test_common_accept_guard_and_atomic_failure(panel):
    state = bind(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    sequence = panel.cp.store.last_seq
    capacity = panel.cp.proj.active_points
    with pytest.raises(ControlPlaneError, match='authoritative'):
        panel.cp.accept_submission(implementation['item_id'])
    assert panel.cp.store.last_seq == sequence
    assert panel.cp.proj.active_points == capacity
    assert panel.cp.proj.work_items[implementation['item_id']].acceptance == AcceptanceState.SUBMITTED


def test_one_final_review_accepts_without_revision_loop_and_replays(panel):
    state, implementation, _ = prepared(panel)
    revision = state['evidence_revision']
    reviewer = actor(panel, report(state))
    state = review(panel, state, reviewer)
    assert state['evidence_revision'] == revision
    assert state['review_revision'] == 1
    ok, result = accept(panel, state, implementation)
    assert ok, result
    assert accept(panel, state, implementation)[1]['replayed']
    path = panel.workspace
    panel.cp.close()
    restored = PanelState(path)
    try:
        contract = restored.acceptance_state()['contracts'][0]
        assert contract['accepted'][0]['candidate_id'] == 'candidate-1'
        assert contract['revision'] == state['revision'] + 1
        assert restored.cp.proj.work_items[implementation['item_id']].acceptance == AcceptanceState.ACCEPTED
    finally:
        restored.cp.close()


def test_late_tester_blocks_accept_and_invalidates_old_review(panel):
    state = bind(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    reviewer = actor(panel, report(state))
    state = review(panel, state, reviewer)
    ok, result = accept(panel, state, implementation)
    assert not ok and result['error'] == 'VERIFICATION_INCOMPLETE'
    old_evidence_revision = state['evidence_revision']
    tester = actor(panel, report(state, [finding()]))
    state = evidence(panel, state, tester)
    assert state['evidence_revision'] == old_evidence_revision + 1
    ok, result = panel.acceptance({'action': 'review', 'contract_id': state['contract_id'], 'expected_revision': state['revision'],
        'request_id': 'stale-review', 'payload': {'candidate_id': 'candidate-1', 'roster_id': 'reviewer',
            'evidence_revision': old_evidence_revision, **reviewer}})
    assert not ok and result['error'] == 'REVIEW_EVIDENCE_STALE'


def test_findings_survive_candidate_change_and_omission_cannot_pass(panel):
    state, implementation, _ = prepared(panel, [finding()])
    state = candidate(panel, state, implementation, 'candidate-2', 1, roster=[{'id': 'reviewer', 'role': 'reviewer'}], snap=snapshot(b'<html>near leg fixed</html>'))
    assert state['contract']['findings']['far-leg']['first_candidate_id'] == 'candidate-1'
    reviewer = actor(panel, report(state))
    state = review(panel, state, reviewer)
    ok, result = accept(panel, state, implementation)
    assert not ok and result['error'] == 'FINDING_UNRESOLVED'


def test_current_resolved_finding_can_pass(panel):
    state, implementation, _ = prepared(panel, [finding()])
    reviewer = actor(panel, report(state, [finding('verified-resolved')]))
    state = review(panel, state, reviewer)
    assert accept(panel, state, implementation)[0]


def test_report_only_continuation_preserves_package_and_finding(panel):
    state, implementation, _ = prepared(panel, [finding()])
    bad = actor(panel, 'JSON marker omitted')
    state = evidence(panel, state, bad, 'reviewer')
    old_evidence = state['evidence_id']
    assert state['parse_error']['error'] == 'REVIEW_REPORT_INVALID'
    old_package = panel.cp.proj.work_items[bad['item_id']].submission_package_id
    panel.review({'item_id': bad['item_id'], 'verdict': 'accept'})
    repaired = actor(panel, report(state, [finding('verified-resolved')]))
    state = review(panel, state, repaired, continuation_of=old_evidence)
    assert state['contract']['evidence'][old_evidence]['parse_error']
    assert state['contract']['findings']['far-leg']['observation'].startswith('Far knee')
    assert panel.cp.proj.work_items[bad['item_id']].submission_package_id == old_package
    assert accept(panel, state, implementation)[0]


def test_takeover_cannot_waive_known_defect(panel):
    state, implementation, _ = prepared(panel, [finding()])
    reviewer = actor(panel, report(state, [finding()], verdict='needs-rework'))
    state = review(panel, state, reviewer)
    state = mutate(panel, state, 'takeover', {'reason': 'Lead will independently verify'})
    root = panel.cp.proj.nodes[panel.cp.root_lead_node]
    state = mutate(panel, state, 'review', {'candidate_id': 'candidate-1', 'evidence_revision': state['evidence_revision'],
        'item_id': root.item_id, 'node_id': root.node_id, 'session_id': root.session_id, 'context_epoch': root.context_epoch,
        'report': report(state, [finding()])})
    ok, result = accept(panel, state, implementation)
    assert not ok and result['error'] == 'FINDING_UNRESOLVED'


def test_amendment_preserves_lineage_and_allows_explicit_not_applicable(panel):
    state, implementation, _ = prepared(panel, [finding()])
    state = mutate(panel, state, 'amend', {'task_lineage': 'cycle-task', 'parent_requirement_revision': 1,
        'source': source('user-2', [{'type': 'text', 'text': 'Use a static diagram instead'}]),
        'requirements': [requirement('static', 'user-2')], 'reason': 'User changed the delivery requirement'})
    assert state['contract_revision'] == 2 and len(state['contract']['sources']) == 2
    state = candidate(panel, state, implementation, 'candidate-2', 1, roster=[{'id': 'reviewer', 'role': 'reviewer'}])
    reviewer = actor(panel, report(state, [{**finding('not-applicable'), 'evidence': ['user-message:user-2']}], requirements=[{'id': 'static', 'result': 'met', 'evidence': ['static-read']}]))
    state = review(panel, state, reviewer)
    assert accept(panel, state, implementation)[0]


@pytest.mark.parametrize('kind', ['hash', 'duplicate', 'traversal', 'dependency'])
def test_bad_candidate_rejected_without_mutating_contract(panel, kind):
    state = bind(panel)
    implementation = actor(panel, 'Implementation output')
    snap = snapshot()
    if kind == 'hash':
        snap['candidate_files'][0]['sha256'] = '0' * 64
    elif kind == 'duplicate':
        snap['candidate_files'].append({**snap['candidate_files'][0], 'path': 'INDEX.html'})
    elif kind == 'traversal':
        snap['candidate_files'][0]['path'] = '../escape.html'
    else:
        snap['required_paths'] = ['motion.js']
    ok, result = panel.acceptance({'action': 'candidate', 'contract_id': state['contract_id'], 'expected_revision': state['revision'],
        'request_id': 'bad-candidate', 'payload': {'candidate_id': 'candidate-1', 'generation': 0, 'requirement_revision': 1,
            'candidate_item_ids': [implementation['item_id']], 'verification_roster': [{'id': 'reviewer', 'role': 'reviewer'}], 'snapshot': snap}})
    assert not ok, result
    assert panel.acceptance_state()['contracts'][0]['revision'] == state['revision']


def test_blob_corruption_rejected_at_actual_accept(panel):
    state, implementation, _ = prepared(panel)
    reviewer = actor(panel, report(state))
    state = review(panel, state, reviewer)
    sha = state['candidate']['candidate_files'][0]['sha256']
    (panel.workspace / 'acceptance' / 'blobs' / sha).write_bytes(b'corrupt')
    ok, result = accept(panel, state, implementation)
    assert not ok and result['error'] in ('CANDIDATE_VIEW_CORRUPT', 'CANDIDATE_BLOB_CORRUPT')
    assert panel.cp.proj.work_items[implementation['item_id']].acceptance == AcceptanceState.SUBMITTED


def test_unknown_mandatory_and_incomplete_dependencies_block(panel):
    state, implementation, _ = prepared(panel)
    reviewer = actor(panel, report(state, requirements=[{'id': 'motion', 'result': 'unknown', 'evidence': []}]))
    state = review(panel, state, reviewer)
    assert accept(panel, state, implementation)[1]['error'] == 'MANDATORY_REQUIREMENT_UNKNOWN'


def test_idempotent_api_retry_and_request_collision(panel):
    body = {'action': 'bind', 'request_id': 'binding', 'payload': {'task_lineage': 'cycle-task', 'source': source(),
        'requirements': [requirement()], 'reviewer_id': None}}
    ok, first = panel.acceptance(body)
    assert ok, first
    ok, second = panel.acceptance(body)
    assert ok and second['replayed'] and second['revision'] == first['revision']
    body['payload']['lead_plan'] = 'changed'
    assert panel.acceptance(body)[1]['error'] == 'ACCEPTANCE_REQUEST_CONFLICT'


def test_duplicate_json_and_missing_candidate_binding_are_invalid(panel):
    state = bind(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    raw = report(state).replace('"schema": "dpswarm-review-v1"', '"schema":"dpswarm-review-v1","schema":"dpswarm-review-v1"')
    with pytest.raises(ControlPlaneError):
        parse_report(raw)
    record = json.loads(report(state).split('```dpswarm-review-v1\n')[1].split('\n```')[0])
    del record['manifest_digest']
    tester = actor(panel, '```dpswarm-review-v1\n' + json.dumps(record) + '\n```')
    state = evidence(panel, state, tester)
    assert state['parse_error']['error'] == 'REVIEW_CANDIDATE_MISMATCH'


def test_http_acceptance_routes_are_scoped_and_advertise_capability(tmp_path):
    server = create_server(tmp_path / 'http', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(path, body=None, session='native-root'):
        headers = {'Authorization': 'Bearer ' + server.hub.root.token, 'X-DPSwarm-Session': session}
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}' + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())
    try:
        assert call('/api/status')[1]['bridge']['runtime']['acceptance_contract'] == 'dpswarm-acceptance-v1'
        assert call('/api/execution/root', {'parent_session_id': 'native-root', 'delegation_depth': 0,
            'provider': 'mock', 'model': 'b-kimi'})[0] == 200
        status, result = call('/api/acceptance', {'action': 'bind', 'request_id': 'bind-http', 'payload': {
            'task_lineage': 'cycle-task', 'source': source(), 'requirements': [requirement()], 'reviewer_id': None}})
        assert status == 200, result
        assert len(call('/api/acceptance')[1]['contracts']) == 1
        assert call('/api/acceptance', session='other')[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        server.hub.close()
        thread.join(timeout=3)


def test_old_review_package_cannot_claim_new_input_revision(panel):
    state = bind(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    reviewer = actor(panel, report(state))
    state = review(panel, state, reviewer)
    tester = actor(panel, report(state))
    state = evidence(panel, state, tester)
    ok, result = panel.acceptance({'action': 'review', 'contract_id': state['contract_id'],
        'expected_revision': state['revision'], 'request_id': 'replay-old-review', 'payload': {
            'candidate_id': 'candidate-1', 'roster_id': 'reviewer', 'evidence_revision': state['evidence_revision'], **reviewer}})
    assert not ok and result['error'] == 'REVIEW_EVIDENCE_STALE'


def test_late_old_generation_findings_are_retained_and_invalidate_review(panel):
    state = bind(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    old_tester = actor(panel, report(state, [finding()]))
    state = candidate(panel, state, implementation, 'candidate-2', 1,
                      roster=[{'id': 'reviewer', 'role': 'reviewer'}])
    reviewer = actor(panel, report(state))
    state = review(panel, state, reviewer)
    state = mutate(panel, state, 'evidence', {'candidate_id': 'candidate-1', 'roster_id': 'tester', **old_tester})
    assert state['contract']['findings']['far-leg']['first_candidate_id'] == 'candidate-1'
    assert state['contract']['current_review_id'] is None


def test_unsealed_file_in_verification_view_blocks_accept(panel):
    from pathlib import Path
    state, implementation, _ = prepared(panel)
    reviewer = actor(panel, report(state))
    state = review(panel, state, reviewer)
    (Path(state['candidate']['view_path']) / 'unsealed.js').write_text('changed input', encoding='utf-8')
    assert accept(panel, state, implementation)[1]['error'] == 'CANDIDATE_VIEW_CORRUPT'


def test_same_requirement_id_changed_by_user_allows_explicit_exclusion(panel):
    state, implementation, _ = prepared(panel, [finding()])
    state = mutate(panel, state, 'amend', {'task_lineage': 'cycle-task', 'parent_requirement_revision': 1,
        'source': source('user-2', 'Replace cycling with a static diagram'),
        'requirements': [{**requirement('motion', 'user-2'), 'description': 'Static diagram only'}],
        'reason': 'User changed the meaning of the same requirement'})
    state = candidate(panel, state, implementation, 'candidate-2', 1, roster=[{'id': 'reviewer', 'role': 'reviewer'}])
    reviewer = actor(panel, report(state, [{**finding('not-applicable'), 'evidence': ['user-message:user-2']}]))
    state = review(panel, state, reviewer)
    assert accept(panel, state, implementation)[0]


def test_review_report_corruption_blocks_actual_accept(panel):
    state, implementation, _ = prepared(panel)
    reviewer = actor(panel, report(state))
    state = review(panel, state, reviewer)
    package = panel.cp.proj.packages[reviewer['package_id']]
    (panel.workspace / 'artifacts' / package['artifact_ref']).write_text('corrupt report', encoding='utf-8')
    assert accept(panel, state, implementation)[1]['error'] == 'EVIDENCE_CORRUPT'
