"""One-shot Coding Plan GLM SSE HTTP worker. No retries, tools or credentials in evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import secrets
import http.client
import json
import math
from pathlib import Path
import re
import socket
import sys
import time
from urllib import error, request
from uuid import uuid4

CODING_GLM_ENDPOINT = 'https://open.bigmodel.cn/api/coding/paas/v4/chat/completions'
ENDPOINT = CODING_GLM_ENDPOINT
MAX_LINE_BYTES = 4 * 1024 * 1024


class StreamFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def redact(value, key):
    """Mask full secrets and long secret fragments, including split deltas."""
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if k.lower() in {'authorization', 'api_key', 'apikey', 'key'}
                    else redact(v, key)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if not isinstance(value, str):
        return value
    if key:
        value = value.replace(key, '[REDACTED]')
        # Delta boundaries can split the secret; do not persist useful pieces.
        fragments = {key[i:i+8] for i in range(max(0, len(key)-7))}
        for fragment in fragments:
            value = value.replace(fragment, '[REDACTED]')
    return re.sub(r'(?i)bearer\s+[^\s"\\]+', 'Bearer [REDACTED]', value)


def evidence_shape(value, in_delta=False):
    """Raw event identity and shape without reversible, fragmented delta text."""
    if isinstance(value, dict):
        return {k: evidence_shape(v, in_delta or k == 'delta') for k, v in value.items()}
    if isinstance(value, list):
        return [evidence_shape(v, in_delta) for v in value]
    if isinstance(value, str) and in_delta:
        return {'redacted': True, 'chars': len(value)}
    return value


class Receiver:
    def __init__(self, key, evidence_dir=None):
        self.key = key
        self.started = time.monotonic()
        self._evidence_key = secrets.token_bytes(32)  # Never persisted or emitted.
        self.base = {}
        self.choices = {}
        self.usage = None
        self.provider_error = None
        self.status = None
        self.metrics = {
            'started_at': datetime.now(timezone.utc).isoformat(),
            'completed': False, 'saw_done': False, 'all_choices_finished': False,
            'finish_reason': None, 'usage_chunk_received': False,
            'usage_after_finish': False, 'terminal_usage_confirmed': False,
            'headers_seconds': None, 'first_data_seconds': None,
            'first_content_seconds': None, 'first_reasoning_seconds': None,
            'first_tool_seconds': None, 'last_event_seconds': None,
            'chunk_count': 0, 'event_count': 0, 'bytes_received': 0,
            'max_event_gap_seconds': 0.0, 'wall_seconds': 0.0,
            'evidence_semantics': 'shapes_lengths_ephemeral_hmac_no_delta_text_not_replayable',
        }
        self.artifacts = {}
        if evidence_dir is not None:
            directory = Path(evidence_dir).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            self.artifacts = {'raw_events': str(directory/'stream.events.jsonl'),
                              'progress': str(directory/'stream.progress.json')}
            # Call evidence directories are unique; never overwrite a prior stream.
            with Path(self.artifacts['raw_events']).open('x', encoding='utf-8'):
                pass
        self.progress()

    def elapsed(self):
        return round(time.monotonic()-self.started, 6)

    def progress(self):
        self.metrics['wall_seconds'] = self.elapsed()
        if not self.artifacts:
            return
        path = Path(self.artifacts['progress'])
        temp = path.with_name(path.name+'.tmp-'+uuid4().hex)
        value = redact({'http_status': self.status, 'stream_metrics': self.metrics,
                        'stream_artifacts': self.artifacts}, self.key)
        temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False)+'\n', encoding='utf-8')
        for attempt in range(6):
            try:
                temp.replace(path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.025 * (attempt + 1))  # Local file sharing only; never HTTP retry.

    def evidence(self, kind, data, raw_lines=None):
        now = self.elapsed()
        previous = self.metrics['last_event_seconds']
        if previous is not None:
            self.metrics['max_event_gap_seconds'] = max(self.metrics['max_event_gap_seconds'], now-previous)
        self.metrics['last_event_seconds'] = now
        self.metrics['event_count'] += 1
        if self.artifacts:
            raw_event = '\n'.join(raw_lines) if raw_lines else (data or '')
            try:
                safe_data = evidence_shape(json.loads(data)) if data and data.strip() != '[DONE]' else data
            except (ValueError, TypeError):
                safe_data = {'redacted': True, 'chars': len(data or '')}
            value = redact({'sequence': self.metrics['event_count'], 'seconds': now,
                            'kind': kind, 'data': safe_data,
                            'raw_event_hmac_sha256': hmac.new(self._evidence_key, raw_event.encode('utf-8'), hashlib.sha256).hexdigest(),
                            'raw_chars': len(raw_event), 'delta_text_redacted': True}, self.key)
            with Path(self.artifacts['raw_events']).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+'\n')
                stream.flush()
        self.progress()

    def first(self, field):
        if self.metrics[field] is None:
            self.metrics[field] = self.elapsed()

    def consume(self, data, raw_lines=None):
        self.evidence('sse', data, raw_lines)
        if data is None:
            return
        self.first('first_data_seconds')
        if data.strip() == '[DONE]':
            self.metrics['saw_done'] = True
            self.progress()
            return
        try:
            chunk = json.loads(data)
        except (ValueError, TypeError) as exc:
            raise StreamFailure('invalid_stream_json', 'SSE data is not valid JSON') from exc
        if not isinstance(chunk, dict):
            raise StreamFailure('invalid_stream_payload', 'SSE JSON must be an object')
        self.metrics['chunk_count'] += 1
        if isinstance(chunk.get('error'), dict):
            self.provider_error = chunk['error']
            if isinstance(chunk.get('usage'), dict):
                self.usage = chunk['usage']
                self.metrics['usage_chunk_received'] = True
            raise StreamFailure('provider_rejected', 'Provider returned a stream error')
        for name in ('id', 'model', 'created', 'system_fingerprint', 'service_tier'):
            if chunk.get(name) is not None:
                if name in ('id', 'model') and name in self.base and self.base[name] != chunk[name]:
                    raise StreamFailure('invalid_stream_payload', 'Stream response identity changed')
                self.base[name] = chunk[name]
        choices = chunk.get('choices', [])
        if not isinstance(choices, list):
            raise StreamFailure('invalid_stream_payload', 'choices must be a list')
        for item in choices:
            if not isinstance(item, dict) or type(item.get('index')) is not int or item['index'] < 0:
                raise StreamFailure('invalid_stream_payload', 'Choice needs a nonnegative integer index')
            index = item['index']
            target = self.choices.setdefault(index, {'content': '', 'reasoning_content': '',
                'role': 'assistant', 'tools': {}, 'finish_reason': None})
            delta = item.get('delta') or {}
            if not isinstance(delta, dict):
                raise StreamFailure('invalid_stream_payload', 'delta must be an object')
            if target['finish_reason'] is not None and any(delta.get(k) for k in ('content', 'reasoning_content', 'tool_calls')):
                raise StreamFailure('invalid_stream_payload', 'Action delta followed a finished choice')
            if delta.get('role') not in (None, 'assistant'):
                raise StreamFailure('invalid_stream_payload', 'Only assistant stream deltas are supported')
            for field, clock in (('content', 'first_content_seconds'),
                                 ('reasoning_content', 'first_reasoning_seconds')):
                fragment = delta.get(field)
                if fragment is not None:
                    if not isinstance(fragment, str):
                        raise StreamFailure('invalid_stream_payload', 'Text delta must be a string')
                    target[field] += fragment
                    if fragment:
                        self.first(clock)
            tools = delta.get('tool_calls') or []
            if not isinstance(tools, list):
                raise StreamFailure('invalid_stream_payload', 'Tool deltas must be a list')
            for tool in tools:
                if not isinstance(tool, dict) or type(tool.get('index')) is not int or tool['index'] < 0:
                    raise StreamFailure('invalid_stream_payload', 'Tool needs a nonnegative integer index')
                self.first('first_tool_seconds')
                out = target['tools'].setdefault(tool['index'], {'id': '', 'type': '',
                                                          'function': {'name': '', 'arguments': ''}})
                for field in ('id', 'type'):
                    fragment = tool.get(field)
                    if fragment is not None:
                        if not isinstance(fragment, str):
                            raise StreamFailure('invalid_stream_payload', 'Tool metadata must be text')
                        if field == 'type' and out[field] and out[field] != fragment:
                            raise StreamFailure('invalid_stream_payload', 'Tool type changed')
                        if not out[field]:
                            out[field] = fragment
                        elif field == 'id' and fragment != out[field]:
                            out[field] += fragment
                fn = tool.get('function') or {}
                if not isinstance(fn, dict):
                    raise StreamFailure('invalid_stream_payload', 'Tool function must be an object')
                for field in ('name', 'arguments'):
                    fragment = fn.get(field)
                    if fragment is not None:
                        if not isinstance(fragment, str):
                            raise StreamFailure('invalid_stream_payload', 'Tool function delta must be text')
                        out['function'][field] += fragment
            finish = item.get('finish_reason')
            if finish is not None:
                if not isinstance(finish, str) or not finish:
                    raise StreamFailure('invalid_stream_payload', 'finish_reason must be nonempty text')
                if target['finish_reason'] not in (None, finish):
                    raise StreamFailure('invalid_stream_payload', 'Choice finish reason changed')
                target['finish_reason'] = finish
        self.metrics['all_choices_finished'] = bool(self.choices) and all(c['finish_reason'] for c in self.choices.values())
        if self.choices:
            self.metrics['finish_reason'] = self.choices[min(self.choices)]['finish_reason']
        if chunk.get('usage') is not None:
            if not isinstance(chunk['usage'], dict):
                raise StreamFailure('invalid_stream_payload', 'usage must be an object')
            self.usage = chunk['usage']  # A snapshot, never add repeated usage chunks.
            self.metrics['usage_chunk_received'] = True
            self.metrics['usage_after_finish'] = bool(self.metrics['all_choices_finished'])
        self.progress()

    def receive(self, response):
        lines = []; data = []; first_line = True
        while True:
            raw = response.readline(MAX_LINE_BYTES+1)
            if not raw:
                if lines:
                    self.consume('\n'.join(data) if data else None, lines)
                break
            if len(raw) > MAX_LINE_BYTES:
                raise StreamFailure('invalid_stream_payload', 'SSE line exceeds the supported size')
            self.metrics['bytes_received'] += len(raw)
            try:
                line = raw.decode('utf-8').rstrip('\r\n')
            except UnicodeDecodeError as exc:
                raise StreamFailure('invalid_stream_utf8', 'SSE response is not UTF-8') from exc
            if first_line:
                line = line.removeprefix('\ufeff'); first_line = False
            if not line:
                if lines:
                    self.consume('\n'.join(data) if data else None, lines)
                    lines = []; data = []
                    if self.metrics['saw_done']:
                        break
                continue
            lines.append(line)
            if line.startswith('data:'):
                field = line[5:]
                data.append(field[1:] if field.startswith(' ') else field)
        if not self.metrics['saw_done'] or not self.metrics['all_choices_finished']:
            raise StreamFailure('incomplete_stream', 'SSE ended without both finish_reason and [DONE]')
        self.metrics['completed'] = True
        self.metrics['terminal_usage_confirmed'] = bool(self.metrics['usage_chunk_received'] and self.metrics['usage_after_finish'])
        # Validate every tool before exposing any assembled action to the parent.
        for choice in self.choices.values():
            if choice['finish_reason'] == 'tool_calls' and not choice['tools']:
                raise StreamFailure('incomplete_tool_call', 'Tool finish reason has no tool call')
            for tool in choice['tools'].values():
                if not tool['id'] or tool['type'] != 'function' or not tool['function']['name']:
                    raise StreamFailure('incomplete_tool_call', 'Tool call identity is incomplete')
                try:
                    args = json.loads(tool['function']['arguments'])
                except (TypeError, ValueError) as exc:
                    raise StreamFailure('invalid_tool_arguments', 'Tool arguments are not complete JSON') from exc
                if not isinstance(args, dict):
                    raise StreamFailure('invalid_tool_arguments', 'Tool arguments must be a JSON object')

    def result(self, failure=None):
        body = {**self.base, 'object': 'chat.completion', 'choices': []}
        if self.usage is not None:
            body['usage'] = self.usage
        if self.provider_error is not None:
            body['error'] = self.provider_error
        if failure is None and self.metrics['completed']:
            for index, choice in sorted(self.choices.items()):
                message = {'role': 'assistant', 'content': choice['content'] or None,
                           'reasoning_content': choice['reasoning_content']}
                if choice['tools']:
                    message['tool_calls'] = [tool for _, tool in sorted(choice['tools'].items())]
                body['choices'].append({'index': index, 'message': message,
                                        'finish_reason': choice['finish_reason']})
        if failure is None and redact(body['choices'], self.key) != body['choices']:
            failure = StreamFailure('action_redaction_mismatch', 'Assembled assistant response required credential redaction')
            body['choices'] = []
        if failure is not None:
            self.metrics['error_code'] = failure.code
        self.progress()
        envelope = {'http_status': self.status,
                    'raw': json.dumps(redact(body, self.key), ensure_ascii=False, allow_nan=False),
                    'stream_metrics': self.metrics, 'stream_artifacts': self.artifacts}
        if failure is not None:
            envelope.update(error_code=failure.code, error_message=str(failure))
        return redact(envelope, self.key)


def run_job(job, *, opener=None):
    key = job.get('key') if isinstance(job, dict) else ''
    key = key if isinstance(key, str) else ''
    receiver = None
    try:
        if not isinstance(job, dict) or job.get('endpoint') != ENDPOINT or not key:
            raise StreamFailure('invalid_stream_job', 'The frozen Coding Plan endpoint and credential are required')
        payload = job.get('payload')
        if not isinstance(payload, str):
            raise StreamFailure('invalid_stream_job', 'payload must be a serialized JSON string')
        try:
            body = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise StreamFailure('invalid_stream_job', 'payload must contain valid JSON') from exc
        if not isinstance(body, dict) or body.get('stream') is not True:
            raise StreamFailure('invalid_stream_job', 'The frozen payload must explicitly enable streaming')
        timeout = job.get('socket_timeout_seconds')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise StreamFailure('invalid_stream_job', 'A positive finite socket timeout is required')
        receiver = Receiver(key, job.get('evidence_dir'))
        req = request.Request(ENDPOINT, data=payload.encode('utf-8'),
            headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json',
                     'Accept': 'text/event-stream'}, method='POST')
        with (opener or request.urlopen)(req, timeout=timeout) as response:
            receiver.status = response.status
            receiver.metrics['headers_seconds'] = receiver.elapsed()
            receiver.progress()
            receiver.receive(response)
        return receiver.result()
    except BaseException as exc:
        if receiver is None:
            receiver = Receiver(key)
        failure = exc if isinstance(exc, StreamFailure) else None
        if isinstance(exc, error.HTTPError):
            receiver.status = exc.code
            receiver.metrics['headers_seconds'] = receiver.elapsed()
            try:
                raw = exc.read().decode('utf-8', errors='replace')
                receiver.evidence('http_error', raw)
                body = json.loads(raw)
                if isinstance(body, dict):
                    receiver.provider_error = body.get('error')
                    if isinstance(body.get('usage'), dict):
                        receiver.usage = body['usage']
                        receiver.metrics['usage_chunk_received'] = True
            except Exception:
                pass
            failure = StreamFailure('http_error', 'HTTP '+str(exc.code))
        elif failure is None:
            reason = getattr(exc, 'reason', exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                code = 'socket_timeout'
            elif isinstance(exc, (error.URLError, ConnectionError, http.client.HTTPException, OSError)):
                code = 'network_error'
            else:
                code = 'stream_worker_error'
            failure = StreamFailure(code, type(exc).__name__+': '+str(exc))
        return receiver.result(failure)


def main():
    try:
        job = json.loads(sys.stdin.read())
        envelope = run_job(job)
    except BaseException:
        envelope = {'http_status': None, 'error_code': 'stream_worker_error',
                    'error_message': 'Unable to process the HTTP worker job',
                    'stream_metrics': {'completed': False, 'saw_done': False,
                                       'terminal_usage_confirmed': False}}
    sys.stdout.write(json.dumps(envelope, ensure_ascii=True, allow_nan=False)+'\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()  # An optional --http-worker argv marker is deliberately ignored.
