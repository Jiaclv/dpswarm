"""Read-only progress, excluding secrets and candidate/code contents."""
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
    records = [json.loads(p.read_text(encoding='utf-8')) for p in (folder / 'calls').glob('*/metadata.json')]
    events = []
    for line in (folder / 'events.jsonl').read_text(encoding='utf-8').splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass  # A live append may be incomplete; never edit its journal.
    path = folder / 'result.json'
    result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    rows.append({'run': folder.name, 'calls': len(records),
        'tokens': sum(r.get('total_tokens') or 0 for r in records),
        'unknown_usage': sum(r.get('total_tokens') is None for r in records),
        'models': {m: sum(r['model_requested'] == m for r in records) for m in sorted({r['model_requested'] for r in records})},
        'protocol_errors': sum(bool(r.get('protocol_error')) for r in records),
        'transport_errors': [r.get('error') for r in records if r.get('error')],
        'phase': events[-1]['event'] if events else None,
        'workers': len(list(folder.glob('worker-*'))),
        'finished': result is not None,
        'resolved': (result.get('score') or {}).get('resolved') if result else None,
        'infrastructure_error': result.get('infrastructure_error') if result else None})
print(json.dumps({'finished': sum(r['finished'] for r in rows),
                  'calls': sum(r['calls'] for r in rows), 'tokens': sum(r['tokens'] for r in rows),
                  'resolved': sum(r['resolved'] is True for r in rows),
                  'runs': [r for r in rows if not r['finished']] if args.active_only else rows}, ensure_ascii=False))
