from copy import deepcopy
import json
from pathlib import Path
import pytest
from modelbench.big_budget_team_20260906 import execution as e
from modelbench.big_budget_team_20260906.contracts import load_cells,build_entry


def make_entries():
    old=e.REPO/'modelbench/minimal_value_20260905'
    m=e.read(old/'batches/a2-v1/manifest.json')
    refs={x['instance']['instance_id']:x for x in m['schedule']}
    return [build_entry(c,refs[c['instance_id']]['instance'],refs[c['instance_id']]['public_checks'],
                        refs[c['instance_id']]['image'],refs[c['instance_id']]['grader_contract']) for c in load_cells()]


def test_all_frozen_cells_partition_into_18_complete_waves():
    entries=make_entries();i=0;waves=[]
    while i<len(entries):
        wave=e.next_wave(entries,i);waves.append(wave);i+=len(wave)
    assert len(waves)==18
    assert all(len(w)==10 and len({x['condition_id'] for x in w})==10 for w in waves)
    assert [x['run_id'] for w in waves for x in w]==[x['run_id'] for x in entries]


@pytest.mark.parametrize('excluded_count', [0, 7, 10])
def test_controller_runs_dispatch_subset_with_atomic_team_capacity_and_grading_barrier(tmp_path,monkeypatch,excluded_count):
    entries=make_entries();batch=tmp_path/'batch';batch.mkdir()
    (batch/'group').mkdir();(batch/'manifest.json').write_text('{}')
    manifest={'schedule':entries,'dispatch_seconds':7*86400,'resource_lock_path':str(tmp_path/'execution.lock'),
        'token_admission_sum_limit':334800000,'generation_watchdog_seconds':7500,'grading_watchdog_seconds':2100}
    dispatch=entries[excluded_count:]
    manifest['generation_attempt_limit']=len(dispatch)
    manifest['token_admission_sum_limit']=sum(x['limits_override']['token_limit'] for x in dispatch)
    if excluded_count:
        manifest['dispatch_run_ids']=[x['run_id'] for x in dispatch]
        manifest['continuation']={'source_batch':'prior-batch','source_manifest_sha256':'fixture',
            'source_transport_policy':None,'prior_totals':{'admitted':excluded_count,'scored':excluded_count,'unknown_calls':5}}
        manifest['result_comparison_policy']='separate transport strata'
    monkeypatch.setattr(e,'verify',lambda b:(manifest,tmp_path))
    monkeypatch.setattr(e.freeze,'assert_import_origins',lambda s:True)
    monkeypatch.setattr(e.time,'sleep',lambda s:None)
    monkeypatch.setattr(e,'cleanup_owned_episode',lambda p:{'confirmed':True})
    monkeypatch.setattr(e.resources,'ResourceMonitor',lambda *a,**kw:type('Monitor',(),{'start':lambda s:None,'stop':lambda s:None})())
    catalog={x['run_id']:x for x in entries};active={};peak=0;started=[];graded=[]
    class Process:
        pid=101
        returncode=0
        def __init__(self,rid,command):self.rid,self.command=rid,command
        def poll(self):
            if self.command=='generate':active.pop(self.rid,None)
            return 0
        def wait(self,timeout=None):return 0
    def child(batch,snapshot,command,rid):
        nonlocal peak
        entry=catalog[rid];directory=batch/'results'/rid;directory.mkdir(parents=True,exist_ok=True)
        if command=='generate':
            assert rid not in started
            active[rid]=1+entry['expected_workers'];peak=max(peak,sum(active.values()))
            assert peak<=12
            started.append(rid)
            result={'run_id':rid,'condition_id':entry['condition_id'],'instance_id':entry['instance']['instance_id'],
                'grading_pending':True,'cleanup_confirmed':True,'official_resolved':None,'accounting':{}}
            e.atomic_json(directory/'generation_result.json',result)
        else:
            assert active=={},'Scoring overlapped generation'
            assert rid not in graded
            graded.append(rid);result=e.read(directory/'generation_result.json')
            result.update(official_resolved=True,grading_pending=False)
        e.atomic_json(directory/'episode_result.json',result)
        return Process(rid,command)
    monkeypatch.setattr(e,'start_child',child)
    result=e.run(batch);state=e.read(batch/'state.json')
    assert result['status']=='completed'
    assert state['admitted']==state['generated']==state['scored']==len(dispatch)
    assert state['token_admission']==manifest['token_admission_sum_limit']
    assert state['total_planned']==len(dispatch) and state['full_plan_total']==180
    if excluded_count:
        assert state['prior_batch_stratum']['unknown_calls']==5
        assert state['prior_batch_stratum']['scored']==excluded_count
        assert len(dispatch)==180-excluded_count
        assert state['token_admission']==(321600000 if excluded_count==7 else 316200000)
    assert state['waves_completed']==(17 if excluded_count==10 else 18)
    assert started==graded==[x['run_id'] for x in dispatch]
    assert peak==12
    assert not (tmp_path/'execution.lock').exists()


def test_controller_refuses_implicit_resume(tmp_path,monkeypatch):
    e.atomic_json(tmp_path/'state.json',{'status':'interrupted'})
    monkeypatch.setattr(e,'verify',lambda b:({},tmp_path))
    monkeypatch.setattr(e.freeze,'assert_import_origins',lambda s:True)
    with pytest.raises(ValueError,match='reconcile'):e.run(tmp_path)


def test_verify_rejects_manifest_byte_drift_before_execution(tmp_path):
    (tmp_path/'manifest.json').write_text('{"protocol":"bad"}')
    (tmp_path/'manifest.sha256').write_text('0'*64)
    with pytest.raises(ValueError,match='manifest changed'):e.verify(tmp_path)


def test_grade_explicitly_uses_one_gib_and_reuses_frozen_result(tmp_path,monkeypatch):
    from modelbench.minimal_value_20260905 import environment
    entry=make_entries()[0];rid=entry['run_id'];directory=tmp_path/'results'/rid
    e.atomic_json(directory/'generation_result.json',{'cleanup_confirmed':True})
    monkeypatch.setattr(e,'verify',lambda b:({'schedule':[entry]},tmp_path))
    monkeypatch.setattr(e.freeze,'assert_import_origins',lambda s:True)
    seen=[]
    def terminal(*args,**kwargs):seen.append(kwargs);return {'completed':True,'resolved':True}
    monkeypatch.setattr(environment,'rootgrade_terminal',terminal)
    def frozen(result,entry,directory,grader):return grader('frozen-patch'),[{'completed':True}]
    monkeypatch.setattr(e,'grade_frozen',frozen)
    monkeypatch.setattr(e,'save_result',lambda b,en,res:{**res,'official_resolved':res['score']['resolved']})
    assert e.grade(tmp_path,rid)['official_resolved'] is True
    assert seen==[{'memory':'1g','cpus':2}]


def test_qualified_image_identity_is_digest_and_descriptor_hash_is_bound():
    qualifications=e.read(e.HERE/'preflight/source_qualification_review.json')
    proofs={q['instance_id']:q for q in qualifications['tasks']}
    for entry in make_entries():
        q=proofs[entry['instance']['instance_id']]
        assert entry['image']==q['image_id']
        assert e.sha(q['image']['path'])==q['image']['sha256']
        assert entry['instance']==q['public_instance']
        assert entry['public_checks']==q['public_checks']
        assert entry['grader_contract']==qualifications['shared_grader_contract']


def test_transport_policy_is_in_probe_source_binding():
    sources=e.transport_sources()
    key='modelbench/big_budget_team_20260906/TRANSPORT_POLICY.json'
    assert sources[key]==e.sha(e.REPO/key)


def test_verify_binds_new_transport_policy_and_rejects_omission_or_drift(tmp_path):
    batch=tmp_path/'batch'
    relative='modelbench/big_budget_team_20260906/TRANSPORT_POLICY.json'
    policy={'glm_read_timeout_seconds':900,'glm_total_timeout_seconds':960}
    e.atomic_json(batch/'runtime_snapshot'/relative,policy)
    provider_policy=batch/'group'/'policy.json'
    e.atomic_json(provider_policy,{'fixture':True})
    manifest={'protocol':e.PROTOCOL,'runtime_sources':{relative:e.sha(batch/'runtime_snapshot'/relative)},
        'input_artifacts':{},'evidence_files':{},'schedule':make_entries(),
        'transport_policy':policy,'transport_policy_sha256':e.sha(batch/'runtime_snapshot'/relative)}
    def freeze_manifest(value):
        e.atomic_json(batch/'manifest.json',value)
        digest=e.sha(batch/'manifest.json')
        (batch/'manifest.sha256').write_text(digest,encoding='ascii')
        e.atomic_json(batch/'group'/'group.json',{'policy_path':str(provider_policy),
            'policy_sha256':e.sha(provider_policy),'manifest_sha256':digest})
    freeze_manifest(manifest)
    assert e.verify(batch)[0]['transport_policy']==policy
    missing=deepcopy(manifest);missing.pop('transport_policy')
    freeze_manifest(missing)
    with pytest.raises(ValueError,match='transport policy binding'):
        e.verify(batch)
    drift=deepcopy(manifest);drift['transport_policy']['glm_read_timeout_seconds']=300
    freeze_manifest(drift)
    with pytest.raises(ValueError,match='transport policy binding'):
        e.verify(batch)


def test_glm_stream_worker_is_in_probe_source_binding():
    sources=e.transport_sources()
    key='modelbench/big_budget_team_20260906/glm_stream_http_worker.py'
    assert sources[key]==e.sha(e.REPO/key)
