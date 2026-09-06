"""Source-backed reports for the fixed-team SWE experiment; no runtime imports."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import os
import uuid

MODELS = ('glm-5.3', 'glm-5.3-flash', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'deepseek-v4-flash')
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')
ECHO_FIELDS = ('model_requested', 'model_reported', 'effort_requested', 'effort_reported',
               'service_tier_requested', 'service_tier_reported', 'adapter_mode')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def integer(value):
    return type(value) is int and value >= 0


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def epoch(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None


def amount(records):
    """Null propagates independently in each observed dimension."""
    result = {'calls': len(records), 'known_subtotals': {}, 'unknown_counts': {}, 'cost_usd': None}
    for key in FIELDS:
        known = [r[key] for r in records if integer(r.get(key))]
        result[key] = sum(known) if len(known) == len(records) else None
        result['known_subtotals'][key] = sum(known)
        result['unknown_counts'][key] = len(records) - len(known)
    attempts = [r['transport_attempt_count'] for r in records if integer(r.get('transport_attempt_count'))]
    result.update(transport_record_count=len(records),
        transport_attempt_count=sum(attempts) if len(attempts) == len(records) else None,
        transport_attempt_count_known_subtotal=sum(attempts),
        transport_attempt_count_unknown_records=len(records) - len(attempts),
        calls_with_transport_attempts=sum(integer(r.get('transport_attempt_count')) and r['transport_attempt_count'] > 0 for r in records),
        calls_with_measured_usage=sum(any(integer(r.get(k)) for k in FIELDS) for r in records),
        calls_with_complete_usage=sum(all(integer(r.get(k)) for k in ('input_tokens', 'output_tokens', 'total_tokens')) for r in records))
    walls = [r['wall_seconds'] for r in records if number(r.get('wall_seconds'))]
    result['sum_call_wall_seconds'] = sum(walls) if len(walls) == len(records) else None
    result['known_call_wall_seconds'] = sum(walls)
    result['unknown_call_wall_count'] = len(records) - len(walls)
    starts, ends = [epoch(r.get('started_at')) for r in records], [epoch(r.get('completed_at')) for r in records]
    result['call_window_start'] = min((r['started_at'] for r in records if epoch(r.get('started_at')) is not None), default=None)
    result['call_window_end'] = max((r['completed_at'] for r in records if epoch(r.get('completed_at')) is not None), default=None)
    result['call_window_seconds'] = max(ends) - min(starts) if records and None not in starts + ends else None
    result['transport_errors'] = sum(bool(r.get('error')) for r in records)
    result['protocol_errors'] = sum(bool(r.get('protocol_error')) for r in records)
    result['settings'] = {key: {'values': dict(Counter(str(r[key]) for r in records if r.get(key) is not None)),
                              'unknown_records': sum(r.get(key) is None for r in records)} for key in ECHO_FIELDS}
    return result


def role_models(records):
    models = dict.fromkeys((*MODELS, *(r.get('model_requested') for r in records
                                       if isinstance(r.get('model_requested'), str))))
    return {model: {'roles': {role: amount([r for r in records if r.get('model_requested') == model and r.get('role') == role])
                             for role in ('lead', 'worker', 'cm')},
                    'total': amount([r for r in records if r.get('model_requested') == model])} for model in models}


def expected_routes(entry, manifest):
    """Frozen declarations only; absent legacy lead/CM routes stay unknown."""
    condition = entry.get('condition')
    pool = entry.get('worker_models')
    if pool is not None and (not isinstance(pool, list) or any(not isinstance(m, str) or not m for m in pool)):
        raise ValueError('Invalid declared worker_models')
    if condition == 'hetero_team':
        if pool is None or len(pool) != 2 or len(set(pool)) != 2:
            raise ValueError('hetero_team requires two distinct ordered worker_models')
        if entry.get('worker_model') is not None:
            raise ValueError('hetero_team cannot declare a single worker_model')
    elif condition == 'solo':
        if pool or entry.get('worker_model') is not None:
            raise ValueError('solo cannot declare worker_models')
        pool = []
    elif condition == 'fixed_team':
        model = entry.get('worker_model')
        if pool is None and model:
            pool = [model] * 2  # The fixed-team protocol explicitly uses two workers.
        if pool is not None and (len(pool) != 2 or len(set(pool)) != 1 or (model and pool[0] != model)):
            raise ValueError('fixed_team worker_models disagree with worker_model')
    limits = {**(manifest.get('limits') or {}), **(entry.get('limits_override') or {}),
              **(entry.get('effective_limits') or {})}
    return {'lead_model': entry.get('lead_model') or manifest.get('lead_model'),
            'worker_models': pool, 'cm_model': limits.get('cm_model'), 'limits': limits}


def validate_result_routes(entry, result, manifest):
    routes = expected_routes(entry, manifest)
    routes['compatibility_warnings'] = []
    routes['worker_pool_semantics'] = 'ordered_worker_instances' if result.get('schema_version') == 12 else 'legacy_model_pool'
    for field, expected in (('lead_model', routes['lead_model']), ('worker_pool', routes['worker_models'])):
        if field in result and expected is not None and result[field] != expected:
            legacy = result.get('schema_version') is None
            if field == 'lead_model' and legacy and result[field] == 'gpt-5.6-sol' and entry.get('lead_model') == expected:
                routes['compatibility_warnings'].append('legacy_hardcoded_lead_model_label')
                continue  # Frozen serializers used Sol even for configured non-Sol solo runs.
            if field == 'worker_pool' and legacy and entry.get('condition') == 'fixed_team' and result[field] == [entry.get('worker_model')]:
                # Historical fixed-team pools listed distinct candidates, not
                # one entry per worker. Admission handles still verify both.
                continue
            if field == 'worker_pool' and legacy and entry.get('condition') == 'hetero_team' and result[field] == [None]:
                routes['compatibility_warnings'].append('legacy_worker_pool_missing_models')
                continue  # Known old serializer defect; never rewrite its raw value.
            raise ValueError(f'Result route identity mismatch: {entry["run_id"]}.{field}')
        if result.get('schema_version') == 12 and field not in result:
            raise ValueError(f'Result schema 12 missing {field}')
    if 'worker_models' in result and routes['worker_models'] is not None and result['worker_models'] != routes['worker_models']:
        raise ValueError('Result worker_models order disagrees with frozen declaration')
    return routes


def effective_mechanisms(entry, result, manifest):
    declared = manifest.get('mechanism_coverage') or {}
    observed = result.get('mechanism_coverage') or {}
    coverage = {name: observed.get(name, declared.get(name)) for name in ('derive', 'split', 'fission', 'cm')}
    if result.get('cm_status') is not None:
        coverage['cm'] = result['cm_status']
    elif 'cm' not in observed:
        limits = expected_routes(entry, manifest)['limits']
        if type(limits.get('cm_enabled')) is bool:
            coverage['cm'] = 'integrated_on_demand' if limits['cm_enabled'] else 'disabled'
    return coverage


def consensus(values):
    unique = list(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else 'mixed' if unique else None


DEATH_PHASE_SEMANTICS = 'observed_worktree_delivery_v1'
LEGACY_DEATH_PHASE_SEMANTICS = 'legacy_command_attempt_delivery'


def observed_lead_death_phase(worktree, *, artifact=None):
    # A saved nonempty patch is positive evidence even if the last probe failed.
    artifact = artifact or {}
    if artifact.get('status') == 'present' and type(artifact.get('bytes')) is int and artifact['bytes'] > 0:
        return 'edited'
    changed = (worktree or {}).get('observed_persisted_change')
    return 'edited' if changed is True else 'no_edit' if changed is False else 'unknown'


def observed_worker_death_phase(*, review_decision, status, delta_status, delta_bytes, worktree):
    """Delivery/adoption facts take precedence; absent evidence is never zero."""
    if review_decision == 'adopt':
        return 'adopted'
    nonempty = delta_status == 'present' and type(delta_bytes) is int and delta_bytes > 0
    if nonempty:
        return 'delivered_not_adopted' if status == 'completed' else 'edited_no_delivery'
    if status == 'completed' and (delta_status != 'present' or type(delta_bytes) is not int or delta_bytes < 0):
        return 'unknown'
    changed = (worktree or {}).get('observed_persisted_change')
    return 'edited_no_delivery' if changed is True else 'no_edit' if changed is False else 'unknown'


def death_phase_evidence(result):
    """Keep unmarked legacy phase values; validate only the declared new meaning."""
    marker = result.get('death_phase_semantics')
    workers = [w for w in result.get('workers') or [] if isinstance(w, dict)]
    findings = []
    if marker is None:
        semantics = LEGACY_DEATH_PHASE_SEMANTICS
    elif marker != DEATH_PHASE_SEMANTICS:
        semantics = 'unknown'
        findings.append('unsupported_death_phase_semantics')
    else:
        semantics = marker
        if result.get('lead_death_phase') != observed_lead_death_phase(result.get('lead_worktree'), artifact=result.get('lead_artifact')):
            findings.append('lead_death_phase_evidence_mismatch')
        if type(result.get('lead_edit_attempted')) is not bool:
            findings.append('lead_edit_attempted_missing_or_invalid')
        for worker in workers:
            if 'review_decision' not in worker or worker['review_decision'] not in (None, 'adopt', 'discard'):
                findings.append('worker_review_decision_missing_or_invalid')
            expected = observed_worker_death_phase(review_decision=worker.get('review_decision'),
                status=worker.get('status'), delta_status=worker.get('delta_status'),
                delta_bytes=worker.get('delta_bytes'), worktree=worker.get('worktree'))
            if worker.get('death_phase') != expected:
                findings.append('worker_death_phase_evidence_mismatch')
    return {'semantics': semantics, 'declared_semantics': marker,
            'lead_death_phase': result.get('lead_death_phase'),
            'worker_death_phases': {w.get('worker_id'): w.get('death_phase') for w in workers},
            'findings': findings}


def death_phase_groups(rows):
    groups = {}
    for row in rows:
        evidence = row['death_phase_evidence']
        group = groups.setdefault(evidence['semantics'], {'runs': 0, 'lead_phases': {}, 'worker_phases': {}})
        group['runs'] += 1
        for key, phases in (('lead_phases', [evidence['lead_death_phase']]),
                            ('worker_phases', evidence['worker_death_phases'].values())):
            for phase in phases:
                phase = phase or 'unknown'
                group[key][phase] = group[key].get(phase, 0) + 1
    return groups


def schema_groups(rows):
    groups = {}
    for row in rows:
        schema = row.get('schema_version')
        key = f'schema_{schema}' if type(schema) is int else 'legacy_unversioned'
        group = groups.setdefault(key, {'runs': 0, 'execution_health': {},
            'lead_observed_persisted_change_runs': 0, 'lead_observed_persisted_change_unknown_runs': 0})
        group['runs'] += 1
        status = (row.get('execution_health') or {}).get('status') if schema == 12 else None
        group['execution_health'][status or 'unknown'] = group['execution_health'].get(status or 'unknown', 0) + 1
        changed = (row.get('lead_worktree') or {}).get('observed_persisted_change') if schema == 12 else None
        group['lead_observed_persisted_change_runs'] += changed is True
        group['lead_observed_persisted_change_unknown_runs'] += type(changed) is not bool
    return groups


def lines(path):
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b'\n'):
        raise ValueError(f'Incomplete journal for completed run: {path}')
    return [json.loads(line) for line in raw.splitlines()]


def worker_coverage(folder, records, expected_models=None):
    events = lines(folder / 'events.jsonl')
    admitted = {e.get('handle', {}).get('node_id'): e for e in events if e.get('event') == 'worker_admitted'}
    for admission in admitted.values():
        worker_id = admission.get('worker_id')
        if expected_models is not None:
            expected_ids = {f'worker-{i + 1}': model for i, model in enumerate(expected_models)}
            if worker_id not in expected_ids or admission.get('handle', {}).get('model') != expected_ids[worker_id]:
                raise ValueError('worker admission model does not match ordered worker_models')
    bindings = {e.get('call_id'): e.get('handle', {}) for e in events if e.get('event') == 'call_reserved'}
    # The CP record is the authoritative attribution when present.
    for e in lines(folder / 'control-plane/ledger/execution.jsonl'):
        if e.get('kind') == 'call_recorded':
            p = e['payload']
            bindings[p['record']['call_id']] = p['handle']
    grouped = defaultdict(list)
    unbound = []
    for record in records:
        if record.get('role') != 'worker':
            continue
        handle = bindings.get(record['call_id'], {})
        node = handle.get('node_id')
        admitted_model = admitted.get(node, {}).get('handle', {}).get('model')
        bound_model = handle.get('model')
        for expected_model in (admitted_model, bound_model):
            if expected_model is not None and record.get('model_requested') != expected_model:
                raise ValueError('worker call model disagrees with its bound worker handle')
        if not node:
            unbound.append(record['call_id'])
        else:
            grouped[node].append(record)
    details = {node: {'worker_id': admitted.get(node, {}).get('worker_id'),
                      'model': admitted.get(node, {}).get('handle', {}).get('model'), **amount(grouped[node])}
               for node in set(admitted) | set(grouped)}
    return {'admitted_workers': len(admitted),
        'workers_with_call_records': sum(bool(v) for v in grouped.values()),
        'workers_with_actual_calls': sum(any(integer(r.get('transport_attempt_count')) and r['transport_attempt_count'] > 0 for r in v) for v in grouped.values()),
        'workers_with_unknown_attempt_records': sum(any(not integer(r.get('transport_attempt_count')) for r in v) for v in grouped.values()),
        'workers_with_measured_usage': sum(any(any(integer(r.get(k)) for k in FIELDS) for r in v) for v in grouped.values()),
        'unbound_worker_call_ids': unbound, 'agents': details,
        'actual_call_definition': 'A local transport attempt > 0; not proof of server acceptance or completed inference'}


def atomic_write(path, text):
    """Only derived report names are replaceable; original evidence is read-only."""
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8', newline='') as f:
            f.write(text)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def report(batch: Path) -> dict:
    batch = Path(batch).resolve()
    manifest_path = batch / 'manifest.json'
    manifest = read(manifest_path)
    schedule = manifest['schedule']
    ids = [e['run_id'] for e in schedule]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate schedule run IDs')
    completed = {p.parent.name for p in (batch / 'results').glob('*/result.json')}
    if completed - set(ids):
        raise ValueError('Completed result outside frozen schedule')
    rows, all_records, per_arm, seen = [], [], defaultdict(list), set()
    findings = []
    for entry in schedule:
        folder = batch / 'results' / entry['run_id']
        if folder.resolve().parent != (batch / 'results').resolve():
            raise ValueError('Run ID escapes result directory')
        if entry['run_id'] not in completed:
            continue
        result = read(folder / 'result.json')
        for field in ('run_id', 'arm', 'condition', 'worker_model'):
            if result.get(field) != entry.get(field):
                raise ValueError(f'Result identity mismatch: {entry["run_id"]}.{field}')
        if result['instance_id'] != entry['instance']['instance_id']:
            raise ValueError('Result task identity mismatch')
        routes = validate_result_routes(entry, result, manifest)
        findings.extend({'run_id': entry['run_id'], 'code': code, 'severity': 'warning'}
                        for code in routes['compatibility_warnings'])
        records = []
        for path in sorted((folder / 'calls').glob('*/metadata.json')):
            value = read(path)
            ident = value.get('call_id')
            if not isinstance(ident, str) or not ident.strip() or ident in seen:
                raise ValueError('Missing or duplicate call ID; no double billing report will be produced')
            if value.get('run_id') != entry['run_id'] or value.get('task_id') != result['instance_id']:
                raise ValueError('Call metadata identity mismatch')
            role, model = value.get('role'), value.get('model_requested')
            if role not in ('lead', 'worker', 'cm') or not isinstance(model, str) or not model:
                raise ValueError('Unknown role/model in call metadata')
            expected = routes['lead_model'] if role == 'lead' else routes['cm_model'] if role == 'cm' else None
            if expected is not None and model != expected:
                raise ValueError('Call model disagrees with declared role model')
            if role == 'worker' and routes['worker_models'] is not None and model not in routes['worker_models']:
                raise ValueError('worker model disagrees with declared worker_models')
            seen.add(ident)
            records.append(value)
        if result.get('call_count') + result.get('cm_call_count', 0) != len(records):
            raise ValueError('Result call count disagrees with available completed metadata')
        coverage = worker_coverage(folder, records, routes['worker_models'])
        for field in ('workers_with_actual_calls', 'workers_with_call_records', 'workers_with_measured_usage'):
            if result.get(field) != coverage[field]:
                findings.append({'run_id': entry['run_id'], 'code': 'worker_coverage_disagreement', 'field': field})
        if coverage['unbound_worker_call_ids']:
            findings.append({'run_id': entry['run_id'], 'code': 'unbound_worker_call_ids', 'call_ids': coverage['unbound_worker_call_ids']})
        phases = death_phase_evidence(result)
        findings.extend({'run_id': entry['run_id'], 'code': code} for code in phases['findings'])
        score = result.get('score') or {}
        graded = score.get('completed') is True
        row = {field: result.get(field) for field in ('run_id', 'arm', 'condition', 'instance_id', 'worker_model',
            'infrastructure_error', 'score', 'budget', 'patch_sha256', 'outcome', 'started_at', 'completed_at',
            'wall_seconds', 'inference_wall_seconds', 'activation_source', 'bootstrap_admitted',
            'team_execution_status', 'team_execution_valid', 'mechanism_coverage', 'schema_version',
            'lead_model', 'worker_pool', 'execution_health', 'lead_worktree')}
        row.update(death_phase_evidence=phases, declared_lead_model=routes['lead_model'], declared_worker_models=routes['worker_models'],
                   worker_pool_semantics=routes['worker_pool_semantics'],
                   observed_requested_lead_models=sorted({r['model_requested'] for r in records if r.get('role') == 'lead'}),
                   effective_mechanism_coverage=effective_mechanisms(entry, result, manifest),
                   graded=graded, resolved=score.get('resolved') if graded else None,
                   coverage=coverage, usage=amount(records), models=role_models(records),
                   result_path=str(folder / 'result.json'), result_sha256=hashlib.sha256((folder / 'result.json').read_bytes()).hexdigest())
        rows.append(row)
        per_arm[entry['arm']].extend(records)
        all_records.extend(records)
    arms = {}
    for arm in dict.fromkeys(e['arm'] for e in schedule):
        entries = [e for e in schedule if e['arm'] == arm]
        selected = [r for r in rows if r['arm'] == arm]
        counts = amount(per_arm[arm])
        arms[arm] = {'condition': entries[0]['condition'], 'worker_model': entries[0].get('worker_model'),
            'scheduled_runs': len(entries), 'completed_runs': len(selected),
            'graded': sum(r['graded'] for r in selected), 'resolved': sum(r['resolved'] is True for r in selected),
            'infrastructure_errors': sum(bool(r['infrastructure_error']) for r in selected),
            'team_execution_valid_runs': sum(r['team_execution_valid'] is True for r in selected),
            'expected_worker_instances_completed_runs': (sum(len(r['declared_worker_models']) for r in selected)
                if all(r['declared_worker_models'] is not None for r in selected) else None),
            'worker_model_configurations': [expected_routes(e, manifest)['worker_models'] for e in entries],
            'worker_instances_with_actual_calls': sum(r['coverage']['workers_with_actual_calls'] for r in selected),
            'worker_instances_with_unknown_attempt_records': sum(r['coverage']['workers_with_unknown_attempt_records'] for r in selected),
            'models': role_models(per_arm[arm]), **counts}
        for field in ('wall_seconds', 'inference_wall_seconds'):
            values = [r[field] for r in selected if number(r.get(field))]
            arms[arm]['sum_run_' + field] = sum(values) if len(values) == len(selected) else None
    matrix = {e['instance']['instance_id']: {} for e in schedule}
    by_run = {r['run_id']: r for r in rows}
    for entry in schedule:
        row = by_run.get(entry['run_id'])
        cell = matrix[entry['instance']['instance_id']].setdefault(entry['arm'], {'runs': []})
        cell['runs'].append({
            'run_id': entry['run_id'], 'completed': row is not None,
            'graded': row['graded'] if row else False, 'resolved': row['resolved'] if row else None,
            'team_execution_valid': row['team_execution_valid'] if row else None,
            'infrastructure_error': bool(row['infrastructure_error']) if row else None})
    summary = {'generated_at': datetime.now(timezone.utc).isoformat(), 'batch': str(batch),
        'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        'reporter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scheduled_runs': len(schedule), 'completed_runs': len(rows), 'task_count': len(matrix), 'arms': arms,
        'evidence_schema_groups': schema_groups(rows),
        'death_phase_evidence_groups': death_phase_groups(rows),
        'models': role_models(all_records), 'rows': rows, 'totals': amount(all_records), 'task_matrix': matrix,
        'findings': findings,
        'scope': {'completed_runs_only': True, 'raw_evidence_modified': False, 'model_calls_made': False,
            'input_includes_cached': True, 'output_includes_reasoning': True, 'parent_excludes_child_usage': True,
            'activation_source': consensus(r.get('activation_source') for r in rows),
            'autonomous_delegation_tested': False if rows and all(r.get('activation_source') == 'experiment_protocol' for r in rows) else None,
            'split': consensus(r['effective_mechanism_coverage']['split'] for r in rows),
            'fission': consensus(r['effective_mechanism_coverage']['fission'] for r in rows),
            'CM': consensus(r['effective_mechanism_coverage']['cm'] for r in rows),
            'CM_usage': amount([r for r in all_records if r.get('role') == 'cm']) if any(r.get('role') == 'cm' for r in all_records)
                        or (rows and all(r['effective_mechanism_coverage']['cm'] is not None for r in rows)) else None,
            'DSH_bridge_exercised': False, 'leaderboard_or_SOTA_claim': False,
            'elapsed_note': 'sum_call_wall_seconds and sums of run duration overlap under concurrency; neither is batch elapsed'}}
    atomic_write(batch / 'summary.json', json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    import io
    output = io.StringIO(newline='')
    columns = ('run_id', 'arm', 'condition', 'instance_id', 'worker_model', 'lead_model', 'worker_pool',
               'schema_version', 'execution_health', 'lead_worktree', 'death_phase_evidence', 'graded', 'resolved',
               'team_execution_valid', 'infrastructure_error', 'patch_sha256', 'wall_seconds', 'inference_wall_seconds',
               'calls', *FIELDS, 'transport_attempt_count', 'workers_with_actual_calls')
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        values = {**row, **row['usage'], 'workers_with_actual_calls': row['coverage']['workers_with_actual_calls']}
        writer.writerow({k: 'null' if values.get(k) is None else json.dumps(values[k], ensure_ascii=False)
                         if isinstance(values[k], (dict, list)) else values[k] for k in columns})
    atomic_write(batch / 'summary.csv', '\ufeff' + output.getvalue())
    text = ['# 固定 Agent Team SWE 探索性评测', '',
        f'已完成 {len(rows)}/{len(schedule)} run。下列结果仅覆盖已保存 result.json 的运行；未完成不记失败。',
        f'冻结计划覆盖 {len(matrix)} 道任务；每次运行的 Lead 与有序 worker 模型声明保留在 summary.json rows。',
        '已声明 Lead 模型：' + ', '.join(sorted({r['declared_lead_model'] for r in rows if r['declared_lead_model']})) + '；缺失声明保持未知。',
        '固定派生和异构方向按各 run 的冻结声明核对；协议建立团队不证明模型自主请求派生。',
        '这些任务上的探索性结果不能据此排列模型能力、宣称 SOTA、完整 DPswarm 效果或榜单成绩。', '',
        '| Arm | 完成/计划 | 官方评分完成 | resolved | worker 有实际尝试/预期 | calls | total tokens | 累计调用秒 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, arm in arms.items():
        text.append(f"| {name} | {arm['completed_runs']}/{arm['scheduled_runs']} | {arm['graded']} | {arm['resolved']} | "
                    f"{arm['worker_instances_with_actual_calls']}/{arm['expected_worker_instances_completed_runs']} | "
                    f"{arm['calls']} | {arm['total_tokens']} | {arm['sum_call_wall_seconds']} |")
    text += ['', '| 任务 | ' + ' | '.join(arms) + ' |', '|---|' + '---|' * len(arms)]
    for task, cells in matrix.items():
        def verdict(cell):
            runs = cell.get('runs', [cell] if 'resolved' in cell else [])
            if not any(run['completed'] for run in runs):
                return '未完成'
            graded = [run for run in runs if run['graded']]
            if not graded:
                return '未完成官方评分'
            if len(graded) == 1:
                value = graded[0]['resolved']
                return '通过' if value is True else '失败' if value is False else '未知'
            passed = sum(run['resolved'] is True for run in graded)
            return f'{passed}/{len(graded)} 通过'
        # 20260904C waves schedule some arms on only part of the instance set;
        # an arm with no cell on a task renders as unscheduled there.
        text.append('| ' + task + ' | ' + ' | '.join(
            verdict(cells[arm]) if arm in cells else '—' for arm in arms) + ' |')
    text += ['', '| Model / role | Calls | Input 含 cache | Cache | Output 含 reasoning | Reasoning | Total | 累计调用秒 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for model, value in summary['models'].items():
        for role, counts in value['roles'].items():
            if counts['calls']:
                text.append(f'| {model} / {role} | ' + ' | '.join(str(counts[k]) if counts[k] is not None else 'null'
                    for k in ('calls', *FIELDS, 'sum_call_wall_seconds')) + ' |')
    text += ['', 'Calls 是包括失败在内的已完成调用记录数。实际尝试覆盖要求 transport_attempt_count > 0；本地尝试不等于服务端接受/推理完成。',
        'usage 和真实 model/effort/tier 缺失均保留 null，已知小计另列；cache/reasoning 已分别包含在 input/output，不重复相加。',
        'Lead、worker、CM 分角色计量；相同模型也不合并角色。累计调用秒与累计运行秒会在并发下重叠，不能当作批次墙钟。',
        'worker 完成、Lead 采纳、队伍执行有效和官方 resolved 是不同指标。基础设施错误、预算、补丁 SHA 和原始 score 保留在 summary.json rows。',
        f"CM 配置状态：{summary['scope']['CM']}；用量从实际 CM 调用记录汇总。SPLIT/FISSION：{summary['scope']['split']}/{summary['scope']['fission']}。",
        '执行健康和真实工作区变化按 schema 分组，缺少字段的旧运行保留 unknown；旧 edit_detected 仅指命令尝试，不计为实际落地修改。',
        '失败阶段按 death_phase_semantics 分组；无标记旧结果保留命令尝试/交付语义，新标记依据实际观测、交付与采纳。缺失证据为 unknown。',
        f'报告内部数据一致性提示：{len(findings)}；这不是完整证据审计，请另运行 validation/audit_results.py。',
        '本报告未调用模型/容器/评分器，未读取 gold patch，未改原始证据。']
    atomic_write(batch / 'REPORT.md', '\n'.join(text) + '\n')
    return summary
