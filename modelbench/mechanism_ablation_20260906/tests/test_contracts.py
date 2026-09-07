from copy import deepcopy
import json
from pathlib import Path
import pytest
from modelbench.mechanism_ablation_20260906 import contracts as c

@pytest.fixture
def schedule():
    public=json.loads((c.HERE/'public_inputs.json').read_text(encoding='utf-8'))['tasks']
    return [c.build_entry(row,public[row['instance_id']]['instance'],public[row['instance_id']]['public_checks'],public[row['instance_id']]['image'],public[row['instance_id']]['grader_contract']) for row in c.load_cells()]

def test_complete_balanced_CM_only_schedule(schedule):
    assert c.validate_schedule(schedule)
    assert len(schedule)==18
    assert sum(e['effective_limits']['token_limit'] for e in schedule)==10800000
    assert sum(e['effective_limits']['cm_call_allowance'] for e in schedule)==108
    for i in range(0,18,2):
        x,y=deepcopy(schedule[i:i+2])
        for item in (x,y):
            for k in ['run_id','episode_id','condition_id','strategy_id','position','priority','configuration_sha256']:item.pop(k)
            for limits in ['effective_limits','limits_override']:
                item[limits].pop('cm_enabled');item[limits].pop('cm_call_allowance')
        assert x==y

@pytest.mark.parametrize('field,value',[('lead_model','gpt-5.6-luna'),('condition_id','FB_HIGH_EQ'),('expected_workers',1),('image','sha256:'+'0'*64)])
def test_reject_identity_mutations(schedule,field,value):
    mutated=deepcopy(schedule[0]);mutated[field]=value
    with pytest.raises(ValueError):c.validate_entry(mutated)

def test_reject_budget_and_CM_drift(schedule):
    for field,value in [('max_calls',160),('cm_enabled',False),('token_limit',2400000)]:
        mutated=deepcopy(schedule[0]);mutated['limits_override'][field]=value
        with pytest.raises(ValueError):c.validate_entry(mutated)

def test_reject_hidden_input(schedule):
    mutated=deepcopy(schedule[0]);mutated['instance']['FAIL_TO_PASS']='hidden'
    with pytest.raises(ValueError):c.validate_entry(mutated)

def test_reject_missing_or_reordered_attempt(schedule):
    with pytest.raises(ValueError):c.validate_schedule(schedule[:-1])
    with pytest.raises(ValueError):c.validate_schedule(list(reversed(schedule)))
