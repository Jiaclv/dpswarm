"""Build a design registry and FlowMap; never prepare or launch experiments."""
from __future__ import annotations
import csv
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

P=Path(__file__).resolve().parent
ROOT=P.parents[3]
PLAN=P.parent/'NEXT_EXPERIMENT_PLAN_20260905.md'
FLOW=Path('C:/Users/93711/.agents/skills/flowmap/scripts/flow.py')
LEDGER=ROOT/'.flowmap/post-v16-plan.json'
TASKS=['pydata__xarray-7229','mwaskom__seaborn-3069','sphinx-doc__sphinx-8035','sympy__sympy-16792','scikit-learn__scikit-learn-25232','astropy__astropy-14995']
T='gpt-5.6-terra'; G='glm-5.3'; L='gpt-5.6-luna'; F='glm-5.3-flash'; S='gpt-5.6-sol'
ARMS={'S':[], 'TT':[T,T], 'TG':[T,G], 'GT':[G,T], 'GG':[G,G], 'TL':[T,L], 'LT':[L,T], 'LL':[L,L], 'GF':[G,F], 'FG':[F,G], 'FF':[F,F]}
CORE=['S','TT','TG','GT','GG']
FAMILY=['TT','TL','LT','LL','GG','GF','FG','FF']
FULL=CORE+['TL','LT','LL','GF','FG','FF']
BASE={'token_limit':600000,'max_calls':28,'wall_seconds':1800,'worker_calls':8,'lead_reserve_calls':2,'cm_call_allowance':12,'cm_enabled':True,'cm_context_budget':12000,'cm_keep_recent':4,'cm_model':'deepseek-v4-flash','cm_thinking':'disabled','cm_max_tokens':4096,'cm_edit_curfew':False,'edit_status_banner':False,'closing_call_reserve_exempt':True,'cm_scout_distill':False,'cm_bootstrap_package':False,'cm_team_memory':False}
STAGES=[
    ('P0','工程离线验收',0,0,'offline',[],None,'R-clean','all'),
    ('P1','通道可用性',6,6,'run',['P0'],None,'R-clean','all'),
    ('P2','F1 F2 F5 全因素',72,72,'run',['P1'],None,'R-clean','all'),
    ('P3-core','核心角色2x2加Solo',90,90,'run',['P1'],None,'R-clean','core_or_sequential'),
    ('P3-full','角色及家族一体版',198,198,'run',['P1'],None,'R-clean','integrated_only'),
    ('P4','后补家族含同期TT/GG',144,144,'run',['P3-core'],None,'R-clean','sequential_only'),
    ('P5','装配全ON/OFF',36,36,'run',['P1'],None,'R-clean','optional'),
    ('P5-components','装配2x2x2分解',72,72,'run',['P5'],None,'R-clean','optional'),
    ('P6','token和工作调用2x2',48,48,'run',['P1'],None,'R-clean','optional'),
    ('R-cost','美元准入与适配器验收',0,0,'offline',['P0'],None,'R-cost','usd_required'),
    ('P7-check','新美元适配器真实可用性',6,8,'episode',['R-cost','P1'],24,'R-cost','usd_required'),
    ('P7','同USD策略开发比较',108,144,'episode',['P7-check'],324,'R-cost','optional'),
    ('P8','新任务确认',180,240,'episode',['P7'],720,'R-cost','optional'),
    ('P9','Lead互换开发比较',72,72,'run',['P1'],None,'R-clean+lead-contract','optional'),
    ('P10-dev','条件启用开发比较',54,54,'episode',['P8'],216,'R-cost+dynamic-host','optional'),
    ('P10-confirm','条件启用新留出确认',180,180,'episode',['P10-dev'],720,'R-cost+dynamic-host','optional'),
]
cells=[]
def cell(stage,task,arm,n,limits=None,lead=S,workers=None,usd=None,strategy=None,selected=False):
    limits=BASE|dict(limits or {})
    worker_models=ARMS.get(arm,[]) if workers is None else workers
    cid=f'{stage}:{task}:{arm}'
    if any(c['cell_id']==cid for c in cells):raise ValueError(cid)
    runtime_condition=('fixed_team' if len(set(worker_models))==1 else 'hetero_team') if worker_models else 'solo'
    cells.append({'cell_id':cid,'stage_id':stage,'task_id':task,'arm_id':arm,'repeat_count':n,'lead_model':lead,'worker_1':worker_models[0] if worker_models else '', 'worker_2':worker_models[1] if worker_models else '', 'runtime_condition':runtime_condition if strategy!='DYNAMIC' else 'not_implemented','strategy':strategy or ('fixed_team' if worker_models else 'solo'),'usd_design_quota':usd,'limits_proposal_json':json.dumps(limits,sort_keys=True),'execution_authorized':False,'state':'proposed','runtime_support':'requires_new_registration_and_gate','selected_from_development':selected})

for task in [TASKS[5],TASKS[2],TASKS[0]]:
    for arm in ['S','TG']:cell('P1',task,arm,1)
for task in [TASKS[2],TASKS[0],TASKS[1]]:
    for f1 in [False,True]:
        for f2 in [2048,4096]:
            for f5 in [False,True]:cell('P2',task,f'S_F1{int(f1)}_F2{f2}_F5{int(f5)}',3,{'cm_edit_curfew':f1,'cm_max_tokens':f2,'edit_status_banner':f5})
for stage,arms in [('P3-core',CORE),('P3-full',FULL),('P4',FAMILY)]:
    for task in TASKS:
        for arm in arms:cell(stage,task,arm,3)
for task in TASKS:
    for on in [False,True]:cell('P5',task,f'TG_assembly{int(on)}',3,{k:on for k in ['cm_scout_distill','cm_bootstrap_package','cm_team_memory']},workers=ARMS['TG'])
for task in [TASKS[2],TASKS[0],TASKS[1]]:
    for scout in [False,True]:
        for package in [False,True]:
            for memory in [False,True]:cell('P5-components',task,f'TG_s{int(scout)}p{int(package)}m{int(memory)}',3,{'cm_scout_distill':scout,'cm_bootstrap_package':package,'cm_team_memory':memory},workers=ARMS['TG'])
for task in [TASKS[2],TASKS[0]]:
    for arm in ['S','TG']:
        for tokens in [600000,900000]:
            for calls in [28,40]:cell('P6',task,f'{arm}_t{tokens}_c{calls}',3,{'token_limit':tokens,'max_calls':calls},workers=ARMS[arm])
usd_limits={'max_calls':40,'token_limit':1500000}
for stage,tasks,reps,quotas in [('P7-check',[TASKS[5],TASKS[2]],1,[4]),('P7',TASKS,3,[2,4]),('P8',[f'HOLDOUT_A_{i:02d}_UNSELECTED' for i in range(1,31)],2,[4])]:
    for task in tasks:
        for strategy in ['S1','S2','T']:
            for quota in quotas:cell(stage,task,f'{strategy}_usd{quota}',reps,usd_limits,workers=ARMS['TG'] if strategy=='T' else [],usd=quota,strategy=strategy)
for task in TASKS:
    for lead in [S,G]:
        for team in [False,True]:cell('P9',task,f'{"TG" if team else "Solo"}_{lead}',3,lead=lead,workers=ARMS['TG'] if team else [])
for stage,tasks,reps in [('P10-dev',TASKS,3),('P10-confirm',[f'HOLDOUT_B_{i:02d}_UNSELECTED' for i in range(1,31)],2)]:
    for task in tasks:
        for strategy in ['S1','T','DYNAMIC']:cell(stage,task,strategy,reps,usd_limits,workers=ARMS['TG'] if strategy=='T' else [],usd=4,strategy=strategy)

stage_rows=[]
for sid,label,n,gen,unit,deps,usd,protocol,scenario in STAGES:
    a=[c for c in cells if c['stage_id']==sid]
    assert sum(c['repeat_count'] for c in a)==n,(sid,n)
    observed_quota=sum((c['usd_design_quota'] or 0)*c['repeat_count'] for c in a)
    if usd is not None:assert observed_quota==usd,(sid,observed_quota,usd)
    stage_rows.append({'stage_id':sid,'name':label,'unit':unit,'planned_units':n,'candidate_generation_runs':gen,'selector_processes':n//3 if sid in ['P7-check','P7','P8'] else 0,'prerequisites_json':json.dumps(deps),'protocol':protocol,'scenario':scenario,'usd_design_quota':usd,'suggested_dispatch_window_hours':math.ceil(n*1800/2*1.5/3600)+1 if n else 0,'execution_authorized':False,'status':'proposed'})

def csv_write(path,rs):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
csv_write(P/'stage_plan.csv',stage_rows)
csv_write(P/'planned_cells.csv',cells)
scenarios={'minimum':['P1','P2','P3-core'],'integrated':['P1','P2','P3-full'],'sequential':['P1','P2','P3-core','P4']}
totals={name:sum(r['planned_units'] for r in stage_rows if r['stage_id'] in ids) for name,ids in scenarios.items()}
assert totals=={'minimum':168,'integrated':276,'sequential':312}
contract={'schema':'dpswarm-post-v16-design-v1','status':'proposed_not_executable','execution_authorized':False,'plan_sha256':hashlib.sha256(PLAN.read_bytes()).hexdigest(),'source_analysis':'../consolidated_v16_20260905/REPORT_ZH.md','root_plan':'../NEXT_EXPERIMENT_PLAN_20260905.md','task_order':TASKS,'role_order':['production_implementation','independent_regression_test'],'fixed_lead':S,'base_limits_proposal':BASE,'worker_arms':ARMS,'mutual_exclusion':[['P3-full','P3-core'],['P3-full','P4']],'scenarios':scenarios,'scenario_units':totals,'heldout_ids':'placeholders; selected and frozen by metadata-only algorithm before evaluation','usd_enforcement':'NOT IMPLEMENTED; gate required; design quota is not present spending authority','full_cli_wave_support':'NOT IMPLEMENTED; new registry binding and gate required','stage_table':'stage_plan.csv','cell_table':'planned_cells.csv'}
contract['strategy_contract']={
    'all_usd_strategies':{'resource_scope':'whole_episode_including_all_candidates_workers_cm_selector_retries','max_calls':40,'cm_call_allowance':12,'token_limit':1500000,'wall_seconds':1800,'lawful_budget_rejection':'strategy_outcome_not_infrastructure_stop'},
    'S1':{'lead':S,'candidate_count':1},
    'S2':{'candidate_models':[S,S],'parallel_start':True,'candidate_budget_fractions':[0.45,0.45],'selector_budget_fraction':0.10,'unused_budget_transfer':False,'candidate_max_calls':[19,19],'selector_max_calls':2,'selector_model':S,'selector_can_edit_patch':False,'selector_official_grader_access':False,'fallback':'first applicable nonempty patch; then second; else empty','oracle_pass_at_2':'secondary_upper_bound_only'},
    'T':{'default_arm':'TG','assembly_on':False,'selected_from_dev':False},
    'DYNAMIC':{'status':'unimplemented_policy_must_be_frozen','fixed_budget':4,'probe_and_route_costs_in_episode':True},
}
contract['confirmatory_analysis']={'stage':'P8','primary_endpoint':'end_to_end_success','definition':'official complete pass plus frozen patch and closed usage/grading evidence; no requirement that every worker completes or team_valid is true','secondary_endpoints':['official_resolved','F2P','P2P','cost','wall_time','team_valid'],'primary_contrasts':['T-S1','T-S2'],'independent_unit':'task','tasks':30,'repeats_within_task':2,'multiple_comparisons':'two predeclared primary contrasts require familywise control','continue_research_threshold':'point delta >=0.10 and adjusted interval lower bound >0','confirmed_practical_magnitude':'adjusted interval lower bound >=0.10','no_optional_stopping':True}
(P/'plan_contract.json').write_text(json.dumps(contract,ensure_ascii=False,indent=2),encoding='utf-8')

def flow(*args):
    p=subprocess.run([sys.executable,str(FLOW),*args,'--file',str(LEDGER)],cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
    if p.returncode:raise RuntimeError(p.stdout+p.stderr)
    return p.stdout

if not LEDGER.exists():
    flow('init','--name','dpswarm-post-v16','--goal','按门槛逐阶段建立工程、机制、异构角色和成本收益证据；当前仅计划','--max-branch-depth','1')
    flow('node','add','--id','goal','--label','可靠且可复核的后续实验证据','--kind','goal','--summary','后续目标尚未执行；本轮只交付计划')
    flow('node','add','--id','plan','--label','完备后续计划与逐格矩阵','--kind','artifact','--status','running','--data-ref',str(PLAN.relative_to(ROOT)))
    for s in stage_rows:
        flow('node','add','--id',s['stage_id'],'--label',f"{s['stage_id']} {s['name']} · {s['planned_units']} {s['unit']}",'--kind','task' if s['unit']=='offline' else 'experiment','--summary',f"仅提案，未启动；方案={s['scenario']}；依赖和互斥条件以主计划为准")
    for s in stage_rows:
        deps=json.loads(s['prerequisites_json']) or ['plan']
        for dep in deps:flow('edge','add','--from',dep,'--to',s['stage_id'],'--kind','depends')
    for source in ['P2','P3-core','P3-full','P4','P5','P6','P8','P9','P10-confirm']:
        flow('edge','add','--from',source,'--to','goal','--kind','data')
    flow('root','set','--id','goal')
    flow('focus','set','--id','plan')
else:
    snapshot=json.loads(LEDGER.read_text(encoding='utf-8'))
    existing={n['id'] for n in snapshot['nodes']}
    for s in stage_rows:
        if s['stage_id'] not in existing:
            flow('node','add','--id',s['stage_id'],'--label',f"{s['stage_id']} {s['name']} · {s['planned_units']} {s['unit']}",'--kind','task' if s['unit']=='offline' else 'experiment','--summary','仅提案，未启动；依赖和互斥以主计划为准')
    # Reconcile only this plan's ordinary dependency edges through the CLI.
    for s in stage_rows:
        desired=json.loads(s['prerequisites_json']) or ['plan']
        old=[e['from'] for e in snapshot['edges'] if e['to']==s['stage_id'] and e['kind']=='depends']
        for source in old:
            if source not in desired:flow('edge','rm','--from',source,'--to',s['stage_id'],'--kind','depends')
        for source in desired:
            if source not in old:flow('edge','add','--from',source,'--to',s['stage_id'],'--kind','depends')
flow('node','status','--id','plan','--status','done','--note','计划、矩阵及独立审查完成；没有启动后续工程或实验')
flow('check')
flow('render','--out',str(P/'flow.html'))
(P/'flow_summary.txt').write_text(flow('summary'),encoding='utf-8')
(P/'plan_validation.json').write_text(json.dumps({'status':'PASS','design_only':True,'cells':len(cells),'stages':len(stage_rows),'scenario_units':totals,'no_models_called':True,'checks':['stage repeat counts','USD quotas','mutually exclusive scenario totals','unique cell ids','FlowMap structural validation']},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'stages':len(stage_rows),'cells':len(cells),'scenario_units':totals,'model_calls':0},ensure_ascii=False))
