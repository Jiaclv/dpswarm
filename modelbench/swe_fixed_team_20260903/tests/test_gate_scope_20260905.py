"""Declared live gate scope is enforced before preparing or running a batch."""
import json

import pytest

from modelbench.swe_fixed_team_20260903 import cli
from modelbench.swe_fixed_team_20260903.tests.test_explicit_gate_revision12 import gate_workspace, prepare_one


def scoped_gate(workspace):
    return {**workspace['gate'], 'live_run_admission': True,
            'input_artifacts': cli.input_artifacts(),
            'allowed_schedule': [{'instance_id':workspace['instance_id'],
                'arm':'hetero_gpt-5.6-terra__glm-5.3', 'repetitions':1}],
            'requested_roles': {'lead':'gpt-5.6-sol', 'production':'gpt-5.6-terra',
                                'tests':'glm-5.3', 'cm':'deepseek-v4-flash'},
            'effective_limits': dict(cli.LIMITS)}


def save_gate(workspace, gate):
    workspace['live'].write_text(json.dumps(gate),encoding='utf-8')


@pytest.mark.parametrize('mismatch', ['instance','arm','repetitions','lead','worker_order','cm',
                                   'limits','inputs','admission','malformed_repetitions','duplicate_scope'])
def test_declared_gate_scope_rejected_before_batch_creation(gate_workspace, mismatch):
    gate = scoped_gate(gate_workspace)
    if mismatch in ('instance','arm','repetitions'):
        key = 'instance_id' if mismatch == 'instance' else mismatch
        gate['allowed_schedule'][0][key] = 2 if key == 'repetitions' else 'not-approved'
    elif mismatch == 'lead':
        gate['requested_roles']['lead'] = 'gpt-5.6-luna'
    elif mismatch == 'worker_order':
        gate['requested_roles'].update(production='glm-5.3', tests='gpt-5.6-terra')
    elif mismatch == 'cm':
        gate['requested_roles']['cm'] = 'glm-5.3'
    elif mismatch == 'limits':
        gate['effective_limits']['token_limit'] = 12345
    elif mismatch == 'inputs':
        gate['input_artifacts']['verified.parquet'] = 'changed-before-freeze'
    elif mismatch == 'admission':
        gate['live_run_admission'] = False
    elif mismatch == 'malformed_repetitions':
        gate['allowed_schedule'][0]['repetitions'] = True
    else:
        gate['allowed_schedule'].append(dict(gate['allowed_schedule'][0]))
    save_gate(gate_workspace, gate)
    with pytest.raises(ValueError,match='[Gg]ate'):
        prepare_one(gate_workspace,validation_gate_path=gate_workspace['live'])
    assert not gate_workspace['batch'].exists()


def test_matching_scope_prepares_once_and_verify_preserves_binding(gate_workspace):
    save_gate(gate_workspace, scoped_gate(gate_workspace))
    manifest = prepare_one(gate_workspace,validation_gate_path=gate_workspace['live'])
    assert cli.verify(gate_workspace['batch']) == manifest


@pytest.mark.parametrize('change',['extra_run','worker_swap','budget','instance'])
def test_verify_rechecks_gate_scope_before_any_runtime_resources(gate_workspace,change):
    save_gate(gate_workspace, scoped_gate(gate_workspace))
    manifest = prepare_one(gate_workspace,validation_gate_path=gate_workspace['live'])
    entry = manifest['schedule'][0]
    if change == 'extra_run':
        manifest['schedule'].append({**entry,'run_id':entry['run_id']+'.extra'})
    elif change == 'worker_swap':
        entry['worker_models'].reverse()
    elif change == 'budget':
        entry['effective_limits']['token_limit'] += 1
    else:
        entry['instance']['instance_id'] = 'not-approved'
    (gate_workspace['batch']/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='[Gg]ate'):
        cli.verify(gate_workspace['batch'])


def test_changed_validation_artifact_rejected_before_batch_creation(gate_workspace):
    save_gate(gate_workspace,scoped_gate(gate_workspace))
    gate_workspace['artifact'].write_text('changed')
    with pytest.raises((ValueError,AssertionError)):
        prepare_one(gate_workspace,validation_gate_path=gate_workspace['live'])
    assert not gate_workspace['batch'].exists()


@pytest.mark.parametrize('location', ['current', 'snapshot'])
def test_verify_rejects_validation_evidence_drift(gate_workspace, location):
    save_gate(gate_workspace, scoped_gate(gate_workspace))
    prepare_one(gate_workspace,validation_gate_path=gate_workspace['live'])
    artifact = (gate_workspace['artifact'] if location == 'current' else
                gate_workspace['batch']/'validation_snapshot/fixture.junit.xml')
    artifact.write_text('changed after prepare',encoding='utf-8')
    with pytest.raises(ValueError,match='validation artifact'):
        cli.verify(gate_workspace['batch'])


def test_artifact_must_stay_inside_validation_directory(gate_workspace):
    outside = gate_workspace['live'].parent.parent/'outside.json'
    outside.write_text('{}',encoding='utf-8')
    gate = scoped_gate(gate_workspace)
    gate['validation_artifacts'] = {'../outside.json':cli.sha(outside)}
    save_gate(gate_workspace,gate)
    with pytest.raises(ValueError,match='escapes validation'):
        prepare_one(gate_workspace,validation_gate_path=gate_workspace['live'])
    assert not gate_workspace['batch'].exists()


@pytest.mark.parametrize('change',['override','base_commit','problem_statement','grader_contract','configuration_hash'])
def test_run_refuses_entry_drift_before_image_or_model_admission(gate_workspace,monkeypatch,change):
    save_gate(gate_workspace, scoped_gate(gate_workspace))
    manifest = prepare_one(gate_workspace,validation_gate_path=gate_workspace['live'])
    entry = manifest['schedule'][0]
    if change == 'override':
        entry['limits_override'] = {'token_limit':700000}
    elif change == 'base_commit':
        entry['instance']['base_commit'] = 'b'*40
    elif change == 'problem_statement':
        entry['instance']['problem_statement'] = 'changed task'
    elif change == 'grader_contract':
        entry['grader_contract']['environment_sha'] = 'changed'
    else:
        entry['configuration_sha256'] = 'changed'
    (gate_workspace['batch']/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    def unexpected(*args,**kwargs):
        pytest.fail('Invalid frozen entry reached resource admission')
    monkeypatch.setattr(cli,'ensure_image',unexpected)
    monkeypatch.setattr(cli,'SweRun',unexpected)
    with pytest.raises(ValueError,match='Gate runtime'):
        cli.run(gate_workspace['batch'])


def test_declared_null_roles_are_rejected(gate_workspace):
    gate = scoped_gate(gate_workspace)
    gate['requested_roles'] = None
    save_gate(gate_workspace,gate)
    with pytest.raises(ValueError,match='requested_roles'):
        prepare_one(gate_workspace,validation_gate_path=gate_workspace['live'])
    assert not gate_workspace['batch'].exists()
