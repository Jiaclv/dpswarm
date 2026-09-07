"""Offline HTTP/SSE fixtures only; never dispatch an external request."""
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from urllib.error import HTTPError, URLError

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.big_budget_team_20260906 import glm_stream_http_worker as w

KEY = 'sk-fixture-secret-credential-abcdefgh12345678'
USAGE = {'prompt_tokens': 7, 'completion_tokens': 11, 'total_tokens': 18,
         'completion_tokens_details': {'reasoning_tokens': 8}}


def chunk(delta=None, finish=None, usage=None, index=0):
    data = {'id': 'fixture-id', 'model': 'glm-5.3-flash', 'created': 123,
            'choices': [{'index': index, 'delta': delta or {}, 'finish_reason': finish}]}
    if usage is not None:
        data['usage'] = usage
    return data


def wire(chunks, done=True):
    lines = []
    for value in chunks:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append('data: '+text+'\n\n')
    if done:
        lines.append('data: [DONE]\n\n')
    return ''.join(lines).encode('utf-8')


class FakeResponse(io.BytesIO):
    status = 200
    def __init__(self, value, failure=None):
        super().__init__(value)
        self.failure = failure
    def readline(self, size=-1):
        value = super().readline(size)
        if not value and self.failure:
            raise self.failure
        return value


def run(tmp_path, content, *, failure=None):
    called = []
    job = {'endpoint': w.ENDPOINT, 'payload': json.dumps({'model': 'glm-5.3-flash',
        'messages': [], 'stream': True, 'tool_stream': True, 'max_tokens': 32768}),
        'key': KEY, 'socket_timeout_seconds': 900, 'evidence_dir': str(tmp_path/'evidence')}
    def opener(req, timeout):
        called.append(req)
        assert req.full_url == w.ENDPOINT and timeout == 900
        assert req.headers['Authorization'] == 'Bearer '+KEY
        assert json.loads(req.data)['tool_stream'] is True
        return FakeResponse(content, failure)
    result = w.run_job(job, opener=opener)
    assert len(called) == 1
    return result


def body(result):
    return json.loads(result['raw'])


def test_fragmented_tools_unicode_reasoning_and_tail_usage(tmp_path):
    values = [chunk({'role': 'assistant', 'reasoning_content': 'think ', 'tool_calls': [
        {'index': 1, 'id': 'tool-2', 'type': 'function', 'function': {'name': 'ba', 'arguments': '{"c'}},
        {'index': 0, 'id': 'tool-1', 'type': 'function', 'function': {'name': 're', 'arguments': '{"path":'}}]}),
        chunk({'role': 'assistant', 'reasoning_content': 'again', 'content': '你好', 'tool_calls': [
            {'index': 0, 'function': {'name': 'ad', 'arguments': '"文档.py"}'}},
            {'index': 1, 'function': {'name': 'sh', 'arguments': 'ommand":"echo ok"}'}}]}),
        chunk(finish='tool_calls'), {'id': 'fixture-id', 'model': 'glm-5.3-flash', 'choices': [], 'usage': USAGE}]
    result = run(tmp_path, wire(values))
    assert 'error_code' not in result
    data = body(result); message = data['choices'][0]['message']
    assert message['content'] == '你好' and message['reasoning_content'] == 'think again'
    assert [(t['id'], t['function']['name']) for t in message['tool_calls']] == [('tool-1', 'read'), ('tool-2', 'bash')]
    assert json.loads(message['tool_calls'][0]['function']['arguments']) == {'path': '文档.py'}
    assert data['usage'] == USAGE
    m = result['stream_metrics']
    assert m['completed'] and m['saw_done'] and m['all_choices_finished'] and m['terminal_usage_confirmed']
    assert m['chunk_count'] == 4 and m['event_count'] == 5
    for field in ['headers_seconds','first_data_seconds','first_content_seconds','first_reasoning_seconds','first_tool_seconds']:
        assert m[field] is not None and m[field] <= m['wall_seconds']
    progress = json.loads(Path(result['stream_artifacts']['progress']).read_text(encoding='utf-8'))
    assert progress['stream_metrics']['completed']
    assert 'reasoning_content' not in json.dumps(progress)


def test_usage_with_finish_length_is_preserved_not_summed(tmp_path):
    result = run(tmp_path, wire([chunk({'reasoning_content':'long'}, usage=USAGE),
        chunk({'content':''}, finish='length', usage=USAGE)]))
    assert body(result)['usage']['total_tokens'] == 18
    assert result['stream_metrics']['completed'] and result['stream_metrics']['finish_reason'] == 'length'
    assert result['stream_metrics']['terminal_usage_confirmed']


@pytest.mark.parametrize('chunks,done', [([chunk({'content':'partial'}, finish='stop', usage=USAGE)], False),
                                       ([chunk({'content':'partial'}, usage=USAGE)], True)])
def test_eof_or_done_without_finish_exposes_no_action(tmp_path, chunks, done):
    result = run(tmp_path, wire(chunks, done))
    assert result['error_code'] == 'incomplete_stream'
    assert body(result)['choices'] == [] and body(result)['usage'] == USAGE
    assert not result['stream_metrics']['completed'] and not result['stream_metrics']['terminal_usage_confirmed']


@pytest.mark.parametrize('exception,code', [(socket.timeout('read timed out'), 'socket_timeout'),
    (ConnectionResetError('reset'), 'network_error'), (URLError(socket.timeout('read timed out')), 'socket_timeout')])
def test_disconnect_preserves_classification_partial_usage_evidence(tmp_path, exception, code):
    result = run(tmp_path, wire([chunk({'content':'partial'}, usage=USAGE)], False), failure=exception)
    assert result['error_code'] == code and body(result)['choices'] == []
    assert body(result)['usage'] == USAGE and not result['stream_metrics']['terminal_usage_confirmed']
    assert Path(result['stream_artifacts']['raw_events']).exists()
    assert json.loads(Path(result['stream_artifacts']['progress']).read_text(encoding='utf-8'))['stream_metrics']['error_code'] == code


def test_http_429_and_1302_are_retained(tmp_path):
    called = []
    def opener(req, timeout):
        called.append(1)
        raise HTTPError(w.ENDPOINT, 429, 'rate limited', {}, io.BytesIO(json.dumps({
            'error': {'code': '1302', 'message': 'secret '+KEY}}).encode()))
    result = w.run_job({'endpoint':w.ENDPOINT,'payload':'{"stream":true}', 'key':KEY,
        'socket_timeout_seconds':900,'evidence_dir':str(tmp_path/'evidence')}, opener=opener)
    assert result['http_status'] == 429 and result['error_code'] == 'http_error'
    assert body(result)['error']['code'] == '1302' and len(called) == 1
    assert KEY not in json.dumps(result)
    assert KEY not in ''.join(p.read_text(encoding='utf-8') for p in tmp_path.rglob('*') if p.is_file())


def test_malformed_json_fails_with_no_action(tmp_path):
    result = run(tmp_path, wire([chunk({'content':'partial'}), '{bad']))
    assert result['error_code'] == 'invalid_stream_json' and body(result)['choices'] == []


def test_incomplete_tool_args_with_done_keeps_terminal_usage_only(tmp_path):
    result = run(tmp_path, wire([chunk({'tool_calls':[{'index':0,'id':'x','type':'function',
        'function':{'name':'bash','arguments':'{"command":'}}]}, finish='length', usage=USAGE)]))
    assert result['error_code'] == 'invalid_tool_arguments'
    assert body(result)['choices'] == [] and body(result)['usage'] == USAGE
    assert result['stream_metrics']['completed'] and result['stream_metrics']['terminal_usage_confirmed']


def test_missing_usage_can_complete_but_is_not_invented(tmp_path):
    result = run(tmp_path, wire([chunk({'content':'finished'}, finish='stop')]))
    assert result['stream_metrics']['completed'] and not result['stream_metrics']['terminal_usage_confirmed']
    assert 'usage' not in body(result)


def test_key_split_into_single_character_deltas_never_appears_in_evidence(tmp_path):
    result = run(tmp_path, wire([chunk({'content':c}) for c in KEY]+[chunk(finish='stop',usage=USAGE)]))
    assert KEY not in json.dumps(result)
    assert result['error_code'] == 'action_redaction_mismatch'
    assert body(result)['choices'] == [] and body(result)['usage'] == USAGE
    assert result['stream_metrics']['terminal_usage_confirmed']
    events = [json.loads(line) for line in Path(result['stream_artifacts']['raw_events']).read_text(encoding='utf-8').splitlines()]
    for e in events:
        if isinstance(e.get('data'),dict):
            for c in e['data'].get('choices',[]):
                if 'content' in c.get('delta',{}):
                    assert c['delta']['content'] == {'redacted':True,'chars':1}
    assert KEY not in ''.join(p.read_text(encoding='utf-8') for p in tmp_path.rglob('*') if p.is_file())


def test_comments_crlf_and_multiline_data(tmp_path):
    value = json.dumps(chunk({'content':'ok'}, finish='stop',usage=USAGE),indent=2)
    raw = ': heartbeat\r\n\r\n'+''.join('data: '+line+'\r\n' for line in value.splitlines())+'\r\ndata: [DONE]\r\n\r\n'
    result = run(tmp_path,raw.encode())
    assert result['stream_metrics']['completed'] and body(result)['choices'][0]['message']['content']=='ok'


def test_model_identity_change_and_action_after_finish_fail(tmp_path):
    values=[chunk({'content':'safe'},finish='stop'),chunk({'content':'extra'})]
    result=run(tmp_path,wire(values))
    assert result['error_code']=='invalid_stream_payload' and body(result)['choices']==[]


def test_cli_marker_and_invalid_job_do_not_dispatch():
    result=subprocess.run([sys.executable,'-B',str(Path(w.__file__)),'--http-worker'],
        input='{}',capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and result.stderr==''
    value=json.loads(result.stdout)
    assert value['error_code']=='invalid_stream_job' and not value['stream_metrics']['completed']


def test_split_secret_in_tool_arguments_is_not_executable(tmp_path):
    text = json.dumps({'command': 'echo '+KEY})
    values = [chunk({'tool_calls':[{'index':0,'id':'call','type':'function','function':{'name':'bash','arguments':''}}]})]
    values += [chunk({'tool_calls':[{'index':0,'function':{'arguments':c}}]}) for c in text]
    values += [chunk(finish='tool_calls', usage=USAGE)]
    result = run(tmp_path,wire(values))
    assert result['error_code']=='action_redaction_mismatch' and body(result)['choices']==[]
    assert body(result)['usage']==USAGE and result['stream_metrics']['terminal_usage_confirmed']
    events=Path(result['stream_artifacts']['raw_events']).read_text(encoding='utf-8')
    assert KEY not in events and 'raw_sha256' not in events
    assert 'raw_event_hmac_sha256' in events


def test_atomic_progress_local_sharing_retry_never_retries_http(tmp_path,monkeypatch):
    replace=Path.replace; calls=[]
    def once(self,target):
        calls.append(str(target))
        if len(calls)==1:
            raise PermissionError('temporary fixture sharing violation')
        return replace(self,target)
    monkeypatch.setattr(Path,'replace',once)
    result=run(tmp_path,wire([chunk({'content':'ok'},finish='stop',usage=USAGE)]))
    assert result['stream_metrics']['completed'] and len(calls)>1


def test_multiple_choice_indices_and_id_fragments_stay_separate(tmp_path):
    values=[chunk({'content':'second'},index=1),chunk({'tool_calls':[
        {'index':0,'id':'call-','type':'function','function':{'name':'read','arguments':'{'}}]},index=0),
        chunk({'tool_calls':[{'index':0,'id':'one','function':{'arguments':'"path":"a.py"}'}}]},finish='tool_calls',index=0),
        chunk(finish='stop',usage=USAGE,index=1)]
    result=run(tmp_path,wire(values))
    assert 'error_code' not in result
    choices=body(result)['choices']
    assert [c['index'] for c in choices]==[0,1]
    assert choices[0]['message']['tool_calls'][0]['id']=='call-one'
    assert choices[1]['message']['content']=='second'
    assert result['stream_metrics']['terminal_usage_confirmed']


def test_response_identity_change_is_rejected(tmp_path):
    other=chunk({'content':'bad'},finish='stop',usage=USAGE);other['model']='glm-5.3'
    result=run(tmp_path,wire([chunk({'content':'first'}),other]))
    assert result['error_code']=='invalid_stream_payload' and body(result)['choices']==[]


def test_bad_utf8_is_classified_and_no_action_is_exposed(tmp_path):
    result=run(tmp_path,b'data: \xff\n\n')
    assert result['error_code']=='invalid_stream_utf8' and body(result)['choices']==[]


def test_standard_api_job_is_rejected_without_dispatch():
    calls=[]
    result=w.run_job({'endpoint':'https://open.bigmodel.cn/api/paas/v4/chat/completions',
        'payload':'{"stream":true,"tool_stream":true}','key':KEY,'socket_timeout_seconds':900},
        opener=lambda *a,**k:calls.append(True))
    assert result['error_code']=='invalid_stream_job' and calls==[]
    assert w.ENDPOINT=='https://open.bigmodel.cn/api/coding/paas/v4/chat/completions'
