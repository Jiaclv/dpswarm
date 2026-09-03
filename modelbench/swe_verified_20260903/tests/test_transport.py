"""Offline adapter contracts; fake provider bytes, real local cancellation only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.swe_verified_20260903 import transport


TOOLS = [{'type': 'function', 'function': {'name': 'write', 'description': 'fixture tool',
    'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                   'required': ['path', 'content'], 'additionalProperties': False}}}]
FAKE_KEY = 'sk-fixture-credential-123456'


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    original_loader = transport._load_v2
    def load():
        module = original_loader()
        module.original._keyconfig = lambda name: {
            'GLM_API_KEY': FAKE_KEY, 'GLM_BASE_URL': 'https://fixture.invalid/api/coding/paas/v4',
        }.get(name)
        return module
    monkeypatch.setattr(transport, '_load_v2', load)
    return transport.SweTransport(tmp_path / 'transport')


def complete(adapter, model='glm-5.3', messages=None, **changes):
    return adapter.complete(model, messages or [{'role': 'user', 'content': 'fixture only'}],
        tools=TOOLS, run_id='fixture-run', role='worker', task_id='fixture-instance',
        **{'call_id': 'fixture-call', **changes})


def native_response(model='glm-5.3', ident='write-1', arguments=None, usage=True):
    assistant = {'role': 'assistant', 'content': None, 'reasoning_content': 'private fixture reasoning',
                 'tool_calls': [{'id': ident, 'type': 'function', 'function': {'name': 'write',
                    'arguments': arguments if arguments is not None else json.dumps({'path': 'a.py', 'content': 'pass'})}}]}
    value = {'model': model, 'choices': [{'finish_reason': 'tool_calls', 'message': assistant}]}
    if usage:
        value['usage'] = {'prompt_tokens': 130, 'completion_tokens': 40, 'total_tokens': 170,
                          'prompt_tokens_details': {'cached_tokens': 100},
                          'completion_tokens_details': {'reasoning_tokens': 30}}
    return {'http_status': 200, 'raw': json.dumps(value)}, assistant


def codex_output(text=None, extra=None):
    if text is None:
        text = json.dumps({'type': 'tool_calls', 'calls': [{'id': 'write-1', 'name': 'write',
            'arguments': {'path': 'a.py', 'content': 'pass'}}]})
    events = [
        {'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message', 'text': text}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 130, 'cached_input_tokens': 100, 'output_tokens': 40}},
    ]
    return '\n'.join(json.dumps(event) for event in events + (extra or [])) + '\n'


def fake_process(monkeypatch, stdout, seen=None):
    def run(argv, **kwargs):
        kwargs['record']['transport_attempt_count'] = 1
        if seen is not None:
            seen.append({'argv': deepcopy(argv), 'input': kwargs['input'], 'cwd': kwargs['cwd']})
        value = stdout(argv, kwargs) if callable(stdout) else stdout
        if '--output-last-message' in argv and isinstance(value, str):
            # Emulate Codex 0.149.0: the artifact is the last agent_message text.
            def messages():
                for line in value.splitlines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    yield event
            texts = [event['item'].get('text', '') for event in messages()
                     if isinstance(event, dict) and event.get('type') == 'item.completed'
                     and isinstance(event.get('item'), dict) and event['item'].get('type') == 'agent_message']
            if texts:
                Path(argv[argv.index('--output-last-message') + 1]).write_text(texts[-1], encoding='utf-8')
        return subprocess.CompletedProcess(argv, 0, value, '')
    monkeypatch.setattr(transport, '_run_process', run)


@pytest.mark.parametrize('model', ['glm-5.3', 'glm-5.3-flash'])
def test_native_wire_preserves_reasoning_history_schema_usage_and_identity(adapter, monkeypatch, model):
    response, assistant = native_response(model=model)
    seen = []
    fake_process(monkeypatch, json.dumps(response), seen)
    messages = [{'role': 'system', 'content': 'fixture policy'}, assistant,
                {'role': 'tool', 'tool_call_id': 'write-1', 'content': '{"ok":true}'}]
    original = deepcopy(messages)
    record = complete(adapter, model=model, messages=messages)
    wire = json.loads(json.loads(seen[0]['input'])['payload'])
    assert wire['messages'] == messages == original
    assert wire['tools'] == TOOLS and wire['tool_choice'] == 'auto'
    assert wire['thinking'] == {'type': 'enabled'} and wire['reasoning_effort'] == 'max'
    assert wire['temperature'] == 1.0 and wire['max_tokens'] == 32768
    assert record['assistant_message'] == assistant
    assert record['action']['calls'] == [{'id': 'write-1', 'name': 'write', 'arguments': {'path': 'a.py', 'content': 'pass'}}]
    assert record['parsed_calls'] == record['action']['calls']
    assert record['usage'] == dict(input_tokens=130, cached_input_tokens=100, output_tokens=40, reasoning_tokens=30, total_tokens=170)
    assert record['model_requested'] == record['model_reported'] == model
    assert record['effort_requested'] == 'max' and record['effort_reported'] is None
    assert record['service_tier_requested'] is record['service_tier_reported'] is None
    assert record['call_id'] == 'fixture-call' and record['history_continuation_safe'] is True
    assert record['tools_used'] == 0 and record['cap_enforced'] is True
    assert record['error'] is record['protocol_error'] is None
    events = [json.loads(line) for line in (adapter.root / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [event['event'] for event in events] == ['started', 'completed']
    assert events[-1]['call_id'] == record['call_id'] and events[-1]['usage'] == record['usage']
    assert Path(record['raw_artifacts']['response']).is_file()
    assert FAKE_KEY not in ''.join(path.read_text(encoding='utf-8') for path in adapter.root.rglob('*') if path.is_file())


@pytest.mark.parametrize('model', ['gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'])
def test_codex_uses_exact_model_max_fast_no_native_tools_and_nullable_reported_fields(adapter, monkeypatch, model):
    seen = []
    fake_process(monkeypatch, codex_output(), seen)
    messages = [{'role': 'system', 'content': 'fixture loop'}]
    record = complete(adapter, model=model, messages=messages, timeout_seconds=90)
    argv = seen[0]['argv']
    assert argv[argv.index('-m') + 1] == model
    assert 'model_reasoning_effort="max"' in argv and 'service_tier="fast"' in argv
    assert all(value in argv for value in ['--ignore-user-config', '--json', '--ephemeral', 'read-only', 'shell_tool', 'multi_agent'])
    assert 'web_search="disabled"' in argv and 'approval_policy="never"' in argv
    request = json.loads(Path(record['raw_artifacts']['request']).read_text(encoding='utf-8'))
    assert request['timeout_seconds'] == 90 and request['cap_enforced'] is False
    assert str(transport.V2_SOURCE) not in argv and seen[0]['cwd'].name == 'empty-cwd'
    assert transport.text_tool_prompt(TOOLS) in seen[0]['input'] or 'Declared tools:' in seen[0]['input']
    assert messages == [{'role': 'system', 'content': 'fixture loop'}]
    assert record['action']['kind'] == 'tools' and record['tools_used'] == 0
    assert record['model_reported'] is record['effort_reported'] is record['service_tier_reported'] is None
    assert record['effort_requested'] == 'max' and record['service_tier_requested'] == 'fast'
    assert record['total_tokens'] == 170 and record['reasoning_tokens'] is None
    assert record['cap_enforced'] is False and record['usage_complete'] is True


@pytest.mark.parametrize('ident,safe', [(None, False), ('', False), ('   ', False), ('valid-id', True)])
def test_protocol_error_preserves_usage_and_marks_unsafe_native_ids(adapter, monkeypatch, ident, safe):
    response, assistant = native_response(ident=ident, arguments='{"path":' if safe else None)
    fake_process(monkeypatch, json.dumps(response))
    record = complete(adapter)
    assert record['error'] is None and record['response_kind'] == 'protocol_error'
    assert record['action'] is None and record['parsed_calls'] == []
    assert record['protocol_error']['code'] == ('invalid_arguments_json' if safe else 'invalid_call_id')
    assert record['assistant_message'] == assistant and record['total_tokens'] == 170
    assert record['history_continuation_safe'] is safe


@pytest.mark.parametrize('model', ['glm-5.3', 'gpt-5.6-sol'])
def test_plaintext_is_no_action_and_unknown_usage_stays_null(adapter, monkeypatch, model):
    if model.startswith('glm-'):
        value = {'http_status': 200, 'raw': json.dumps({'model': model, 'choices': [
            {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'DONE'}}]})}
        stdout = json.dumps(value)
    else:
        stdout = '\n'.join(json.dumps(event) for event in [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'DONE'}},
            {'type': 'turn.completed'},
        ])
    fake_process(monkeypatch, stdout)
    record = complete(adapter, model=model)
    assert record['action'] == {'kind': 'no_action', 'calls': [], 'text': 'DONE'}
    assert all(value is None for value in record['usage'].values())
    assert record['usage_complete'] is False and 'finished' not in record


def test_model_echo_and_action_redaction_fail_closed_with_observed_usage(adapter, monkeypatch):
    reply, _ = native_response(model='different-model')
    fake_process(monkeypatch, json.dumps(reply))
    mismatch = complete(adapter, call_id='echo-mismatch')
    assert mismatch['error']['code'] == 'model_echo_mismatch' and mismatch['total_tokens'] == 170
    reply, _ = native_response(arguments=json.dumps({'path': 'test.py', 'content': 'api_key = ' + FAKE_KEY}))
    fake_process(monkeypatch, json.dumps(reply))
    redacted = complete(adapter, call_id='redaction')
    assert redacted['error']['code'] == 'action_redaction_mismatch'
    assert redacted['assistant_message'] is None and redacted['action'] is None
    assert redacted['total_tokens'] == 170
    assert FAKE_KEY not in (adapter.root / 'calls.jsonl').read_text(encoding='utf-8')


@pytest.mark.parametrize('code', ['cancelled', 'total_deadline'])
def test_interrupted_codex_retains_complete_usage_already_in_partial_stdout(adapter, monkeypatch, code):
    def interrupted(argv, **kwargs):
        kwargs['record']['transport_attempt_count'] = 1
        raise transport._Interrupted(argv, code, codex_output(), 'fixture cancellation')
    monkeypatch.setattr(transport, '_run_process', interrupted)
    record = complete(adapter, model='gpt-5.6-sol')
    assert record['error']['code'] == code and record['action'] is None
    assert record['total_tokens'] == 170 and record['input_tokens'] == 130
    assert Path(record['raw_artifacts']['stdout']).is_file()
    assert record['timeout_kind'] == ('total_deadline' if code == 'total_deadline' else None)


def test_codex_internal_tool_or_reconnect_events_are_reported(adapter, monkeypatch):
    fake_process(monkeypatch, codex_output(extra=[
        {'type': 'item.completed', 'item': {'id': 'tool-1', 'type': 'command_execution'}},
        {'type': 'status', 'message': 'Reconnecting attempt 2'},
    ]))
    record = complete(adapter, model='gpt-5.6-luna')
    assert record['error'] is not None and record['tools_used'] == 1
    assert record['action'] is None and record['total_tokens'] == 170
    assert record['reconnect_detected'] is True and record['attempt_count'] is None


def test_call_identity_cannot_overwrite_and_concurrent_calls_have_separate_settings(adapter, monkeypatch):
    def output(argv, kwargs):
        if '--http-worker' in argv:
            model = json.loads(json.loads(kwargs['input'])['payload'])['model']
            return json.dumps(native_response(model=model)[0])
        return codex_output()
    fake_process(monkeypatch, output)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(complete, adapter, model=model, call_id=model) for model in transport.MODELS]
        records = [future.result() for future in futures]
    events_before = (adapter.root / 'calls.jsonl').read_bytes()
    with pytest.raises(transport.CallIdentityError):
        complete(adapter, call_id=transport.MODELS[0])
    assert (adapter.root / 'calls.jsonl').read_bytes() == events_before
    events = [json.loads(line) for line in events_before.decode().splitlines()]
    assert len(events) == 10 and len(list((adapter.root / 'calls').iterdir())) == 5
    assert {record['model_requested'] for record in records} == set(transport.MODELS)
    assert all(record['error'] is None for record in records)


@pytest.mark.parametrize('reason', ['cancelled', 'total_deadline'])
def test_real_owned_local_process_cancels_or_times_out_without_provider(reason, tmp_path):
    ready = tmp_path / 'ready.txt'
    code = 'from pathlib import Path; import time; Path(' + repr(str(ready)) + ').write_text("ready"); print("partial",flush=True); time.sleep(20)'
    cancel = threading.Event()
    record = {}
    def cancel_after_start():
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:
            time.sleep(.02)
        if reason == 'cancelled':
            cancel.set()
    thread = threading.Thread(target=cancel_after_start)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(transport._Interrupted) as error:
            transport._run_process([sys.executable, '-I', '-c', code], input='', cwd=tmp_path,
                deadline_at=started + (5 if reason == 'cancelled' else .8), cancel_event=cancel, record=record)
        assert error.value.code == reason and 'partial' in error.value.stdout
        assert record['cancellation']['parent_exited'] is True
        assert record['cancellation']['tree_termination_confirmed'] is True
        assert time.monotonic() - started < 5
    finally:
        thread.join(timeout=5)


def test_pre_cancel_and_invalid_configuration_are_logged_without_launch(adapter, monkeypatch):
    cancel = threading.Event(); cancel.set()
    monkeypatch.setattr(transport.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No process should start'))
    cancelled = complete(adapter, cancel_event=cancel)
    invalid = complete(adapter, call_id='invalid-timeout', timeout_seconds=float('nan'))
    assert cancelled['error']['code'] == 'cancelled' and cancelled['transport_attempt_count'] == 0
    assert cancelled['total_tokens'] is None
    assert invalid['error']['code'] == 'invalid_request' and invalid['transport_attempt_count'] == 0
    assert len((adapter.root / 'calls.jsonl').read_text(encoding='utf-8').splitlines()) == 4


@pytest.mark.parametrize('model,role,condition', [
    ('gpt-5.6-sol', 'lead', 'solo'), ('gpt-5.6-sol', 'lead', 'dpswarm'),
    ('glm-5.3', 'worker', 'dpswarm'), ('glm-5.3-flash', 'worker', 'dpswarm'),
    ('gpt-5.6-terra', 'worker', 'dpswarm'), ('gpt-5.6-luna', 'worker', 'dpswarm'),
])
def test_real_flask_identity_and_legitimate_credential_examples_reach_real_cp(
        adapter, tmp_path, monkeypatch, model, role, condition):
    """The live Flask incident used this exact instance/run identity."""
    from modelbench.swe_verified_20260903.control import SweControl
    from dpswarm.team_runtime.ledger import RunBudget
    task_id = 'pallets__flask-5014'
    run_id = task_id + '__' + condition
    adapter = transport.SweTransport(tmp_path / run_id)
    code = ('api_key = "example_fixture_token_123"\n'
            'slug = "sk-legitimate-source-example"\n'
            'jwt_fixture = "eyJhbGciOiJIUzI1NiJ9.fixture.signature"\n'
            'headers = {"Authorization": "Bearer sk-example-not-a-real-credential"}\n'
            'header_line = "Authorization: Bearer jwt-fixture-example"\n')
    arguments = {'path': 'flask-fixture.py', 'content': code}
    native = model.startswith('glm-')
    seen = []
    output = (json.dumps(native_response(model=model, arguments=json.dumps(arguments))[0]) if native else
        codex_output(json.dumps({'type': 'tool_calls', 'calls': [
            {'id': 'write-1', 'name': 'write', 'arguments': arguments}]})))
    fake_process(monkeypatch, output, seen)
    issue = task_id + ': inspect sk-legitimate-issue-string and api_key variables.'
    messages = [{'role': 'system', 'content': 'Offline fixture'}, {'role': 'user', 'content': issue}]
    if native:
        previous = native_response(model=model, ident='previous', arguments=json.dumps(arguments))[1]
        messages += [previous, {'role': 'tool', 'tool_call_id': 'previous',
                                'content': json.dumps({'stdout': code})}]
    else:
        messages += [{'role': 'user', 'content': 'Tool result: ' + json.dumps({'stdout': code})}]
    before = deepcopy(messages)
    control = SweControl(adapter.root, task_id)
    try:
        handle = control.lead
        if role == 'worker':
            handle = control.delegate(handle, {'model': model, 'task': issue})[0]
            control.activate(handle)
        call_id = str(uuid4())
        budget = RunBudget()
        budget.reserve(call_id, role, 32768)
        record = adapter.complete(model, messages, tools=TOOLS, run_id=run_id, role=role,
                                  task_id=task_id, call_id=call_id)
        assert record['error'] is record['protocol_error'] is None
        assert record['run_id'] == run_id and record['task_id'] == task_id and record['call_id'] == call_id
        assert record['action']['calls'][0]['arguments'] == arguments
        assert messages == before
        budget.complete(call_id, record)
        control.record_call(handle, record)
        assert budget.summary()['total_tokens'] == control.get_usage()['total']['total_tokens'] == 170
        assert control.get_usage()['total']['calls'] == 1
        events = [json.loads(line) for line in (adapter.root / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        assert len(events) == 2 and all(event['run_id'] == run_id and event['task_id'] == task_id for event in events)
        assert json.loads(Path(record['raw_artifacts']['metadata']).read_text(encoding='utf-8')) == record
        prompt = json.loads(Path(record['raw_artifacts']['prompt']).read_text(encoding='utf-8'))
        assert prompt['messages'] == messages
        request = json.loads(Path(record['raw_artifacts']['request']).read_text(encoding='utf-8'))
        if native:
            wire = json.loads(json.loads(seen[0]['input'])['payload'])
            assert wire['messages'] == request['body']['messages'] == messages
        else:
            wire = json.loads(seen[0]['input'].split('\n\n', 1)[1])
            saved = json.loads(request['stdin'].split('\n\n', 1)[1])
            assert wire == saved and wire['messages'][:-1] == messages
        assert record['history_continuation_safe'] is True
    finally:
        control.close()


@pytest.mark.parametrize('model', ['glm-5.3', 'gpt-5.6-sol'])
def test_known_secret_output_fails_closed_without_artifact_leak(adapter, monkeypatch, model):
    args = {'path': 'fixture.py', 'content': 'credential = ' + FAKE_KEY}
    output = (json.dumps(native_response(model=model, arguments=json.dumps(args))[0]) if model.startswith('glm-') else
        codex_output(json.dumps({'type': 'tool_calls', 'calls': [{'id': 'write-1', 'name': 'write', 'arguments': args}]})))
    fake_process(monkeypatch, output)
    record = complete(adapter, model=model)
    assert record['error']['code'] == 'action_redaction_mismatch'
    assert record['assistant_message'] is None and record['action'] is None and record['parsed_calls'] == []
    assert record['total_tokens'] == 170
    assert FAKE_KEY not in json.dumps(record)
    assert FAKE_KEY not in ''.join(path.read_text(encoding='utf-8') for path in adapter.root.rglob('*') if path.is_file())


def test_known_secret_in_task_input_is_logged_redacted_without_sending(adapter, monkeypatch):
    monkeypatch.setattr(transport, '_run_process', lambda *a, **k: pytest.fail('Known credential must not be sent'))
    messages = [{'role': 'user', 'content': 'Credential appeared in tool output: ' + FAKE_KEY}]
    before = deepcopy(messages)
    record = complete(adapter, model='gpt-5.6-sol', messages=messages)
    assert record['error']['code'] == 'request_contains_known_secret'
    assert record['transport_attempt_count'] == 0 and record['total_tokens'] is None
    assert messages == before
    assert FAKE_KEY not in ''.join(path.read_text(encoding='utf-8') for path in adapter.root.rglob('*') if path.is_file())


def test_provider_diagnostic_auth_headers_are_redacted_without_touching_model_examples(adapter, monkeypatch):
    diagnostic_token = 'provider-diagnostic-credential-example'
    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, codex_output(extra=[
            {'type': 'error', 'message': 'HTTP Authorization: Bearer ' + diagnostic_token}]),
            'Authorization: Bearer ' + diagnostic_token + '\nProxy-Authorization: Basic ' + diagnostic_token + '\n')
    monkeypatch.setattr(transport, '_run_process', process)
    record = complete(adapter, model='gpt-5.6-sol')
    assert record['error'] is not None and record['total_tokens'] == 170
    assert diagnostic_token not in json.dumps(record)
    artifacts = ''.join(path.read_text(encoding='utf-8') for path in adapter.root.rglob('*') if path.is_file())
    assert diagnostic_token not in artifacts and 'Authorization: Bearer [REDACTED]' in artifacts
    assert transport._diagnostic_redact('Authorization: Bearer [REDACTED]') == 'Authorization: Bearer [REDACTED]'
    assert transport._redact({'role': 'assistant', 'content': 'Authorization: Bearer ' + diagnostic_token})['content'].endswith(diagnostic_token)


def test_cm_native_call_disables_thinking_and_uses_short_socket_timeout(adapter, monkeypatch):
    text_only = {'http_status': 200, 'raw': json.dumps({'model': 'glm-5.3-flash', 'choices': [
        {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'zero-loss summary'}}]})}
    seen = []
    fake_process(monkeypatch, json.dumps(text_only), seen)
    record = adapter.complete('glm-5.3-flash', [{'role': 'user', 'content': 'summarize the materials'}],
        tools=[], run_id='fixture-run', role='cm', task_id='fixture-instance', call_id='cm-fixture-1',
        max_tokens=2048)
    job = json.loads(seen[0]['input'])
    wire = json.loads(job['payload'])
    assert wire['thinking'] == {'type': 'disabled'}
    assert job['socket_timeout_seconds'] == 120
    assert wire['max_tokens'] == 2048 and record['max_tokens_requested'] == 2048
    assert record['glm_thinking'] == {'type': 'disabled'} and record['socket_timeout_seconds'] == 120
    assert record['action']['kind'] == 'no_action' and record['action']['text'] == 'zero-loss summary'
    assert record['error'] is None and record['total_tokens'] == 170 or record['total_tokens'] is None
