"""Offline gate selection tests using only synthetic inputs and source files."""
import json
from pathlib import Path
import sys

import pytest

from modelbench.swe_fixed_team_20260903 import cli


@pytest.fixture
def gate_workspace(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    here = repo / 'modelbench/swe_fixed_team_20260903'
    prior = repo / 'modelbench/swe_verified_20260903'
    official = prior / 'official'
    validation = here / 'validation'
    official.mkdir(parents=True)
    validation.mkdir(parents=True)
    old = prior / 'pilot_v2'
    old.mkdir()
    write = lambda path, value: path.write_text(json.dumps(value), encoding='utf-8')
    write(old / 'USER_STOP.json', {'all_started_runs_completed': True,
          'inflight_model_calls_at_stop': 0, 'completed_calls': 0, 'candidate_tokens': 0})
    write(old / 'manifest.json', {'fixture': True})
    (official / 'verified.parquet').write_bytes(b'opaque fixture bytes')
    write(official / 'versions.json', {'dataset_sha256': cli.sha(official / 'verified.parquet')})
    instance = {'instance_id': 'sphinx-doc__sphinx-8035', 'repo': 'fixture/project',
                'base_commit': 'a' * 40, 'version': 'fixture', 'problem_statement': 'Synthetic task'}
    write(official / 'selected_public.json', [instance])
    write(official / 'grader_controller.json', {'image_id': 'fixture-only'})
    (prior / 'environment.py').write_text('# synthetic source\n', encoding='utf-8')
    runtime_sources = {cli.ENV_SOURCE: cli.sha(prior / 'environment.py')}
    artifact = validation / 'fixture.junit.xml'
    artifact.write_text('<testsuite tests="1" failures="0" errors="0"/>', encoding='utf-8')
    default = validation / 'gate_revision12.json'
    live = validation / 'gate_live_fixture.json'
    gate = {'status': 'PASS', 'runtime_sources': runtime_sources,
            'validation_artifacts': {'fixture.junit.xml': cli.sha(artifact)}}
    write(default, {**gate, 'status': 'OFFLINE_PASS'})
    write(live, gate)
    for name, value in {'REPO': repo, 'HERE': here, 'PRIOR': prior, 'OFFICIAL': official,
                        'GATE_PATH': default}.items():
        monkeypatch.setattr(cli, name, value)
    monkeypatch.setattr(cli, 'INPUT_NAMES', ('verified.parquet', 'versions.json',
                                          'selected_public.json', 'grader_controller.json'))
    monkeypatch.setattr(cli, 'sources', lambda: dict(runtime_sources))
    monkeypatch.setattr(cli, 'report', lambda batch: None)
    return {'batch': tmp_path / 'one-run', 'default': default, 'live': live,
            'gate': gate, 'artifact': artifact, 'official': official,
            'runtime_sources': runtime_sources, 'instance_id': instance['instance_id']}


def prepare_one(workspace, **kwargs):
    return cli.prepare(workspace['batch'], only_instances=[workspace['instance_id']],
                       only_arms=['hetero_gpt-5.6-terra__glm-5.3'], wave='hetero16', **kwargs)


def test_default_offline_gate_still_rejects_before_batch_creation(gate_workspace):
    with pytest.raises(AssertionError, match='Validation missing'):
        prepare_one(gate_workspace)
    assert not gate_workspace['batch'].exists()


@pytest.mark.parametrize('status', ['OFFLINE_PASS', 'FAIL'])
def test_explicit_gate_does_not_bypass_status(gate_workspace, status):
    gate_workspace['live'].write_text(json.dumps({**gate_workspace['gate'], 'status': status}))
    with pytest.raises(AssertionError, match='Validation missing'):
        prepare_one(gate_workspace, validation_gate_path=gate_workspace['live'])
    assert not gate_workspace['batch'].exists()


def test_explicit_gate_rejects_changed_runtime_sources(gate_workspace):
    gate_workspace['runtime_sources'][cli.ENV_SOURCE] = 'changed'
    with pytest.raises(AssertionError, match='sources changed'):
        prepare_one(gate_workspace, validation_gate_path=gate_workspace['live'])
    assert not gate_workspace['batch'].exists()


def test_explicit_gate_binds_exact_single_schedule_and_snapshots(gate_workspace):
    default_before = gate_workspace['default'].read_bytes()
    manifest = prepare_one(gate_workspace, validation_gate_path=gate_workspace['live'])
    assert manifest['validation_gate_path'] == str(gate_workspace['live'].resolve())
    assert manifest['validation_gate_sha256'] == cli.sha(gate_workspace['live'])
    assert manifest['scheduled_runs'] == len(manifest['schedule']) == 1
    assert manifest['selection']['rule'] == (
        'Explicitly requested frozen public instances in original order; '
        'selection rationale recorded in validation gate/plan')
    entry = manifest['schedule'][0]
    assert entry['lead_model'] == 'gpt-5.6-sol'
    assert entry['worker_models'] == ['gpt-5.6-terra', 'glm-5.3']
    assert entry['effective_limits']['token_limit'] == 600_000
    assert entry['effective_limits']['max_calls'] == 28
    assert entry['effective_limits']['cm_call_allowance'] == 12
    assert len(entry['configuration_sha256']) == 64
    assert entry['grader_contract']['input_artifacts'] == manifest['input_artifacts']
    snapshot = gate_workspace['batch'] / 'validation_snapshot'
    assert (snapshot / 'gate.json').read_bytes() == gate_workspace['live'].read_bytes()
    assert (snapshot / 'fixture.junit.xml').read_bytes() == gate_workspace['artifact'].read_bytes()
    assert gate_workspace['default'].read_bytes() == default_before
    assert cli.verify(gate_workspace['batch']) == manifest


def test_explicit_gate_artifact_hash_is_still_checked(gate_workspace):
    gate_workspace['artifact'].write_text('changed after validation', encoding='utf-8')
    with pytest.raises(ValueError, match='validation artifact'):
        prepare_one(gate_workspace, validation_gate_path=gate_workspace['live'])
    assert not gate_workspace['batch'].exists()


@pytest.mark.parametrize('changed', ['gate', 'source', 'input'])
def test_verify_retains_all_current_fingerprint_checks(gate_workspace, changed):
    prepare_one(gate_workspace, validation_gate_path=gate_workspace['live'])
    if changed == 'gate':
        gate_workspace['live'].write_text('{}', encoding='utf-8')
    elif changed == 'source':
        gate_workspace['runtime_sources'][cli.ENV_SOURCE] = 'changed'
    else:
        (gate_workspace['official'] / 'verified.parquet').write_bytes(b'changed')
    with pytest.raises(AssertionError, match='drift'):
        cli.verify(gate_workspace['batch'])


def test_prepare_cli_forwards_explicit_gate(gate_workspace, monkeypatch):
    received = {}
    def prepare(batch, **kwargs):
        received.update(batch=batch, **kwargs)
        batch.mkdir()
        (batch / 'manifest.json').write_text('{}', encoding='utf-8')
        return {'schedule': [None]}
    monkeypatch.setattr(cli, 'prepare', prepare)
    monkeypatch.setattr(sys, 'argv', ['cli', 'prepare', '--batch', str(gate_workspace['batch']),
                                    '--gate', str(gate_workspace['live'])])
    cli.main()
    assert received['validation_gate_path'] == Path(gate_workspace['live'])


@pytest.mark.parametrize('command', ['run', 'report'])
def test_gate_option_cannot_rebind_an_existing_batch(command, gate_workspace, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['cli', command, '--batch', str(gate_workspace['batch']),
                                    '--gate', str(gate_workspace['live'])])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
