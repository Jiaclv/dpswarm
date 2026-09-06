"""Approved test-evidence role contract; scripted transport and fake containers only."""
from copy import deepcopy

import pytest

from modelbench.swe_fixed_team_20260903 import runner
from modelbench.swe_fixed_team_20260903.tests.test_runner import action, done, events, make_run
from modelbench.swe_fixed_team_20260903.tests.test_runner_cm import install_cm_transport


APPROVED_TEST_WORKER = (
    'For behavior-changing fixes, derive a focused counterexample from the public issue: explain one '
    'plausible wrong implementation the tests distinguish, using distinguishable input sources or '
    'relevant boundary conditions where applicable. Run the tests against your unchanged production '
    'baseline when feasible. Report the exact command, observed test status and selected scope; if a '
    'meaningful failing baseline cannot be established, explain why. Do not infer test success from a '
    "pipeline's final command status, and do not claim validation of a production patch you have not received.")
APPROVED_LEAD = (
    "During final integration review, inspect the test worker's counterexample and baseline evidence, "
    'then run the same focused tests with the adopted production changes when feasible. State the observed '
    'results and test scope. If evidence is unavailable or the tests cannot distinguish a plausible wrong '
    'implementation, explicitly record the limitation and the reason for your adoption or discard decision. '
    'Local test success is not an official benchmark result.')


def tools_by_name(call):
    return {tool['function']['name']: tool['function'] for tool in call['tools']}


def test_contract_constants_match_the_approved_words():
    assert runner.TEST_EVIDENCE_CONTRACT_VERSION == 'test_evidence_20260905_v1'
    assert runner.TEST_WORKER_EVIDENCE_CONTRACT == APPROVED_TEST_WORKER
    assert runner.LEAD_TEST_EVIDENCE_CONTRACT == APPROVED_LEAD


@pytest.mark.parametrize('condition,models', [
    ('fixed_team', ['glm-5.3', 'glm-5.3']),
    ('hetero_team', ['gpt-5.6-terra', 'glm-5.3']),
])
def test_real_scripted_calls_receive_role_specific_contracts_and_unchanged_tools(make_run, condition, models):
    run, _, _ = make_run(condition=condition, model=models[0], worker_models=models)
    result = run.run()
    assert result['infrastructure_error'] is None
    calls = run.transport.seen
    normal_lead = next(call for call in calls
                       if call['actor'] == 'lead' and 'review_worker' in tools_by_name(call))
    production = next(call for call in calls if call['actor'] == 'worker-1')
    regression = next(call for call in calls if call['actor'] == 'worker-2')
    assert APPROVED_LEAD in normal_lead['messages'][0]['content']
    assert APPROVED_TEST_WORKER in regression['messages'][0]['content']
    assert APPROVED_LEAD not in regression['messages'][0]['content']
    assert APPROVED_TEST_WORKER not in production['messages'][0]['content']
    assert APPROVED_LEAD not in production['messages'][0]['content']
    workers = [run.workers['worker-1'], run.workers['worker-2']]
    assert [worker.request['title'] for worker in workers] == [
        'Production implementation', 'Regression test implementation']
    assert [worker.handle.model for worker in workers] == models
    assert 'Do not modify test files.' in production['messages'][0]['content']
    assert 'Do not modify production files.' in regression['messages'][0]['content']
    assert workers[1].request['task'] == runner.FIXED_ASSIGNMENTS[1]['task']
    assert workers[1].request['task'].count(APPROVED_TEST_WORKER) == 1
    lead_tools = tools_by_name(normal_lead)
    assert set(lead_tools) == {'bash', 'finish', 'collect', 'review_worker', 'reply_worker'}
    assert set(tools_by_name(regression)) == {'bash', 'finish', 'ask_lead'}
    finish_schema = {
        'type': 'object', 'properties': {
            'summary': {'type': 'string'},
            'status': {'type': 'string', 'enum': ['completed', 'blocked']}},
        'required': ['summary', 'status'], 'additionalProperties': False}
    assert lead_tools['finish']['parameters'] == finish_schema
    assert tools_by_name(regression)['finish']['parameters'] == finish_schema
    assert lead_tools['review_worker']['parameters'] == {
        'type': 'object', 'properties': {
            'worker_id': {'type': 'string'},
            'decision': {'type': 'string', 'enum': ['adopt', 'discard']},
            'reason': {'type': 'string', 'minLength': 1, 'pattern': r'\S'}},
        'required': ['worker_id', 'decision', 'reason'], 'additionalProperties': False}
    # The contract stays in role instructions; no automatic acceptance gate was added.
    assert all(worker['review_decision'] == 'adopt' for worker in result['workers'])


def test_solo_call_does_not_inherit_team_evidence_contracts(make_run):
    run, _, _ = make_run(condition='solo', lead=[[done()]])
    result = run.run()
    assert result['infrastructure_error'] is None and not result['workers']
    assert len(run.transport.seen) == 1
    call = run.transport.seen[0]
    assert APPROVED_TEST_WORKER not in call['messages'][0]['content']
    assert APPROVED_LEAD not in call['messages'][0]['content']
    assert set(tools_by_name(call)) == {'bash', 'finish'}


def test_assembly_uses_the_admitted_task_and_normal_lead_contract_resumes_after_scout(make_run, monkeypatch):
    assembled_tasks = []
    original = runner.ContextAssembler.assemble

    def assemble(self, brief, *args, **kwargs):
        assembled_tasks.append(brief.task_intent)
        return original(self, brief, *args, **kwargs)

    monkeypatch.setattr(runner.ContextAssembler, 'assemble', assemble)
    run, _, _ = make_run(limits_override={
        'cm_team_memory': True, 'cm_scout_distill': True, 'cm_bootstrap_package': True})
    result = run.run()
    assert result['infrastructure_error'] is None
    assert assembled_tasks == [run.workers[worker_id].request['task'] for worker_id in ('worker-1', 'worker-2')]
    assert APPROVED_TEST_WORKER not in assembled_tasks[0]
    assert APPROVED_TEST_WORKER in assembled_tasks[1]
    lead_calls = [call for call in run.transport.seen if call['actor'] == 'lead']
    assert set(tools_by_name(lead_calls[0])) == {'bash'}
    assert APPROVED_LEAD not in lead_calls[0]['messages'][0]['content']
    assert APPROVED_TEST_WORKER not in lead_calls[0]['messages'][0]['content']
    assert len(lead_calls) >= 2
    assert all(APPROVED_LEAD in call['messages'][0]['content'] for call in lead_calls[1:])
    regression = next(call for call in run.transport.seen if call['actor'] == 'worker-2')
    assert APPROVED_TEST_WORKER in regression['messages'][0]['content']
    assert APPROVED_TEST_WORKER in regression['messages'][1]['content']
    assert 'scout note' in regression['messages'][1]['content']


def test_contract_does_not_change_execution_or_context_defaults(make_run):
    expected = {
        'max_calls': 28, 'token_limit': 600_000, 'wall_seconds': 1800,
        'worker_calls': 8, 'active_workers': 2, 'delegations': 2,
        'lead_reserve_calls': 2, 'cm_call_allowance': 12,
        'cm_enabled': True, 'cm_model': 'deepseek-v4-flash', 'cm_max_tokens': 4096,
        'cm_team_memory': False, 'cm_scout_distill': False, 'cm_bootstrap_package': False,
        'cm_edit_curfew': True, 'closing_call_reserve_exempt': True, 'edit_status_banner': True,
    }
    before = deepcopy(runner.LIMITS)
    run, _, _ = make_run(condition='solo', lead=[[done()]])
    assert {key: run.limits[key] for key in expected} == expected
    run.prompt()
    assert runner.LIMITS == before


def test_test_worker_contract_survives_cm_in_actual_transport_messages(make_run):
    # Read-only rounds trigger CM before the test edit, preserving the default edit curfew.
    scripts = {'worker-2': [[action('bash', command='inspect-regression')]] * 2
               + [[action('bash', command='edit-regression'), done()]]}
    run, _, _ = make_run(workers=scripts, limits_override={
        'cm_context_budget': 200, 'cm_keep_recent': 2})
    seen_cm = []
    install_cm_transport(run, seen_cm=seen_cm)
    result = run.run()
    assert result['infrastructure_error'] is None
    assert any(event['event'] == 'cm_compression' and event['trigger']['agent'] == 'worker-2'
               for event in events(run))
    regression_calls = [call for call in run.transport.seen if call['actor'] == 'worker-2']
    compressed_calls = [call for call in regression_calls
                        if any(message.get('role') == 'user'
                               and message.get('content', '').startswith('Context summary (context manager,')
                               for message in call['messages'][1:])]
    assert compressed_calls
    original_system = regression_calls[0]['messages'][0]
    assert APPROVED_TEST_WORKER in original_system['content']
    for call in compressed_calls:
        assert call['messages'][0] == original_system
        assert APPROVED_LEAD not in call['messages'][0]['content']
        assert set(tools_by_name(call)) == {'bash', 'finish', 'ask_lead'}
    assert seen_cm
    # The task can appear as CM selection material, but CM receives no new role duty.
    for call in seen_cm:
        assert APPROVED_TEST_WORKER not in call['messages'][0]['content']
        assert APPROVED_LEAD not in call['messages'][0]['content']
    assert run.workers['worker-2'].delivery['status'] == 'completed'


def test_test_worker_contract_survives_finish_only_closing_transport(make_run):
    work_calls = runner.LIMITS['worker_calls']
    scripts = {'worker-2': [[action('bash', command='edit-regression')]] * work_calls + [[done()]]}
    run, _, _ = make_run(workers=scripts)
    result = run.run()
    assert result['infrastructure_error'] is None
    closing_events = [event for event in events(run) if event['event'] == 'closing_call_settled']
    assert len(closing_events) == 1 and closing_events[0]['worker_id'] == 'worker-2'
    regression_calls = [call for call in run.transport.seen if call['actor'] == 'worker-2']
    assert len(regression_calls) == work_calls + 1
    closing = regression_calls[-1]
    assert closing['call_id'] == closing_events[0]['call_id']
    assert closing['messages'][0] == regression_calls[0]['messages'][0]
    assert APPROVED_TEST_WORKER in closing['messages'][0]['content']
    assert APPROVED_LEAD not in closing['messages'][0]['content']
    assert set(tools_by_name(closing)) == {'finish'}
    assert 'closing call; only finish is declared' in closing['messages'][-1]['content']
    assert run.workers['worker-2'].delivery['status'] == 'completed'
