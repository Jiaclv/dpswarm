"""Explicit SymPy-native public-check parsing for two frozen public tasks.

Installation affects only the current preparation process. No test, container,
model request or official input write is performed. Other command strings use
the original preparation parser unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import hashlib
import importlib
import json
from pathlib import Path
import re

VERSION = 'sympy_native_public_checks_v2'
COMMANDS = {
    'sympy__sympy-13852': 'python bin/test --no-colors sympy/functions/special/tests/test_zeta_functions.py -k polylog',
    'sympy__sympy-22456': 'python bin/test --no-colors sympy/codegen/tests/test_ast.py -k String',
}
_SUMMARY = re.compile(r'(?:^|\n)[ =\t]*tests finished:\s*(.*?)\bin\s+([0-9]+(?:\.[0-9]+)?)\s+seconds\b[ =\t]*(?=\n|$)', re.S)
_COUNT = re.compile(r'([0-9]+) (passed|failed|skipped|expected to fail but passed|expected to fail|exceptions)')
_LABELS = {'passed':'passed', 'failed':'failed', 'skipped':'skipped',
           'expected to fail':'expected_failures', 'expected to fail but passed':'unexpected_passes',
           'exceptions':'errors'}


def native_execution(result):
    evidence = {'framework':'sympy-native', 'executed':0, 'passed':0, 'failed':0,
                'errors':0, 'skipped':0, 'expected_failures':0, 'unexpected_passes':0,
                'summary_complete':False, 'parser_version':VERSION}
    if result.get('timed_out') or type(result.get('exit_code')) is not int or result['exit_code'] not in (0,1):
        return {**evidence, 'rejection_reason':'timeout_or_unsupported_exit_code'}
    if result.get('output_truncated'):
        return {**evidence, 'rejection_reason':'truncated_output'}
    output = str(result.get('stdout','')) + '\n' + str(result.get('stderr',''))
    output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output).replace('\r\n','\n')
    summaries = list(_SUMMARY.finditer(output))
    if len(summaries) != 1 or output.count('tests finished:') != 1:
        return {**evidence, 'rejection_reason':'missing_or_ambiguous_complete_summary'}
    body = re.sub(r'\s+', ' ', summaries[0][1]).strip()
    parts = [item.strip() for item in body.split(',') if item.strip()]
    if not parts or re.fullmatch(r'[0-9]+ passed', parts[0]) is None:
        return {**evidence, 'rejection_reason':'invalid_summary_fields'}
    seen = set()
    for item in parts:
        match = _COUNT.fullmatch(item)
        if match is None:
            return {**evidence, 'rejection_reason':'invalid_summary_fields'}
        amount, label = int(match[1]), match[2]
        # Native unit-test and doctest failures can both be printed as failed.
        if label in seen and label != 'failed':
            return {**evidence, 'rejection_reason':'duplicate_summary_fields'}
        seen.add(label)
        evidence[_LABELS[label]] += amount
    evidence.update(summary_complete=True, duration_seconds=float(summaries[0][2]))
    expected_exit = int(evidence['failed'] > 0 or evidence['errors'] > 0)
    if result['exit_code'] != expected_exit:
        return {**evidence, 'rejection_reason':'summary_exit_code_mismatch'}
    if evidence['errors']:
        return {**evidence, 'rejection_reason':'native_runner_exceptions'}
    evidence['executed'] = evidence['passed'] + evidence['failed']
    if not evidence['executed']:
        evidence['rejection_reason'] = 'zero_runnable_tests'
    return evidence


def public_check_execution(command, result, *, original=None):
    if command in COMMANDS.values():
        return native_execution(result)
    if original is None:
        module = importlib.import_module('modelbench.minimal_value_20260905.operations.prepare_a2')
        candidate = module.public_check_execution
        original = getattr(candidate, '_sympy_native_original', candidate)
    return original(command, result)


def install(module=None, receipt_path=None):
    module = module or importlib.import_module('modelbench.minimal_value_20260905.operations.prepare_a2')
    original = module.public_check_execution
    if getattr(original, '_sympy_native_version', None):
        raise RuntimeError('SymPy public parser already installed; use a fresh preparation process')
    receipt = {'status':'installed', 'version':VERSION,
        'at':datetime.now(timezone.utc).isoformat(), 'commands':COMMANDS,
        'installer_path':str(Path(__file__).resolve()),
        'installer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scope':'Current preparation process only; all other command strings delegate unchanged',
        'models_started':0, 'containers_started':0, 'tests_executed':0}
    if receipt_path is not None:
        path = Path(receipt_path)
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as stream:
            json.dump(receipt,stream,ensure_ascii=True,indent=2)
            stream.write('\n')
    @wraps(original)
    def parser(command,result):
        return public_check_execution(command,result,original=original)
    parser._sympy_native_original = original
    parser._sympy_native_version = VERSION
    module.public_check_execution = parser
    return receipt
