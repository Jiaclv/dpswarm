"""Read-only derivation from immutable per-call and per-run regression evidence."""
import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path


def nullable_sum(values):
    return sum(values) if all(v is not None for v in values) else None


def aggregate(records):
    tokens = [v.get('total_tokens') for v in records]
    return {'calls': len(records), 'total_tokens': nullable_sum(tokens),
            'known_token_subtotal': sum(v for v in tokens if v is not None),
            'unknown_total_calls': sum(v is None for v in tokens),
            'input_tokens': nullable_sum([v.get('input_tokens') for v in records]),
            'cached_input_tokens': nullable_sum([v.get('cached_input_tokens') for v in records]),
            'output_tokens': nullable_sum([v.get('output_tokens') for v in records]),
            'reasoning_tokens': nullable_sum([v.get('reasoning_tokens') for v in records]),
            'model_wall_seconds': sum(v.get('wall_seconds') or 0 for v in records),
            'transport_errors': sum(bool(v.get('error')) for v in records), 'cost_usd': None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    results = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((root / 'results').glob('*/result.json'))]
    records = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((root / 'calls').glob('*/metadata.json'))]
    indexed = {c['call_id']: c for c in records}
    assigned_ids = [cid for r in results for cid in r['call_ids']]
    if len(assigned_ids) != len(set(assigned_ids)):
        raise ValueError('A call ID is charged to more than one run')
    orphaned = sorted(set(indexed) - set(assigned_ids))
    groups, role_groups = defaultdict(list), defaultdict(list)
    for record in records:
        groups[record['model_requested']].append(record)
        role_groups[record['role']].append(record)
    run_rows, phase_rows = [], []
    for result in results:
        folder = root / 'results' / result['run_id']
        first_path = folder / 'first_attestation.json'
        first = json.loads(first_path.read_text(encoding='utf-8')) if first_path.exists() else {}
        sandbox_path = folder / 'sandbox' / 'containers.json'
        sandbox_state = json.loads(sandbox_path.read_text(encoding='utf-8')) if sandbox_path.exists() else {}
        end_to_end = None
        if sandbox_state.get('closed') and sandbox_state.get('updated_at'):
            end_to_end = (datetime.fromisoformat(sandbox_state['updated_at']) - datetime.fromisoformat(result['started_at'])).total_seconds()
        handoff_attempts, handoff_rejections, malformed = 0, [], []
        for step_file in sorted((folder / 'phases' / 'planner').glob('turn_*.json')):
            step = json.loads(step_file.read_text(encoding='utf-8'))
            for tool in step.get('tool_results', []):
                if tool['call']['name'] == 'submit_handoff':
                    handoff_attempts += 1
                    if not tool['result'].get('ok'):
                        handoff_rejections.append(tool['result'])
        for phase in result['phases']:
            phase_records = [indexed[cid] for cid in phase['call_ids']]
            phase_rows.append({'run_id': result['run_id'], 'role': phase['role'], 'phase': phase['phase'],
                'status': phase['status'], 'protocol_errors': phase['protocol_errors'], 'no_actions': phase['no_actions'],
                'phase_wall_seconds': phase['wall_seconds'], **aggregate(phase_records)})
        run_rows.append({'run_id': result['run_id'], 'executor': result['executor'], 'task': result['task_id'],
            'status': result['status'],
            'grader_pass': (result.get('score') or {}).get('pass'),
            'control_accepted': result.get('control', {}).get('control_accepted'),
            'pass': (result.get('score') or {}).get('pass') is True and result.get('control', {}).get('control_accepted') is True,
            'first_verifier_pass': first.get('verdict') == 'pass', 'handoff_validated': result['handoff_validated'],
            'first_verifier_attestation_missing_or_invalid': 'error' in first or 'invalid' in first,
            'executor_calls': sum(indexed[cid]['role'] == 'executor' for cid in result['call_ids']),
            'handoff_attempts': handoff_attempts, 'handoff_rejections': handoff_rejections,
            'run_wall_seconds': result['wall_seconds'], 'clarifications': result.get('clarifications', {}).get('requests', {}),
            'end_to_end_wall_seconds': end_to_end,
            'end_to_end_time_source': 'sandbox.closed.updated_at minus run.started_at (UTC); includes CP finalization and container cleanup',
            **aggregate([indexed[cid] for cid in result['call_ids']])})
    stats = {'scheduled_runs': len(manifest['schedule']), 'completed_runs': len(results),
        'all_calls': aggregate(records), 'orphaned_or_running_call_ids': orphaned,
        'by_model': {k: aggregate(v) for k, v in groups.items()},
        'by_role': {k: aggregate(v) for k, v in role_groups.items()}, 'runs': run_rows, 'phases': phase_rows,
        'phase_protocol_errors': sum(p['protocol_errors'] for p in phase_rows),
        'phase_no_actions': sum(p['no_actions'] for p in phase_rows),
        'phase_budget_exhaustions': sum(p['status'] == 'phase_budget_exhausted' for p in phase_rows),
        'handoff_blocked_runs': sum(not r['handoff_validated'] for r in run_rows),
        'handoff_rejected_attempts': sum(len(r['handoff_rejections']) for r in run_rows),
        'clarification_requests': sum(len(r['clarifications']) for r in run_rows),
        'reported_model_values': sorted({str(r.get('model_reported')) for r in records}),
        'reported_effort_values': sorted({str(r.get('effort_reported')) for r in records}),
        'reported_service_tier_values': sorted({str(r.get('service_tier_reported')) for r in records})}
    (root / 'analysis.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    def number(value):
        return '未知' if value is None else f'{value:,}'
    lines = ['# 修复后真实回归结果', '',
        f"完成 {len(results)}/{len(manifest['schedule'])} 轮；最终通过 {sum(r['pass'] is True for r in run_rows)} 轮。",
        '初次 V 通过仅为 verifier attestation；最终通过来自原隔离 grader 和 CP 证据验收。', '',
        '| Executor | 任务 | 最终通过 | 初次 V 通过 | 调用数 | 整队 token | 含清理秒数 |',
        '|---|---|---|---|---:|---:|---:|']
    for row in run_rows:
        elapsed = '未知' if row['end_to_end_wall_seconds'] is None else f"{row['end_to_end_wall_seconds']:.2f}"
        lines.append(f"| {row['executor']} | {row['task']} | {row['pass']} | {row['first_verifier_pass']} | {row['calls']} | {number(row['total_tokens'])} | {elapsed} |")
    lines += ['', '| 模型（跨角色） | 调用数 | 输入 token | 缓存输入 token | 输出 token | 总 token | 模型耗时秒数合计 |',
              '|---|---:|---:|---:|---:|---:|---:|']
    for model, row in stats['by_model'].items():
        lines.append(f"| {model} | {row['calls']} | {number(row['input_tokens'])} | {number(row['cached_input_tokens'])} | {number(row['output_tokens'])} | {number(row['total_tokens'])} | {row['model_wall_seconds']:.2f} |")
    lines += ['', f"协议错误 {stats['phase_protocol_errors']}；NoAction {stats['phase_no_actions']}；交接语义拒绝 {stats['handoff_rejected_attempts']} 次；真实澄清请求 {stats['clarification_requests']} 次。",
        '缓存输入是输入的子集，reasoning 是输出的子集，不重复加到总量。含清理秒数由运行开始与沙箱closed时间戳相减；原result.wall_seconds记录到grader返回，未包含随后控制面验收和清理。并行模型耗时合计不等于批次墙钟；批次时间见 batch_timing.json。费用未知。', '',
        '所有 GPT 请求 max + fast；GLM 请求 thinking enabled + max。请求值不等于服务端确认值；实际回显集合和逐阶段/逐调用计量见 analysis.json 与 calls/*/metadata.json。', '',
        '该结果评估原生工具、约束交接和有界调度的组合；未单独隔离因果。此次每轮上限20调用，旧主实验18调用只作历史参照。未运行的候选不作新的性能排名。', '',
        '如真实模型未触发澄清，其调度与恢复正确性仅由离线脚本化模型、真实 ControlPlane 的集成测试支持，不能声称已测得真实模型澄清成功率。']
    (root / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'completed': len(results), 'passes': sum(r['pass'] is True for r in run_rows),
                      'protocol_errors': stats['phase_protocol_errors'], **stats['all_calls']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
