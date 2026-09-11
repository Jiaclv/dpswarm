"""Offline grading barrier and immutable-contract tests; fake graders only."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('tested_parallel_episode',ROOT/'parallel_episode.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
r=p.resources


@pytest.fixture
def group(tmp_path):
    batch=tmp_path/'batch';batch.mkdir();r.atomic_json(batch/'manifest.json',{})
    directory=tmp_path/'group';directory.mkdir()
    ids=['a1-01-S','a1-02-T','a1-02-L','a1-02-R2']
    policy={'execution_mode':'parallel_pilot','max_parallel_episodes':4,'per_episode_model_slots':4,
            'global_model_slots':8,'candidate_container_cap':7,'grader_max_containers':2,
            'memory_admission_mode':'monitored_overcommit','monitor_is_hard_limit':False,
            'manifest_sha256':r.sha(batch/'manifest.json'),'run_ids':ids}
    pp=directory/'policy.json';r.atomic_json(pp,policy)
    value={'version':1,'batch':str(batch),'run_ids':ids,'manifest_sha256':r.sha(batch/'manifest.json'),
           'global_model_slots':8,'candidate_container_cap':7,'global_lock_dir':str(directory/'locks'),
           'wait_timeout_seconds':1,'grader_wait_timeout_seconds':1,'policy_path':str(pp),'policy_sha256':r.sha(pp)}
    value['operations_sources']={name:r.sha(ROOT/name) for name in ('parallel_controller.py','recovery_controller.py','parallel_episode.py','parallel_resources.py')}
    r.atomic_json(directory/'group.json',value)
    return directory,value,r.sha(directory/'group.json')


def mark(group, run_id, phase='candidate_closed', cleanup=True):
    directory,value,digest=group
    return p.candidate_marker(directory,digest,run_id,phase=phase,cleanup_confirmed=cleanup)


def test_load_exact_group_and_refuse_policy_drift(group):
    directory,value,digest=group
    assert p.load_group(directory,value['batch'],value['run_ids'][0])[1]==digest
    Path(value['policy_path']).write_text('{}')
    with pytest.raises(r.ResourceError,match='policy changed'):
        p.load_group(directory,value['batch'],value['run_ids'][0])


def test_candidate_marker_cannot_rewrite_failed_to_closed(group):
    directory,value,digest=group
    mark(group,value['run_ids'][0],phase='candidate_failed')
    with pytest.raises(r.ResourceError,match='cannot be replaced'):
        mark(group,value['run_ids'][0])


def test_barrier_waits_for_all_closures_then_checks_absence_before_grading(group,tmp_path):
    directory,value,digest=group
    patch=tmp_path/'patch';patch.write_text('patch')
    calls=[];now=[0.0]
    def sleep(seconds):
        now[0]+=seconds
        if now[0]>.1:
            for run_id in value['run_ids'][1:]:mark(group,run_id)
    def grade(*args,**kwargs):calls.append('grade');return {'completed':True}
    def absent(cli,path):calls.append(Path(path).name);return []
    barrier=p.BarrierGrader(directory,value,digest,value['run_ids'][0],grader=grade,cli=None,
                           clock=lambda:now[0],sleep=sleep,absent=absent)
    assert barrier({},patch,r.sha(patch),tmp_path/'grade')['completed']
    assert calls[-1]=='grade' and len(calls)==6
    assert barrier.records[0]['generation_barrier_seconds']>.1
    assert not list((directory/'locks'/'grader').glob('*.lock'))


@pytest.mark.parametrize('phase,cleanup',[('candidate_failed',True),('candidate_closed',False)])
def test_failed_or_unclean_candidate_never_allows_grading(group,phase,cleanup):
    directory,value,digest=group
    for run_id in value['run_ids']:mark(group,run_id,phase=phase,cleanup=cleanup)
    barrier=p.BarrierGrader(directory,value,digest,value['run_ids'][0],grader=lambda *a,**k:pytest.fail('grader called'),cli=None)
    with pytest.raises(r.ResourceError,match='failed or cleanup'):
        barrier.wait_candidates()
    assert (directory/'CANCEL').exists()


def test_barrier_timeout_never_calls_grader(group):
    directory,value,digest=group
    now=[0.0]
    def sleep(seconds):now[0]+=seconds
    barrier=p.BarrierGrader(directory,value,digest,value['run_ids'][0],grader=lambda *a,**k:pytest.fail('grader called'),cli=None,
                           clock=lambda:now[0],sleep=sleep)
    with pytest.raises(r.ResourceError,match='barrier deadline'):
        barrier.wait_candidates()


def test_group_change_while_waiting_fails_closed(group):
    directory,value,digest=group
    barrier=p.BarrierGrader(directory,value,digest,value['run_ids'][0],grader=None,cli=None)
    r.atomic_json(directory/'group.json',value|{'extra':True})
    with pytest.raises(r.ResourceError,match='group changed'):
        barrier.wait_candidates()


def test_confirm_absent_rejects_exists_or_docker_unavailable(tmp_path):
    cli=SimpleNamespace(_recorded_containers=lambda _: {'owned':'owner'})
    assert p.confirm_absent(cli,tmp_path,inspect=lambda _:SimpleNamespace(returncode=1,stderr='No such container'))==['owned']
    for rc,error in [(0,''),(1,'Cannot connect to Docker')]:
        with pytest.raises(r.ResourceError,match='absence is unknown'):
            p.confirm_absent(cli,tmp_path,inspect=lambda _:SimpleNamespace(returncode=rc,stderr=error))


def test_patch_mutation_while_waiting_never_calls_grader(group,tmp_path):
    directory,value,digest=group
    patch=tmp_path/'patch';patch.write_text('old');expected=r.sha(patch)
    now=[0.0]
    def sleep(seconds):
        now[0]+=seconds;patch.write_text('changed')
        for run_id in value['run_ids'][1:]:mark(group,run_id)
    barrier=p.BarrierGrader(directory,value,digest,value['run_ids'][0],grader=lambda *a,**k:pytest.fail('grader called'),cli=None,
                           absent=lambda *a:[],clock=lambda:now[0],sleep=sleep)
    with pytest.raises(r.ResourceError,match='changed in grading queue'):
        barrier({},patch,expected,tmp_path/'grade')


def test_grader_lock_released_even_when_fake_grader_fails(group,tmp_path):
    directory,value,digest=group
    for run_id in value['run_ids']:mark(group,run_id)
    patch=tmp_path/'patch';patch.write_text('patch')
    def grade(*args,**kwargs):raise RuntimeError('fake failure')
    barrier=p.BarrierGrader(directory,value,digest,value['run_ids'][0],grader=grade,cli=None,absent=lambda *a:[])
    with pytest.raises(RuntimeError,match='fake failure'):
        barrier({},patch,r.sha(patch),tmp_path/'grade')
    assert not list((directory/'locks'/'grader').glob('*.lock'))
    assert len(barrier.records)==1


def bounded_group(group, ids):
    directory, value, digest = group
    policy = r.read(value['policy_path']) | {'run_ids': ids, 'memory_admission_mode': 'bounded_container_limits',
        'candidate_memory': '1g', 'grader_memory': '1g', 'resource_profile': 'resource-defaults.json'}
    r.atomic_json(value['policy_path'], policy)
    value = value | {'run_ids': ids, 'profile_path': str(ROOT/'resource-defaults.json'),
                     'profile_sha256': r.sha(ROOT/'resource-defaults.json'),
                     'policy_sha256': r.sha(value['policy_path'])}
    value['operations_sources'] = value['operations_sources'] | {
        name: r.sha(ROOT/name) for name in ('memory_profile.py', 'resource-defaults.json')}
    r.atomic_json(directory/'group.json', value)
    return directory, value, r.sha(directory/'group.json')


@pytest.mark.parametrize('ids', [['remaining-one'], ['remaining-S', 'remaining-D']])
def test_bounded_profile_admits_partial_final_groups(group, ids):
    directory, value, digest = bounded_group(group, ids)
    loaded, got, policy = p.load_group(directory, value['batch'], ids[0])
    assert got == digest and loaded['run_ids'] == ids
    assert policy['candidate_memory'] == policy['grader_memory'] == '1g'


def test_bounded_profile_refuses_unbound_installer_or_config(group):
    directory, value, digest = bounded_group(group, ['remaining-S', 'remaining-D'])
    value['operations_sources'].pop('memory_profile.py')
    r.atomic_json(directory/'group.json', value)
    with pytest.raises(r.ResourceError, match='source identity'):
        p.load_group(directory, value['batch'], 'remaining-S')


def test_two_episode_barrier_grades_only_after_both_closed(group, tmp_path):
    group = bounded_group(group, ['remaining-S', 'remaining-D'])
    directory, value, digest = group
    patch = tmp_path/'patch'; patch.write_text('patch')
    calls = []; now = [0.0]
    def sleep(seconds):
        now[0] += seconds
        mark(group, 'remaining-D')
    barrier = p.BarrierGrader(directory, value, digest, 'remaining-S',
        grader=lambda *a, **k: calls.append('grade'), cli=None, absent=lambda *a: [],
        clock=lambda: now[0], sleep=sleep)
    barrier({}, patch, r.sha(patch), tmp_path/'grade')
    assert calls == ['grade'] and barrier.records[0]['generation_barrier_seconds'] > 0


def test_profile_install_returns_new_grader_alias_and_durable_receipt(monkeypatch, tmp_path):
    called = []
    replacement = lambda *a, **k: None
    fake_environment = SimpleNamespace(rootgrade_terminal='old alias')
    def install(path, receipt):
        called.append((path, receipt))
        fake_environment.rootgrade_terminal = replacement
        return {'status': 'installed', 'profile_sha256': 'profile-hash'}
    fake_profile = SimpleNamespace(install=install)
    fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
    monkeypatch.setattr(p.importlib.util, 'spec_from_file_location', lambda *a: fake_spec)
    monkeypatch.setattr(p.importlib.util, 'module_from_spec', lambda *a: fake_profile)
    monkeypatch.setattr(p.importlib, 'import_module', lambda *a: fake_environment)
    group = {'profile_path': 'profile-file', 'profile_sha256': 'profile-hash'}
    assert p.install_memory_profile(group, {'memory_admission_mode': 'bounded_container_limits'}, tmp_path) is replacement
    assert called == [('profile-file', tmp_path/'memory-profile-installation.json')]


def twelve_group(group):
    directory, value, digest = bounded_group(group, ['remaining-S', 'remaining-D'])
    policy = r.read(value['policy_path']) | {'max_parallel_episodes':12, 'candidate_container_cap':12,
        'provider_limits_version':1, 'provider_limits_source':'operator_configured',
        'provider_limits':{'codex_account':4, 'glm_coding':1, 'deepseek':4}}
    r.atomic_json(value['policy_path'], policy)
    value = value | {'candidate_container_cap':12, 'policy_sha256':r.sha(value['policy_path']),
        'episode_admission_mode':'atomic_episode_candidate_quota',
        'candidate_requirements':{'remaining-S':1,'remaining-D':2},
        'wait_timeout_seconds':5700,'grader_wait_timeout_seconds':21900}
    value.update({key:policy[key] for key in ('provider_limits','provider_limits_version','provider_limits_source')})
    value['operations_sources']['provider_limits.py'] = r.sha(ROOT/'provider_limits.py')
    r.atomic_json(directory/'group.json', value)
    digest = r.sha(directory/'group.json')
    for run_id, quota in value['candidate_requirements'].items():
        r.atomic_json(directory/'admissions'/(run_id+'.json'), {'run_id':run_id, 'group_sha256':digest,
            'candidate_quota':quota, 'status':'admitted'})
    return directory, value, digest


def test_twelve_mode_requires_prior_full_episode_capacity_receipt(group):
    directory, value, digest = twelve_group(group)
    assert p.load_group(directory, value['batch'], 'remaining-S')[1] == digest
    receipt = directory/'admissions'/'remaining-S.json'
    r.atomic_json(receipt, r.read(receipt) | {'candidate_quota':3})
    with pytest.raises(r.ResourceError, match='admission receipt'):
        p.load_group(directory, value['batch'], 'remaining-S')


def test_twelve_barrier_waits_for_supervisor_capacity_release_before_grading(group, tmp_path):
    group = twelve_group(group)
    directory, value, digest = group
    for run_id in value['run_ids']:mark(group, run_id)
    patch=tmp_path/'patch';patch.write_text('patch')
    now=[0.0];calls=[]
    def sleep(seconds):
        now[0] += seconds
        if now[0] >= .2:
            for run_id, quota in value['candidate_requirements'].items():
                r.atomic_json(directory/'candidate-releases'/(run_id+'.json'), {
                    'run_id':run_id, 'group_sha256':digest, 'candidate_quota':quota, 'status':'released'})
    barrier=p.BarrierGrader(directory,value,digest,'remaining-S',grader=lambda *a,**k:calls.append('grade'),
        cli=None,absent=lambda *a:[],clock=lambda:now[0],sleep=sleep)
    barrier({},patch,r.sha(patch),tmp_path/'grade')
    assert calls == ['grade'] and barrier.records[0]['generation_barrier_seconds'] >= .2
