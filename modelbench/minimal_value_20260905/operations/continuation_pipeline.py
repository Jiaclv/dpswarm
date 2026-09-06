"""Finite A1 completion observer; A2 launch is deliberately absent.

Wait for the already-authorized final A1 group, verify its terminal resources,
and call the original frozen-source engineering gate without changing results.
This observer starts no candidate, grader, container or qualification process.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import pipeline as previous

REPO, OPERATIONS = previous.REPO, previous.OPERATIONS
MODULE = 'modelbench.minimal_value_20260905.operations.continuation_pipeline'
MAX_TOTAL_SECONDS = 96 * 3600
read, write, utc = previous.read, previous.write, previous.utc


class ObserverError(RuntimeError):
    pass


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ObserverError('Original package start must include its timezone')
    return parsed


def original_runtime(batch):
    manifest, sources, transport = previous.original_runtime(batch)
    if manifest.get('stage') != 'a1' or manifest.get('scheduled_episodes') != 10:
        raise ObserverError('Only the existing ten-episode A1 may be observed')
    return manifest, sources, transport


def resource_absence(batch, manifest, *, cli=None, inspect=None, clock=time.monotonic):
    """Read-only ownership check; this function never cleans or starts anything."""
    if cli is None:
        from .. import cli
    owned = {}
    for entry in manifest['schedule']:
        owned.update(cli._recorded_containers(Path(batch) / 'results' / entry['run_id']))
    deadline = clock() + 180
    if inspect is None:
        def inspect(identity, timeout):
            return subprocess.run(['docker', 'inspect', identity], capture_output=True,
                        text=True, encoding='utf-8', errors='replace', timeout=timeout)
    for identity in sorted(owned):
        remaining = deadline - clock()
        if remaining <= 0:
            raise ObserverError('Read-only owned-resource verification timed out')
        result = inspect(identity, min(20, remaining))
        if result.returncode == 0 or 'no such' not in result.stderr.lower():
            raise ObserverError('An A1-owned container remains or its absence is unknown')
    return {'confirmed': True, 'checked_identities': sorted(owned),
            'method': 'read-only Docker inspect of all recorded A1 owner identities'}


def gate_evidence(batch, manifest, *, sha=None):
    if sha is None:
        from ..cli import sha
    rows, hashes = [], {}
    for entry in manifest['schedule']:
        path = Path(batch) / 'results' / entry['run_id'] / 'episode_result.json'
        if not path.is_file():
            raise ObserverError('Missing A1 terminal result: ' + entry['run_id'])
        result = read(path)
        hashes[str(path.resolve())] = sha(path)
        row = {'run_id': entry['run_id'], 'arm': entry['arm'],
               'official_resolved': result.get('official_resolved'),
               'accounting_integrity_passed': (result.get('accounting_integrity') or {}).get('passed'),
               'workers_with_actual_calls': result.get('workers_with_actual_calls'),
               'team_execution_valid': result.get('team_execution_valid'),
               'over_token_limit': (result.get('budget') or {}).get('over_token_limit'),
               'total_tokens': (result.get('budget') or {}).get('total_tokens'),
               'over_token_limit_is_not_itself_checked_by_original_gate': True}
        if entry['arm'] in ('D', 'T'):
            row['workers'] = [{key: worker.get(key) for key in (
                'worker_id', 'worker_role', 'model', 'status', 'delta_status',
                'delta_bytes', 'patch_sha256', 'review_decision', 'death_phase')}
                for worker in result.get('workers', [])]
            row['noncompleted_worker_ids'] = [w.get('worker_id') for w in result.get('workers', [])
                                             if w.get('status') != 'completed']
        rows.append(row)
    return {'episode_result_hashes': hashes, 'episodes': rows,
            'boundary': 'Raw fields are preserved; present delta is not evidence of a completed worker'}


def run(a1_batch, run_dir, *, package_started_at=None, runtime=original_runtime,
        alive=previous.controller_alive, absent=resource_absence, validate=None,
        clock=time.monotonic, sleep=time.sleep, now=lambda: datetime.now(timezone.utc)):
    """Observe once, with no resume, no stage mutation and no A2 dispatch path."""
    from .. import cli
    batch, directory = Path(a1_batch).resolve(), Path(run_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / 'observer_state.json').exists():
        raise ObserverError('Existing observer state is never implicitly resumed')
    with (directory / 'controller.lock').open('x', encoding='utf-8') as stream:
        json.dump({'pid': os.getpid(), 'started_at': utc()}, stream)
    record = {'status': 'running', 'phase': 'waiting_for_a1', 'started_at': utc(),
              'a1_batch': str(batch), 'a2_started': False, 'qualification_started': False,
              'model_calls_started': 0, 'containers_started': 0,
              'maximum_package_seconds': MAX_TOTAL_SECONDS,
              'scope': 'Completion observation and original A1 gate only; A2 remains unstarted'}
    write(directory / 'observer_state.json', record)
    source_hashes = {}
    try:
        manifest, sources, transport = runtime(batch)
        state = read(batch / 'state.json')
        anchor = state['started_at']
        if package_started_at is not None and parse_time(anchor) != parse_time(package_started_at):
            raise ObserverError('Original package anchor changed after observer admission')
        elapsed = max(0.0, (now() - parse_time(anchor)).total_seconds())
        deadline = clock() + max(0.0, MAX_TOTAL_SECONDS - elapsed)
        record.update(package_started_at=anchor, elapsed_before_observer_seconds=elapsed,
                      manifest_sha256=cli.sha(batch / 'manifest.json'))
        while True:
            if any((directory / marker).exists() for marker in ('STOP', 'CANCEL')):
                raise ObserverError('observer_stop_requested')
            if clock() >= deadline:
                raise ObserverError('original_96_hour_package_deadline_elapsed')
            state = read(batch / 'state.json')
            if state.get('started_at') != anchor:
                raise ObserverError('Original stage start was reset')
            running = alive(batch)
            record.update(last_checked_at=utc(), observed_completed_episodes=state.get('completed_episodes'),
                          observed_stop_reason=state.get('stop_reason'), controller_running=running)
            write(directory / 'observer_state.json', record)
            if running:
                sleep(min(10, max(.01, deadline - clock())))
                continue
            if (state.get('completed_episodes') != 10 or not state.get('completed_at')
                    or state.get('active_run_id') or state.get('active_episodes')):
                raise ObserverError('A1 controller exited without all ten terminal episodes')
            if Path(manifest['resource_lock_path']).exists():
                raise ObserverError('A1 controller exited while its resource lease remains')
            break
        record['resource_absence'] = absent(batch, manifest)
        if record['resource_absence'].get('confirmed') is not True:
            raise ObserverError('A1 resource cleanup is unconfirmed')
        evidence = gate_evidence(batch, manifest)
        source_hashes = dict(evidence['episode_result_hashes'])
        source_hashes[str(batch / 'state.json')] = cli.sha(batch / 'state.json')
        source_hashes[str(batch / 'manifest.json')] = cli.sha(batch / 'manifest.json')
        record['source_hashes'] = source_hashes
        write(directory / 'a1-gate-evidence.json', evidence | {'source_hashes': source_hashes})
        record['phase'] = 'validating_original_a1_gate'
        write(directory / 'observer_state.json', record)
        gate = validate or cli.validate_a1
        try:
            result = gate(batch, expected_sources=sources, expected_transport=transport)
            if result.get('passed') is not True:
                raise ObserverError('Original A1 gate returned no passing result')
        except Exception as exc:
            record['a1_validation'] = {'passed': False, 'error_type': type(exc).__name__, 'error': str(exc)}
            record.update(status='blocked', phase='a1_gate_blocked', reason=str(exc))
        else:
            record['a1_validation'] = result
            record.update(status='passed', phase='a1_gate_passed_observer_complete')
        for path, expected in source_hashes.items():
            if cli.sha(path) != expected:
                raise ObserverError('Original A1 evidence changed during observation')
        record['original_evidence_unchanged'] = True
    except Exception as exc:
        record.update(status='blocked', phase='observation_blocked', reason=str(exc), error_type=type(exc).__name__)
    record.update(completed_at=utc(), a2_started=False, qualification_started=False)
    write(directory / 'observer_state.json', record)
    write(directory / 'result.json', record)
    return record


def launch(a1_batch, run_dir, *, popen=subprocess.Popen):
    """Explicit caller action: starts only this read-only completion observer."""
    batch, directory = Path(a1_batch).resolve(), Path(run_dir).resolve()
    if directory.exists():
        raise ObserverError('Observer launch directory already exists')
    original_runtime(batch)
    anchor = read(batch / 'state.json')['started_at']
    elapsed = (datetime.now(timezone.utc) - parse_time(anchor)).total_seconds()
    if elapsed < 0 or elapsed >= MAX_TOTAL_SECONDS:
        raise ObserverError('Original package anchor is future or expired')
    directory.mkdir(parents=True)
    argv = [sys.executable, '-B', '-m', MODULE, 'run', '--a1-batch', str(batch),
            '--run-dir', str(directory), '--package-started-at', anchor]
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    with (directory / 'stdout.log').open('xb') as out, (directory / 'stderr.log').open('xb') as err:
        process = popen(argv, cwd=REPO, env=previous.child_environment(), stdout=out, stderr=err, **options)
    descriptor = {'pid': process.pid, 'argv': argv, 'started_at': utc(), 'package_started_at': anchor,
                  'maximum_package_seconds': MAX_TOTAL_SECONDS, 'a2_dispatch_capability': False}
    write(directory / 'launch.json', descriptor)
    return descriptor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('launch', 'run'))
    parser.add_argument('--a1-batch', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--package-started-at')
    args = parser.parse_args()
    result = (launch(args.a1_batch, args.run_dir) if args.command == 'launch' else
              run(args.a1_batch, args.run_dir, package_started_at=args.package_started_at))
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 1 if result.get('status') == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
