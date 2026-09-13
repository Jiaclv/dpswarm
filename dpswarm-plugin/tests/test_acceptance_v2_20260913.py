"""dpswarm-review-v2 semantics: plan-bound checks (P1) and finding dispositions (P2a).

Regression matrix R01–R07 from reports/2026-09-13/b7e149cb-mechanism-fix-plan.md.
No model/provider calls; reuses the offline PanelState fixtures.
"""
import base64
import copy
import hashlib
import json

import pytest

from dpswarm.acceptance import REVIEW_V2, parse_report, verify_v2_semantics
from dpswarm.control import ControlPlaneError
from dpswarm.server import PanelState


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


def bind_v2(st, reviewer='reviewer'):
    ok, result = st.acceptance({'action': 'bind', 'request_id': 'bind-1', 'payload': {
        'task_lineage': 'v2-task', 'source': source(), 'requirements': [requirement()],
        'reviewer_id': reviewer, 'review_contract': REVIEW_V2}})
    assert ok, result
    return result


def attempt(st, state, action, payload, request_id=None):
    return st.acceptance({'action': action, 'contract_id': state['contract_id'],
        'expected_revision': state['revision'], 'request_id': request_id or f'{action}-{state["revision"]}-attempt', 'payload': payload})

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


def snapshot(content=b'<html>candidate</html>'):
    return {'schema': 'dpswarm-candidate-v1', 'kind': 'files', 'binding': {}, 'entry_paths': ['index.html'],
        'changed_paths': ['index.html'], 'candidate_files': [{'path': 'index.html', 'operation': 'file',
            'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content), 'content_base64': base64.b64encode(content).decode()}],
        'unknown_dependencies': [], 'dependency_complete': True, 'consumed_manifest_refs': []}


def candidate(st, state, implementation, cid='candidate-1'):
    return mutate(st, state, 'candidate', {'candidate_id': cid, 'generation': 0,
        'requirement_revision': state['requirement_revision'], 'candidate_item_ids': [implementation['item_id']],
        'verification_roster': [{'id': 'tester', 'role': 'tester'}, {'id': 'reviewer', 'role': 'reviewer'}],
        'snapshot': snapshot()})


def v2_report(state, *, checks=None, results=None, requirements=None, findings=None, verdict='pass', plan_revision=1):
    """A v2 report whose default shape is a completed static check on motion."""
    plan = {'revision': plan_revision, 'coverage': 'Static read of the sealed candidate covers the requirement.',
            'checks': checks if checks is not None else [
                {'id': 'check-static', 'requirement_ids': ['motion'], 'method': 'static:read', 'required_for_claim': True,
                 'rationale': 'The requirement is statically decidable from the sealed bytes.'}]}
    record = {'schema': REVIEW_V2, 'candidate_id': state['candidate']['candidate_id'],
              'manifest_digest': state['candidate']['manifest_digest'], 'requirement_revision': state['requirement_revision'],
              'evidence_revision': state['evidence_revision'], 'verdict': verdict,
              'verification_plan': plan,
              'check_results': results if results is not None else [
                  {'check_id': 'check-static', 'method': 'static:read', 'execution_status': 'completed',
                   'evidence_refs': ['snapshot read'], 'limitations': [], 'environment_ref': 'sealed-view',
                   'candidate_id': state['candidate']['candidate_id'], 'manifest_digest': state['candidate']['manifest_digest'],
                   'requirement_revision': state['requirement_revision'], 'verification_plan_revision': plan_revision}],
              'requirements': requirements if requirements is not None else [
                  {'id': 'motion', 'result': 'met', 'evidence': ['sealed bytes'],
                   'verification_plan_revision': plan_revision, 'check_refs': ['check-static']}],
              'findings': findings or [], 'unavailable_checks': []}
    return 'Review narrative\n```' + REVIEW_V2 + '\n' + json.dumps(record) + '\n```\n'


def evidence(st, state, who, role='tester'):
    return mutate(st, state, 'evidence', {'candidate_id': state['candidate']['candidate_id'], 'roster_id': role, **who})


def review(st, state, who):
    return mutate(st, state, 'review', {'candidate_id': state['candidate']['candidate_id'],
        'evidence_revision': state['evidence_revision'], 'roster_id': 'reviewer', **who})


def prepared_v2(st, report_text=None, findings=None):
    state = bind_v2(st)
    implementation = actor(st, 'Implementation output')
    state = candidate(st, state, implementation)
    tester = actor(st, report_text or v2_report(state, findings=findings))
    state = evidence(st, state, tester)
    return state, implementation


# R15: the contract freezes its report version at bind time.
def test_v2_contract_rejects_v1_report_and_vice_versa_kept(panel):
    state = bind_v2(panel)
    assert state['contract']['review_contract'] == REVIEW_V2
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    v1_text = 'narrative\n```dpswarm-review-v1\n' + json.dumps({
        'schema': 'dpswarm-review-v1', 'candidate_id': state['candidate']['candidate_id'],
        'manifest_digest': state['candidate']['manifest_digest'], 'requirement_revision': state['requirement_revision'],
        'verdict': 'pass', 'requirements': [{'id': 'motion', 'result': 'met', 'evidence': ['x']}],
        'findings': [], 'unavailable_checks': []}) + '\n```\n'
    tester = actor(panel, v1_text)
    ok, result = attempt(panel, state, 'evidence', {'candidate_id': state['candidate']['candidate_id'],
        'roster_id': 'tester', **tester})
    assert not ok and result['error'] == 'REVIEW_CONTRACT_MISMATCH', result


def test_mixed_or_missing_fence_rejected():
    from dpswarm.control import ControlPlaneError as CPE
    with pytest.raises(CPE, match='Exactly one complete review JSON fence'):
        parse_report('```dpswarm-review-v1\n{}\n```\n```dpswarm-review-v2\n{}\n```\n')


# R01: a met mandatory requirement without a completed check cannot register.
def test_r01_met_claim_without_completed_check_rejected(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    text = v2_report(state, results=[
        {'check_id': 'check-static', 'method': 'static:read', 'execution_status': 'not_run',
         'evidence_refs': [], 'limitations': ['browser unavailable'], 'environment_ref': None,
         'candidate_id': state['candidate']['candidate_id'], 'manifest_digest': state['candidate']['manifest_digest'],
         'requirement_revision': state['requirement_revision'], 'verification_plan_revision': 1}])
    tester = actor(panel, text)
    state2 = evidence(panel, state, tester)
    # Registration stores the parse error; the final review submission surfaces it.
    stored = state2['contract']['evidence'][state2['candidate']['roster_evidence']['tester']]
    assert stored['parse_error']['error'] == 'VERIFICATION_CHECK_INCOMPLETE'
    ok, result = attempt(panel, state2, 'review', {'candidate_id': state2['candidate']['candidate_id'],
        'evidence_revision': state2['evidence_revision'], 'roster_id': 'reviewer', **actor(panel, text)})
    assert not ok and result['error'] == 'VERIFICATION_CHECK_INCOMPLETE', result


# R02: a static task with a completed static check passes end to end.
def test_r02_static_completed_check_accepts(panel):
    state, implementation = prepared_v2(panel)
    reviewer = actor(panel, v2_report(state))
    state = review(panel, state, reviewer)
    ok, result = panel.review({'item_id': implementation['item_id'], 'verdict': 'accept',
        'contract_id': state['contract_id'], 'candidate_id': state['candidate']['candidate_id'],
        'review_id': state['review']['review_id'], 'expected_revision': state['revision']})
    assert ok, result


# R03: an equivalent alternative method is a reviewer judgement, not a schema rule.
def test_r03_alternative_method_accepted(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    text = v2_report(state, checks=[{'id': 'alt-check', 'requirement_ids': ['motion'], 'method': 'rendered:frame-compare',
                 'required_for_claim': True, 'rationale': 'Equivalent to the planned static read for this decidable claim.'}],
        results=[{'check_id': 'alt-check', 'method': 'rendered:frame-compare', 'execution_status': 'completed',
                  'evidence_refs': ['frame set'], 'limitations': [], 'environment_ref': 'headless',
                  'candidate_id': state['candidate']['candidate_id'], 'manifest_digest': state['candidate']['manifest_digest'],
                  'requirement_revision': state['requirement_revision'], 'verification_plan_revision': 1}],
        requirements=[{'id': 'motion', 'result': 'met', 'evidence': ['frames'],
                       'verification_plan_revision': 1, 'check_refs': ['alt-check']}])
    tester = actor(panel, text)
    state = evidence(panel, state, tester)
    fresh = v2_report(state,
        checks=[{'id': 'alt-check', 'requirement_ids': ['motion'], 'method': 'rendered:frame-compare',
                 'required_for_claim': True, 'rationale': 'Equivalent to the planned static read for this decidable claim.'}],
        results=[{'check_id': 'alt-check', 'method': 'rendered:frame-compare', 'execution_status': 'completed',
                  'evidence_refs': ['frame set'], 'limitations': [], 'environment_ref': 'headless',
                  'candidate_id': state['candidate']['candidate_id'], 'manifest_digest': state['candidate']['manifest_digest'],
                  'requirement_revision': state['requirement_revision'], 'verification_plan_revision': 1}],
        requirements=[{'id': 'motion', 'result': 'met', 'evidence': ['frames'],
                       'verification_plan_revision': 1, 'check_refs': ['alt-check']}])
    reviewer = actor(panel, fresh)
    state = review(panel, state, reviewer)
    assert state['review']['record']['requirements'][0]['check_refs'] == ['alt-check']


# R04: stale plan revision or wrong candidate binding is rejected.
def test_r04a_stale_plan_revision_rejected(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    # Plan revision 2, but the check result still binds revision 1: stale.
    base = json.loads(v2_report(state, plan_revision=2).split('```' + REVIEW_V2 + chr(10))[1].split(chr(10) + '```')[0])
    base['check_results'][0]['verification_plan_revision'] = 1
    text = 'n' + chr(10) + '```' + REVIEW_V2 + chr(10) + json.dumps(base) + chr(10) + '```' + chr(10)
    tester = actor(panel, text)
    state2 = evidence(panel, state, tester)
    stored = state2['contract']['evidence'][state2['candidate']['roster_evidence']['tester']]
    assert stored['parse_error']['error'] == 'VERIFICATION_PLAN_STALE'

def test_r04b_wrong_candidate_binding_rejected(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    other = v2_report(state)
    record = json.loads(other.split('```' + REVIEW_V2 + '\n')[1].split('\n```')[0])
    record['check_results'][0]['candidate_id'] = 'other-candidate'
    text2 = 'n\n```' + REVIEW_V2 + '\n' + json.dumps(record) + '\n```\n'
    tester2 = actor(panel, text2)
    state2 = evidence(panel, state, tester2)
    stored2 = state2['contract']['evidence'][state2['candidate']['roster_evidence']['tester']]
    assert stored2['parse_error']['error'] == 'VERIFICATION_CHECK_CANDIDATE_MISMATCH'


# R05: suggestions need an explicit disposition; retained closes without edits.
def test_r05_pending_suggestion_blocks_a_pass(panel):
    state, _ = prepared_v2(panel, findings=[{'id': 'F1', 'requirement_ids': ['motion'], 'paths': ['index.html'],
        'observation': 'Consider a softer palette.', 'classification': 'suggestion', 'state': 'open',
        'evidence': [], 'reason': '', 'disposition': 'pending'}])
    pending_findings = [{'id': 'F1', 'requirement_ids': ['motion'], 'paths': ['index.html'],
        'observation': 'Consider a softer palette.', 'classification': 'suggestion', 'state': 'open',
        'evidence': [], 'reason': '', 'disposition': 'pending'}]
    ok, result = attempt(panel, state, 'review', {'candidate_id': state['candidate']['candidate_id'],
        'evidence_revision': state['evidence_revision'], 'roster_id': 'reviewer',
        **actor(panel, v2_report(state, findings=pending_findings))})
    assert not ok and result['error'] == 'FINDING_DISPOSITION_REQUIRED', result

def test_r05_retained_suggestion_passes_without_product_rework(panel):
    findings = [{'id': 'F1', 'requirement_ids': ['motion'], 'paths': ['index.html'],
        'observation': 'Consider a softer palette.', 'classification': 'suggestion', 'state': 'open',
        'evidence': ['current read'], 'reason': 'Optional styling preference.', 'disposition': 'retained_suggestion',
        'disposition_reason': 'Aesthetic preference; does not affect the mandatory requirement.'}]
    state, _ = prepared_v2(panel, findings=findings)
    reviewer = actor(panel, v2_report(state, findings=findings))
    state = review(panel, state, reviewer)
    assert state['review']['record']['findings'][0]['disposition'] == 'retained_suggestion'


# R06: a defect cannot be laundered into a retained suggestion.
def test_r06_defect_retained_suggestion_rejected(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    laundered = v2_report(state, findings=[{'id': 'F2', 'requirement_ids': ['motion'], 'paths': ['index.html'],
        'observation': 'Wheels do not rotate.', 'classification': 'defect', 'state': 'open',
        'evidence': ['frame read'], 'reason': 'Marked as suggestion.', 'disposition': 'retained_suggestion',
        'disposition_reason': 'Kept for now.'}])
    tester = actor(panel, laundered)
    state2 = evidence(panel, state, tester)
    stored = state2['contract']['evidence'][state2['candidate']['roster_evidence']['tester']]
    assert stored['parse_error']['error'] == 'FINDING_DISPOSITION_INVALID'


# R07: mandatory failed cannot pass regardless of labels.
def test_r07_mandatory_failed_blocks_pass(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    failing = v2_report(state, verdict='pass',
        requirements=[{'id': 'motion', 'result': 'failed', 'evidence': ['static read found nothing'],
                       'verification_plan_revision': 1, 'check_refs': ['check-static']}])
    tester = actor(panel, failing)
    state2 = evidence(panel, state, tester)
    failing2 = v2_report(state2, verdict='pass',
        requirements=[{'id': 'motion', 'result': 'failed', 'evidence': ['static read found nothing'],
                       'verification_plan_revision': 1, 'check_refs': ['check-static']}])
    state3 = review(panel, state2, actor(panel, failing2))
    ok, result = panel.review({'item_id': implementation['item_id'], 'verdict': 'accept',
        'contract_id': state3['contract_id'], 'candidate_id': state3['candidate']['candidate_id'],
        'review_id': state3['review']['review_id'], 'expected_revision': state3['revision']})
    assert not ok and result['error'] == 'MANDATORY_REQUIREMENT_UNKNOWN', result


def test_verify_v2_semantics_direct(plan_revision=1):
    # Direct unit coverage for the semantic gate on a synthetic contract.
    contract = {'requirements': [{'id': 'r1', 'mandatory': True, 'description': 'x'}]}
    candidate = {'candidate_id': 'c1', 'manifest_digest': 'a' * 64, 'requirement_revision': 1}
    record = {'schema': REVIEW_V2, 'verdict': 'pass',
              'verification_plan': {'revision': 1, 'coverage': 'c', 'checks': [
                  {'id': 'k1', 'requirement_ids': ['r1'], 'method': 'static', 'required_for_claim': True}]},
              'check_results': [{'check_id': 'k1', 'method': 'static', 'execution_status': 'completed',
                  'evidence_refs': ['e'], 'limitations': [], 'candidate_id': 'c1', 'manifest_digest': 'a' * 64,
                  'requirement_revision': 1, 'verification_plan_revision': 1}],
              'requirements': [{'id': 'r1', 'result': 'met', 'evidence': ['e'],
                  'verification_plan_revision': 1, 'check_refs': ['k1']}],
              'findings': []}
    verify_v2_semantics(contract, record, candidate)  # valid shape passes
    record['check_results'][0]['verification_plan_revision'] = 2
    with pytest.raises(ControlPlaneError) as ei:
        verify_v2_semantics(contract, record, candidate)
    assert ei.value.code == 'VERIFICATION_PLAN_STALE'


# R08 (P2b): a format-only report continuation cannot drift semantics.
def _continuation_base(st, state):
    """Register a first tester report and return (state, prior evidence id, report text)."""
    text = v2_report(state)
    tester = actor(st, text)
    state = evidence(st, state, tester)
    prior_id = state['candidate']['roster_evidence']['tester']
    return state, prior_id, text


_continuation_seq = [0]
def _continuation(st, state, prior_id, record, purpose):
    _continuation_seq[0] += 1
    text = 'n\n```' + REVIEW_V2 + '\n' + json.dumps(record) + '\n```'
    tester = actor(st, text)
    return attempt(st, state, 'evidence', {'candidate_id': state['candidate']['candidate_id'],
        'roster_id': 'tester', 'continuation_of': prior_id,
        **({'repair_purpose': purpose} if purpose else {}), **tester},
        request_id=f'continue-{_continuation_seq[0]}')


def _rebind(state, record):
    """Refresh the report bindings against the post-evidence state."""
    record = copy.deepcopy(record)
    record['candidate_id'] = state['candidate']['candidate_id']
    record['manifest_digest'] = state['candidate']['manifest_digest']
    record['evidence_revision'] = state['evidence_revision']
    for row in record['check_results']:
        row['candidate_id'] = state['candidate']['candidate_id']
        row['manifest_digest'] = state['candidate']['manifest_digest']
    return record


def test_r08_format_repair_keeping_semantics_registers(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    state, prior_id, text = _continuation_base(panel, state)
    record = _rebind(state, json.loads(text.split('```' + REVIEW_V2 + '\n')[1].split('\n```')[0]))
    record['unavailable_checks'] = ['renderer check :: browser unavailable in repair environment']
    ok, result = _continuation(panel, state, prior_id, record, 'format')
    assert ok, result


def test_r08_format_repair_changing_verdict_is_rejected_and_prior_kept(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    state, prior_id, text = _continuation_base(panel, state)
    record = _rebind(state, json.loads(text.split('```' + REVIEW_V2 + '\n')[1].split('\n```')[0]))
    record['verdict'] = 'needs-rework'
    ok, result = _continuation(panel, state, prior_id, record, 'format')
    assert not ok and result['error'] == 'REPAIR_SEMANTIC_DRIFT', result
    assert state['candidate']['roster_evidence']['tester'] == prior_id, 'the prior report stays current'


def test_r08_substantive_purpose_and_default_may_change_conclusions(panel):
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    state, prior_id, text = _continuation_base(panel, state)
    record = _rebind(state, json.loads(text.split('```' + REVIEW_V2 + '\n')[1].split('\n```')[0]))
    record['verdict'] = 'needs-rework'
    record['requirements'][0]['result'] = 'failed'
    def refresh(state):
        contract = next(iter(panel.cp.proj.acceptance_contracts.values()))
        return {**state, 'revision': contract['revision'], 'evidence_revision': contract['evidence_revision']}
    for purpose in (None, 'substantive'):
        ok, result = _continuation(panel, state, prior_id, record, purpose)
        assert ok, (purpose, result)
        state = refresh(state)
        contract = next(iter(panel.cp.proj.acceptance_contracts.values()))
        prior_id = contract['candidates'][state['candidate']['candidate_id']]['roster_evidence']['tester']


def test_r08_format_repair_without_readable_prior_requires_substantive(panel):
    # A parse-error prior report has no readable semantics to compare against.
    state = bind_v2(panel)
    implementation = actor(panel, 'Implementation output')
    state = candidate(panel, state, implementation)
    broken = 'narrative\n```' + REVIEW_V2 + '\n{"schema": "' + REVIEW_V2 + '"}\n```\n'
    tester = actor(panel, broken)
    state = evidence(panel, state, tester)
    prior_id = state['candidate']['roster_evidence']['tester']
    ok, result = _continuation(panel, state, prior_id, _rebind(state, json.loads(
        v2_report(state).split('```' + REVIEW_V2 + '\n')[1].split('\n```')[0])), 'format')
    assert not ok and result['error'] == 'REPAIR_SEMANTIC_BASE_MISSING', result
