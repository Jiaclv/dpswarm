"""Offline replay of pilot_v2 GPT calls through the terminal-message binding.

Read-only over frozen evidence. For every Codex (non-native) call in the given
runs, emulate the CLI's --output-last-message artifact (last non-empty completed
agent_message), bind it with _codex_terminal_text, and parse with the call's own
prompt.json tools. Matplotlib calls must reproduce the recorded action exactly;
the seven Sphinx Luna 'Extra data' calls must now bind and parse cleanly, with
every non-empty message individually parsing to the same action.

No model, container, grader, or candidate feedback. Usage totals are not
recomputed; failed historical calls keep their recorded cost.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.swe_verified_20260903.transport import CodexTerminalError, _codex_terminal_text  # noqa: F401  (adds dpswarm-plugin to sys.path)
from dpswarm.team_runtime.protocol import ProtocolError, parse_text_response


def agent_message_texts(stdout):
    texts = []
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        events.append(event)
        if (event.get('type') == 'item.completed' and isinstance(event.get('item'), dict)
                and event['item'].get('type') == 'agent_message'):
            texts.append(event['item'].get('text', ''))
    return events, texts


def replay_call(folder):
    meta = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
    prompt = json.loads((folder / 'prompt.json').read_text(encoding='utf-8'))
    stdout_path = folder / 'stdout.jsonl'
    result = {'call_id': meta['call_id'], 'model': meta['model_requested'],
              'folder': folder.name, 'recorded_action': meta.get('action'),
              'recorded_error': (meta.get('error') or {}).get('message')}
    if meta.get('adapter_mode') == 'glm_native_tools_swe' or str(meta.get('model_requested', '')).startswith('glm-'):
        result['skipped'] = 'native_glm'
        return result
    if not stdout_path.exists():
        result['skipped'] = 'no_stdout'
        return result
    stdout = stdout_path.read_text(encoding='utf-8')
    events, texts = agent_message_texts(stdout)
    non_empty = [text for text in texts if text.strip()]
    result['agent_message_count'] = len(texts)
    result['non_empty_message_count'] = len(non_empty)
    record = {}
    terminal_text = non_empty[-1] if non_empty else None
    try:
        bound = _codex_terminal_text(stdout, terminal_text, record)
        result['selection'] = record.get('codex_response_selection')
    except CodexTerminalError as exc:
        result.update(new_status='binding_error', code=exc.code, message=str(exc))
        return result
    try:
        action = parse_text_response(bound, prompt['tools'])
    except ProtocolError as exc:
        result.update(new_status='protocol_error', code=exc.code, message=str(exc))
        return result
    result['new_status'] = 'ok'
    result['new_action'] = action
    # Every non-empty message must individually parse to the same action, proving
    # the duplicated-envelope failure shape rather than divergent model intent.
    individual = []
    for text in non_empty:
        try:
            parsed = parse_text_response(text, prompt['tools'])
            individual.append(parsed.get('calls'))
        except ProtocolError as exc:
            individual.append('protocol_error:' + exc.code)
    result['individual_message_calls_identical'] = len({json.dumps(c, sort_keys=True, default=str)
                                                        for c in individual}) <= 1
    recorded = result['recorded_action']
    if recorded is None:
        result['matches_recorded'] = None  # historically failed call; nothing to match
    else:
        result['matches_recorded'] = (action == recorded)
    result['bound_equals_recorded_text'] = (bound == meta.get('text'))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--batch', type=Path, required=True, help='pilot_v2 batch directory with results/')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results_dir = args.batch / 'results'
    calls = []
    for run in sorted(results_dir.iterdir()):
        calls_dir = run / 'calls'
        if not calls_dir.is_dir():
            continue
        for folder in sorted(calls_dir.iterdir()):
            if (folder / 'metadata.json').is_file() and (folder / 'prompt.json').is_file():
                outcome = replay_call(folder)
                outcome['run_id'] = run.name
                calls.append(outcome)
    gpt_calls = [c for c in calls if not c.get('skipped')]
    mismatches = [c['call_id'] for c in gpt_calls if c.get('matches_recorded') is False]
    rebound = [c for c in gpt_calls if c.get('matches_recorded') is None]
    summary = {
        'batch': str(args.batch),
        'calls_examined': len(calls),
        'gpt_calls_replayed': len(gpt_calls),
        'skipped_native_glm': len([c for c in calls if c.get('skipped') == 'native_glm']),
        'new_status_counts': {status: len([c for c in gpt_calls if c.get('new_status') == status])
                              for status in {c.get('new_status') for c in gpt_calls}},
        'historically_ok_all_match_recorded_action': not mismatches,
        'mismatch_call_ids': mismatches,
        'historically_failed_call_ids_rebound': [c['call_id'] for c in rebound],
        'rebound_now_ok': len([c for c in rebound if c.get('new_status') == 'ok']),
        'rebound_with_divergent_message_actions': [c['call_id'] for c in rebound
                                                   if not c.get('individual_message_calls_identical', True)],
        'scope': 'Read-only replay over frozen pilot_v2 evidence; no usage recomputed, no candidate feedback.',
    }
    payload = {'summary': summary, 'calls': calls}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
