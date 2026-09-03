"""Read-only timing evidence for completed fixed-team runs. No runtime imports.

Use --batch and a new --output-dir. A local transport wall time is not pure
model computation; admission-to-activation combines scheduling and setup.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

RUNNER_SOURCE = 'modelbench/swe_fixed_team_20260903/runner.py'
IDENTITY = ('run_id', 'node_id', 'item_id', 'session_id', 'attempt', 'context_epoch', 'model', 'role')
DEFINITIONS = {
    'admission_to_activation_seconds': 'Runtime worker_admitted.at to worker_activated.at: protocol staging/thread scheduling, container admission wait and environment fork/setup. NOT pure queue time.',
    'worker_start_to_activation_seconds': 'Delivery started_at (worker thread entered) to runtime activation: container admission wait plus environment fork/setup.',
    'sum_model_call_wall_seconds': 'Sum of metadata.wall_seconds for this exact agent identity, including failed/local transport records. NOT provider compute time and excludes time before transport.complete starts.',
    'delivery_wall_seconds': 'Recorded worker monotonic wall: worker thread entry through delivery preparation; includes slot wait, setup, tools and patch export, excludes later CP submission/failure recording and finally environment cleanup.',
    'first_call_offset_from_run_seconds': 'First call metadata.started_at minus result.started_at. This is a local call-record start, not an observed server start.',
    'first_known_attempt_offset_from_run_seconds': 'First metadata.started_at with transport_attempt_count > 0 minus run start; earlier unknown-attempt records may exist.',
    'activation_to_first_call_seconds': 'Activation to first metadata.started_at; includes prompt preparation and any later admission/model-slot waiting.',
    'inference_phase_wall_seconds': 'Recorded result.inference_wall_seconds: run initialization, agent work, worker waits and candidate/CP/environment cleanup before grader admission.',
    'grading_phase_wall_seconds': 'Result wall_seconds minus inference_wall_seconds when a score object exists. Includes grader resource admission, controller/test execution, cleanup and result bookkeeping; NOT pure test duration.',
    'inference_boundary_at': 'result.patch_frozen_at is assigned AFTER candidate cleanup and BEFORE grader resource acquisition in this frozen runner; distinct from runtime patch_frozen event.',
    'registered_to_cp_terminal_seconds': 'CP lead registration/worker reservation through that agent CP terminal event; not a sum of child time.',
    'unknowns': 'Pure queue, isolated environment setup, pure model compute and official test-only durations have no dedicated timing boundaries here and remain null.',
    'concurrency': 'Per-agent, per-run and call durations overlap under concurrency. These timings cannot rank model speed; do not add parent elapsed time to child elapsed time.',
}


def valid_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def moment(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (AttributeError, ValueError, OverflowError):
        return None


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def handle_key(handle):
    if not isinstance(handle, dict) or not isinstance(handle.get('node_id'), str):
        return None
    return (handle['node_id'], handle.get('session_id'), handle.get('attempt'), handle.get('context_epoch'))


class TimingReader:
    def __init__(self, batch):
        self.batch = Path(batch).resolve()
        self.evidence = []
        self.observations = []
        self.seen_calls = set()

    def read(self, path, *, jsonl=False, optional=False):
        path = Path(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if optional:
                return [] if jsonl else None
            raise
        self.evidence.append({'path': str(path), 'sha256_at_read': sha_bytes(raw), 'bytes_at_read': len(raw)})
        if not jsonl:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError(f'Expected JSON object: {path}')
            return value
        parts = raw.splitlines(keepends=True)
        if parts and not parts[-1].endswith(b'\n'):
            self.observations.append({'code': 'incomplete_append_tail_ignored', 'path': str(path)})
            parts.pop()
        rows = [json.loads(line) for line in parts]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f'Expected JSONL objects: {path}')
        return rows

    def duration(self, start, end, scope, field):
        a, b = moment(start), moment(end)
        if a is None or b is None:
            return None
        if b < a:
            self.observations.append({'code': 'negative_utc_interval', 'scope': scope, 'field': field})
            return None
        return round(b - a, 6)

    def run(self, entry):
        folder = self.batch / 'results' / entry['run_id']
        if folder.resolve().parent != (self.batch / 'results').resolve():
            raise ValueError('Run ID escapes results directory')
        result = self.read(folder / 'result.json')
        for field in ('run_id', 'arm', 'condition', 'worker_model'):
            if result.get(field) != entry.get(field):
                raise ValueError(f'Result identity mismatch: {entry["run_id"]}.{field}')
        if result.get('instance_id') != entry['instance']['instance_id']:
            raise ValueError('Result task mismatch')
        events = self.read(folder / 'events.jsonl', jsonl=True)
        rich = self.read(folder / 'control-plane/ledger/execution.jsonl', jsonl=True)
        handles, labels, parents, registered, admitted, activated, terminal = {}, {}, {}, {}, {}, {}, {}
        runtime_bindings, cp_bindings = {}, {}
        event_refs = {}

        def register(handle):
            key = handle_key(handle)
            if key is None:
                return None
            if key in handles and any(handles[key].get(k) != handle.get(k) for k in IDENTITY):
                raise ValueError('Conflicting agent handle identity')
            if handle.get('run_id') != entry['run_id'] or handle.get('role') not in ('lead', 'worker'):
                raise ValueError('Agent handle outside declared run/roles')
            handles[key] = handle
            return key

        for label, value in (result.get('agent_usage') or {}).items():
            key = register(value.get('handle'))
            if key:
                labels[key] = label
        deliveries = {}
        for value in result.get('workers', []):
            key = register(value.get('handle'))
            if key:
                deliveries[key] = value
                labels[key] = value.get('worker_id')
        for index, event in enumerate(rich, 1):
            kind, payload = event.get('kind'), event.get('payload') or {}
            key = register(payload.get('handle'))
            if key and kind in ('lead_registered', 'worker_reserved'):
                registered.setdefault(key, event.get('at'))
                if kind == 'worker_reserved':
                    parents[key] = payload.get('caller', {}).get('node_id')
            if key and kind in ('agent_submitted', 'agent_failed'):
                terminal.setdefault(key, event.get('at'))
            if key and kind == 'call_recorded':
                ident = payload.get('record', {}).get('call_id')
                if ident in cp_bindings:
                    raise ValueError('Duplicate CP call attribution')
                cp_bindings[ident] = key
            if kind in ('root_finished', 'run_closed_without_final'):
                for candidate, handle in handles.items():
                    if handle['role'] == 'lead':
                        terminal.setdefault(candidate, event.get('at'))
        for index, event in enumerate(events, 1):
            key = register(event.get('handle'))
            if event.get('event') == 'run_started':
                register(event.get('root_handle'))
            if key and event.get('event') in ('worker_admitted', 'worker_activated'):
                target = admitted if event['event'] == 'worker_admitted' else activated
                if key in target:
                    self.observations.append({'code': 'duplicate_lifecycle_event', 'run_id': entry['run_id'],
                                              'event': event['event'], 'node_id': key[0]})
                target.setdefault(key, event.get('at'))
                labels[key] = event.get('worker_id')
                event_refs.setdefault(key, {})[event['event']] = str(folder / 'events.jsonl') + ':' + str(index)
            if key and event.get('event') == 'call_reserved':
                ident = event.get('call_id')
                if ident in runtime_bindings:
                    raise ValueError('Duplicate runtime call attribution')
                runtime_bindings[ident] = key
        for ident in cp_bindings.keys() & runtime_bindings.keys():
            if cp_bindings[ident] != runtime_bindings[ident]:
                raise ValueError('CP and runtime disagree on call attribution')
        bindings = {**runtime_bindings, **cp_bindings}
        assigned = {key: [] for key in handles}
        records = []
        for path in sorted((folder / 'calls').glob('*/metadata.json')):
            record = self.read(path)
            ident = record.get('call_id')
            if not isinstance(ident, str) or not ident.strip() or ident in self.seen_calls:
                raise ValueError('Missing or duplicate call ID; refusing double-counted timing')
            self.seen_calls.add(ident)
            key = bindings.get(ident)
            if key not in assigned:
                raise ValueError('Call has no unique CP/runtime agent handle')
            handle = handles[key]
            if any(record.get(k) != v for k, v in (('run_id', entry['run_id']), ('task_id', result['instance_id']),
                       ('role', handle['role']), ('model_requested', handle['model']))):
                raise ValueError('Call metadata and handle disagree')
            assigned[key].append(record)
            records.append(record)
        if len(records) != result.get('call_count') or set(bindings) != {r['call_id'] for r in records}:
            raise ValueError('Completed run has missing metadata or pending/unattributed calls')
        common = {k: result.get(k) for k in ('run_id', 'arm', 'condition', 'instance_id', 'worker_model')}
        run_start = result.get('started_at')
        agent_rows = []
        for key, handle in handles.items():
            calls = assigned[key]
            starts_known = all(moment(r.get('started_at')) is not None for r in calls)
            first = min(calls, key=lambda r: moment(r['started_at'])) if calls and starts_known else None
            attempts = [r for r in calls if type(r.get('transport_attempt_count')) is int and r['transport_attempt_count'] > 0]
            attempt_starts_known = all(moment(r.get('started_at')) is not None for r in attempts)
            first_attempt = min(attempts, key=lambda r: moment(r['started_at'])) if attempts and attempt_starts_known else None
            walls = [r['wall_seconds'] for r in calls if valid_number(r.get('wall_seconds'))]
            delivery = deliveries.get(key, {})
            first_at = first.get('started_at') if first else None
            first_attempt_at = first_attempt.get('started_at') if first_attempt else None
            row = {**common, 'agent_id': labels.get(key) or ('lead' if handle['role'] == 'lead' else handle['node_id']),
                'node_id': handle['node_id'], 'item_id': handle.get('item_id'), 'session_id': handle.get('session_id'),
                'attempt': handle.get('attempt'), 'context_epoch': handle.get('context_epoch'),
                'parent_node_id': parents.get(key), 'role': handle['role'], 'model_requested': handle['model'],
                'model_reported_counts': dict(Counter(r['model_reported'] for r in calls if isinstance(r.get('model_reported'), str))),
                'unknown_model_reported_records': sum(r.get('model_reported') is None for r in calls),
                'activation_source': 'experiment_protocol' if key in admitted else None,
                'cp_registered_at': registered.get(key), 'admitted_at': admitted.get(key), 'activated_at': activated.get(key),
                'worker_thread_started_at': delivery.get('started_at'), 'delivery_completed_at': delivery.get('completed_at'),
                'cp_terminal_at': terminal.get(key),
                'status': delivery.get('status') if handle['role'] == 'worker' else (result.get('outcome') or {}).get('status'),
                'call_records': len(calls), 'call_ids': [r['call_id'] for r in calls],
                'calls_with_transport_attempts': len(attempts),
                'unknown_attempt_records': sum(not (type(r.get('transport_attempt_count')) is int
                                                   and r['transport_attempt_count'] >= 0) for r in calls),
                'first_call_id': first.get('call_id') if first else None, 'first_call_started_at': first_at,
                'first_known_attempt_call_id': first_attempt.get('call_id') if first_attempt else None,
                'first_known_attempt_record_started_at': first_attempt_at,
                'sum_model_call_wall_seconds': sum(walls) if len(walls) == len(calls) else None,
                'known_model_call_wall_subtotal_seconds': sum(walls), 'unknown_call_wall_records': len(calls) - len(walls),
                'delivery_wall_seconds': delivery.get('wall_seconds') if valid_number(delivery.get('wall_seconds')) else None,
                'admission_to_activation_scope': DEFINITIONS['admission_to_activation_seconds'],
                'model_call_wall_scope': DEFINITIONS['sum_model_call_wall_seconds'],
                'delivery_wall_scope': DEFINITIONS['delivery_wall_seconds'],
                'pure_queue_seconds': None, 'isolated_environment_setup_seconds': None, 'pure_model_compute_seconds': None,
                'lifecycle_event_evidence': event_refs.get(key, {}), 'result_path': str(folder / 'result.json')}
            for field, start, end in (
                ('admission_to_activation_seconds', admitted.get(key), activated.get(key)),
                ('worker_start_to_activation_seconds', delivery.get('started_at'), activated.get(key)),
                ('first_call_offset_from_run_seconds', run_start, first_at),
                ('first_known_attempt_offset_from_run_seconds', run_start, first_attempt_at),
                ('admission_to_first_call_seconds', admitted.get(key), first_at),
                ('activation_to_first_call_seconds', activated.get(key), first_at),
                ('delivery_utc_seconds', delivery.get('started_at'), delivery.get('completed_at')),
                ('activation_to_delivery_seconds', activated.get(key), delivery.get('completed_at')),
                ('registered_to_cp_terminal_seconds', registered.get(key), terminal.get(key)),
            ):
                row[field] = self.duration(start, end, entry['run_id'] + '/' + row['agent_id'], field)
            agent_rows.append(row)
        wall = result.get('wall_seconds') if valid_number(result.get('wall_seconds')) else None
        inference = result.get('inference_wall_seconds') if valid_number(result.get('inference_wall_seconds')) else None
        remainder = round(wall - inference, 6) if wall is not None and inference is not None and wall >= inference else None
        score = result.get('score')
        run_row = {**common, 'started_at': run_start, 'completed_at': result.get('completed_at'),
            'run_wall_seconds': wall, 'inference_phase_wall_seconds': inference,
            'run_wall_utc_seconds': self.duration(run_start, result.get('completed_at'), entry['run_id'], 'run_wall_utc_seconds'),
            'inference_boundary_at': result.get('patch_frozen_at'),
            'patch_export_event_at': next((e.get('at') for e in events if e.get('event') == 'patch_frozen'), None),
            'post_inference_wall_seconds': remainder,
            'post_inference_utc_seconds': self.duration(result.get('patch_frozen_at'), result.get('completed_at'), entry['run_id'], 'post_inference_utc_seconds'),
            'grading_phase_wall_seconds': remainder if isinstance(score, dict) else None,
            'grading_phase_scope': DEFINITIONS['grading_phase_wall_seconds'],
            'grading_phase_observed': isinstance(score, dict), 'official_test_only_seconds': None,
            'official_completed': score.get('completed') if isinstance(score, dict) else None,
            'official_resolved': score.get('resolved') if isinstance(score, dict) else None,
            'call_records': len(records), 'agent_count': len(agent_rows),
            'sum_model_call_wall_seconds': sum(r['wall_seconds'] for r in records) if all(valid_number(r.get('wall_seconds')) for r in records) else None,
            'result_path': str(folder / 'result.json')}
        return agent_rows, run_row

    def all(self):
        manifest = self.read(self.batch / 'manifest.json')
        source_path = self.batch / 'source_snapshot' / RUNNER_SOURCE
        raw_source = source_path.read_bytes()
        if sha_bytes(raw_source) != manifest.get('runtime_sources', {}).get(RUNNER_SOURCE):
            raise ValueError('Frozen runner snapshot fingerprint mismatch')
        self.evidence.append({'path': str(source_path), 'sha256_at_read': sha_bytes(raw_source), 'bytes_at_read': len(raw_source)})
        schedule = manifest['schedule']
        declared = [e['run_id'] for e in schedule]
        if len(set(declared)) != len(declared):
            raise ValueError('Duplicate scheduled run ID')
        completed = {p.parent.name for p in (self.batch / 'results').glob('*/result.json')}
        if completed - set(declared):
            raise ValueError('Completed run outside schedule')
        agents, runs = [], []
        for entry in schedule:
            if entry['run_id'] in completed:
                a, r = self.run(entry)
                agents.extend(a); runs.append(r)
        return {'generated_at': datetime.now(timezone.utc).isoformat(), 'batch': str(self.batch),
            'script_sha256': sha_bytes(Path(__file__).read_bytes()), 'scheduled_runs': len(schedule),
            'completed_runs': len(runs), 'skipped_incomplete_runs': [r for r in declared if r not in completed],
            'agents': agents, 'runs': runs, 'definitions': DEFINITIONS, 'evidence': self.evidence,
            'observations': self.observations,
            'scope': {'completed_results_only': True, 'CM': 'not_integrated', 'CM_rows_included': False,
                      'cost_estimates': False, 'models_called': False, 'containers_called': False,
                      'raw_evidence_modified': False, 'model_speed_ranking_supported': False}}


def write_csv(path, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('x', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: 'null' if v is None else json.dumps(v, ensure_ascii=False)
                             if isinstance(v, (list, dict)) else v for k, v in row.items()})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    batch, output = args.batch.resolve(), args.output_dir.resolve()
    if output.exists():
        parser.error('Choose a new output directory; existing reports are never overwritten')
    manifest = json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))
    protected = [batch / x for x in ('results', 'source_snapshot', 'inputs_snapshot', 'validation_snapshot')]
    if manifest.get('official_directory'):
        protected.append(Path(manifest['official_directory']))
    if any(output.is_relative_to(p.resolve()) for p in protected):
        parser.error('Output cannot be within original evidence directories')
    data = TimingReader(batch).all()
    output.mkdir(parents=True, exist_ok=False)
    with (output / 'timing_summary.json').open('x', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')
    write_csv(output / 'agents.csv', data['agents'])
    write_csv(output / 'runs.csv', data['runs'])
    print(json.dumps({'output_dir': str(output), 'completed_runs': data['completed_runs'],
                      'agents': len(data['agents']), 'observations': len(data['observations'])}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
