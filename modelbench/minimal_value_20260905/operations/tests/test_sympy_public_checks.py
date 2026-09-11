"""Native public-summary parsing only: no Docker, models or SymPy execution."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('tested_sympy_public_checks',ROOT/'sympy_public_checks.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
COMMAND=m.COMMANDS['sympy__sympy-13852']


def result(text,exit_code=0,**kwargs):
    return {'stdout':text,'stderr':'','exit_code':exit_code,'timed_out':False,**kwargs}


def test_native_pass_count_is_function_count_and_not_pytest():
    observed=m.public_check_execution(COMMAND,result('=== tests finished: 1 passed, in 0.42 seconds ===\n'))
    assert observed['framework']=='sympy-native'
    assert observed['executed']==observed['passed']==1 and observed['summary_complete']


def test_wrapped_native_summary_counts_failures_and_expected_outcomes_separately():
    observed=m.public_check_execution(COMMAND,result(
        'tests finished: 2 passed, 1 failed, 2 skipped,\n'
        '3 expected to fail, 1 expected to fail but passed,\n'
        'in 0.45 seconds\nDO *NOT* COMMIT!\n',1))
    assert observed['executed']==3 and observed['failed']==1 and observed['skipped']==2
    assert observed['expected_failures']==3 and observed['unexpected_passes']==1


def test_exceptions_do_not_qualify_even_when_other_test_passed():
    observed=m.native_execution(result('tests finished: 1 passed, 1 exceptions, in 1.00 seconds\n',1))
    assert observed['executed']==0 and observed['passed']==1 and observed['errors']==1
    assert observed['rejection_reason']=='native_runner_exceptions'


@pytest.mark.parametrize('text,reason',[
    ('', 'missing_or_ambiguous_complete_summary'),
    ('ModuleNotFoundError: No module named pytest\n','missing_or_ambiguous_complete_summary'),
    ('1 passed in 0.3s\n','missing_or_ambiguous_complete_summary'),
    ('tests finished: 1 passed,','missing_or_ambiguous_complete_summary'),
    ('tests finished: 0 passed, in 0.0 seconds\n','zero_runnable_tests'),
    ('tests finished: 0 passed, 1 skipped, in 0.0 seconds\n','zero_runnable_tests'),
    ('tests finished: 0 passed, 1 expected to fail, in 0.0 seconds\n','zero_runnable_tests'),
    ('tests finished: 1 passed, 4 invented, in 0.0 seconds\n','invalid_summary_fields'),
    ('tests finished: 1 passed, 1 passed, in 0.0 seconds\n','duplicate_summary_fields'),
    ('tests finished: 1 passed, in 0.0 seconds\ntests finished: 1 passed, in 0.0 seconds\n','missing_or_ambiguous_complete_summary'),
])
def test_missing_zero_or_unrecognized_summary_is_rejected(text,reason):
    observed=m.native_execution(result(text))
    assert observed['executed']==0 and observed['rejection_reason']==reason


@pytest.mark.parametrize('kwargs',[{'timed_out':True},{'exit_code':2},{'exit_code':True},{'output_truncated':True}])
def test_interrupted_or_truncated_output_never_counts_as_valid_execution(kwargs):
    observed=m.native_execution(result('tests finished: 1 passed, in 0.3 seconds\n',**kwargs))
    assert observed['executed']==0 and 'rejection_reason' in observed


def test_exit_status_must_agree_with_complete_native_summary():
    for text,code in [('tests finished: 1 passed, in 0.3 seconds\n',1),
                      ('tests finished: 0 passed, 1 failed, in 0.3 seconds\n',0)]:
        observed=m.native_execution(result(text,code))
        assert observed['executed']==0 and observed['rejection_reason']=='summary_exit_code_mismatch'


def test_ansi_and_stderr_summary_are_supported():
    observed=m.native_execution(result('',stderr='\x1b[32m=== tests finished: 1 passed, in 0.3 seconds ===\x1b[0m\r\n'))
    assert observed['executed']==1


def test_only_exact_commands_are_intercepted_and_other_results_delegate_unchanged():
    sentinel=object();calls=[]
    def old(command,result):calls.append((command,result));return sentinel
    for command in ['python -m pytest tests/test_other.py -q', COMMAND+' extra', COMMAND.replace(' -k polylog','')]:
        value=result('tests finished: 1 passed, in 0.3 seconds\n')
        assert m.public_check_execution(command,value,original=old) is sentinel
        assert calls[-1]==(command,value)


def test_install_is_process_local_and_refuses_duplicate_install(tmp_path):
    calls=[]
    module=SimpleNamespace(public_check_execution=lambda command,value:calls.append(command) or {'original':True})
    receipt=m.install(module,receipt_path=tmp_path/'receipt.json')
    assert receipt['models_started']==receipt['containers_started']==receipt['tests_executed']==0
    assert module.public_check_execution('other',{})=={'original':True}
    assert module.public_check_execution(COMMAND,result('tests finished: 1 passed, in 0.1 seconds\n'))['executed']==1
    assert calls==['other']
    with pytest.raises(RuntimeError,match='already installed'):m.install(module)


def test_v2_keeps_exact_16_tasks_and_other_14_values_byte_identical():
    def value_bytes(text,key):
        start=text.index(json.dumps(key)+': ')+len(json.dumps(key)+': ')
        _,length=json.JSONDecoder().raw_decode(text[start:])
        return text[start:start+length].encode('utf-8')
    for old_name,new_name in [('public_check_definitions.json','public_check_definitions_v2.json'),
                             ('public_check_provenance.json','public_check_provenance_v2.json')]:
        old=(ROOT/old_name).read_bytes().decode('utf-8');new=(ROOT/new_name).read_bytes().decode('utf-8')
        old_values,new_values=json.loads(old),json.loads(new)
        assert set(old_values)==set(new_values) and len(new_values)==16
        for instance_id in old_values:
            if instance_id not in m.COMMANDS:
                assert value_bytes(old,instance_id)==value_bytes(new,instance_id)
    definitions=json.loads((ROOT/'public_check_definitions_v2.json').read_text())
    for instance_id,command in m.COMMANDS.items():assert list(definitions[instance_id].values())==[command]
