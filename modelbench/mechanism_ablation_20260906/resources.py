"""C1 sampling and conservative pair admission; no container is started here."""
from __future__ import annotations
from collections import deque
from copy import deepcopy
import threading
import time
import json
import subprocess
from pathlib import Path
from modelbench.minimal_value_20260905.operations.parallel_resources import (
    GIB, ResourceMonitor as _Monitor, sample_resources as _sample_resources, atomic_json, stopped)


ROLE_LIMITS = {'candidate': (GIB, 2000000000),
               'grader_target': (GIB, 2000000000),
               'grader_controller': (768 * 1024 ** 2, 1000000000)}


def _recorded_roles(owned_roots):
    """Owner plus exact persisted Docker ID/name, including creation intents."""
    records = []
    def add(owner, role, identities, source):
        if not isinstance(owner, str) or not owner:
            return
        for identity in identities:
            if isinstance(identity, str) and identity:
                records.append({'owner':owner, 'role':role, 'identity':identity.lstrip('/'),
                                'source_record':str(source.resolve())})
    for root in owned_roots:
        for path in Path(root).rglob('*.json'):
            if path.name not in ('container.json', 'container-intent.json', 'request.json'):
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                continue
            owner = value.get('owner')
            if path.name != 'request.json':
                add(owner, 'candidate', [value.get('id'), value.get('name')], path)
            elif value.get('grader_contract'):
                for name, role in [('controller.json','grader_controller'), ('containers.json','grader_target')]:
                    descriptor = path.parent / name
                    if descriptor.exists():
                        identities = json.loads(descriptor.read_text(encoding='utf-8'))
                        if not isinstance(identities, list):
                            raise ValueError('Grader container descriptor must be a list')
                        add(owner, role, identities, descriptor)
    return records


def _attach_roles(value, owned_roots, inspected):
    records = _recorded_roles(owned_roots)
    for container in value.get('containers', []):
        actual = inspected.get(container['id']) or {}
        owner = (actual.get('Config', {}).get('Labels') or {}).get('dpswarm.swe.owner')
        name = str(actual.get('Name') or '').lstrip('/')
        full_id = actual.get('Id') or ''
        container.update(owner=owner, name=name)
        by_identity = [record for record in records if record['identity'] in (full_id, name)]
        if not container.get('owned_by_group'):
            if by_identity:
                container.update(owned_by_group=True, resource_role='unknown',
                    role_evidence_error='recorded_container_owner_mismatch')
            else:
                container['resource_role'] = 'unrelated'
            continue
        matches = [record for record in by_identity if record['owner'] == owner]
        identities = {(record['owner'], record['role']) for record in matches}
        if len(identities) != 1:
            container['resource_role'] = 'unknown'
            container['role_evidence_error'] = 'missing_or_ambiguous_recorded_owner_and_identity'
        else:
            container['resource_role'] = matches[0]['role']
            container['role_source_record'] = matches[0]['source_record']
    return value


def sample_resources(owned_roots, *, docker=None, virtual_memory=None, cpu_percent=None):
    """Reuse the old snapshot and its inspect bytes; no additional Docker call."""
    inspected = {}
    deadline = time.monotonic() + 10
    def capture(args):
        if docker is None:
            result = subprocess.run(['docker', *args], capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=max(.001, deadline-time.monotonic()))
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, args, output=result.stdout, stderr=result.stderr)
            raw = result.stdout
        else:
            raw = docker(args)
        if args[0] == 'inspect':
            try:
                rows = json.loads(raw)
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and isinstance(row.get('Id'), str):
                            inspected[row['Id'][:12]] = row
            except (ValueError, TypeError):
                pass  # The original sampler owns JSON/schema error classification.
        return raw
    value = _sample_resources(owned_roots, docker=capture,
        virtual_memory=virtual_memory, cpu_percent=cpu_percent)
    return _attach_roles(value, owned_roots, inspected)


def topology_error(value):
    owned = [item for item in value.get('containers', []) if item.get('owned_by_group')]
    if any(item.get('resource_role') not in ROLE_LIMITS or not item.get('owner') for item in owned):
        return 'container_role_or_owner_unknown'
    candidates = [item for item in owned if item['resource_role'] == 'candidate']
    graders = [item for item in owned if item['resource_role'].startswith('grader_')]
    if len(candidates) != value['candidate_container_count'] or len(graders) != value['grader_container_count']:
        return 'container_role_count_mismatch'
    if candidates and graders:
        return 'generation_grading_overlap'
    if len({item['owner'] for item in graders}) > 1:
        return 'multiple_grading_owners'
    for role in ('grader_controller', 'grader_target'):
        if sum(item['resource_role'] == role for item in graders) > 1:
            return 'duplicate_grader_role'
    for item in owned:
        memory, cpus = ROLE_LIMITS[item['resource_role']]
        if (item.get('memory_limit_bytes') != memory or item.get('memory_swap_limit_bytes') != memory
                or item.get('nano_cpus') != cpus):
            return 'container_resource_contract_mismatch'
    return None


def admission_headroom(sample, active_slots, new_slots=6):
    if not sample or sample.get('sample_error'):
        return False
    # Include already admitted environments not yet visible to Docker sampling.
    unseen = max(0, active_slots - sample['candidate_container_count'])
    extra = (new_slots + unseen) * GIB
    return (active_slots + new_slots <= 18
        and sample['owned_memory_bytes'] + extra < 12 * GIB
        and sample['host_available_bytes'] - extra >= 2 * GIB
        and sample['docker_remaining_estimate_bytes'] - extra >= 2 * GIB
        and sample['grader_container_count'] == 0)


class ResourceMonitor(_Monitor):
    def __init__(self, group_dir, owned_roots, *, sample=sample_resources):
        super().__init__(group_dir, owned_roots, sample=sample, interval_seconds=10,
            owned_memory_limit_bytes=12 * GIB, host_available_min_bytes=2 * GIB,
            docker_remaining_min_bytes=2 * GIB, candidate_cap=18, grader_cap=2,
            max_sample_failures=3, container_memory_limit_bytes=GIB)
        self.policy['topology'] = 'candidates_or_one_grading_owner_with_one_controller_and_one_target'
        self.policy['role_limits'] = {role:{'memory_bytes':limits[0], 'nano_cpus':limits[1]} for role,limits in ROLE_LIMITS.items()}
        self._recent = deque(maxlen=3)
        self._recent_lock = threading.Lock()

    def tick(self):
        value = super().tick()
        if not value.get('sample_error'):
            reason = topology_error(value)
            if reason:
                self.trip(reason, value)
        value = {**value, 'sampled_monotonic': time.monotonic()}
        with self._recent_lock:
            if value.get('sample_error') or stopped(self.group_dir):
                self._recent.clear()
            else:
                self._recent.append(value)
        atomic_json(self.group_dir / 'resource-status.json', value)
        return value

    def observations(self):
        with self._recent_lock:
            return deepcopy(list(self._recent))


def ramp_ready(observations, active_slots, *, after=0, now=None):
    now = time.monotonic() if now is None else now
    if len(observations) < 3:
        return False
    times = [s['sampled_monotonic'] for s in observations]
    return (times[0] >= after and now - times[-1] <= 35
        and all(b - a >= 9.5 for a, b in zip(times, times[1:]))
        and all(admission_headroom(s, active_slots, 6) for s in observations))
