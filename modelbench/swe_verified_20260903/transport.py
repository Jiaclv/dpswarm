"""Traceable SWE-bench calls using the existing authenticated model transports.

No tools are executed here. Native GLM history belongs to the caller; GPT uses
the controlled Codex CLI with an explicit text tool envelope. Each call ID can
be used once. Cancellation stops the owned local process tree, but cannot undo
provider work or charges already accepted remotely.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4


V2_SOURCE = Path(__file__).resolve().parents[1] / 'team_runtime_v2/transport.py'
_PLUGIN = Path(__file__).resolve().parents[2] / 'dpswarm-plugin'
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))
from dpswarm.team_runtime.protocol import (
    ProtocolError, normalize_tool_declarations, parse_native_response, parse_text_response,
)

MODELS = ('glm-5.3', 'glm-5.3-flash', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')
_LOG_LOCK = threading.Lock()
_USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
                 'reasoning_tokens', 'total_tokens')
_AUTH_HEADER = re.compile(
    r'(?i)(\b(?:proxy-)?authorization[\s\"\']*[:=]\s*[\"\']?(?:bearer|basic)\s+)'
    r'(\[REDACTED\]|[^\s\"\',;}\]]+)')


def _exact_redact(value, secrets=()):
    """Only positively known credentials may affect task/model data."""
    if isinstance(value, str):
        for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        return value
    if isinstance(value, list):
        return [_exact_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {_exact_redact(key, secrets): _exact_redact(item, secrets) for key, item in value.items()}
    return value


def _diagnostic_redact(value, secrets=()):
    """Credential-header masking is restricted to provider diagnostics."""
    value = _exact_redact(value, secrets)
    if isinstance(value, str):
        return _AUTH_HEADER.sub(lambda match: match[1] + '[REDACTED]', value)
    if isinstance(value, list):
        return [_diagnostic_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: ('[REDACTED]' if str(key).lower() in ('authorization', 'proxy-authorization')
                      and item else _diagnostic_redact(item)) for key, item in value.items()}
    return value


def _diagnostic_envelope(value):
    """Recognize transport envelopes, never inspect nested task/tool strings."""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if value.get('role') == 'assistant':
        return value
    if value.get('type') in ('error', 'turn.failed', 'warning', 'status', 'reconnecting', 'retry'):
        for key in ('message', 'error', 'detail', 'details'):
            if key in result:
                result[key] = _diagnostic_redact(result[key])
    if 'http_status' in value or 'error_code' in value:
        if 'error_message' in result:
            result['error_message'] = _diagnostic_redact(result['error_message'])
        if isinstance(result.get('raw'), str):
            result['raw'] = _redact(result['raw'])
    if ('model_requested' in value and 'call_id' in value) or ('error' in value and 'choices' not in value):
        for key in ('error', 'provider_error_events', 'reconnect_events'):
            if key in result:
                result[key] = _diagnostic_redact(result[key])
    return result


def _redact(value, secrets=()):
    """Preserve IDs, code, examples and executable history; redact log envelopes.

    Bare sk-/JWT strings and api_key variable names are not credential evidence.
    JSONL provider error events are diagnostics; assistant text is always data.
    """
    value = _exact_redact(value, secrets)
    if isinstance(value, dict):
        return _diagnostic_envelope(value)
    if not isinstance(value, str):
        return value
    lines = value.splitlines(keepends=True)
    sanitized = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            sanitized.append(line)
            continue
        changed = _diagnostic_envelope(parsed)
        sanitized.append(json.dumps(changed, ensure_ascii=False) + ('\n' if line.endswith('\n') else '')
                         if changed != parsed else line)
    return ''.join(sanitized)


class CallIdentityError(ValueError):
    """No new provider call occurs when a call identity is invalid or reused."""


class CodexTerminalError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _codex_terminal_text(stdout, terminal_text, record):
    """Bind the CLI's explicit final artifact to one completed turn.

    Codex 0.149.0 exec JSONL drops message phase. Its output-last-message file
    is written only after successful turn completion, from the last agent
    message, without adding a newline. Do not choose text by JSON validity.
    """
    events = []
    try:
        for line in stdout.splitlines():
            if line.strip():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('JSONL event is not an object')
                events.append(event)
    except (ValueError, TypeError) as exc:
        raise CodexTerminalError('codex_terminal_ambiguous', 'Invalid JSONL prevents terminal-message binding') from exc
    completed = [i for i, event in enumerate(events) if event.get('type') == 'turn.completed']
    started = [i for i, event in enumerate(events) if event.get('type') == 'turn.started']
    messages = [(i, event['item']) for i, event in enumerate(events)
                if event.get('type') == 'item.completed' and isinstance(event.get('item'), dict)
                and event['item'].get('type') == 'agent_message']
    selection = {'policy': 'codex-output-last-message-exact-v1', 'agent_message_count': len(messages),
                 'turn_completed_count': len(completed), 'turn_started_count': len(started),
                 'terminal_artifact_present': terminal_text is not None,
                 'selected_item_id': None, 'selected_event_index': None}
    record['codex_response_selection'] = selection
    if len(completed) != 1 or len(started) > 1 or not messages:
        raise CodexTerminalError('codex_terminal_ambiguous', 'Expected one completed turn with an agent message')
    end = completed[0]
    if (any(index >= end for index, _ in messages)
            or (started and (started[0] >= messages[0][0] or started[0] >= end))
            or any(event.get('type') in ('turn.started', 'turn.failed') for event in events[end + 1:])
            or any(not isinstance(item.get('text'), str) for _, item in messages)):
        raise CodexTerminalError('codex_terminal_ambiguous', 'Agent messages do not belong to one terminal turn')
    if terminal_text is None:
        raise CodexTerminalError('codex_terminal_missing', 'Codex did not write the required final-message artifact')
    index, final = messages[-1]
    if terminal_text != final['text']:
        raise CodexTerminalError('codex_terminal_mismatch', 'Final-message artifact differs from the terminal agent message')
    selection.update(selected_item_id=final.get('id'), selected_event_index=index,
                     terminal_text_sha256=hashlib.sha256(terminal_text.encode('utf-8')).hexdigest())
    return terminal_text


class _Interrupted(subprocess.TimeoutExpired):
    def __init__(self, argv, code, stdout='', stderr=''):
        super().__init__(argv, 0, output=stdout, stderr=stderr)
        self.code = code


def _load_v2():
    # Each call receives independent globals. Replacing its process hooks never
    # mutates a shared imported transport or another concurrent request.
    spec = importlib.util.spec_from_file_location('_swe_call_v2_' + uuid4().hex, V2_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text_tool_prompt(tools):
    declarations = normalize_tool_declarations(tools)
    return ('For external tool requests return exactly one JSON object, without prose or Markdown fences:\n'
            '{"type":"tool_calls","calls":[{"id":"unique_id","name":"declared_name","arguments":{}}]}.\n'
            'Use only the declared tools. Every argument belongs inside arguments. The external loop executes '
            'tools; do not execute your own tools. Ordinary text is a NoAction observation. '
            'Only the external loop decides the effect of a declared completion tool; text or DONE does not finish work. '
            'These instructions do not override built-in safety or higher-priority instructions.\n'
            'Declared tools:\n' + json.dumps(declarations, ensure_ascii=False))


def _native_history_safe(assistant):
    calls = assistant.get('tool_calls')
    if calls is None or calls == []:
        return True
    if not isinstance(calls, list):
        return False
    ids = set()
    for call in calls:
        if not isinstance(call, dict) or call.get('type') != 'function':
            return False
        ident, function = call.get('id'), call.get('function')
        if not isinstance(ident, str) or not ident.strip() or ident in ids:
            return False
        if (not isinstance(function, dict) or not isinstance(function.get('name'), str)
                or not function['name'].strip() or not isinstance(function.get('arguments'), str)):
            return False
        ids.add(ident)
    return True


def _write_once(path, value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def _journal_lock(root):
    # Parallel threads and independent processes both append whole durable
    # events. The per-call exclusive directory is the identity reservation.
    with _LOG_LOCK, (root / '.calls.lock').open('a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _terminate_tree(process):
    evidence = {'pid': process.pid, 'mechanism': 'taskkill_tree' if os.name == 'nt' else 'kill_process_group'}
    if os.name == 'nt':
        result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                capture_output=True, timeout=15, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        evidence['returncode'] = result.returncode
        evidence['tree_termination_confirmed'] = result.returncode == 0
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            evidence['tree_termination_confirmed'] = True
        except ProcessLookupError:
            evidence['tree_termination_confirmed'] = process.poll() is not None
    return evidence


def _run_process(argv, *, input, cwd, deadline_at, cancel_event, record):
    def interrupted():
        if cancel_event is not None and cancel_event.is_set():
            return 'cancelled'
        if time.monotonic() >= deadline_at:
            return 'total_deadline'
        return None

    code = interrupted()
    if code:
        raise _Interrupted(argv, code)
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=cwd, text=True, encoding='utf-8', errors='replace', **options)
    record['transport_attempt_count'] = 1
    record['process_id'] = process.pid
    pending_input = input
    while True:
        code = interrupted()
        if code:
            try:
                record['cancellation'] = _terminate_tree(process)
            except Exception as exc:
                record['cancellation'] = {'pid': process.pid, 'tree_termination_confirmed': False,
                                          'error_type': type(exc).__name__}
                process.kill()
            try:
                stdout, stderr = process.communicate(timeout=15)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                stdout, stderr = exc.stdout or '', exc.stderr or ''
            record['cancellation']['parent_exited'] = process.poll() is not None
            raise _Interrupted(argv, code, stdout, stderr)
        try:
            stdout, stderr = process.communicate(input=pending_input,
                timeout=max(.001, min(.1, deadline_at - time.monotonic())))
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            pending_input = None


class SweTransport:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _log(self, value):
        with _journal_lock(self.root):
            with (self.root / 'calls.jsonl').open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())

    def complete(self, model, messages, *, tools, run_id, role, task_id, call_id,
                 max_tokens=32768, timeout_seconds=600, cancel_event=None):
        for name, value in {'run_id': run_id, 'role': role, 'task_id': task_id, 'call_id': call_id}.items():
            if not isinstance(value, str) or not value.strip():
                raise CallIdentityError(name + ' must be a nonempty string')
        folder = self.root / 'calls' / hashlib.sha256(call_id.encode('utf-8')).hexdigest()
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise CallIdentityError('call_id has already been reserved; it cannot be retried or overwritten') from exc
        v2 = _load_v2()
        original = v2.original
        secrets, interruption = (), {}
        # Frozen methods look up this global at call time. This module is private
        # to this call, so every inherited dump uses the SWE policy without
        # changing frozen source or another concurrent call's credential set.
        original._redact = lambda value, extra=(): _redact(value, secrets + tuple(extra))
        started, clock_start = original._now(), time.monotonic()
        native = model in ('glm-5.3', 'glm-5.3-flash')
        record = {
            'call_id': call_id, 'run_id': run_id, 'role': role, 'task_id': task_id,
            'model_requested': model, 'model_reported': None,
            'effort_requested': 'max', 'effort_reported': None,
            'service_tier_requested': None if native else 'fast', 'service_tier_reported': None,
            'assistant_message': None, 'text': '', 'action': None, 'parsed_calls': [],
            'response_kind': None, 'protocol_error': None, 'error': None,
            'history_continuation_safe': False,
            **{field: None for field in _USAGE_FIELDS},
            'usage_source': None, 'total_tokens_source': None, 'input_tokens_includes_cached': True,
            'stop_reason': None, 'wall_seconds': None, 'started_at': started, 'completed_at': None,
            'timestamps': {'started_at': started, 'completed_at': None},
            'max_tokens_requested': max_tokens, 'cap_enforced': native,
            'glm_thinking': ({'type': 'disabled'} if native and role == 'cm' else None),
            'total_deadline_seconds': timeout_seconds if isinstance(timeout_seconds, (int, float))
                and math.isfinite(timeout_seconds) else None,
            'socket_timeout_seconds': (120 if role == 'cm' else 300) if native else None,
            'timeout_kind': None, 'cancellation': None, 'tools_used': 0,
            'transport_attempt_count': 0, 'attempt_count': 1, 'retry_attempted': False,
            'reconnect_detected': False, 'reconnect_events': [],
            'adapter_mode': 'glm_native_tools_swe' if native else 'codex_text_tools_swe',
            'raw_artifacts': {'directory': str(folder)}, 'artifacts_redacted': True,
            'redaction_policy': 'known-secrets-exact; provider-diagnostic-auth-headers-only-v2',
        }
        _write_once(folder / 'started.json', original._redact(record))
        self._log(original._redact({'event': 'started', **record}))
        try:
            if model not in MODELS:
                raise v2.TransportError('unsupported_model', 'Model is not in the five-model allowlist')
            if type(max_tokens) is not int or max_tokens <= 0:
                raise v2.TransportError('invalid_request', 'max_tokens must be a positive integer')
            if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                    or not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > 600):
                raise v2.TransportError('invalid_request', 'timeout_seconds must be finite and in (0, 600]')
            if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
                raise v2.TransportError('invalid_request', 'messages must be a list of objects')
            declarations = normalize_tool_declarations(tools)
            wire_messages = deepcopy(messages)
            try:
                key = original._keyconfig('GLM_API_KEY')
            except Exception:
                if native:
                    raise
                key = None
            secrets = (key,) if isinstance(key, str) and key else ()
            if native and not key:
                raise v2.TransportError('missing_credentials', 'GLM_API_KEY is missing')
            if not native:
                wire_messages.append({'role': 'user', 'content': text_tool_prompt(declarations)})
            _write_once(folder / 'prompt.json', original._redact({
                'model': model, 'messages': messages, 'wire_messages': wire_messages,
                'tools': declarations, 'run_id': run_id, 'role': role, 'task_id': task_id, 'call_id': call_id,
            }, secrets))
            record['raw_artifacts']['prompt'] = str(folder / 'prompt.json')
            if _exact_redact([messages, declarations], secrets) != [messages, declarations]:
                raise v2.TransportError('request_contains_known_secret',
                                        'Known credential found in task input; provider request was not sent')
            deadline_at = clock_start + timeout_seconds

            def run(argv, *, input, cwd, timeout=None):
                try:
                    result = _run_process(argv, input=input, cwd=cwd, deadline_at=deadline_at,
                                          cancel_event=cancel_event, record=record)
                    # stderr is a diagnostic channel, never assistant/tool data.
                    result.stderr = _diagnostic_redact(result.stderr, secrets)
                    return result
                except _Interrupted as exc:
                    interruption['code'] = exc.code
                    exc.stderr = _diagnostic_redact(exc.stderr, secrets)
                    raise

            if native:
                def exchange(endpoint, payload, key, *, socket_timeout_seconds, deadline_seconds):
                    job = json.dumps({'endpoint': endpoint, 'payload': payload, 'key': key,
                                      'socket_timeout_seconds': socket_timeout_seconds}, ensure_ascii=True)
                    cwd = folder / 'empty-cwd'
                    cwd.mkdir()
                    try:
                        process = run([sys.executable, '-I', str(V2_SOURCE), '--http-worker'], input=job, cwd=cwd)
                        stdout, stderr = process.stdout, process.stderr
                        record['http_worker_returncode'] = process.returncode
                    except _Interrupted as exc:
                        stdout, stderr = exc.stdout or '', exc.stderr or ''
                        process = None
                    if isinstance(stdout, bytes):
                        stdout = stdout.decode('utf-8', errors='replace')
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode('utf-8', errors='replace')
                    _write_once(folder / 'http-worker.stdout.json', original._redact(stdout, secrets))
                    _write_once(folder / 'http-worker.stderr.txt', original._redact(stderr, secrets))
                    record['raw_artifacts'].update(http_worker_stdout=str(folder / 'http-worker.stdout.json'),
                        http_worker_stderr=str(folder / 'http-worker.stderr.txt'))
                    try:
                        value = json.loads(stdout)
                    except ValueError:
                        raise v2.TransportError(interruption.get('code', 'http_worker_error'),
                                                'HTTP worker did not return a complete response') from None
                    if not isinstance(value, dict) or (process is not None and process.returncode):
                        raise v2.TransportError('http_worker_error', 'HTTP worker response is invalid')
                    return value
                v2._http_exchange = exchange
                if role == 'cm':  # shorter read timeout: summarization must not hold the run
                    v2.SOCKET_TIMEOUT_SECONDS = 120
                v2.V2Transport._glm_v2(self, record, wire_messages, declarations, folder, secrets,
                                      deadline_at - time.monotonic())
            else:
                original.TIMEOUT_SECONDS = timeout_seconds
                terminal = {'text': None, 'stdout': None, 'capture_error': None}
                temporary_terminal = folder / 'last-message.raw.tmp'

                def codex_run(argv, *, input, cwd, timeout=None):
                    actual_argv = list(argv)
                    actual_argv[-1:-1] = ['--output-last-message', str(temporary_terminal)]
                    request_path = folder / 'process-request.json'
                    _write_once(request_path, original._redact({'argv': actual_argv, 'cwd': str(cwd),
                        'stdin': input, 'cap_enforced': False, 'timeout_seconds': timeout,
                        'terminal_message_contract': 'codex-output-last-message-exact-v1'}))
                    record['raw_artifacts']['request_template'] = record['raw_artifacts']['request']
                    record['raw_artifacts']['request'] = str(request_path)
                    try:
                        result = run(actual_argv, input=input, cwd=cwd, timeout=timeout)
                        terminal['stdout'] = result.stdout
                        return result
                    finally:
                        # Preserve the CLI artifact after exact credential
                        # redaction, including interrupted partial calls. A
                        # capture failure must not prevent stdout usage parsing.
                        if temporary_terminal.exists() or temporary_terminal.is_symlink():
                            try:
                                if temporary_terminal.is_symlink() or not temporary_terminal.is_file():
                                    raise ValueError('Final-message artifact is not a regular file')
                                payload = temporary_terminal.read_bytes()
                                try:
                                    terminal['text'] = payload.decode('utf-8')
                                except UnicodeDecodeError:
                                    terminal['capture_error'] = 'Final-message artifact is not valid UTF-8'
                                for secret in secrets:
                                    payload = payload.replace(secret.encode('utf-8'), b'[REDACTED]')
                                terminal_path = folder / 'last-message.txt'
                                with terminal_path.open('xb') as stream:
                                    stream.write(payload)
                                    stream.flush()
                                    os.fsync(stream.fileno())
                                record['raw_artifacts']['last_message'] = str(terminal_path)
                            except (OSError, ValueError) as exc:
                                terminal['capture_error'] = str(exc)
                            finally:
                                if temporary_terminal.is_file() or temporary_terminal.is_symlink():
                                    temporary_terminal.unlink()

                original._run_codex_process = codex_run
                try:
                    original.ExperimentTransport._codex(self, record, wire_messages, folder)
                except TimeoutError as exc:
                    # The legacy adapter reports every interruption through its
                    # timeout branch; a runner cancellation is not a deadline.
                    if interruption:
                        raise v2.TransportError(interruption['code'],
                            'Local model request %s after %.1f seconds' % (
                                'cancelled by the experiment runner' if interruption['code'] == 'cancelled'
                                else 'reached its total deadline',
                                time.monotonic() - clock_start)) from exc
                    raise
                if terminal['capture_error']:
                    raise CodexTerminalError('codex_terminal_artifact_invalid', terminal['capture_error'])
                record['text'] = _codex_terminal_text(terminal['stdout'], terminal['text'], record)
                record['assistant_message'] = {'role': 'assistant', 'content': record['text']}
            if interruption:
                raise v2.TransportError(interruption['code'], 'Local model request interrupted')
            assistant = record['assistant_message']
            if _exact_redact(assistant, secrets) != assistant:
                raise v2.TransportError('action_redaction_mismatch', 'Redaction would change response history or actions')
            record['history_continuation_safe'] = _native_history_safe(assistant) if native else True
            try:
                action = (parse_native_response(assistant, declarations) if native
                          else parse_text_response(record['text'], declarations))
                record.update(action=action, parsed_calls=deepcopy(action['calls']), response_kind=action['kind'])
            except ProtocolError as exc:
                record['protocol_error'] = {'code': exc.code, 'message': str(exc)}
                record['response_kind'] = 'protocol_error'
        except Exception as exc:
            code = interruption.get('code') or getattr(exc, 'code', 'transport_error')
            record['error'] = {'type': type(exc).__name__, 'code': code, 'message': str(exc)}
            record['timeout_kind'] = code if code in ('total_deadline', 'socket_timeout') else None
            record['assistant_message'], record['text'] = None, ''
            record['response_kind'] = 'transport_error'
        finally:
            record['wall_seconds'] = round(time.monotonic() - clock_start, 6)
            record['completed_at'] = original._now()
            record['timestamps']['completed_at'] = record['completed_at']
            record['usage'] = {field: record[field] for field in _USAGE_FIELDS}
            record['usage_complete'] = all(record[field] is not None for field in ('input_tokens', 'output_tokens', 'total_tokens'))
            record['raw_artifacts'].update(metadata=str(folder / 'metadata.json'), output=str(folder / 'output.md'))
            record = original._redact(record, secrets)
            _write_once(folder / 'output.md', record['text'])
            _write_once(folder / 'metadata.json', record)
            self._log({'event': 'completed', **record})
        return record


__all__ = ['SweTransport', 'CallIdentityError', 'MODELS', 'text_tool_prompt']
