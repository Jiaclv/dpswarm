"""At most five normal qualification calls; never a model concurrency load test."""
import json
import math
from pathlib import Path
from uuid import uuid4
from modelbench.minimal_value_20260905.budget import EpisodeBudget
from .execution import HERE,read,sha,utc,atomic_json,transport_sources
from .provider import ProviderGate, LIMITS
from .transport import CodingPlanTransport,_base


def main():
    root=HERE/'preflight/coding-transport-probe-run-v4'
    if root.exists():raise ValueError('Probe attempt already exists; no automatic retry')
    root.mkdir(parents=True)
    policy={'provider_limits_version':3,'provider_limits_source':'operator_configured',
        'provider_limits':dict(LIMITS),
        'global_model_slots':16,'per_episode_model_slots':4}
    atomic_json(root/'policy.json',policy)
    group={'global_lock_dir':str(root/'locks'),'policy_path':str(root/'policy.json'),
           'policy_sha256':sha(root/'policy.json')}
    atomic_json(root/'group.json',group)
    gate=ProviderGate(root,group,policy);transport=CodingPlanTransport(root)
    limits={'max_calls':7,'token_limit':100000,'cm_call_allowance':1}
    budget=EpisodeBudget(**limits,deadline_seconds=3600,scopes={'probe':limits},path=root/'episode-budget.json')
    scope=budget.scope('probe')
    declarations=[{'type':'function','function':{'name':'finish','description':'Finish this connectivity probe.',
        'parameters':{'type':'object','properties':{'summary':{'type':'string'}},'required':['summary'],'additionalProperties':False}}}]
    checks=[]
    models=['glm-5.3-flash','glm-5.3','gpt-5.6-sol','gpt-5.6-terra','deepseek-v4-flash']
    for model in models:
        role='cm' if model.startswith('deepseek') else 'lead';call_id='probe-'+uuid4().hex
        tools=[] if role=='cm' else declarations
        instruction='Return the word READY.' if role=='cm' else 'Call finish with summary READY. This is a connectivity probe, with no repository or external action.'
        if model.startswith('gpt'):
            instruction=_base.text_tool_prompt(tools)+'\n'+instruction
        messages=[{'role':'system','content':'You are performing a small connectivity check.'},{'role':'user','content':instruction}]
        maximum=4096 if role=='cm' else 2048
        reserve=math.ceil(len(json.dumps(messages,ensure_ascii=False))/3)+maximum+(8192 if role=='cm' else 0)
        scope.reserve(call_id,role,reserve)
        record=None
        try:
            with gate.route(model,run_id='b1-preflight',role=role):
                if not gate.acquire(timeout=30):raise RuntimeError('Probe provider admission timed out')
                try:
                    record=gate.complete(CodingPlanTransport.complete,transport,model,messages,
                        tools=tools,run_id='b1-preflight',role=role,task_id='connectivity-only',
                        call_id=call_id,max_tokens=maximum,timeout_seconds=300)
                finally:gate.release()
        except Exception as exc:
            record=record or {'call_id':call_id,'model_requested':model,'error':{'type':type(exc).__name__,'message':str(exc)},
                              'input_tokens':None,'output_tokens':None,'total_tokens':None}
        scope.complete(call_id,record)
        ok=not record.get('error') and not record.get('protocol_error') and isinstance(record.get('total_tokens'),int)
        checks.append({'model_requested':model,'model_reported':record.get('model_reported'),
            'call_id':call_id,'http_status':record.get('http_status'),'endpoint':record.get('endpoint'),
            'adapter':record.get('adapter_mode'),'total_tokens':record.get('total_tokens'),
            'error':record.get('error'),'protocol_error':record.get('protocol_error'),'passed':ok})
        summary={'passed':len(checks)==5 and all(x['passed'] for x in checks),'calls_issued':len(checks),
            'created_at':utc(),'checks':checks,'transport_sources':transport_sources(),
            'budget':budget.summary(),'probe_directory':str(root),
            'glm_channel':'bigmodel_coding_plan','account_key_source':'existing local credential, never stored in manifest',
            'entitlement_source':'user confirmed Coding Plan; shared GLM cap is operator configured, not an entitlement claim',
            'actual_billed_cash_verified':False}
        atomic_json(HERE/'preflight/transport-probes-coding-v4.json',summary)
        print(json.dumps(checks[-1],ensure_ascii=True),flush=True)
        if not ok:break
    budget.freeze()
    if not summary['passed']:raise RuntimeError('Transport qualification incomplete; no experiment admission')


if __name__=='__main__':main()
