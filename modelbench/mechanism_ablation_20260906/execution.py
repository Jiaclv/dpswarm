"""Finite C1 execution: eighteen attempts, a six-run health gate, no retries."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from modelbench.big_budget_team_20260906 import execution as _b1
from modelbench.big_budget_team_20260906.coding_quota import inspect_quota, assess_wave
from modelbench.minimal_value_20260905 import freeze
from modelbench.minimal_value_20260905.cli import cleanup_owned_episode, kill_owned_process_tree, stage_lease
from modelbench.minimal_value_20260905.runtime_integrity import atomic_json
from . import provider, resources

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OFFICIAL = REPO / 'modelbench/minimal_value_20260905/official'
PROTOCOL = 'mechanism_ablation_execution_v1'
MODULE = 'modelbench.mechanism_ablation_20260906.execution'
read, sha, utc = _b1.read, _b1.sha, _b1.utc
summarize, _record_stop, _stop_request, _signal_drain = (
    _b1.summarize, _b1._record_stop, _b1._stop_request, _b1._signal_drain)
FIXED = {'candidate_container_cap':18, 'initial_episode_slots':4, 'episode_slots':6,
    'candidate_memory':'1g', 'candidate_cpus':2, 'generation_watchdog_seconds':7500,
    'grading_watchdog_seconds':2100, 'dispatch_seconds':86400,
    'cooperative_stop_grace_seconds':1080, 'final_drain_seconds':45300,
    'generation_attempt_limit':18, 'token_admission_sum_limit':10800000,
    'ordinary_ticket_limit':504, 'cm_ticket_limit':108,
    'pilot_generation_attempts':6, 'automatic_generation_retry':False,
    'grading_attempts_per_generation':1}


def runtime_sources():
    sources = _b1.runtime_sources()
    for pattern in ('*.py', '*.json', 'C1_PLANNED_RUNS.csv', 'PLAN_ZH.md'):
        for path in HERE.glob(pattern):
            sources[path.relative_to(REPO).as_posix()] = sha(path)
    return dict(sorted(sources.items()))


def transport_sources():
    names = ('transport.py', 'provider.py', 'provider_limits.py', 'parallel_resources.py',
             'keyconfig.py', 'protocol.py', 'transports.py', 'TRANSPORT_POLICY.json', 'glm_stream_http_worker.py',
             'coding_quota.py', 'CODING_PLAN_AMENDMENT.json', 'PLAN.json')
    return {p:h for p,h in runtime_sources().items() if any(n in p for n in names) or p.startswith('modelbench/mechanism_ablation_20260906/')}


def _validate_manifest(value):
    from .contracts import validate_schedule
    if value.get('protocol') != PROTOCOL:
        raise ValueError('Wrong C1 execution protocol')
    authorization = value.get('authorization') or {}
    if (authorization.get('execution_authorized') is not True or authorization.get('stage') != 'C1'
            or authorization.get('generation_attempts') != 18):
        raise ValueError('Explicit C1 eighteen-attempt authorization required')
    schedule = value.get('schedule')
    validate_schedule(schedule)
    if len(schedule) != 18 or len({e['run_id'] for e in schedule}) != 18:
        raise ValueError('Only the eighteen C1 cells are dispatchable')
    for key, required in FIXED.items():
        if value.get(key) != required or type(value.get(key)) is not type(required):
            raise ValueError('C1 frozen execution field differs: ' + key)
    for i in range(0, 18, 2):
        pair = schedule[i:i+2]
        if (pair[0]['instance']['instance_id'] != pair[1]['instance']['instance_id']
                or pair[0]['rep'] != pair[1]['rep']
                or {e['condition_id'] for e in pair} != {'FCM_ON','FCM_OFF'}):
            raise ValueError('CM pair must be adjacent in the frozen schedule')
    if [e['rep'] for e in schedule[:6]] != [1] * 6:
        raise ValueError('First six are the three formal replicate-one pairs')
    if value.get('dispatch_run_ids', [e['run_id'] for e in schedule]) != [e['run_id'] for e in schedule]:
        raise ValueError('C1 dispatch cannot silently omit or append cells')
    provider.validate_policy(value.get('provider_policy'))
    if value.get('automatic_model_retry', False) is not False:
        raise ValueError('Automatic model retry is forbidden')


def _safe_child(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Frozen artifact path escapes its root')
    return path


def _validate_transport_binding(manifest, policy_path):
    if (manifest.get('transport_policy') != read(policy_path)
            or manifest.get('transport_policy_sha256') != sha(policy_path)):
        raise ValueError('Declared transport policy differs from frozen Coding Plan wire policy')


def prepare(batch, *, manifest_path):
    """Freeze a parent-authored manifest after offline/probe evidence validation."""
    batch = Path(batch).resolve()
    if batch.exists():
        raise ValueError('Prepared batches may not be overwritten')
    template_path = Path(manifest_path).resolve()
    manifest = read(template_path)
    manifest.setdefault('protocol', PROTOCOL)
    for key, value in FIXED.items():
        manifest.setdefault(key, value)
    manifest.setdefault('provider_policy', provider.policy())
    _validate_manifest(manifest)
    _validate_transport_binding(manifest, REPO / 'modelbench/big_budget_team_20260906/TRANSPORT_POLICY.json')
    source, inputs = runtime_sources(), freeze.input_files(OFFICIAL)
    origins = manifest.get('evidence_origins') or {}
    required = {'offline-validation.json', 'offline-tests.xml', 'transport-probes.json',
                'source_qualification_review.json', 'authorization.json'}
    if not required <= set(origins):
        raise ValueError('Missing qualification evidence files: ' + str(sorted(required-set(origins))))
    for name, proof in origins.items():
        if Path(name).name != name or sha(proof['path']) != proof['sha256']:
            raise ValueError('Evidence origin identity mismatch: ' + name)
    gate = read(origins['offline-validation.json']['path'])
    if gate.get('passed') is not True or gate.get('runtime_sources') != source:
        raise ValueError('Offline validation must bind exact C1 sources')
    if gate.get('junit_sha256') != origins['offline-tests.xml']['sha256']:
        raise ValueError('Offline JUnit hash mismatch')
    probes = read(origins['transport-probes.json']['path'])
    if (probes.get('passed') is not True or type(probes.get('calls_issued')) is not int
            or not 0 < probes['calls_issued'] <= 3 or probes.get('transport_sources') != transport_sources()):
        raise ValueError('Bounded C1 model probes are not qualified for these source bytes')
    auth = read(origins['authorization.json']['path'])
    if auth != manifest['authorization']:
        raise ValueError('Manifest authorization must equal its bound evidence')
    qualification = read(origins['source_qualification_review.json']['path'])
    if not (qualification.get('passed') is True or qualification.get('status') in (
            'six_source_qualifications_reusable_under_exact_grader_and_resource_contract',
            'three_source_qualifications_reusable_under_exact_grader_and_resource_contract')):
        raise ValueError('Source environment qualifications have not passed')
    for proof in qualification.get('input_artifacts', []) + qualification.get('adapter_sources', []):
        if sha(proof['path']) != proof['sha256']:
            raise ValueError('Source qualification evidence changed')
    qualified = {q['instance_id']:q for q in qualification.get('tasks', [])}
    for entry in manifest['schedule']:
        q = qualified.get(entry['instance']['instance_id'])
        if not q or not q.get('qualified') or q.get('public_instance') != entry['instance'] or (
                q.get('image_id') != entry['image'] or q.get('public_checks') != entry['public_checks']):
            raise ValueError('Candidate inputs differ from qualified environment')
        if qualification.get('shared_grader_contract') != entry['grader_contract']:
            raise ValueError('Grader contract differs from qualification')
    batch.mkdir(parents=True)
    snapshot = batch / 'runtime_snapshot'
    for relative, expected in source.items():
        target = _safe_child(snapshot, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
        if sha(target) != expected:
            raise ValueError('Runtime changed while freezing')
    for relative, expected in inputs.items():
        target = _safe_child(snapshot / 'modelbench/minimal_value_20260905/official', relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(OFFICIAL / relative, target)
        if sha(target) != expected:
            raise ValueError('Official input changed while freezing')
    evidence = {}
    for name, proof in origins.items():
        target = batch / 'preflight' / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(proof['path'], target)
        evidence['preflight/' + name] = sha(target)
        if evidence['preflight/' + name] != proof['sha256']:
            raise ValueError('Qualification changed while freezing')
    pricing = HERE / 'pricing.json'
    if not pricing.exists():
        pricing = REPO / 'modelbench/big_budget_team_20260906/pricing.json'
    shutil.copyfile(pricing, batch / 'pricing.json')
    evidence['pricing.json'] = sha(batch / 'pricing.json')
    manifest.update(created_at=utc(), runtime_sources=source, input_artifacts=inputs,
        evidence_files=evidence, source_repo=str(REPO), manifest_template_sha256=sha(template_path),
        resource_lock_path=str(REPO / 'modelbench/big_budget_team_20260906/execution.lock'),
        probe_call_count=probes['calls_issued'], valuation='frozen API-equivalent; not subscription cash')
    atomic_json(batch / 'manifest.json', manifest)
    (batch / 'manifest.sha256').write_text(sha(batch / 'manifest.json') + '\n', encoding='ascii')
    groupdir = batch / 'group'
    groupdir.mkdir()
    atomic_json(groupdir / 'policy.json', manifest['provider_policy'])
    atomic_json(groupdir / 'group.json', {'version':1, 'batch':str(batch),
        'manifest_sha256':sha(batch / 'manifest.json'), 'global_lock_dir':str(groupdir / 'locks'),
        'policy_path':str(groupdir / 'policy.json'), 'policy_sha256':sha(groupdir / 'policy.json')})
    verify(batch)
    return {'prepared':True, 'batch':str(batch), 'episodes':18, 'snapshot':str(snapshot)}


def verify(batch):
    batch = Path(batch).resolve()
    manifest = read(batch / 'manifest.json')
    if sha(batch / 'manifest.json') != (batch / 'manifest.sha256').read_text(encoding='ascii').strip():
        raise ValueError('Manifest hash mismatch')
    _validate_manifest(manifest)
    snapshot = batch / 'runtime_snapshot'
    for relative, expected in manifest['runtime_sources'].items():
        if sha(_safe_child(snapshot, relative)) != expected:
            raise ValueError('Frozen runtime hash mismatch: ' + relative)
    for relative, expected in manifest['input_artifacts'].items():
        if sha(_safe_child(snapshot / 'modelbench/minimal_value_20260905/official', relative)) != expected:
            raise ValueError('Frozen official input mismatch: ' + relative)
    for relative, expected in manifest['evidence_files'].items():
        if sha(_safe_child(batch, relative)) != expected:
            raise ValueError('Frozen evidence mismatch: ' + relative)
    _validate_transport_binding(manifest, snapshot / 'modelbench/big_budget_team_20260906/TRANSPORT_POLICY.json')
    group = read(batch / 'group/group.json')
    if group['manifest_sha256'] != sha(batch / 'manifest.json') or group['batch'] != str(batch):
        raise ValueError('Provider group is not bound to this batch')
    if (Path(group['policy_path']).resolve() != batch / 'group/policy.json'
            or Path(group['global_lock_dir']).resolve() != batch / 'group/locks'
            or sha(group['policy_path']) != group['policy_sha256']
            or read(group['policy_path']) != manifest['provider_policy']):
        raise ValueError('Provider policy/group path mismatch')
    return manifest, snapshot


def _entry(manifest, run_id):
    result = next((e for e in manifest['schedule'] if e['run_id'] == run_id), None)
    if result is None:
        raise ValueError('Run outside the authorized C1 matrix')
    return result


def save_result(batch, entry, result):
    result.update(fresh_attempt_id=str(Path(batch).resolve()) + '::' + entry['run_id'],
                  experiment_protocol=PROTOCOL)
    return _b1.save_result(batch, entry, result)


def episode(batch, run_id):
    from .runtime import AblationRun
    from .transport import AblationTransport
    from modelbench.minimal_value_20260905 import runner
    from modelbench.minimal_value_20260905.budget import EpisodeBudget
    batch = Path(batch).resolve()
    manifest, snapshot = verify(batch)
    freeze.assert_import_origins(snapshot)
    entry = _entry(manifest, run_id)
    directory = batch / 'results' / run_id
    if directory.exists():
        raise ValueError('Existing generation attempt; no retry')
    receipt = read(batch / 'admissions' / (run_id + '.json'))
    if receipt.get('manifest_sha256') != sha(batch / 'manifest.json') or receipt.get('status') != 'admitted':
        raise ValueError('Missing bound admission')
    groupdir = batch / 'group'
    provider.install(groupdir, read(groupdir / 'group.json'), read(groupdir / 'policy.json'),
        runner_module=runner, run_class=AblationRun, transport_class=AblationTransport)
    runner.CONTAINER_SLOTS = threading.BoundedSemaphore(3)
    limits = entry['limits_override']
    q = {k:limits[k] for k in ('max_calls', 'token_limit', 'cm_call_allowance')}
    budget = EpisodeBudget(**q, deadline_seconds=limits['wall_seconds'], scopes={'solver':q})
    started = budget.root.created_at
    component, result = None, {}
    try:
        component = AblationRun(batch, entry, transport_factory=AblationTransport,
            budget=budget.scope('solver'), start_clock=started, deadline=budget.deadline_at, grade_enabled=False)
        budget.path = directory / 'episode-budget.json'
        budget._persist()
        result = component.run()
        budget.freeze()
        result.update(budget=budget.summary(), root_budget_snapshot=budget.snapshot(), grading_pending=True)
    except Exception as exc:
        if component is not None:
            component.cancel.set()
        cleaned = cleanup_owned_episode(directory)
        result.update(infrastructure_error={'type':type(exc).__name__, 'message':str(exc)},
            cleanup_confirmed=cleaned['confirmed'], controller_cleanup=cleaned,
            budget=budget.summary(), root_budget_snapshot=budget.snapshot())
    result['episode_wall_seconds'] = time.monotonic() - started
    result = save_result(batch, entry, result)
    atomic_json(directory / 'generation_result.json', result)
    return {'run_id':run_id, 'generation_finished':True, 'infrastructure_error':result.get('infrastructure_error')}


def grade_once(result, entry, directory, *, grader=None, cleanup=None):
    from modelbench.minimal_value_20260905.environment import rootgrade_terminal
    grader, cleanup = grader or rootgrade_terminal, cleanup or cleanup_owned_episode
    directory = Path(directory).resolve()
    artifact = result.get('artifact') or result.get('lead_artifact') or {}
    if (result.get('quiesced') is not True or result.get('cleanup_confirmed') is not True
            or result.get('infrastructure_error') or artifact.get('status') != 'present'):
        raise ValueError('Unfrozen or failed candidate cannot enter grading')
    patch = Path(artifact['path']).resolve()
    expected = artifact['sha256']
    if not patch.is_relative_to(directory) or sha(patch) != expected:
        raise ValueError('Frozen patch identity mismatch')
    marker = directory / 'grade-admission.json'
    with marker.open('x', encoding='utf-8') as stream:
        json.dump({'at':utc(), 'patch_sha256':expected, 'run_id':entry['run_id'], 'attempt':1}, stream)
    try:
        score = grader(entry['instance'], patch, expected, directory / 'grade-1',
            grader_contract=entry['grader_contract'], image=entry['image'], model_name=entry['arm'],
            timeout=900, memory='1g', cpus=2)
    finally:
        cleaned = cleanup(directory)
        atomic_json(directory / 'grade-cleanup.json', cleaned)
        if not cleaned['confirmed']:
            raise RuntimeError('Grader cleanup unconfirmed')
    if sha(patch) != expected:
        raise ValueError('Frozen patch changed during grading')
    return score, [score]


def grade(batch, run_id):
    batch = Path(batch).resolve()
    manifest, snapshot = verify(batch)
    freeze.assert_import_origins(snapshot)
    entry = _entry(manifest, run_id)
    directory = batch / 'results' / run_id
    result = read(directory / 'generation_result.json')
    started = time.monotonic()
    try:
        score, attempts = grade_once(result, entry, directory)
        result.update(score=score, grading_attempts=attempts, grading_pending=False)
        if not score.get('completed') and not (score.get('failure_kind') == 'candidate_empty_patch' and score.get('resolved') is False):
            result['grading_error'] = {'type':'OfficialScoringIncomplete', 'message':'Single permitted grading attempt failed'}
    except Exception as exc:
        result.update(grading_pending=False, grading_error={'type':type(exc).__name__, 'message':str(exc)})
    result['grading_wall_seconds'] = time.monotonic() - started
    result = save_result(batch, entry, result)
    return {'run_id':run_id, 'official_resolved':result.get('official_resolved'), 'grading_error':result.get('grading_error')}


def child_env(snapshot):
    value = _b1.child_env(snapshot)
    value['PYTHONUTF8'] = '1'
    return value


def start_child(batch, snapshot, command, run_id):
    logs = batch / 'logs'
    logs.mkdir(exist_ok=True)
    argv = [sys.executable, '-u', '-B', '-m', MODULE, command, '--batch', str(batch), '--run-id', run_id]
    with (logs / (run_id + '.' + command + '.out.log')).open('xb') as out, (
            logs / (run_id + '.' + command + '.err.log')).open('xb') as err:
        options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session':True}
        process = subprocess.Popen(argv, cwd=snapshot, env=child_env(snapshot), stdout=out, stderr=err, **options)
    atomic_json(logs / (run_id + '.' + command + '.process.json'), {'pid':process.pid, 'argv':argv, 'at':utc()})
    return process


def generation_health(result):
    """Operational health only: never screen quality, empty patches or budget stops."""
    failures = []
    budget = result.get('budget') or {}
    account = result.get('accounting') or {}
    if result.get('infrastructure_error'):
        failures.append('infrastructure_error')
    if result.get('cleanup_confirmed') is not True:
        failures.append('cleanup_unconfirmed')
    if budget.get('frozen') is not True or budget.get('pending_call_count') != 0 or budget.get('unknown_call_count') != 0:
        failures.append('usage_or_budget_unreconciled')
    if account.get('error') or account.get('protocol_issues') or account.get('usage_unknown_calls') not in (0, None):
        failures.append('accounting_protocol_error')
    if result.get('artifact_execution_integrity') is False:
        failures.append('artifact_execution_integrity')
    outcome = result.get('outcome') or {}
    if outcome.get('status') == 'transport_error' or result.get('status') == 'transport_error':
        failures.append('transport_error')
    return failures


def pilot_health(batch, entries):
    rows = []
    for entry in entries:
        directory = Path(batch) / 'results' / entry['run_id']
        result = read(directory / 'episode_result.json')
        issues = generation_health(result)
        if result.get('grading_pending') is not False or type(result.get('official_resolved')) is not bool or result.get('grading_error'):
            issues.append('official_scoring_incomplete')
        cm_failures = []
        events = directory / 'events.jsonl'
        if events.exists():
            for line in events.read_text(encoding='utf-8').splitlines():
                event = json.loads(line)
                if event.get('event') in ('cm_call_failed',):
                    cm_failures.append(event.get('event'))
        if cm_failures:
            issues.append('cm_route_or_admission_failure')
        rows.append({'run_id':entry['run_id'], 'issues':issues, 'cm_health_events':cm_failures})
    return {'at':utc(), 'passed':len(rows) == 6 and not any(r['issues'] for r in rows), 'rows':rows,
        'policy':'operational health only; resolved, empty patch, budget exhaustion and protocol repair counts are not quality gates'}


def _quota_pair(batch, state, pair, active):
    """One read-only query per admission wave; waits never block supervision."""
    now = time.monotonic()
    signature = ([e['run_id'] for e in pair], sorted(active))
    if state.get('_quota_signature') == signature and now < state.get('_quota_recheck', 0):
        return False
    key = freeze.credential_environment(REPO).get('GLM_API_KEY')
    snapshot = inspect_quota(key, timeout_seconds=20)
    # Current remaining quota must also cover still-live admitted obligations.
    obligations = [item['entry'] for item in active.values()] + list(pair)
    decision = assess_wave(snapshot, obligations)
    state['quota_check_count'] = state.get('quota_check_count', 0) + 1
    path = batch / 'quota' / ('check-%05d.json' % state['quota_check_count'])
    if path.exists():
        raise ValueError('Quota evidence collision')
    atomic_json(path, {'at':utc(), 'snapshot':snapshot, 'decision':decision,
        'prospective_pair':[e['run_id'] for e in pair], 'live_obligation_run_ids':sorted(active),
        'reservation_policy':'conservative full token cap for each live episode plus prospective pair'})
    state['quota_evidence'] = {'path':str(path), 'sha256':sha(path)}
    state['quota_decision'] = decision
    state['_quota_signature'], state['_quota_recheck'] = signature, now + 60
    if decision['status'] == 'ready':
        state['status'] = 'running'
        return True
    if decision['status'] == 'wait' and isinstance(decision.get('next_reset_ms'), (int, float)):
        state['status'] = 'waiting_for_coding_quota'
        state['quota_next_reset_ms'] = decision['next_reset_ms']
        state['_quota_recheck'] = now + max(60, decision['next_reset_ms'] / 1000 - time.time())
        return False
    _record_stop(state, 'coding_quota_unavailable', {'decision':decision, 'path':str(path)})
    return False


def _admit_pair(batch, snapshot, state, pair, active, manifest):
    # Entries retain frozen pair order; each paid admission is fenced separately.
    for entry in pair:
        request = _stop_request(batch)
        if request:
            _signal_drain(batch, state, request)
            return
        rid = entry['run_id']
        if state['admitted'] >= 18 or state['token_admission'] + 600000 > 10800000:
            raise ValueError('C1 admission budget exceeded')
        receipt = {'run_id':rid, 'at':utc(), 'status':'admitted',
            'manifest_sha256':sha(batch / 'manifest.json'), 'candidate_slots':3, 'token_limit':600000,
            'fresh_attempt_id':str(batch) + '::' + rid}
        path = batch / 'admissions' / (rid + '.json')
        path.parent.mkdir(exist_ok=True)
        with path.open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        state['admitted'] += 1
        state['token_admission'] += 600000
        summarize(batch, state)
        process = start_child(batch, snapshot, 'episode', rid)
        active[rid] = {'process':process, 'slots':3, 'started':time.monotonic(), 'entry':entry}


def _terminate(batch, rid, item, reason, lease):
    process = item['process']
    killed = kill_owned_process_tree(process) if process.poll() is None else {'confirmed':True}
    atomic_json(batch / 'logs' / (rid + '.termination.json'), {**killed, 'reason':reason})
    if not killed['confirmed'] or process.poll() is None:
        lease['retain'] = True
        raise RuntimeError('Owned process termination unconfirmed')


def coordinate(batch):
    batch = Path(batch).resolve()
    manifest, snapshot = verify(batch)
    freeze.assert_import_origins(snapshot)
    if (batch / 'state.json').exists():
        raise ValueError('Existing controller state; no implicit restart')
    if resources.stopped(batch / 'group') or (batch / 'STOP').exists():
        raise ValueError('Stopped group may not launch')
    state = {'status':'running', 'pid':os.getpid(), 'started_at':utc(), 'admitted':0,
        'token_admission':0, 'active':[], 'waves_completed':0, 'stop_reason':None,
        'total_planned':18, 'full_plan_total':18, 'episode_capacity':4,
        'pilot_health_gate':'pending', 'transport_policy':manifest.get('transport_policy')}
    deadline = time.monotonic() + manifest['dispatch_seconds']
    hard_deadline = deadline + manifest['final_drain_seconds']
    active, grader_process, grader_rid = {}, None, None
    monitor = resources.ResourceMonitor(batch / 'group', [batch / 'results'])
    with stage_lease(manifest['resource_lock_path'], batch) as lease:
        try:
            monitor.start()
            summarize(batch, state)
            for stage_index, stage_entries in enumerate((manifest['schedule'][:6], manifest['schedule'][6:])):
                if state['stop_reason']:
                    break
                state['phase'] = 'pilot_generation' if stage_index == 0 else 'remaining_generation'
                state['current_block'] = 'C1-first6' if stage_index == 0 else 'C1-remaining12'
                pending = list(stage_entries)
                ramp_after = None
                approved_pair = set()
                while pending or active:
                    now = time.monotonic()
                    if now >= hard_deadline:
                        raise RuntimeError('Finite overall deadline exceeded')
                    request = _stop_request(batch)
                    if request:
                        _signal_drain(batch, state, request)
                    if now >= deadline and pending:
                        _record_stop(state, 'dispatch_deadline')
                    observations = monitor.observations()
                    if (len(active) == 4 and state['episode_capacity'] == 4 and ramp_after is None):
                        ramp_after = now
                    if (ramp_after is not None and state['episode_capacity'] == 4 and
                            resources.ramp_ready(observations, sum(v['slots'] for v in active.values()), after=ramp_after)):
                        state['episode_capacity'] = 6
                        atomic_json(batch / 'resource-ramp.json', {'at':utc(), 'capacity':6,
                            'observations':observations, 'policy':'three spaced healthy samples plus projected six-container headroom'})
                    # Reap before admission, so an error can never silently free a new slot.
                    for rid, item in list(active.items()):
                        process = item['process']
                        expired = now - item['started'] > manifest['generation_watchdog_seconds']
                        grace = bool(state['stop_reason'] and state['stop_reason'] != 'dispatch_deadline' and now >= state.get('stop_monotonic', now) + manifest['cooperative_stop_grace_seconds'])
                        hard = bool(request and request.get('hard'))
                        if expired or grace or hard:
                            reason = 'generation_watchdog' if expired else 'resource_safety_termination' if hard else 'cooperative_stop_grace_expired'
                            _record_stop(state, reason)
                            _terminate(batch, rid, item, reason, lease)
                        code = process.poll()
                        if code is None:
                            continue
                        directory = batch / 'results' / rid
                        cleaned = cleanup_owned_episode(directory)
                        atomic_json(batch / 'logs' / (rid + '.cleanup.json'), cleaned)
                        if not cleaned['confirmed']:
                            lease['retain'] = True
                            _record_stop(state, 'cleanup_unconfirmed')
                        result_path = directory / 'generation_result.json'
                        if code != 0 or not result_path.exists():
                            _record_stop(state, 'generation_process_failed')
                            atomic_json(batch / 'logs' / (rid + '.failure.json'), {'returncode':code, 'result_present':result_path.exists()})
                        else:
                            issues = generation_health(read(result_path))
                            if issues:
                                _record_stop(state, 'generation_health_failed', {'run_id':rid, 'issues':issues})
                        del active[rid]
                    if state['stop_reason'] and state['stop_reason'] != 'dispatch_deadline':
                        _signal_drain(batch, state, {'reason':state['stop_reason'], 'hard':False})
                    if pending and not state['stop_reason']:
                        first = pending[0]
                        pair = [entry for entry in pending if entry['block_id'] == first['block_id']][:2]
                        used = sum(item['slots'] for item in active.values())
                        latest = observations[-1] if observations else None
                        fresh = latest and now - latest['sampled_monotonic'] <= 35
                        if (len(active) < state['episode_capacity'] and fresh
                                and resources.admission_headroom(latest, used, 3)):
                            if first['run_id'] not in approved_pair:
                                if len(pair) != 2:
                                    raise ValueError('Second pair member lacks its bound quota commitment')
                                if _quota_pair(batch, state, pair, active):
                                    approved_pair = {entry['run_id'] for entry in pair}
                                    state['quota_pair_commitment'] = {'run_ids':sorted(approved_pair),
                                        'at':utc(), 'evidence':state.get('quota_evidence')}
                            if first['run_id'] in approved_pair and not state['stop_reason']:
                                _admit_pair(batch, snapshot, state, [first], active, manifest)
                                admitted_ids = {path.stem for path in (batch / 'admissions').glob('*.json')}
                                pending = [entry for entry in pending if entry['run_id'] not in admitted_ids]
                        elif not active:
                            state['status'] = 'waiting_for_resource_headroom'
                    if state['stop_reason'] and state['stop_reason'] != 'dispatch_deadline':
                        _signal_drain(batch, state, {'reason':state['stop_reason'], 'hard':False})
                    state['active'] = [{'run_id':rid, 'pid':v['process'].pid, 'slots':3} for rid,v in active.items()]
                    summarize(batch, state)
                    if not active and state['stop_reason']:
                        break
                    time.sleep(1)
                if lease['retain']:
                    break
                # Complete the already generated cohort before opening the next one.
                for entry in stage_entries:
                    rid = entry['run_id']
                    path = batch / 'results' / rid / 'generation_result.json'
                    if not path.exists():
                        continue
                    result = read(path)
                    if result.get('infrastructure_error') or not result.get('cleanup_confirmed'):
                        continue
                    request = _stop_request(batch)
                    if request and request.get('hard'):
                        _record_stop(state, request['reason'], request)
                        break
                    if active:
                        raise RuntimeError('Generation must be empty before official grading')
                    if time.monotonic() >= hard_deadline:
                        raise RuntimeError('Finite drain deadline exceeded before grading')
                    state['phase'] = 'pilot_grading' if stage_index == 0 else 'remaining_grading'
                    state['grading_run_id'] = rid
                    summarize(batch, state)
                    grader_process, grader_rid = start_child(batch, snapshot, 'grade', rid), rid
                    grade_started = time.monotonic()
                    while grader_process.poll() is None:
                        request = _stop_request(batch)
                        if (time.monotonic() - grade_started >= manifest['grading_watchdog_seconds']
                                or time.monotonic() >= hard_deadline or (request and request.get('hard'))):
                            reason = 'resource_safety_termination' if request and request.get('hard') else 'grading_watchdog'
                            _record_stop(state, reason)
                            _terminate(batch, rid, {'process':grader_process}, reason, lease)
                            break
                        time.sleep(1)
                    cleaned = cleanup_owned_episode(path.parent)
                    if not cleaned['confirmed']:
                        lease['retain'] = True
                        _record_stop(state, 'grading_cleanup_unconfirmed')
                    if grader_process.returncode != 0:
                        _record_stop(state, 'grading_process_failed')
                    elif not (path.parent / 'episode_result.json').exists():
                        _record_stop(state, 'grading_result_missing')
                    else:
                        finished = read(path.parent / 'episode_result.json')
                        if finished.get('grading_error') or finished.get('infrastructure_error') or type(finished.get('official_resolved')) is not bool:
                            _record_stop(state, 'grading_or_evidence_error')
                    grader_process, grader_rid = None, None
                    summarize(batch, state)
                    if lease['retain']:
                        break
                state.pop('grading_run_id', None)
                state['waves_completed'] += 1
                if stage_index == 0 and not state['stop_reason']:
                    proof = pilot_health(batch, stage_entries)
                    atomic_json(batch / 'pilot-health.json', proof)
                    state['pilot_health_gate'] = 'passed' if proof['passed'] else 'failed'
                    if not proof['passed']:
                        _record_stop(state, 'pilot_operational_health_failed')
                summarize(batch, state)
            state['status'] = 'completed' if state['scored'] == 18 and not state['stop_reason'] else 'stopped_incomplete'
            summarize(batch, state)
        except BaseException as exc:
            _record_stop(state, type(exc).__name__ + ': ' + str(exc))
            state['status'] = 'controller_failed'
            state['controller_error'] = {'type':type(exc).__name__, 'message':str(exc)}
            _signal_drain(batch, state, {'reason':state['stop_reason'], 'hard':False})
            cleanup_targets = list(active.items())
            if grader_process is not None:
                cleanup_targets.insert(0, (grader_rid, {'process':grader_process}))
            for rid, item in cleanup_targets:
                try:
                    _terminate(batch, rid, item, 'controller_exception', lease)
                    cleaned = cleanup_owned_episode(batch / 'results' / rid)
                    if not cleaned['confirmed']:
                        lease['retain'] = True
                    else:
                        active.pop(rid, None)
                except BaseException as cleanup_error:
                    lease['retain'] = True
                    atomic_json(batch / 'logs' / (rid + '.exception-cleanup-error.json'),
                        {'type':type(cleanup_error).__name__, 'message':str(cleanup_error)})
            state['active'] = [{'run_id':rid, 'pid':v['process'].pid, 'slots':3} for rid,v in active.items()]
            summarize(batch, state)
            raise
        finally:
            monitor.stop()
    return {k:state.get(k) for k in ('status','admitted','generated','scored','resolved','stop_reason','pilot_health_gate')}


def launch(batch):
    batch = Path(batch).resolve()
    manifest, snapshot = verify(batch)
    if (batch / 'launch.json').exists() or (batch / 'state.json').exists():
        raise ValueError('Existing launch; duplicate controller forbidden')
    if Path(manifest['resource_lock_path']).exists():
        raise ValueError('Another experiment owner or unreconciled lease exists')
    env = child_env(snapshot)
    credentials = freeze.credential_environment(REPO)
    for key in ('GLM_API_KEY','GLM_BASE_URL','DEEPSEEK_API_KEY','DEEPSEEK_BASE_URL'):
        if credentials.get(key):
            env[key] = credentials[key]
    argv = [sys.executable, '-u', '-B', '-m', MODULE, 'coordinate', '--batch', str(batch)]
    # An exclusive launch-intent also fences the parent-side launch race.
    with (batch / 'launch-intent.json').open('x', encoding='utf-8') as stream:
        json.dump({'at':utc(), 'manifest_sha256':sha(batch / 'manifest.json')}, stream)
    with (batch / 'controller.stdout.log').open('xb') as out, (batch / 'controller.stderr.log').open('xb') as err:
        options = {'creationflags':subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == 'nt' else {'start_new_session':True}
        process = subprocess.Popen(argv, cwd=snapshot, env=env, stdout=out, stderr=err,
            stdin=subprocess.DEVNULL, **options)
    descriptor = {'pid':process.pid, 'argv':argv, 'at':utc(), 'snapshot':str(snapshot),
                  'manifest_sha256':sha(batch / 'manifest.json')}
    atomic_json(batch / 'launch.json', descriptor)
    return descriptor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare','launch','coordinate','episode','grade','status','verify'])
    parser.add_argument('--batch', required=True)
    parser.add_argument('--manifest-path')
    parser.add_argument('--run-id')
    args = parser.parse_args()
    if args.command == 'prepare':
        if not args.manifest_path:
            parser.error('prepare requires --manifest-path')
        result = prepare(args.batch, manifest_path=args.manifest_path)
    elif args.command in ('episode','grade'):
        if not args.run_id:
            parser.error('episode/grade requires --run-id')
        result = globals()[args.command](args.batch, args.run_id)
    elif args.command == 'status':
        result = read(Path(args.batch) / 'state.json')
    elif args.command == 'verify':
        manifest, snapshot = verify(args.batch)
        result = {'passed':True,'episodes':len(manifest['schedule']),'snapshot':str(snapshot)}
    else:
        result = globals()[args.command](args.batch)
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
