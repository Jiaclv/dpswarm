"""One tiny live Codex call proving the terminal-message binding end to end.

This is an infrastructure smoke check, not a candidate benchmark call: the
prompt is a fixed synthetic instruction, no SWE task content is sent, and the
result is recorded under validation/smoke_revision3/ with full raw artifacts.
"""
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.swe_verified_20260903.transport import SweTransport

FINISH = [{'type': 'function', 'function': {'name': 'finish', 'description': 'End the task',
    'parameters': {'type': 'object', 'properties': {'status': {'type': 'string'}, 'summary': {'type': 'string'}},
                   'required': ['status', 'summary'], 'additionalProperties': False}}}]


def main():
    root = Path(__file__).resolve().parent / 'smoke_revision3'
    transport = SweTransport(root)
    record = transport.complete(
        'gpt-5.6-sol',
        [{'role': 'user', 'content': 'Smoke test of the terminal-message contract. '
                                     'Request the finish tool now with status "completed" and summary "smoke".'}],
        tools=FINISH, run_id='revision3-smoke', role='lead', task_id='smoke-terminal-binding',
        call_id='revision3-smoke-codex-terminal-1', max_tokens=32768, timeout_seconds=180)
    folder = Path(record['raw_artifacts']['directory'])
    request = json.loads((folder / 'process-request.json').read_text(encoding='utf-8'))
    checks = {
        'error_is_none': record['error'] is None,
        'protocol_error_is_none': record['protocol_error'] is None,
        'action_kind_tools': (record.get('action') or {}).get('kind') == 'tools',
        'action_is_finish': (record.get('parsed_calls') or [{}])[0].get('name') == 'finish',
        'argv_has_output_last_message': '--output-last-message' in request['argv'],
        'terminal_artifact_saved': (folder / 'last-message.txt').is_file(),
        'selection_recorded': record.get('codex_response_selection', {}).get('policy')
                              == 'codex-output-last-message-exact-v1',
        'usage_known': record.get('total_tokens') is not None,
        'total_tokens': record.get('total_tokens'),
        'wall_seconds': record.get('wall_seconds'),
    }
    checks['pass'] = all(v for k, v in checks.items()
                         if k in ('error_is_none', 'protocol_error_is_none', 'action_kind_tools',
                                  'action_is_finish', 'argv_has_output_last_message',
                                  'terminal_artifact_saved', 'selection_recorded', 'usage_known'))
    out = root / 'smoke_result.json'
    out.write_text(json.dumps({'checks': checks, 'call_id': record['call_id']}, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    sys.exit(0 if checks['pass'] else 1)


if __name__ == '__main__':
    main()
