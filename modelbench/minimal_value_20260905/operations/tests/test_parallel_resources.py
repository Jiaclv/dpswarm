"""Offline resource admission/failure tests. Never invokes a model or Docker."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tested_parallel_resources', ROOT / 'parallel_resources.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def sample(**overrides):
    return {'owned_memory_bytes': 1024, 'host_available_bytes': 4*r.GIB,
            'docker_remaining_estimate_bytes': 10*r.GIB, 'candidate_container_count': 0,
            'grader_container_count': 0, **overrides}


def test_shared_slots_bound_two_instances_and_release(tmp_path):
    a, b = r.FileSemaphore(tmp_path, 1), r.FileSemaphore(tmp_path, 1)
    assert a.acquire(timeout=0)
    assert b.acquire(timeout=0) is False
    a.release()
    assert b.acquire(timeout=0)
    b.release()
    assert list(tmp_path.glob('*.lock')) == []


def test_slots_work_across_real_processes_without_external_calls(tmp_path):
    held = r.FileSemaphore(tmp_path / 'slots', 1)
    assert held.acquire(timeout=0)
    script = '''import importlib.util,sys
s=importlib.util.spec_from_file_location("resources",sys.argv[1]);r=importlib.util.module_from_spec(s);s.loader.exec_module(r)
x=r.FileSemaphore(sys.argv[2],1)
ok=x.acquire(timeout=0)
if ok:x.release()
print(ok)
'''
    def child():
        return subprocess.run([sys.executable, '-B', '-c', script, str(ROOT/'parallel_resources.py'), str(tmp_path/'slots')],
                              capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    assert child() == 'False'
    held.release()
    assert child() == 'True'


def test_owner_death_does_not_auto_reclaim_slot(tmp_path):
    (tmp_path/'slot-0.lock').write_text(json.dumps({'pid': 999999999, 'token': 'dead'}))
    assert r.FileSemaphore(tmp_path, 1).acquire(timeout=0) is False


def test_recursive_or_forged_release_fails_closed(tmp_path):
    x = r.FileSemaphore(tmp_path, 1)
    assert x.acquire(timeout=0)
    with pytest.raises(r.ResourceError, match='Recursive'):
        x.acquire(timeout=0)
    (tmp_path/'slot-0.lock').write_text(json.dumps({'pid': 0, 'token': 'forged'}))
    with pytest.raises(r.ResourceError, match='ownership'):
        x.release()
    assert (tmp_path/'slot-0.lock').exists()


def test_combined_bound_retains_local_limit_and_frees_on_global_failure(tmp_path):
    global_slot = r.FileSemaphore(tmp_path/'global', 1)
    holder = r.FileSemaphore(tmp_path/'global', 1)
    local = threading.BoundedSemaphore(1)
    both = r.CombinedSemaphore(local, global_slot)
    assert holder.acquire(timeout=0)
    assert both.acquire(timeout=0) is False
    assert local.acquire(timeout=0)
    local.release()
    holder.release()
    assert both.acquire(timeout=0)
    assert local.acquire(timeout=0) is False
    both.release()


def test_cancel_rejects_admission_and_returns_local_slot(tmp_path):
    group = tmp_path/'group'; group.mkdir()
    global_slot = r.FileSemaphore(tmp_path/'global', 1, group_dir=group)
    local = threading.BoundedSemaphore(1)
    (group/'CANCEL').touch()
    with pytest.raises(r.ResourceError, match='stopped'):
        r.CombinedSemaphore(local, global_slot).acquire(timeout=0)
    assert local.acquire(timeout=0)


@pytest.mark.parametrize('value,want', [('115MiB',115*1024**2), ('3GiB',3*r.GIB), ('2GB',2*10**9), ('0B',0)])
def test_memory_parser(value, want):
    assert r.memory_bytes(value) == want


@pytest.mark.parametrize('override,reason', [
    ({'owned_memory_bytes':8*r.GIB},'owned_memory_threshold'),
    ({'host_available_bytes':2*r.GIB-1},'host_available_threshold'),
    ({'docker_remaining_estimate_bytes':3*r.GIB-1},'docker_remaining_estimate_threshold'),
    ({'candidate_container_count':8},'candidate_container_cap_exceeded'),
    ({'grader_container_count':3},'grader_container_cap_exceeded'),
])
def test_monitor_trips_shared_cancel_on_each_policy_threshold(tmp_path, override, reason):
    monitor = r.ResourceMonitor(tmp_path, [], sample=lambda _:sample(**override))
    monitor.tick()
    assert (tmp_path/'CANCEL').exists()
    assert r.read(tmp_path/'TRIP.json')['reason'] == reason


def test_sample_failures_require_two_consecutive_and_reset(tmp_path):
    values = iter([RuntimeError(), sample(), RuntimeError(), RuntimeError()])
    def sampler(_):
        value = next(values)
        if isinstance(value, Exception): raise value
        return value
    monitor = r.ResourceMonitor(tmp_path, [], sample=sampler)
    for _ in range(3):
        monitor.tick()
        assert not (tmp_path/'TRIP.json').exists()
    monitor.tick()
    assert r.read(tmp_path/'TRIP.json')['reason'] == 'resource_sampling_unavailable'
    assert len((tmp_path/'resource-samples.jsonl').read_text().splitlines()) == 4


def test_sampling_classifies_both_grader_containers_from_request_owner(tmp_path):
    episode = tmp_path/'episode'; episode.mkdir()
    r.atomic_json(episode/'container-intent.json', {'owner':'candidate'})
    r.atomic_json(episode/'grade-1'/'grader'/'request.json', {'owner':'grader','grader_contract':{'frozen':True}})
    ids = ['a'*64,'b'*64,'c'*64]
    records = [{'Id':iid,'Config':{'Labels':{'dpswarm.swe.owner':owner}},'HostConfig':{'Memory':3*r.GIB,'NanoCpus':2*10**9}}
               for iid,owner in zip(ids,['candidate','grader','grader'])]
    def docker(args):
        if args[0]=='info': return json.dumps({'MemTotal':15*r.GIB,'NCPU':12})
        if args[0]=='ps': return '\n'.join(ids)
        if args[0]=='inspect': return json.dumps(records)
        if args[0]=='stats': return '\n'.join(json.dumps({'ID':i[:12],'MemUsage':'100MiB / 3GiB','CPUPerc':'12.5%'}) for i in ids)
        pytest.fail('unexpected Docker command')
    result = r.sample_resources([episode], docker=docker,
                virtual_memory=lambda:SimpleNamespace(available=4*r.GIB,total=32*r.GIB),cpu_percent=lambda:4.0)
    assert result['candidate_container_count']==1 and result['grader_container_count']==2
    assert result['owned_memory_bytes']==300*1024**2
    assert all(x['memory_limit_bytes']==3*r.GIB for x in result['containers'])


def test_monitor_from_group_uses_hash_bound_policy(tmp_path):
    policy = {'monitor_is_hard_limit':False,'memory_admission_mode':'monitored_overcommit',
              'candidate_container_cap':7,'grader_max_containers':2,
              'softguard':{'sample_seconds':2,'owned_memory_limit_gib':8,'host_available_min_gib':2,
                           'docker_available_min_gib':3,'max_consecutive_sample_failures':2}}
    path=tmp_path/'policy.json';r.atomic_json(path,policy)
    r.atomic_json(tmp_path/'group.json',{'policy_path':str(path),'policy_sha256':r.sha(path)})
    m=r.monitor_from_group(tmp_path,[],sample=lambda _:sample())
    assert m.policy['owned_memory_limit_bytes']==8*r.GIB
    path.write_text('{}')
    with pytest.raises(r.ResourceError,match='hash'):
        r.monitor_from_group(tmp_path,[])


def test_unlabelled_container_counts_in_vm_remaining_but_not_owned(tmp_path):
    records=[{'Id':'d'*64,'Config':{'Labels':{}},'HostConfig':{'Memory':0,'NanoCpus':0}}]
    def docker(args):
        if args[0]=='info':return json.dumps({'MemTotal':15*r.GIB,'NCPU':12})
        if args[0]=='ps':return 'd'*12
        if args[0]=='inspect':return json.dumps(records)
        if args[0]=='stats':return json.dumps({'ID':'d'*12,'MemUsage':'50MiB / 15GiB','CPUPerc':'0.1%'})
        pytest.fail('unexpected command')
    value=r.sample_resources([],docker=docker,virtual_memory=lambda:SimpleNamespace(available=4*r.GIB,total=32*r.GIB),cpu_percent=lambda:1)
    assert value['owned_memory_bytes']==0
    assert value['all_container_memory_bytes']==50*1024**2
    assert value['docker_remaining_estimate_bytes']==15*r.GIB-50*1024**2
    assert value['candidate_container_count']==value['grader_container_count']==value['unrelated_swe_container_count']==0

@pytest.mark.parametrize('memory,swap,trip', [
    (r.GIB, r.GIB, False), (768*1024**2, 768*1024**2, False),
    (3*r.GIB, 3*r.GIB, True), (r.GIB, 2*r.GIB, True), (0, 0, True),
])
def test_bounded_monitor_checks_actual_limits_without_increasing_auxiliary(tmp_path, memory, swap, trip):
    containers = [{'owned_by_group': True, 'memory_limit_bytes': memory, 'memory_swap_limit_bytes': swap}]
    monitor = r.ResourceMonitor(tmp_path, [], sample=lambda _: sample(containers=containers),
                                container_memory_limit_bytes=r.GIB)
    monitor.tick()
    assert (tmp_path/'TRIP.json').exists() is trip
    if trip:
        assert r.read(tmp_path/'TRIP.json')['reason'] == 'container_memory_limit_mismatch'


def test_bounded_monitor_does_not_apply_cap_to_unrelated_container(tmp_path):
    containers = [{'owned_by_group': False, 'memory_limit_bytes': 3*r.GIB, 'memory_swap_limit_bytes': -1}]
    monitor = r.ResourceMonitor(tmp_path, [], sample=lambda _: sample(containers=containers),
                                container_memory_limit_bytes=r.GIB)
    monitor.tick()
    assert not (tmp_path/'TRIP.json').exists()


def test_monitor_from_bounded_group_applies_creation_cap_audit(tmp_path):
    policy = {'monitor_is_hard_limit':False, 'memory_admission_mode':'bounded_container_limits',
        'candidate_memory':'1g', 'grader_memory':'1g', 'candidate_container_cap':7,'grader_max_containers':2,
        'softguard':{'sample_seconds':2,'owned_memory_limit_gib':8,'host_available_min_gib':2,
                    'docker_available_min_gib':3,'max_consecutive_sample_failures':2}}
    path=tmp_path/'policy.json';r.atomic_json(path,policy)
    r.atomic_json(tmp_path/'group.json',{'policy_path':str(path),'policy_sha256':r.sha(path)})
    monitor = r.monitor_from_group(tmp_path, [], sample=lambda _: sample())
    assert monitor.policy['container_memory_limit_bytes'] == r.GIB
