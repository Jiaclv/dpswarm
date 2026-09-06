"""Prepare the explicitly authorized option-B batch without any model dispatch."""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil
import sys

HERE=Path(__file__).resolve().parent
EXPERIMENT=HERE.parent
AUTHORIZATION=HERE/'A2_RECOVERY_B_AUTHORIZATION_20260905.json'

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def require(condition,message):
    if not condition: raise ValueError(message)

def write_new(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')

def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

def check_contract(manifest,state,carry,auth):
    require(manifest['stage']=='a2' and manifest['scheduled_episodes']==80,'Expected original A2 logical schedule')
    require(len(manifest['schedule'])==80 and len({e['run_id'] for e in manifest['schedule']})==80,'Invalid source schedule')
    require(state['completed_episodes']==12 and not state.get('active_episodes') and not state.get('active_run_id'),'Expected twelve valid carried results and stopped source')
    require(state['stop_reason']=='parallel_trial_failed' and state['parallel_trial']['cleanup_confirmed'] is True,'Source incident is not safely stopped')
    require(auth['selected_recovery_option']=='B' and auth['user_confirmation']=='ok','Missing explicit option B authorization')
    require(auth['total_admission_cap']==51600000 and auth['additional_authorized_attempts']==6,'Budget amendment changed')
    require(auth['new_attempt_count']==68 and auth['new_attempt_token_admission']==600000,'New-attempt scope changed')
    require(auth['cost_stop_usd']==480 and auth['cost_warning_usd']==320,'Original known-cost boundaries changed')
    require(auth['global_model_slots']==8 and auth['provider_limits']=={'codex_account':4,'glm_coding':1,'deepseek':4},'Provider concurrency changed')
    require(auth['candidate_memory']=='1g' and auth['candidate_container_cap']==12,'Container policy changed')
    require(state['token_admission_sum']==carry['legacy_token_admission_sum']==auth['legacy_admission_tokens_retained']==10800000,'Historical admission cannot be refunded')
    require(math.isclose(state['known_cost_usd'],carry['legacy_known_cost_usd'],rel_tol=0,abs_tol=1e-9),'Legacy known subtotal differs')
    require(carry['carried_valid']==state['episodes'],'Carried result identities differ')
    require([x['run_id'] for x in carry['carried_valid']]==[e['run_id'] for e in manifest['schedule'][:12]],'Carried identities are not original first twelve')
    require(len(carry['legacy_unknown_calls'])==3 and carry['legacy_unknown_reserved_tokens']==118387,'Legacy unknown accounting changed')
    require(state['started_at']==auth['stage_started_at'] and state['package_started_at']==auth['package_started_at'],'Original time anchors changed')
    require(manifest['stage_limits']['dispatch_seconds']==auth['stage_dispatch_seconds']==219600,'Original stage window changed')
    require(auth['package_max_seconds']==345600,'Original package window changed')
    require(auth['automatic_additional_retry'] is False,'No further retry authority')
    require(10800000+68*600000==auth['total_admission_cap'],'Global admission equation failed')
    return True

def copy_verified(src,dst,expected):
    require(src.is_file() and sha(src)==expected,'Source drift: '+str(src))
    dst.parent.mkdir(parents=True,exist_ok=True)
    require(not dst.exists(),'Destination already exists: '+str(dst))
    shutil.copyfile(src,dst)
    require(sha(src)==sha(dst)==expected,'Copy identity differs: '+str(dst))

def safe_relative(root,relative):
    path=(Path(root)/relative).resolve()
    require(path.is_relative_to(Path(root).resolve()) and path!=Path(root).resolve(),'Path escapes snapshot')
    return path

def prepare(source_batch,batch,authorization=AUTHORIZATION,*,builder=None):
    source_batch,batch,authorization=Path(source_batch).resolve(),Path(batch).resolve(),Path(authorization).resolve()
    require(source_batch.is_relative_to(EXPERIMENT/'batches') and batch.is_relative_to(EXPERIMENT/'batches'),'Batch outside experiment')
    require(source_batch!=batch and not batch.exists(),'Recovery must use a new batch directory')
    require(authorization==AUTHORIZATION.resolve(),'Unexpected authorization document')
    original,old_state,auth=read(source_batch/'manifest.json'),read(source_batch/'state.json'),read(authorization)
    source_manifest_sha=sha(source_batch/'manifest.json')
    source_state_sha=sha(source_batch/'state.json')
    require((source_batch/'manifest.sha256').read_text(encoding='ascii').strip()==source_manifest_sha,'Original manifest identity differs')
    require(not Path(original['resource_lock_path']).exists(),'Shared stage lease held')
    previous=load_module('_prepare_recovery_previous',HERE/'recovery_controller.py').previous_controller_status(source_batch)
    build=builder or load_module('_prepare_recovery_accounting',HERE/'a2_recovery_accounting.py').build_carryover
    carry=build(source_batch,incident_dir=HERE/'a2-wave4-incident-v1')
    check_contract(original,old_state,carry,auth)
    require(sha(source_batch/'state.json')==source_state_sha,'Source changed during carryover audit')
    current=datetime.now(timezone.utc)
    stage_deadline=datetime.fromisoformat(old_state['started_at'])+timedelta(seconds=219600)
    package_deadline=datetime.fromisoformat(old_state['package_started_at'])+timedelta(hours=96)
    require(current<min(stage_deadline,package_deadline),'Original execution window expired')
    batch.mkdir(parents=True,exist_ok=False)
    copy_verified(source_batch/'manifest.json',batch/'source-manifest.json',source_manifest_sha)
    copy_verified(source_batch/'state.json',batch/'source-state.json',source_state_sha)
    source_snapshot=source_batch/'runtime_snapshot'
    new_snapshot=batch/'runtime_snapshot'
    for relative,expected in original['runtime_sources'].items():
        copy_verified(safe_relative(source_snapshot,relative),safe_relative(new_snapshot,relative),expected)
    source_official=source_snapshot/'modelbench/minimal_value_20260905/official'
    new_official=new_snapshot/'modelbench/minimal_value_20260905/official'
    for relative,expected in original['input_artifacts'].items():
        copy_verified(safe_relative(source_official,relative),safe_relative(new_official,relative),expected)
    require(not (new_snapshot/'modelbench/keys.local.json').exists(),'Credentials copied into snapshot')
    write_new(batch/'carryover.json',carry)
    pending=[e['run_id'] for e in original['schedule'][12:]]
    failed=old_state['parallel_trial']['settlements']
    require(set(failed)==set(pending[:6]),'Recovery retries are not exactly six interrupted attempts')
    plan={'version':1,'source_batch':str(source_batch),'source_manifest_sha256':source_manifest_sha,'source_state_sha256':source_state_sha,
          'carried_valid':carry['carried_valid'],'pending_run_ids':pending,
          'legacy_token_admission_sum':carry['legacy_token_admission_sum'],'legacy_known_cost_usd':carry['legacy_known_cost_usd'],
          'legacy_unknown_calls':carry['legacy_unknown_calls'],'legacy_unknown_reserved_tokens':carry['legacy_unknown_reserved_tokens'],
          'total_admission_cap':51600000,'cost_stop_usd':480.0,'cost_warning_usd':320.0,
          'stage_started_at':old_state['started_at'],'package_started_at':old_state['package_started_at'],
          'stage_deadline':stage_deadline.isoformat(),'package_deadline':package_deadline.isoformat(),'deadline':min(stage_deadline,package_deadline).isoformat(),
          'authorization_path':str(authorization),'authorization_sha256':sha(authorization),
          'carryover_path':str(batch/'carryover.json'),'carryover_sha256':sha(batch/'carryover.json'),
          'execution_root':str(new_snapshot),'attempt_lineage':[
              {'run_id':rid,'attempt_index':2 if rid in failed else 1,'source_attempt_dir':str(source_batch/'results'/rid) if rid in failed else None,
               'source_attempt_outcome':'infrastructure_interrupted' if rid in failed else 'never_started'} for rid in pending],
          'logical_target_count':80,'carried_valid_count':12,'new_attempt_count':68,'legacy_attempt_count':18,'maximum_total_attempt_count':86,
          'new_state_completed_episodes_semantics':'Only engineering-valid new attempts; carried twelve are references in this recovery plan',
          'new_state_token_admission_semantics':'Historical 10.8M plus new attempts; never refund interrupted attempts',
          'overall_cost_computable':False,'old_unknown_retained':True,'new_unknown_stops_dispatch':True,
          'automatic_additional_retries':False,'original_failed_wave_path':old_state['parallel_trial']['group_dir'],
          'created_at':current.isoformat()}
    write_new(batch/'recovery-plan.json',plan)
    manifest=copy.deepcopy(original)
    manifest['root']=str(new_snapshot)
    manifest['stage_limits']['token_admission_sum']=51600000
    manifest['recovery_plan_sha256']=sha(batch/'recovery-plan.json')
    manifest['source_manifest_sha256']=source_manifest_sha
    changed=[key for key in manifest if manifest.get(key)!=original.get(key)]
    require(set(changed)=={'root','stage_limits','recovery_plan_sha256','source_manifest_sha256'},'Unexpected manifest change')
    preserved=copy.deepcopy(manifest['stage_limits']);preserved['token_admission_sum']=48000000
    require(preserved==original['stage_limits'] and manifest['schedule']==original['schedule'],'Scientific schedule or limits changed')
    write_new(batch/'manifest.json',manifest)
    (batch/'manifest.sha256').write_text(sha(batch/'manifest.json')+'\n',encoding='ascii')
    write_new(batch/'state.json',{'stage':'a2','started_at':old_state['started_at'],'package_started_at':old_state['package_started_at'],
        'completed_episodes':0,'episodes':[],'known_cost_usd':carry['legacy_known_cost_usd'],'token_admission_sum':10800000,
        'stop_reason':None,'recovery_plan_sha256':sha(batch/'recovery-plan.json'),'legacy_unknown_call_count':3,
        'overall_cost_computable':False,'carried_valid_episodes':12,'new_attempts_scheduled':68})
    require(sha(source_batch/'state.json')==source_state_sha and sha(source_batch/'manifest.json')==source_manifest_sha,'Original source evidence changed')
    receipt={'prepared':True,'models_started':0,'batch':str(batch),'source_batch':str(source_batch),'manifest_sha256':sha(batch/'manifest.json'),
       'recovery_plan_sha256':sha(batch/'recovery-plan.json'),'carryover_sha256':sha(batch/'carryover.json'),
       'original_manifest_unchanged':True,'original_state_unchanged':True,'runtime_files_copied':len(original['runtime_sources']),
       'input_files_copied':len(original['input_artifacts']),'manifest_allowed_changes':changed,'preparation_source_sha256':sha(Path(__file__)),
       'previous_controller':previous,'new_attempt_count':68,'global_admission_cap':51600000,'finished_at':datetime.now(timezone.utc).isoformat()}
    write_new(batch/'preparation-receipt.json',receipt)
    return receipt

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-batch',type=Path,required=True)
    parser.add_argument('--batch',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.source_batch,args.batch),ensure_ascii=True),flush=True)

if __name__=='__main__':main()
