"""Read-only evidence verification, then write new A2 backfill closeout artifacts."""
from pathlib import Path
from datetime import datetime, timezone
import csv, hashlib, json, math, statistics, subprocess
import psutil
BASE=Path(__file__).resolve().parent.parent
OPS=BASE/'operations'; BATCH=BASE/'batches/a2-backfill-v1'; RUN=OPS/'a2-backfill-auto-v1'

def read(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def require(ok,message):
    if not ok: raise RuntimeError(message)
def write_new(p,v):
    with Path(p).open('x',encoding='utf-8') as f: json.dump(v,f,ensure_ascii=False,indent=2); f.write('\n')

def main():
    state=read(BATCH/'state.json'); manifest=read(BATCH/'manifest.json'); launch=read(RUN/'launch.json')
    auth=read(OPS/'A2_BACKFILL_AUTHORIZATION_20260906.json'); runplan=read(RUN/'plan.json')
    require(state['completed_episodes']==7 and len(state['episodes'])==7,'All seven scores are not complete')
    require(not state.get('active_episodes'),'Active episodes remain')
    require(not Path(manifest['resource_lock_path']).exists() and not (BATCH/'continuation.lock').exists(),'Experiment leases remain')
    try:
        proc=psutil.Process(launch['pid'])
        require(abs(proc.create_time()-launch['created_at'])>0.01,'Backfill supervisor still alive')
    except psutil.NoSuchProcess: pass
    trial=state.get('parallel_trial',{})
    require(trial.get('cleanup_confirmed') is True and not trial.get('report_error'),'Wave cleanup or report incomplete')
    docker=subprocess.run(['docker','ps','--quiet'],capture_output=True,text=True,check=True)
    require(not docker.stdout.strip(),'Running Docker containers remain')
    for p,d in runplan['sources'].items(): require(sha(p)==d,'Operator source drift: '+p)
    oldpath=OPS/'a2-resume-auto-v3/CLOSEOUT_VALIDATION.json'; old=read(oldpath)
    refs=list(old['results'])+list(state['episodes']); expected={e['run_id']:e for e in manifest['schedule']}
    require(len(refs)==80 and len({r['run_id'] for r in refs})==80 and {r['run_id'] for r in refs}==set(expected),'Full matrix is not 80 unique positions')
    require({r['run_id'] for r in state['episodes']}==set(auth['pending_run_ids']),'New results differ from authorized seven')
    rows=[]; validated=[]; evidence={str(oldpath):sha(oldpath),str(BATCH/'state.json'):sha(BATCH/'state.json'),str(BATCH/'manifest.json'):sha(BATCH/'manifest.json')}
    for ref in refs:
        p=Path(ref['path']); require(sha(p)==ref['sha256'],'Result hash mismatch: '+ref['run_id']); r=read(p); e=expected[ref['run_id']]; score=r['score']
        require(r['run_id']==ref['run_id'] and score.get('completed') is True and type(score.get('resolved')) is bool,'Invalid official score')
        require(r.get('quiesced') is True and r.get('cleanup_confirmed') is True,'Result lacks quiescence/cleanup')
        artifact=r.get('artifact') or r.get('lead_artifact'); patch=Path(artifact['path']); digest=sha(patch)
        require(digest==artifact['sha256']==score['patch_sha256'],'Patch binding differs')
        require(bool(score.get('reports_sha256')),'Official report hashes missing')
        grader=Path(score['grader_dir']).resolve()
        for rel,d in score['reports_sha256'].items():
            report=(grader/rel).resolve(); require(report.is_relative_to(grader) and sha(report)==d,'Official report binding differs'); evidence[str(report)]=d
        accounting=r['accounting']; known=float(accounting['api_equivalent_known_subtotal_usd'])
        call_known=sum(float(c['api_equivalent_usd']) for c in accounting['calls'] if c.get('api_equivalent_usd') is not None)
        require(math.isclose(known,call_known,rel_tol=0,abs_tol=1e-8),'Call/result cost disagreement')
        rows.append(dict(run_id=ref['run_id'],arm=e['arm'],block=e['block_id'],instance_id=e['instance']['instance_id'],resolved=score['resolved'],known_api_equivalent_usd=known,known_tokens=accounting['total_tokens_known_subtotal'],known_calls=accounting['call_count'],usage_unknown_calls=accounting['usage_unknown_calls'],generation_seconds=r['inference_wall_seconds'],backfilled=ref['run_id'] in auth['pending_run_ids'],result_path=str(p),result_sha256=ref['sha256']))
        validated.append(dict(run_id=ref['run_id'],path=str(p),sha256=ref['sha256'],resolved=score['resolved'])); evidence[str(p)]=ref['sha256'];evidence[str(patch)]=digest
    new=[r for r in rows if r['backfilled']]; new_cost=sum(r['known_api_equivalent_usd'] for r in new)
    require(state['token_admission_sum']==55800000,'Cumulative admissions differ')
    require(math.isclose(state['known_cost_usd'],auth['legacy_known_cost_usd']+new_cost,rel_tol=0,abs_tol=1e-8),'Cumulative cost differs')
    unknown=read(BATCH/'recovery-plan.json')['legacy_unknown_calls'];require(len(unknown)==10 and sum(r['reserved_tokens'] for r in unknown)==308099,'Historical unknowns changed')
    arms=[]
    for arm in ['S','L','R2','D','T']:
        rr=[r for r in rows if r['arm']==arm];require(len(rr)==16,'Incomplete arm');arms.append(dict(arm=arm,valid=16,resolved=sum(r['resolved'] for r in rr),mean_api_equivalent_usd=statistics.mean(r['known_api_equivalent_usd'] for r in rr),median_generation_minutes=statistics.median(r['generation_seconds'] for r in rr)/60,mean_known_tokens=statistics.mean(r['known_tokens'] for r in rr)))
    receipt=dict(status='PASS',checked_at=datetime.now(timezone.utc).isoformat(),valid_total=80,resolved=sum(r['resolved'] for r in rows),unresolved=sum(not r['resolved'] for r in rows),original_matrix_complete=True,original_matrix_size=80,carried_valid=73,new_valid=7,new_resolved=sum(r['resolved'] for r in new),total_admitted_attempts=93,token_admission_sum=55800000,token_admission_is_actual_usage=False,known_api_equivalent_usd=state['known_cost_usd'],new_known_api_equivalent_usd=new_cost,unknown_call_count=10+sum(r['usage_unknown_calls'] for r in new),historical_unknown_reserved_tokens=308099,overall_cost_computable=False,supervisor_exited=True,running_docker_containers=0,active_stage_lease_released=True,continuation_lease_released=True,wave_cleanup_confirmed=True,operator_source_hashes_verified=len(runplan['sources']),result_hashes_verified=80,results=validated,arms=arms,new_results=new,source_refs=evidence)
    write_new(RUN/'CLOSEOUT_VALIDATION.json',receipt)
    with (RUN/'FULL_MATRIX_RESULTS.csv').open('x',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(sorted(rows,key=lambda r:r['run_id']))
    print(json.dumps({k:receipt[k] for k in ['status','valid_total','resolved','new_resolved','new_known_api_equivalent_usd','known_api_equivalent_usd','arms']},ensure_ascii=True))

if __name__=='__main__': main()
