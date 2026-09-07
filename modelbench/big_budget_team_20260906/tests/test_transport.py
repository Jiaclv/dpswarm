"""Offline Coding Plan route tests. HTTP subprocess output is entirely synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.big_budget_team_20260906 import transport as t

KEY = 'sk-fixture-standard-key-12345'
TOOLS = [{'type': 'function', 'function': {'name': 'read', 'description': 'fixture',
          'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                         'required': ['path'], 'additionalProperties': False}}}]


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    loader = t._load_original_v2
    def load():
        runtime = loader()
        runtime.original._keyconfig = lambda name: {
            'GLM_API_KEY': KEY,
            # The configured Coding endpoint must agree with the actual wire route.
            'GLM_BASE_URL': 'https://open.bigmodel.cn/api/coding/paas/v4',
        }.get(name)
        return runtime
    monkeypatch.setattr(t, '_load_original_v2', load)
    return t.CodingPlanTransport(tmp_path/'calls')


def invoke(adapter, model='glm-5.3-flash', **kwargs):
    return adapter.complete(model, [{'role': 'user', 'content': 'offline fixture'}],
        tools=TOOLS, run_id='fixture', role=kwargs.pop('role', 'worker'),
        task_id='fixture-task', call_id=kwargs.pop('call_id', 'once'), **kwargs)


def response(model='glm-5.3-flash', usage=True):
    value = {'model': model, 'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': None, 'reasoning_content': 'fixture reasoning',
        'tool_calls': [{'id': 'read-one', 'type': 'function',
                        'function': {'name': 'read', 'arguments': '{"path":"a.py"}'}}]}}]}
    if usage:
        value['usage'] = {'prompt_tokens': 100, 'completion_tokens': 20,
                         'prompt_tokens_details': {'cached_tokens': 30},
                         'completion_tokens_details': {'reasoning_tokens': 10}}
    return value


def stream_metrics(usage=True, **changes):
    return dict({'completed': True, 'saw_done': True, 'all_choices_finished': True,
                 'terminal_usage_confirmed': usage, 'usage_chunk_received': usage,
                 'finish_reason': 'tool_calls'}, **changes)


def fake_http(monkeypatch, body, status=200):
    observed = []
    def run(argv, **kwargs):
        assert '--http-worker' in argv
        kwargs['record']['transport_attempt_count'] = 1
        observed.append(json.loads(kwargs['input']))
        envelope = {'http_status': status, 'raw': json.dumps(body),
                    'stream_metrics': stream_metrics(usage=isinstance(body, dict) and bool(body.get('usage')))}
        if status >= 400:
            envelope['error_code'] = 'http_error'
        return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), '')
    monkeypatch.setattr(t, '_run_process_original', run)
    return observed


@pytest.mark.parametrize('model', ['glm-5.3', 'glm-5.3-flash'])
def test_coding_endpoint_native_history_usage_and_immutable_journal(adapter, monkeypatch, model):
    observed = fake_http(monkeypatch, response(model))
    result = invoke(adapter, model)
    wire = observed[0]
    assert wire['endpoint'] == t.CODING_GLM_ENDPOINT
    payload = json.loads(wire['payload'])
    assert payload['model'] == model and payload['reasoning_effort'] == 'max'
    assert payload['tools'] == TOOLS and payload['thinking'] == {'type': 'enabled'}
    assert result['model_reported'] == model and result['error'] is None
    assert result['parsed_calls'][0]['arguments'] == {'path': 'a.py'}
    assert result['total_tokens'] == 120 and result['cached_input_tokens'] == 30
    assert result['reasoning_tokens'] == 10 and result['history_continuation_safe'] is True
    assert result['endpoint'] == t.CODING_GLM_ENDPOINT
    rows = [json.loads(line) for line in (adapter.root/'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [row['event'] for row in rows] == ['started', 'completed']
    assert all(row['adapter_mode'] == t.GLM_ADAPTER for row in rows)
    metadata = json.loads(Path(result['raw_artifacts']['metadata']).read_text(encoding='utf-8'))
    assert metadata['provider_channel'] == 'bigmodel_coding_plan'
    assert metadata['endpoint'] == result['endpoint']
    assert KEY not in ''.join(p.read_text(encoding='utf-8') for p in adapter.root.rglob('*') if p.is_file())
    with pytest.raises(t.CallIdentityError):
        invoke(adapter, model)
    assert len(observed) == 1


def test_missing_usage_stays_unknown_and_cm_disables_thinking(adapter, monkeypatch):
    observed = fake_http(monkeypatch, response(usage=False))
    result = invoke(adapter, role='cm')
    assert result['total_tokens'] is None and result['usage_complete'] is False
    assert json.loads(observed[0]['payload'])['thinking'] == {'type': 'disabled'}


@pytest.mark.parametrize('status,code,signal', [(429, '1302', 'account_rate_limit_1302'),
    (400, '1302', 'account_rate_limit_1302'), (503, '1305', 'platform_overload_1305'),
    (429, 'other', 'http_429')])
def test_rate_limits_are_recorded_without_retry_or_fallback(adapter, monkeypatch, status, code, signal):
    seen = fake_http(monkeypatch, {'error': {'code': code, 'message': 'provider fixture'}}, status)
    result = invoke(adapter)
    assert result['error']['code'] == 'provider_rejected'
    assert result['provider_error_code'] == code and result['provider_limit_signal'] == signal
    assert result['http_status'] == status and result['transport_attempt_count'] == 1
    assert result['total_tokens'] is None and len(seen) == 1
    assert result['automatic_channel_fallback'] is False


def test_model_mismatch_is_failure_but_observed_usage_is_retained(adapter, monkeypatch):
    seen = fake_http(monkeypatch, response('glm-5.3'))
    result = invoke(adapter, 'glm-5.3-flash')
    assert result['error']['code'] == 'model_echo_mismatch'
    assert result['model_reported'] == 'glm-5.3' and result['total_tokens'] == 120
    assert len(seen) == 1


@pytest.mark.parametrize('base', [None, 'https://open.bigmodel.cn/api/paas/v4',
    'https://example.com/api/paas/v4', 'https://user:secret@open.bigmodel.cn/api/paas/v4',
    t.CODING_GLM_BASE_URL+'?key=secret', t.CODING_GLM_BASE_URL+'#fragment'])
def test_reject_noncoding_endpoint_before_creating_artifacts(tmp_path, base):
    target = tmp_path/'not-created'
    with pytest.raises(ValueError):
        t.CodingPlanTransport(target, base_url=base)
    assert not target.exists()


def test_historical_transport_loader_is_not_modified():
    from modelbench.swe_verified_20260903 import transport as historical
    assert historical.SweTransport is not t.CodingPlanTransport
    assert historical._load_v2 is not t._load_v2
    runtime = historical._load_v2()
    assert runtime.V2Transport._glm_v2.__name__ == '_glm_v2'


def test_rejected_response_retains_observed_usage(adapter, monkeypatch):
    body = response(); body['error'] = {'code': '1305', 'message': 'fixture'}
    seen = fake_http(monkeypatch, body, 503)
    result = invoke(adapter)
    assert result['error']['code'] == 'provider_rejected'
    assert result['total_tokens'] == 120 and result['usage_complete'] is True
    assert len(seen) == 1


def test_non_json_http_429_is_recorded(adapter, monkeypatch):
    seen = fake_http(monkeypatch, 'busy', 429)
    result = invoke(adapter)
    assert result['error']['code'] == 'invalid_wire_json'
    assert result['provider_limit_signal'] == 'http_429'
    assert result['http_status'] == 429 and len(seen) == 1



def fake_envelope(monkeypatch, envelope):
    observed = []
    def run(argv, **kwargs):
        assert '--http-worker' in argv
        kwargs['record']['transport_attempt_count'] = 1
        observed.append(json.loads(kwargs['input']))
        return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), '')
    monkeypatch.setattr(t, '_run_process_original', run)
    return observed


@pytest.mark.parametrize('status,raw', [(None, None), (200, '{"partial":')])
def test_socket_timeout_keeps_diagnosis_raw_evidence_and_unknown_usage(adapter, monkeypatch, status, raw):
    envelope = {'error_code': 'socket_timeout', 'error_message': 'TimeoutError: The read operation timed out'}
    if status is not None:
        envelope['http_status'] = status
    if raw is not None:
        envelope['raw'] = raw
    seen = fake_envelope(monkeypatch, envelope)
    result = invoke(adapter)
    assert result['error']['code'] == result['timeout_kind'] == 'socket_timeout'
    assert result['error']['message'] == envelope['error_message']
    assert result['http_status'] == status
    assert result['http_exchange_error']['error_code'] == 'socket_timeout'
    assert result['http_exchange_error']['error_message'] == envelope['error_message']
    assert result['total_tokens'] is None and result['input_tokens'] is None and result['output_tokens'] is None
    assert result['usage_complete'] is False and result['retry_attempted'] is False
    assert result['transport_attempt_count'] == 1 and len(seen) == 1
    raw_stdout = json.loads(Path(result['raw_artifacts']['http_worker_stdout']).read_text(encoding='utf-8'))
    assert raw_stdout == envelope
    if raw is not None:
        assert Path(result['raw_artifacts']['response']).read_text(encoding='utf-8') == raw
    metadata = json.loads(Path(result['raw_artifacts']['metadata']).read_text(encoding='utf-8'))
    assert metadata['error'] == result['error'] and metadata['timeout_kind'] == 'socket_timeout'


def test_network_failure_is_not_provider_rejection_and_diagnostics_are_redacted(adapter, monkeypatch):
    message = 'URLError: connection reset; Authorization: Bearer reflected-token; ' + KEY
    seen = fake_envelope(monkeypatch, {'error_code': 'network_error', 'error_message': message})
    result = invoke(adapter)
    assert result['error']['code'] == 'network_error' and result['timeout_kind'] is None
    assert result['http_status'] is None and result['total_tokens'] is None
    assert 'connection reset' in result['error']['message']
    assert 'connection reset' in result['http_exchange_error']['error_message']
    assert len(seen) == 1 and result['automatic_channel_fallback'] is False
    texts = ''.join(p.read_text(encoding='utf-8') for p in adapter.root.rglob('*') if p.is_file())
    assert KEY not in texts and 'reflected-token' not in texts


def test_transport_failure_keeps_partial_usage_unsettled(adapter, monkeypatch):
    seen = fake_envelope(monkeypatch, {'http_status': 200, 'raw': json.dumps(response()),
        'error_code': 'socket_timeout', 'error_message': 'TimeoutError: fixture late read timeout'})
    result = invoke(adapter)
    assert result['error']['code'] == 'socket_timeout' and result['timeout_kind'] == 'socket_timeout'
    assert result['total_tokens'] is None and result['usage_complete'] is False
    assert result['observed_partial_usage'] == response()['usage']
    assert result['observed_partial_usage_is_final'] is False
    assert result['assistant_message'] is None and result['action'] is None
    assert len(seen) == 1


def test_bound_policy_allows_900_read_with_960_total_and_explicit_tool_streaming(adapter, monkeypatch):
    seen = fake_http(monkeypatch, response())
    result = invoke(adapter, timeout_seconds=960)
    assert result['error'] is None
    assert result['glm_read_timeout_seconds_configured'] == 900
    assert result['glm_read_timeout_seconds_effective'] == seen[0]['socket_timeout_seconds'] == 900
    assert result['socket_timeout_seconds'] == 900 and result['glm_stream'] is True and result['glm_tool_stream'] is True
    assert result['total_deadline_seconds_requested'] == result['total_deadline_seconds_effective'] == 960
    assert result['total_deadline_seconds'] == 960 and result['model_total_timeout_limit_seconds'] == 960
    assert result['transport_policy_sha256'] == t.read_transport_policy()[1]
    assert json.loads(seen[0]['payload'])['stream'] is True
    assert json.loads(seen[0]['payload'])['tool_stream'] is True


@pytest.mark.parametrize('role,total,expected_cap', [('worker', 600, 600), ('lead', 90, 90), ('cm', 600, 120)])
def test_explicit_read_timeout_is_clipped_to_total_deadline(adapter, tmp_path, monkeypatch, role, total, expected_cap):
    configured = t.CodingPlanTransport(tmp_path / 'configured', glm_read_timeout_seconds=900)
    seen = fake_http(monkeypatch, response())
    result = invoke(configured, role=role, timeout_seconds=total)
    assert result['error'] is None and result['total_deadline_seconds'] == total
    assert result['glm_read_timeout_seconds_configured'] == (120 if role == 'cm' else 900)
    effective = result['glm_read_timeout_seconds_effective']
    assert 0 < effective <= expected_cap
    assert effective == result['socket_timeout_seconds'] == seen[0]['socket_timeout_seconds']
    assert json.loads(seen[0]['payload'])['stream'] is True
    assert json.loads(seen[0]['payload'])['tool_stream'] is True
    events = [json.loads(line) for line in (configured.root / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert events[0]['glm_read_timeout_seconds_configured'] == result['glm_read_timeout_seconds_configured']
    assert events[0]['glm_read_timeout_seconds_effective'] is None
    assert events[-1]['glm_read_timeout_seconds_effective'] == effective


def test_long_caller_deadline_is_clipped_to_glm_policy(adapter, tmp_path, monkeypatch):
    configured = t.CodingPlanTransport(tmp_path / 'configured', glm_read_timeout_seconds=900)
    seen = fake_http(monkeypatch, response())
    result = invoke(configured, timeout_seconds=1200)
    assert result['error'] is None and len(seen) == 1
    assert result['glm_read_timeout_seconds_configured'] == 900
    assert result['glm_read_timeout_seconds_effective'] == 900
    assert result['total_deadline_seconds_requested'] == 1200
    assert result['total_deadline_seconds_effective'] == result['total_deadline_seconds'] == 960
    assert result['transport_attempt_count'] == 1


def test_outer_deadline_remains_enforced_with_long_read_setting(adapter, tmp_path, monkeypatch):
    configured = t.CodingPlanTransport(tmp_path / 'configured', glm_read_timeout_seconds=900)
    seen = []
    def run(argv, **kwargs):
        kwargs['record']['transport_attempt_count'] = 1
        seen.append(json.loads(kwargs['input']))
        raise t._base._Interrupted(argv, 'total_deadline', '', 'offline bounded deadline')
    monkeypatch.setattr(t, '_run_process_original', run)
    result = invoke(configured, timeout_seconds=90)
    assert result['error']['code'] == result['timeout_kind'] == 'total_deadline'
    assert result['total_tokens'] is None and result['retry_attempted'] is False
    assert len(seen) == 1 and 0 < seen[0]['socket_timeout_seconds'] <= 90


@pytest.mark.parametrize('setting', [0, -1, True, float('nan'), float('inf'), 7201, '900'])
def test_invalid_read_timeout_fails_before_creating_artifacts(tmp_path, setting):
    target = tmp_path / 'not-created'
    with pytest.raises(ValueError, match='must be finite'):
        t.CodingPlanTransport(target, glm_read_timeout_seconds=setting)
    assert not target.exists()


def test_concurrent_adapters_record_their_own_timeout_settings(adapter, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    barrier = threading.Barrier(2)
    configured = [t.CodingPlanTransport(tmp_path / 'short', glm_read_timeout_seconds=125),
                  t.CodingPlanTransport(tmp_path / 'long', glm_read_timeout_seconds=900)]
    def run(argv, **kwargs):
        kwargs['record']['transport_attempt_count'] = 1
        barrier.wait(timeout=5)
        return subprocess.CompletedProcess(argv, 0, json.dumps({'http_status': 200, 'raw': json.dumps(response()), 'stream_metrics': stream_metrics()}), '')
    monkeypatch.setattr(t, '_run_process_original', run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: invoke(item, timeout_seconds=600), configured))
    for instance, result, setting in zip(configured, results, (125, 900)):
        assert result['error'] is None and result['glm_read_timeout_seconds_configured'] == setting
        rows = [json.loads(line) for line in (instance.root / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        assert all(row['glm_read_timeout_seconds_configured'] == setting for row in rows)
    assert results[0]['glm_read_timeout_seconds_effective'] == 125
    assert 0 < results[1]['glm_read_timeout_seconds_effective'] <= 600



@pytest.mark.parametrize('model,role,requested,expected', [
    ('gpt-5.6-sol', 'lead', 960, 600), ('gpt-5.6-terra', 'worker', 960, 600),
    ('gpt-5.6-luna', 'lead', 960, 600), ('deepseek-v4-flash', 'cm', 960, 600),
    ('glm-5.3', 'cm', 960, 600), ('glm-5.3-flash', 'lead', 960, 960),
    ('glm-5.3', 'worker', 45, 45), ('gpt-5.6-sol', 'lead', 45, 45),
])
def test_model_policy_clips_before_inherited_transport_and_never_extends_remaining_time(adapter, monkeypatch, model, role, requested, expected):
    seen = []
    def complete(self, requested_model, messages, **kwargs):
        seen.append((requested_model, kwargs['timeout_seconds']))
        return {'call_id': kwargs['call_id'], 'model_requested': requested_model,
                'total_deadline_seconds': kwargs['timeout_seconds']}
    monkeypatch.setattr(t._base.SweTransport, 'complete', complete)
    record = invoke(adapter, model, role=role, timeout_seconds=requested)
    assert seen == [(model, expected)]
    assert record['total_deadline_seconds_requested'] == requested
    assert record['total_deadline_seconds_effective'] == record['total_deadline_seconds'] == expected
    assert record['transport_policy_sha256'] == adapter.transport_policy_sha256


def test_policy_drift_is_rejected_before_artifacts(tmp_path, monkeypatch):
    policy, _ = t.read_transport_policy()
    policy['glm_total_timeout_seconds'] = 1200
    source = tmp_path / 'changed-policy.json'
    source.write_text(json.dumps(policy), encoding='utf-8')
    monkeypatch.setattr(t, 'TRANSPORT_POLICY_PATH', source)
    target = tmp_path / 'calls'
    with pytest.raises(ValueError, match='approved transport contract'):
        t.CodingPlanTransport(target)
    assert not target.exists()


def test_private_worker_routes_glm_only_and_preserves_supervision(adapter, monkeypatch):
    observed = []
    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{}', '')
    monkeypatch.setattr(t, '_run_process_original', run)
    original_source = t._base.V2_SOURCE
    directory = adapter.root / 'routing'
    common = {'cwd': directory / 'empty-cwd', 'deadline_at': 123.456,
              'cancel_event': object(), 'record': {'raw_artifacts': {'directory': str(directory)}}}
    base_argv = [sys.executable, '-I', str(original_source), '--http-worker']
    jobs = [
        {'endpoint': t.CODING_GLM_ENDPOINT, 'payload': json.dumps({'model': 'glm-5.3', 'stream': True}), 'key': KEY},
        {'endpoint': 'https://api.deepseek.com/chat/completions', 'payload': json.dumps({'model': 'deepseek-v4-flash', 'stream': False}), 'key': KEY},
        {'endpoint': t.CODING_GLM_ENDPOINT, 'payload': json.dumps({'model': 'glm-5.3', 'stream': False}), 'key': KEY},
    ]
    for job in jobs:
        t._run_process(base_argv, input=json.dumps(job), **common)
    assert observed[0][0] == [sys.executable, '-I', str(t.GLM_STREAM_WORKER), '--http-worker']
    assert json.loads(observed[0][1]['input'])['evidence_dir'] == str(directory / 'glm-stream')
    assert all(item[0] == base_argv for item in observed[1:])
    assert all(item[1]['deadline_at'] == common['deadline_at'] and item[1]['cancel_event'] is common['cancel_event'] for item in observed)
    assert t._base.V2_SOURCE == original_source
    from modelbench.swe_verified_20260903 import transport as historical
    assert historical._run_process is not t._run_process


@pytest.mark.parametrize('missing', ['completed', 'saw_done', 'all_choices_finished'])
def test_partial_stream_never_exposes_parseable_tool_action(adapter, monkeypatch, missing):
    metrics = stream_metrics(**{missing: False})
    seen = fake_envelope(monkeypatch, {'http_status': 200, 'raw': json.dumps(response()), 'stream_metrics': metrics})
    result = invoke(adapter)
    assert result['error']['code'] == 'incomplete_stream'
    assert result['action'] is None and result['assistant_message'] is None and not result['parsed_calls']
    assert result['total_tokens'] is None and result['observed_partial_usage'] == response()['usage']
    assert len(seen) == 1


def test_missing_stream_metrics_rejects_actions_and_usage(adapter, monkeypatch):
    fake_envelope(monkeypatch, {'http_status': 200, 'raw': json.dumps(response())})
    result = invoke(adapter)
    assert result['error']['code'] == 'incomplete_stream'
    assert result['total_tokens'] is None and result['action'] is None


@pytest.mark.parametrize('reason', ['length', 'content_filter', None])
def test_complete_stream_truncated_or_filtered_output_retains_usage_but_blocks_action(adapter, monkeypatch, reason):
    body = response()
    body['choices'][0]['finish_reason'] = reason
    fake_envelope(monkeypatch, {'http_status': 200, 'raw': json.dumps(body), 'stream_metrics': stream_metrics(finish_reason=reason)})
    result = invoke(adapter)
    assert result['error']['code'] == 'model_output_incomplete'
    assert result['stop_reason'] == reason
    assert result['total_tokens'] == 120 and result['reasoning_tokens'] == 10
    assert result['action'] is None and result['assistant_message'] is None and not result['parsed_calls']


@pytest.mark.parametrize('code', ['invalid_tool_arguments', 'incomplete_tool_call', 'action_redaction_mismatch'])
def test_worker_action_validation_error_retains_terminal_usage_without_action(adapter, monkeypatch, code):
    body = response(); body['choices'] = []
    fake_envelope(monkeypatch, {'http_status': 200, 'raw': json.dumps(body),
        'error_code': code, 'error_message': 'worker rejected action', 'stream_metrics': stream_metrics()})
    result = invoke(adapter)
    assert result['error']['code'] == code and result['total_tokens'] == 120
    assert result['usage_complete'] is True
    assert result['action'] is None and result['assistant_message'] is None and not result['parsed_calls']


def test_done_without_terminal_usage_allows_finished_action_with_unknown_accounting(adapter, monkeypatch):
    fake_envelope(monkeypatch, {'http_status': 200, 'raw': json.dumps(response(usage=False)),
                               'stream_metrics': stream_metrics(usage=False)})
    result = invoke(adapter)
    assert result['error'] is None and result['parsed_calls'][0]['arguments'] == {'path': 'a.py'}
    assert result['total_tokens'] is None and result['usage_complete'] is False


def test_outer_deadline_recovers_progress_and_event_fingerprints_without_partial_usage(adapter, monkeypatch):
    def run(argv, **kwargs):
        job = json.loads(kwargs['input'])
        directory = Path(job['evidence_dir']); directory.mkdir(parents=True)
        (directory / 'stream.events.jsonl').write_text('{"chunk":1,"content_sha256":"fixture"}\n', encoding='utf-8')
        (directory / 'stream.progress.json').write_text(json.dumps({
            'stream_metrics': stream_metrics(completed=False, saw_done=False, terminal_usage_confirmed=False, chunk_count=42),
            'stream_artifacts': {'raw_events': str(directory / 'stream.events.jsonl'), 'progress': str(directory / 'stream.progress.json')},
            'usage': {'total_tokens': 999}, 'assistant_message': {'content': 'must not recover'}}), encoding='utf-8')
        kwargs['record']['transport_attempt_count'] = 1
        raise t._base._Interrupted(argv, 'total_deadline', '', 'synthetic deadline')
    monkeypatch.setattr(t, '_run_process_original', run)
    result = invoke(adapter, timeout_seconds=960)
    assert result['error']['code'] == result['timeout_kind'] == 'total_deadline'
    assert result['stream_metrics']['chunk_count'] == 42
    assert result['stream_metrics']['completed'] is False
    assert set(result['stream_artifacts']) == {'raw_events', 'progress'}
    assert all(Path(path).is_file() for path in result['stream_artifacts'].values())
    assert result['total_tokens'] is None and result['action'] is None and result['assistant_message'] is None
    assert result['transport_attempt_count'] == 1 and result['retry_attempted'] is False


def test_worker_cannot_link_artifacts_outside_its_call_directory(adapter, tmp_path, monkeypatch):
    outside = tmp_path / 'outside.txt'; outside.write_text('do not inspect', encoding='utf-8')
    fake_envelope(monkeypatch, {'http_status': 200, 'raw': json.dumps(response()),
        'stream_metrics': stream_metrics(), 'stream_artifacts': {'raw_events': str(outside)}})
    result = invoke(adapter)
    assert result['error'] is None
    assert result['stream_artifact_error'] == 'Worker artifact outside the expected call-local paths'
    assert str(outside) not in result['stream_artifacts'].values()
    assert str(outside) not in result['raw_artifacts'].values()


def test_stream_protocol_is_recorded_in_started_and_terminal_artifacts(adapter, monkeypatch):
    fake_http(monkeypatch, response())
    result = invoke(adapter)
    assert result['transport_protocol'] == 'big_budget_transport_coding_stream_v4'
    assert result['adapter_mode'] == 'glm_coding_plan_stream_native_tools_swe_v4'
    assert result['http_worker_script'] == str(t.GLM_STREAM_WORKER)
    rows = [json.loads(line) for line in (adapter.root / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert all(row['glm_stream'] is True and row['glm_tool_stream'] is True for row in rows)
    assert all(row['transport_protocol'] == result['transport_protocol'] for row in rows)


@pytest.mark.parametrize('mode,expected_error,expected_usage', [
    ('complete', None, 120), ('missing_done', 'incomplete_stream', None),
    ('length', 'model_output_incomplete', 120),
    ('credential_action', 'action_redaction_mismatch', 120),
])
def test_real_stream_aggregator_integrates_with_adapter_offline(adapter, monkeypatch, mode, expected_error, expected_usage):
    import io
    from modelbench.big_budget_team_20260906 import glm_stream_http_worker as worker
    arguments = json.dumps({'path': KEY if mode == 'credential_action' else 'a.py'})
    chunks = [
        {'model': 'glm-5.3-flash', 'choices': [{'index': 0, 'delta': {'role': 'assistant',
            'tool_calls': [{'index': 0, 'id': 'read-one', 'type': 'function',
                            'function': {'name': 'read', 'arguments': arguments[:8]}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0,
            'function': {'arguments': arguments[8:]}}]}}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'length' if mode == 'length' else 'tool_calls'}],
         'usage': dict(response()['usage'], total_tokens=120)},
    ]
    payload = ''.join('data: '+json.dumps(item)+'\n\n' for item in chunks)
    if mode != 'missing_done':
        payload += 'data: [DONE]\n\n'
    class Reply(io.BytesIO):
        status = 200
    seen = []
    def opener(request, timeout):
        body = json.loads(request.data)
        assert body['stream'] is True and body['tool_stream'] is True
        seen.append((request.full_url, timeout))
        return Reply(payload.encode('utf-8'))
    def run(argv, **kwargs):
        assert str(t.GLM_STREAM_WORKER) in argv
        kwargs['record']['transport_attempt_count'] = 1
        result = worker.run_job(json.loads(kwargs['input']), opener=opener)
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), '')
    monkeypatch.setattr(t, '_run_process_original', run)
    result = invoke(adapter, timeout_seconds=960)
    assert (result['error']['code'] if result['error'] else None) == expected_error
    assert result['total_tokens'] == expected_usage
    assert result['stream_metrics']['saw_done'] is (mode != 'missing_done')
    assert set(result['stream_artifacts']) == {'raw_events', 'progress'}
    assert result['raw_artifacts']['glm_stream_raw_events'] == result['stream_artifacts']['raw_events']
    if expected_error is None:
        assert result['parsed_calls'][0]['arguments'] == {'path': 'a.py'}
    else:
        assert not result['parsed_calls'] and result['action'] is None and result['assistant_message'] is None
    texts = ''.join(path.read_text(encoding='utf-8') for path in adapter.root.rglob('*') if path.is_file())
    assert KEY not in texts
    assert len(seen) == 1 and result['transport_attempt_count'] == 1


@pytest.mark.parametrize('configured', [None, 'https://open.bigmodel.cn/api/paas/v4',
    'https://example.com/api/coding/paas/v4', 'https://open.bigmodel.cn/api/coding/paas/v4?key=forbidden'])
def test_configured_endpoint_must_be_coding_before_artifact_creation(tmp_path,monkeypatch,configured):
    loader=t._load_original_v2
    def load():
        runtime=loader();runtime.original._keyconfig=lambda name: configured if name=='GLM_BASE_URL' else KEY
        return runtime
    monkeypatch.setattr(t,'_load_original_v2',load)
    target=tmp_path/'should-not-exist'
    with pytest.raises(ValueError):
        t.CodingPlanTransport(target)
    assert not target.exists()


def test_config_endpoint_drift_is_refused_before_http(adapter,monkeypatch):
    loader=t._load_original_v2
    def changed():
        runtime=loader();runtime.original._keyconfig=lambda name: 'https://open.bigmodel.cn/api/paas/v4' if name=='GLM_BASE_URL' else KEY
        return runtime
    monkeypatch.setattr(t,'_load_original_v2',changed)
    seen=fake_http(monkeypatch,response())
    result=invoke(adapter)
    assert result['error']['code']=='configured_endpoint_mismatch'
    assert result['configured_endpoint_verified_at_call'] is False
    assert seen==[] and result['transport_attempt_count']==0


def test_coding_channel_config_and_wire_are_bound(adapter,monkeypatch):
    seen=fake_http(monkeypatch,response())
    result=invoke(adapter)
    assert result['error'] is None and result['provider_channel']=='bigmodel_coding_plan'
    assert result['configured_endpoint_verified_at_call'] is True
    assert result['configured_glm_base_url']==t.CODING_GLM_BASE_URL
    assert seen[0]['endpoint']==t.CODING_GLM_ENDPOINT
    assert adapter.transport_policy['glm_endpoint']==t.CODING_GLM_ENDPOINT
    assert adapter.transport_policy['glm_channel']=='bigmodel_coding_plan'
    assert t.StandardApiTransport is t.CodingPlanTransport
    assert t.STANDARD_GLM_ENDPOINT==t.CODING_GLM_ENDPOINT


def test_balance_error_1113_retains_provider_code_and_signal(adapter,monkeypatch):
    seen=fake_http(monkeypatch,{'error':{'code':'1113','message':'fixture billing unavailable'}},429)
    result=invoke(adapter)
    assert result['provider_error_code']=='1113'
    assert result['provider_limit_signal']=='account_billing_unavailable'
    assert len(seen)==1 and result['error']['code']=='provider_rejected'
