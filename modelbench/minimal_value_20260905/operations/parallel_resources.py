"""External trial resources; file slots are hard admission, memory sampling is soft.

Never reclaims a stale slot automatically. A killed owner stops the group; only
the supervisor may reconcile its recorded processes and containers afterwards.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from uuid import uuid4

GIB = 1024 ** 3
SLOT_RELEASE_RETRY_SECONDS = 1.0


class ResourceError(RuntimeError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def stopped(group_dir):
    root = Path(group_dir)
    return (root / 'CANCEL').exists() or (root / 'TRIP.json').exists()


class FileSemaphore:
    """Cross-process bounded slots, compatible with acquire(timeout)/release.

    Slot ownership is also thread-specific. Slots survive process death; this
    deliberately fails closed instead of admitting a replacement over a live
    provider subprocess whose parent may have died.
    """
    def __init__(self, directory, capacity, *, group_dir=None, group_sha256=None,
                 name='slot', clock=time.monotonic, sleep=time.sleep, validate_leases=False):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('A positive integer slot capacity is required')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.capacity, self.group_dir = capacity, Path(group_dir) if group_dir else None
        self.group_sha256, self.name = group_sha256, name
        self.clock, self.sleep = clock, sleep
        self.validate_leases = validate_leases
        if validate_leases:
            import psutil
            self.process_created_at = psutil.Process(os.getpid()).create_time()
        else:
            self.process_created_at = None
        self._held, self._lock = {}, threading.RLock()
        self.wait_seconds, self.acquisitions = 0.0, 0
        self.release_retries, self.release_retry_seconds = 0, 0.0

    def _validate_existing(self):
        if not self.validate_leases:
            return
        import psutil
        for path in self.directory.glob('slot-*.lock'):
            try:
                record = read(path)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                # Another process may have just exclusively opened this file.
                # An incomplete write never becomes available, and becomes a
                # group failure after a bounded construction grace period.
                try:
                    if time.time() - path.stat().st_mtime <= 2:
                        continue
                except FileNotFoundError:
                    continue
                raise ResourceError('Provider lease is unreadable') from None
            if (record.get('capacity') != self.capacity or record.get('resource') != self.name
                    or record.get('group_sha256') != self.group_sha256
                    or type(record.get('pid')) is not int or type(record.get('native_thread_id')) is not int
                    or not record.get('token')
                    or not isinstance(record.get('process_created_at'), (int, float))):
                raise ResourceError('Provider lease identity mismatch')
            try:
                process = psutil.Process(record['pid'])
                valid = (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                         and abs(process.create_time() - record['process_created_at']) < .01
                         and record['native_thread_id'] in {thread.id for thread in process.threads()})
            except psutil.NoSuchProcess:
                valid = False
            if not valid:
                # The owner can release and exit between our read and liveness
                # check. Only a still-present identical lease is abandoned.
                try:
                    current = read(path)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    try:
                        if time.time() - path.stat().st_mtime <= 2:
                            continue
                    except FileNotFoundError:
                        continue
                    raise ResourceError('Replacement provider lease is unreadable') from None
                if current.get('token') != record['token']:
                    continue
                # Never reclaim: a provider child may outlive its parent.
                raise ResourceError('Provider lease owner exited or its PID/thread was reused')

    def acquire(self, blocking=True, timeout=None):
        if timeout is not None and timeout < 0:
            raise ValueError('Negative acquisition timeout')
        started = self.clock()
        deadline = None if timeout is None else started + timeout
        thread = threading.get_ident()
        try:
            while True:
                if self.group_dir and stopped(self.group_dir):
                    raise ResourceError('Parallel group stopped before resource admission')
                self._validate_existing()
                with self._lock:
                    if thread in self._held:
                        raise ResourceError('Recursive resource acquisition is forbidden')
                    for index in range(self.capacity):
                        path = self.directory / ('slot-' + str(index) + '.lock')
                        token = uuid4().hex
                        try:
                            with path.open('x', encoding='utf-8') as stream:
                                json.dump({'pid': os.getpid(), 'thread': thread, 'native_thread_id': threading.get_native_id(), 'token': token,
                                           'group_sha256': self.group_sha256, 'resource': self.name,
                                           'capacity': self.capacity, 'at': utc(),
                                           'process_created_at': self.process_created_at}, stream)
                                stream.flush()
                                os.fsync(stream.fileno())
                        except FileExistsError:
                            continue
                        except BaseException:
                            # An incomplete slot remains held on an I/O failure.
                            raise
                        self._held[thread] = (path, token)
                        self.acquisitions += 1
                        return True
                if not blocking or (deadline is not None and self.clock() >= deadline):
                    return False
                self.sleep(min(.025, max(0, deadline - self.clock())) if deadline is not None else .025)
        finally:
            with self._lock:
                self.wait_seconds += max(0.0, self.clock() - started)

    def release(self):
        with self._lock:
            identity = self._held.get(threading.get_ident())
            if identity is None:
                raise ResourceError('Resource release without an acquisition')
            path, token = identity
            deadline = self.clock() + SLOT_RELEASE_RETRY_SECONDS
            # Windows readers need not grant FILE_SHARE_DELETE. A brief reader
            # can therefore deny unlink even after the model request settled.
            # Retry only this owner's local file operation; keep the slot and
            # local ownership until unlink succeeds, never reacquire or refund.
            while True:
                try:
                    record = read(path)
                    if record.get('token') != token or record.get('pid') != os.getpid():
                        raise ResourceError('Resource slot ownership changed')
                    path.unlink()
                    break
                except PermissionError:
                    remaining = deadline - self.clock()
                    if os.name != 'nt' or remaining <= 0:
                        raise
                    delay = min(.025, remaining)
                    self.release_retries += 1
                    started = self.clock()
                    self.sleep(delay)
                    self.release_retry_seconds += max(0.0, self.clock() - started)
                    # The next attempt must read and verify the original token
                    # and PID again. A replacement or missing slot fails closed.
            del self._held[threading.get_ident()]

    @contextmanager
    def held(self, *, timeout):
        if not self.acquire(timeout=timeout):
            raise ResourceError('Resource queue deadline elapsed')
        try:
            yield self
        finally:
            self.release()

    def summary(self):
        with self._lock:
            return {'capacity': self.capacity, 'acquisitions': self.acquisitions,
                    'queue_wait_seconds_sum': self.wait_seconds,
                    'release_file_retries': self.release_retries,
                    'release_retry_seconds_sum': self.release_retry_seconds,
                    'held_by_this_process': len(self._held), 'admission': 'cross_process_file_slots'}


class CombinedSemaphore:
    """Retain the original per-process bound while adding a shared group bound."""
    def __init__(self, local, shared):
        self.local, self.shared = local, shared

    def acquire(self, blocking=True, timeout=None):
        started = time.monotonic()
        if timeout is None:
            acquired = self.local.acquire(blocking=blocking)
        else:
            acquired = self.local.acquire(blocking=blocking, timeout=timeout)
        if not acquired:
            return False
        try:
            remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
            if self.shared.acquire(blocking=blocking, timeout=remaining):
                return True
        except BaseException:
            self.local.release()
            raise
        self.local.release()
        return False

    def release(self):
        self.shared.release()
        self.local.release()


def memory_bytes(value):
    match = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)\s*', str(value), re.I)
    if not match:
        raise ResourceError('Docker memory sample cannot be parsed')
    number, unit = match.groups()
    unit = unit.lower()
    power = {'b': 0, 'kb': 1, 'kib': 1, 'mb': 2, 'mib': 2, 'gb': 3, 'gib': 3, 'tb': 4, 'tib': 4}[unit]
    return int(float(number) * (1024 if 'i' in unit else 1000) ** power)


class SamplingError(ResourceError):
    """Safe, structured sampling diagnostics; no raw daemon output or labels."""
    def __init__(self, stage, reason, **details):
        super().__init__('Resource sample ' + stage + ': ' + reason)
        self.diagnostics = {'stage': stage, 'reason': reason, **details}


def _docker_sample_failure(stage, args, returncode, stderr, stdout=None):
    def failure(reason, **details):
        error = SamplingError(stage, reason, returncode=returncode, **details)
        # Raw Docker diagnostics stay in a separate local artifact, never inline
        # in status summaries. The sampler command contains only resource IDs.
        error.command_evidence = {'stage': stage, 'returncode': returncode,
            'arguments': list(args), 'stderr': str(stderr or '')[:65536],
            'stdout': str(stdout or '')[:65536],
            'stderr_truncated': len(str(stderr or '')) > 65536,
            'stdout_truncated': len(str(stdout or '')) > 65536}
        return error

    # Only an exact not-found response for a requested hexadecimal ID is churn.
    # Mixed daemon errors, unrecognized output and unrelated IDs stay failures.
    requested = {value for value in args if re.fullmatch(r'[0-9a-f]{12,64}', value)}
    missing = []
    lines = [line.strip() for line in str(stderr or '').splitlines() if line.strip()]
    for line in lines:
        match = re.fullmatch(r'(?i:Error: No such object: |Error response from daemon: No such container: )([0-9a-f]{12,64})', line)
        if not match or match[1] not in requested:
            break
        missing.append(match[1][:12])
    else:
        if missing and requested:
            return failure('container_not_found', missing_ids=sorted(set(missing)))
    # Docker stats may report only EOF when a selected container exits while
    # its stream is read. This is only a candidate for lifecycle reconciliation;
    # a successful fresh ps must still prove that the running set changed.
    if stage == 'stats' and requested and lines and all(line == 'EOF' for line in lines):
        return failure('stats_stream_eof')
    lowered = str(stderr or '').lower()
    daemon = any(text in lowered for text in ('cannot connect to the docker daemon', 'is the docker daemon running',
                                              'error during connect', 'docker engine is not running'))
    return failure('daemon_unavailable' if daemon else 'docker_command_failed')


def sample_resources(owned_roots, *, docker=None, virtual_memory=None, cpu_percent=None):
    """Read-only bounded sampling, reconciling verified container lifecycle races.

    Retry a complete snapshot only when a new successful ps proves the running
    set changed. Incomplete data for a still-running container is never zeroed.
    All attempts share the original ten-second Docker sampling deadline.
    """
    if virtual_memory is None or cpu_percent is None:
        import psutil
        virtual_memory = virtual_memory or psutil.virtual_memory
        cpu_percent = cpu_percent or psutil.cpu_percent
    sampling_deadline = time.monotonic() + 10

    def command(args, stage):
        remaining = sampling_deadline - time.monotonic()
        if remaining <= 0:
            raise SamplingError(stage, 'sampling_deadline_elapsed')
        try:
            if docker is not None:
                return docker(args)
            result = subprocess.run(['docker', *args], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=max(.001, remaining))
            if result.returncode:
                raise _docker_sample_failure(stage, args, result.returncode, result.stderr, result.stdout)
            return result.stdout
        except SamplingError:
            raise
        except subprocess.CalledProcessError as exc:
            raise _docker_sample_failure(stage, args, exc.returncode, exc.stderr, exc.stdout) from None
        except subprocess.TimeoutExpired:
            raise SamplingError(stage, 'sampling_deadline_elapsed') from None
        except OSError as exc:
            raise SamplingError(stage, 'docker_client_os_error', error_type=type(exc).__name__,
                                errno=exc.errno, winerror=getattr(exc, 'winerror', None)) from None
        except Exception as exc:
            raise SamplingError(stage, 'docker_callback_failed', error_type=type(exc).__name__) from None

    def parse_json(raw, stage):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise SamplingError(stage, 'invalid_json') from None

    def running_ids(stage):
        ids = command(['ps', '-q'], stage).split()
        if any(not re.fullmatch(r'[0-9a-f]{12,64}', value) for value in ids) or len({v[:12] for v in ids}) != len(ids):
            raise SamplingError(stage, 'invalid_container_identity')
        return ids

    def identity(row, stage):
        value = row.get('Id') if stage == 'inspect' else row.get('ID') or row.get('Container')
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{12,64}', value):
            raise SamplingError(stage, 'invalid_container_identity')
        return value[:12]

    def indexed(rows, expected, stage):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SamplingError(stage, 'invalid_row_schema')
        result = {}
        for row in rows:
            key = identity(row, stage)
            if key in result or key not in expected:
                raise SamplingError(stage, 'duplicate_or_unrequested_identity')
            result[key] = row
        return result

    info = parse_json(command(['info', '--format', '{{json .}}'], 'info'), 'info')
    try:
        total = int(info['MemTotal'])
        if total <= 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise SamplingError('info', 'invalid_total_memory') from None
    retry_events = []
    records, samples = {}, {}
    for attempt in range(1, 4):
        ids = running_ids('ps')
        expected = {value[:12] for value in ids}
        changed_stage = 'ps_verify'
        trigger_reason = 'running_set_changed'
        try:
            records, samples = {}, {}
            if ids:
                records = indexed(parse_json(command(['inspect', *ids], 'inspect'), 'inspect'), expected, 'inspect')
                if set(records) != expected:
                    raise SamplingError('inspect', 'incomplete_rows')
                raw = command(['stats', '--no-stream', '--format', '{{json .}}', *ids], 'stats')
                samples = indexed([parse_json(line, 'stats') for line in raw.splitlines() if line.strip()], expected, 'stats')
                if set(samples) != expected:
                    raise SamplingError('stats', 'incomplete_rows')
        except SamplingError as exc:
            if exc.diagnostics['reason'] not in ('container_not_found', 'incomplete_rows', 'stats_stream_eof'):
                raise
            current = {value[:12] for value in running_ids('ps_reconcile')}
            missing = set(exc.diagnostics.get('missing_ids', []))
            if current == expected or missing & current:
                raise exc
            changed_stage = exc.diagnostics['stage']
            trigger_reason = exc.diagnostics['reason']
        else:
            current = {value[:12] for value in running_ids('ps_verify')}
            if current == expected:
                break
        retry_events.append({'stage': changed_stage, 'reason': 'verified_running_set_changed',
                             'trigger_reason': trigger_reason,
                             'removed_ids': sorted(expected-current), 'added_ids': sorted(current-expected)})
        if attempt == 3:
            raise SamplingError('consistency', 'container_set_unstable', attempts=attempt, retry_events=retry_events)

    known_owners, grader_owners = set(), set()
    try:
        for root in owned_roots:
            for path in Path(root).rglob('*.json'):
                if path.name in ('container.json', 'container-intent.json', 'request.json'):
                    row = read(path)
                    if row.get('owner'):
                        known_owners.add(row['owner'])
                        if path.name == 'request.json' and row.get('grader_contract'):
                            grader_owners.add(row['owner'])
    except Exception as exc:
        raise SamplingError('owner_records', 'owner_record_unreadable', error_type=type(exc).__name__,
                            errno=getattr(exc, 'errno', None), winerror=getattr(exc, 'winerror', None)) from None
    candidate_count, grader_count, unrelated_count = 0, 0, 0
    quotas, stats = {}, []
    try:
        for identifier, record in records.items():
            owner = (record.get('Config', {}).get('Labels') or {}).get('dpswarm.swe.owner')
            limits = record.get('HostConfig') or {}
            quotas[identifier] = {'memory_limit_bytes': int(limits.get('Memory') or 0),
                                  'memory_swap_limit_bytes': int(limits.get('MemorySwap') or 0),
                                  'nano_cpus': int(limits.get('NanoCpus') or 0), 'owned_by_group': owner in known_owners}
            if owner not in known_owners:
                if owner:
                    unrelated_count += 1
            elif owner in grader_owners:
                grader_count += 1
            else:
                candidate_count += 1
    except (TypeError, ValueError, AttributeError):
        raise SamplingError('inspect', 'invalid_limits_or_labels') from None
    try:
        for identifier, item in samples.items():
            used = memory_bytes(item['MemUsage'].split('/')[0])
            cpu = float(item['CPUPerc'].rstrip('%'))
            if not math.isfinite(cpu) or cpu < 0:
                raise ValueError()
            stats.append({'id': identifier, 'used_memory_bytes': used, 'cpu_percent': cpu, **quotas[identifier]})
    except (KeyError, TypeError, ValueError, AttributeError, ResourceError):
        raise SamplingError('stats', 'invalid_memory_or_cpu') from None
    try:
        vm = virtual_memory()
        host_available, host_total, host_cpu = int(vm.available), int(vm.total), float(cpu_percent())
        if not 0 <= host_available <= host_total or host_total <= 0 or not math.isfinite(host_cpu) or host_cpu < 0:
            raise ValueError()
    except Exception as exc:
        raise SamplingError('host', 'host_sample_unavailable_or_invalid', error_type=type(exc).__name__) from None
    all_used = sum(item['used_memory_bytes'] for item in stats)
    used = sum(item['used_memory_bytes'] for item in stats if item['owned_by_group'])
    return {'at': utc(), 'docker_total_bytes': total, 'docker_cpus': info.get('NCPU'),
            'host_available_bytes': host_available, 'host_total_bytes': host_total,
            'host_cpu_percent': host_cpu, 'owned_memory_bytes': used,
            'all_container_memory_bytes': all_used,
            'docker_remaining_estimate_bytes': max(0, total - all_used),
            'docker_remaining_estimate_kind': 'Docker total minus ALL running container working-set samples; excludes VM/daemon overhead and page cache',
            'candidate_container_count': candidate_count, 'grader_container_count': grader_count,
            'unrelated_swe_container_count': unrelated_count, 'containers': stats,
            'sampling_consistency': {'attempts': attempt, 'container_set_verified': True, 'retry_events': retry_events},
            'memory_sample_kind': 'Docker stats working-set display, not a hard reservation'}


class ResourceMonitor:
    """Supervisor-owned sampling loop. On trip, persist CANCEL; never kill here."""
    def __init__(self, group_dir, owned_roots, *, sample=sample_resources, interval_seconds=2,
                 owned_memory_limit_bytes=8 * GIB, host_available_min_bytes=2 * GIB,
                 docker_remaining_min_bytes=3 * GIB, candidate_cap=7, grader_cap=2,
                 max_sample_failures=2, container_memory_limit_bytes=None):
        self.group_dir, self.owned_roots = Path(group_dir), [Path(p) for p in owned_roots]
        self.sample, self.interval = sample, interval_seconds
        self.policy = {'owned_memory_limit_bytes': owned_memory_limit_bytes,
                       'host_available_min_bytes': host_available_min_bytes,
                       'docker_remaining_min_bytes': docker_remaining_min_bytes,
                       'candidate_cap': candidate_cap, 'grader_cap': grader_cap,
                       'max_sample_failures': max_sample_failures,
                       'container_memory_limit_bytes': container_memory_limit_bytes}
        self.failures = 0
        self._stop, self._thread, self._lock = threading.Event(), None, threading.Lock()

    def trip(self, reason, sample=None):
        with self._lock:
            target = self.group_dir / 'TRIP.json'
            if not target.exists():
                atomic_json(target, {'at': utc(), 'reason': reason, 'sample': sample,
                                     'policy': self.policy, 'protection': 'soft_sampled_circuit_breaker'})
            (self.group_dir / 'CANCEL').touch(exist_ok=True)

    def tick(self):
        started = time.monotonic()
        try:
            value = self.sample(self.owned_roots)
            self.failures = 0
        except Exception as exc:
            self.failures += 1
            value = {'at': utc(), 'sample_error': type(exc).__name__, 'consecutive_failures': self.failures,
                     'sample_error_details': getattr(exc, 'diagnostics', {'stage': 'sample_callback',
                         'reason': 'unexpected_exception', 'error_type': type(exc).__name__})}
            evidence = getattr(exc, 'command_evidence', None)
            if evidence is not None:
                path = self.group_dir / 'sampling-errors' / (uuid4().hex + '.json')
                try:
                    atomic_json(path, {'at': value['at'], **evidence})
                    value['sample_error_evidence'] = {'path': str(path.resolve()), 'sha256': sha(path)}
                except OSError as evidence_error:
                    value['sample_error_evidence'] = {'write_failed': type(evidence_error).__name__,
                        'errno': evidence_error.errno, 'winerror': getattr(evidence_error, 'winerror', None)}
            if self.failures >= self.policy['max_sample_failures']:
                self.trip('resource_sampling_unavailable', value)
        else:
            checks = [
                (value['owned_memory_bytes'] >= self.policy['owned_memory_limit_bytes'], 'owned_memory_threshold'),
                (value['host_available_bytes'] < self.policy['host_available_min_bytes'], 'host_available_threshold'),
                (value['docker_remaining_estimate_bytes'] < self.policy['docker_remaining_min_bytes'], 'docker_remaining_estimate_threshold'),
                (value['candidate_container_count'] > self.policy['candidate_cap'], 'candidate_container_cap_exceeded'),
                (value['grader_container_count'] > self.policy['grader_cap'], 'grader_container_cap_exceeded'),
            ]
            ceiling = self.policy['container_memory_limit_bytes']
            if ceiling is not None:
                invalid = any(item.get('owned_by_group') and
                    (not 0 < item.get('memory_limit_bytes', 0) <= ceiling
                     or item.get('memory_swap_limit_bytes') != item.get('memory_limit_bytes'))
                    for item in value.get('containers', []))
                checks.insert(0, (invalid, 'container_memory_limit_mismatch'))
            for failed, reason in checks:
                if failed:
                    self.trip(reason, value)
                    break
        value['sampling_wall_seconds'] = time.monotonic() - started
        value['interval_target_seconds'] = self.interval
        value['interval_is_hard_realtime'] = False
        self.group_dir.mkdir(parents=True, exist_ok=True)
        with (self.group_dir / 'resource-samples.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + '\n')
            stream.flush()
        return value

    def _run(self):
        try:
            while not self._stop.is_set() and not stopped(self.group_dir):
                self.tick()
                self._stop.wait(self.interval)
        except BaseException as exc:
            self.trip('resource_monitor_failed_' + type(exc).__name__,
                      {'sample_error_details': getattr(exc, 'diagnostics', {'stage': 'monitor',
                          'reason': 'unexpected_exception', 'error_type': type(exc).__name__})})

    def start(self):
        if self._thread is not None:
            raise ResourceError('Resource monitor cannot be started twice')
        self._thread = threading.Thread(target=self._run, name='parallel-resource-monitor', daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=35):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        return {'stopped': self._thread is None or not self._thread.is_alive(),
                'tripped': (self.group_dir / 'TRIP.json').exists()}


def monitor_from_group(group_dir, owned_roots, **kwargs):
    """Build the supervisor monitor from the exact hash-bound operator policy."""
    group_dir = Path(group_dir)
    group = read(group_dir / 'group.json')
    if sha(group['policy_path']) != group['policy_sha256']:
        raise ResourceError('Resource policy hash mismatch')
    policy = read(group['policy_path'])
    soft = policy['softguard']
    mode = policy.get('memory_admission_mode')
    if policy.get('monitor_is_hard_limit') is not False or mode not in ('monitored_overcommit', 'bounded_container_limits'):
        raise ResourceError('Expected explicit sampled memory protection policy')
    if mode == 'bounded_container_limits' and (policy.get('candidate_memory') != '1g' or policy.get('grader_memory') != '1g'):
        raise ResourceError('Bounded policy must cap candidate and grading containers at 1 GiB')
    return ResourceMonitor(group_dir, owned_roots,
        interval_seconds=soft['sample_seconds'],
        owned_memory_limit_bytes=int(soft['owned_memory_limit_gib'] * GIB),
        host_available_min_bytes=int(soft['host_available_min_gib'] * GIB),
        docker_remaining_min_bytes=int(soft['docker_available_min_gib'] * GIB),
        candidate_cap=policy['candidate_container_cap'], grader_cap=policy['grader_max_containers'],
        max_sample_failures=soft['max_consecutive_sample_failures'],
        container_memory_limit_bytes=GIB if mode == 'bounded_container_limits' else None, **kwargs)