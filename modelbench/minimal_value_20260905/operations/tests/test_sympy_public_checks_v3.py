"""Offline v3 boundaries; no Docker, model request or SymPy test execution."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
import warnings

import pytest

OPS = Path(__file__).resolve().parents[1]
ROOT = OPS.parent


def load(name):
    spec = importlib.util.spec_from_file_location('tested_' + name, OPS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v2 = load('sympy_public_checks')
v3 = load('sympy_public_checks_v3')
PROBE_PATH = ROOT / 'validation/sympy-compat-probe-v1/result.json'
PROBE = json.loads(PROBE_PATH.read_text(encoding='utf-8'))
COMMAND = v3.COMMANDS['sympy__sympy-13852']


def test_command_and_record_match_successful_real_probe_exactly():
    assert COMMAND.encode() == PROBE['command'].encode()
    assert v3.COMMANDS['sympy__sympy-22456'] == v2.COMMANDS['sympy__sympy-22456']
    observed = v3.public_check_execution(COMMAND, PROBE['result'])
    assert observed['framework'] == 'sympy-native'
    assert observed['executed'] == observed['passed'] == 1
    assert observed['errors'] == 0 and observed['summary_complete']
    assert observed['parser_version'] == 'sympy_native_public_checks_v3'
    evidence = v3.COMPATIBILITY['verified_probe']
    assert evidence['sha256'] == hashlib.sha256(PROBE_PATH.read_bytes()).hexdigest()
    assert evidence['command_sha256'] == hashlib.sha256(COMMAND.encode()).hexdigest()
    assert evidence['model_calls'] == PROBE['model_calls'] == 0
    assert evidence['observed_hash_randomization'] == 'off'
    assert 'hash randomization: off' in PROBE['result']['stdout']
    cleanup_path = Path(evidence['cleanup_path'])
    assert evidence['cleanup_sha256'] == hashlib.sha256(cleanup_path.read_bytes()).hexdigest()
    assert json.loads(cleanup_path.read_text(encoding='utf-8'))['confirmed']


@pytest.mark.parametrize('result', [
    {'stdout': 'tests finished: 1 passed, in 0.1 seconds\n', 'exit_code': 0},
    {'stdout': 'tests finished: 0 passed, 1 failed, in 0.1 seconds\n', 'exit_code': 1},
    {'stdout': 'tests finished: 1 passed, 1 exceptions, in 0.1 seconds\n', 'exit_code': 1},
    {'stdout': 'tests finished: 1 passed, 2 skipped,\n3 expected to fail,\n1 expected to fail but passed, in 0.2 seconds\n', 'exit_code': 0},
    {'stdout': 'tests finished: 0 passed, 1 skipped, in 0.2 seconds\n', 'exit_code': 0},
    {'stdout': 'tests finished: 1 passed,', 'exit_code': 0},
    {'stdout': 'tests finished: 1 passed, in 0.1 seconds\n', 'exit_code': 0, 'timed_out': True},
    {'stdout': 'tests finished: 1 passed, in 0.1 seconds\n', 'exit_code': 0, 'output_truncated': True},
])
def test_counter_semantics_are_identical_to_frozen_v2(result):
    old = v2.native_execution(result)
    new = v3.native_execution(result)
    old.pop('parser_version')
    new.pop('parser_version')
    assert new == old


def test_only_two_exact_current_commands_are_intercepted():
    sentinel = object()
    old = lambda command, result: sentinel
    for command in [v2.COMMANDS['sympy__sympy-13852'], COMMAND + '\n',
                    COMMAND.replace('subprocess=False', 'subprocess=True'),
                    'python -m pytest tests/test_other.py -q']:
        assert v3.public_check_execution(command, PROBE['result'], original=old) is sentinel
    assert v3.public_check_execution(v3.COMMANDS['sympy__sympy-22456'], PROBE['result'], original=old)['executed'] == 1


def test_receipt_v3_reports_installer_zero_activity_separate_from_prior_probe(tmp_path):
    module = SimpleNamespace(public_check_execution=lambda command, result: {'old': True})
    receipt = v3.install(module, tmp_path / 'receipt.json')
    assert receipt['version'] == 'sympy_native_public_checks_v3'
    assert receipt['models_started'] == receipt['containers_started'] == receipt['tests_executed'] == 0
    assert receipt['compatibility']['verified_probe']['executed_test_functions'] == 1
    assert receipt['installer_sha256'] == hashlib.sha256((OPS / 'sympy_public_checks_v3.py').read_bytes()).hexdigest()
    assert json.loads((tmp_path / 'receipt.json').read_text(encoding='utf-8')) == receipt
    assert module.public_check_execution('unrelated', {}) == {'old': True}
    assert module.public_check_execution(COMMAND, PROBE['result'])['executed'] == 1
    with pytest.raises(RuntimeError, match='already installed'):
        v3.install(module)


def value_bytes(raw, key):
    marker = json.dumps(key) + ': '
    start = raw.index(marker) + len(marker)
    _, length = json.JSONDecoder().raw_decode(raw[start:])
    return raw[start:start + length].encode('utf-8')


def test_all_16_tasks_preserved_other_14_raw_objects_identical():
    for kind in ('definitions', 'provenance'):
        old = (OPS / f'public_check_{kind}_v2.json').read_bytes().decode('utf-8')
        new = (OPS / f'public_check_{kind}_v3.json').read_bytes().decode('utf-8')
        before, after = json.loads(old), json.loads(new)
        assert set(before) == set(after) and len(after) == 16
        for task in before:
            if task not in v3.COMMANDS:
                assert value_bytes(old, task) == value_bytes(new, task)
        if kind == 'definitions':
            assert value_bytes(old, 'sympy__sympy-22456') == value_bytes(new, 'sympy__sympy-22456')
            for task, command in v3.COMMANDS.items():
                assert list(after[task].values()) == [command]
        else:
            check = after['sympy__sympy-13852']['checks']['public_polylog_expansion']
            assert check['command'] == COMMAND and check['timeout_seconds'] == 120
            assert check['compatibility'] == v3.COMPATIBILITY
            assert check['compatibility']['subprocess'] is False


def test_old_frozen_parser_definitions_and_provenance_unmodified():
    for name, expected in {
        'sympy_public_checks.py': '4b51bc1b9683cb5ea797c3cb7c0375eb138e36bc8d40effb20e4253703c3aa19',
        'public_check_definitions_v2.json': 'aab5882c04d0429a05ab64d04f2c0725030dddb268eae0fd52a867e5723d0c21',
        'public_check_provenance_v2.json': '371b870a63b01dc750e6d6c14097c283d9c91e67007380137b8bc974acc8e707',
    }.items():
        assert hashlib.sha256((OPS / name).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize('variant,category,module,expected_error', [
    ('exact', DeprecationWarning, 'sympy.core.function', False),
    ('unrelated', DeprecationWarning, 'sympy.core.function', True),
    ('exact', DeprecationWarning, 'sympy.core.other', True),
    ('suffix', DeprecationWarning, 'sympy.core.function', True),
    ('exact', RuntimeWarning, 'sympy.core.function', True),
])
def test_exact_command_warning_filter_preserves_five_boundaries(variant, category, module, expected_error):
    # Execute only the command's warning wrapper definition with a fake native
    # runner method. Neither SymPy import nor the command/test function runs.
    lines = COMMAND.splitlines()
    assert lines[0] == "python - <<'PY'" and lines[-1] == 'PY'
    tree = ast.parse('\n'.join(lines[1:-1]))
    selected = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == 'test_with_py39_filter'
                or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'message' for t in node.targets)]
    namespace = {'warnings': warnings, 're': re}
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<frozen-warning-wrapper>', 'exec'), namespace)
    message = namespace['message']
    text = message if variant == 'exact' else message + ' extra' if variant == 'suffix' else 'unrelated deprecation'
    calls = []
    owner = object()
    def original(self, *args, **kwargs):
        calls.append((self, args, kwargs))
        warnings.warn_explicit(text, category, 'public-source.py', 1246, module=module)
        return 'native-return'
    namespace['original_test'] = original
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        warnings.filterwarnings('error', '.*', DeprecationWarning, module='sympy.*')
        before = list(warnings.filters)
        if expected_error:
            with pytest.raises(category):
                namespace['test_with_py39_filter'](owner, 1, sort=True)
        else:
            assert namespace['test_with_py39_filter'](owner, 1, sort=True) == 'native-return'
        assert warnings.filters == before
    assert calls == [(owner, (1,), {'sort': True})]
