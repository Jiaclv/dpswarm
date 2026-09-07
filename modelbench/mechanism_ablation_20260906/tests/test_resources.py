"""Offline physical-role tests for the scoring supervisor. No Docker/API calls."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from modelbench.mechanism_ablation_20260906 import resources as r


def container(role, owner='grade-owner', identifier='a'):
    memory,cpus=r.ROLE_LIMITS[role]
    return {'id':identifier*12,'owner':owner,'resource_role':role,'owned_by_group':True,
        'used_memory_bytes':30*1024**2,'memory_limit_bytes':memory,
        'memory_swap_limit_bytes':memory,'nano_cpus':cpus,'cpu_percent':0}


def observation(containers):
    candidates=sum(c['resource_role']=='candidate' for c in containers)
    return {'at':'2026-09-06T00:00:00+00:00','containers':deepcopy(containers),
        'candidate_container_count':candidates,'grader_container_count':len(containers)-candidates,
        'owned_memory_bytes':sum(c['used_memory_bytes'] for c in containers),
        'host_available_bytes':8*r.GIB,'docker_remaining_estimate_bytes':14*r.GIB}


def test_normal_single_official_job_has_two_physical_containers(tmp_path):
    value=observation([container('grader_controller'),container('grader_target',identifier='b')])
    monitor=r.ResourceMonitor(tmp_path,[],sample=lambda _:value)
    sample=monitor.tick()
    assert sample['grader_container_count']==2 and monitor.policy['grader_cap']==2
    assert not (tmp_path/'TRIP.json').exists()
    assert len(monitor.observations())==1


@pytest.mark.parametrize('roles',[['grader_controller'],['grader_target'],['candidate']])
def test_normal_transient_grader_and_candidate_profiles(tmp_path,roles):
    value=observation([container(role) for role in roles])
    monitor=r.ResourceMonitor(tmp_path,[],sample=lambda _:value)
    monitor.tick()
    assert not (tmp_path/'TRIP.json').exists()


def test_two_grading_jobs_rejected_even_when_only_two_physical_containers(tmp_path):
    value=observation([container('grader_controller','one'),container('grader_target','two',identifier='b')])
    monitor=r.ResourceMonitor(tmp_path,[],sample=lambda _:value)
    monitor.tick()
    assert json.loads((tmp_path/'TRIP.json').read_text())['reason']=='multiple_grading_owners'


def test_four_physical_graders_exceed_two_container_cap(tmp_path):
    value=observation([container(role,owner,identifier=ident) for role,owner,ident in [
        ('grader_controller','one','a'),('grader_target','one','b'),
        ('grader_controller','two','c'),('grader_target','two','d')]])
    monitor=r.ResourceMonitor(tmp_path,[],sample=lambda _:value)
    monitor.tick()
    assert json.loads((tmp_path/'TRIP.json').read_text())['reason']=='grader_container_cap_exceeded'


@pytest.mark.parametrize('mutation',['candidate_memory','controller_cpu','swap','unknown','overlap','duplicate'])
def test_wrong_resource_role_or_overlap_is_rejected(tmp_path,mutation):
    cs=[container('grader_controller'),container('grader_target',identifier='b')]
    if mutation=='candidate_memory':
        cs=[container('candidate')];cs[0]['memory_limit_bytes']=768*1024**2;cs[0]['memory_swap_limit_bytes']=768*1024**2
    elif mutation=='controller_cpu':cs[0]['nano_cpus']=2000000000
    elif mutation=='swap':cs[0]['memory_swap_limit_bytes']=r.GIB
    elif mutation=='overlap':cs=[container('candidate','candidate-owner'),container('grader_controller')]
    elif mutation=='duplicate':cs=[container('grader_target'),container('grader_target',identifier='b')]
    value=observation(cs)
    if mutation=='unknown':value['containers'][0]['resource_role']='unknown'
    monitor=r.ResourceMonitor(tmp_path,[],sample=lambda _:value)
    monitor.tick()
    assert (tmp_path/'TRIP.json').exists() and (tmp_path/'CANCEL').exists()
    assert monitor.observations()==[]


def test_sampler_uses_same_inspect_owner_and_recorded_roles_without_extra_queries(tmp_path):
    job=tmp_path/'grader';job.mkdir()
    owner='same-grade-owner';a,b='a'*64,'b'*64
    (job/'request.json').write_text(json.dumps({'owner':owner,'grader_contract':{'frozen':True}}))
    (job/'controller.json').write_text(json.dumps([a]))
    (job/'containers.json').write_text(json.dumps([b]))
    rows=[]
    for identity,name,role in [(a,'controller','grader_controller'),(b,'target','grader_target')]:
        memory,cpus=r.ROLE_LIMITS[role]
        rows.append({'Id':identity,'Name':'/'+name,'Config':{'Labels':{'dpswarm.swe.owner':owner}},
            'HostConfig':{'Memory':memory,'MemorySwap':memory,'NanoCpus':cpus}})
    calls=[]
    def docker(args):
        calls.append(args[0])
        if args[0]=='info':return json.dumps({'MemTotal':16*r.GIB,'NCPU':24})
        if args[0]=='ps':return a+'\n'+b+'\n'
        if args[0]=='inspect':return json.dumps(rows)
        if args[0]=='stats':return '\n'.join(json.dumps({'ID':identity,'MemUsage':'30MiB / 1GiB','CPUPerc':'0%'}) for identity in [a,b])
        raise AssertionError(args)
    value=r.sample_resources([tmp_path],docker=docker,
        virtual_memory=lambda:SimpleNamespace(available=8*r.GIB,total=32*r.GIB),cpu_percent=lambda:0)
    assert calls.count('inspect')==1
    assert {c['resource_role'] for c in value['containers']}=={'grader_controller','grader_target'}
    assert {c['owner'] for c in value['containers']}=={owner}
    assert r.topology_error(value) is None


def test_recorded_id_with_wrong_actual_owner_is_not_treated_as_unrelated(tmp_path):
    identity='a'*64
    (tmp_path/'container.json').write_text(json.dumps({'owner':'expected','id':identity}))
    value=observation([container('candidate')]);value['containers'][0]['owned_by_group']=False
    inspected={'a'*12:{'Id':identity,'Name':'/candidate','Config':{'Labels':{'dpswarm.swe.owner':'wrong'}}}}
    r._attach_roles(value,[tmp_path],inspected)
    assert value['containers'][0]['resource_role']=='unknown'
    assert r.topology_error(value)=='container_role_or_owner_unknown'
