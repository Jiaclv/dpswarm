"""One independent episode under the explicitly recorded parallel-pilot policy.

This file is an external operator wrapper, not part of the immutable runtime.
Runtime imports originate only in the original admitted batch snapshot. It
narrows the original local resource slots with group-wide file semaphores and
injects a grading barrier; it never changes a prompt, model, task or budget.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
# Import the external helper without importing a mutable modelbench package.
_spec = importlib.util.spec_from_file_location('_parallel_resource_guard', HERE / 'parallel_resources.py')
resources = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resources)


def require(condition, message):
    if not condition:
        raise resources.ResourceError(message)


def verify_operator_sources(group):
    expected = group.get('operations_sources') or {}
    names = ('parallel_controller.py', 'recovery_controller.py', 'parallel_episode.py', 'parallel_resources.py')
    supported = (set(names), set(names) | {'memory_profile.py', 'resource-defaults.json'},
                 set(names) | {'memory_profile.py', 'resource-defaults.json', 'provider_limits.py'})
    require(set(expected) in supported, 'External operator source identity is incomplete')
    for name in expected:
        require(resources.sha(HERE / name) == expected[name], 'External operator source changed: ' + name)

def load_group(group_dir, batch, run_id):
    group_dir, batch = Path(group_dir).resolve(), Path(batch).resolve()
    path = group_dir / 'group.json'
    group = resources.read(path)
    group_hash = resources.sha(path)
    verify_operator_sources(group)
    require(group.get('version') == 1 and Path(group['batch']).resolve() == batch, 'Parallel group identity mismatch')
    ids = group.get('run_ids')
    require(isinstance(ids, list) and 1 <= len(ids) <= 12 and len(ids) == len(set(ids)) and run_id in ids,
            'Parallel group must contain one to twelve unique declared episodes')
    require(all(isinstance(value, str) and value and Path(value).name == value and '/' not in value and '\\' not in value for value in ids),
            'Invalid group episode identity')
    require(resources.sha(batch / 'manifest.json') == group['manifest_sha256'], 'Parallel manifest identity changed')
    require(resources.sha(group['policy_path']) == group['policy_sha256'], 'Parallel policy changed')
    policy = resources.read(group['policy_path'])
    for key in ('global_model_slots', 'candidate_container_cap', 'manifest_sha256', 'run_ids'):
        require(group.get(key) == policy.get(key), 'Group/policy mismatch: ' + key)
    maximum = policy.get('max_parallel_episodes')
    require(policy.get('execution_mode') == 'parallel_pilot' and maximum in (4, 12) and len(ids) <= maximum
            and policy.get('per_episode_model_slots') == 4 and policy.get('global_model_slots') == 8
            and policy.get('candidate_container_cap') == {4: 7, 12: 12}.get(maximum)
            and policy.get('grader_max_containers') == 2,
            'Unreviewed parallel resource allocation')
    require(policy.get('monitor_is_hard_limit') is False, 'Resource monitoring must remain labelled as sampled protection')
    if policy.get('memory_admission_mode') == 'bounded_container_limits':
        require(policy.get('candidate_memory') == policy.get('grader_memory') == '1g'
                and policy.get('resource_profile') == 'resource-defaults.json',
                'Bounded groups require the explicit 1-GiB creation profile')
        profile_path = Path(group.get('profile_path', '')).resolve()
        require(profile_path == HERE / 'resource-defaults.json', 'Unexpected memory profile path')
        require(group.get('profile_sha256') == resources.sha(profile_path)
                == group['operations_sources'].get('resource-defaults.json')
                and 'memory_profile.py' in group['operations_sources'],
                'Memory profile identity is incomplete or changed')
    else:
        require(policy.get('memory_admission_mode') == 'monitored_overcommit' and len(ids) == 4,
                'Legacy overcommit is limited to the original four-episode protocol')
    if maximum == 12:
        require(policy.get('memory_admission_mode') == 'bounded_container_limits', 'Twelve-episode groups require creation memory limits')
        require('provider_limits.py' in group['operations_sources'], 'Provider limiter source identity is missing')
        limits_spec = importlib.util.spec_from_file_location('_validated_provider_limits', HERE / 'provider_limits.py')
        limits_module = importlib.util.module_from_spec(limits_spec)
        limits_spec.loader.exec_module(limits_module)
        limits_module.validate_policy(policy)
        for key in ('provider_limits', 'provider_limits_version', 'provider_limits_source'):
            require(group.get(key) == policy.get(key), 'Provider group/policy mismatch: ' + key)
        require(group.get('episode_admission_mode') == 'atomic_episode_candidate_quota', 'Episode capacity must be reserved before startup')
        receipt = resources.read(group_dir / 'admissions' / (run_id + '.json'))
        require(receipt.get('run_id') == run_id and receipt.get('group_sha256') == group_hash
                and receipt.get('status') == 'admitted'
                and receipt.get('candidate_quota') == (group.get('candidate_requirements') or {}).get(run_id)
                and receipt.get('candidate_quota') in (1, 2, 3), 'Episode admission receipt does not match group capacity')
    lock_dir = Path(group['global_lock_dir']).resolve()
    require(lock_dir.is_relative_to(group_dir), 'Shared resource locks escaped group')
    require(0 < group.get('wait_timeout_seconds', 0) <= (5700 if maximum == 12 else 2100), 'Invalid generation barrier timeout')
    require(0 < group.get('grader_wait_timeout_seconds', 7500) <= (21900 if maximum == 12 else 7500), 'Invalid grader queue timeout')
    require(not resources.stopped(group_dir) and not (batch / 'CANCEL').exists(), 'Parallel group cancelled before admission')
    return group, group_hash, policy


def confirm_absent(cli, directory, *, inspect=None):
    """Candidate phase may end only after all its recorded writers are removed."""
    owned = cli._recorded_containers(directory)
    if inspect is None:
        def inspect(identity):
            return subprocess.run(['docker', 'inspect', identity], capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=20)
    for identity in owned:
        observed = inspect(identity)
        require(observed.returncode != 0 and 'no such' in observed.stderr.lower(),
                'Candidate container remains or its absence is unknown')
    return sorted(owned)


def candidate_marker(group_dir, group_hash, run_id, *, phase, cleanup_confirmed, **evidence):
    require(phase in ('candidate_closed', 'candidate_failed'), 'Invalid candidate terminal phase')
    path = Path(group_dir) / 'candidates' / (run_id + '.json')
    record = {'run_id': run_id, 'group_sha256': group_hash, 'phase': phase,
              'cleanup_confirmed': cleanup_confirmed is True, 'at': resources.utc(), **evidence}
    if path.exists():
        existing = resources.read(path)
        require(all(existing.get(k) == record[k] for k in ('run_id', 'group_sha256', 'phase', 'cleanup_confirmed')),
                'Candidate terminal marker cannot be replaced')
        return existing
    resources.atomic_json(path, record)
    return record


class BarrierGrader:
    def __init__(self, group_dir, group, group_hash, run_id, *, grader, cli,
                 clock=time.monotonic, sleep=time.sleep, absent=confirm_absent):
        self.group_dir, self.group = Path(group_dir), group
        self.group_hash, self.run_id = group_hash, run_id
        self.grader, self.cli, self.clock, self.sleep, self.absent = grader, cli, clock, sleep, absent
        self.directory = Path(group['batch']) / 'results' / run_id
        self.records = []
        self.slot = resources.FileSemaphore(Path(group['global_lock_dir']) / 'grader', 1,
                    group_dir=group_dir, group_sha256=group_hash, name='grader', clock=clock, sleep=sleep)

    def _check_stop(self):
        require(not resources.stopped(self.group_dir) and not (Path(self.group['batch']) / 'CANCEL').exists(),
                'Parallel grading stopped')
        require(resources.sha(self.group_dir / 'group.json') == self.group_hash, 'Parallel group changed while active')
        verify_operator_sources(self.group)
        require(resources.sha(self.group['policy_path']) == self.group['policy_sha256'], 'Parallel policy changed while active')

    def wait_candidates(self):
        started = self.clock()
        deadline = started + self.group['wait_timeout_seconds']
        while True:
            self._check_stop()
            ready = []
            for run_id in self.group['run_ids']:
                path = self.group_dir / 'candidates' / (run_id + '.json')
                if not path.exists():
                    continue
                record = resources.read(path)
                require(record.get('run_id') == run_id and record.get('group_sha256') == self.group_hash,
                        'Candidate barrier evidence identity mismatch')
                if record.get('phase') == 'candidate_failed' or record.get('cleanup_confirmed') is not True:
                    (self.group_dir / 'CANCEL').touch(exist_ok=True)
                    raise resources.ResourceError('An independent candidate failed or cleanup is unconfirmed')
                require(record.get('phase') == 'candidate_closed', 'Unexpected candidate barrier phase')
                if self.group.get('episode_admission_mode') == 'atomic_episode_candidate_quota':
                    released = self.group_dir / 'candidate-releases' / (run_id + '.json')
                    if not released.exists():
                        continue
                    release = resources.read(released)
                    require(release.get('run_id') == run_id and release.get('group_sha256') == self.group_hash
                            and release.get('status') == 'released'
                            and release.get('candidate_quota') == self.group['candidate_requirements'][run_id],
                            'Candidate release evidence identity mismatch')
                ready.append(run_id)
            if len(ready) == len(self.group['run_ids']):
                return max(0.0, self.clock() - started)
            if self.clock() >= deadline:
                raise resources.ResourceError('Candidate generation barrier deadline elapsed')
            self.sleep(.1)

    def __call__(self, instance, path, expected, run_dir, **kwargs):
        self._check_stop()
        require(resources.sha(path) == expected, 'Candidate patch changed before barrier')
        absence = self.absent(self.cli, self.directory)
        candidate_marker(self.group_dir, self.group_hash, self.run_id, phase='candidate_closed',
                         cleanup_confirmed=True, patch_sha256=expected, owned_absence_verified=absence)
        barrier_seconds = self.wait_candidates()
        queued = self.clock()
        with self.slot.held(timeout=self.group.get('grader_wait_timeout_seconds', 7500)):
            self._check_stop()
            # Reverify every candidate, including its selector scratch copies;
            # marker presence alone is not a substitute for actual absence.
            for run_id in self.group['run_ids']:
                self.absent(self.cli, Path(self.group['batch']) / 'results' / run_id)
            require(not list((Path(self.group['global_lock_dir']) / 'providers').rglob('*.lock')),
                    'Provider admission slot remains at grading barrier')
            for name in ('models', 'candidates'):
                require(not list((Path(self.group['global_lock_dir']) / name).glob('*.lock')),
                        'Candidate/model admission slot remains at grading barrier')
            require(resources.sha(path) == expected, 'Candidate patch changed in grading queue')
            queue_seconds = max(0.0, self.clock() - queued)
            started = self.clock()
            record = {'run_dir': str(run_dir), 'generation_barrier_seconds': barrier_seconds,
                      'grader_queue_seconds': queue_seconds, 'grader_started_at': resources.utc()}
            try:
                return self.grader(instance, path, expected, run_dir, **kwargs)
            finally:
                record.update(grader_seconds=max(0.0, self.clock() - started), grader_ended_at=resources.utc())
                self.records.append(record)
                resources.atomic_json(self.group_dir / 'episodes' / self.run_id / 'grading-timing.json', self.records)


def activate_snapshot(snapshot):
    snapshot = Path(snapshot).resolve()
    require(snapshot.is_dir(), 'Admitted runtime snapshot missing')
    for name, module in list(sys.modules.items()):
        if name == 'modelbench' or name.startswith(('modelbench.', 'dpswarm.')) or name == 'dpswarm':
            filename = getattr(module, '__file__', None)
            if filename:
                require(Path(filename).resolve().is_relative_to(snapshot), 'Mutable runtime was imported before snapshot admission')
    sys.path[:0] = [str(snapshot), str(snapshot / 'dpswarm-plugin')]
    os.chdir(snapshot)
    from modelbench.minimal_value_20260905 import cli, freeze, runner
    from modelbench.minimal_value_20260905.environment import rootgrade_terminal
    from modelbench.minimal_value_20260905.transport_identity import assert_transport_identity
    require(Path(cli.__file__).resolve().is_relative_to(snapshot), 'Episode CLI did not originate in frozen snapshot')
    freeze.assert_import_origins(snapshot)
    return cli, freeze, runner, rootgrade_terminal, assert_transport_identity


def install_memory_profile(group, policy, evidence_dir):
    """Bind the explicit creation override after frozen runtime activation."""
    if policy['memory_admission_mode'] != 'bounded_container_limits':
        return None
    spec = importlib.util.spec_from_file_location('_parallel_memory_profile', HERE / 'memory_profile.py')
    profile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profile)
    receipt = profile.install(group['profile_path'], Path(evidence_dir) / 'memory-profile-installation.json')
    require(receipt.get('status') == 'installed'
            and receipt.get('profile_sha256') == group['profile_sha256'], 'Memory creation profile installation failed')
    # activate_snapshot returned the original function object before installation.
    # Fetch the replacement here instead of passing that stale alias to grading.
    environment = importlib.import_module('modelbench.minimal_value_20260905.environment')
    return environment.rootgrade_terminal


def execute(batch, run_id, group_dir, snapshot_root):
    batch, group_dir, snapshot = Path(batch).resolve(), Path(group_dir).resolve(), Path(snapshot_root).resolve()
    group, group_hash, policy = load_group(group_dir, batch, run_id)
    require(snapshot == batch / 'runtime_snapshot', 'Only the original batch snapshot is admitted')
    cli, freeze, runner, grader, identity = activate_snapshot(snapshot)
    manifest = cli.load_manifest(batch)
    freeze.verify_snapshot(batch, manifest)
    identity(manifest['transport_environment'])
    evidence_dir = group_dir / 'episodes' / run_id
    profile_grader = install_memory_profile(group, policy, evidence_dir)
    if profile_grader is not None:
        grader = profile_grader
    require(len([entry for entry in manifest['schedule'] if entry['run_id'] == run_id]) == 1,
            'Episode is not uniquely admitted by frozen schedule')
    # Both runner and R2's default imports observe these process variables. No
    # immutable source or global transport implementation is modified.
    model_slots = resources.FileSemaphore(Path(group['global_lock_dir']) / 'models', policy['global_model_slots'],
                    group_dir=group_dir, group_sha256=group_hash, name='models')
    container_slots = resources.FileSemaphore(Path(group['global_lock_dir']) / 'candidates', policy['candidate_container_cap'],
                    group_dir=group_dir, group_sha256=group_hash, name='candidates')
    runner.MODEL_SLOTS = resources.CombinedSemaphore(runner.MODEL_SLOTS, model_slots)
    runner.CONTAINER_SLOTS = resources.CombinedSemaphore(runner.CONTAINER_SLOTS, container_slots)
    # memory_profile imports R2 before these assignments; update its captured
    # aliases explicitly so selector verification shares the same global gates.
    r2 = importlib.import_module('modelbench.minimal_value_20260905.r2')
    r2.MODEL_SLOTS, r2.CONTAINER_SLOTS = runner.MODEL_SLOTS, runner.CONTAINER_SLOTS
    provider_gate = None
    if policy.get('max_parallel_episodes') == 12:
        spec = importlib.util.spec_from_file_location('_parallel_provider_limits', HERE / 'provider_limits.py')
        provider_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provider_module)
        provider_gate = provider_module.install(group_dir, group, policy, runner, runner.MODEL_SLOTS)
    barrier = BarrierGrader(group_dir, group, group_hash, run_id, grader=grader, cli=cli)
    directory = batch / 'results' / run_id
    resources.atomic_json(evidence_dir / 'wrapper-start.json', {
        'at': resources.utc(), 'pid': os.getpid(), 'group_sha256': group_hash,
        'runtime_cli': str(Path(cli.__file__).resolve()), 'execution_mode': 'parallel_pilot',
        'external_resource_variation': 'original local4 model/local3 container AND hash-bound group model/candidate slots; all-candidate grading barrier',
        'global_model_slots': policy['global_model_slots'], 'candidate_container_cap': policy['candidate_container_cap'],
        'provider_limits': policy.get('provider_limits'),
        'provider_limits_source': policy.get('provider_limits_source'),
        'inference_includes_model_and_container_queue': True,
        'inference_excludes_grading_barrier_and_grader_queue': True,
        'memory_admission_mode': policy['memory_admission_mode'], 'monitor_is_hard_limit': False,
        'memory_profile_sha256': group.get('profile_sha256'),
        'memory_limits_apply_at_creation': profile_grader is not None})
    result = None
    try:
        result = cli.episode(batch, run_id, grader=barrier)
        return result
    finally:
        marker = group_dir / 'candidates' / (run_id + '.json')
        if not marker.exists():
            # cli.episode has already tried its normal cleanup. This is only a
            # read-only absence check; the supervisor owns forced cleanup.
            confirmed, error = False, None
            try:
                confirm_absent(cli, directory)
                confirmed = True
            except Exception as exc:
                error = type(exc).__name__
            candidate_marker(group_dir, group_hash, run_id, phase='candidate_failed',
                             cleanup_confirmed=confirmed, reason='episode_failed_before_grading', absence_error=error)
            (group_dir / 'CANCEL').touch(exist_ok=True)
        resources.atomic_json(evidence_dir / 'resource-slots.json', {
            'at': resources.utc(), 'models': model_slots.summary(), 'candidates': container_slots.summary(),
            'graders': barrier.slot.summary(), 'providers': provider_gate.summary() if provider_gate else None,
            'queue_sum_is_not_episode_wall_time': True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--group-dir', type=Path, required=True)
    parser.add_argument('--snapshot-root', type=Path, required=True)
    args = parser.parse_args()
    result = execute(args.batch, args.run_id, args.group_dir, args.snapshot_root)
    # Avoid the original Windows GBK stdout projection failure, and never print
    # prompts, hidden test contents or provider credentials to the operator log.
    print(json.dumps({'run_id': args.run_id, 'official_resolved': result.get('official_resolved'),
                      'infrastructure_error': result.get('infrastructure_error'),
                      'cleanup_confirmed': result.get('cleanup_confirmed')}, ensure_ascii=True))
    return 0 if not result.get('infrastructure_error') else 1


if __name__ == '__main__':
    raise SystemExit(main())
