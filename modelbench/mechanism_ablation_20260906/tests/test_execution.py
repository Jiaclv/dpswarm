"""Offline execution safety tests. No provider, Docker or real subprocess calls."""
from copy import deepcopy
import json
from pathlib import Path
import time
import pytest
from modelbench.mechanism_ablation_20260906 import execution as e, resources as r, provider


def healthy():
    return {'status':'budget_exhausted', 'outcome':{'status':'budget_exhausted'},
        'budget':{'frozen':True,'pending_call_count':0,'unknown_call_count':0},
        'accounting':{'usage_unknown_calls':0,'protocol_issues':[], 'call_count':1,
                      'total_tokens_known_subtotal':1,'api_equivalent_known_subtotal_usd':0},
        'cleanup_confirmed':True,'quiesced':True,'artifact_execution_integrity':True,
        'grading_pending':True,'official_resolved':None}


def sample(at=100, candidates=12, owned=1*r.GIB, host=24*r.GIB):
    return {'sampled_monotonic':at,'candidate_container_count':candidates,
        'grader_container_count':0,'owned_memory_bytes':owned,
        'host_available_bytes':host,'docker_remaining_estimate_bytes':24*r.GIB}


def test_operational_health_does_not_gate_failure_empty_or_budget():
    value=healthy();value.update(patch_bytes=0, official_resolved=False, protocol_errors=4)
    assert e.generation_health(value)==[]


@pytest.mark.parametrize('kind',['unknown','pending','not_frozen','cleanup','transport','ledger'])
def test_operational_errors_fail_health(kind):
    value=healthy()
    if kind=='unknown':value['budget']['unknown_call_count']=1
    elif kind=='pending':value['budget']['pending_call_count']=1
    elif kind=='not_frozen':value['budget']['frozen']=False
    elif kind=='cleanup':value['cleanup_confirmed']=False
    elif kind=='transport':value['outcome']['status']='transport_error'
    else:value['accounting']['protocol_issues']=['model_mismatch']
    assert e.generation_health(value)


def test_ramp_needs_three_spaced_fresh_post_admission_samples():
    observations=[sample(t) for t in (100,110,120)]
    assert r.ramp_ready(observations,12,after=99,now=121)
    assert not r.ramp_ready(observations[:2],12,after=99,now=121)
    assert not r.ramp_ready(observations,12,after=101,now=121)
    assert not r.ramp_ready(observations,12,after=99,now=156)
    assert not r.ramp_ready([sample(t) for t in (100,101,102)],12,after=99,now=103)


def test_admission_reserves_unmaterialized_and_new_container_caps():
    assert r.admission_headroom(sample(),12,6)
    assert not r.admission_headroom(sample(candidates=0),12,6)
    assert not r.admission_headroom(sample(host=7*r.GIB),12,6)
    assert not r.admission_headroom(sample(owned=6*r.GIB),12,6)
    assert not r.admission_headroom(sample(),18,6)
    value=sample();value['grader_container_count']=1
    assert not r.admission_headroom(value,0,6)


def test_provider_policy_preserves_shared_caps_and_exact_global12():
    assert provider.validate_policy(provider.policy())=={'codex_account':4,'glm_coding_plan':4,'deepseek':4}
    wrong=provider.policy();wrong['global_model_slots']=16
    with pytest.raises(Exception):provider.validate_policy(wrong)
    wrong=provider.policy();wrong['provider_limits']['glm_coding_plan']=12
    with pytest.raises(Exception):provider.validate_policy(wrong)


def test_grade_is_exactly_one_attempt_on_failure_and_reentry_forbidden(tmp_path):
    patch=tmp_path/'model.patch';patch.write_text('unchanged frozen artifact')
    value=healthy();value['artifact']={'status':'present','path':str(patch),'sha256':e.sha(patch)}
    entry={'run_id':'one','instance':{},'grader_contract':{},'image':'frozen','arm':'T'}
    calls=[]
    def grader(*args,**kwargs):
        calls.append(kwargs)
        return {'completed':False,'resolved':None,'failure_kind':'local_failure'}
    score,attempts=e.grade_once(value,entry,tmp_path,grader=grader,cleanup=lambda p:{'confirmed':True})
    assert len(calls)==len(attempts)==1
    assert calls[0]['memory']=='1g' and calls[0]['cpus']==2
    with pytest.raises(FileExistsError):
        e.grade_once(value,entry,tmp_path,grader=grader,cleanup=lambda p:{'confirmed':True})
    assert len(calls)==1


def test_grade_checks_patch_before_invocation(tmp_path):
    patch=tmp_path/'model.patch';patch.write_text('artifact')
    value=healthy();value['artifact']={'status':'present','path':str(patch),'sha256':'wrong'}
    with pytest.raises(ValueError):
        e.grade_once(value,{},tmp_path,grader=lambda *a,**k:pytest.fail('must not grade'))


def setup_simulation(tmp_path, monkeypatch, *, grade_error=False, generation_error=False):
    batch=tmp_path/'batch';batch.mkdir();(batch/'group').mkdir()
    entries=[]
    for i in range(18):
        entries.append({'run_id':'c1-'+str(i),'condition_id':'FCM_ON' if i%2==0 else 'FCM_OFF',
            'instance':{'instance_id':'task-'+str(i//2%3)},'rep':i//6+1,
            'limits_override':{'token_limit':600000},'arm':'T','block_id':'pair-'+str(i//2)})
    manifest={**e.FIXED,'schedule':entries,'resource_lock_path':str(tmp_path/'owner.lock')}
    e.atomic_json(batch/'manifest.json',manifest)
    monkeypatch.setattr(e,'verify',lambda p:(manifest,tmp_path/'snapshot'))
    monkeypatch.setattr(e.freeze,'assert_import_origins',lambda p:{})
    monkeypatch.setattr(e,'cleanup_owned_episode',lambda p:{'confirmed':True})
    monkeypatch.setattr(e.time,'sleep',lambda s:None)
    events=[]
    monkeypatch.setattr(e,'_quota_pair',lambda *a:True)
    class Monitor:
        def __init__(self,*a,**k):pass
        def start(self):return self
        def stop(self):return {'stopped':True}
        def observations(self):return [sample(time.monotonic(),candidates=0,owned=0)]
    monkeypatch.setattr(e.resources,'ResourceMonitor',Monitor)
    class Process:
        pid=12345
        returncode=0
        def poll(self):return 0
    def start(batch,snapshot,command,rid):
        events.append((command,rid))
        directory=batch/'results'/rid;directory.mkdir(parents=True,exist_ok=True)
        entry=next(x for x in entries if x['run_id']==rid)
        if command=='episode':
            value=healthy()
            if generation_error and rid=='c1-0':value['budget']['unknown_call_count']=1
            value.update(run_id=rid,condition_id=entry['condition_id'],instance_id=entry['instance']['instance_id'])
            e.atomic_json(directory/'generation_result.json',value)
        else:
            value=e.read(directory/'generation_result.json');value.update(grading_pending=False,official_resolved=False)
            if grade_error and rid=='c1-0':value['grading_error']={'type':'test_failure'}
        e.atomic_json(directory/'episode_result.json',value)
        return Process()
    monkeypatch.setattr(e,'start_child',start)
    return batch,events


def test_finite_eighteen_and_first_six_health_gate_no_quality_selection(tmp_path,monkeypatch):
    batch,events=setup_simulation(tmp_path,monkeypatch)
    result=e.coordinate(batch)
    assert result['status']=='completed' and result['admitted']==result['scored']==18
    assert result['resolved']==0 and result['pilot_health_gate']=='passed'
    commands=[x[0] for x in events]
    assert commands==['episode']*6+['grade']*6+['episode']*12+['grade']*12
    assert len(list((batch/'admissions').glob('*.json')))==18
    with pytest.raises(ValueError):e.coordinate(batch)


def test_pilot_infrastructure_failure_never_dispatches_remaining_twelve(tmp_path,monkeypatch):
    batch,events=setup_simulation(tmp_path,monkeypatch,grade_error=True)
    result=e.coordinate(batch)
    assert result['status']=='stopped_incomplete' and result['admitted']==6
    assert len([x for x in events if x[0]=='episode'])==6


def test_generation_unknown_stops_before_next_pair(tmp_path,monkeypatch):
    batch,events=setup_simulation(tmp_path,monkeypatch,generation_error=True)
    result=e.coordinate(batch)
    assert result['status']=='stopped_incomplete' and result['admitted']==1
    assert len([x for x in events if x[0]=='episode'])==1
    assert (batch/'group/CANCEL').exists()
    assert e.read(batch/'group/controller-stop.json')['reason']=='generation_health_failed'


def test_launch_existing_intent_cannot_duplicate_process(tmp_path,monkeypatch):
    (tmp_path/'launch-intent.json').write_text('{}')
    monkeypatch.setattr(e,'verify',lambda p:({'resource_lock_path':str(tmp_path/'owner.lock')},tmp_path/'snapshot'))
    monkeypatch.setattr(e.freeze,'credential_environment',lambda p:{})
    monkeypatch.setattr(e.subprocess,'Popen',lambda *a,**k:pytest.fail('must not launch'))
    with pytest.raises(FileExistsError):e.launch(tmp_path)


def test_low_headroom_allows_single_episode_without_weakening_ramp():
    value=sample(candidates=0,owned=0,host=int(7.4*r.GIB))
    assert r.admission_headroom(value,0,3)
    assert not r.admission_headroom(value,0,6)
    assert not r.admission_headroom(value,3,3)
    value['candidate_container_count']=3;value['owned_memory_bytes']=r.GIB//4
    assert r.admission_headroom(value,3,3)


def test_single_episode_admissions_preserve_pair_quota_order(tmp_path,monkeypatch):
    batch,events=setup_simulation(tmp_path,monkeypatch)
    quota_pairs=[]
    def quota(batch,state,pair,active):
        quota_pairs.append([entry['run_id'] for entry in pair]);return True
    monkeypatch.setattr(e,'_quota_pair',quota)
    assert e.coordinate(batch)['status']=='completed'
    assert quota_pairs==[['c1-'+str(i),'c1-'+str(i+1)] for i in range(0,18,2)]
    assert [rid for command,rid in events if command=='episode']==['c1-'+str(i) for i in range(18)]
