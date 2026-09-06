"""Finite, model-free A2 preparation using the declared engineering-gate revision.



Qualification is the only parallel phase (four independent task environments).

Candidate model dispatch remains an explicit call to parallel_controller.

"""

from __future__ import annotations



import argparse

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from datetime import datetime, timezone, timedelta

import hashlib

import json

import os

from pathlib import Path

import shutil

import subprocess

import sys

import time



HERE = Path(__file__).resolve().parent

REPO = HERE.parents[2]

MODULE = 'modelbench.minimal_value_20260905.operations.a2_parallel_prepare'

MAX_SECONDS = 57600

WORKERS = 4





def read(path):

    return json.loads(Path(path).read_text(encoding='utf-8'))





def sha(path):

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()





def utc():

    return datetime.now(timezone.utc).isoformat()





def write(path, value):

    from ..runtime_integrity import atomic_json

    atomic_json(Path(path), value)





def frozen(path, value):

    from ..data import freeze_json

    freeze_json(Path(path), value)





def check_stop(run_dir, a1, *, deadline=None):

    # Historical pipeline STOPs belong to closed launches. New user authority is

    # recorded in this run's contract; never delete or reinterpret those files.

    for root in (run_dir, a1):

        if any((Path(root) / marker).exists() for marker in ('STOP', 'CANCEL')):

            raise RuntimeError('Current preparation or predecessor STOP/CANCEL')

    if deadline is not None and time.monotonic() >= deadline:

        raise TimeoutError('Preparation/package deadline')





def remaining_package(a1):

    start = datetime.fromisoformat(read(Path(a1) / 'state.json')['started_at'].replace('Z', '+00:00'))

    if start.tzinfo is None:

        raise ValueError('Package anchor has no timezone')

    return min(MAX_SECONDS, (start + timedelta(hours=96) - datetime.now(timezone.utc)).total_seconds())





def install_gate(a1, run_dir):

    from .. import cli, freeze

    from ..transport_identity import transport_identity

    from . import a1_engineering_gate as revised

    check_stop(run_dir, a1)

    evidence = revised.validate_a1(a1, expected_sources=freeze.runtime_sources(),

                                   expected_transport=transport_identity())

    if evidence.get('passed') is not True:

        raise ValueError('Revised engineering gate failed')

    # Explicit, process-local alias; source and original failures stay intact.

    cli.validate_a1 = revised.validate_a1

    frozen(Path(run_dir) / 'gate-installation.json', {

        'revision_source': str(Path(revised.__file__).resolve()),

        'revision_sha256': sha(revised.__file__), 'evidence': evidence,

        'patched_binding': 'modelbench.minimal_value_20260905.cli.validate_a1',

        'scope': 'this preparation process only', 'installed_at': utc(),

        'old_source_unchanged': True, 'model_calls': 0})

    return evidence





def qualify(a1, run_dir, *, attempt='a2_attempt_01', definitions=None, provenance=None, sympy_native_public_checks=False):

    from .. import cli, data, environment

    from . import prepare_a2, memory_profile

    run_dir, a1 = Path(run_dir).resolve(), Path(a1).resolve()

    check_stop(run_dir, a1)

    remaining = remaining_package(a1)

    if remaining <= 0:

        raise TimeoutError('Original first-package deadline has expired')

    deadline = time.monotonic() + remaining

    evidence = install_gate(a1, run_dir)

    staging = run_dir / 'staged-inputs'

    if (definitions is None) != (provenance is None):
        raise ValueError('Definitions and provenance must be provided together')
    prepare_a2.stage_a2_inputs(staging_dir=staging, definitions_path=definitions, provenance_path=provenance)
    if sympy_native_public_checks:
        from . import sympy_public_checks_v3 as sympy_public_checks
        sympy_public_checks.install()
        frozen(run_dir / 'public-runner-installation.json', {
            'adapter_path': str(Path(sympy_public_checks.__file__).resolve()),
            'adapter_sha256': sha(sympy_public_checks.__file__), 'installed_at': utc(),
            'patched_binding': 'prepare_a2.public_check_execution',
            'scope': 'public base-commit SymPy native runner counts only', 'model_calls': 0})

    publication = prepare_a2.publish_a2_inputs(a1, staging_dir=staging)

    memory_profile.install(HERE / 'resource-defaults.json', run_dir / 'memory-profile-installation.json')

    manifest = cli.load_manifest(a1)

    owned = prepare_a2.qualification_owned_root(attempt)

    if owned.exists():

        raise ValueError('This preparation attempt already exists; explicit audit required')

    contract = environment.capture_grader_contract()

    public = data.load_public('d1', prepare_a2._official())

    checks = read(prepare_a2._official() / 'public_checks.json')

    if len(public) != 16:

        raise ValueError('The frozen 16-task D1 selection is required')

    result = {'status': 'running', 'phase': 'qualification', 'started_at': utc(),

              'model_calls': 0, 'parallel_qualification_tasks': WORKERS,

              'task_count': 16, 'qualified_count': 0, 'completed': [],

              'owned_root': str(owned), 'a1_evidence': evidence, 'all_qualified': False}

    stop = run_dir / 'STOP'

    with cli.stage_lease(manifest['resource_lock_path'], owned) as lease:

        frozen(owned / 'preparation-contract.json', {

            'protocol': 'a2_parallel_model_free_preparation_v1', 'source_sha256': sha(__file__),

            'grader_contract': contract, 'publication_sha256': publication['publication_sha256'],

            'a1_evidence': evidence, 'attempt': attempt, 'wall_seconds': remaining,

            'parallel_tasks': WORKERS, 'candidate_memory': '1g', 'evaluation_memory': '1g',

            'model_calls': 0, 'profile_sha256': sha(HERE / 'resource-defaults.json')})

        write(run_dir / 'preparation-state.json', result)

        pending = list(public)

        active = {}

        failed = False

        lease['retain'] = True

        try:

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:

                try:

                    while pending or active:

                        check_stop(run_dir, a1, deadline=deadline)

                        while pending and len(active) < WORKERS and not failed:

                            if environment.capture_grader_contract() != contract:

                                raise ValueError('Qualification inputs changed')

                            row = pending.pop(0)

                            fut = pool.submit(prepare_a2._qualify_one, row, contract=contract,

                                checks=checks[row['instance_id']], owned_root=owned, attempt=attempt,

                                deadline=deadline, stop_file=stop)

                            active[fut] = row['instance_id']

                        result['active_tasks'] = list(active.values())

                        result['pending_tasks'] = len(pending)

                        write(run_dir / 'preparation-state.json', result)

                        if not active:

                            break

                        done, _ = wait(active, timeout=5, return_when=FIRST_COMPLETED)

                        for future in done:

                            identity = active.pop(future)

                            try:

                                summary = future.result()

                            except Exception as exc:

                                summary = {'qualified': False, 'cleanup_confirmed': False,

                                           'error_type': type(exc).__name__, 'error': str(exc)}

                            if summary.get('cleanup_confirmed') is not True:

                                lease['retain'] = True

                            passed = summary.get('qualified') is True

                            failed |= not passed

                            result['qualified_count'] += int(passed)

                            result['completed'].append({'instance_id': identity, 'qualified': passed,

                                'cleanup_confirmed': summary.get('cleanup_confirmed'),

                                'error_type': summary.get('error_type'),

                                'path': str(owned / identity / 'qualification.json')})

                        result['last_checked_at'] = utc()

                        write(run_dir / 'preparation-state.json', result)

                        if failed and not active:

                            break

                except BaseException:

                    if not stop.exists():

                        stop.write_text('Preparation stopped; preserve completed qualification evidence.\n', encoding='utf-8')

                    raise

        finally:

            # The executor has joined every submitted task before this audit.

            cleanup = cli.cleanup_owned_episode(owned)

            lease['retain'] = cleanup.get('confirmed') is not True

            write(run_dir / 'preparation-cleanup.json', cleanup)

        result['final_cleanup_confirmed'] = cleanup.get('confirmed') is True
        result['all_qualified'] = result['qualified_count'] == 16 and result['final_cleanup_confirmed']

        result.update(status='complete' if result['all_qualified'] else 'blocked',

                      phase='qualification_finished', completed_at=utc(), active_tasks=[])

        frozen(owned / 'operation-summary.json', result)

        write(run_dir / 'preparation-state.json', result)

    return result





def freeze_a2(a1, a2, run_dir):

    from .. import cli

    run_dir = Path(run_dir).resolve()

    check_stop(run_dir, a1)

    if not read(run_dir / 'preparation-state.json').get('all_qualified'):

        raise ValueError('All 16 qualifications must pass first')

    # A fresh process needs the explicit gate alias again, with its own receipt.

    phase = run_dir / 'freeze'

    phase.mkdir(parents=True, exist_ok=False)

    install_gate(a1, phase)

    gate = cli.gate('a2', destination=phase / 'a2-gate')

    if gate.get('status') != 'PASS':

        raise ValueError('A2 runtime/input gate failed')

    manifest = cli.prepare('a2', a2, phase / 'a2-gate/gate.json', a1_batch=a1)

    frozen(phase / 'result.json', {'prepared': True, 'scheduled_episodes': len(manifest['schedule']),

                                  'manifest_sha256': sha(Path(a2) / 'manifest.json'), 'model_calls': 0})

    return {'prepared': True, 'batch': str(Path(a2).resolve()), 'model_calls': 0}





def main():

    parser = argparse.ArgumentParser()

    parser.add_argument('operation', choices=('qualify', 'freeze'))

    parser.add_argument('--a1-batch', type=Path, required=True)

    parser.add_argument('--a2-batch', type=Path)

    parser.add_argument('--run-dir', type=Path, required=True)

    parser.add_argument('--attempt', default='a2_attempt_01')
    parser.add_argument('--definitions', type=Path)
    parser.add_argument('--provenance', type=Path)
    parser.add_argument('--sympy-native-public-checks', action='store_true')

    args = parser.parse_args()

    try:

        if args.operation == 'qualify':

            value = qualify(args.a1_batch, args.run_dir, attempt=args.attempt,
                definitions=args.definitions, provenance=args.provenance,
                sympy_native_public_checks=args.sympy_native_public_checks)

        else:

            if args.a2_batch is None:

                parser.error('--a2-batch required for freeze')

            value = freeze_a2(args.a1_batch, args.a2_batch, args.run_dir)

    except Exception as exc:

        value = {'status': 'failed', 'phase': args.operation, 'at': utc(),

                 'error_type': type(exc).__name__, 'error': str(exc), 'model_calls': 0}

        write(args.run_dir / (args.operation + '-failure.json'), value)

        print(json.dumps(value, ensure_ascii=True), flush=True)

        raise SystemExit(1)

    print(json.dumps(value, ensure_ascii=True), flush=True)

    if value.get('status') == 'blocked':

        raise SystemExit(1)





if __name__ == '__main__':

    main()

