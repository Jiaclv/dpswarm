import copy
import importlib.util
import json
from pathlib import Path
import sys
import pytest

OPS=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('_test_prepare_recovery',OPS/'prepare_recovery.py')
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)

@pytest.fixture
def contract():
    b=OPS.parent/'batches/a2-v1'
    manifest=m.read(b/'manifest.json');state=m.read(b/'state.json');auth=m.read(m.AUTHORIZATION)
    carry={'carried_valid':copy.deepcopy(state['episodes']),'legacy_token_admission_sum':10800000,
           'legacy_known_cost_usd':state['known_cost_usd'],'legacy_unknown_calls':[{'id':str(i)} for i in range(3)],
           'legacy_unknown_reserved_tokens':118387}
    return manifest,state,carry,auth

def test_current_authorization_accepts_exact_preserved_contract(contract):
    assert m.check_contract(*contract)

@pytest.mark.parametrize('target,key,value',[
 ('state','token_admission_sum',7200000),
 ('state','started_at','2026-09-05T23:59:59+00:00'),
 ('state','known_cost_usd',17.883500314479928),
 ('auth','total_admission_cap',52000000),
 ('auth','new_attempt_count',69),
 ('auth','automatic_additional_retry',True),
 ('auth','selected_recovery_option','A'),
 ('auth','cost_stop_usd',500),
 ('carry','legacy_unknown_calls',[]),
 ('carry','legacy_unknown_reserved_tokens',0),
])
def test_changed_budget_unknown_scope_or_anchor_blocks(contract,target,key,value):
    manifest,state,carry,auth=copy.deepcopy(contract)
    {'state':state,'carry':carry,'auth':auth}[target][key]=value
    with pytest.raises(ValueError):m.check_contract(manifest,state,carry,auth)

def test_carried_projection_cannot_be_reordered(contract):
    manifest,state,carry,auth=copy.deepcopy(contract)
    carry['carried_valid'].reverse()
    with pytest.raises(ValueError):m.check_contract(manifest,state,carry,auth)

def test_existing_directory_is_not_overwritten(tmp_path):
    p=tmp_path/'receipt.json';p.write_text('original',encoding='utf-8')
    with pytest.raises(FileExistsError):m.write_new(p,{'new':True})
    assert p.read_text(encoding='utf-8')=='original'

def test_snapshot_relative_path_cannot_escape(tmp_path):
    with pytest.raises(ValueError):m.safe_relative(tmp_path/'snapshot','../keys.local.json')

def test_copy_checks_both_content_and_no_overwrite(tmp_path):
    source=tmp_path/'source';source.write_bytes(b'frozen-data')
    dest=tmp_path/'new/sub/file'
    m.copy_verified(source,dest,m.sha(source))
    assert dest.read_bytes()==b'frozen-data'
    with pytest.raises(ValueError):m.copy_verified(source,dest,m.sha(source))
    source.write_bytes(b'drift')
    with pytest.raises(ValueError):m.copy_verified(source,tmp_path/'other','a'*64)
    assert not (tmp_path/'other').exists()
