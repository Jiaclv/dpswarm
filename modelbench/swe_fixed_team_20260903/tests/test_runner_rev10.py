"""Revision 10 tests: assembly defaults OFF with per-arm opt-in, CM-pool scan
wave, pre-registered probe panel wave, and instance-grouped batch execution.
Offline only."""
import json
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_fixed_team_20260903 import cli, runner  # runner.py inserts dpswarm-plugin into sys.path
from modelbench.swe_fixed_team_20260903.tests.test_runner import action, done, events, make_run

ASSEMBLY_ON = {'cm_team_memory': True, 'cm_scout_distill': True, 'cm_bootstrap_package': True}


# ------------------------------------------------------------- new defaults

def test_team_assembly_defaults_off_but_remains_per_arm_opt_in():
    for flag in ('cm_team_memory', 'cm_scout_distill', 'cm_bootstrap_package'):
        assert runner.LIMITS[flag] is False, flag + ' must default to False in rev10'
    assert runner.LIMITS['cm_enabled'] is True  # on-demand compression unaffected


def test_default_team_cold_starts_without_assembly(make_run):
    run, env, _ = make_run(condition='hetero_team', worker_models=['gpt-5.6-terra', 'glm-5.3'])
    result = run.run()
    assert result['infrastructure_error'] is None
    kinds = [e['event'] for e in events(run)]
    for absent in ('scout_round_completed', 'scout_distilled', 'cm_assembly'):
        assert absent not in kinds, absent + ' must not run under rev10 defaults'
    assert not (run.folder / 'memory.jsonl').exists()
    assert not any((run.folder / w / 'context_package').exists() for w in ('worker-1', 'worker-2'))
    first = [s for s in run.transport.seen if s['actor'] == 'worker-1'][0]
    assert first['messages'][1]['content'].startswith('Runtime status')  # cold start, no package
    assert result['team_execution_status'] == 'workers_completed'


def test_assembly_opt_in_via_override_restores_scout_and_packages(make_run):
    run, env, _ = make_run(limits_override=ASSEMBLY_ON)
    assert all(run.limits[f] is True for f in ASSEMBLY_ON)
    result = run.run()
    assert result['infrastructure_error'] is None
    kinds = [e['event'] for e in events(run)]
    assert 'scout_round_completed' in kinds and 'scout_distilled' in kinds
    assert (run.folder / 'memory.jsonl').exists()
    for worker_id in ('worker-1', 'worker-2'):
        assert list((run.folder / worker_id / 'context_package').glob('*.manifest.json'))
    started = [e for e in events(run) if e['event'] == 'run_started'][0]
    assert started['limits'] == run.limits


# --------------------------------------------------------------------- waves

@pytest.fixture
def gate_fixture(tmp_path, monkeypatch):
    gate = {'status': 'PASS', 'runtime_sources': cli.sources(), 'validation_artifacts': {}}
    gate_path = tmp_path / 'gate_fixture.json'
    gate_path.write_text(json.dumps(gate), encoding='utf-8')
    monkeypatch.setattr(cli, 'GATE_PATH', gate_path)
    return gate_path


def test_cmscan13_manifest_freezes_pool_arms_and_staggered_pairs(tmp_path, gate_fixture):
    batch = tmp_path / 'pilot_fixture_v13'
    manifest = cli.prepare(batch, only_instances=['sphinx-doc__sphinx-8035'], wave='cmscan13')
    schedule = manifest['schedule']
    assert len(schedule) == 6
    ids = [e['run_id'] for e in schedule]
    assert len(set(ids)) == 6
    reps = {}
    for entry in schedule:
        reps[entry['arm']] = reps.get(entry['arm'], 0) + 1
        pool = entry['effective_limits']['cm_call_allowance']
        assert pool == (6 if entry['arm'].endswith('.cm6') else 24)
        assert entry['effective_limits']['max_calls'] == 28  # scan budget stays at the anchored wave
        for flag in ('cm_team_memory', 'cm_scout_distill', 'cm_bootstrap_package'):
            assert entry['effective_limits'][flag] is False  # assembly stays off in the scan
    assert reps == {'solo_gpt-5.6-sol.cm6': 2, 'solo_gpt-5.6-sol.cm24': 2,
                    'heterooff_gpt-5.6-terra__glm-5.3.cm6': 1, 'heterooff_gpt-5.6-terra__glm-5.3.cm24': 1}
    assert manifest['maximum_calls'] == 6 * 28
    for pair in [set(ids[i:i + 2]) for i in range(0, len(ids), 2)]:
        arms = {next(e['arm'] for e in schedule if e['run_id'] == i) for i in pair}
        assert len(arms) == len(pair), 'a pair holds two runs of one arm'


def test_probepanel_manifest_is_preregistered_and_excludes_anchors(tmp_path, gate_fixture):
    batch = tmp_path / 'pilot_fixture_v14'
    manifest = cli.prepare(batch, wave='probepanel')
    schedule = manifest['schedule']
    assert len(schedule) == 8
    assert [e['instance']['instance_id'] for e in schedule] == [
        r['instance_id'] for r in json.loads(
            (cli.OFFICIAL / 'selected_public.json').read_text(encoding='utf-8'))
        if r['instance_id'] not in cli.PROBE_ANCHORS]
    assert {e['arm'] for e in schedule} == {cli.PROBE_ARM}
    assert all(e['condition'] == 'solo' for e in schedule)
    assert all(e['effective_limits'] == {**runner.LIMITS} for e in schedule)
    assert 'Pre-registered' in manifest['selection']['rule'] and 'probe outcome' in manifest['selection']['rule']
    assert len({e['run_id'] for e in schedule}) == 8
    assert manifest['maximum_calls'] == 8 * 28


def test_instance_blocks_matches_historical_and_panel_layouts():
    def entry(iid, rid):
        return {'run_id': rid, 'instance': {'instance_id': iid}}
    historical = cli.instance_blocks([entry('sphinx', str(i)) for i in range(6)])
    assert len(historical) == 1 and len(historical[0][1]) == 6
    panel = cli.instance_blocks([entry(f'i{i}', str(i)) for i in range(8)])
    assert len(panel) == 8 and all(len(entries) == 1 for _, entries in panel)
    interleaved = cli.instance_blocks([entry('a', '1'), entry('b', '2'), entry('a', '3')])
    assert [key for key, _ in interleaved] == ['a', 'b', 'a']
