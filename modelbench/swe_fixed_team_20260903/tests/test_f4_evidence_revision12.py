"""Offline F4 evidence semantics: observed state, delivery and legacy boundaries.

The scripted runner uses fake environments and transport. No provider, Docker,
task solution, historical result, or official grader is consulted.
"""
from copy import deepcopy
import json

import pytest

from modelbench.swe_fixed_team_20260903 import reporting
from modelbench.swe_fixed_team_20260903.tests.test_reporting_20260905 import (
    fixture as report_fixture, write_json)
from modelbench.swe_fixed_team_20260903.tests.test_runner import (
    action, done, make_run, settle)
from modelbench.swe_fixed_team_20260903.validation.audit_results import Audit


SEMANTICS = 'observed_worktree_delivery_v1'
ATTEMPT_WITHOUT_WRITE = 'python -c "open(\'missing.py\',\'w\').write(\'attempt\')"'


def observed(value):
    return {'observed_persisted_change': value}


@pytest.mark.parametrize('worktree,expected', [
    (observed(True), 'edited'),
    (observed(False), 'no_edit'),
    ({}, 'unknown'),
    (None, 'unknown'),
    (observed(None), 'unknown'),
    (observed(1), 'unknown'),
    (observed('true'), 'unknown'),
])
def test_lead_phase_requires_boolean_observed_state(worktree, expected):
    assert reporting.DEATH_PHASE_SEMANTICS == SEMANTICS
    assert reporting.observed_lead_death_phase(worktree) == expected


@pytest.mark.parametrize('decision,status,delta_status,delta_bytes,worktree,expected', [
    ('adopt', 'completed', 'present', 20, observed(True), 'adopted'),
    # Explicit adoption retains precedence even when other evidence is absent.
    ('adopt', None, None, None, None, 'adopted'),
    ('discard', 'completed', 'present', 20, observed(False), 'delivered_not_adopted'),
    (None, 'completed', 'present', 20, None, 'delivered_not_adopted'),
    ('discard', 'completed', 'present', 0, observed(True), 'edited_no_delivery'),
    ('discard', 'completed', 'present', 0, observed(False), 'no_edit'),
    ('discard', 'completed', 'present', 0, {}, 'unknown'),
    (None, 'completed', None, 20, observed(True), 'unknown'),
    (None, 'completed', 'missing', 20, observed(False), 'unknown'),
    (None, 'completed', 'present', None, observed(True), 'unknown'),
    (None, 'completed', 'present', -1, observed(False), 'unknown'),
    (None, 'completed', 'present', True, observed(True), 'unknown'),
    (None, 'completed', 'present', '20', observed(True), 'unknown'),
    ('discard', 'blocked', 'present', 20, observed(True), 'edited_no_delivery'),
    ('discard', 'blocked', 'present', 0, observed(False), 'no_edit'),
    (None, 'not_started', None, None, {}, 'unknown'),
])
def test_worker_phase_preserves_delivery_and_unknown_boundaries(
        decision, status, delta_status, delta_bytes, worktree, expected):
    assert reporting.observed_worker_death_phase(
        review_decision=decision, status=status, delta_status=delta_status,
        delta_bytes=delta_bytes, worktree=worktree) == expected


@pytest.mark.parametrize('changed', [False, None])
def test_nonempty_exported_lead_artifact_survives_stale_or_missing_observation(changed):
    # A successful export proves a persisted change even if the last probe failed.
    assert reporting.observed_lead_death_phase(
        observed(changed), artifact={'status': 'present', 'bytes': 20}) == 'edited'


@pytest.mark.parametrize('changed', [False, None])
def test_nonempty_worker_delta_survives_stale_or_missing_observation(changed):
    for status, phase in [('completed', 'delivered_not_adopted'), ('blocked', 'edited_no_delivery')]:
        assert reporting.observed_worker_death_phase(
            review_decision='discard', status=status, delta_status='present',
            delta_bytes=20, worktree=observed(changed)) == phase


def marked_result():
    return {
        'schema_version': 12, 'death_phase_semantics': SEMANTICS,
        'lead_edit_attempted': False, 'lead_worktree': observed(True),
        'lead_death_phase': 'edited',
        'workers': [
            {'worker_id': 'worker-1', 'review_decision': 'adopt',
             'status': 'completed', 'delta_status': 'present', 'delta_bytes': 20,
             'worktree': observed(True), 'death_phase': 'adopted'},
            {'worker_id': 'worker-2', 'review_decision': 'discard',
             'status': 'blocked', 'delta_status': 'present', 'delta_bytes': 0,
             'worktree': observed(False), 'death_phase': 'no_edit'},
        ],
    }


def test_explicit_semantics_validates_without_mutating_result():
    result = marked_result()
    before = deepcopy(result)
    evidence = reporting.death_phase_evidence(result)
    assert evidence['semantics'] == evidence['declared_semantics'] == SEMANTICS
    assert evidence['lead_death_phase'] == 'edited'
    assert evidence['worker_death_phases'] == {'worker-1': 'adopted', 'worker-2': 'no_edit'}
    assert evidence['findings'] == []
    assert result == before


def test_schema12_without_marker_preserves_historical_mislabel_as_legacy():
    result = marked_result()
    result.pop('death_phase_semantics')
    result.pop('lead_edit_attempted')
    # Old schema 12 recorded the command-attempt heuristic despite a real change.
    result['lead_death_phase'] = 'no_edit'
    result['workers'][1]['worktree'] = observed(True)
    for worker in result['workers']:
        worker.pop('review_decision')
    before = deepcopy(result)
    evidence = reporting.death_phase_evidence(result)
    assert evidence['semantics'] == 'legacy_command_attempt_delivery'
    assert evidence['declared_semantics'] is None
    assert evidence['lead_death_phase'] == 'no_edit'
    assert evidence['worker_death_phases']['worker-2'] == 'no_edit'
    assert evidence['findings'] == []
    assert result == before


@pytest.mark.parametrize('actor', ['lead', 'worker-2'])
def test_new_marker_exposes_wrong_phase_without_rewriting_it(actor):
    result = marked_result()
    if actor == 'lead':
        result['lead_death_phase'] = 'no_edit'
    else:
        result['workers'][1]['death_phase'] = 'edited_no_delivery'
    before = deepcopy(result)
    evidence = reporting.death_phase_evidence(result)
    assert evidence['findings']
    assert all(isinstance(code, str) for code in evidence['findings'])
    assert evidence['lead_death_phase'] == result['lead_death_phase']
    assert evidence['worker_death_phases']['worker-2'] == result['workers'][1]['death_phase']
    assert result == before


def test_unsupported_semantics_is_explicit_and_raw_values_survive():
    result = marked_result()
    result['death_phase_semantics'] = 'future_unrecognized_v9'
    before = deepcopy(result)
    evidence = reporting.death_phase_evidence(result)
    assert evidence['declared_semantics'] == 'future_unrecognized_v9'
    assert 'unsupported_death_phase_semantics' in evidence['findings']
    assert evidence['lead_death_phase'] == result['lead_death_phase']
    assert result == before


@pytest.mark.parametrize('command,attempted,phase', [
    ('edit-lead', False, 'edited'),
    (ATTEMPT_WITHOUT_WRITE, True, 'no_edit'),
])
def test_runner_lead_phase_follows_observation_instead_of_command_pattern(
        make_run, command, attempted, phase):
    run, _, _ = make_run(condition='solo', lead=[[action('bash', command=command), done()]])
    result = run.run()
    assert result['infrastructure_error'] is None
    assert result['death_phase_semantics'] == SEMANTICS
    assert result['lead_edit_attempted'] is attempted
    assert result['lead_worktree']['observed_persisted_change'] is (phase == 'edited')
    assert result['lead_death_phase'] == phase
    assert reporting.death_phase_evidence(result)['findings'] == []


@pytest.mark.parametrize('command,attempted,phase', [
    ('edit-production', False, 'edited_no_delivery'),
    (ATTEMPT_WITHOUT_WRITE, True, 'no_edit'),
])
def test_runner_blocked_worker_phase_follows_observed_change(
        make_run, command, attempted, phase):
    run, _, _ = make_run(
        workers={'worker-1': [[action('bash', command=command), done('blocked')]]},
        lead=[settle('worker-1', decision='discard') + settle('worker-2') + [done()]])
    result = run.run()
    worker = next(w for w in result['workers'] if w['worker_id'] == 'worker-1')
    assert result['infrastructure_error'] is None
    assert worker['review_decision'] == 'discard' and worker['status'] == 'blocked'
    assert worker['edit_attempted'] is attempted
    assert worker['worktree']['observed_persisted_change'] is (phase == 'edited_no_delivery')
    assert worker['death_phase'] == phase
    assert reporting.death_phase_evidence(result)['findings'] == []


def test_runner_preserves_delivered_and_adopted_phases_and_observes_adoption(make_run):
    run, _, _ = make_run(
        lead=[settle('worker-1', decision='discard') + settle('worker-2') + [done()]])
    result = run.run()
    workers = {worker['worker_id']: worker for worker in result['workers']}
    assert result['infrastructure_error'] is None
    assert result['lead_edit_attempted'] is False
    assert result['lead_worktree']['first_change_origin'] == 'adoption'
    assert result['lead_death_phase'] == 'edited'
    assert workers['worker-1']['review_decision'] == 'discard'
    assert workers['worker-1']['death_phase'] == 'delivered_not_adopted'
    assert workers['worker-2']['review_decision'] == 'adopt'
    assert workers['worker-2']['death_phase'] == 'adopted'
    assert reporting.death_phase_evidence(result)['findings'] == []


    audit = Audit(run.folder.parent.parent)
    assert audit.forensics(run.folder, result)['death_phase_evidence']['findings'] == []
    assert audit.findings == []


@pytest.mark.parametrize('tamper,expected_code', [
    ('decision', 'worker_review_decision_evidence_mismatch'),
    ('delta', 'worker_delta_size_evidence_mismatch'),
])
def test_audit_rejects_self_consistent_labels_that_disagree_with_saved_evidence(
        tmp_path, tamper, expected_code):
    result = marked_result()
    run = tmp_path / 'run'
    for worker in result['workers']:
        path = run / worker['worker_id'] / 'delta.patch'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'x' * worker['delta_bytes'])
    events = [
        {'event': 'patch_frozen', 'lead_edit_detected': False},
        *({'event': 'tool_completed', 'tool': 'review_worker',
           'result': {'worker_id': worker['worker_id'], 'decision': worker['review_decision']}}
          for worker in result['workers']),
    ]
    (run / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
    audit = Audit(tmp_path)
    assert audit.forensics(run, result)['death_phase_evidence']['findings'] == []
    assert audit.findings == []
    if tamper == 'decision':
        # Result fields agree internally; the independent event detects tampering.
        result['workers'][0].update(review_decision='discard', death_phase='delivered_not_adopted')
    else:
        (run / 'worker-1/delta.patch').write_bytes(b'x' * 21)
    assert reporting.death_phase_evidence(result)['findings'] == []
    audit.forensics(run, result)
    assert expected_code in {finding['code'] for finding in audit.findings}


@pytest.mark.parametrize('marked', [False, True])
def test_report_and_audit_expose_same_semantics_without_rewriting_raw(tmp_path, marked):
    report_fixture(tmp_path)
    path = tmp_path / 'results/run-0/result.json'
    result = json.loads(path.read_text(encoding='utf-8'))
    result.update(lead_death_phase='no_edit', lead_edit_attempted=False)
    # The fixture intentionally has observed=True, exposing the historic mismatch.
    if marked:
        result['death_phase_semantics'] = SEMANTICS
    write_json(path, result)
    before = path.read_bytes()
    report = reporting.report(tmp_path)
    evidence = report['rows'][0]['death_phase_evidence']
    assert evidence['lead_death_phase'] == 'no_edit'
    assert bool(evidence['findings']) is marked
    assert evidence['semantics'] == (SEMANTICS if marked else 'legacy_command_attempt_delivery')
    if marked:
        assert set(evidence['findings']) <= {f['code'] for f in report['findings']}
    else:
        assert report['findings'] == []
    audit = Audit(tmp_path)
    forensics = audit.forensics(path.parent, result)
    assert forensics['death_phase_evidence'] == evidence
    assert forensics['lead_death_phase'] == 'no_edit'
    assert path.read_bytes() == before
