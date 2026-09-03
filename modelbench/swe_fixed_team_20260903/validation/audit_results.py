"""Read-only evidence audit; writes one new JSON chosen with --output.

Only runs with result.json at audit start are examined. No runtime is imported,
no model/container/grader is invoked, and no gold patch or private test content
is decoded. Frozen private inputs are fingerprinted as opaque byte streams.
Use --snapshot-only for historical versions whose live source has moved on.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')
MODELS = ('glm-5.3', 'glm-5.3-flash', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def integer(value):
    return value if type(value) is int and value >= 0 else None


def usage(record):
    values = {key: record.get(key) for key in FIELDS}
    inp, out = values['input_tokens'], values['output_tokens']
    return {**values, 'total_tokens': inp + out if type(inp) is int and type(out) is int else values['total_tokens']}


def aggregate(records, *, canonical_total=False):
    rows = [{**r, **usage(r)} if canonical_total else r for r in records]
    result = {'calls': len(rows), 'known_subtotals': {}, 'unknown_counts': {}}
    for key in FIELDS:
        known = [r.get(key) for r in rows if type(r.get(key)) is int and r[key] >= 0]
        unknown = len(rows) - len(known)
        result[key] = None if unknown else sum(known)
        result['known_subtotals'][key] = sum(known)
        result['unknown_counts'][key] = unknown
    attempts = [r['transport_attempt_count'] for r in rows if integer(r.get('transport_attempt_count')) is not None]
    result.update(transport_record_count=len(rows),
        transport_attempt_count=sum(attempts) if len(attempts) == len(rows) else None,
        transport_attempt_count_known_subtotal=sum(attempts),
        transport_attempt_count_unknown_records=len(rows) - len(attempts),
        calls_with_transport_attempts=sum(integer(r.get('transport_attempt_count')) is not None and r['transport_attempt_count'] > 0 for r in rows),
        calls_with_measured_usage=sum(any(integer(r.get(k)) is not None for k in FIELDS) for r in rows),
        calls_with_complete_usage=sum(all(integer(r.get(k)) is not None for k in ('input_tokens', 'output_tokens', 'total_tokens')) for r in rows))
    return result


class Audit:
    def __init__(self, batch, *, snapshot_only=False):
        self.batch = Path(batch).resolve()
        self.snapshot_only = snapshot_only
        self.findings = []
        self.scope = 'manifest'
        # Manifest-level limits are the authoritative CM expectations; schedule
        # entries do not carry them.
        self.limits = (json.loads((self.batch / 'manifest.json').read_text(encoding='utf-8')).get('limits') or {}) \
            if (self.batch / 'manifest.json').exists() else {}

    def issue(self, code, path, *, severity='error', field=None, observed=None, expected=None):
        item = {'scope': self.scope, 'severity': severity, 'code': code, 'path': str(path)}
        if field is not None:
            item['field'] = field
        # Only scalar accounting/identity evidence is emitted, never task text.
        for key, value in (('observed', observed), ('expected', expected)):
            if value is None or isinstance(value, (bool, int, float, str)):
                item[key] = value
        self.findings.append(item)

    def equal(self, actual, expected, code, path, field=None, *, severity='error'):
        if actual != expected:
            self.issue(code, path, severity=severity, field=field, observed=actual, expected=expected)
            return False
        return True

    def path(self, base, value):
        if not isinstance(value, str):
            self.issue('invalid_artifact_path', base)
            return None
        base = Path(base).resolve()
        candidate = Path(value)
        target = candidate if candidate.is_absolute() else base / candidate
        if target.is_symlink() or not target.resolve().is_relative_to(base):
            self.issue('artifact_path_outside_evidence_root', base, observed=value)
            return None
        return target

    def read(self, path, *, optional=False):
        if path is None:
            return None
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except FileNotFoundError:
            if not optional:
                self.issue('missing_json_artifact', path)
        except (OSError, UnicodeError, ValueError):
            self.issue('invalid_json_artifact', path)
        return None

    def jsonl(self, path, *, optional=False):
        try:
            raw = Path(path).read_bytes()
        except FileNotFoundError:
            if not optional:
                self.issue('missing_jsonl_artifact', path)
            return []
        if raw and not raw.endswith(b'\n'):
            self.issue('incomplete_jsonl_tail', path)
        result = []
        for number, line in enumerate(raw.splitlines(), 1):
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError()
                result.append(value)
            except (ValueError, UnicodeError):
                self.issue('invalid_jsonl_record', path, observed=number)
        return result

    def fingerprint(self, path, expected, code, *, optional=False):
        if path is None:
            return False
        if not path.is_file() or path.is_symlink():
            if not optional:
                self.issue('missing_fingerprint_artifact', path)
            return False
        return self.equal(sha(path), expected, code, path)

    def frozen(self, manifest):
        # Manifest paths describe the reused official environment and this
        # experiment's gate; never infer them from the validation script cwd.
        official = manifest.get('official_directory')
        gate_path = manifest.get('validation_gate_path')
        if not isinstance(official, str) or not Path(official).is_absolute():
            self.issue('invalid_official_directory', self.batch / 'manifest.json')
            official = str(self.batch / 'missing-official-directory')
        summary = {'source_files': 0, 'input_files': 0, 'current_files_checked': not self.snapshot_only,
                   'source_inventory_scope': 'exact manifest runtime_sources entries', 'official_directory': official}
        for kind, base, current, field in (
            ('source', self.batch / 'source_snapshot', REPO, 'runtime_sources'),
            ('input', self.batch / 'inputs_snapshot', Path(official), 'input_artifacts'),
        ):
            entries = manifest.get(field, {})
            if not isinstance(entries, dict) or not entries:
                self.issue('missing_frozen_manifest_entries', self.batch / 'manifest.json', field=field)
                continue
            summary[kind + '_files'] = len(entries)
            for name, expected in entries.items():
                self.fingerprint(self.path(base, name), expected, kind + '_snapshot_drift')
                if not self.snapshot_only:
                    self.fingerprint(self.path(current, name), expected, 'current_' + kind + '_drift')
        gate = self.batch / 'validation_snapshot/gate.json'
        if gate.exists():
            self.fingerprint(gate, manifest.get('validation_gate_sha256'), 'validation_gate_snapshot_drift')
            value = self.read(gate) or {}
            for name, expected in value.get('validation_artifacts', {}).items():
                self.fingerprint(self.path(gate.parent, name), expected, 'validation_artifact_snapshot_drift')
        else:
            self.issue('validation_snapshot_unavailable', gate, severity='warning')
        if not self.snapshot_only:
            if not isinstance(gate_path, str) or not Path(gate_path).is_absolute():
                self.issue('invalid_validation_gate_path', self.batch / 'manifest.json')
            else:
                self.fingerprint(Path(gate_path), manifest.get('validation_gate_sha256'), 'current_validation_gate_drift')
        self.equal(manifest.get('public_instances_sha256'), manifest.get('input_artifacts', {}).get('selected_public.json'),
                   'public_input_manifest_disagreement', self.batch / 'manifest.json')
        dataset_hash = manifest.get('selection', {}).get('dataset_sha256') or manifest.get('versions', {}).get('dataset_sha256')
        self.equal(dataset_hash, manifest.get('input_artifacts', {}).get('verified.parquet'),
                   'dataset_manifest_disagreement', self.batch / 'manifest.json')
        return summary

    def call(self, path, record, entry):
        run = path.parent.parent.parent
        call_id, model = record.get('call_id'), record.get('model_requested')
        for key, expected in (('run_id', entry['run_id']), ('task_id', entry['instance']['instance_id'])):
            self.equal(record.get(key), expected, 'call_identity_mismatch', path, key)
        cm_model = self.limits.get('cm_model') or 'glm-5.3-flash'
        if model not in MODELS or record.get('role') not in ('lead', 'worker', 'cm'):
            self.issue('unrecognized_call_route', path)
        expected_model = ('gpt-5.6-sol' if record.get('role') == 'lead'
                          else cm_model if record.get('role') == 'cm'
                          else entry.get('worker_model'))
        self.equal(model, expected_model, 'fixed_role_model_mismatch', path)
        if record.get('role') == 'worker' and entry.get('condition') != 'fixed_team':
            self.issue('worker_call_in_solo_arm', path)
        if not isinstance(call_id, str) or not call_id.strip():
            self.issue('invalid_call_id', path)
        elif path.parent.name != hashlib.sha256(call_id.encode('utf-8')).hexdigest():
            self.issue('call_directory_identity_mismatch', path)
        for key in FIELDS:
            value = record.get(key)
            if value is not None and integer(value) is None:
                self.issue('invalid_usage_number', path, field=key)
            if isinstance(record.get('usage'), dict):
                self.equal(record['usage'].get(key), value, 'nested_usage_mismatch', path, key)
        for sub, total in (('cached_input_tokens', 'input_tokens'), ('reasoning_tokens', 'output_tokens')):
            if integer(record.get(sub)) is not None and integer(record.get(total)) is not None and record[sub] > record[total]:
                self.issue('usage_subdimension_exceeds_total', path, field=sub)
        native = str(model).startswith('glm-')
        cm_max_tokens = self.limits.get('cm_max_tokens') or 32768  # rev<=5 CM cap
        expected_max_tokens = cm_max_tokens if record.get('role') == 'cm' else 32768
        for key, expected in (('effort_requested', 'max'), ('service_tier_requested', None if native else 'fast'),
                              ('max_tokens_requested', expected_max_tokens), ('cap_enforced', native)):
            self.equal(record.get(key), expected, 'request_setting_mismatch', path, key)
        if record.get('effort_reported') is not None:
            self.issue('effort_echo_requires_raw_support', path, severity='warning')
        wall = record.get('wall_seconds')
        if not isinstance(wall, (int, float)) or isinstance(wall, bool) or not math.isfinite(wall) or wall < 0:
            self.issue('invalid_wall_seconds', path)
        deadline = record.get('total_deadline_seconds')
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not 0 < deadline <= 600:
            self.issue('invalid_call_deadline_setting', path)
        artifacts = record.get('raw_artifacts') or {}
        paths = {}
        for key, value in artifacts.items():
            target = self.path(run, value)
            paths[key] = target
            if target is not None and not (target.is_dir() if key == 'directory' else target.is_file()):
                self.issue('missing_raw_artifact', target, field=key)
        if paths.get('output') is not None and paths['output'].is_file():
            self.equal(paths['output'].read_text(encoding='utf-8'), record.get('text'),
                       'output_metadata_text_mismatch', paths['output'])
        if not record.get('error'):
            for key in (('prompt', 'request', 'response', 'metadata', 'output') if native else
                        ('prompt', 'request', 'stdout', 'stderr', 'metadata', 'output')):
                if key not in artifacts:
                    self.issue('raw_artifact_reference_missing', path, field=key)
        request = self.read(paths.get('request'), optional=True) or {}
        observed_usage, echoed_model, echoed_tier = None, None, None
        if native and request:
            body = request.get('body') or {}
            expected_thinking = ({'type': 'disabled'} if self.limits.get('cm_thinking') == 'disabled'
                                 else {'type': 'enabled'}) if record.get('role') == 'cm' else {'type': 'enabled'}
            expected_wire_max_tokens = cm_max_tokens if record.get('role') == 'cm' else 32768
            for key, expected in (('model', model), ('reasoning_effort', 'max'), ('temperature', 1.0),
                                  ('thinking', expected_thinking), ('max_tokens', expected_wire_max_tokens), ('tool_choice', 'auto')):
                self.equal(body.get(key), expected, 'native_wire_setting_mismatch', paths['request'], key)
            if not isinstance(body.get('tools'), list) or not body['tools']:
                if record.get('role') != 'cm':  # CM calls are tool-less by design
                    self.issue('native_tool_schema_missing', paths['request'])
            self.equal(record.get('request_body_sha256'),
                       hashlib.sha256(json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest(),
                       'native_request_body_hash_mismatch', paths['request'])
            response = self.read(paths.get('response'), optional=True) or {}
            if isinstance(response, dict):
                echoed_model, echoed_tier = response.get('model'), response.get('service_tier')
                raw = response.get('usage') or {}
                observed_usage = {'input_tokens': integer(raw.get('prompt_tokens')), 'output_tokens': integer(raw.get('completion_tokens')),
                    'cached_input_tokens': integer((raw.get('prompt_tokens_details') or {}).get('cached_tokens')),
                    'reasoning_tokens': integer((raw.get('completion_tokens_details') or {}).get('reasoning_tokens')),
                    'total_tokens': integer(raw.get('total_tokens'))}
        elif not native and request:
            argv = request.get('argv') or []
            actual_model = argv[argv.index('-m') + 1] if '-m' in argv and argv.index('-m') + 1 < len(argv) else None
            self.equal(actual_model, model, 'codex_wire_model_mismatch', paths['request'])
            for argument in ('--ignore-user-config', '--json', '--ephemeral', '--skip-git-repo-check',
                             'model_reasoning_effort="max"', 'service_tier="fast"', 'approval_policy="never"',
                             'web_search="disabled"'):
                if argument not in argv:
                    self.issue('codex_wire_setting_missing', paths['request'], observed=argument)
            for pair in (('-s', 'read-only'), ('--disable', 'shell_tool'), ('--disable', 'multi_agent')):
                if not any(tuple(argv[i:i + 2]) == pair for i in range(len(argv) - 1)):
                    self.issue('codex_isolation_setting_missing', paths['request'], observed=' '.join(pair))
            events = self.jsonl(paths['stdout'], optional=True) if paths.get('stdout') else []
            for event in events:
                for key in ('model', 'model_slug'):
                    if isinstance(event.get(key), str):
                        echoed_model = event[key]
                if isinstance(event.get('service_tier'), str):
                    echoed_tier = event['service_tier']
                if event.get('type') == 'turn.completed':
                    raw = event.get('usage') or {}
                    observed_usage = {key: integer(raw.get(key)) for key in FIELDS}
        if observed_usage is not None:
            if observed_usage['total_tokens'] is None and all(observed_usage[k] is not None for k in ('input_tokens', 'output_tokens')):
                observed_usage['total_tokens'] = observed_usage['input_tokens'] + observed_usage['output_tokens']
            for key in FIELDS:
                self.equal(record.get(key), observed_usage[key], 'raw_usage_metadata_mismatch', path, key)
        self.equal(record.get('model_reported'), echoed_model, 'model_echo_metadata_mismatch', path)
        self.equal(record.get('service_tier_reported'), echoed_tier, 'tier_echo_metadata_mismatch', path)
        if not record.get('error') and (native or echoed_model is not None):
            self.equal(echoed_model, model, 'successful_call_wrong_model', path)

    def budget(self, run, result, records):
        path = run / 'budget.json'
        saved = self.read(path) or {}
        if saved:
            self.equal(saved.get('snapshot_hash'), digest({k: v for k, v in saved.items() if k != 'snapshot_hash'}),
                       'budget_snapshot_hash_mismatch', path)
        tickets = saved.get('tickets') or {}
        completed = [v for v in tickets.values() if v.get('status') == 'completed']
        pending = [v for v in tickets.values() if v.get('status') == 'reserved']
        by_id = {r.get('call_id'): r for r in records}
        actual_ids = [v.get('call_id') for v in completed]
        for ident in set(actual_ids) ^ set(by_id):
            self.issue('budget_call_identity_set_mismatch', path, observed=ident)
        for ticket in completed:
            record = by_id.get(ticket.get('call_id'))
            if record is not None:
                self.equal(ticket.get('record_hash'), digest(record), 'budget_record_digest_mismatch', path)
                self.equal(ticket.get('record'), record, 'budget_record_evidence_mismatch', path)
        counted = aggregate(records, canonical_total=True)
        known = counted['known_subtotals']['total_tokens']
        unknown = counted['unknown_counts']['total_tokens']
        held = sum(t.get('reserved_tokens', 0) for t in pending)
        held += sum(t.get('reserved_tokens', 0) for t in completed if (t.get('usage') or {}).get('total_tokens') is None)
        expected = {'call_count': len(tickets), 'completed_call_count': len(completed), 'pending_call_count': len(pending),
            'unknown_call_count': unknown, 'known_subtotal': known, 'total_tokens': None if unknown or pending else known,
            'reserved_tokens': held, 'committed_tokens': known + held,
            'remaining_calls': max(0, saved.get('max_calls', 0) - len(tickets)),
            'remaining_tokens': max(0, saved.get('token_limit', 0) - known - held),
            'over_token_limit': known + held > saved.get('token_limit', 0)}
        for key, value in expected.items():
            self.equal((result.get('budget') or {}).get(key), value, 'result_budget_mismatch', run / 'result.json', key)
        self.equal(result.get('call_count') + result.get('cm_call_count', 0), len(records),
                   'result_call_count_mismatch', run / 'result.json')
        self.equal(result.get('cm_call_count', 0), len([r for r in records if r.get('role') == 'cm']),
                   'result_cm_call_count_mismatch', run / 'result.json')
        if pending:
            self.issue('completed_run_has_pending_calls', path, observed=len(pending))
        for model in MODELS:
            rows = [r for r in records if r.get('model_requested') == model and r.get('role') != 'cm']
            sums = aggregate(rows)
            actual = (result.get('model_usage') or {}).get(model, {})
            self.equal(actual.get('calls'), len(rows), 'result_model_call_count_mismatch', run / 'result.json', model)
            for field in FIELDS:
                self.equal(actual.get(field), sums[field], 'result_model_usage_mismatch', run / 'result.json', model + '.' + field)
                self.equal(actual.get(field + '_known_subtotal'), sums['known_subtotals'][field],
                           'result_model_known_subtotal_mismatch', run / 'result.json', model + '.' + field)
            wall = sum(r.get('wall_seconds') or 0 for r in rows)
            if abs((actual.get('sum_call_wall_seconds') or 0) - wall) > .00001:
                self.issue('result_model_wall_mismatch', run / 'result.json', field=model)
        return counted

    def control(self, run, entry, result, records):
        base = run / 'control-plane'
        manifest = self.read(base / 'manifest.json') or {}
        self.equal(manifest.get('run_id'), entry['run_id'], 'cp_run_identity_mismatch', base / 'manifest.json')
        self.equal(manifest.get('instance_id'), entry['instance']['instance_id'], 'cp_task_identity_mismatch', base / 'manifest.json')
        journal = self.jsonl(base / 'ledger/execution.jsonl')
        previous = '0' * 64
        for number, event in enumerate(journal, 1):
            self.equal(event.get('seq'), number, 'cp_rich_event_sequence_mismatch', base / 'ledger/execution.jsonl')
            self.equal(event.get('prev_hash'), previous, 'cp_rich_event_chain_mismatch', base / 'ledger/execution.jsonl')
            self.equal(event.get('hash'), digest({k: v for k, v in event.items() if k != 'hash'}),
                       'cp_rich_event_hash_mismatch', base / 'ledger/execution.jsonl')
            previous = event.get('hash')
        snap_path = base / 'ledger/execution.snapshot.json'
        snapshot = self.read(snap_path) or {}
        if snapshot:
            self.equal(snapshot.get('snapshot_hash'), digest({k: v for k, v in snapshot.items() if k != 'snapshot_hash'}),
                       'cp_snapshot_hash_mismatch', snap_path)
            seq = snapshot.get('event_seq')
            if type(seq) is int and 1 <= seq <= len(journal):
                event = journal[seq - 1]
                self.equal(snapshot.get('event_hash'), event.get('hash'), 'cp_snapshot_event_mismatch', snap_path)
                self.equal(snapshot.get('state'), event.get('payload', {}).get('state'), 'cp_snapshot_state_mismatch', snap_path)
            else:
                self.issue('cp_snapshot_sequence_invalid', snap_path)
        state = snapshot.get('state') or {}
        for key in ('active_points', 'open_worker_slots_used'):
            self.equal((state.get('cp') or {}).get(key), 0, 'cp_unreleased_capacity', snap_path, key)
        self.equal((state.get('cp') or {}).get('seal_phase', {}).get('root'), 'completed', 'cp_root_not_sealed', snap_path)
        calls = [e.get('payload', {}) for e in journal if e.get('kind') == 'call_recorded']
        ids = [c.get('record', {}).get('call_id') for c in calls]
        metadata = {r.get('call_id'): r for r in records if r.get('role') != 'cm'}  # CM is event-attributed, not CP-recorded
        for ident, count in Counter(ids).items():
            if count != 1:
                self.issue('duplicate_cp_call_record', base / 'ledger/execution.jsonl', observed=ident)
        for ident in set(ids) ^ set(metadata):
            self.issue('cp_call_identity_set_mismatch', base / 'ledger/execution.jsonl', observed=ident)
        transactions = self.jsonl(base / 'events.jsonl')
        core = [event for row in transactions for event in row.get('events', [row])]
        core_by_seq = {e.get('seq'): e for e in core}
        token_events = [e for e in core if e.get('kind') == 'token_usage_recorded']
        for value in calls:
            record, handle = value.get('record', {}), value.get('handle', {})
            ident = record.get('call_id')
            self.equal(record, metadata.get(ident), 'cp_call_evidence_mismatch', base / 'ledger/execution.jsonl')
            for key, expected in (('run_id', entry['run_id']), ('instance_id', entry['instance']['instance_id']),
                                  ('model', record.get('model_requested')), ('role', record.get('role'))):
                self.equal(handle.get(key), expected, 'cp_call_handle_identity_mismatch', base / 'ledger/execution.jsonl', key)
            self.fingerprint(self.path(run, handle.get('manifest_path')), handle.get('manifest_hash'), 'cp_agent_manifest_drift')
            usage_value = usage(record)
            inp, cache = usage_value['input_tokens'], usage_value['cached_input_tokens']
            mapping = {'node_id': handle.get('node_id'), 'input': inp - cache if inp is not None and cache is not None else None,
                'output': usage_value['output_tokens'], 'cache_read': cache, 'cache_write': None, 'cost': None}
            event = core_by_seq.get(value.get('cp_token_event_seq'), {})
            self.equal(event.get('kind'), 'token_usage_recorded', 'cp_usage_event_missing', base / 'events.jsonl')
            self.equal(event.get('payload'), mapping, 'cp_exclusive_cache_mapping_mismatch', base / 'events.jsonl')
        self.equal(len(token_events), len(calls), 'cp_unattributed_token_events', base / 'events.jsonl')
        totals = aggregate([c.get('record', {}) for c in calls], canonical_total=True)
        for field in FIELDS:
            self.equal((state.get('usage') or {}).get('total', {}).get(field), totals[field], 'cp_usage_aggregate_mismatch', snap_path, field)
            for dimension in ('known_subtotals', 'unknown_counts'):
                self.equal((state.get('usage') or {}).get('total', {}).get(dimension, {}).get(field),
                           totals[dimension][field], 'cp_usage_completeness_mismatch', snap_path, dimension + '.' + field)
        self.equal((state.get('usage') or {}).get('total', {}).get('calls'), len(calls), 'cp_usage_call_count_mismatch', snap_path)
        if result.get('cp_result'):
            self.equal(result['cp_result'].get('official_resolved'), None, 'cp_claims_official_verdict', run / 'result.json')
            self.equal(result['cp_result'].get('usage'), state.get('usage'), 'result_cp_usage_mismatch', run / 'result.json')
            self.equal(result['cp_result'].get('cp'), state.get('cp'), 'result_cp_state_mismatch', run / 'result.json')
        return {'rich_call_records': len(calls), 'core_token_events': len(token_events),
                'rich_event_hashes_checked': len(journal), 'invariant_replay_performed': False,
                'stored_invariant_replay_claim': (result.get('cp_result') or {}).get('invariant_replay_passed')}

    def grade(self, run, entry, result, manifest):
        patch = run / 'model.patch'
        if patch.is_file():
            self.fingerprint(patch, result.get('patch_sha256'), 'final_patch_hash_mismatch')
        elif not result.get('infrastructure_error'):
            self.issue('final_patch_missing', patch)
        score = result.get('score')
        if not isinstance(score, dict):
            if not result.get('infrastructure_error'):
                self.issue('completed_run_has_no_grading_result', run / 'result.json')
            return {'completed': False, 'resolved': None, 'reports_checked': 0}
        job = self.path(run, score.get('grader_dir', str(run / 'environment/grader')))
        if job is None:
            return {'completed': score.get('completed'), 'resolved': score.get('resolved'), 'reports_checked': 0}
        saved = self.read(job / 'result.json', optional=score.get('status') == 'grader_error') or {}
        if saved:
            self.equal({k: v for k, v in score.items() if k != 'grader_dir'}, saved, 'official_result_copy_mismatch', job / 'result.json')
            self.equal(saved.get('patch_sha256'), result.get('patch_sha256'), 'graded_patch_hash_mismatch', job / 'result.json')
        request = self.read(job / 'request.json', optional=not score.get('completed')) or {}
        if request:
            for field, expected in (('instance_id', entry['instance']['instance_id']),
                                    ('base_commit', entry['instance'].get('base_commit')),
                                    ('patch_sha256', result.get('patch_sha256')),
                                    ('grader_contract', entry.get('grader_contract'))):
                self.equal(request.get(field), expected, 'grader_request_binding_mismatch', job / 'request.json', field)
            for path in (job / 'model.patch', run / 'environment/final.patch'):
                self.fingerprint(path, result.get('patch_sha256'), 'grader_patch_copy_mismatch')
            binding = score.get('binding') or {}
            self.equal(binding.get('input_artifacts'), manifest.get('input_artifacts'), 'grader_input_binding_mismatch', job / 'result.json')
            self.equal(binding.get('environment_sha'), manifest.get('runtime_sources', {}).get('modelbench/swe_verified_20260903/environment.py'),
                       'grader_source_binding_mismatch', job / 'result.json')
        reports, hashes = score.get('reports') or [], score.get('reports_sha256') or {}
        self.equal(set(reports), set(hashes), 'official_report_hash_inventory_mismatch', job / 'result.json')
        resolved = []
        for name in reports:
            path = self.path(job, name)
            self.fingerprint(path, hashes.get(name), 'official_report_hash_mismatch')
            # Select only the verdict field. No private test names/results are emitted.
            report = self.read(path) or {}
            verdict = report.get(entry['instance']['instance_id'], {}).get('resolved')
            if type(verdict) is not bool:
                self.issue('official_report_verdict_missing', path)
            resolved.append(verdict)
        if score.get('completed'):
            if not reports:
                self.issue('official_completed_without_report', job / 'result.json')
            for verdict in resolved:
                self.equal(score.get('resolved'), verdict, 'official_resolved_mismatch', job / 'result.json')
        elif score.get('failure_kind') == 'candidate_empty_patch':
            self.equal(score.get('resolved'), False, 'empty_patch_verdict_mismatch', job / 'result.json')
            if patch.is_file() and patch.read_bytes().strip():
                self.issue('nonempty_patch_classified_empty', patch)
        elif score.get('resolved') is not None:
            self.issue('incomplete_grading_has_verdict', job / 'result.json')
        return {'completed': score.get('completed'), 'resolved': score.get('resolved'),
                'failure_kind': score.get('failure_kind'), 'reports_checked': len(reports)}

    def activation(self, run, entry, result, records):
        """Cross-check fixed protocol events, real CP handles and agent billing."""
        path = run / 'events.jsonl'
        runtime = self.jsonl(path)
        rich = self.jsonl(run / 'control-plane/ledger/execution.jsonl')
        requested = [e for e in runtime if e.get('event') == 'team_activation_requested']
        admitted = [e for e in runtime if e.get('event') == 'worker_admitted']
        activated = [e for e in runtime if e.get('event') == 'worker_activated']
        first_calls = [e for e in runtime if e.get('event') == 'worker_first_call_completed']
        fixed = entry.get('condition') == 'fixed_team'
        self.equal(result.get('fixed_team_requested'), fixed, 'fixed_team_requested_mismatch', run / 'result.json')
        self.equal(result.get('activation_source'), 'experiment_protocol' if fixed else None,
                   'activation_source_mismatch', run / 'result.json')
        if len(requested) > 1 or (not fixed and (requested or admitted or activated or first_calls)):
            self.issue('unexpected_bootstrap_event_count', path)
        if fixed and not requested and not result.get('infrastructure_error'):
            self.issue('fixed_team_missing_request_event', path)
        for event in requested:
            for key, expected in (('source', 'experiment_protocol'), ('requested_workers', 2),
                                  ('mechanism', 'derive'), ('worker_model', entry.get('worker_model'))):
                self.equal(event.get(key), expected, 'bootstrap_request_mismatch', path, key)
        cp_admitted = {e['payload']['handle']['node_id']: e['payload'] for e in rich
                       if e.get('kind') == 'worker_reserved'}
        cp_activated = {e['payload']['handle']['node_id'] for e in rich if e.get('kind') == 'worker_activated'}
        runtime_nodes = [e.get('handle', {}).get('node_id') for e in admitted]
        self.equal(set(runtime_nodes), set(cp_admitted), 'bootstrap_cp_admission_set_mismatch', path)
        if len(set(runtime_nodes)) != len(runtime_nodes) or len(runtime_nodes) > 2:
            self.issue('duplicate_or_excess_bootstrap_workers', path)
        for event in admitted:
            handle = event.get('handle') or {}
            self.equal(event.get('source'), 'experiment_protocol', 'bootstrap_admission_source_mismatch', path)
            self.equal(handle.get('model'), entry.get('worker_model'), 'bootstrap_worker_model_mismatch', path)
            self.equal(handle.get('role'), 'worker', 'bootstrap_worker_role_mismatch', path)
            cp = cp_admitted.get(handle.get('node_id'), {})
            self.equal(handle, cp.get('handle'), 'bootstrap_handle_mismatch', path)
            self.equal(event.get('request'), cp.get('request'), 'bootstrap_request_cp_mismatch', path)
        self.equal({e.get('handle', {}).get('node_id') for e in activated}, cp_activated,
                   'bootstrap_cp_activation_set_mismatch', path)
        for event in activated + first_calls:
            self.equal(event.get('activation_source'), 'experiment_protocol', 'worker_activation_source_mismatch', path)
        by_id = {r['call_id']: r for r in records}
        bindings = {}
        for event in rich:
            if event.get('kind') == 'call_recorded':
                payload = event['payload']
                bindings[payload['record']['call_id']] = payload['handle']
        worker_records = {handle['node_id'] for ident, handle in bindings.items()
                          if ident in by_id and by_id[ident].get('role') == 'worker'}
        worker_nodes = {handle['node_id'] for ident, handle in bindings.items()
                        if ident in by_id and by_id[ident].get('role') == 'worker'
                        and integer(by_id[ident].get('transport_attempt_count')) is not None
                        and by_id[ident]['transport_attempt_count'] > 0}
        worker_usage = {handle['node_id'] for ident, handle in bindings.items()
                        if ident in by_id and by_id[ident].get('role') == 'worker'
                        and any(integer(by_id[ident].get(k)) is not None for k in FIELDS)}
        first_nodes = [e.get('handle', {}).get('node_id') for e in first_calls]
        self.equal(set(first_nodes), worker_nodes, 'worker_first_call_coverage_mismatch', path)
        if len(set(first_nodes)) != len(first_nodes):
            self.issue('duplicate_worker_first_call_event', path)
        for event in first_calls:
            self.equal(event.get('handle'), bindings.get(event.get('call_id')), 'worker_first_call_binding_mismatch', path)
        for key, expected in (('bootstrap_admitted_workers', len(runtime_nodes)),
                              ('bootstrap_admitted', len(runtime_nodes) == 2),
                              ('workers_with_actual_calls', len(worker_nodes)),
                              ('workers_with_call_records', len(worker_records)),
                              ('workers_with_measured_usage', len(worker_usage))):
            self.equal(result.get(key), expected, 'bootstrap_result_count_mismatch', run / 'result.json', key)
        self.equal(result.get('team_execution_valid'), result.get('team_execution_status') == 'workers_completed' if fixed else None,
                   'team_validity_flag_mismatch', run / 'result.json')
        if result.get('team_execution_status') == 'workers_completed':
            if len(cp_admitted) != 2 or len(result.get('workers', [])) != 2:
                self.issue('false_worker_completion_coverage', run / 'result.json')
            if any(w.get('status') != 'completed' for w in result.get('workers', [])):
                self.issue('false_worker_completion_status', run / 'result.json')
        claimed = result.get('agent_usage')
        if not isinstance(claimed, dict):
            self.issue('per_agent_usage_missing', run / 'result.json')
            claimed = {}
        assigned_ids = []
        for name, value in claimed.items():
            handle = value.get('handle') or {}
            selected = [r for ident, r in by_id.items() if bindings.get(ident) == handle]
            ids = [r['call_id'] for r in selected]
            assigned_ids.extend(ids)
            self.equal(Counter(value.get('call_ids', [])), Counter(ids), 'agent_call_set_mismatch', run / 'result.json', name)
            sums = aggregate(selected)
            for key in ('calls', *FIELDS, 'known_subtotals', 'unknown_counts', 'transport_record_count',
                        'transport_attempt_count', 'transport_attempt_count_known_subtotal',
                        'transport_attempt_count_unknown_records', 'calls_with_transport_attempts',
                        'calls_with_measured_usage', 'calls_with_complete_usage'):
                self.equal(value.get(key), sums[key], 'agent_usage_mismatch', run / 'result.json', name + '.' + key)
            wall = sum(r.get('wall_seconds') or 0 for r in selected)
            if not isinstance(value.get('sum_call_wall_seconds'), (int, float)) or abs(value['sum_call_wall_seconds'] - wall) > 1e-5:
                self.issue('agent_wall_mismatch', run / 'result.json', field=name)
        self.equal(Counter(assigned_ids), Counter([i for i, r in by_id.items() if r.get('role') != 'cm']),
                   'agent_billing_not_conserved', run / 'result.json')
        attempts = aggregate([r for r in records if r.get('role') != 'cm'])
        for field in ('transport_record_count', 'transport_attempt_count', 'transport_attempt_count_known_subtotal',
                      'transport_attempt_count_unknown_records', 'calls_with_transport_attempts',
                      'calls_with_measured_usage', 'calls_with_complete_usage'):
            self.equal(result.get(field), attempts[field], 'result_transport_attempts_mismatch', run / 'result.json', field)
        return {'source': 'experiment_protocol' if fixed else None, 'protocol_request_events': len(requested),
                'admitted_workers': len(cp_admitted), 'activated_workers': len(cp_activated),
                'workers_with_actual_calls': len(worker_nodes), 'workers_with_call_records': len(worker_records),
                'workers_with_measured_usage': len(worker_usage), 'model_requested_delegation': sum(
                    c.get('name') == 'delegate' for r in records for c in (r.get('action') or {}).get('calls', [])),
                'team_execution_status': result.get('team_execution_status'),
                'team_execution_valid': result.get('team_execution_valid')}

    def run(self, entry, manifest):
        run = self.batch / 'results' / entry['run_id']
        self.scope = entry['run_id']
        initial = len(self.findings)
        result = self.read(run / 'result.json') or {}
        self.equal(result.get('run_id'), entry['run_id'], 'result_run_identity_mismatch', run / 'result.json')
        self.equal(result.get('instance_id'), entry['instance']['instance_id'], 'result_task_identity_mismatch', run / 'result.json')
        self.equal(result.get('condition'), entry['condition'], 'result_condition_mismatch', run / 'result.json')
        for field in ('arm', 'worker_model'):
            self.equal(result.get(field), entry.get(field), 'result_arm_identity_mismatch', run / 'result.json', field)
        paths = sorted((run / 'calls').glob('*/metadata.json'))
        records = []
        for path in paths:
            record = self.read(path)
            if isinstance(record, dict):
                records.append(record)
                self.call(path, record, entry)
        ids = [r.get('call_id') for r in records]
        for ident, count in Counter(ids).items():
            if count != 1:
                self.issue('duplicate_call_metadata_id', run / 'calls', observed=ident)
        events = self.jsonl(run / 'calls.jsonl', optional=not records)
        for kind in ('started', 'completed'):
            grouped = [e for e in events if e.get('event') == kind]
            self.equal(Counter(e.get('call_id') for e in grouped), Counter(ids), 'call_journal_identity_set_mismatch', run / 'calls.jsonl', kind)
            if kind == 'completed':
                by_id = {r.get('call_id'): r for r in records}
                for event in grouped:
                    self.equal({k: v for k, v in event.items() if k != 'event'}, by_id.get(event.get('call_id')),
                               'completed_event_metadata_mismatch', run / 'calls.jsonl')
        accounting = self.budget(run, result, records)
        self.equal(result.get('protocol_errors'), sum(bool(r.get('protocol_error')) for r in records),
                   'result_protocol_error_count_mismatch', run / 'result.json')
        control = self.control(run, entry, result, records)
        grading = self.grade(run, entry, result, manifest)
        activation = self.activation(run, entry, result, records)
        outcome = result.get('outcome') or {}
        # Transport failures are valid observations, not automatically audit corruption.
        return {'run_id': entry['run_id'], 'instance_id': entry['instance']['instance_id'], 'condition': entry['condition'],
            'arm': entry.get('arm'), 'worker_model': entry.get('worker_model'),
            'result_sha256': sha(run / 'result.json'), 'calls': len(records), 'call_ids': ids, 'usage': accounting,
            'outcome': outcome.get('status'), 'infrastructure_error_type': (result.get('infrastructure_error') or {}).get('type'),
            'transport_errors': sum(bool(r.get('error')) for r in records),
            'protocol_errors': sum(bool(r.get('protocol_error')) for r in records),
            'requested_settings': [{'model': m, 'effort': 'max', 'tier': None if m.startswith('glm-') else 'fast'}
                                   for m in MODELS if any(r.get('model_requested') == m for r in records)],
            'echo_unknown_counts': {field: sum(r.get(field) is None for r in records)
                                    for field in ('model_reported', 'effort_reported', 'service_tier_reported')},
            'model_usage': {m: aggregate([r for r in records if r.get('model_requested') == m]) for m in MODELS},
            'sum_call_wall_seconds': sum(r.get('wall_seconds') or 0 for r in records),
            'control': control, 'grading': grading, 'activation': activation,
            'audit_error_count': sum(f['severity'] == 'error' for f in self.findings[initial:])}

    def all(self):
        manifest_path = self.batch / 'manifest.json'
        manifest = self.read(manifest_path)
        if not isinstance(manifest, dict):
            raise ValueError('A valid frozen batch manifest is required')
        schedule = manifest.get('schedule') or []
        completed_at_start = {p.parent.name for p in (self.batch / 'results').glob('*/result.json')}
        declared = [e['run_id'] for e in schedule]
        if len(set(declared)) != len(declared):
            self.issue('duplicate_schedule_run_id', manifest_path)
        for name in sorted(completed_at_start - set(declared)):
            self.issue('completed_run_not_in_schedule', self.batch / 'results' / name)
        frozen = self.frozen(manifest)
        runs = []
        for entry in schedule:
            if entry['run_id'] not in completed_at_start:
                continue
            try:
                runs.append(self.run(entry, manifest))
            except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
                self.issue('audit_could_not_finish_run', self.batch / 'results' / entry['run_id'], observed=type(exc).__name__)
        for ident, count in Counter(ident for run in runs for ident in run['call_ids']).items():
            if count != 1:
                self.issue('cross_run_duplicate_call_id', self.batch / 'results', observed=ident)
        errors = sum(f['severity'] == 'error' for f in self.findings)
        return {'generated_at': datetime.now(timezone.utc).isoformat(), 'batch': str(self.batch),
            'manifest_sha256': sha(manifest_path), 'auditor_sha256': sha(Path(__file__)),
            'status': 'FAIL' if errors else ('PASS_WITH_WARNINGS' if self.findings else 'PASS'),
            'mode': 'frozen_snapshot_only' if self.snapshot_only else 'frozen_snapshot_and_current_sources_inputs',
            'scheduled_runs': len(schedule), 'completed_runs_at_start': len(completed_at_start), 'audited_runs': len(runs),
            'skipped_incomplete_runs': [name for name in declared if name not in completed_at_start],
            'frozen_artifacts': frozen, 'calls': sum(r['calls'] for r in runs),
            'known_total_tokens': sum(r['usage']['known_subtotals']['total_tokens'] for r in runs),
            'unknown_total_usage_calls': sum(r['usage']['unknown_counts']['total_tokens'] for r in runs),
            'error_count': errors, 'warning_count': len(self.findings) - errors, 'runs': runs, 'findings': self.findings,
            'scope': {'models_called': False, 'grader_rerun': False, 'original_evidence_modified': False,
                'private_inputs': 'opaque SHA256 only; gold patches and private tests are not decoded',
                'cp': 'raw journal/handle/token mapping and rich hash chain checked; no invariant replay or correctness claim',
                'pricing': 'not computed; missing provider echoes and usage remain unknown'}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New audit JSON; existing files are never overwritten')
    parser.add_argument('--snapshot-only', action='store_true', help='Historical batch: do not compare current source/input files')
    parser.add_argument('--expected-infrastructure-failure', action='store_true',
                        help='Label an explicitly known zero-call, ungraded infrastructure stop; retain integrity status')
    args = parser.parse_args(argv)
    batch, output = args.batch.resolve(), args.output.resolve()
    if output.exists() or output.suffix.lower() != '.json':
        parser.error('--output must be a new .json file')
    manifest = json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))
    official = Path(manifest['official_directory']).resolve()
    protected = (batch / 'results', batch / 'source_snapshot', batch / 'inputs_snapshot', batch / 'validation_snapshot', official)
    if any(output.is_relative_to(path.resolve()) for path in protected):
        parser.error('Audit output cannot be written inside original evidence directories')
    audit = Audit(batch, snapshot_only=args.snapshot_only).all()
    if args.expected_infrastructure_failure:
        if not audit['runs'] or any(r['calls'] or r['grading']['completed'] or not r['infrastructure_error_type']
                                    for r in audit['runs']):
            parser.error('Expected infrastructure classification requires only zero-call, ungraded infrastructure-failure runs')
        audit['audit_integrity_status'] = audit['status']
        audit['status'] = 'EXPECTED_KNOWN_INFRA_FAILURE'
        audit['is_benchmark_score'] = False
        audit['interpretation'] = 'Explicitly classified infrastructure initialization stop; zero candidate calls, no official score. Integrity findings remain unchanged.'
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(audit, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'audit': str(output), 'status': audit['status'], 'audited_runs': audit['audited_runs'],
                      'calls': audit['calls'], 'known_total_tokens': audit['known_total_tokens'],
                      'errors': audit['error_count'], 'warnings': audit['warning_count']}))
    return 1 if audit['error_count'] else 0


if __name__ == '__main__':
    sys.exit(main())
