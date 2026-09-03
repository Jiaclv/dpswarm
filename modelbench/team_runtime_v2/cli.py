"""Freeze, run and summarize an independent repair regression without old edits."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

from .paths import ROOT, REPO, PLUGIN, LEGACY
from .runner import TeamRun, LIMITS, MAX_CALLS, TOKEN_LIMIT, CLARIFICATION_DEADLINE, dump, tree_hashes, utc

TASKS = ('SPEC5_config_system', 'INT1_pipeline_repair')
MODELS = ('glm-5.3', 'glm-5.3-flash')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_legacy():
    main = json.loads((LEGACY / 'manifest.json').read_text(encoding='utf-8'))
    native = json.loads((LEGACY / 'native_followup' / 'manifest.json').read_text(encoding='utf-8'))
    for manifest in (main, native):
        for name, expected in manifest['program_sha256'].items():
            if sha(LEGACY / name) != expected:
                raise RuntimeError('Frozen historical program drift: ' + name)
        for name, expected in manifest['control_core_sha256'].items():
            if sha(REPO / name) != expected:
                raise RuntimeError('Frozen control core drift: ' + name)
    for task, expected in main['instances'].items():
        if tree_hashes(LEGACY / 'instances' / task) != expected:
            raise RuntimeError('Frozen historical instance drift: ' + task)
    return main


def sources():
    files = list(ROOT.glob('*.py')) + [ROOT / 'PLAN.md']
    files += list((PLUGIN / 'dpswarm' / 'team_runtime').glob('*.py'))
    files += [PLUGIN / 'dpswarm' / 'providers' / name for name in ('base.py', 'openai_compat.py')]
    return {str(p.relative_to(REPO)).replace('\\', '/'): sha(p) for p in sorted(files)}


def prepare(root, *, repeats=2):
    root = Path(root).resolve()
    main = verify_legacy()
    for task in TASKS:
        destination = root / 'instances' / task
        if not destination.exists():
            shutil.copytree(LEGACY / 'instances' / task, destination)
        if tree_hashes(destination) != main['instances'][task]:
            raise RuntimeError('V2 instance differs from frozen source: ' + task)
    image = subprocess.run(['docker', 'image', 'inspect', main['image_id']], capture_output=True,
                           text=True, encoding='utf-8', check=True)
    if json.loads(image.stdout)[0]['Id'] != main['image_id']:
        raise RuntimeError('Image identity mismatch')
    schedule = [{'run_id': f'{task}__v2__{model}__r{repeat}', 'task_id': task,
                 'condition': 'team_v2', 'executor': model, 'repeat': repeat}
                for repeat in range(1, repeats + 1) for task in TASKS for model in MODELS]
    random.Random(20260903).shuffle(schedule)
    manifest = {'experiment_kind': 'combined_repair_regression_v2', 'created_at': utc(),
        'schedule': schedule, 'image_id': main['image_id'], 'source_commit': main['source_commit'],
        'runtime_sources': sources(), 'historical_manifest_sha256': sha(LEGACY / 'manifest.json'),
        'native_manifest_sha256': sha(LEGACY / 'native_followup' / 'manifest.json'),
        'instances': main['instances'], 'limits': LIMITS, 'max_calls_per_run': MAX_CALLS,
        'token_admission_limit_per_run': TOKEN_LIMIT, 'max_parallel_runs': 2,
        'clarification_deadline_seconds': CLARIFICATION_DEADLINE,
        'maximum_scheduled_calls': MAX_CALLS * len(schedule),
        'sum_token_admission_limits': TOKEN_LIMIT * len(schedule),
        'batch_deadline_seconds': 14_400, 'planner': 'gpt-5.6-sol', 'verifier': 'gpt-5.6-terra',
        'effort_requested': 'max', 'gpt_service_tier_requested': 'fast',
        'actual_settings_policy': 'Only provider echoes are observed; missing values remain null',
        'stop_policy': 'Stop scheduling on infrastructure/grader error or two consecutive invalid handoffs; no retries or replacement runs',
        'comparison_scope': 'Combination regression, 20-call v2 limit; historical 18-call runs are not a matched causal baseline',
        'python': sys.version}
    target = root / 'manifest.json'
    if target.exists():
        existing = json.loads(target.read_text(encoding='utf-8'))
        for field in manifest:
            if field not in ('created_at', 'python') and manifest[field] != existing[field]:
                raise RuntimeError('Frozen V2 manifest drift: ' + field)
        return existing
    dump(target, manifest)
    return manifest


def verify(root):
    verify_legacy()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if sources() != manifest['runtime_sources']:
        raise RuntimeError('Frozen V2 runtime changed; create a new regression version, never reuse results')
    for task, expected in manifest['instances'].items():
        if tree_hashes(root / 'instances' / task) != expected:
            raise RuntimeError('Frozen V2 instance changed: ' + task)
    return manifest


def report(root, manifest):
    results = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((root / 'results').glob('*/result.json'))]
    rows = []
    for value in results:
        phases = value.get('phases', [])
        rows.append({key: value.get(key) for key in ('run_id', 'task_id', 'executor', 'repeat', 'status',
            'call_count', 'input_tokens', 'output_tokens', 'total_tokens', 'total_tokens_known_subtotal',
            'usage_complete', 'wall_seconds', 'handoff_validated')} | {
            'pass': (value.get('score') or {}).get('pass'),
            'checks': (value.get('score') or {}).get('secondary'),
            'protocol_errors': sum(p['protocol_errors'] for p in phases),
            'no_actions': sum(p['no_actions'] for p in phases),
            'clarification_count': len(value.get('clarifications', {}).get('requests', {})),
            'terminal_reason': value.get('terminal_reason')})
    if rows:
        with (root / 'summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    known = sum(v.get('total_tokens_known_subtotal', 0) for v in results)
    unknown = sum(v.get('budget', {}).get('unknown_call_count', 0) for v in results)
    summary = {'created_at': utc(), 'scheduled_runs': len(manifest['schedule']), 'completed_runs': len(results),
        'passes': sum(r['pass'] is True for r in rows), 'calls': sum(r['call_count'] for r in rows),
        'total_tokens': None if unknown else known, 'known_token_subtotal': known, 'unknown_usage_calls': unknown,
        'sum_run_wall_seconds': sum(r['wall_seconds'] for r in rows), 'cost_usd': None,
        'protocol_errors': sum(r['protocol_errors'] for r in rows), 'no_actions': sum(r['no_actions'] for r in rows),
        'rows': rows}
    dump(root / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'run', 'report'))
    parser.add_argument('--root', type=Path, default=ROOT / 'regression_20260903')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--run-id')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--pause-after-calls', type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == 'prepare':
        manifest = prepare(root)
        print(json.dumps({'prepared': True, 'runs': len(manifest['schedule']), 'root': str(root)}, ensure_ascii=False))
        return
    manifest = verify(root)
    if args.command == 'report':
        print(json.dumps(report(root, manifest), ensure_ascii=False))
        return
    entries = [e for e in manifest['schedule'] if not args.run_id or e['run_id'] == args.run_id]
    if args.run_id and not entries:
        raise ValueError('Unknown frozen run_id')
    if args.resume and not args.run_id:
        raise ValueError('--resume requires a single explicit --run-id')
    started = time.time()
    failures, stop = 0, False
    # Dispatch bounded waves so a gate failure stops new work. An already-started
    # peer finishes and is retained; all remaining entries are explicitly logged.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for offset in range(0, len(entries), args.workers):
            if time.time() - started >= manifest['batch_deadline_seconds']:
                stop = True
            if stop:
                dump(root / 'stopped.json', {'at': utc(), 'reason': 'predeclared_stop_gate',
                    'not_started': [e['run_id'] for e in entries[offset:]]})
                break
            wave = entries[offset:offset + args.workers]
            futures = [pool.submit(TeamRun(root, entry, manifest).run, resume=args.resume,
                pause_after_calls=args.pause_after_calls) for entry in wave]
            for future in as_completed(futures):
                result = future.result()
                print('RUN_FINISHED ' + json.dumps({key: result.get(key) for key in
                    ('run_id', 'status', 'score', 'handoff_validated', 'call_count', 'total_tokens', 'wall_seconds')}, ensure_ascii=False), flush=True)
                failures = failures + 1 if not result.get('handoff_validated') else 0
                if result['status'] in ('infrastructure_error', 'grader_error') or failures >= 2:
                    stop = True
            report(root, manifest)
    dump(root / 'batch_timing.json', {'started_epoch': started, 'completed_at': utc(), 'wall_seconds': time.time() - started})


if __name__ == '__main__':
    main()
