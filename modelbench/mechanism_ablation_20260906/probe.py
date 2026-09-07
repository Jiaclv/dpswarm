"""Exactly three qualification requests; no model retries or task solving."""
from __future__ import annotations
import json,math,re
from pathlib import Path
from uuid import uuid4
from modelbench.minimal_value_20260905.budget import EpisodeBudget
from modelbench.minimal_value_20260905.runtime_integrity import atomic_json
from modelbench.swe_verified_20260903.transport import text_tool_prompt
from .provider import ProviderGate,policy
from .transport import AblationTransport
from .execution import HERE,sha,utc,transport_sources

DECLARATIONS=[{'type':'function','function':{'name':'finish','description':'Finish this isolated connectivity probe.','parameters':{'type':'object','properties':{'summary':{'type':'string'}},'required':['summary'],'additionalProperties':False}}}]
CM_FACTS='Synthetic context only, not a benchmark task:\nTask: correct edge-case behavior without changing the public API.\nFile src/widget.py was modified at base abc123. Unit command pytest tests/test_widget.py -q passed 3 tests. The full suite has NOT been run. Full-suite status marker: FULL_SUITE_NOT_RUN.\nAn earlier proposal to remove the cache was rejected; do not repeat that proposal.\nThere is an unresolved conflict: reviewer A requires preserving an empty result, while reviewer B requires an error. Neither interpretation is approved. Conflict status marker: CONFLICT_UNRESOLVED.\nPatch identifier Q7F2 must be preserved. Next action is to resolve that conflict before claiming completion.\n'

def validate_record(record,model):
    issues=[]
    if record.get('error') or record.get('protocol_error'):issues.append('transport_or_protocol_error')
    if any(type(record.get(k)) is not int or record[k]<0 for k in ('input_tokens','output_tokens','total_tokens')):issues.append('usage_incomplete')
    if record.get('model_requested')!=model:issues.append('request_identity')
    action=record.get('action') or {}
    if model=='deepseek-v4-flash':
        text=action.get('text') or record.get('text') or ''
        if action.get('calls') or record.get('stop_reason')!='stop' or not text.strip():issues.append('CM_not_complete_plain_text')
        if 'Q7F2' not in text or 'FULL_SUITE_NOT_RUN' not in text:issues.append('CM_critical_fact_not_preserved')
        if 'CONFLICT_UNRESOLVED' not in text:issues.append('CM_conflict_not_preserved')
        try:
            request_path=Path((record.get('raw_artifacts') or {})['request'])
            wire=json.loads(request_path.read_text(encoding='utf-8'))
            body=wire.get('body') or {}
            if (wire.get('endpoint')!='https://api.deepseek.com/chat/completions' or body.get('model')!=model or body.get('tools')!=[] or body.get('thinking')!={'type':'disabled'} or body.get('max_tokens')!=4096 or body.get('stream') is not False):issues.append('CM_wire_request_contract')
            record['_probe_request_evidence']={'path':str(request_path),'sha256':sha(request_path),'endpoint':wire.get('endpoint')}
        except (OSError,ValueError,KeyError,TypeError):issues.append('CM_request_evidence_missing')
    else:
        calls=action.get('calls') or []
        if len(calls)!=1 or calls[0].get('name')!='finish' or calls[0].get('arguments',{}).get('summary')!='READY':issues.append('expected_single_finish_READY')
        if model=='glm-5.3-flash' and (record.get('endpoint')!='https://open.bigmodel.cn/api/coding/paas/v4/chat/completions' or record.get('model_reported')!=model):issues.append('GLM_Coding_identity')
        if model.startswith('gpt') and not record.get('native_tool_policy'):issues.append('Codex_tool_policy_missing')
    return issues

def main():
    auth=json.loads((HERE/'authorization.json').read_text(encoding='utf-8'))
    if auth.get('execution_authorized') is not True or auth.get('preflight_model_probes_max')!=3:raise ValueError('Three-probe authorization required')
    root=HERE/'preflight/probe-v1'
    if root.exists():raise ValueError('Probe directory exists; no automatic retry')
    root.mkdir(parents=True)
    pp=policy();atomic_json(root/'policy.json',pp)
    group={'global_lock_dir':str(root/'locks'),'policy_path':str(root/'policy.json'),'policy_sha256':sha(root/'policy.json')}
    atomic_json(root/'group.json',group)
    gate=ProviderGate(root,group,pp);transport=AblationTransport(root)
    limits={'max_calls':2,'token_limit':100000,'cm_call_allowance':1}
    budget=EpisodeBudget(**limits,deadline_seconds=2400,scopes={'probe':limits},path=root/'episode-budget.json');scope=budget.scope('probe')
    bindings=transport_sources();checks=[]
    summary={'passed':False,'calls_issued':0,'created_at':utc(),'checks':checks,'transport_sources':bindings,'probe_directory':str(root),'automatic_retry':False}
    atomic_json(HERE/'preflight/transport-probes.json',summary)
    for model in ('gpt-5.6-sol','glm-5.3-flash','deepseek-v4-flash'):
        role='cm' if model.startswith('deepseek') else 'lead'
        declarations=[] if role=='cm' else DECLARATIONS
        if role=='cm':
            messages=[{'role':'system','content':'You are a context manager. Faithfully summarize supplied history in English in under 500 tokens. Do not add facts, resolve conflicts or turn unverified claims into facts. Preserve the exact patch identifier, both uppercase status markers, and the distinction between local tests and the full suite.'},{'role':'user','content':CM_FACTS}]
        else:
            instruction='Call finish once with summary exactly READY. Do not execute any native tool. This probe has no repository or external task.'
            if model.startswith('gpt'):instruction=text_tool_prompt(declarations)+'\n'+instruction
            messages=[{'role':'system','content':'Perform a small isolated protocol qualification.'},{'role':'user','content':instruction}]
        maximum=4096 if role=='cm' else 32768
        call_id='C1-probe-'+uuid4().hex
        reserve=math.ceil(len(json.dumps(messages,ensure_ascii=False))/3)+maximum+(8192 if role=='cm' else 0)
        scope.reserve(call_id,role,reserve)
        record=None
        try:
            with gate.route(model,run_id='C1-preflight',role=role):
                if not gate.acquire(timeout=30):raise RuntimeError('Probe provider admission timeout')
                try:
                    summary['calls_issued']+=1;atomic_json(HERE/'preflight/transport-probes.json',summary)
                    record=gate.complete(AblationTransport.complete,transport,model,messages,tools=declarations,run_id='C1-preflight',role=role,task_id='synthetic-protocol-only',call_id=call_id,max_tokens=maximum,timeout_seconds=600)
                finally:gate.release()
        except Exception as exc:
            record=record or {'call_id':call_id,'model_requested':model,'error':{'type':type(exc).__name__,'message':str(exc)},'input_tokens':None,'output_tokens':None,'total_tokens':None}
        scope.complete(call_id,record)
        problems=validate_record(record,model)
        check={'model_requested':model,'model_reported':record.get('model_reported'),'role':role,'call_id':call_id,'http_status':record.get('http_status'),'endpoint':record.get('endpoint') or (record.get('_probe_request_evidence') or {}).get('endpoint'),'request_evidence':record.get('_probe_request_evidence'),'total_tokens':record.get('total_tokens'),'error':record.get('error'),'protocol_error':record.get('protocol_error'),'issues':problems,'passed':not problems,'raw_metadata_path':(record.get('raw_artifacts') or {}).get('metadata')}
        checks.append(check);summary.update(checks=checks,budget=budget.summary(),passed=len(checks)==3 and all(c['passed'] for c in checks))
        atomic_json(HERE/'preflight/transport-probes.json',summary)
        print(json.dumps(check,ensure_ascii=True),flush=True)
        if problems:break
    budget.freeze()
    if bindings!=transport_sources():summary.update(passed=False,binding_error='Transport sources changed during qualification')
    summary.update(budget=budget.summary(),finished_at=utc())
    atomic_json(HERE/'preflight/transport-probes.json',summary)
    if not summary['passed']:raise RuntimeError('Qualification incomplete; no experiment admission')

if __name__=='__main__':main()
