"""Read-only live status; no candidate content, credentials, or grader answers."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('batch', type=Path)
parser.add_argument('--active-only', action='store_true')
args = parser.parse_args()
rows = []
for folder in sorted((args.batch / 'results').glob('*')):
    if not folder.is_dir():
        continue
    events = []
    for line in (folder / 'events.jsonl').read_text(encoding='utf-8').splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    bindings = {e['call_id']: e['handle'] for e in events if e['event'] == 'call_reserved'}
    records = [json.loads(p.read_text(encoding='utf-8')) for p in (folder / 'calls').glob('*/metadata.json')]
    settled = {r['call_id'] for r in records}
    agents = {}
    for record in records:
        handle = bindings.get(record['call_id'], {})
        node = handle.get('node_id', 'unbound')
        agent = agents.setdefault(node, {'role': record['role'], 'model': record['model_requested'],
            'calls': 0, 'actual_attempt_calls': 0, 'known_tokens': 0, 'unknown_usage_calls': 0})
        agent['calls'] += 1
        attempt = record.get('transport_attempt_count')
        agent['actual_attempt_calls'] += type(attempt) is int and attempt > 0
        token = record.get('total_tokens')
        if type(token) is int:
            agent['known_tokens'] += token
        else:
            agent['unknown_usage_calls'] += 1
    path = folder / 'result.json'
    result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    rows.append({'run_id': folder.name, 'finished': result is not None,
        'phase': events[-1]['event'] if events else None,
        'calls': len(records), 'known_tokens': sum(a['known_tokens'] for a in agents.values()),
        'pending_calls': len(set(bindings) - settled),
        'actual_workers': sum(a['role'] == 'worker' and a['actual_attempt_calls'] > 0 for a in agents.values()),
        'agents': agents, 'resolved': (result.get('score') or {}).get('resolved') if result else None,
        'protocol_errors': sum(bool(r.get('protocol_error')) for r in records),
        'transport_errors': sum(bool(r.get('error')) for r in records)})
print(json.dumps({'completed_runs': sum(r['finished'] for r in rows),
    'calls': sum(r['calls'] for r in rows), 'known_tokens': sum(r['known_tokens'] for r in rows),
    'actual_workers': sum(r['actual_workers'] for r in rows),
    'pending_calls': sum(r['pending_calls'] for r in rows),
    'rows': [r for r in rows if not r['finished']] if args.active_only else rows}, ensure_ascii=False))
