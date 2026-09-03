"""Freeze and run a paired, bounded SWE Verified pilot; never replace failures."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import time

from .runner import SweRun, LIMITS, MODELS, dump, utc
from .environment import ensure_image, cleanup_image, image_name

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OFFICIAL = HERE / 'official'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sources():
    files = list(HERE.glob('*.py')) + [HERE / 'PLAN.md']
    files += list((HERE / 'tests').glob('*.py'))
    files += list((REPO / 'dpswarm-plugin/dpswarm').rglob('*.py'))
    files += [REPO / 'modelbench/team_runtime_v2/transport.py',
              REPO / 'modelbench/team_eval20260903/transports.py']
    return {p.relative_to(REPO).as_posix(): sha(p) for p in sorted(set(files))}


def input_artifacts():
    names = ('versions.json', 'selection.json', 'selected_public.json', 'verified.parquet',
             'grader_controller.json', 'grader.Dockerfile', 'resource_limits.json',
             'grader/test_specs_provenance.json', 'grader/test_specs.json', 'grader/selected.json')
    return {name: sha(OFFICIAL / name) for name in names}


def prepare(batch):
    gate_path = HERE / 'validation/gate.json'
    gate = read(gate_path)
    assert gate.get('status') == 'PASS', 'SWE integration gate has not passed'
    assert gate.get('runtime_sources') == sources(), 'Sources changed after validation'
    selection = read(OFFICIAL / 'selection.json')
    public = read(OFFICIAL / 'selected_public.json')
    selected = [v['instance_id'] for v in selection['selected']]
    assert [v['instance_id'] for v in public] == selected and len(set(selected)) == 10
    assert all(set(v) <= {'instance_id', 'repo', 'base_commit', 'problem_statement', 'version'} for v in public)
    assert sha(OFFICIAL / 'verified.parquet') == selection['dataset_sha256']
    provenance = read(OFFICIAL / 'grader/test_specs_provenance.json')
    assert sha(OFFICIAL / 'grader/test_specs.json') == provenance['sha256']
    schedule = []
    for i, instance in enumerate(public):
        for condition in (('solo', 'dpswarm') if i % 2 == 0 else ('dpswarm', 'solo')):
            schedule.append({'run_id': instance['instance_id'] + '__' + condition,
                             'condition': condition, 'instance': instance, 'image': image_name(instance['instance_id'])})
    manifest = {'created_at': utc(), 'experiment': 'SWE Verified repo-balanced paired pilot',
        'scope': 'Independent experimental RunnerAdapter using real DPswarm CP; DSH bridge not exercised',
        'validation_gate_sha256': sha(gate_path),
        'versions': read(OFFICIAL / 'versions.json'), 'selection': selection,
        'input_artifacts': input_artifacts(), 'grader_controller': read(OFFICIAL / 'grader_controller.json'),
        'public_instances_sha256': sha(OFFICIAL / 'selected_public.json'),
        'runtime_sources': sources(), 'schedule': schedule, 'limits': LIMITS,
        'lead_model': 'gpt-5.6-sol', 'candidate_worker_models': MODELS,
        'conditions': ['solo', 'dpswarm'], 'nested_worker_delegation': False,
        'model_level_policy': 'Equal B admission labels, unranked; not measured AA scores',
        'gpt_effort_requested': 'max', 'gpt_service_tier_requested': 'fast',
        'glm_effort_requested': 'max', 'glm_thinking_requested': True,
        'actual_settings': 'Record provider echoes; missing echoes remain null',
        'max_parallel_runs': 2, 'maximum_calls': 20 * LIMITS['max_calls'],
        'sum_token_admission_limits': 20 * LIMITS['token_limit'],
        'batch_wall_limit_seconds': 10800,
        'worker_scope': 'Independent repository copy at delegate-time patch; Lead explicitly adopts delta',
        'grading': 'Unmodified official v4.1.0 run_instance, fresh test container, only after final patch freeze',
        'grading_resources': '3 GiB / 2 CPU testbed plus <=768 MiB Linux controller; network off',
        'hidden_evidence': 'Gold patch/test_patch/test outcomes never enter candidate messages or tools',
        'stop_policy': 'After a pair finishes, stop new pairs on infrastructure/grader error or unknown/pending usage; confirmed candidate_empty_patch is an unresolved candidate failure and continues. Preserve all results. No replacement tasks or best-of reruns.',
        'comparison': '10 equal-weight repository strata, not an estimate of the 500-task population or a SOTA claim',
        'cost_usd': None, 'cost_note': 'No verified monetary charge/price source; exact observed tokens and time reported'}
    for entry in schedule:
        entry['grader_contract'] = {'input_artifacts': manifest['input_artifacts'],
            'environment_sha': manifest['runtime_sources']['modelbench/swe_verified_20260903/environment.py']}
    batch.mkdir(parents=True, exist_ok=True)
    target = batch / 'manifest.json'
    if target.exists():
        previous = read(target)
        assert {k: v for k, v in previous.items() if k != 'created_at'} == {k: v for k, v in manifest.items() if k != 'created_at'}, 'Frozen pilot changed'
        return previous
    dump(target, manifest)
    evidence = batch / 'validation_snapshot'
    evidence.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(gate_path, evidence / 'gate.json')
    for relative in gate.get('validation_artifacts', {}):
        destination = evidence / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / 'validation' / relative, destination)
    for name, expected in manifest['runtime_sources'].items():
        path = batch / 'source_snapshot' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, path)
        assert sha(path) == expected
    for name, expected in manifest['input_artifacts'].items():
        path = batch / 'inputs_snapshot' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(OFFICIAL / name, path)
        assert sha(path) == expected
    return manifest


def verify(batch):
    manifest = read(batch / 'manifest.json')
    assert sha(HERE / 'validation/gate.json') == manifest['validation_gate_sha256'], 'Validation evidence changed'
    assert sources() == manifest['runtime_sources'], 'Runtime changed since freeze; use a new version'
    assert sha(OFFICIAL / 'verified.parquet') == manifest['selection']['dataset_sha256'], 'Dataset drift'
    assert sha(OFFICIAL / 'selected_public.json') == manifest['public_instances_sha256'], 'Public task drift'
    assert input_artifacts() == manifest['input_artifacts'], 'Frozen environment or grader artifact drift'
    return manifest


def report(batch):
    manifest = read(batch / 'manifest.json')
    results = [read(path) for path in sorted((batch / 'results').glob('*/result.json'))]
    token_fields = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')
    rows, models = [], {model: {'calls': 0, 'known_total_tokens': 0, 'unknown_calls': 0, 'wall_seconds': 0.0,
        'known_subtotals': {key: 0 for key in token_fields}, 'unknown_counts': {key: 0 for key in token_fields},
        'model_echoes': 0, 'effort_echoes': 0, 'service_tier_echoes': 0} for model in MODELS}
    for result in results:
        score = result.get('score') or {}
        budget = result.get('budget') or {}
        rows.append({'instance_id': result['instance_id'], 'condition': result['condition'],
            'resolved': score.get('resolved'), 'graded': score.get('completed', False),
            'outcome': result.get('outcome', {}).get('status'), 'infrastructure_error': result.get('infrastructure_error'),
            'grader_error': score.get('infrastructure_error') or score.get('error'),
            'calls': result['call_count'], 'total_tokens': budget.get('total_tokens'),
            'known_tokens': budget.get('known_subtotal'), 'unknown_calls': budget.get('unknown_call_count'),
            'wall_seconds': result['wall_seconds'], 'inference_wall_seconds': result['inference_wall_seconds'],
            'delegations': result['delegations'], 'questions': result['questions'],
            'protocol_errors': result['protocol_errors'], 'patch_sha256': result['patch_sha256']})
        for path in (batch / 'results' / result['run_id'] / 'calls').glob('*/metadata.json'):
            record = read(path)
            if record.get('model_requested') not in models:
                continue
            aggregate = models[record['model_requested']]
            aggregate['calls'] += 1
            aggregate['known_total_tokens'] += record.get('total_tokens') or 0
            aggregate['unknown_calls'] += record.get('total_tokens') is None
            aggregate['wall_seconds'] += record.get('wall_seconds') or 0
            for key in token_fields:
                aggregate['known_subtotals'][key] += record.get(key) or 0
                aggregate['unknown_counts'][key] += record.get(key) is None
            for field, target in (('model_reported', 'model_echoes'), ('effort_reported', 'effort_echoes'),
                                  ('service_tier_reported', 'service_tier_echoes')):
                aggregate[target] += record.get(field) is not None
    for aggregate in models.values():
        for key in token_fields:
            aggregate[key] = None if aggregate['unknown_counts'][key] else aggregate['known_subtotals'][key]
    if rows:
        with (batch / 'summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    by_condition = {}
    for condition in ('solo', 'dpswarm'):
        arm = [row for row in rows if row['condition'] == condition]
        by_condition[condition] = {'completed_runs': len(arm), 'graded': sum(row['graded'] for row in arm),
            'resolved': sum(row['resolved'] is True for row in arm),
            'known_tokens': sum(row['known_tokens'] or 0 for row in arm),
            'unknown_calls': sum(row['unknown_calls'] or 0 for row in arm),
            'calls': sum(row['calls'] for row in arm), 'delegations': sum(row['delegations'] for row in arm),
            'sum_run_wall_seconds': sum(row['wall_seconds'] for row in arm)}
    pairs = []
    for selected in manifest['selection']['selected']:
        selected_rows = {row['condition']: row for row in rows if row['instance_id'] == selected['instance_id']}
        if len(selected_rows) == 2:
            a, b = selected_rows['solo'], selected_rows['dpswarm']
            pairs.append({'instance_id': selected['instance_id'], 'solo_resolved': a['resolved'], 'dpswarm_resolved': b['resolved'],
                'team_rescue': a['resolved'] is False and b['resolved'] is True,
                'team_harm': a['resolved'] is True and b['resolved'] is False})
    summary = {'generated_at': utc(), 'scheduled_runs': 20, 'completed_runs': len(rows),
               'conditions': by_condition, 'models': models, 'pairs': pairs, 'rows': rows,
               'sota_claim': False, 'official_leaderboard_submission': False}
    dump(batch / 'summary.json', summary)
    lines = ['# SWE-bench Verified pilot', '',
        f"Completed: {len(rows)}/20 runs. This is a 10-repository pilot, not a full leaderboard score.", '',
        '| Condition | Runs | Graded | Resolved | Calls | Known tokens | Unknown calls | Delegations |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for condition, values in by_condition.items():
        lines.append('| ' + condition + ' | ' + ' | '.join(str(values[key]) for key in
            ('completed_runs', 'graded', 'resolved', 'calls', 'known_tokens', 'unknown_calls', 'delegations')) + ' |')
    lines += ['', '| Instance | Condition | Resolved | Calls | Tokens | Seconds | Workers |', '|---|---|---|---:|---:|---:|---:|']
    for row in rows:
        lines.append(f"| {row['instance_id']} | {row['condition']} | {row['resolved']} | {row['calls']} | {row['total_tokens']} | {row['wall_seconds']:.1f} | {row['delegations']} |")
    lines += ['', 'Raw per-call requests, responses, usage, timing and settings are under results/*/calls; tools/history/CP evidence are in each run.',
              'Worker model rows with zero calls mean not selected, not a tested result. Monetary cost is unknown. Inference and grading time are separate.',
              'DSH integration was not exercised. This experimental adapter uses a separate CP tree for each run and explicit worker patch adoption.']
    (batch / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return summary


def run(batch):
    manifest = verify(batch)
    started, start_clock = utc(), time.monotonic()
    stop = None
    schedule = manifest['schedule']
    for index in range(0, len(schedule), 2):
        verify(batch)
        pair = schedule[index:index + 2]
        if time.monotonic() - start_clock >= manifest['batch_wall_limit_seconds']:
            stop = {'reason': 'batch_wall_limit', 'next_pair': index // 2}
            break
        pending = []
        for entry in pair:
            folder = batch / 'results' / entry['run_id']
            if (folder / 'result.json').exists():
                continue
            if folder.exists():
                raise RuntimeError('Unfinished run needs explicit reconciliation; not replaying: ' + entry['run_id'])
            pending.append(entry)
        if not pending:
            continue
        instance_id = pair[0]['instance']['instance_id']
        try:
            image = ensure_image(instance_id)
            dump(batch / 'images' / (instance_id + '.json'), image)
        except Exception as exc:
            stop = {'reason': 'image_error', 'instance_id': instance_id, 'error': str(exc)}
            break
        print(json.dumps({'event': 'pair_started', 'at': utc(), 'instance_id': instance_id,
                          'run_ids': [e['run_id'] for e in pending]}), flush=True)
        pair_results = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(SweRun(batch, entry).run): entry for entry in pending}
            for future in as_completed(futures):
                result = future.result()
                pair_results.append(result)
                report(batch)
                print(json.dumps({'event': 'run_completed', 'run_id': result['run_id'],
                    'resolved': (result.get('score') or {}).get('resolved'),
                    'calls': result['call_count'], 'tokens': result['budget']['total_tokens'],
                    'workers': result['delegations'], 'wall_seconds': result['wall_seconds']}), flush=True)
        dump(batch / 'cleanup' / (instance_id + '.json'), cleanup_image(instance_id))
        for result in pair_results:
            score, budget = result.get('score') or {}, result['budget']
            candidate_empty = score.get('failure_kind') == 'candidate_empty_patch' and score.get('resolved') is False
            if result.get('infrastructure_error') or (not score.get('completed') and not candidate_empty) or budget['unknown_call_count'] or budget['pending_call_count']:
                stop = {'reason': 'predeclared_gate', 'run_id': result['run_id'],
                        'infrastructure_error': result.get('infrastructure_error'), 'score': score,
                        'unknown_calls': budget['unknown_call_count'], 'pending_calls': budget['pending_call_count']}
        if stop:
            break
    summary = report(batch)
    dump(batch / 'batch.json', {'started_at': started, 'completed_at': utc(),
                              'wall_seconds': time.monotonic() - start_clock, 'stop': stop,
                              'completed_runs': summary['completed_runs'], 'scheduled_runs': 20})
    print(json.dumps({'event': 'batch_finished', 'completed': summary['completed_runs'], 'stop': stop}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'run', 'report'])
    parser.add_argument('--batch', type=Path, default=HERE / 'pilot_v1')
    args = parser.parse_args()
    batch = args.batch.resolve()
    if args.command == 'prepare':
        manifest = prepare(batch)
        print(json.dumps({'frozen': str(batch), 'runs': len(manifest['schedule']), 'sources': len(manifest['runtime_sources'])}))
    elif args.command == 'run':
        run(batch)
    else:
        summary = report(batch)
        print(json.dumps({'completed': summary['completed_runs'], 'conditions': summary['conditions']}))


if __name__ == '__main__':
    main()
