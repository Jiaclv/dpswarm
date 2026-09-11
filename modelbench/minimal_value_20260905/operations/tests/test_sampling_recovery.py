"""Sampling lifecycle tests; Docker probe requires explicit opt-in and stage lease."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

OPS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sampling_recovery_resources', OPS/'parallel_resources.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
A, B = 'a'*64, 'b'*64
HOST = dict(virtual_memory=lambda: SimpleNamespace(available=8*r.GIB, total=32*r.GIB), cpu_percent=lambda: 1)


def record(cid, *, memory=r.GIB):
    return {'Id': cid, 'Config': {'Labels': {'dpswarm.swe.owner': 'owned'}},
            'HostConfig': {'Memory': memory, 'MemorySwap': memory, 'NanoCpus': 2*10**9}}


def stat(cid):
    return {'ID': cid[:12], 'MemUsage': '12MiB / 1GiB', 'CPUPerc': '0.1%'}


class Docker:
    def __init__(self, sets, *, inspect=None, stats=None):
        self.sets = iter(sets)
        self.inspect = iter(inspect) if inspect is not None else None
        self.stats = iter(stats) if stats is not None else None
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == 'info':
            return json.dumps({'MemTotal': 16*r.GIB, 'NCPU': 4})
        if args[0] == 'ps':
            return '\n'.join(next(self.sets))
        if args[0] == 'inspect':
            value = next(self.inspect) if self.inspect is not None else [record(cid) for cid in args[1:]]
        elif args[0] == 'stats':
            value = next(self.stats) if self.stats is not None else [stat(cid) for cid in args[4:]]
        else:
            raise AssertionError('Unexpected Docker operation')
        if isinstance(value, BaseException):
            raise value
        return json.dumps(value) if args[0] == 'inspect' else '\n'.join(json.dumps(row) for row in value)


def owned(tmp_path):
    r.atomic_json(tmp_path/'container-intent.json', {'owner': 'owned'})
    return [tmp_path]


@pytest.mark.parametrize('stage', ['inspect', 'stats'])
@pytest.mark.parametrize('error_prefix', ['Error: No such object: ', 'error: no such object: ', 'Error response from daemon: No such container: '])
def test_real_not_found_shape_retries_only_after_ps_confirms_disappearance(tmp_path, stage, error_prefix):
    error = subprocess.CalledProcessError(1, ['docker', stage], stderr=error_prefix+A+'\n')
    kwargs = {stage: [error]}
    docker = Docker([[A], [], [], []], **kwargs)
    value = r.sample_resources(owned(tmp_path), docker=docker, **HOST)
    assert value['containers'] == []
    assert value['sampling_consistency']['attempts'] == 2
    assert value['sampling_consistency']['retry_events'][0]['removed_ids'] == [A[:12]]


@pytest.mark.parametrize('stage', ['inspect', 'stats'])
def test_missing_rows_for_departed_container_are_reconciled(tmp_path, stage):
    docker = Docker([[A], [], [], []], **{stage: [[]]})
    value = r.sample_resources(owned(tmp_path), docker=docker, **HOST)
    assert value['sampling_consistency']['attempts'] == 2
    assert value['all_container_memory_bytes'] == 0


def test_departure_retains_complete_survivor_measurement(tmp_path):
    docker = Docker([[A, B], [B], [B], [B]], stats=[[stat(B)], [stat(B)]])
    value = r.sample_resources(owned(tmp_path), docker=docker, **HOST)
    assert value['candidate_container_count'] == 1
    assert value['owned_memory_bytes'] == 12*1024**2
    assert value['containers'][0]['id'] == B[:12]


def test_new_arrival_at_final_ps_forces_fresh_measurements(tmp_path):
    docker = Docker([[A], [A, B], [A, B], [A, B]])
    value = r.sample_resources(owned(tmp_path), docker=docker, **HOST)
    assert value['candidate_container_count'] == 2
    assert value['owned_memory_bytes'] == 24*1024**2
    assert value['sampling_consistency']['attempts'] == 2


@pytest.mark.parametrize('stage', ['inspect', 'stats'])
def test_missing_rows_for_still_running_container_fail_closed(tmp_path, stage):
    docker = Docker([[A], [A]], **{stage: [[]]})
    with pytest.raises(r.SamplingError) as error:
        r.sample_resources(owned(tmp_path), docker=docker, **HOST)
    assert error.value.diagnostics == {'stage': stage, 'reason': 'incomplete_rows'}


def test_not_found_claim_cannot_discard_still_running_container(tmp_path):
    error = subprocess.CalledProcessError(1, ['docker', 'inspect'], stderr='Error: No such object: '+A)
    docker = Docker([[A], [A, B]], inspect=[error])
    with pytest.raises(r.SamplingError, match='container_not_found'):
        r.sample_resources(owned(tmp_path), docker=docker, **HOST)


@pytest.mark.parametrize('stderr,reason', [
    ('Cannot connect to the Docker daemon. Is the docker daemon running?', 'daemon_unavailable'),
    ('Error: No such object: '+A+'\nprivate-secret-payload', 'docker_command_failed'),
    ('Error: No such object: '+B, 'docker_command_failed'),
])
def test_daemon_or_unrecognized_failure_is_not_churn_or_secret_leak(tmp_path, stderr, reason):
    docker = Docker([[A]], inspect=[subprocess.CalledProcessError(1, 'docker', stderr=stderr)])
    with pytest.raises(r.SamplingError) as error:
        r.sample_resources(owned(tmp_path), docker=docker, **HOST)
    assert error.value.diagnostics == {'stage': 'inspect', 'reason': reason, 'returncode': 1}
    assert 'private-secret' not in str(error.value)+json.dumps(error.value.diagnostics)
    assert len([c for c in docker.calls if c[0]=='ps']) == 1


@pytest.mark.parametrize('rows,reason', [
    ([stat(A), stat(A)], 'duplicate_or_unrequested_identity'),
    ([stat(B)], 'duplicate_or_unrequested_identity'),
    ([{**stat(A), 'CPUPerc': 'nan%'}], 'invalid_memory_or_cpu'),
    ([{**stat(A), 'MemUsage': 'bad'}], 'invalid_memory_or_cpu'),
])
def test_corrupt_or_unmatched_stats_stay_errors(tmp_path, rows, reason):
    docker = Docker([[A], [A]], stats=[rows])
    with pytest.raises(r.SamplingError, match=reason):
        r.sample_resources(owned(tmp_path), docker=docker, **HOST)


def test_churn_is_bounded_without_changing_monitor_failure_threshold(tmp_path):
    docker = Docker([[A], [B], [B], [A], [A], [B]])
    with pytest.raises(r.SamplingError) as error:
        r.sample_resources(owned(tmp_path), docker=docker, **HOST)
    assert error.value.diagnostics['reason'] == 'container_set_unstable'
    assert error.value.diagnostics['attempts'] == 3


def test_timeout_is_stage_specific_and_cannot_be_retried_as_churn(tmp_path):
    docker = Docker([[A]], stats=[subprocess.TimeoutExpired(['docker', 'stats'], 10)])
    with pytest.raises(r.SamplingError) as error:
        r.sample_resources(owned(tmp_path), docker=docker, **HOST)
    assert error.value.diagnostics == {'stage': 'stats', 'reason': 'sampling_deadline_elapsed'}


def test_retries_share_original_ten_second_deadline(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(r.time, 'monotonic', lambda: clock[0])
    base = Docker([[A], []], inspect=[[]])
    def docker(args):
        value = base(args)
        if args[0] == 'ps' and value == '':
            clock[0] = 10.01
        return value
    with pytest.raises(r.SamplingError, match='sampling_deadline_elapsed'):
        r.sample_resources(owned(tmp_path), docker=docker, **HOST)


def test_monitor_records_safe_stage_reason_and_still_trips_after_two_failures(tmp_path):
    def fail(_):
        raise r.SamplingError('stats', 'incomplete_rows')
    monitor = r.ResourceMonitor(tmp_path, [], sample=fail)
    first = monitor.tick()
    assert not (tmp_path/'CANCEL').exists()
    second = monitor.tick()
    assert first['sample_error_details'] == second['sample_error_details'] == {'stage': 'stats', 'reason': 'incomplete_rows'}
    assert second['consecutive_failures'] == monitor.policy['max_sample_failures'] == 2
    assert r.read(tmp_path/'TRIP.json')['reason'] == 'resource_sampling_unavailable'


def test_reconciled_sample_still_enforces_one_gib_creation_limit(tmp_path):
    docker = Docker([[A], [A, B], [A, B], [A, B]],
                    inspect=[[record(A)], [record(A), record(B, memory=2*r.GIB)]])
    monitor = r.ResourceMonitor(tmp_path, owned(tmp_path),
        sample=lambda roots: r.sample_resources(roots, docker=docker, **HOST), container_memory_limit_bytes=r.GIB)
    monitor.tick()
    assert r.read(tmp_path/'TRIP.json')['reason'] == 'container_memory_limit_mismatch'


@pytest.mark.skipif(os.environ.get('DPSWARM_SAMPLING_DOCKER_PROBE') != '1', reason='Explicit local Docker probe opt-in required')
@pytest.mark.parametrize('phase', ['inspect', 'stats'])
def test_real_short_lived_docker_container_disappears_between_reads(tmp_path, monkeypatch, phase):
    from modelbench.minimal_value_20260905.cli import stage_lease
    raw_run = subprocess.run
    def real(args, **kwargs):
        return raw_run(['docker', *args], capture_output=True, text=True, encoding='utf-8', timeout=15, **kwargs)
    lock = OPS.parent/'validation/active-stage.lock'
    cid, removed, expected_label = None, False, uuid4().hex
    with stage_lease(lock, tmp_path) as lease:
        image = real(['image', 'inspect', 'redis:7-alpine', '--format', '{{.Id}}'])
        assert image.returncode == 0, 'Probe uses an already-present image only'
        try:
            created = real(['run', '-d', '--rm', '--network', 'none', '--read-only', '--memory', '64m',
                '--memory-swap', '64m', '--cpus', '.1', '--label', 'dpswarm.sampling.probe='+expected_label,
                '--entrypoint', '/bin/sh', image.stdout.strip(), '-c', 'sleep 30'])
            assert created.returncode == 0, 'Local probe container creation failed'
            cid = created.stdout.strip()
            def raced(argv, **kwargs):
                nonlocal removed
                if len(argv)>1 and argv[1] == phase and not removed:
                    label = real(['inspect', '--format', '{{index .Config.Labels "dpswarm.sampling.probe"}}', cid])
                    assert label.returncode == 0 and label.stdout.strip() == expected_label
                    assert real(['rm', '-f', cid]).returncode == 0
                    removed = True
                return raw_run(argv, **kwargs)
            monkeypatch.setattr(r.subprocess, 'run', raced)
            value = r.sample_resources([])
            assert removed and cid[:12] not in {row['id'] for row in value['containers']}
            assert value['sampling_consistency']['attempts'] == 2
            assert value['sampling_consistency']['retry_events'][0]['stage'] == phase
            print(json.dumps({'real_docker_race_phase': phase, 'reconciled': True, 'probe_memory_bytes': 64*1024**2}))
        finally:
            if cid:
                label = real(['inspect', '--format', '{{index .Config.Labels "dpswarm.sampling.probe"}}', cid])
                if label.returncode == 0:
                    if label.stdout.strip() != expected_label:
                        lease['retain'] = True
                        raise AssertionError('Probe owner changed; retain shared lease')
                    real(['rm', '-f', cid])
                absent = real(['inspect', cid])
                if absent.returncode == 0 or 'no such' not in absent.stderr.lower():
                    lease['retain'] = True
                    raise AssertionError('Probe cleanup is unconfirmed; retain shared lease')
