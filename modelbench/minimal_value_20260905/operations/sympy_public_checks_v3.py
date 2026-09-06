"""Explicit revision 3 SymPy-native public-check parsing for two frozen tasks.

The 13852 command matches the successful compatibility probe byte-for-byte.
Native counter semantics are retained from v2; original frozen files are intact.

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

VERSION = 'sympy_native_public_checks_v3'
COMMANDS = {'sympy__sympy-13852': 'python - <<\'PY\'\nimport re, sys, warnings\nfrom sympy.utilities import runtests as runner\nmessage = "Using or importing the ABCs from \'collections\' instead of from \'collections.abc\' is deprecated since Python 3.3, and in 3.10 it will stop working"\noriginal_test = runner.SymPyTests.test\ndef test_with_py39_filter(self, *args, **kwargs):\n    with warnings.catch_warnings():\n        warnings.filterwarnings(\'ignore\', \'^\' + re.escape(message) + \'$\', DeprecationWarning, r\'^sympy\\.core\\.function$\')\n        return original_test(self, *args, **kwargs)\nrunner.SymPyTests.test = test_with_py39_filter\nsys.exit(0 if runner.test(\'sympy/functions/special/tests/test_zeta_functions.py\', kw=\'polylog\', colors=False, subprocess=False) else 1)\nPY', 'sympy__sympy-22456': 'python bin/test --no-colors sympy/codegen/tests/test_ast.py -k String'}
COMPATIBILITY = {'version': 3, 'task': 'sympy__sympy-13852', 'warning_filter': {'action': 'ignore', 'message': "Using or importing the ABCs from 'collections' instead of from 'collections.abc' is deprecated since Python 3.3, and in 3.10 it will stop working", 'message_regex': "^Using\\ or\\ importing\\ the\\ ABCs\\ from\\ 'collections'\\ instead\\ of\\ from\\ 'collections\\.abc'\\ is\\ deprecated\\ since\\ Python\\ 3\\.3,\\ and\\ in\\ 3\\.10\\ it\\ will\\ stop\\ working$", 'category': 'DeprecationWarning', 'module_regex': '^sympy\\.core\\.function$', 'installation_point': 'SymPyTests.test entry, after _test installs error filters and before original native runner executes tests', 'scope': 'warnings.catch_warnings restores filters on return; all other messages, modules and categories retain original runner treatment'}, 'native_runner_preserved': 'Original SymPyTests.test executes test discovery, cache handling, original function assertions, exception reporting and native summary. No source or test assertion is changed.', 'subprocess': False, 'hash_seed_difference': 'The native hash-seed subprocess is disabled to retain the process-local warning filter. The successful probe reports hash randomization: off, unlike attempt02 which used a generated PYTHONHASHSEED; this is an explicit execution-protocol difference, not equivalent hash-seed coverage.', 'verified_probe': {'path': 'K:\\秋招\\项目\\DPswarm\\modelbench\\minimal_value_20260905\\validation\\sympy-compat-probe-v1\\result.json', 'sha256': '5511baf51303ef03bb5b813222e2d1c6c85934412d9c0f07be74caa201cdfa6e', 'command_sha256': '99fb0f4ec056c4e4f002de40040a79d97c39683df3508030d3361a329726130f', 'exit_code': 0, 'executed_test_functions': 1, 'passed': 1, 'errors': 0, 'model_calls': 0, 'native_summary_seconds': 0.04, 'command_duration_seconds': 1.125, 'python': '3.9.20-final-0', 'observed_hash_randomization': 'off', 'cleanup_path': 'K:\\秋招\\项目\\DPswarm\\modelbench\\minimal_value_20260905\\validation\\sympy-compat-probe-v1\\final-cleanup.json', 'cleanup_sha256': 'b74cb86b6c65f2820f1e9c0ae7e975139c2596445d2cdeedb0cce206c351b68d', 'scope': 'One real public test function passed in the task image; not an A2 qualification pass, official result, or mechanism outcome.'}, 'counting_semantics_source': {'path': 'sympy_public_checks.py', 'sha256': '4b51bc1b9683cb5ea797c3cb7c0375eb138e36bc8d40effb20e4253703c3aa19', 'version': 'sympy_native_public_checks_v2', 'relationship': 'Native summary-counting implementation copied unchanged except reported parser version; no runtime import dependency.'}}

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
        'compatibility':COMPATIBILITY,
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
