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
    return {model: {'roles': {role: amount([r for r in records if r.get('model_requested') == model and r.get('role') == role])
                             for role in ('lead', 'worker', 'cm')},
                    'total': amount([r for r in records if r.get('model_requested') == model])} for model in MODELS}


def lines(path):
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b'\n'):
        raise ValueError(f'Incomplete journal for completed run: {path}')
    return [json.loads(line) for line in raw.splitlines()]


def worker_coverage(folder, records):
    events = lines(folder / 'events.jsonl')
    admitted = {e.get('handle', {}).get('node_id'): e for e in events if e.get('event') == 'worker_admitted'}
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
        node = bindings.get(record['call_id'], {}).get('node_id')
        if not node:
            unbound.append(record['call_id'])
        else:
            grouped[node].append(record)
    details = {node: {'worker_id': admitted.get(node, {}).get('worker_id'), **amount(grouped[node])}
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
        records = []
        for path in sorted((folder / 'calls').glob('*/metadata.json')):
            value = read(path)
            ident = value.get('call_id')
            if not isinstance(ident, str) or not ident.strip() or ident in seen:
                raise ValueError('Missing or duplicate call ID; no double billing report will be produced')
            if value.get('run_id') != entry['run_id'] or value.get('task_id') != result['instance_id']:
                raise ValueError('Call metadata identity mismatch')
            if value.get('role') == 'cm':
                if value.get('model_requested') != ((manifest.get('limits') or {}).get('cm_model') or 'glm-5.3-flash'):
                    raise ValueError('Unknown role/model in call metadata')
            elif value.get('model_requested') not in MODELS or value.get('role') not in ('lead', 'worker'):
                raise ValueError('Unknown role/model in call metadata')
            seen.add(ident)
            records.append(value)
        if result.get('call_count') + result.get('cm_call_count', 0) != len(records):
            raise ValueError('Result call count disagrees with available completed metadata')
        coverage = worker_coverage(folder, records)
        for field in ('workers_with_actual_calls', 'workers_with_call_records', 'workers_with_measured_usage'):
            if result.get(field) != coverage[field]:
                findings.append({'run_id': entry['run_id'], 'code': 'worker_coverage_disagreement', 'field': field})
        if coverage['unbound_worker_call_ids']:
            findings.append({'run_id': entry['run_id'], 'code': 'unbound_worker_call_ids', 'call_ids': coverage['unbound_worker_call_ids']})
        score = result.get('score') or {}
        graded = score.get('completed') is True
        row = {field: result.get(field) for field in ('run_id', 'arm', 'condition', 'instance_id', 'worker_model',
            'infrastructure_error', 'score', 'budget', 'patch_sha256', 'outcome', 'started_at', 'completed_at',
            'wall_seconds', 'inference_wall_seconds', 'activation_source', 'bootstrap_admitted',
            'team_execution_status', 'team_execution_valid', 'mechanism_coverage')}
        row.update(graded=graded, resolved=score.get('resolved') if graded else None,
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
            'expected_worker_instances_completed_runs': sum(r['condition'] in ('fixed_team', 'hetero_team') for r in selected) * 2,
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
        matrix[entry['instance']['instance_id']][entry['arm']] = {
            'run_id': entry['run_id'], 'completed': row is not None,
            'graded': row['graded'] if row else False, 'resolved': row['resolved'] if row else None,
            'team_execution_valid': row['team_execution_valid'] if row else None,
            'infrastructure_error': bool(row['infrastructure_error']) if row else None}
    summary = {'generated_at': datetime.now(timezone.utc).isoformat(), 'batch': str(batch),
        'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        'reporter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scheduled_runs': len(schedule), 'completed_runs': len(rows), 'arms': arms,
        'models': role_models(all_records), 'rows': rows, 'totals': amount(all_records), 'task_matrix': matrix,
        'findings': findings,
        'scope': {'completed_runs_only': True, 'raw_evidence_modified': False, 'model_calls_made': False,
            'input_includes_cached': True, 'output_includes_reasoning': True, 'parent_excludes_child_usage': True,
            'activation_source': 'experiment_protocol', 'autonomous_delegation_tested': False,
            'split': 'not_exposed', 'fission': 'not_exposed', 'CM': 'not_integrated', 'CM_usage': None,
            'DSH_bridge_exercised': False, 'leaderboard_or_SOTA_claim': False,
            'elapsed_note': 'sum_call_wall_seconds and sums of run duration overlap under concurrency; neither is batch elapsed'}}
    atomic_write(batch / 'summary.json', json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    import io
    output = io.StringIO(newline='')
    columns = ('run_id', 'arm', 'condition', 'instance_id', 'worker_model', 'graded', 'resolved',
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
        '固定条件由实验协议强制建立 Sol Lead + 两名同候选模型的真实 DERIVE worker；这不证明模型自主请求派生。',
        '仅两题，不能据此排列模型能力、宣称 SOTA、完整 DPswarm 效果或 SWE-bench Verified 榜单成绩。', '',
        '| Arm | 完成/计划 | 官方评分完成 | resolved | worker 有实际尝试/预期 | calls | total tokens | 累计调用秒 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, arm in arms.items():
        text.append(f"| {name} | {arm['completed_runs']}/{arm['scheduled_runs']} | {arm['graded']} | {arm['resolved']} | "
                    f"{arm['worker_instances_with_actual_calls']}/{arm['expected_worker_instances_completed_runs']} | "
                    f"{arm['calls']} | {arm['total_tokens']} | {arm['sum_call_wall_seconds']} |")
    text += ['', '| 任务 | ' + ' | '.join(arms) + ' |', '|---|' + '---|' * len(arms)]
    for task, cells in matrix.items():
        def verdict(cell):
            if not cell['completed']: return '未完成'
            if not cell['graded']: return '未完成官方评分'
            return '通过' if cell['resolved'] is True else '失败' if cell['resolved'] is False else '未知'
        text.append('| ' + task + ' | ' + ' | '.join(verdict(cells[arm]) for arm in arms) + ' |')
    text += ['', '| Model / role | Calls | Input 含 cache | Cache | Output 含 reasoning | Reasoning | Total | 累计调用秒 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for model, value in summary['models'].items():
        for role, counts in value['roles'].items():
            if counts['calls']:
                text.append(f'| {model} / {role} | ' + ' | '.join(str(counts[k]) if counts[k] is not None else 'null'
                    for k in ('calls', *FIELDS, 'sum_call_wall_seconds')) + ' |')
    text += ['', 'Calls 是包括失败在内的已完成调用记录数。实际尝试覆盖要求 transport_attempt_count > 0；本地尝试不等于服务端接受/推理完成。',
        'usage 和真实 model/effort/tier 缺失均保留 null，已知小计另列；cache/reasoning 已分别包含在 input/output，不重复相加。',
        'Lead 与 worker 单独归属，即使同用 Sol 也不合并角色。累计调用秒与累计运行秒会在并发下重叠，不能当作批次墙钟。',
        'worker 完成、Lead 采纳、队伍执行有效和官方 resolved 是不同指标。基础设施错误、预算、补丁 SHA 和原始 score 保留在 summary.json rows。',
        'CM 为 not_integrated，用量 null；SPLIT/FISSION 为 not_exposed。强制派生的使用不证明自主调度能力。',
        f'报告内部数据一致性提示：{len(findings)}；这不是完整证据审计，请另运行 validation/audit_results.py。',
        '本报告未调用模型/容器/评分器，未读取 gold patch，未改原始证据。']
    atomic_write(batch / 'REPORT.md', '\n'.join(text) + '\n')
    return summary
