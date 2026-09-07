from copy import deepcopy
import pytest
from modelbench.big_budget_team_20260906 import execution as e


@pytest.fixture
def scenario(tmp_path):
    source = tmp_path/'source'; batch = tmp_path/'mvp'
    entries = e.read(e.HERE/'batches/b1-coding-continuation-v1/manifest.json')['schedule']
    e.atomic_json(source/'manifest.json', {'schedule':entries})
    (source/'manifest.sha256').write_text(e.sha(source/'manifest.json'),encoding='ascii')
    ids = [x['run_id'] for x in entries if x['rep']==1 and x['block_id'] in ('b1-r1-q06','b1-r1-q04')
           and x['condition_id'] in ('T00','T11','S-S')]
    current = [x['run_id'] for x in entries if x['block_id']=='b1-r1-q05']
    auth = {'protocol':'big_budget_core_mvp_v1','approved':True,'maximum_new_generation_admissions':6,
        'automatic_generation_retry':False,'predecessor_batch':str(source),'selected_run_ids':ids,
        'predecessor_manifest_sha256':e.sha(source/'manifest.json'),'predecessor_run_ids':current}
    details = e._mvp_details(auth,entries,batch)
    chosen = [x for x in entries if x['run_id'] in ids]
    manifest = {'protocol':e.PROTOCOL,'schedule':entries,'mvp':details,'dispatch_run_ids':ids,
        'runtime_sources':{},'input_artifacts':{},'evidence_files':{},'generation_attempt_limit':6,
        'token_admission_sum_limit':10800000,'ordinary_ticket_limit':696,'cm_ticket_limit':280,
        'automatic_generation_retry':False,'dispatch_seconds':3600,'final_drain_seconds':3600,
        'generation_watchdog_seconds':7500,'grading_watchdog_seconds':2100,
        'resource_lock_path':str(tmp_path/'execution.lock')}
    e.atomic_json(batch/'preflight/mvp-authorization.json',auth)
    manifest['evidence_files']['preflight/mvp-authorization.json']=e.sha(batch/'preflight/mvp-authorization.json')
    e.atomic_json(batch/'group/policy.json',{})
    def freeze():
        e.atomic_json(batch/'manifest.json',manifest)
        (batch/'manifest.sha256').write_text(e.sha(batch/'manifest.json'),encoding='ascii')
        e.atomic_json(batch/'group/group.json',{'policy_path':str(batch/'group/policy.json'),
            'policy_sha256':e.sha(batch/'group/policy.json'),'manifest_sha256':e.sha(batch/'manifest.json')})
    freeze()
    return source,batch,entries,chosen,auth,manifest,freeze


def complete_predecessor(source, auth):
    e.atomic_json(source/'state.json',{'status':'stopped_incomplete','stop_reason':'operator_stop',
        'admitted':10,'generated':10,'scored':10,'active':[]})
    for rid in auth['predecessor_run_ids']:
        e.atomic_json(source/'admissions'/(rid+'.json'),{'run_id':rid})
        e.atomic_json(source/'results'/rid/'episode_result.json',{'run_id':rid,'grading_pending':False,
            'official_resolved':False,'cleanup_confirmed':True})


def test_fixed_six_keep_solver_configuration_and_bind_new_batch_identity(scenario):
    source,batch,entries,chosen,auth,m,freeze=scenario
    verified,_=e.verify(batch)
    assert e.dispatch_schedule(verified)==chosen
    assert len(chosen)==6 and [len(e.next_wave(chosen,i)) for i in (0,3)]==[3,3]
    assert sum(x['limits_override']['token_limit'] for x in chosen)==10800000
    assert sum(x['limits_override']['max_calls'] for x in chosen)==696
    assert sum(x['limits_override']['cm_call_allowance'] for x in chosen)==280
    for entry,attempt in zip(chosen,m['mvp']['fresh_attempts']):
        assert attempt['fresh_attempt_id']==str(batch.resolve())+'::'+entry['run_id']
        assert attempt['source_entry']['configuration_sha256']==entry['configuration_sha256']
    assert len({a['fresh_attempt_id'] for a in m['mvp']['fresh_attempts']})==6


@pytest.mark.parametrize('mutation',['seventh','reverse','unapproved','retry','source_hash'])
def test_authorization_rejects_scope_order_approval_retry_or_source_drift(scenario,mutation):
    source,batch,entries,chosen,auth,m,freeze=scenario
    auth=deepcopy(auth)
    if mutation=='seventh':auth['selected_run_ids'].append(entries[0]['run_id'])
    elif mutation=='reverse':auth['selected_run_ids'].reverse()
    elif mutation=='unapproved':auth['approved']=False
    elif mutation=='retry':auth['automatic_generation_retry']=True
    else:auth['predecessor_manifest_sha256']='bad'
    with pytest.raises(ValueError):e._mvp_details(auth,entries,batch)


@pytest.mark.parametrize('mutation',['attempt_limit','extra_dispatch','fresh_identity','configuration'])
def test_frozen_mvp_verify_rejects_tampering(scenario,mutation):
    source,batch,entries,chosen,auth,m,freeze=scenario
    if mutation=='attempt_limit':m['generation_attempt_limit']=7
    elif mutation=='extra_dispatch':m['dispatch_run_ids']=[x['run_id'] for x in entries if x['run_id'] in set(auth['selected_run_ids']+[entries[0]['run_id']])]
    elif mutation=='fresh_identity':m['mvp']['fresh_attempts'][0]['fresh_attempt_id']='old'
    else:m['schedule'][0]['limits_override']['token_limit']+=1
    freeze()
    with pytest.raises(ValueError):e.verify(batch)


@pytest.mark.parametrize('mutation',['running','unscored','active','provider_stop','bad_result','missing_admission'])
def test_predecessor_gate_rejects_inflight_incomplete_or_abnormal_stop(scenario,mutation):
    source,batch,entries,chosen,auth,m,freeze=scenario
    complete_predecessor(source,auth);state=e.read(source/'state.json')
    if mutation=='running':state['status']='running'
    elif mutation=='unscored':state['scored']=9
    elif mutation=='active':state['active']=[{'run_id':auth['predecessor_run_ids'][0]}]
    elif mutation=='provider_stop':state['stop_reason']='provider_stop:quota'
    elif mutation=='bad_result':e.atomic_json(source/'results'/auth['predecessor_run_ids'][0]/'episode_result.json',{'run_id':'wrong'})
    else:(source/'admissions'/(auth['predecessor_run_ids'][0]+'.json')).unlink()
    e.atomic_json(source/'state.json',state)
    with pytest.raises(ValueError,match='MVP prerequisite'):e._mvp_ready(m)


@pytest.mark.parametrize('command',['launch','run','generate'])
def test_all_generation_entrypoints_block_before_predecessor_grading(scenario,monkeypatch,command):
    source,batch,entries,chosen,auth,m,freeze=scenario
    e.atomic_json(source/'state.json',{'status':'running','admitted':10,'generated':5,'scored':0,'active':[{}]})
    monkeypatch.setattr(e.freeze,'assert_import_origins',lambda p:True)
    monkeypatch.setattr(e.subprocess,'Popen',lambda *a,**k:pytest.fail('Must not launch process before predecessor scoring'))
    with pytest.raises(ValueError,match='MVP prerequisite'):
        getattr(e,command)(batch,*([chosen[0]['run_id']] if command=='generate' else []))
    assert not (batch/'launch.json').exists() and not (batch/'admissions').exists()


def test_six_only_controller_uses_two_grading_barriers_and_finishes_without_retry(scenario,monkeypatch):
    source,batch,entries,chosen,auth,m,freeze=scenario
    complete_predecessor(source,auth);e._mvp_ready(m)
    monkeypatch.setattr(e.freeze,'assert_import_origins',lambda s:True)
    monkeypatch.setattr(e.time,'sleep',lambda s:None)
    monkeypatch.setattr(e,'cleanup_owned_episode',lambda p:{'confirmed':True})
    monkeypatch.setattr(e.resources,'ResourceMonitor',lambda *a,**k:type('M',(),{'start':lambda s:None,'stop':lambda s:None})())
    active={};started=[];graded=[];peaks=[];catalog={x['run_id']:x for x in chosen}
    class Process:
        pid=999;returncode=0
        def __init__(self,rid,command):self.rid,self.command=rid,command
        def poll(self):
            if self.command=='generate':active.pop(self.rid,None)
            return 0
        def wait(self,timeout=None):return 0
    def child(batch,snapshot,command,rid):
        entry=catalog[rid];directory=batch/'results'/rid
        if command=='generate':
            assert rid not in started
            if len(started)>=3:assert len(graded)==3
            started.append(rid);active[rid]=entry['candidate_container_slots'];peaks.append(sum(active.values()))
            result={'run_id':rid,'condition_id':entry['condition_id'],'instance_id':entry['instance']['instance_id'],
                'grading_pending':True,'cleanup_confirmed':True,'official_resolved':None,'accounting':{}}
            e.atomic_json(directory/'generation_result.json',result)
        else:
            assert not active and rid not in graded
            graded.append(rid);result=e.read(directory/'generation_result.json')
            result.update(grading_pending=False,official_resolved=False)
        e.atomic_json(directory/'episode_result.json',result)
        return Process(rid,command)
    monkeypatch.setattr(e,'start_child',child)
    outcome=e.run(batch);state=e.read(batch/'state.json')
    assert outcome['status']=='completed' and state['waves_completed']==2
    assert state['admitted']==state['generated']==state['scored']==6
    assert started==graded==auth['selected_run_ids'] and max(peaks)==7
    assert state['token_admission']==10800000 and not (batch/'STOP').exists()
    assert not (batch.parent/'execution.lock').exists()


def test_saved_result_carries_fresh_attempt_and_source_entry(scenario,monkeypatch):
    source,batch,entries,chosen,auth,m,freeze=scenario
    monkeypatch.setattr(e.reporting,'accounting',lambda *a:{'api_equivalent_usd':0,'api_equivalent_known_subtotal_usd':0})
    monkeypatch.setattr(e.reporting,'result_status',lambda *a:{})
    e.atomic_json(batch/'pricing.json',{})
    result=e.save_result(batch,chosen[0],{})
    assert result['run_id']==chosen[0]['run_id']
    assert result['fresh_attempt_id']==m['mvp']['fresh_attempts'][0]['fresh_attempt_id']
    assert result['source_entry']==m['mvp']['fresh_attempts'][0]['source_entry']
