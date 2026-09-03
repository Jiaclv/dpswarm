"""Freeze and execute the user's forced-team pilot without changing old runs."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time
from uuid import uuid4

from .runner import SweRun, LIMITS, MODELS, dump, utc
from .reporting import report
from ..swe_verified_20260903.environment import ensure_image, cleanup_image, image_name

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PRIOR = REPO / 'modelbench/swe_verified_20260903'
OFFICIAL = PRIOR / 'official'
ENV_SOURCE = 'modelbench/swe_verified_20260903/environment.py'
GATE_PATH = HERE / 'validation/gate_revision6.json'
INPUT_NAMES = ('versions.json', 'selection.json', 'selected_public.json', 'verified.parquet',
    'grader_controller.json', 'grader.Dockerfile', 'resource_limits.json',
    'grader/test_specs_provenance.json', 'grader/test_specs.json', 'grader/selected.json')
TASK_IDS = ['matplotlib__matplotlib-25122', 'sphinx-doc__sphinx-8035']

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def sources():
    files = list(HERE.glob('*.py')) + [HERE / 'PLAN.md'] + list((HERE / 'tests').rglob('*.py'))
    files += list(PRIOR.glob('*.py')) + list((PRIOR / 'tests').glob('*.py'))
    files += list((REPO / 'dpswarm-plugin/dpswarm').rglob('*.py'))
    files += [REPO / 'modelbench/team_runtime_v2/transport.py', REPO / 'modelbench/team_eval20260903/transports.py']
    return {p.relative_to(REPO).as_posix(): sha(p) for p in sorted(set(files))}

def input_artifacts():
    return {name: sha(OFFICIAL / name) for name in INPUT_NAMES}

def prepare(batch, only_instances=None, reverse_arms=False, only_arms=None):
    gate_path = GATE_PATH
    gate = read(gate_path)
    assert gate['status'] == 'PASS' and gate['runtime_sources'] == sources(), 'Validation missing or sources changed'
    old = PRIOR / 'pilot_v2'
    stop = read(old / 'USER_STOP.json')
    assert stop['all_started_runs_completed'] and stop['inflight_model_calls_at_stop'] == 0
    started = {read(p)['instance_id'] for p in (old / 'results').glob('*/result.json')}
    public = read(OFFICIAL / 'selected_public.json')
    selected = [r for r in public if r['instance_id'] not in started][:2]
    if only_instances:
        # Revision-3 batches re-run a named instance on the new adapter; the
        # original task order rule does not apply to them.
        selected = [r for r in public if r['instance_id'] in set(only_instances)]
        assert [r['instance_id'] for r in selected] == list(only_instances), 'Requested instances absent from the frozen public selection'
    else:
        assert [r['instance_id'] for r in selected] == TASK_IDS
    assert all(set(r) <= {'instance_id', 'repo', 'base_commit', 'version', 'problem_statement'} for r in selected)
    versions = read(OFFICIAL / 'versions.json')
    assert sha(OFFICIAL / 'verified.parquet') == versions['dataset_sha256']
    arms = ['solo'] + ['fixed_' + model for model in MODELS]
    if only_arms:
        arms = [arm for arm in arms if arm in set(only_arms)]
        assert arms, 'No scheduled arm matches --only-arm'
    schedule = []
    for ordinal, instance in enumerate(selected):
        order = list(reversed(arms)) if (ordinal % 2 == 1) != reverse_arms else arms
        for arm in order:
            worker_model = None if arm == 'solo' else arm.removeprefix('fixed_')
            schedule.append({'run_id': instance['instance_id'] + '__' + arm, 'arm': arm,
                'condition': 'solo' if arm == 'solo' else 'fixed_team', 'worker_model': worker_model,
                'instance': instance, 'image': image_name(instance['instance_id'])})
    manifest = {'created_at': utc(), 'experiment': 'SWE fixed DERIVE teams across five worker models',
        'selection': {'selected': [{'instance_id': r['instance_id'], 'repo': r['repo']} for r in selected],
            'rule': ('Revision-3 rerun of named instances on the repaired adapter; complements pilot_v2, never rewrites it'
                     if only_instances else
                     'First two previously unstarted instances in the original metadata-frozen order; no answer/result-based replacement')},
        'versions': versions, 'official_directory': str(OFFICIAL),
        'validation_gate_path': str(gate_path), 'validation_gate_sha256': sha(gate_path),
        'runtime_sources': sources(), 'input_artifacts': input_artifacts(),
        'grader_controller': read(OFFICIAL / 'grader_controller.json'),
        'public_instances_sha256': sha(OFFICIAL / 'selected_public.json'),
        'lead_model': 'gpt-5.6-sol', 'candidate_worker_models': MODELS, 'arms': arms,
        'conditions': ['solo', 'fixed_team'], 'schedule': schedule,
        'limits': LIMITS, 'scheduled_runs': len(schedule), 'fixed_workers_per_team': 2,
        'worker_roles': ['production implementation', 'independent regression tests'],
        'activation_source': 'experiment_protocol; not a model decision',
        'mechanism_coverage': {'derive': 'fixed_team', 'split': 'not_exposed', 'fission': 'not_exposed',
                               'cm': 'integrated_on_demand'},
        'model_level_policy': 'Equal B admission labels, unranked; not AA or model strength measurements',
        'gpt_effort_requested': 'max', 'gpt_service_tier_requested': 'fast',
        'glm_effort_requested': 'max', 'glm_thinking_requested': True,
        'actual_settings': 'Only provider echoes are observed; absent values remain null',
        'maximum_calls': len(schedule) * LIMITS['max_calls'],
        'sum_token_admission_limits': len(schedule) * LIMITS['token_limit'],
        'max_parallel_runs': 2, 'batch_wall_limit_seconds': 10800,
        'grading': 'Unmodified official v4.1.0 run_instance after all candidate work ends and patch freezes',
        'prior_batch': {'path': str(old), 'manifest_sha256': sha(old / 'manifest.json'),
            'stop_sha256': sha(old / 'USER_STOP.json'), 'calls': stop['completed_calls'], 'tokens': stop['candidate_tokens']},
        'scope': 'Fixed derived worker team experiment; split/fission/CM/DSH bridge not exercised',
        'comparison': 'Two tasks with common roles and budget; not a model ranking or SOTA claim',
        'stop_policy': 'After each pair: infrastructure/grader failure, unknown/pending usage or absent required worker calls stops new pairs. Known empty patch and ordinary unresolved continue. STOP_AFTER_PAIR supports user-directed boundary stop.',
        'cost_usd': None, 'cost_note': 'No verified monetary charge source; record raw usage and timing'}
    for entry in schedule:
        entry['grader_contract'] = {'input_artifacts': manifest['input_artifacts'],
                                    'environment_sha': manifest['runtime_sources'][ENV_SOURCE]}
    batch.mkdir(parents=True, exist_ok=True)
    target = batch / 'manifest.json'
    if target.exists():
        previous = read(target)
        assert {k:v for k,v in previous.items() if k != 'created_at'} == {k:v for k,v in manifest.items() if k != 'created_at'}, 'Frozen batch differs'
        return previous
    dump(target, manifest)
    for relative, expected in manifest['runtime_sources'].items():
        dest = batch / 'source_snapshot' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, dest)
        assert sha(dest) == expected
    for relative, expected in manifest['input_artifacts'].items():
        dest = batch / 'inputs_snapshot' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(OFFICIAL / relative, dest)
        assert sha(dest) == expected
    snapshot = batch / 'validation_snapshot'
    snapshot.mkdir()
    shutil.copyfile(gate_path, snapshot / 'gate.json')
    for relative, expected in gate.get('validation_artifacts', {}).items():
        dest = snapshot / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / 'validation' / relative, dest)
        assert sha(dest) == expected
    report(batch)
    return manifest

def verify(batch):
    manifest = read(batch / 'manifest.json')
    assert sha(Path(manifest['validation_gate_path'])) == manifest['validation_gate_sha256'], 'Validation drift'
    assert sources() == manifest['runtime_sources'], 'Runtime drift; use a separate revision'
    assert input_artifacts() == manifest['input_artifacts'], 'Grader/input drift'
    return manifest

def run(batch):
    manifest = verify(batch)
    invocation = uuid4().hex
    started, clock = utc(), time.monotonic()
    stop = None
    dump(batch / 'invocations' / (invocation + '.started.json'), {'started_at': started, 'manifest_sha256': sha(batch / 'manifest.json')})
    previous = read(batch / 'batch.json') if (batch / 'batch.json').exists() else {}
    cumulative_before = previous.get('cumulative_invocation_wall_seconds', 0)
    schedule = manifest['schedule']
    for offset in range(0, len(schedule), 6):
        entries = schedule[offset:offset + 6]
        pending_any = any(not (batch / 'results' / e['run_id'] / 'result.json').exists() for e in entries)
        if not pending_any:
            continue
        if (batch / 'STOP_AFTER_PAIR').exists():
            stop = {'reason': 'user_boundary_stop', 'control_sha256': sha(batch / 'STOP_AFTER_PAIR')}
            break
        instance_id = entries[0]['instance']['instance_id']
        verify(batch)
        try:
            dump(batch / 'images' / (instance_id + '.json'), ensure_image(instance_id))
        except Exception as exc:
            stop = {'reason': 'image_error', 'instance_id': instance_id, 'error': str(exc)}
            break
        for start in range(0, 6, 2):
            verify(batch)
            if (batch / 'STOP_AFTER_PAIR').exists():
                stop = {'reason': 'user_boundary_stop', 'control_sha256': sha(batch / 'STOP_AFTER_PAIR')}
                break
            if cumulative_before + time.monotonic() - clock >= manifest['batch_wall_limit_seconds']:
                stop = {'reason': 'batch_admission_deadline'}
                break
            pending = []
            for entry in entries[start:start+2]:
                directory = batch / 'results' / entry['run_id']
                if (directory / 'result.json').exists():
                    continue
                if directory.exists():
                    stop = {'reason': 'unfinished_run_requires_reconciliation', 'run_id': entry['run_id']}
                    break
                pending.append(entry)
            if stop:
                break
            if not pending:
                continue
            print(json.dumps({'event': 'pair_started', 'at': utc(), 'instance_id': instance_id, 'run_ids': [e['run_id'] for e in pending]}), flush=True)
            results = []
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {}
                for entry in pending:
                    try:
                        futures[executor.submit(SweRun(batch, entry).run)] = entry
                    except Exception as exc:
                        stop = {'reason': 'runner_initialization_error', 'run_id': entry['run_id'], 'error': str(exc)}
                        break
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        result = future.result()
                        results.append(result)
                        report(batch)
                        print(json.dumps({'event': 'run_completed', 'run_id': result['run_id'],
                            'arm': entry['arm'], 'resolved': (result.get('score') or {}).get('resolved'),
                            'calls': result['call_count'], 'tokens': result['budget']['total_tokens'],
                            'actual_workers': result.get('workers_with_actual_calls'), 'team_execution_valid': result.get('team_execution_valid'),
                            'wall_seconds': result['wall_seconds']}), flush=True)
                    except Exception as exc:
                        stop = {'reason': 'uncaught_runtime_error', 'run_id': entry['run_id'], 'error': type(exc).__name__ + ': ' + str(exc)}
            for result in results:
                score, budget = result.get('score') or {}, result['budget']
                known_empty = score.get('failure_kind') == 'candidate_empty_patch' and score.get('resolved') is False
                no_workers = result['condition'] == 'fixed_team' and result.get('workers_with_actual_calls', 0) != 2
                if result.get('infrastructure_error') or (not score.get('completed') and not known_empty) or budget['unknown_call_count'] or budget['pending_call_count'] or no_workers:
                    stop = {'reason': 'predeclared_gate', 'run_id': result['run_id'],
                        'infrastructure_error': result.get('infrastructure_error'), 'score': score,
                        'unknown_calls': budget['unknown_call_count'], 'pending_calls': budget['pending_call_count'],
                        'worker_call_coverage_missing': no_workers}
            if stop:
                break
        try:
            dump(batch / 'cleanup' / (instance_id + '.' + invocation + '.json'), cleanup_image(instance_id))
        except Exception as exc:
            stop = stop or {'reason': 'image_cleanup_error', 'instance_id': instance_id, 'error': str(exc)}
        if stop:
            break
    summary = report(batch)
    record = {'invocation_id': invocation, 'started_at': started, 'completed_at': utc(),
        'wall_seconds': time.monotonic() - clock,
        'cumulative_invocation_wall_seconds': cumulative_before + time.monotonic() - clock,
        'stop': stop, 'completed_runs': summary['completed_runs'], 'scheduled_runs': len(schedule)}
    dump(batch / 'invocations' / (invocation + '.finished.json'), record)
    dump(batch / 'batch.json', record)
    print(json.dumps({'event': 'batch_finished', 'completed': summary['completed_runs'], 'stop': stop}), flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'run', 'report'])
    parser.add_argument('--batch', type=Path, default=HERE / 'pilot_v2')
    parser.add_argument('--only-instance', action='append', default=None,
                        help='Revision-3 rerun: restrict the batch to the named instance(s)')
    parser.add_argument('--only-arm', action='append', default=None,
                        help='Restrict the batch to the named arm(s), e.g. solo or fixed_gpt-5.6-sol')
    parser.add_argument('--reverse-arms', action='store_true',
                        help='Use the reversed arm order for every selected instance')
    args = parser.parse_args()
    batch = args.batch.resolve()
    if args.command == 'prepare':
        value = prepare(batch, only_instances=args.only_instance, reverse_arms=args.reverse_arms,
                        only_arms=args.only_arm)
        print(json.dumps({'batch': str(batch), 'runs': len(value['schedule']), 'manifest_sha256': sha(batch / 'manifest.json')}))
    elif args.command == 'run':
        run(batch)
    else:
        value = report(batch)
        print(json.dumps({'completed': value['completed_runs'], 'scheduled': value['scheduled_runs']}))

if __name__ == '__main__':
    main()
