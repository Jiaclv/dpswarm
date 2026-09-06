"""Explicit evidence-only recovery of the known post-commit stdout failure.

No provider or grader is invoked. Immutable inputs and overwritten projections are
hash-bound before the canonical ledger and existing official result are joined.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def build_result(batch):
    from modelbench.minimal_value_20260905 import cli, freeze, reporting
    from modelbench.minimal_value_20260905.budget import EpisodeBudget
    batch = Path(batch).resolve()
    manifest = cli.load_manifest(batch)
    freeze.verify_snapshot(batch, manifest)
    require(freeze.runtime_sources() == manifest['runtime_sources'], 'Current runtime differs from frozen source')
    require(manifest['stage'] == 'a1', 'Recovery is restricted to the first A1 episode')
    entry = manifest['schedule'][0]
    require(entry['run_id'] == 'a1-01-D', 'Unexpected first episode')
    directory = batch / 'results' / entry['run_id']
    state = cli.read(batch / 'state.json')
    require(state.get('completed_episodes') == 1 and state.get('token_admission_sum') == 600000,
            'Recovery cannot reset or discard additional admissions')
    require(state.get('stop_reason') == 'episode_process_failed', 'Unexpected original stop reason')
    require(len(state.get('episodes', [])) == 1 and not state.get('active_run_id'), 'Unexpected completed prefix')
    require({p.name for p in (batch / 'results').iterdir()} == {entry['run_id']}, 'Other episode artifacts exist')
    for marker in ('STOP', 'CANCEL'):
        require(not (batch / marker).exists(), 'Explicit stop marker remains')
    require(not Path(manifest['resource_lock_path']).exists(), 'Shared lease is still held')
    raw_path = directory / 'result.json'
    ledger_path = directory / 'episode-budget.json'
    score_path = directory / 'grade-1' / 'grader' / 'result.json'
    stderr = batch / 'controller-logs' / (entry['run_id'] + '.stderr.log')
    failure_text = stderr.read_text(encoding='utf-8', errors='replace')
    require("UnicodeEncodeError: 'gbk' codec" in failure_text and 'print(json.dumps(result' in failure_text,
            'Known post-commit stdout failure is not proven')
    result = copy.deepcopy(cli.read(raw_path))
    require(result.get('infrastructure_error') is None and result.get('quiesced') is True
            and result.get('cleanup_confirmed') is True, 'Raw candidate did not finish cleanly')
    require(result.get('run_id') == entry['run_id'] and result.get('instance_id') == entry['instance']['instance_id'],
            'Raw candidate identity differs from schedule')
    artifact = result['artifact']
    patch = Path(artifact['path']).resolve()
    require(patch.is_relative_to(directory) and cli.sha(patch) == artifact['sha256'], 'Candidate patch changed')
    snapshot = cli.read(ledger_path)
    budget = EpisodeBudget.from_snapshot(snapshot)
    summary = budget.summary()
    require(summary['frozen'] is True and summary['pending_call_count'] == 0
            and summary['unknown_call_count'] == 0, 'Canonical ledger is not settled and frozen')
    score = cli.read(score_path)
    require(score.get('completed') is True and type(score.get('resolved')) is bool, 'Official grading is incomplete')
    require(score.get('patch_sha256') == artifact['sha256'], 'Official grading used a different patch')
    require(score.get('instance_id') == entry['instance']['instance_id']
            and score.get('base_commit') == entry['instance']['base_commit']
            and score.get('image_id') == entry['image'], 'Official task/image identity mismatch')
    require(score.get('process_exit_code') == 0 and score.get('cleanup_errors') == [], 'Official grader did not close cleanly')
    contract = entry['grader_contract']
    for key in ('scope', 'input_artifacts', 'environment_sha'):
        require(score.get('binding', {}).get(key) == contract[key], 'Official grading contract mismatch: ' + key)
    require(score.get('reports') and set(score['reports']) == set(score.get('reports_sha256', {})), 'Official reports unbound')
    request_path = score_path.parent / 'request.json'
    request = cli.read(request_path)
    for key in ('instance_id', 'base_commit', 'image_id', 'patch_sha256'):
        require(request.get(key) == score[key], 'Official request identity mismatch: ' + key)
    require(request.get('grader_contract') == contract and request.get('model_name') == entry['arm'],
            'Official request used a different grading contract')
    graded_patch = Path(request['patch_path']).resolve()
    require(graded_patch.is_relative_to(directory) and cli.sha(graded_patch) == artifact['sha256'],
            'Copied official patch differs from candidate')
    sources = [batch / 'manifest.json', batch / 'manifest.sha256', batch / 'gate.json',
               raw_path, ledger_path, score_path, request_path, graded_patch, patch, stderr]
    for relative, expected in score['reports_sha256'].items():
        path = (score_path.parent / relative).resolve()
        require(path.is_relative_to(score_path.parent) and cli.sha(path) == expected, 'Official report changed')
        report = cli.read(path)
        item = report[entry['instance']['instance_id']]
        require(item.get('resolved') is score['resolved'] and item.get('patch_successfully_applied') is True,
                'Official report and result disagree')
        sources.append(path)
    result.update(budget=summary, root_budget_snapshot=snapshot, score=score,
                  grading_attempts=[score], grading_pending=False, episode_wall_seconds=None)
    account = reporting.accounting(directory, cli.read(cli.HERE / 'pricing.json'))
    integrity = reporting.audit_accounting(directory, result, account)
    require(integrity.get('passed') is True and account['cost_computable'] and not account['protocol_issues'],
            'Recovered accounting is not admissible')
    require(abs(state['known_cost_usd'] - account['api_equivalent_known_subtotal_usd']) < 1e-12,
            'Recovery would alter previously observed stage cost')
    result.update(run_id=entry['run_id'], arm=entry['arm'], instance_id=entry['instance']['instance_id'],
                  accounting=account, accounting_integrity=integrity,
                  api_equivalent_usd=account['api_equivalent_usd'],
                  api_equivalent_known_subtotal_usd=account['api_equivalent_known_subtotal_usd'])
    result.update(reporting.result_status(result, entry))
    require(reporting.stop_reason(result) is None, 'Recovered result still has an experiment stop condition')
    source_hashes = {str(path): cli.sha(path) for path in sources}
    return manifest, entry, state, result, source_hashes


def recover(batch, recovery_id):
    from modelbench.minimal_value_20260905 import cli, reporting
    from modelbench.minimal_value_20260905.operations.pipeline import controller_alive
    import psutil
    batch = Path(batch).resolve()
    require(recovery_id == 'stdout-utf8-20260905', 'Only the reviewed recovery is supported')
    manifest, entry, state, result, sources = build_result(batch)
    require(not controller_alive(batch), 'Original controller is still active')
    for process in psutil.process_iter(['pid', 'cmdline']):
        command = process.info['cmdline'] or []
        require(not (str(batch) in command and any(x in command for x in ('_run', '_episode'))),
                'An experiment child is still active')
    directory = batch / 'results' / entry['run_id']
    owned = cli._recorded_containers(directory)
    for identity in owned:
        observed = subprocess.run(['docker', 'inspect', identity], capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=20)
        require(observed.returncode != 0 and 'no such' in observed.stderr.lower(),
                'An owned container remains or its absence cannot be verified')
    recovery = batch / 'recoveries' / recovery_id
    require(not recovery.exists(), 'Recovery artifacts already exist; investigate before another write')
    now = cli.utc()
    with cli.stage_lease(manifest['resource_lock_path'], batch):
        for path, expected in sources.items():
            require(cli.sha(path) == expected, 'Evidence changed before recovery commit')
        before = recovery / 'before'
        before.mkdir(parents=True)
        backup = {}
        for relative in ('state.json', 'launch.json', 'controller.lock', 'summary.json', 'calls.json',
                         'episodes.csv', 'REPORT_ZH.md', 'results/a1-01-D/episode_result.json'):
            source = batch / relative
            if source.is_file():
                target = before / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                require(cli.sha(source) == cli.sha(target), 'Backup verification failed')
                backup[relative] = {'path': str(target), 'sha256': cli.sha(target)}
        record_path = recovery / 'recovery.json'
        record = {'recovery_id': recovery_id, 'status': 'prepared', 'at': now,
                  'authorization': {'user_reply': '那就继续', 'scope': 'Recover completed first episode without rerun and continue the already authorized 10 plus 80 package'},
                  'reason': 'Post-commit GBK stdout encoding exception followed by parent overwrite of the terminal projection',
                  'sources': sources, 'before': backup, 'model_calls_added': 0, 'grader_calls_added': 0,
                  'immutable_runtime_changed': False, 'owned_container_absence_verified': owned,
                  'unknown_fields': ['episode_wall_seconds including grading; original monotonic value was overwritten'],
                  'preserved_inference_wall_seconds': result['inference_wall_seconds']}
        cli.atomic_json(record_path, record)
        result['recovery'] = {'record': str(record_path), 'recovery_id': recovery_id, 'recovered_at': now,
                              'episode_wall_seconds_status': 'unknown_original_terminal_projection_overwritten'}
        result['controller_completed_at'] = None
        cli.atomic_json(directory / 'episode_result.json', result)
        state['episodes'][0]['sha256'] = cli.sha(directory / 'episode_result.json')
        state['stop_reason'] = None
        state.pop('completed_at', None)
        state.pop('supervision', None)
        state['recovery'] = {'status': 'evidence_restored_awaiting_explicit_resume', 'record': str(record_path),
                             'recovery_id': recovery_id, 'restored_at': now}
        cli.atomic_json(batch / 'state.json', state)
        reporting.write_report(batch)
        for path, expected in sources.items():
            require(cli.sha(path) == expected, 'Recovery unexpectedly changed immutable evidence')
        record.update(status='evidence_restored', restored_result_sha256=cli.sha(directory / 'episode_result.json'),
                      restored_state_sha256=cli.sha(batch / 'state.json'), accounting_integrity=result['accounting_integrity'],
                      official_resolved=result['official_resolved'], calls=result['accounting']['call_count'],
                      tokens=result['accounting']['total_tokens_known_subtotal'], api_equivalent_usd=result['api_equivalent_usd'])
        cli.atomic_json(record_path, record)
    return {key: record[key] for key in ('status', 'official_resolved', 'calls', 'tokens', 'api_equivalent_usd')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--recovery-id', default='stdout-utf8-20260905')
    parser.add_argument('--commit', action='store_true')
    args = parser.parse_args()
    if args.commit:
        output = recover(args.batch, args.recovery_id)
    else:
        _, _, _, result, sources = build_result(args.batch)
        output = {'dry_run': True, 'official_resolved': result['official_resolved'],
                  'accounting_integrity': result['accounting_integrity'], 'source_files': len(sources)}
    print(json.dumps(output, ensure_ascii=True))
