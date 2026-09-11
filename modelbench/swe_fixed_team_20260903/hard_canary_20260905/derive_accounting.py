"""Read this completed run and write new per-call accounting artifacts only."""
from collections import Counter
from datetime import datetime
from pathlib import Path
import csv
import hashlib
import json

BATCH = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    manifest = read(BATCH / 'manifest.json')
    assert len(manifest['schedule']) == 1
    run = BATCH / 'results' / manifest['schedule'][0]['run_id']
    result = read(run / 'result.json')
    audit = read(BATCH / 'audit.json')
    assert audit['status'] == 'PASS' and audit['audited_runs'] == 1
    records = [json.loads(line) for line in (run / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    completed = sorted((row for row in records if row['event'] == 'completed'), key=lambda x: x['started_at'])
    started = [row for row in records if row['event'] == 'started']
    assert len(completed) == len(started) == len({x['call_id'] for x in completed}) == 31
    assert {x['call_id'] for x in started} == {x['call_id'] for x in completed}
    actors = {call: actor for actor, usage in result['agent_usage'].items() for call in usage['call_ids']}
    rows = []
    for i, call in enumerate(completed, 1):
        row = {k: call.get(k) for k in (
            'call_id','model_requested','model_reported','role','effort_requested','effort_reported',
            'service_tier_requested','service_tier_reported','adapter_mode','started_at','completed_at',
            'wall_seconds','input_tokens','cached_input_tokens','output_tokens','reasoning_tokens',
            'total_tokens','usage_source','transport_attempt_count','max_tokens_requested','cap_enforced',
            'retry_attempted','reconnect_detected','error')}
        row.update(sequence=i, actor=actors.get(call['call_id'], 'cm'))
        assert row['actor'] != 'cm' or call['role'] == 'cm'
        rows.append(row)
    assert sum(r['total_tokens'] for r in rows) == result['budget']['total_tokens'] == audit['known_total_tokens']
    path = BATCH / 'call-accounting.csv'
    with path.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    roles = []
    for actor in ('lead','worker-1','worker-2','cm'):
        selected = [r for r in rows if r['actor'] == actor]
        entry = {'actor':actor,'model_requested':selected[0]['model_requested'],'calls':len(selected),
                 'sum_call_wall_seconds':round(sum(r['wall_seconds'] for r in selected),3),
                 'unknown_fields':{}}
        for key in ('input_tokens','cached_input_tokens','output_tokens','reasoning_tokens','total_tokens'):
            entry[key] = sum(r[key] for r in selected) if all(r[key] is not None for r in selected) else None
            entry['unknown_fields'][key] = sum(r[key] is None for r in selected)
        roles.append(entry)
    summary = {'run_id':result['run_id'],'official_completed':result['score']['completed'],
        'official_resolved':result['score']['resolved'],'team_execution_valid':result['team_execution_valid'],
        'started_at':result['started_at'],'completed_at':result['completed_at'],
        'run_wall_seconds':result['wall_seconds'],'candidate_wall_seconds':result['inference_wall_seconds'],
        'grading_and_finalization_seconds':round(result['wall_seconds']-result['inference_wall_seconds'],3),
        'all_call_records':len(rows),'work_calls':result['call_count'],'cm_calls':result['cm_call_count'],
        'known_total_tokens':sum(r['total_tokens'] for r in rows),'budget':result['budget'],'roles':roles,
        'sum_call_wall_seconds_is_parallel_sum':True,
        'request_echo_unknown':{k:sum(r[k] is None for r in rows) for k in ('model_reported','effort_reported','service_tier_reported')},
        'cost_usd':None,'price_basis':None,'audit_status':audit['status'],
        'provenance':{str(p.relative_to(BATCH)):hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (BATCH/'manifest.json', BATCH/'audit.json',run/'result.json',run/'calls.jsonl')}}
    with (BATCH / 'experiment-accounting.json').open('x', encoding='utf-8') as stream:
        json.dump(summary, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
    print(json.dumps(summary,indent=2))


if __name__ == '__main__':
    main()
