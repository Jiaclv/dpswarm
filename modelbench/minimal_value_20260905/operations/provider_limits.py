"""Process-local routing into shared account/provider admission buckets.

Limits are operator choices, never inferred official account entitlements. No
request is retried here. Canonical transport records remain byte-for-byte owned
by the frozen transport; queue and circuit-breaker evidence uses sidecars.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import threading
import time
import traceback
from uuid import uuid4

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('_provider_shared_resources', HERE/'parallel_resources.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
MODEL_BUCKETS = {
    'gpt-5.6-sol': 'codex_account', 'gpt-5.6-luna': 'codex_account',
    'gpt-5.6-terra': 'codex_account', 'glm-5.3': 'glm_coding',
    'glm-5.3-flash': 'glm_coding', 'deepseek-v4-flash': 'deepseek',
}
ADAPTERS = {'codex_account': 'codex_text_tools_swe', 'glm_coding': 'glm_native_tools_swe',
            'deepseek': 'deepseek_native_tools_swe'}


def validate_policy(policy):
    limits = policy.get('provider_limits')
    if (policy.get('provider_limits_version') != 1
            or policy.get('provider_limits_source') != 'operator_configured'
            or not isinstance(limits, dict) or set(limits) != set(ADAPTERS)
            or any(type(value) is not int or not 1 <= value <= 12 for value in limits.values())):
        raise r.ResourceError('A versioned operator-configured provider limit is required')
    return dict(limits)


def limit_signal(record):
    """Only transport diagnostic envelopes count; model/task text never does."""
    if record.get('http_status') == 429:
        return 'http_429'
    roots = [record.get(key) for key in ('error', 'provider_error_events', 'reconnect_events')]
    pattern = re.compile(r'(?i)(?:\b429\b|rate[_ -]?limit|too many requests|'
                         r'concurren(?:cy|t)[_ -]?(?:request[_ -]?)?limit|'
                         r'usage[_ -]?limit[_ -]?reached|quota[_ -]?exceeded|'
                         r'请求频率|并发超限|并发数超|达到并发|并发限制)')
    def matches(value):
        if isinstance(value, dict):
            if value.get('http_status') == 429 or value.get('status_code') == 429:
                return True
            return any(matches(v) for k, v in value.items()
                       if k in ('error', 'code', 'message', 'detail', 'details', 'type'))
        if isinstance(value, list):
            return any(matches(item) for item in value)
        return isinstance(value, str) and pattern.search(value) is not None
    return 'explicit_provider_limit' if any(matches(value) for value in roots) else None


def release_error_diagnostic(exc):
    """Local release diagnostics without request text, credentials or locals."""
    error_number = getattr(exc, 'errno', None)
    winerror = getattr(exc, 'winerror', None)
    # Canonical OS messages avoid recording arbitrary exception payloads. The
    # stack records only source filenames/functions/line numbers, never locals
    # or source-code lines that might embed provider configuration.
    message = 'Local resource release failed'
    if isinstance(error_number, int):
        message = os.strerror(error_number)
    elif isinstance(exc, r.ResourceError) and str(exc) in {
            'Resource release without an acquisition', 'Resource slot ownership changed'}:
        message = str(exc)
    return {'error_type': type(exc).__name__, 'errno': error_number, 'winerror': winerror,
            'message': message,
            'traceback': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                          for frame in traceback.extract_tb(exc.__traceback__)]}


class ProviderGate:
    def __init__(self, group_dir, group, policy, underlying):
        self.directory, self.group, self.policy = Path(group_dir), group, policy
        self.group_hash = r.sha(self.directory/'group.json')
        self.limits = validate_policy(policy)
        self.underlying = underlying
        self.local = threading.local()
        self.event_lock = threading.RLock()
        self.event_path = self.directory/'provider-events'/('process-' + str(os.getpid()) + '.jsonl')
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        self.buckets = {name: r.FileSemaphore(Path(group['global_lock_dir'])/'providers'/name, cap,
            group_dir=self.directory, group_sha256=self.group_hash, name='provider:' + name,
            validate_leases=True) for name, cap in self.limits.items()}

    def event(self, kind, **details):
        with self.event_lock:
            with self.event_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'at': r.utc(), 'event': kind, 'pid': os.getpid(),
                    'thread': threading.get_ident(), 'group_sha256': self.group_hash, **details},
                    ensure_ascii=True, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())

    def trip(self, reason, **diagnostic):
        # Stop marker first: even failure to persist detailed diagnostics must
        # prevent subsequent acquisition. No stale lease is ever reclaimed.
        (self.directory/'CANCEL').touch(exist_ok=True)
        record = {'at': r.utc(), 'reason': reason, 'pid': os.getpid(),
            'group_sha256': self.group_hash, 'provider_limits': self.limits,
            'automatic_retry': False, **diagnostic}
        target = self.directory/('provider-trip-' + uuid4().hex + '.json')
        r.atomic_json(target, record)
        self.event('group_admission_stopped', diagnostic=str(target), reason=reason)

    def check(self):
        if r.stopped(self.directory):
            raise r.ResourceError('Provider admission stopped for this group')
        if (r.sha(self.directory/'group.json') != self.group_hash
                or r.sha(self.group['policy_path']) != self.group['policy_sha256']):
            self.trip('provider_policy_identity_changed')
            raise r.ResourceError('Provider policy changed while running')

    @contextmanager
    def route(self, model, *, run_id, role):
        if getattr(self.local, 'context', None) is not None:
            raise r.ResourceError('Nested provider route is not permitted')
        if model not in MODEL_BUCKETS:
            self.trip('unmapped_provider_model')
            raise r.ResourceError('Model has no admitted provider bucket')
        self.check()
        context = {'model': model, 'bucket': MODEL_BUCKETS[model], 'run_id': run_id, 'role': role,
                   'request_token': uuid4().hex, 'queue_started': None,
                   'provider_queue_seconds': 0.0, 'global_local_queue_seconds': 0.0, 'held': False}
        self.local.context = context
        try:
            yield
        finally:
            if context['held']:
                self.trip('provider_route_exited_with_lease')
            elif context['queue_started'] is not None:
                self.event('queue_abandoned', **self.info(context),
                           queue_seconds=time.monotonic()-context['queue_started'])
            self.local.context = None

    @staticmethod
    def info(context):
        return {key: context[key] for key in ('model', 'bucket', 'run_id', 'role', 'request_token')}

    def acquire(self, blocking=True, timeout=None):
        context = getattr(self.local, 'context', None)
        if context is None or context['held']:
            self.trip('model_slot_missing_provider_context')
            raise r.ResourceError('Model acquisition has no unique provider route')
        self.check()
        started = time.monotonic()
        if context['queue_started'] is None:
            context['request_token'] = uuid4().hex
            context['provider_queue_seconds'] = context['global_local_queue_seconds'] = 0.0
            context['queue_started'] = started
            self.event('queue_started', **self.info(context))
        bucket = self.buckets[context['bucket']]
        acquired = False
        try:
            acquired = bucket.acquire(blocking=blocking, timeout=timeout)
            context['provider_queue_seconds'] += time.monotonic()-started
            if not acquired:
                return False
            self.event('provider_acquired', **self.info(context), capacity=bucket.capacity)
            queued = time.monotonic()
            remaining = None if timeout is None else max(0, timeout-(queued-started))
            if not self.underlying.acquire(blocking=blocking, timeout=remaining):
                context['global_local_queue_seconds'] += time.monotonic()-queued
                bucket.release()
                acquired = False
                self.event('provider_released', **self.info(context), reason='global_local_unavailable')
                return False
            context['global_local_queue_seconds'] += time.monotonic()-queued
            context['held'] = True
            self.event('all_slots_acquired', **self.info(context),
                provider_queue_seconds=context['provider_queue_seconds'],
                global_local_queue_seconds=context['global_local_queue_seconds'],
                queue_seconds=time.monotonic()-context['queue_started'])
            context['queue_started'] = None
            return True
        except BaseException as exc:
            if acquired and not context['held']:
                bucket.release()
            self.trip('provider_lease_or_admission_error', error_type=type(exc).__name__)
            raise

    def release(self):
        context = getattr(self.local, 'context', None)
        if context is None or not context['held']:
            self.trip('provider_release_without_lease')
            raise r.ResourceError('Provider release has no owned lease')
        phase = 'global_and_local_slots'
        try:
            self.underlying.release()
            phase = 'provider_slot'
            self.buckets[context['bucket']].release()
            context['held'] = False
            phase = 'release_event'
            self.event('provider_released', **self.info(context), reason='call_settled')
        except BaseException as exc:
            self.trip('provider_release_failed', release_phase=phase, **self.info(context),
                      **release_error_diagnostic(exc))
            raise

    def complete(self, original, transport, model, messages, **kwargs):
        context = getattr(self.local, 'context', None)
        if (context is None or not context['held'] or context['model'] != model
                or context['bucket'] != MODEL_BUCKETS.get(model)):
            self.trip('transport_without_matching_provider_lease')
            raise r.ResourceError('Actual transport model does not match its admitted provider lease')
        call_id = kwargs['call_id']
        # The frozen transport writes a zero-attempt local cancellation record
        # if the group stopped between budget reservation and dispatch.
        if r.stopped(self.directory):
            cancelled = threading.Event(); cancelled.set()
            kwargs['cancel_event'] = cancelled
        self.event('transport_entered', **self.info(context), call_id=call_id,
                   admission_stopped=r.stopped(self.directory))
        record = original(transport, model, messages, **kwargs)
        signal = limit_signal(record)
        if record.get('adapter_mode') not in (None, ADAPTERS[context['bucket']]):
            self.trip('actual_provider_adapter_mismatch', call_id=call_id, bucket=context['bucket'])
        if signal:
            self.trip('provider_rate_limit', call_id=call_id, bucket=context['bucket'],
                signal=signal, http_status=record.get('http_status'),
                metadata_path=(record.get('raw_artifacts') or {}).get('metadata'))
        self.event('transport_returned', **self.info(context), call_id=call_id,
                   rate_limit_signal=signal, transport_attempt_count=record.get('transport_attempt_count'))
        return record

    def summary(self):
        return {'limits': self.limits, 'source': 'operator_configured', 'model_buckets': MODEL_BUCKETS,
                'buckets': {name: slot.summary() for name, slot in self.buckets.items()},
                'events': str(self.event_path), 'automatic_retry': False}


def install(group_dir, group, policy, runner, underlying):
    """Install in one fresh frozen-runtime process before constructing runs."""
    if getattr(runner, '_provider_gate_installation', None) is not None:
        raise r.ResourceError('Provider gate is already installed')
    gate = ProviderGate(group_dir, group, policy, underlying)
    prefix = 'modelbench.minimal_value_20260905'
    selector = importlib.import_module(prefix + '.selector')
    r2 = importlib.import_module(prefix + '.r2')
    transport = importlib.import_module('modelbench.swe_verified_20260903.transport')
    original_call, original_cm = runner.ValueRun._call, runner.ValueRun._cm_call
    original_selector, original_transport = selector.RestrictedSelector.run, transport.SweTransport.complete

    @wraps(original_call)
    def call(self, handle, *args, **kwargs):
        with gate.route(handle.model, run_id=self.run_id, role=handle.role):
            return original_call(self, handle, *args, **kwargs)

    @wraps(original_cm)
    def cm(self, call_id, handle, *args, **kwargs):
        with gate.route(self.limits['cm_model'], run_id=self.run_id, role='cm'):
            return original_cm(self, call_id, handle, *args, **kwargs)

    @wraps(original_selector)
    def select(self, *args, **kwargs):
        self.model_slots = gate
        with gate.route('gpt-5.6-sol', run_id=self.entry['run_id'], role='selector'):
            return original_selector(self, *args, **kwargs)

    @wraps(original_transport)
    def complete(self, model, messages, **kwargs):
        return gate.complete(original_transport, self, model, messages, **kwargs)

    runner.ValueRun._call, runner.ValueRun._cm_call = call, cm
    selector.RestrictedSelector.run = select
    transport.SweTransport.complete = complete
    runner.MODEL_SLOTS = r2.MODEL_SLOTS = gate
    r2.CONTAINER_SLOTS = runner.CONTAINER_SLOTS
    runner._provider_gate_installation = gate
    gate.event('installed', limits=gate.limits, model_buckets=MODEL_BUCKETS,
               limit_source='operator_configured', source_sha256=r.sha(__file__),
               coverage=['lead', 'worker', 'R2 candidate', 'selector', 'cm'])
    return gate
