"""Finite C1 contracts. No provider, subprocess or container side effects."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
import csv,json,re
from pathlib import Path
from modelbench.minimal_value_20260905.contracts import PUBLIC_FIELDS,FORBIDDEN,digest
from modelbench.minimal_value_20260905.runtime_integrity import resolve_limits

HERE=Path(__file__).resolve().parent
PROTOCOL='mechanism_ablation_C1_20260906_v1'
CONDITIONS=('FCM_ON','FCM_OFF')

def load_plan(path=None):
    p=json.loads(Path(path or HERE/'PLAN.json').read_text(encoding='utf-8-sig'))
    if p.get('schema')!='mechanism_ablation_plan_v1' or p.get('default_stage')!='C1' or p.get('default_generation_attempts')!=18:
        raise ValueError('Only the explicit 18-cell C1 plan is supported')
    c=p['default_common']
    expected={'token_limit':600000,'ordinary_call_limit':28,'worker_work_call_limit':8,'wall_seconds':7200,'cm_model':'deepseek-v4-flash','cm_provider':'deepseek','cm_summary_instruction_target_tokens':2000,'cm_api_output_max_tokens':4096,'cm_context_threshold_estimated_tokens':12000,'cm_keep_recent_messages':4,'container_memory':'1g','container_cpus':2,'cm_team_memory':False,'cm_edit_curfew':False,'cm_scout_distill':False,'cm_bootstrap_package':False,'automatic_model_retry':False,'model_fallback':False}
    if any(type(c.get(k)) is not type(v) or c[k]!=v for k,v in expected.items()):raise ValueError('C1 common configuration drift')
    if c['roles']!=[{'role':'lead','model':'gpt-5.6-sol'},{'role':'implementation','model':'glm-5.3-flash'},{'role':'test','model':'glm-5.3-flash'}]:raise ValueError('C1 role-model drift')
    if p['default_conditions']!=[{'condition_id':'FCM_ON','cm_enabled':True,'cm_call_limit':12},{'condition_id':'FCM_OFF','cm_enabled':False,'cm_call_limit':0}]:raise ValueError('C1 CM treatment drift')
    if {t['instance_id'] for t in p['tasks']}!={'pydata__xarray-6992','matplotlib__matplotlib-20826','pylint-dev__pylint-6386'}:raise ValueError('C1 task scope drift')
    if p['scheduler']['provider_shared_limits']!={'codex_account':4,'glm_coding_plan':4,'deepseek':4}:raise ValueError('Provider pool drift')
    return p

def load_cells(path=None,*,plan=None):
    p=load_plan() if plan is None else plan
    with Path(path or HERE/'C1_PLANNED_RUNS.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    tasks={t['instance_id'] for t in p['tasks']}
    if len(rows)!=18 or [int(r['planned_order']) for r in rows]!=list(range(1,19)):raise ValueError('C1 matrix count/order drift')
    if len({r['run_id'] for r in rows})!=18:raise ValueError('Duplicate run identity')
    for r in rows:
        on=r['condition_id']=='FCM_ON'
        if (r['stage']!='C1' or r['instance_id'] not in tasks or r['condition_id'] not in CONDITIONS or int(r['replicate']) not in (1,2,3)
            or int(r['token_limit'])!=600000 or int(r['ordinary_call_limit'])!=28 or int(r['cm_call_limit'])!=(12 if on else 0)
            or r['cm_enabled']!=str(on).lower() or r['cm_model']!='deepseek-v4-flash' or r['cm_provider_pool']!='deepseek'
            or [r['lead_model'],r['implementation_model'],r['test_model']]!=['gpt-5.6-sol','glm-5.3-flash','glm-5.3-flash']
            or int(r['candidate_container_slots'])!=3 or r['pair_id']!=f"C1-{r['instance_id']}-r{r['replicate']}"
            or (r['pilot'].lower()=='true')!=(int(r['planned_order'])<=6)):
            raise ValueError('C1 cell definition drift: '+r.get('run_id','?'))
    if set(Counter((r['instance_id'],r['condition_id']) for r in rows).values())!={3} or len({(r['instance_id'],r['condition_id'],r['replicate']) for r in rows})!=18:raise ValueError('C1 repeats not balanced')
    for index in range(0,18,2):
        x,y=rows[index:index+2]
        if x['pair_id']!=y['pair_id'] or {x['condition_id'],y['condition_id']}!=set(CONDITIONS):raise ValueError('Incomplete adjacent pair')
    return rows

def _entry(cell,instance,checks,image,grader_contract,plan):
    if not isinstance(instance,dict) or set(instance)!=set(PUBLIC_FIELDS) or FORBIDDEN.intersection(instance):raise ValueError('Only public instance fields are allowed')
    if any(not isinstance(v,str) or not v.strip() for v in instance.values()):raise ValueError('Public values must be nonempty text')
    task=next((t for t in plan['tasks'] if t['instance_id']==instance['instance_id']),None)
    if task is None or cell['instance_id']!=instance['instance_id'] or instance['base_commit']!=task['base_commit'] or instance['repo']!=task['repository'] or instance['version']!=task['version']:raise ValueError('Public task identity differs from frozen plan')
    if image!=task['image']:raise ValueError('Image differs from frozen plan')
    if not isinstance(checks,dict) or FORBIDDEN.intersection(checks) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in checks.items()):raise ValueError('Invalid public checks')
    if not isinstance(grader_contract,dict):raise ValueError('A frozen grader contract is required')
    from modelbench.minimal_value_20260905.runner import LIMITS
    on=cell['condition_id']=='FCM_ON'
    overrides={'token_limit':600000,'max_calls':28,'worker_calls':8,'lead_reserve_calls':2,'wall_seconds':7200,'cm_call_allowance':12 if on else 0,'cm_enabled':on,'cm_model':'deepseek-v4-flash','cm_provider':'deepseek','cm_context_budget':12000,'cm_keep_recent':4,'cm_max_tokens':4096,'cm_reservation_slack':8192,'cm_thinking':'disabled','cm_edit_curfew':False,'cm_team_memory':False,'cm_scout_distill':False,'cm_bootstrap_package':False,'edit_status_banner':False,'closing_call_reserve_exempt':True,'memory':'1g','cpus':2}
    effective=resolve_limits({**LIMITS,'active_workers':2,'delegations':2},overrides)
    specs=[{'worker_id':'worker-1','role':'implementation','model':'glm-5.3-flash'},{'worker_id':'worker-2','role':'test','model':'glm-5.3-flash'}]
    value={'protocol':PROTOCOL,'plan_sha256':digest(plan),'run_id':cell['run_id'],'episode_id':cell['run_id'],'condition_id':cell['condition_id'],'strategy_id':cell['condition_id'],'base_arm':'T','arm':'T','condition':'value_team','lead_model':'gpt-5.6-sol','worker_specs':specs,'worker_models':['glm-5.3-flash']*2,'worker_model':None,'expected_workers':2,'token_factor':0,'call_bundle_factor':0,'limits_override':overrides,'effective_limits':effective,'instance':deepcopy(instance),'public_checks':deepcopy(checks),'image':image,'grader_contract':deepcopy(grader_contract),'activation_source':'experiment_protocol','block_id':cell['pair_id'],'phase':1 if cell['pilot'].lower()=='true' else 2,'rep':int(cell['replicate']),'position':1 if int(cell['planned_order'])%2 else 2,'priority':int(cell['planned_order']),'candidate_container_slots':3}
    value['configuration_sha256']=digest(value)
    return value

def build_entry(cell,public_instance,public_checks=None,image=None,grader_contract=None,*,plan=None):
    p=load_plan() if plan is None else plan
    expected=[r for r in load_cells(plan=p) if r['run_id']==cell.get('run_id')]
    if expected!=[cell]:raise ValueError('Cell is not an unchanged member of C1')
    return _entry(cell,public_instance,{} if public_checks is None else public_checks,image,grader_contract,p)

def validate_entry(entry,*,plan=None):
    p=load_plan() if plan is None else plan
    if not isinstance(entry,dict):raise ValueError('Entry must be a mapping')
    cells=[r for r in load_cells(plan=p) if r['run_id']==entry.get('run_id')]
    if len(cells)!=1:raise ValueError('Entry is outside the C1 plan')
    expected=_entry(cells[0],entry.get('instance'),entry.get('public_checks'),entry.get('image'),entry.get('grader_contract'),p)
    if entry!=expected:raise ValueError('Entry configuration/identity/hash drift')
    return deepcopy(expected)

def validate_schedule(entries,*,plan=None):
    p=load_plan() if plan is None else plan;cells=load_cells(plan=p)
    if not isinstance(entries,list) or [e.get('run_id') for e in entries]!=[c['run_id'] for c in cells]:raise ValueError('Schedule must contain only the 18 ordered C1 cells')
    inputs={}
    for e in entries:
        validate_entry(e,plan=p);task=e['instance']['instance_id'];view={k:e[k] for k in ('instance','public_checks','image','grader_contract')}
        if task in inputs and inputs[task]!=view:raise ValueError('Public inputs differ between arms')
        inputs[task]=view
    return True
