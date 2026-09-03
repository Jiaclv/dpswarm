"""Restore the model-visible turn budget omitted in the first v2 integration."""
import argparse
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import random
import shutil
import time

from ..paths import ROOT, REPO, LEGACY
from ..runner import TeamRun, LIMITS, MAX_CALLS, TOKEN_LIMIT, dump, tree_hashes, utc
from .. import cli as v2_cli


class BudgetAwareTeamRun(TeamRun):
    def tool(self, phase, call, index, count):
        changes = []
        normalized = deepcopy(call)
        if call['name'] == 'submit_handoff':
            facts = normalized['arguments'].get('facts')
            if isinstance(facts, dict) and isinstance(facts.get('deliverables'), list):
                for index_path, original in enumerate(facts['deliverables']):
                    if not isinstance(original, str):
                        continue
                    path = original
                    if path.startswith('/shared/workspace/'):
                        path = path[len('/shared/workspace/'):]
                    while path.startswith('./'):
                        path = path[2:]
                    # This is a representation adapter, not path traversal or
                    # answer completion. Never collapse '..', backslashes,
                    # arbitrary absolute roots, or change a filename.
                    if not path or path.startswith('/') or '\\' in path or '..' in path.split('/'):
                        continue
                    if path != original:
                        facts['deliverables'][index_path] = path
                        changes.append({'field': f'facts.deliverables[{index_path}]', 'from': original, 'to': path})
        result = super().tool(phase, normalized, index, count)
        if changes:
            result['representation_normalizations'] = changes
        return result

    def turn(self):
        phase = self.data['current']
        budget = self.budget.summary()
        ordinal = phase['calls'] + 1
        phase_remaining = max(0, LIMITS[phase['phase']] - ordinal)
        global_remaining = max(0, budget['remaining_calls'] - 1)
        content = (f"Runtime budget: phase {phase['phase']}, model call {ordinal}/{LIMITS[phase['phase']]}. "
                   f"After this call: {phase_remaining} additional phase calls and {global_remaining} global calls remain. "
                   "Errors and NoAction consume calls. Batch related tools (up to eight) when useful. "
                   "Deliver required artifacts and finish_phase within this budget; informational prose is not completion.")
        if phase_remaining == 0 or global_remaining == 0:
            content += (" LAST ALLOWED CALL for this phase. Submit the necessary handoff/attestation/artifacts "
                        "and finish_phase in this tool batch. If checks remain unresolved, state that honestly "
                        "and finish blocked; do not claim unseen tool results passed.")
        self.history(phase['role']).append({'role': 'user', 'content': content})
        return super().turn()


def source_hashes():
    hashes = v2_cli.sources()
    files = [Path(__file__), Path(__file__).with_name('__init__.py'), Path(__file__).with_name('PLAN.md')]
    return hashes | {str(p.relative_to(REPO)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def prepare(root):
    original = v2_cli.verify_legacy()
    entries = [{'run_id': f'{task}__budget_visible__{model}__r1', 'task_id': task,
                'condition': 'team_v2_budget_visible', 'executor': model, 'repeat': 1}
               for task in v2_cli.TASKS for model in v2_cli.MODELS]
    random.Random(20260903).shuffle(entries)
    manifest = {'created_at': utc(), 'experiment_kind': 'budget_and_path_alias_repair_followup',
        'schedule': entries, 'image_id': original['image_id'], 'source_commit': original['source_commit'],
        'instances': original['instances'], 'runtime_sources': source_hashes(), 'limits': LIMITS,
        'max_calls_per_run': MAX_CALLS, 'token_admission_limit_per_run': TOKEN_LIMIT,
        'max_parallel_runs': 2, 'batch_deadline_seconds': 14400,
        'maximum_scheduled_calls': MAX_CALLS * len(entries),
        'sum_token_admission_limits': TOKEN_LIMIT * len(entries),
        'planner': 'gpt-5.6-sol', 'verifier': 'gpt-5.6-terra', 'effort_requested': 'max',
        'gpt_service_tier_requested': 'fast',
        'difference_from_v2': 'Restore per-call visible phase/global budget and last-call delivery reminder; normalize equivalent workspace-relative/absolute deliverable paths before unchanged finite-fact validation; same tools, scheduler, limits and grader',
        'comparison_scope': 'Four-run engineering follow-up after the observed budget-prompt omission; no population or model-ranking claim',
        'stop_policy': 'Stop new waves on infrastructure/grader error or STOP file; started peer may finish; no replacement runs'}
    for task, expected in original['instances'].items():
        target = root / 'instances' / task
        if not target.exists():
            shutil.copytree(LEGACY / 'instances' / task, target)
        if tree_hashes(target) != expected:
            raise RuntimeError('Instance mismatch: ' + task)
    target = root / 'manifest.json'
    if target.exists():
        old = json.loads(target.read_text(encoding='utf-8'))
        if {k:v for k,v in old.items() if k != 'created_at'} != {k:v for k,v in manifest.items() if k != 'created_at'}:
            raise RuntimeError('Frozen budget-visibility revision drift')
        return old
    dump(target, manifest)
    for name, expected in manifest['runtime_sources'].items():
        source = REPO / name
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Source changed during freezing: ' + name)
        destination = root / 'source_snapshot' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return manifest


def verify(root):
    v2_cli.verify_legacy()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if source_hashes() != manifest['runtime_sources']:
        raise RuntimeError('Frozen budget-visibility sources changed')
    for task, expected in manifest['instances'].items():
        if tree_hashes(root / 'instances' / task) != expected:
            raise RuntimeError('Frozen instance changed: ' + task)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'run', 'report'))
    parser.add_argument('--root', type=Path, default=ROOT / 'regression_budget_visible_20260903')
    parser.add_argument('--run-id')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == 'prepare':
        manifest = prepare(root)
        print(json.dumps({'prepared': True, 'runs': len(manifest['schedule'])}))
        return
    manifest = verify(root)
    if args.command == 'report':
        print(json.dumps(v2_cli.report(root, manifest), ensure_ascii=False))
        return
    entries = [e for e in manifest['schedule'] if not args.run_id or e['run_id'] == args.run_id]
    if not entries or (args.resume and not args.run_id):
        raise ValueError('Choose a valid explicit run ID for resume')
    started, stopped = time.time(), False
    with ThreadPoolExecutor(max_workers=2) as pool:
        for offset in range(0, len(entries), 2):
            if stopped or (root / 'STOP').exists() or time.time() - started >= 14400:
                dump(root / 'stopped.json', {'at': utc(), 'reason': 'engineering_stop_gate',
                                           'not_started': [e['run_id'] for e in entries[offset:]]})
                break
            futures = [pool.submit(BudgetAwareTeamRun(root, entry, manifest).run, resume=args.resume)
                       for entry in entries[offset:offset+2]]
            for future in as_completed(futures):
                result = future.result()
                print('RUN_FINISHED ' + json.dumps({k: result.get(k) for k in
                    ('run_id', 'status', 'score', 'call_count', 'total_tokens', 'wall_seconds')}, ensure_ascii=False), flush=True)
                if result['status'] in ('infrastructure_error', 'grader_error'):
                    stopped = True
            v2_cli.report(root, manifest)
    dump(root / 'batch_timing.json', {'started_epoch': started, 'completed_at': utc(), 'wall_seconds': time.time()-started})


if __name__ == '__main__':
    main()
