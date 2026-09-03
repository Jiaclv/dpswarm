"""Codex terminal-message binding: discriminating offline fixtures, no model calls.

The pilot_v2 Sphinx Luna defect came from joining multiple agent_message events
with newlines before parsing. The adapter now binds the CLI's explicit
--output-last-message artifact to the terminal agent message of one completed
turn and must never pick a response by JSON parseability.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.swe_verified_20260903 import transport
from modelbench.swe_verified_20260903.tests.test_transport import (
    FAKE_KEY, TOOLS, complete, fake_process,
)


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


ENVELOPE = json.dumps({'type': 'tool_calls', 'calls': [{'id': 'write-1', 'name': 'write',
    'arguments': {'path': 'a.py', 'content': 'pass'}}]})
OTHER_ENVELOPE = json.dumps({'type': 'tool_calls', 'calls': [{'id': 'write-2', 'name': 'write',
    'arguments': {'path': 'b.py', 'content': 'pass'}}]})


def events_stdout(events, usage=True):
    items = list(events)
    completed = {'type': 'turn.completed'}
    if usage:
        completed['usage'] = {'input_tokens': 130, 'cached_input_tokens': 100, 'output_tokens': 40}
    items.append(completed)
    return '\n'.join(json.dumps(event) for event in items) + '\n'


def message(text, ident='answer'):
    return {'type': 'item.completed', 'item': {'id': ident, 'type': 'agent_message', 'text': text}}


def gpt(adapter, monkeypatch, stdout, call_id='fixture-call'):
    fake_process(monkeypatch, stdout)
    return complete(adapter, model='gpt-5.6-sol', call_id=call_id)


def test_intermediate_prose_then_final_json_uses_terminal_artifact(adapter, monkeypatch):
    record = gpt(adapter, monkeypatch, events_stdout([message('working on it'), message(ENVELOPE)]))
    assert record['error'] is None and record['protocol_error'] is None
    assert record['action']['calls'] == [{'id': 'write-1', 'name': 'write',
                                          'arguments': {'path': 'a.py', 'content': 'pass'}}]
    assert record['codex_response_selection']['agent_message_count'] == 2
    assert record['codex_response_selection']['selected_item_id'] == 'answer'
    assert Path(record['raw_artifacts']['last_message']).read_text(encoding='utf-8') == ENVELOPE


def test_duplicate_identical_json_messages_no_longer_extra_data(adapter, monkeypatch):
    """The exact pilot_v2 Sphinx Luna failure shape: two identical envelopes."""
    stdout = events_stdout([message(ENVELOPE), message(ENVELOPE)])
    record = gpt(adapter, monkeypatch, stdout)
    assert record['error'] is None and record['protocol_error'] is None
    assert record['action']['kind'] == 'tools' and record['total_tokens'] == 170
    assert record['codex_response_selection']['agent_message_count'] == 2


def test_different_intermediate_and_final_json_execute_only_the_final(adapter, monkeypatch):
    record = gpt(adapter, monkeypatch, events_stdout([message(OTHER_ENVELOPE), message(ENVELOPE)]))
    assert record['error'] is None
    assert record['parsed_calls'] == [{'id': 'write-1', 'name': 'write',
                                       'arguments': {'path': 'a.py', 'content': 'pass'}}]
    assert record['parsed_calls'][0]['id'] != 'write-2'


def test_invalid_final_message_is_protocol_error_not_fallback_to_earlier(adapter, monkeypatch):
    """Never select by JSON parseability: a legal earlier message must not win."""
    record = gpt(adapter, monkeypatch, events_stdout([message(ENVELOPE), message('{not json')]))
    assert record['error'] is None and record['response_kind'] == 'protocol_error'
    assert record['protocol_error']['code'] == 'invalid_json'
    assert record['action'] is None and record['parsed_calls'] == []


def test_missing_terminal_artifact_fails_closed(adapter, monkeypatch):
    def silent(argv, **kwargs):
        kwargs['record']['transport_attempt_count'] = 1
        return subprocess.CompletedProcess(argv, 0, events_stdout([message(ENVELOPE)]), '')
    monkeypatch.setattr(transport, '_run_process', silent)
    record = complete(adapter, model='gpt-5.6-sol', call_id='missing-terminal')
    assert record['error']['code'] == 'codex_terminal_missing'
    assert record['action'] is None and record['total_tokens'] == 170
    assert record['codex_response_selection']['terminal_artifact_present'] is False


def test_mismatched_terminal_artifact_fails_closed(adapter, monkeypatch):
    def forged(argv, **kwargs):
        kwargs['record']['transport_attempt_count'] = 1
        Path(argv[argv.index('--output-last-message') + 1]).write_text(OTHER_ENVELOPE, encoding='utf-8')
        return subprocess.CompletedProcess(argv, 0, events_stdout([message(ENVELOPE)]), '')
    monkeypatch.setattr(transport, '_run_process', forged)
    record = complete(adapter, model='gpt-5.6-sol', call_id='mismatch-terminal')
    assert record['error']['code'] == 'codex_terminal_mismatch'
    assert record['action'] is None and record['parsed_calls'] == []


def test_multiple_completed_turns_are_ambiguous(adapter, monkeypatch):
    stdout = (events_stdout([message(ENVELOPE)]) + events_stdout([message(ENVELOPE)]))
    record = gpt(adapter, monkeypatch, stdout)
    assert record['error']['code'] == 'codex_terminal_ambiguous'
    assert record['action'] is None


def test_corrupted_jsonl_line_blocks_binding_but_keeps_usage(adapter, monkeypatch):
    stdout = '{"type":"item.completed","item":{"id":"ans' + '\n' + events_stdout([message(ENVELOPE)])
    record = gpt(adapter, monkeypatch, stdout)
    assert record['error']['code'] == 'codex_terminal_ambiguous'
    assert record['total_tokens'] == 170 and record['action'] is None


def test_terminal_artifact_redacts_known_credentials(adapter, monkeypatch):
    secret_text = json.dumps({'type': 'tool_calls', 'calls': [{'id': 'write-1', 'name': 'write',
        'arguments': {'path': 'a.py', 'content': 'token = ' + FAKE_KEY}}]})
    record = gpt(adapter, monkeypatch, events_stdout([message(secret_text)]))
    assert record['error']['code'] == 'action_redaction_mismatch'
    assert FAKE_KEY not in Path(record['raw_artifacts']['last_message']).read_text(encoding='utf-8')
    assert FAKE_KEY not in ''.join(path.read_text(encoding='utf-8')
                                   for path in adapter.root.rglob('*') if path.is_file())


@pytest.mark.parametrize('code', ['cancelled', 'total_deadline'])
def test_interrupted_call_reports_true_cause_without_terminal_binding(adapter, monkeypatch, code):
    stdout = events_stdout([message(ENVELOPE)])
    def interrupted(argv, **kwargs):
        kwargs['record']['transport_attempt_count'] = 1
        raise transport._Interrupted(argv, code, stdout, 'fixture interruption')
    monkeypatch.setattr(transport, '_run_process', interrupted)
    record = complete(adapter, model='gpt-5.6-sol', call_id='interrupted-' + code)
    assert record['error']['code'] == code
    assert 'exceeded' not in record['error']['message']
    assert ('cancelled by the experiment runner' in record['error']['message']) == (code == 'cancelled')
    assert record['total_tokens'] == 170 and record['action'] is None
    assert 'codex_response_selection' not in record
