"""Explicit Coding Plan GLM route; reuse the audited SWE call lifecycle.

No fallback to Coding Plan, alternate models, or unaccounted retries.  The
inherited module is loaded privately so historical runners retain their route.
"""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

CODING_GLM_BASE_URL = 'https://open.bigmodel.cn/api/coding/paas/v4'
CODING_GLM_ENDPOINT = CODING_GLM_BASE_URL + '/chat/completions'
GLM_MODELS = frozenset(('glm-5.3', 'glm-5.3-flash'))
GLM_ADAPTER = 'glm_coding_plan_stream_native_tools_swe_v4'
GLM_STREAM_WORKER = Path(__file__).resolve().parent / 'glm_stream_http_worker.py'
TRANSPORT_POLICY_PATH = Path(__file__).resolve().parent / 'TRANSPORT_POLICY.json'
_EXPECTED_TRANSPORT_POLICY = {
    'protocol': 'big_budget_transport_coding_stream_v4',
    'glm_read_timeout_seconds': 900, 'glm_total_timeout_seconds': 960,
    'other_model_total_timeout_seconds': 600,
    'cm_read_timeout_seconds': 120, 'cm_total_timeout_seconds': 600,
    'stream': True, 'tool_stream': True, 'automatic_retry': False,
    'glm_channel': 'bigmodel_coding_plan', 'glm_endpoint': CODING_GLM_ENDPOINT,
}


def read_transport_policy():
    """Bind exact frozen protocol and timeout semantics without editing historical contracts."""
    raw = TRANSPORT_POLICY_PATH.read_bytes()
    policy = json.loads(raw.decode('utf-8'))
    if (not isinstance(policy, dict) or set(policy) != set(_EXPECTED_TRANSPORT_POLICY)
            or any(type(policy[k]) is not type(v) or policy[k] != v
                   for k, v in _EXPECTED_TRANSPORT_POLICY.items())):
        raise ValueError('Transport policy differs from the approved transport contract')
    return policy, hashlib.sha256(raw).hexdigest()


_CALL_POLICY = ContextVar('big_budget_glm_call_policy', default=None)
SOURCE = Path(__file__).resolve().parents[1] / 'swe_verified_20260903/transport.py'
_spec = importlib.util.spec_from_file_location('_big_budget_swe_' + uuid4().hex, SOURCE)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
_load_original_v2 = _base._load_v2
_write_original = _base._write_once
_run_process_original = _base._run_process


def validate_endpoint(base_url):
    if not isinstance(base_url, str):
        raise ValueError('GLM Coding Plan base URL must be an explicit string')
    parsed = urlsplit(base_url)
    if (base_url.rstrip('/') != CODING_GLM_BASE_URL
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Only the frozen BigModel Coding Plan endpoint is allowed')
    return CODING_GLM_BASE_URL


def configured_glm_base_url(runtime=None):
    """Use the same environment/local configuration loader as GLM_API_KEY."""
    runtime = runtime if runtime is not None else _load_original_v2()
    return validate_endpoint(runtime.original._keyconfig('GLM_BASE_URL'))


def _stamp(record):
    policy = _CALL_POLICY.get()
    if isinstance(record, dict) and 'call_id' in record and policy is not None:
        record.update(transport_policy_sha256=policy['policy_sha256'],
                      transport_protocol=policy['protocol'],
                      total_deadline_seconds_requested=policy['requested_seconds'],
                      total_deadline_seconds_effective=policy['effective_seconds'],
                      model_total_timeout_limit_seconds=policy['model_cap_seconds'])
    if isinstance(record, dict) and record.get('model_requested') in GLM_MODELS:
        record.update(adapter_mode=GLM_ADAPTER, provider_channel='bigmodel_coding_plan',
                      endpoint=CODING_GLM_ENDPOINT, base_url=CODING_GLM_BASE_URL,
                      automatic_channel_fallback=False,
                      configured_glm_base_url=policy.get('configured_glm_base_url') if policy else None,
                      endpoint_configuration_source='GLM_BASE_URL via keyconfig environment-or-local')
        policy = _CALL_POLICY.get()
        if policy is not None and 'glm_read_timeout_seconds_configured' not in record:
            record.update(glm_read_timeout_seconds_configured=policy['read_timeout_seconds'],
                          glm_read_timeout_seconds_effective=None,
                          socket_timeout_seconds=None, glm_stream=True, glm_tool_stream=True)
    return record


def _write_once(path, value):
    return _write_original(path, _stamp(value))



def _stream_observations(record, envelope, secrets):
    """Retain call-local diagnostics, never recover an action from progress."""
    metrics = envelope.get('stream_metrics')
    if isinstance(metrics, dict):
        record['stream_metrics'] = _base._diagnostic_redact(deepcopy(metrics), secrets)
    directory = Path(record['raw_artifacts']['directory']) / 'glm-stream'
    expected = {'raw_events': directory / 'stream.events.jsonl',
                'progress': directory / 'stream.progress.json'}
    supplied = envelope.get('stream_artifacts')
    if isinstance(supplied, dict):
        for name, path in supplied.items():
            try:
                valid = name in expected and Path(path).resolve() == expected[name].resolve()
            except (TypeError, ValueError, OSError):
                valid = False
            if not valid:
                record['stream_artifact_error'] = 'Worker artifact outside the expected call-local paths'
    paths = {name: str(path) for name, path in expected.items() if path.is_file()}
    record['stream_artifacts'] = paths
    record['raw_artifacts'].update({'glm_stream_' + name: path for name, path in paths.items()})


def _run_process(argv, **kwargs):
    """Route only this private adapter's GLM SSE HTTP jobs to its owned worker."""
    if '--http-worker' not in argv or str(_base.V2_SOURCE) not in argv:
        return _run_process_original(argv, **kwargs)
    try:
        job = json.loads(kwargs['input'])
        payload = json.loads(job.get('payload', '{}'))
        stream = (job.get('endpoint') == CODING_GLM_ENDPOINT
                  and payload.get('model') in GLM_MODELS and payload.get('stream') is True)
    except (KeyError, TypeError, ValueError, AttributeError):
        stream = False
    if not stream:
        return _run_process_original(argv, **kwargs)
    record = kwargs['record']
    directory = Path(kwargs['cwd']).parent / 'glm-stream'
    job['evidence_dir'] = str(directory)
    routed = list(argv)
    routed[routed.index(str(_base.V2_SOURCE))] = str(GLM_STREAM_WORKER)
    options = dict(kwargs, input=json.dumps(job, ensure_ascii=True))
    record['http_worker_script'] = str(GLM_STREAM_WORKER)
    record['http_worker_mode'] = 'glm_sse'
    try:
        return _run_process_original(routed, **options)
    finally:
        # The outer supervisor may terminate a worker before stdout is complete.
        # Its atomic progress retains timing and counts without authorizing output.
        envelope = {}
        progress = directory / 'stream.progress.json'
        if progress.is_file():
            try:
                envelope = json.loads(progress.read_text(encoding='utf-8'))
                if not isinstance(envelope, dict):
                    envelope = {}
                    record['stream_progress_error'] = 'Progress was not an object'
            except (OSError, ValueError) as exc:
                record['stream_progress_error'] = type(exc).__name__
        _stream_observations(record, envelope, (job.get('key'),))


def _coding_call(runtime):
    def glm(self, record, messages, declarations, folder, secrets, deadline_seconds):
        _stamp(record)
        record['configured_endpoint_verified_at_call'] = False
        try:
            configured = configured_glm_base_url(runtime)
        except (TypeError, ValueError):
            raise runtime.TransportError('configured_endpoint_mismatch',
                'GLM_BASE_URL must match the frozen Coding Plan endpoint') from None
        if configured != self.glm_base_url:
            raise runtime.TransportError('configured_endpoint_mismatch',
                'Configured GLM endpoint changed after transport construction')
        record['configured_endpoint_verified_at_call'] = True
        deadline = time.monotonic() + deadline_seconds
        payload = {'model': record['model_requested'], 'messages': deepcopy(messages),
                   'tools': declarations, 'tool_choice': 'auto',
                   'thinking': record.get('glm_thinking') or {'type': 'enabled'},
                   'reasoning_effort': 'max', 'temperature': 1.0,
                   'max_tokens': record['max_tokens_requested'], 'stream': True, 'tool_stream': True}
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        runtime.original._dump(folder / 'wire.request.json',
                               {'endpoint': CODING_GLM_ENDPOINT, 'body': payload}, secrets)
        record['request_body_sha256'] = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        record['raw_artifacts']['request'] = str(folder / 'wire.request.json')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise runtime.TransportError('total_deadline', 'Total call deadline elapsed before GLM HTTP start')
        configured = (self.glm_cm_read_timeout_seconds if record.get('role') == 'cm'
                      else self.glm_read_timeout_seconds)
        effective = min(configured, remaining)
        record.update(glm_read_timeout_seconds_configured=configured,
                      glm_read_timeout_seconds_effective=effective,
                      socket_timeout_seconds=effective, glm_stream=True, glm_tool_stream=True)
        response = runtime._http_exchange(CODING_GLM_ENDPOINT, serialized, secrets[0],
                         socket_timeout_seconds=effective, deadline_seconds=remaining)
        _stream_observations(record, response, secrets)
        record['http_status'] = response.get('http_status')
        wire_error = response.get('error_code')
        wire_message = response.get('error_message')
        transport_failure = bool(wire_error and wire_error != 'http_error')
        if wire_error:
            record['http_exchange_error'] = _base._diagnostic_redact({
                'error_code': wire_error, 'error_message': wire_message,
                'http_status': response.get('http_status')}, secrets)

        def fail_transport():
            raise runtime.TransportError(str(wire_error), str(wire_message or
                'GLM HTTP transport failed: ' + str(wire_error)))

        if record['http_status'] == 429:
            record['provider_limit_signal'] = 'http_429'
        if 'raw' in response:
            (folder / 'wire.response.raw.json').write_text(
                runtime.original._redact(response['raw'], secrets), encoding='utf-8')
            record['raw_artifacts']['response'] = str(folder / 'wire.response.raw.json')
        try:
            data = json.loads(response.get('raw') or '{}')
        except (TypeError, ValueError):
            if transport_failure:
                fail_transport()
            raise runtime.TransportError('invalid_wire_json', 'GLM response is not JSON') from None
        if not isinstance(data, dict):
            if transport_failure:
                fail_transport()
            raise runtime.TransportError('invalid_wire_json', 'GLM response is not an object')
        provider_error = data.get('error')
        if isinstance(provider_error, dict):
            record['provider_error_code'] = str(provider_error.get('code', ''))
            # Diagnostic message is redacted by the inherited recorder.
            record['provider_error_events'] = [deepcopy(provider_error)]
        code = record.get('provider_error_code')
        if code == '1113':
            record['provider_limit_signal'] = 'account_billing_unavailable'
        elif response.get('http_status') == 429 or code in ('1302', '1305'):
            record['provider_limit_signal'] = ('platform_overload_1305' if code == '1305'
                                               else 'account_rate_limit_1302' if code == '1302'
                                               else 'http_429')
        record['model_reported'] = data.get('model')
        record['service_tier_reported'] = data.get('service_tier')
        usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
        metrics = record.get('stream_metrics') or {}
        stream_complete = all(metrics.get(key) is True for key in (
            'completed', 'saw_done', 'all_choices_finished'))
        if usage and not (stream_complete and metrics.get('terminal_usage_confirmed') is True):
            record['observed_partial_usage'] = _base._diagnostic_redact(deepcopy(usage), secrets)
            record['observed_partial_usage_is_final'] = False
            usage = {}
        details = usage.get('prompt_tokens_details') or {}
        output_details = usage.get('completion_tokens_details') or {}
        integer = runtime.original._integer
        record['input_tokens'] = integer(usage.get('prompt_tokens'))
        record['cached_input_tokens'] = integer(details.get('cached_tokens')) if isinstance(details, dict) else None
        record['output_tokens'] = integer(usage.get('completion_tokens'))
        record['reasoning_tokens'] = integer(output_details.get('reasoning_tokens')) if isinstance(output_details, dict) else None
        record['total_tokens'] = integer(usage.get('total_tokens'))
        record['usage_source'] = 'glm.stream.terminal_usage' if usage else None
        record['total_tokens_source'] = 'glm.stream.terminal_usage.total_tokens' if record['total_tokens'] is not None else None
        if record['total_tokens'] is None and record['input_tokens'] is not None and record['output_tokens'] is not None:
            record['total_tokens'] = record['input_tokens'] + record['output_tokens']
            record['total_tokens_source'] = 'derived_input_plus_output'
        # A local read timeout or connection error is not a provider rejection.
        # Only usage confirmed at the complete stream boundary settles accounting.
        if transport_failure:
            fail_transport()
        if wire_error or provider_error:
            raise runtime.TransportError('provider_rejected',
                f"GLM Coding Plan HTTP {response.get('http_status')}; provider code {code or 'unknown'}"
                + (f"; {wire_message}" if wire_message else ''))
        if not stream_complete:
            raise runtime.TransportError('incomplete_stream',
                'GLM stream did not confirm all finish boundaries and DONE; partial actions withheld')
        if record['model_reported'] != record['model_requested']:
            raise runtime.TransportError('model_echo_mismatch', 'GLM model echo differs from requested model')
        choices = data.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise runtime.TransportError('invalid_wire_response', 'GLM returned no valid choice')
        record['stop_reason'] = choices[0].get('finish_reason')
        if any(not isinstance(choice, dict) or choice.get('finish_reason') not in ('stop', 'tool_calls')
               for choice in choices):
            raise runtime.TransportError('model_output_incomplete',
                'GLM output did not finish normally; truncated or filtered actions withheld')
        assistant = choices[0].get('message')
        if not isinstance(assistant, dict) or assistant.get('role', 'assistant') != 'assistant':
            raise runtime.TransportError('invalid_wire_response', 'GLM response is not an assistant message')
        if assistant.get('content') is not None and not isinstance(assistant['content'], str):
            raise runtime.TransportError('invalid_wire_response', 'GLM content must be text or null')
        record['assistant_message'] = deepcopy(assistant)
        record['assistant_message'].setdefault('role', 'assistant')
        record['text'] = assistant.get('content') or ''
        calls = assistant.get('tool_calls')
        record['native_tool_calls_requested'] = len(calls) if isinstance(calls, list) else None
    return glm


def _load_v2():
    runtime = _load_original_v2()
    runtime.V2Transport._glm_v2 = _coding_call(runtime)
    return runtime


_base._load_v2 = _load_v2
_base._write_once = _write_once
_base._run_process = _run_process


class CodingPlanTransport(_base.SweTransport):
    def __init__(self, root, *, base_url=CODING_GLM_BASE_URL,
                 glm_read_timeout_seconds=None, glm_cm_read_timeout_seconds=None):
        self.glm_base_url = validate_endpoint(base_url)
        self.configured_glm_base_url = configured_glm_base_url()
        if self.glm_base_url != self.configured_glm_base_url:
            raise ValueError('Requested and configured Coding Plan endpoints must match')
        self.transport_policy, self.transport_policy_sha256 = read_transport_policy()
        if glm_read_timeout_seconds is None:
            glm_read_timeout_seconds = self.transport_policy['glm_read_timeout_seconds']
        if glm_cm_read_timeout_seconds is None:
            glm_cm_read_timeout_seconds = self.transport_policy['cm_read_timeout_seconds']
        for name, value in [('glm_read_timeout_seconds', glm_read_timeout_seconds),
                            ('glm_cm_read_timeout_seconds', glm_cm_read_timeout_seconds)]:
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 < value <= 7200):
                raise ValueError(name + ' must be finite and in (0, 7200]')
            setattr(self, name, value)
        super().__init__(root)

    def _log(self, value):
        return super()._log(_stamp(value))

    def total_timeout_limit(self, model, role):
        if role == 'cm':
            return self.transport_policy['cm_total_timeout_seconds']
        key = 'glm_total_timeout_seconds' if model in GLM_MODELS else 'other_model_total_timeout_seconds'
        return self.transport_policy[key]

    def complete(self, model, messages, **kwargs):
        requested = kwargs.get('timeout_seconds', 600)
        numeric = type(requested) in (int, float) and math.isfinite(requested)
        cap = self.total_timeout_limit(model, kwargs.get('role'))
        valid = numeric and requested > 0
        effective = min(requested, cap) if valid else requested
        # Never extend a caller's remaining episode budget, even for GLM.
        kwargs['timeout_seconds'] = effective
        policy = {'policy_sha256': self.transport_policy_sha256,
                  'protocol': self.transport_policy['protocol'],
                  'configured_glm_base_url': self.configured_glm_base_url,
                  'requested_seconds': requested if numeric else None,
                  'effective_seconds': effective if valid else None,
                  'model_cap_seconds': cap,
                  'read_timeout_seconds': (self.glm_cm_read_timeout_seconds
                      if kwargs.get('role') == 'cm' else self.glm_read_timeout_seconds)}
        token = _CALL_POLICY.set(policy)
        try:
            return _stamp(super().complete(model, messages, **kwargs))
        finally:
            _CALL_POLICY.reset(token)


# Deprecated source aliases only. They route exclusively to Coding Plan and
# reject an explicit standard API URL; historical frozen modules stay unchanged.
StandardApiTransport = CodingPlanTransport
STANDARD_GLM_BASE_URL = CODING_GLM_BASE_URL
STANDARD_GLM_ENDPOINT = CODING_GLM_ENDPOINT
SweTransport = CodingPlanTransport
CallIdentityError = _base.CallIdentityError
