"""Offline lineage, exact dispatch and explicit qualification tests."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
from modelbench.big_budget_team_20260906 import execution as e
from modelbench.big_budget_team_20260906.tests.test_execution import make_entries
from modelbench.minimal_value_20260905.budget import EpisodeBudget


def seal(batch, manifest):
    e.atomic_json(batch/'manifest.json', manifest)
    (batch/'manifest.sha256').write_text(e.sha(batch/'manifest.json'), encoding='ascii')
    policy=batch/'group/policy.json'
    e.atomic_json(policy, {'fixture': True})
    e.atomic_json(batch/'group/group.json', {'policy_path': str(policy), 'policy_sha256': e.sha(policy),
        'manifest_sha256': e.sha(batch/'manifest.json')})


@pytest.fixture
def source(tmp_path):
    entries=make_entries();batch=tmp_path/'original'
    manifest={'protocol':e.PROTOCOL,'schedule':entries,'runtime_sources':{},'input_artifacts':{},'evidence_files':{}}
    seal(batch,manifest)
    e.atomic_json(batch/'state.json',{'status':'stopped_incomplete','active':[],
        'admitted':7,'generated':7,'scored':7})
    for index,entry in enumerate(entries[:7]):
        rid=entry['run_id'];directory=batch/'results'/rid
        e.atomic_json(batch/'admissions'/(rid+'.json'),{'run_id':rid,'status':'admitted',
            'manifest_sha256':e.sha(batch/'manifest.json'),'token_limit':entry['limits_override']['token_limit']})
        limits=entry['limits_override'];q={k:limits[k] for k in ('max_calls','token_limit','cm_call_allowance')}
        budget=EpisodeBudget(**q,scopes={'solver':q})
        budget.scope('solver').reserve('call','lead',100)
        budget.scope('solver').complete('call',{'call_id':'call','input_tokens':None if index<5 else 100,'output_tokens':None if index<5 else 100})
        budget.freeze()
        result={'run_id':rid,'grading_pending':False,'official_resolved':index<2,'cleanup_confirmed':True,
            'budget':budget.summary(),'api_equivalent_usd':None if index<5 else 1.0}
        for name in ('generation_result.json','episode_result.json'):
            e.atomic_json(directory/name,result)
        e.atomic_json(directory/'episode-budget.json',budget.snapshot())
        e.atomic_json(directory/'calls/call/metadata.json',{'total_tokens':None if index<5 else 200})
    return batch,entries


def continuation_manifest(source):
    batch,entries=source
    proof=e.continuation_details(batch,entries)
    remaining=entries[7:]
    return {'protocol':e.PROTOCOL,'schedule':entries,'runtime_sources':{},'input_artifacts':{},'evidence_files':{},
        'continuation':proof,'dispatch_run_ids':[x['run_id'] for x in remaining],
        'generation_attempt_limit':len(remaining),
        'token_admission_sum_limit':sum(x['limits_override']['token_limit'] for x in remaining),
        'ordinary_ticket_limit':sum(x['limits_override']['max_calls'] for x in remaining),
        'cm_ticket_limit':sum(x['limits_override']['cm_call_allowance'] for x in remaining)}


def test_sealed_source_excludes_all_admissions_and_preserves_unknowns(source):
    batch,entries=source
    before={p.relative_to(batch).as_posix():e.sha(p) for p in batch.rglob('*') if p.is_file()}
    proof=e.continuation_details(batch,entries)
    assert proof['excluded_run_ids']==[x['run_id'] for x in entries[:7]]
    assert proof['prior_totals']['token_admission']==13200000
    assert proof['prior_totals']['unknown_calls']==5
    assert proof['prior_totals']['reserved_tokens']==500
    assert all(x['total_tokens'] is None and x['api_equivalent_usd'] is None for x in proof['prior_results'][:5])
    assert before=={p.relative_to(batch).as_posix():e.sha(p) for p in batch.rglob('*') if p.is_file()}


def test_continuation_keeps_original_schedule_and_exact_173_limits(source,tmp_path):
    manifest=continuation_manifest(source);batch=tmp_path/'new';seal(batch,manifest)
    validated,_=e.verify(batch)
    assert len(validated['schedule'])==180
    assert len(e.dispatch_schedule(validated))==173
    assert validated['token_admission_sum_limit']==321600000
    assert validated['ordinary_ticket_limit']==20948 and validated['cm_ticket_limit']==8420
    assert [x['condition_id'] for x in e.dispatch_schedule(validated)[:3]]==['F-T','T01','T11']


@pytest.mark.parametrize('change', ['receipt','result','metadata','state'])
def test_verified_lineage_detects_changed_old_evidence(source,tmp_path,change):
    old,entries=source;batch=tmp_path/'new';seal(batch,continuation_manifest(source))
    rid=entries[0]['run_id']
    paths={'receipt':old/'admissions'/(rid+'.json'),'result':old/'results'/rid/'episode_result.json',
           'metadata':old/'results'/rid/'calls/call/metadata.json','state':old/'state.json'}
    value=e.read(paths[change]);value['extra']='mutation';e.atomic_json(paths[change],value)
    with pytest.raises(ValueError,match='source evidence changed'):e.verify(batch)


@pytest.mark.parametrize('bad', ['reinclude','reorder','budget','unbound_subset'])
def test_manifest_cannot_expand_or_reorder_dispatch_or_budget(source,tmp_path,bad):
    manifest=continuation_manifest(source);batch=tmp_path/'new'
    if bad=='reinclude':manifest['dispatch_run_ids'].insert(0,source[1][0]['run_id'])
    elif bad=='reorder':manifest['dispatch_run_ids'][:2]=reversed(manifest['dispatch_run_ids'][:2])
    elif bad=='budget':manifest['generation_attempt_limit']=180
    else:manifest.pop('continuation')
    seal(batch,manifest)
    with pytest.raises(ValueError):e.verify(batch)


@pytest.mark.parametrize('bad', ['active','orphan','pending','schedule'])
def test_source_must_be_stopped_complete_and_same_contract(source,bad):
    batch,entries=source
    if bad=='active':
        state=e.read(batch/'state.json');state['active']=[{'run_id':'still-running'}];e.atomic_json(batch/'state.json',state)
    elif bad=='orphan':(batch/'results'/'unadmitted').mkdir()
    elif bad=='pending':
        path=batch/'results'/entries[0]['run_id']/'episode_result.json';result=e.read(path)
        result['grading_pending']=False
        snapshot=path.parent/'episode-budget.json';budget=EpisodeBudget.from_snapshot(e.read(snapshot))
        budget.root.frozen=False;budget.frozen_scopes.clear();budget.scope('solver').reserve('still-pending','lead',100)
        e.atomic_json(snapshot,budget.snapshot());e.atomic_json(path,result)
    else:
        entries=deepcopy(entries);entries[0]['notes']='contract changed'
    with pytest.raises(ValueError):e.continuation_details(batch,entries)


@pytest.mark.parametrize('command', ['generate','grade'])
def test_generation_and_grading_reject_every_excluded_run_before_resources(source,tmp_path,monkeypatch,command):
    manifest=continuation_manifest(source)
    monkeypatch.setattr(e,'verify',lambda batch:(manifest,tmp_path))
    monkeypatch.setattr(e.freeze,'assert_import_origins',lambda snapshot:True)
    with pytest.raises(ValueError,match='outside this batch dispatch subset'):
        getattr(e,command)(tmp_path/'new',source[1][0]['run_id'])
    assert not (tmp_path/'new/results').exists()


def test_prepare_freezes_explicit_qualification_evidence_and_original_lineage(source,tmp_path,monkeypatch):
    from modelbench.minimal_value_20260905 import environment
    old,entries=source
    repo=tmp_path/'repo';repo.mkdir()
    e.atomic_json(repo/'modelbench/minimal_value_20260905/batches/a2-v1/manifest.json',{'schedule':entries})
    qualification=e.read(e.HERE/'preflight/source_qualification_review.json')
    proofs=tmp_path/'current-proof';proofs.mkdir()
    xml=proofs/'tests.xml';xml.write_text('<testsuites/>',encoding='utf-8')
    gate=proofs/'validation.json';e.atomic_json(gate,{'passed':True,'runtime_sources':{},'junit_sha256':e.sha(xml)})
    probes=proofs/'probes.json';e.atomic_json(probes,{'passed':True,'calls_issued':5,'transport_sources':{}})
    qualification_path=proofs/'qualification.json';e.atomic_json(qualification_path,qualification)
    monkeypatch.setattr(e,'REPO',repo)
    monkeypatch.setattr(e,'runtime_sources',lambda:{})
    monkeypatch.setattr(e,'transport_sources',lambda:{})
    monkeypatch.setattr(e.freeze,'input_files',lambda official:{})
    monkeypatch.setattr(environment,'capture_grader_contract',lambda:qualification['shared_grader_contract'])
    target=tmp_path/'new'
    result=e.prepare(target,continue_from=old,offline_validation=gate,offline_tests=xml,
        transport_probes=probes,source_qualification=qualification_path)
    assert result['episodes']==173 and result['full_plan_episodes']==180 and result['prior_admitted_excluded']==7
    manifest,_=e.verify(target)
    assert manifest['schedule']==entries
    assert manifest['evidence_origins']['offline-validation.json']=={'path':str(gate),'sha256':e.sha(gate)}
    assert e.sha(target/'preflight/offline-validation.json')==e.sha(gate)
    assert len(manifest['dispatch_run_ids'])==173
    assert manifest['transport_policy']['stream'] is True
    assert not (target/'admissions').exists() and not (target/'results').exists()


@pytest.fixture
def two_generations(source,tmp_path):
    old,entries=source
    middle=tmp_path/'stream-generation'
    manifest=continuation_manifest(source)
    manifest['continuation']=e._legacy_continuation_details(old,entries)
    seal(middle,manifest)
    e.atomic_json(middle/'state.json',{'status':'stopped_incomplete','active':[],
        'admitted':3,'generated':1,'scored':1,'stop_reason':'generation_process_failed'})
    for index,entry in enumerate(entries[7:10]):
        rid=entry['run_id'];directory=middle/'results'/rid
        e.atomic_json(middle/'admissions'/(rid+'.json'),{'run_id':rid,'status':'admitted',
            'manifest_sha256':e.sha(middle/'manifest.json'),'token_limit':entry['limits_override']['token_limit']})
        limits=entry['limits_override'];q={k:limits[k] for k in ('max_calls','token_limit','cm_call_allowance')}
        budget=EpisodeBudget(**q,scopes={'solver':q})
        scope=budget.scope('solver');scope.reserve('first','lead',100)
        if index==1:
            scope.complete('first',{'call_id':'first','input_tokens':100,'output_tokens':100});budget.freeze()
            result={'run_id':rid,'grading_pending':False,'official_resolved':False,'cleanup_confirmed':True,
                    'budget':budget.summary(),'api_equivalent_usd':1.0}
            for name in ('generation_result.json','episode_result.json'):e.atomic_json(directory/name,result)
        elif index==0:scope.reserve('second','worker',100)
        e.atomic_json(directory/'episode-budget.json',budget.snapshot())
        e.atomic_json(middle/'logs'/(rid+'.cleanup.json'),{'confirmed':True})
        e.atomic_json(middle/'logs'/(rid+'.failure.json'),{'returncode':15,'result_present':False})
    return old,middle,entries


def test_legacy_v1_and_two_generation_lineage_preserve_interrupted_attempts(two_generations,tmp_path):
    old,middle,entries=two_generations
    before={str(p):e.sha(p) for base in (old,middle) for p in base.rglob('*') if p.is_file()}
    assert e.verify(middle)[0]['continuation']['protocol']==e.LEGACY_CONTINUATION_PROTOCOL
    proof=e.continuation_details(middle,entries)
    assert proof['excluded_run_ids']==[entry['run_id'] for entry in entries[:10]]
    assert proof['prior_totals']['admitted']==10
    assert proof['prior_totals']['scored']==proof['prior_totals']['generated']==8
    assert proof['prior_totals']['interrupted']==2
    assert proof['prior_totals']['unknown_calls']==5 and proof['prior_totals']['pending_calls']==3
    assert proof['prior_totals']['token_admission']==18600000
    interrupted=[row for row in proof['prior_results'] if row['attempt_status']=='interrupted']
    assert len(interrupted)==2 and all(row['official_resolved'] is None and row['total_tokens'] is None for row in interrupted)
    assert [node['batch'] for node in proof['lineage']]==[str(old),str(middle)]
    manifest=continuation_manifest((old,entries));remaining=entries[10:]
    manifest.update(continuation=proof,dispatch_run_ids=[entry['run_id'] for entry in remaining],
        generation_attempt_limit=170,token_admission_sum_limit=316200000,
        ordinary_ticket_limit=20468,cm_ticket_limit=8228)
    latest=tmp_path/'coding-generation';seal(latest,manifest)
    assert len(e.dispatch_schedule(e.verify(latest)[0]))==170
    assert before=={str(p):e.sha(p) for base in (old,middle) for p in base.rglob('*') if p.is_file()}


def test_grandparent_evidence_mutation_is_detected_through_legacy_generation(two_generations,tmp_path):
    old,middle,entries=two_generations
    proof=e.continuation_details(middle,entries)
    path=old/'results'/entries[0]['run_id']/'calls/call/metadata.json'
    e.atomic_json(path,{'total_tokens':999})
    with pytest.raises(ValueError,match='source evidence changed'):
        e.continuation_details(middle,entries)


def test_source_with_unconfirmed_interrupted_cleanup_cannot_continue(two_generations):
    old,middle,entries=two_generations
    rid=entries[7]['run_id'];e.atomic_json(middle/'logs'/(rid+'.cleanup.json'),{'confirmed':False})
    with pytest.raises(ValueError,match='no confirmed'):
        e.continuation_details(middle,entries)
