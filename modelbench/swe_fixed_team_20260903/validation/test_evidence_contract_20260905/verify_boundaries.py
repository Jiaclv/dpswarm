"""Read-only source/history verification for the approved role-text change."""
import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from modelbench.swe_fixed_team_20260903 import cli, runner

ART = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def nodes(tree):
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.name] = node
        elif isinstance(node, ast.ClassDef):
            # Preserve the class shell and separately compare every method.
            methods = [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for method in methods:
                result[f'{node.name}.{method.name}'] = method
            node.body = [item for item in node.body if item not in methods]
            result[node.name] = node
        else:
            result[f'toplevel:{len(result)}'] = node
    return result


def verify():
    before = nodes(ast.parse((ART / 'runner.before.py').read_text(encoding='utf-8')))
    after = nodes(ast.parse((ROOT / 'modelbench/swe_fixed_team_20260903/runner.py').read_text(encoding='utf-8')))
    removed = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())
    changed = sorted(name for name in before.keys() & after.keys()
                     if ast.dump(before[name]) != ast.dump(after[name]))
    assert not removed, removed
    assert added == ['LEAD_TEST_EVIDENCE_CONTRACT', 'TEST_EVIDENCE_CONTRACT_VERSION', 'TEST_WORKER_EVIDENCE_CONTRACT'], added
    assert changed == ['FIXED_ASSIGNMENTS', 'SweRun.prompt'], changed
    prior_assignments = ast.literal_eval(before['FIXED_ASSIGNMENTS'].value)
    assert runner.FIXED_ASSIGNMENTS[0] == prior_assignments[0]
    assert runner.FIXED_ASSIGNMENTS[1]['title'] == prior_assignments[1]['title']
    assert runner.FIXED_ASSIGNMENTS[1]['task'] == prior_assignments[1]['task'] + '\n\n' + runner.TEST_WORKER_EVIDENCE_CONTRACT
    approved = (ROOT / '机制改动提案-测试证据-20260905.md').read_text(encoding='utf-8')
    approved_quotes = [line.removeprefix('> ') for line in approved.splitlines() if line.startswith('> ')]
    assert approved_quotes == [runner.TEST_WORKER_EVIDENCE_CONTRACT, runner.LEAD_TEST_EVIDENCE_CONTRACT]

    baseline_sources = read(ART / 'baseline-runtime-sources.json')
    current_sources = cli.sources()
    changed_sources = sorted(path for path in baseline_sources.keys() | current_sources.keys()
                             if baseline_sources.get(path) != current_sources.get(path))
    assert changed_sources == [
        'modelbench/swe_fixed_team_20260903/cli.py',
        'modelbench/swe_fixed_team_20260903/runner.py',
        'modelbench/swe_fixed_team_20260903/tests/test_runner_rev11.py',
        'modelbench/swe_fixed_team_20260903/tests/test_test_evidence_contract_20260905.py'], changed_sources
    protected = read(ART / 'protected-before.json')
    mismatches = [path for path, expected in protected.items()
                  if not (ROOT / path).is_file() or sha(ROOT / path) != expected]
    assert not mismatches, mismatches
    old_gate = read(cli.HERE / 'validation/gate_revision12_post_canary_20260905.json')
    assert cli.input_artifacts() == old_gate['input_artifacts']
    assert all(sha(ROOT / path) == expected for path, expected in old_gate['additional_sources'].items())
    result = {
        'status': 'PASS',
        'runner_ast_added': added, 'runner_ast_changed': changed,
        'runner_ast_unchanged': sorted(before.keys() - set(changed)),
        'production_assignment_unchanged': True,
        'test_assignment_only_appends_approved_text': True,
        'contract_text_exactly_matches_approved_proposal': True,
        'runtime_changes_from_approved_work_baseline': changed_sources,
        'runtime_source_count': len(current_sources),
        'protected_files_unchanged': len(protected),
        'protected_mismatches': mismatches,
        'prior_input_and_auditor_hashes_unchanged': True,
        'contract_version': runner.TEST_EVIDENCE_CONTRACT_VERSION,
        'contract_text_sha256': {name: hashlib.sha256(getattr(runner, name).encode()).hexdigest()
                                 for name in ('TEST_WORKER_EVIDENCE_CONTRACT', 'LEAD_TEST_EVIDENCE_CONTRACT')},
        'mechanism_document_sha256': sha(ROOT / 'DPswarm-机制架构.md'),
        'approved_proposal_sha256': sha(ROOT / '机制改动提案-测试证据-20260905.md'),
    }
    (ART / 'boundary-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('status', 'runner_ast_changed', 'runtime_source_count', 'protected_files_unchanged')}, ensure_ascii=False))


if __name__ == '__main__':
    verify()
