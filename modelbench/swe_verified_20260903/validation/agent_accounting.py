"""Read-only partial agent accounting for the frozen SWE pilot.

Each invocation writes a new output directory. Real call IDs are charged once;
parent totals do not include their children's calls. CM placeholders are not
agents or observed zero-cost calls. This script never imports the runtime.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[1]
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')
HANDLE_FIELDS = ('run_id', 'node_id', 'item_id', 'session_id', 'attempt', 'context_epoch', 'model', 'role')


def stamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return value if isinstance(value, str) else None


def epoch(value):
    if isinstance(value, (int, float)):
        return value
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def usage(records, pending=0):
    out = {'completed_calls': len(records), 'pending_calls': pending, 'calls': len(records) + pending,
           'known_subtotals': {}, 'unknown_counts': {}}
    for key in FIELDS:
        values = [r.get(key) for r in records]
        known = [v for v in values if type(v) is int and v >= 0]
        unknown = len(values) - len(known) + pending
        out[key] = None if unknown else sum(known)
        out['known_subtotals'][key] = sum(known)
        out['unknown_counts'][key] = unknown
    return out


class Accounting:
    def __init__(self, batch):
        self.batch = Path(batch).resolve()
        self.observations = []

    def note(self, code, path, **fields):
        self.observations.append({'code': code, 'path': str(path), **fields})

    def read(self, path, optional=False):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except FileNotFoundError:
            if not optional:
                self.note('missing_json', path)
        except (ValueError, OSError, UnicodeError):
            self.note('incomplete_or_invalid_json_at_partial_read', path)
        return None

    def lines(self, path):
        try:
            raw = Path(path).read_bytes()
        except FileNotFoundError:
            return []
        lines = raw.splitlines(keepends=True)
        if lines and not lines[-1].endswith(b'\n'):
            self.note('partial_append_tail_ignored', path)
            lines.pop()
        result = []
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except (ValueError, UnicodeError):
                self.note('invalid_complete_jsonl_line', path)
        return result

    def exposed(self, manifest):
        relative = 'modelbench/swe_verified_20260903/runner.py'
        path = self.batch / 'source_snapshot' / relative
        source = path.read_text(encoding='utf-8')
        if fingerprint(path) != manifest['runtime_sources'][relative]:
            raise ValueError('Frozen runner snapshot fingerprint mismatch')
        tree = ast.parse(source)
        groups = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ('BASE_TOOLS', 'LEAD_TOOLS', 'WORKER_TOOLS'):
                        groups[target.id] = [item.args[0].value for item in node.value.elts
                            if isinstance(item, ast.Call) and item.args and isinstance(item.args[0], ast.Constant)]
        control_name = 'modelbench/swe_verified_20260903/control.py'
        control = self.batch / 'source_snapshot' / control_name
        if fingerprint(control) != manifest['runtime_sources'][control_name]:
            raise ValueError('Frozen control snapshot fingerprint mismatch')
        ctree = ast.parse(control.read_text(encoding='utf-8'))
        topology = [{'kind': n.args[0].attr.lower(), 'line': n.lineno} for n in ast.walk(ctree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'create_work_item'
            and n.args and isinstance(n.args[0], ast.Attribute)]
        supported = {item['kind'] for item in topology}
        if supported != {'derive'} or 'delegate' not in groups.get('LEAD_TOOLS', []):
            raise ValueError('This auditor only claims the frozen derive-only SWE adapter contract')
        return {'tool_groups': groups, 'topology_api_calls': topology,
            'runner_snapshot': str(path), 'control_snapshot': str(control),
            'CM': {'status': 'not_integrated', 'meaning': 'No CM service or provider route is wired into this SWE loop'},
            'split': {'status': 'not_exposed'}, 'fission': {'status': 'not_exposed'}}

    def run(self, entry, capabilities):
        run = self.batch / 'results' / entry['run_id']
        result = self.read(run / 'result.json', optional=True)
        runtime = self.lines(run / 'events.jsonl')
        rich = self.lines(run / 'control-plane/ledger/execution.jsonl')
        core = [e for row in self.lines(run / 'control-plane/events.jsonl') for e in row.get('events', [row])]
        checkpoints = [e for e in rich if e.get('kind') == 'execution.checkpoint']
        state = checkpoints[-1].get('payload', {}).get('state', {}) if checkpoints else {}
        handles, parents, starts, ends, statuses = {}, {}, {}, {}, {}
        cp_bindings, runtime_bindings = {}, {}
        worker_reserved, activated, submitted, decisions = [], [], [], []

        def register(handle):
            if isinstance(handle, dict) and isinstance(handle.get('node_id'), str):
                key = (handle['node_id'], handle.get('session_id'), handle.get('attempt'), handle.get('context_epoch'))
                if key in handles and any(handles[key].get(field) != handle.get(field) for field in HANDLE_FIELDS):
                    self.note('conflicting_handle', run, node_id=handle['node_id'])
                handles[key] = handle
                return key
            return None

        for event in rich:
            kind, payload = event.get('kind'), event.get('payload', {})
            handle = payload.get('handle')
            key = register(handle)
            if kind in ('lead_registered', 'worker_activated') and key:
                starts.setdefault(key, (event.get('at'), kind))
                statuses[key] = 'active'
            if kind == 'worker_reserved' and key:
                worker_reserved.append((key, event))
                parents[key] = payload.get('caller', {}).get('node_id')
                statuses[key] = 'provisioning'
            if kind == 'worker_activated' and key:
                activated.append(key)
            if kind in ('agent_submitted', 'agent_failed') and key:
                ends.setdefault(key, (event.get('at'), kind))
                statuses[key] = 'submitted' if kind == 'agent_submitted' else 'failed'
                if kind == 'agent_submitted':
                    submitted.append(key)
            if kind == 'worker_decided':
                worker_key = register(payload.get('worker'))
                if worker_key:
                    decisions.append((worker_key, payload.get('decision')))
                    statuses[worker_key] = 'adopted' if payload.get('decision') == 'adopt' else 'discarded'
            if kind == 'call_recorded' and key:
                ident = payload.get('record', {}).get('call_id')
                if ident in cp_bindings:
                    self.note('duplicate_cp_call_id', run, call_id=ident)
                cp_bindings[ident] = key
            if kind in ('root_finished', 'run_closed_without_final'):
                for registered in handles:
                    ends.setdefault(registered, (event.get('at'), kind))
        for value in state.get('agents', {}).values():
            key = register(value.get('handle'))
            if key:
                statuses[key] = value.get('status')
        for event in runtime:
            key = register(event.get('handle'))
            if event.get('event') == 'run_started':
                register(event.get('root_handle'))
            if event.get('event') == 'call_reserved' and key:
                runtime_bindings[event.get('call_id')] = key
        for ident in cp_bindings.keys() & runtime_bindings.keys():
            if cp_bindings[ident] != runtime_bindings[ident]:
                self.note('cp_runtime_call_binding_disagrees', run, call_id=ident)
        bindings = {**runtime_bindings, **cp_bindings}
        records, started_ids = {}, set()
        for path in sorted((run / 'calls').glob('*/metadata.json')):
            record = self.read(path)
            if not isinstance(record, dict):
                continue
            ident = record.get('call_id')
            if ident in records:
                self.note('duplicate_call_metadata', path, call_id=ident)
                continue
            records[ident] = record
        for path in sorted((run / 'calls').glob('*/started.json')):
            record = self.read(path)
            if isinstance(record, dict):
                started_ids.add(record.get('call_id'))
        assigned = {key: [] for key in handles}
        pending = {key: [] for key in handles}
        assignments, unassigned = [], []
        for ident in sorted(set(records) | set(bindings) | started_ids, key=str):
            record, key = records.get(ident), bindings.get(ident)
            if key not in handles:
                unassigned.append(ident)
                self.note('call_without_unique_agent_handle', run, call_id=ident)
            elif record is not None:
                handle = handles[key]
                if any(record.get(k) != v for k, v in (('run_id', entry['run_id']), ('task_id', entry['instance']['instance_id']),
                            ('model_requested', handle.get('model')), ('role', handle.get('role')))):
                    self.note('call_identity_mismatch', run, call_id=ident)
                assigned[key].append(record)
            else:
                pending[key].append(ident)
            assignments.append({'run_id': entry['run_id'], 'call_id': ident, 'node_id': key[0] if key else None,
                'status': 'completed' if record is not None else 'pending',
                'binding_source': 'cp_call_recorded' if ident in cp_bindings else 'runtime_call_reserved' if ident in runtime_bindings else None,
                'model': record.get('model_requested') if record else handles.get(key, {}).get('model'),
                'role': record.get('role') if record else handles.get(key, {}).get('role'),
                **{field: record.get(field) if record else None for field in FIELDS}})
        rows = []
        for key, handle in handles.items():
            calls = assigned[key]
            account = usage(calls, len(pending[key]))
            start, start_source = starts.get(key, (None, None))
            end, end_source = ends.get(key, (None, None))
            start_num, end_num = epoch(start), epoch(end)
            role = handle.get('role')
            actions = Counter(c.get('name') for r in calls for c in (r.get('action') or {}).get('calls', []))
            tool_starts = Counter(e.get('tool_call', {}).get('name') for e in runtime
                if e.get('event') == 'tool_started' and e.get('handle', {}).get('node_id') == handle.get('node_id'))
            node_state = state.get('cp', {}).get('work_items', {}).get(handle.get('item_id'), {})
            candidate = (result.get('outcome') or {}).get('status') if result and role == 'lead' else None
            rows.append({'run_id': entry['run_id'], 'condition': entry['condition'], 'instance_id': entry['instance']['instance_id'],
                'kind': 'agent', 'agent_id': handle.get('node_id'), 'node_id': handle.get('node_id'), 'item_id': handle.get('item_id'),
                'session_id': handle.get('session_id'), 'attempt': handle.get('attempt'), 'epoch': handle.get('context_epoch'),
                'parent_agent_id': parents.get(key), 'model': handle.get('model'), 'role': role,
                'action': 'root_execution' if role == 'lead' else node_state.get('kind', 'derive'),
                'trigger_agent_id': None, 'was_instantiated': True, **account,
                'start': stamp(start), 'end': stamp(end), 'start_source': start_source, 'end_source': end_source,
                'sum_call_wall_seconds': sum(r.get('wall_seconds') or 0 for r in calls),
                'agent_wall_seconds': round(end_num - start_num, 6) if start_num is not None and end_num is not None else None,
                'agent_wall_scope': 'registered/activated context through submission/failure; not a sum of child/model times',
                'status': statuses.get(key, 'unknown'), 'candidate_result': candidate,
                'cp_acceptance': node_state.get('acceptance'),
                'run_completed': result is not None, 'run_official_resolved': (result.get('score') or {}).get('resolved') if result else None,
                'requested_tool_actions': dict(actions), 'started_tool_actions': dict(tool_starts),
                'call_ids': [r['call_id'] for r in calls], 'pending_call_ids': pending[key],
                'cost_usd': None, 'includes_child_usage': False,
                'handle_manifest_path': handle.get('manifest_path'), 'handle_manifest_sha256': handle.get('manifest_hash')})
        requested = sum(c.get('name') == 'delegate' for r in records.values() for c in (r.get('action') or {}).get('calls', []))
        exposed = entry['condition'] == 'dpswarm'
        worker_keys = {key for key, _ in worker_reserved}
        core_kinds = Counter(e.get('payload', {}).get('kind') for e in core if e.get('kind') == 'work_item_created')
        cm_trace = [e for e in core if str(e.get('payload', {}).get('node_id', '')).startswith('ctx-job:')
                    or str(e.get('kind', '')).startswith(('cm_', 'context_job'))]
        if cm_trace or core_kinds.get('fission') or core_kinds.get('split'):
            self.note('unexpected_unintegrated_mechanism_trace', run)
        mechanisms = {'run_id': entry['run_id'], 'condition': entry['condition'], 'run_completed': result is not None,
            'cp_created': any(e.get('kind') == 'root_started' for e in core),
            'actual_collaboration_activated': bool(activated), 'worker_model_calls_observed': sum(r.get('role') == 'worker' for r in records.values()),
            'derive': {'status': 'exposed' if exposed else 'not_exposed', 'capability_exposed': exposed,
                'requested': requested if exposed else None, 'admitted': len(worker_keys) if exposed else None,
                'activated': len(set(activated)) if exposed else None,
                'completed': len(worker_keys & set(submitted)) if exposed else None,
                'completed_meaning': 'worker submitted a delivery; not Lead adoption or official resolution',
                'adopted': sum(decision == 'adopt' for _, decision in decisions) if exposed else None,
                'discarded': sum(decision == 'discard' for _, decision in decisions) if exposed else None,
                'observed_core_derive_items': core_kinds.get('derive', 0)},
            'split': {'status': 'not_exposed', 'capability_exposed': False, 'requested': None, 'admitted': None, 'activated': None, 'completed': None},
            'fission': {'status': 'not_exposed', 'capability_exposed': False, 'requested': None, 'admitted': None, 'activated': None, 'completed': None},
            'CM': {'status': 'not_integrated', 'capability_exposed': False, 'requested': None, 'admitted': None,
                'activated': None, 'completed': None, 'usage': None, 'trigger_agent_id': None}}
        cm_row = {'run_id': entry['run_id'], 'condition': entry['condition'], 'kind': 'context_manager',
            'agent_id': None, 'node_id': None, 'item_id': None, 'session_id': None, 'attempt': None, 'epoch': None,
            'parent_agent_id': None, 'trigger_agent_id': None, 'model': None, 'role': 'context-manager', 'action': 'CM',
            'was_instantiated': False, 'status': 'not_integrated', 'calls': None,
            **{field: None for field in FIELDS}, 'start': None, 'end': None,
            'sum_call_wall_seconds': None, 'agent_wall_seconds': None, 'cost_usd': None,
            'reason': 'The frozen SWE adapter has no CM route; these nulls are not measured zero usage'}
        pending_ids = (set(bindings) | started_ids) - set(records)
        totals = usage(list(records.values()), len(pending_ids))
        if sum(len(r['call_ids']) for r in rows) + sum(ident in records for ident in unassigned) != len(records):
            self.note('call_assignment_conservation_failed', run)
        return rows, cm_row, mechanisms, assignments, {'run_id': entry['run_id'], 'run_completed': result is not None,
            **totals, 'unassigned_call_ids': unassigned, 'agent_count': len(rows),
            'model_calls_with_protocol_error': sum(bool(r.get('protocol_error')) for r in records.values()),
            'model_calls_with_transport_error': sum(bool(r.get('error')) for r in records.values())}

    def all(self):
        manifest = self.read(self.batch / 'manifest.json')
        if not manifest:
            raise ValueError('Frozen batch manifest is required')
        capabilities = self.exposed(manifest)
        entries = [e for e in manifest['schedule'] if (self.batch / 'results' / e['run_id']).is_dir()]
        agents, cm, mechanisms, assignments, runs = [], [], [], [], []
        for entry in entries:
            a, c, m, x, r = self.run(entry, capabilities)
            agents.extend(a); cm.append(c); mechanisms.append(m); assignments.extend(x); runs.append(r)
        return {'generated_at': datetime.now(timezone.utc).isoformat(), 'batch': str(self.batch),
            'manifest_sha256': fingerprint(self.batch / 'manifest.json'), 'script_sha256': fingerprint(Path(__file__)),
            'scope': 'Partial read snapshot; running calls may settle after their observed record boundary',
            'started_runs': len(runs), 'completed_runs': sum(r['run_completed'] for r in runs),
            'scheduled_runs': len(manifest['schedule']), 'agents': agents, 'context_managers': cm,
            'runs': runs, 'call_assignments': assignments, 'mechanisms': mechanisms, 'capabilities': capabilities,
            'accounting_rules': {'input_includes_cache': True, 'output_includes_reasoning': True,
                'parent_usage_excludes_children': True, 'each_call_id_charged_once_per_run': True,
                'unknown_usage_is_null': True, 'CM_usage_measured': False, 'cost_usd': None,
                'agent_wall_is_not_sum_call_wall': True, 'DSH_bridge_exercised': False},
            'observations': self.observations}


def write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: 'null' if value is None else json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list)) else value for key, value in row.items()})


def emit(output, data):
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'agent_accounting.json', data)
    write_csv(output / 'agents.csv', data['agents'])
    write_csv(output / 'context_managers.csv', data['context_managers'])
    write_json(output / 'call_assignments.json', data['call_assignments'])
    write_json(output / 'mechanisms.json', {'generated_at': data['generated_at'], 'capabilities': data['capabilities'],
        'runs': data['mechanisms'], 'observations': data['observations']})
    active_workers = sum(r['role'] == 'worker' for r in data['agents'])
    worker_calls = sum(r['completed_calls'] for r in data['agents'] if r['role'] == 'worker')
    cp_runs = sum(m['cp_created'] for m in data['mechanisms'])
    collab = sum(m['actual_collaboration_activated'] for m in data['mechanisms'])
    known = sum(r['known_subtotals']['total_tokens'] for r in data['runs'])
    unknown = sum(r['unknown_counts']['total_tokens'] for r in data['runs'])
    lines = ['# SWE pilot agent accounting', '',
        f"Snapshot: {data['generated_at']}. Started runs: {data['started_runs']}; completed: {data['completed_runs']}/{data['scheduled_runs']}.", '',
        f'CP created in {cp_runs} runs. Collaboration activated in {collab} runs. Observed worker agents: {active_workers}; completed worker model calls: {worker_calls}.',
        f'Known total tokens across all calls: {known}; unknown/pending total-usage calls: {unknown}. Parent and child calls are counted separately once.', '',
        '| Mechanism | Exposure/integration | Interpretation |', '|---|---|---|',
        '| derive | Exposed only to dpswarm Lead as delegate; CP creates DERIVE work items | Actual request/admission/activation/submission counts appear below |',
        '| split | not_exposed | Not an observed autonomous decision to skip split |',
        '| fission | not_exposed | Not an observed autonomous decision to skip fission |',
        '| CM | not_integrated; usage null | No CM instance, trigger or measured zero-cost call |', '',
        '| Run | Complete | CP | Derive exposed | Requested | Admitted | Activated | Submitted | Adopted |',
        '|---|---|---|---|---:|---:|---:|---:|---:|']
    for m in data['mechanisms']:
        d = m['derive']
        show = lambda key: str(d[key]) if d[key] is not None else 'not_exposed'
        lines.append(f"| {m['run_id']} | {m['run_completed']} | {m['cp_created']} | {d['capability_exposed']} | " +
                     ' | '.join(show(k) for k in ('requested', 'admitted', 'activated', 'completed', 'adopted')) + ' |')
    lines += ['', '| Run / Agent | Role / Model | Completed / Pending calls | Input incl. cache | Cache | Output incl. reasoning | Reasoning | Total | Call seconds | Agent seconds | Status |',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for row in data['agents']:
        show = lambda key: str(row[key]) if row[key] is not None else 'null'
        lines.append(f"| {row['run_id']} / {row['agent_id']} | {row['role']} / {row['model']} | {row['completed_calls']} / {row['pending_calls']} | " +
            ' | '.join(show(k) for k in (*FIELDS, 'sum_call_wall_seconds', 'agent_wall_seconds', 'status')) + ' |')
    lines += ['', 'CSV null is written literally as null. JSON preserves null. Cache and reasoning are dimensions already included in input/output, not extra token charges.',
        'agents.csv includes only instantiated agents. context_managers.csv contains clearly marked not_integrated placeholders and is excluded from agent/call totals.',
        'Agent wall time is derived only when registration/activation and submission/failure endpoints exist. It includes context lifecycle overhead and is not provider latency or a sum of child time.',
        'CP completion/submission does not prove collaboration or task correctness. A worker submission is not adoption. Official run verdicts are not individual-worker grades.',
        'The DSH bridge, split, fission and CM are not integrated by this experiment. No capability or model ranking follows from absent worker selection.',
        'Read-only analysis; no model, container, grading or candidate feedback was performed. No original evidence was overwritten.']
    with (output / 'mechanisms.md').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(lines) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, default=HERE / 'pilot_v2')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    batch, output = args.batch.resolve(), args.output_dir.resolve()
    if output.exists():
        parser.error('Output directory already exists; choose a new directory')
    if any(output.is_relative_to((batch / name).resolve()) for name in ('results', 'source_snapshot', 'inputs_snapshot', 'validation_snapshot')):
        parser.error('Output cannot be inside original evidence directories')
    data = Accounting(batch).all()
    emit(output, data)
    print(json.dumps({'output_dir': str(output), 'started_runs': data['started_runs'], 'completed_runs': data['completed_runs'],
        'agents': len(data['agents']), 'worker_agents': sum(r['role'] == 'worker' for r in data['agents']),
        'completed_calls': sum(r['completed_calls'] for r in data['runs']),
        'pending_calls': sum(r['pending_calls'] for r in data['runs']), 'observations': len(data['observations'])}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
