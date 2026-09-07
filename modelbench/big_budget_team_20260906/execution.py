"""Finite B1 preparation, isolated generation, serial grading and supervision."""
from __future__ import annotations
import argparse
from collections import Counter
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

from modelbench.minimal_value_20260905 import freeze, reporting
from modelbench.minimal_value_20260905.cli import (
    cleanup_owned_episode, grade_frozen, kill_owned_process_tree, stage_lease)
from modelbench.minimal_value_20260905.runtime_integrity import atomic_json
from modelbench.minimal_value_20260905.operations import parallel_resources as resources

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OFFICIAL = REPO / 'modelbench/minimal_value_20260905/official'
PROTOCOL = 'big_budget_team_execution_v1'
LEGACY_CONTINUATION_PROTOCOL = 'big_budget_dispatch_continuation_v1'
CONTINUATION_PROTOCOL = 'big_budget_dispatch_continuation_v2'


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_sources():
    source = freeze.runtime_sources(REPO)
    for parent, pattern in [(HERE, '*.py'), (HERE, '*.json'),
                            (REPO/'modelbench/minimal_value_20260905/operations', '*.py')]:
        for path in parent.glob(pattern):
            source[path.relative_to(REPO).as_posix()] = sha(path)
    for name in ['planned_cells.csv', 'task_metadata.csv', 'PLAN_ZH.md']:
        path = HERE/name
        source[path.relative_to(REPO).as_posix()] = sha(path)
    path = REPO/'modelbench/minimal_value_20260905/operations/resource-defaults.json'
    source[path.relative_to(REPO).as_posix()] = sha(path)
    return dict(sorted(source.items()))


def transport_sources():
    return {k:v for k,v in runtime_sources().items() if any(name in k for name in (
        'transport.py','provider.py','provider_limits.py','parallel_resources.py','keyconfig.py','protocol.py',
        'TRANSPORT_POLICY.json','glm_stream_http_worker.py','coding_quota.py','CODING_PLAN_AMENDMENT.json'))}



def dispatch_schedule(manifest):
    """The full randomized plan stays intact; only dispatch eligibility narrows."""
    schedule = manifest['schedule']
    requested = manifest.get('dispatch_run_ids')
    if requested is None:
        return schedule
    if not isinstance(requested, list) or len(requested) != len(set(requested)):
        raise ValueError('Dispatch IDs must be a unique ordered list')
    selected = [entry for entry in schedule if entry['run_id'] in set(requested)]
    if [entry['run_id'] for entry in selected] != requested:
        raise ValueError('Dispatch subset must retain the full schedule order')
    return selected


def _legacy_continuation_details(source_batch, schedule):
    """Read-only seal of a stopped source, including every already admitted cell."""
    source_batch = Path(source_batch).resolve()
    if read(source_batch/'manifest.json').get('continuation'):
        raise ValueError('Nested continuation requires an explicit cumulative lineage')
    source, _ = verify(source_batch)
    if source['schedule'] != schedule:
        raise ValueError('Continuation must preserve the complete original schedule and contracts')
    state = read(source_batch/'state.json')
    if state.get('status') not in ('stopped_incomplete', 'completed') or state.get('active') != []:
        raise ValueError('Continuation source must be stopped with no active generation')
    receipts = sorted((source_batch/'admissions').glob('*.json'))
    admitted = {path.stem for path in receipts}
    catalog = {entry['run_id']: entry for entry in schedule}
    if (not admitted or not admitted <= set(catalog)
            or state.get('admitted') != len(admitted)):
        raise ValueError('Source admissions do not reconcile with stopped controller state')
    observed = {path.name for path in (source_batch/'results').iterdir() if path.is_dir()}
    processes = {path.name.removesuffix('.generate.process.json')
                 for path in (source_batch/'logs').glob('*.generate.process.json')}
    if observed != admitted or not processes <= admitted:
        raise ValueError('Source contains an unbound or incomplete generation attempt')
    hashes = {name: sha(source_batch/name) for name in ('manifest.json', 'manifest.sha256', 'state.json')}
    summaries = []
    for entry in schedule:
        rid = entry['run_id']
        if rid not in admitted:
            continue
        receipt_path = source_batch/'admissions'/(rid+'.json')
        receipt = read(receipt_path)
        if (receipt.get('run_id') != rid or receipt.get('status') != 'admitted'
                or receipt.get('manifest_sha256') != sha(source_batch/'manifest.json')
                or receipt.get('token_limit') != entry['limits_override']['token_limit']):
            raise ValueError('Source admission receipt is not bound: '+rid)
        directory = source_batch/'results'/rid
        result = read(directory/'episode_result.json')
        budget = result.get('budget') or {}
        if (result.get('run_id') != rid or result.get('grading_pending') is not False
                or type(result.get('official_resolved')) is not bool
                or result.get('cleanup_confirmed') is not True
                or budget.get('frozen') is not True or budget.get('pending_call_count') != 0):
            raise ValueError('Source attempt lacks a cleaned, frozen, scored terminal result: '+rid)
        for path in (receipt_path, directory/'generation_result.json', directory/'episode_result.json', directory/'episode-budget.json'):
            hashes[path.relative_to(source_batch).as_posix()] = sha(path)
        # Bind recorded calls, including unknown usage. They remain old observations.
        for path in sorted((directory/'calls').glob('*/metadata.json')):
            hashes[path.relative_to(source_batch).as_posix()] = sha(path)
        summaries.append({'run_id': rid, 'condition_id': entry['condition_id'],
            'official_resolved': result['official_resolved'],
            'unknown_call_count': budget.get('unknown_call_count'),
            'reserved_tokens': budget.get('reserved_tokens'),
            'total_tokens': budget.get('total_tokens'),
            'api_equivalent_usd': result.get('api_equivalent_usd')})
    if state.get('generated') != len(admitted) or state.get('scored') != len(admitted):
        raise ValueError('Source generation and scoring totals do not reconcile')
    ids = [entry['run_id'] for entry in schedule if entry['run_id'] in admitted]
    return {'protocol': LEGACY_CONTINUATION_PROTOCOL, 'source_batch': str(source_batch),
        'source_manifest_sha256': sha(source_batch/'manifest.json'),
        'source_artifacts': dict(sorted(hashes.items())), 'excluded_run_ids': ids,
        'source_transport_policy': source.get('transport_policy'),
        'source_transport_policy_sha256': source.get('transport_policy_sha256'),
        'source_runtime_sources': source['runtime_sources'],
        'prior_results': summaries,
        'prior_totals': {'admitted': len(ids), 'generated': len(ids), 'scored': len(ids),
            'resolved': sum(item['official_resolved'] for item in summaries),
            'unknown_calls': sum(item['unknown_call_count'] for item in summaries),
            'reserved_tokens': sum(item['reserved_tokens'] for item in summaries),
            'token_admission': sum(catalog[rid]['limits_override']['token_limit'] for rid in ids)},
        'policy': 'exclude every source admission, including unknown usage; never retry an already started cell'}



def _prior_totals(rows):
    return {'admitted':len(rows), 'generated':sum(row['generation_result_present'] for row in rows),
        'scored':sum(type(row['official_resolved']) is bool for row in rows),
        'interrupted':sum(row['attempt_status']=='interrupted' for row in rows),
        'resolved':sum(row['official_resolved'] is True for row in rows),
        'unknown_calls':sum(row.get('unknown_call_count') or 0 for row in rows),
        'pending_calls':sum(row.get('pending_call_count') or 0 for row in rows),
        'reserved_tokens':sum(row.get('reserved_tokens') or 0 for row in rows),
        'token_admission':sum(row['token_admission'] for row in rows)}


def continuation_details(source_batch, schedule, *, _lineage_seen=None):
    """Seal all prior admissions across generations without inventing terminal results."""
    from modelbench.minimal_value_20260905.budget import EpisodeBudget
    source_batch=Path(source_batch).resolve()
    source,_=verify(source_batch,_lineage_seen=_lineage_seen)
    if source['schedule']!=schedule:
        raise ValueError('Continuation must preserve the complete original schedule and contracts')
    state=read(source_batch/'state.json')
    if state.get('status') not in ('stopped_incomplete','completed','controller_failed') or state.get('active')!=[]:
        raise ValueError('Continuation source must be stopped with no active generation')
    catalog={entry['run_id']:entry for entry in schedule}
    receipts=sorted((source_batch/'admissions').glob('*.json'));admitted={p.stem for p in receipts}
    allowed={entry['run_id'] for entry in dispatch_schedule(source)}
    if not admitted or not admitted<=allowed or state.get('admitted')!=len(admitted):
        raise ValueError('Source admissions do not reconcile with stopped controller state')
    result_root=source_batch/'results'
    observed={p.name for p in result_root.iterdir() if p.is_dir()} if result_root.exists() else set()
    processes={p.name.removesuffix('.generate.process.json') for p in (source_batch/'logs').glob('*.generate.process.json')}
    if not observed<=admitted or not processes<=admitted:
        raise ValueError('Source contains an unbound generation attempt')
    hashes={name:sha(source_batch/name) for name in ('manifest.json','manifest.sha256','state.json')}
    local=[]
    for entry in schedule:
        rid=entry['run_id']
        if rid not in admitted:continue
        receipt_path=source_batch/'admissions'/(rid+'.json');receipt=read(receipt_path)
        if (receipt.get('run_id')!=rid or receipt.get('status')!='admitted'
                or receipt.get('manifest_sha256')!=sha(source_batch/'manifest.json')
                or receipt.get('token_limit')!=entry['limits_override']['token_limit']):
            raise ValueError('Source admission receipt is not bound: '+rid)
        directory=result_root/rid;generation=directory/'generation_result.json';terminal=directory/'episode_result.json'
        result=read(terminal) if terminal.is_file() else {}
        if result and result.get('run_id')!=rid:raise ValueError('Source result identity mismatch: '+rid)
        cleanup_path=source_batch/'logs'/(rid+'.cleanup.json')
        cleaned=result.get('cleanup_confirmed') is True or (cleanup_path.is_file() and read(cleanup_path).get('confirmed') is True)
        if (directory.exists() or rid in processes) and not cleaned:
            raise ValueError('Source attempt has no confirmed controller or terminal cleanup: '+rid)
        snapshot_path=directory/'episode-budget.json'
        budget=EpisodeBudget.from_snapshot(read(snapshot_path)).summary() if snapshot_path.is_file() else result.get('budget') or {}
        scored=type(result.get('official_resolved')) is bool and result.get('grading_pending') is False
        if scored and (not generation.is_file() or budget.get('frozen') is not True or budget.get('pending_call_count')):
            raise ValueError('Scored source result lacks a frozen completed budget: '+rid)
        # Absence of an export is an interrupted observation, never an empty patch or failure score.
        local.append({'run_id':rid,'condition_id':entry['condition_id'], 'source_batch':str(source_batch),
            'source_manifest_sha256':sha(source_batch/'manifest.json'),
            'source_transport_policy':source.get('transport_policy'),
            'attempt_status':'scored' if scored else 'interrupted',
            'generation_result_present':generation.is_file(), 'episode_result_present':terminal.is_file(),
            'official_resolved':result['official_resolved'] if scored else None,
            'cleanup_confirmed':cleaned,'budget_frozen':budget.get('frozen'),
            'unknown_call_count':budget.get('unknown_call_count'),'pending_call_count':budget.get('pending_call_count'),
            'reserved_tokens':budget.get('reserved_tokens'),'known_token_subtotal':budget.get('known_subtotal'),
            'total_tokens':budget.get('total_tokens'),'api_equivalent_usd':result.get('api_equivalent_usd'),
            'token_admission':entry['limits_override']['token_limit']})
        files=[receipt_path,generation,terminal,snapshot_path,directory/'result.json',directory/'model.patch']
        files+=list((directory/'calls').glob('*/metadata.json'))
        files+=list((source_batch/'logs').glob(rid+'.*.json'))
        for path in files:
            if path.is_file():hashes[path.relative_to(source_batch).as_posix()]=sha(path)
    local_totals=_prior_totals(local)
    if state.get('generated')!=local_totals['generated'] or state.get('scored')!=local_totals['scored']:
        raise ValueError('Source generation and scoring totals do not reconcile')
    ancestry=source.get('continuation') or {};ancestors=[];lineage=[]
    if ancestry:
        for row in ancestry['prior_results']:
            value=dict(row)
            value.setdefault('source_batch',ancestry['source_batch'])
            value.setdefault('source_manifest_sha256',ancestry['source_manifest_sha256'])
            value.setdefault('source_transport_policy',ancestry.get('source_transport_policy'))
            value.setdefault('attempt_status','scored')
            value.setdefault('generation_result_present',True)
            value.setdefault('episode_result_present',True)
            value.setdefault('cleanup_confirmed',True);value.setdefault('budget_frozen',True)
            value.setdefault('pending_call_count',0)
            value.setdefault('known_token_subtotal',None)
            value.setdefault('token_admission',catalog[value['run_id']]['limits_override']['token_limit'])
            ancestors.append(value)
        lineage=list(ancestry.get('lineage') or [{'batch':ancestry['source_batch'],
            'manifest_sha256':ancestry['source_manifest_sha256'],
            'transport_policy':ancestry.get('source_transport_policy'),'totals':_prior_totals(ancestors)}])
    rows=ancestors+local
    if len({row['run_id'] for row in rows})!=len(rows):raise ValueError('A run was admitted in multiple lineage generations')
    ordering={entry['run_id']:index for index,entry in enumerate(schedule)}
    rows.sort(key=lambda row:ordering[row['run_id']])
    lineage.append({'batch':str(source_batch),'manifest_sha256':sha(source_batch/'manifest.json'),
                    'transport_policy':source.get('transport_policy'),'totals':local_totals})
    return {'protocol':CONTINUATION_PROTOCOL,'source_batch':str(source_batch),
        'source_manifest_sha256':sha(source_batch/'manifest.json'),
        'source_artifacts':dict(sorted(hashes.items())), 'excluded_run_ids':[row['run_id'] for row in rows],
        'source_transport_policy':source.get('transport_policy'),
        'source_transport_policy_sha256':source.get('transport_policy_sha256'),
        'source_runtime_sources':source['runtime_sources'],'lineage':lineage,
        'prior_results':rows,'prior_totals':_prior_totals(rows),
        'policy':'exclude every prior admission across all generations; preserve scored, interrupted, unknown and pending separately; never retry'}


def _mvp_details(authorization, schedule, batch):
    """One explicitly approved six-cell addendum; original solver identities stay intact."""
    auth = authorization
    if (auth.get('protocol') != 'big_budget_core_mvp_v1' or auth.get('approved') is not True
            or auth.get('maximum_new_generation_admissions') != 6
            or auth.get('automatic_generation_retry') is not False):
        raise ValueError('MVP requires explicit authorization for exactly six fresh attempts')
    tasks = {'pylint-dev__pylint-6386', 'pydata__xarray-6992'}
    chosen = [e for e in schedule if e['rep'] == 1 and e['instance']['instance_id'] in tasks
              and e['condition_id'] in ('T00', 'T11', 'S-S')]
    ids = [e['run_id'] for e in chosen]
    if len(chosen) != 6 or auth.get('selected_run_ids') != ids:
        raise ValueError('MVP selection must be the fixed two tasks by three conditions in original order')
    predecessor = Path(auth['predecessor_batch']).resolve()
    batch = Path(batch).resolve()
    if predecessor == batch:
        raise ValueError('MVP fresh attempts require a separate new batch')
    source = read(predecessor/'manifest.json')
    if source['schedule'] != schedule:
        raise ValueError('MVP source entries must retain their qualified solver configuration')
    current = [e['run_id'] for e in schedule if e['block_id'] == 'b1-r1-q05']
    if auth.get('predecessor_run_ids') != current or len(current) != 10:
        raise ValueError('MVP prerequisite must be the current ten admitted Matplotlib attempts')
    manifest_sha = sha(predecessor/'manifest.json')
    if auth.get('predecessor_manifest_sha256') != manifest_sha:
        raise ValueError('MVP authorization predecessor manifest binding changed')
    if (predecessor/'manifest.sha256').read_text(encoding='ascii').strip() != manifest_sha:
        raise ValueError('MVP predecessor manifest hash changed')
    return {'protocol':'big_budget_core_mvp_dispatch_v1', 'predecessor_batch':str(predecessor),
        'predecessor_manifest_sha256':manifest_sha, 'predecessor_run_ids':current,
        'dispatch_run_ids':ids, 'automatic_generation_retry':False,
        'fresh_attempts':[{'fresh_attempt_id':str(batch)+'::'+e['run_id'],
            'source_entry':{'batch':str(predecessor), 'manifest_sha256':manifest_sha,
                'run_id':e['run_id'], 'configuration_sha256':e['configuration_sha256']},
            'identity_scope':'batch absolute path plus canonical run_id',
            'reason':'explicitly authorized fresh Coding Plan attempt; retain prior standard API results separately'}
            for e in chosen]}


def _mvp_ready(manifest):
    """No child or controller starts until all ten predecessor grades are durable."""
    details = manifest.get('mvp')
    if details is None:
        return
    source = Path(details['predecessor_batch'])
    state = read(source/'state.json')
    ids = details['predecessor_run_ids']
    if (state.get('status') not in ('stopped_incomplete', 'completed')
            or state.get('stop_reason') not in (None, 'operator_stop')
            or (state.get('status') == 'stopped_incomplete' and state.get('stop_reason') != 'operator_stop')
            or state.get('active') or state.get('grading_run_id')
            or any(state.get(k) != 10 for k in ('admitted', 'generated', 'scored'))
            or {p.stem for p in (source/'admissions').glob('*.json')} != set(ids)):
        raise ValueError('MVP prerequisite: current ten must finish generation and official scoring first')
    for rid in ids:
        result = read(source/'results'/rid/'episode_result.json')
        if (result.get('run_id') != rid or result.get('grading_pending') is not False
                or type(result.get('official_resolved')) is not bool
                or result.get('cleanup_confirmed') is not True
                or result.get('infrastructure_error') or result.get('grading_error')):
            raise ValueError('MVP prerequisite result is not a clean official terminal: '+rid)


def verify(batch, *, _lineage_seen=None):
    batch = Path(batch).resolve()
    seen=set(_lineage_seen or ())
    if batch in seen:raise ValueError('Continuation lineage cycle')
    seen.add(batch)
    m = read(batch/'manifest.json')
    if (batch/'manifest.sha256').read_text(encoding='ascii').strip() != sha(batch/'manifest.json'):
        raise ValueError('Execution manifest changed')
    if m['protocol'] != PROTOCOL:
        raise ValueError('Wrong protocol')
    snapshot = batch/'runtime_snapshot'
    for path, expected in m['runtime_sources'].items():
        if sha(snapshot/path) != expected:
            raise ValueError('Frozen source drift: '+path)
    policy_relative = 'modelbench/big_budget_team_20260906/TRANSPORT_POLICY.json'
    if policy_relative in m['runtime_sources']:
        policy_path = snapshot/policy_relative
        if (m.get('transport_policy_sha256') != sha(policy_path)
                or m.get('transport_policy') != read(policy_path)):
            raise ValueError('Frozen transport policy binding changed or missing')
    official = snapshot/'modelbench/minimal_value_20260905/official'
    for path, expected in m['input_artifacts'].items():
        if sha(official/path) != expected:
            raise ValueError('Frozen input drift: '+path)
    if (snapshot/'modelbench/keys.local.json').exists():
        raise ValueError('Credential file in snapshot')
    for name, expected in m['evidence_files'].items():
        if sha(batch/name) != expected:
            raise ValueError('Frozen evidence drift: '+name)
    for name, origin in m.get('evidence_origins', {}).items():
        if origin.get('sha256') != m['evidence_files'].get('preflight/'+name):
            raise ValueError('Qualification evidence origin binding changed')
    group = read(batch/'group/group.json')
    if sha(group['policy_path']) != group['policy_sha256']:
        raise ValueError('Provider policy changed')
    if group['manifest_sha256'] != sha(batch/'manifest.json'):
        raise ValueError('Group manifest binding changed')
    from .contracts import validate_schedule
    validate_schedule(m['schedule'])
    dispatched = dispatch_schedule(m)
    continuation = m.get('continuation')
    if m.get('mvp') is not None:
        if continuation is not None:
            raise ValueError('MVP fresh attempts cannot also be a no-repeat continuation')
        observed = _mvp_details(read(batch/'preflight/mvp-authorization.json'), m['schedule'], batch)
        if observed != m['mvp'] or m.get('dispatch_run_ids') != observed['dispatch_run_ids']:
            raise ValueError('MVP frozen selection or fresh-attempt identity changed')
        limits = {'generation_attempt_limit':6,
            'token_admission_sum_limit':sum(e['limits_override']['token_limit'] for e in dispatched),
            'ordinary_ticket_limit':sum(e['limits_override']['max_calls'] for e in dispatched),
            'cm_ticket_limit':sum(e['limits_override']['cm_call_allowance'] for e in dispatched)}
        if any(m.get(k) != v for k,v in limits.items()) or m.get('automatic_generation_retry') is not False:
            raise ValueError('MVP exact admission limits or no-retry rule changed')
    elif continuation is not None:
        if Path(continuation.get('source_batch', batch)).resolve() == batch:
            raise ValueError('A continuation cannot refer to itself')
        if continuation.get('protocol')==LEGACY_CONTINUATION_PROTOCOL:
            observed=_legacy_continuation_details(continuation['source_batch'],m['schedule'])
        elif continuation.get('protocol')==CONTINUATION_PROTOCOL:
            observed=continuation_details(continuation['source_batch'],m['schedule'],_lineage_seen=seen)
        else:raise ValueError('Unrecognized continuation protocol')
        if observed != continuation:
            raise ValueError('Continuation source evidence changed')
        expected = [entry['run_id'] for entry in m['schedule']
                    if entry['run_id'] not in set(continuation['excluded_run_ids'])]
        if m.get('dispatch_run_ids') != expected:
            raise ValueError('Continuation dispatch must exclude every prior admission exactly once')
        limits = {'generation_attempt_limit': len(dispatched),
                  'token_admission_sum_limit': sum(x['limits_override']['token_limit'] for x in dispatched),
                  'ordinary_ticket_limit': sum(x['limits_override']['max_calls'] for x in dispatched),
                  'cm_ticket_limit': sum(x['limits_override']['cm_call_allowance'] for x in dispatched)}
        if any(m.get(key) != value for key, value in limits.items()):
            raise ValueError('Continuation admission limits do not match the remaining subset')
    elif m.get('dispatch_run_ids') is not None and len(dispatched) != len(m['schedule']):
        raise ValueError('A reduced dispatch subset requires a bound continuation source')
    return m, snapshot


def prepare(batch, *, continue_from=None, offline_validation=None, offline_tests=None,
            transport_probes=None, source_qualification=None, mvp_authorization=None):
    from .contracts import build_entry, load_cells, load_plan, validate_schedule
    from modelbench.minimal_value_20260905.environment import capture_grader_contract
    batch = Path(batch).resolve()
    if batch.exists():
        raise ValueError('No implicit overwrite of a prepared batch')
    if continue_from is not None and mvp_authorization is not None:
        raise ValueError('MVP fresh attempts and continuation are mutually exclusive')
    plan = load_plan()
    source = runtime_sources()
    paths = {
        'offline-validation.json': Path(offline_validation or HERE/'preflight/offline-validation.json').resolve(),
        'offline-tests.xml': Path(offline_tests or HERE/'preflight/offline-tests.xml').resolve(),
        'transport-probes.json': Path(transport_probes or HERE/'preflight/transport-probes.json').resolve(),
        'source_qualification_review.json': Path(source_qualification or HERE/'preflight/source_qualification_review.json').resolve()}
    if mvp_authorization is not None:
        paths['mvp-authorization.json'] = Path(mvp_authorization).resolve()
    evidence_input_hashes = {name:sha(path) for name,path in paths.items()}
    gate = read(paths['offline-validation.json'])
    if gate.get('passed') is not True or gate.get('runtime_sources') != source:
        raise ValueError('Offline validation must bind exact current runtime sources')
    if sha(paths['offline-tests.xml']) != gate['junit_sha256']:
        raise ValueError('Offline test evidence changed')
    probes = read(paths['transport-probes.json'])
    if probes.get('passed') is not True or probes.get('calls_issued', 99) > 8:
        raise ValueError('Bounded transport probes have not qualified')
    if probes.get('transport_sources') != transport_sources():
        raise ValueError('Probe runtime binding changed')
    qualification = read(paths['source_qualification_review.json'])
    if qualification.get('status') != 'six_source_qualifications_reusable_under_exact_grader_and_resource_contract':
        raise ValueError('Source environment qualifications have not passed')
    for proof in qualification['input_artifacts'] + qualification['adapter_sources']:
        if sha(proof['path']) != proof['sha256']:
            raise ValueError('Qualification source bytes changed')
    if len(qualification['tasks']) != 6:
        raise ValueError('All six task qualifications are required')
    prior = read(REPO/'modelbench/minimal_value_20260905/batches/a2-v1/manifest.json')
    images = {e['instance']['instance_id']: e['image'] for e in prior['schedule']}
    public = {e['instance_id']:e for e in read(OFFICIAL/'selected_public.json')}
    checks = read(OFFICIAL/'public_checks.json')
    grader = capture_grader_contract()
    entries = [build_entry(cell,public[cell['instance_id']],checks[cell['instance_id']],
                           images[cell['instance_id']],grader) for cell in load_cells()]
    validate_schedule(entries)
    if grader != qualification['shared_grader_contract']:
        raise ValueError('Grader differs from qualified contract')
    qualified={q['instance_id']:q for q in qualification['tasks']}
    for entry in entries:
        q=qualified[entry['instance']['instance_id']]
        if entry['instance']!=q['public_instance'] or entry['image']!=q['image_id'] or sha(q['image']['path'])!=q['image']['sha256'] or entry['public_checks']!=q['public_checks'] or not q.get('qualified'):
            raise ValueError('Actual candidate inputs differ from qualified baseline')
    continuation = continuation_details(continue_from, entries) if continue_from is not None else None
    excluded = set(continuation['excluded_run_ids']) if continuation else set()
    mvp = _mvp_details(read(paths['mvp-authorization.json']), entries, batch) if mvp_authorization is not None else None
    dispatched = [entry for entry in entries if entry['run_id'] not in excluded
                  and (mvp is None or entry['run_id'] in mvp['dispatch_run_ids'])]
    if not dispatched:
        raise ValueError('No unstarted cells remain for continuation')
    inputs = freeze.input_files(OFFICIAL)
    batch.mkdir(parents=True)
    snap = batch/'runtime_snapshot'
    for relative, expected in source.items():
        target = snap/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(REPO/relative,target)
        if sha(target) != expected:
            raise ValueError('Source changed while freezing')
    for relative, expected in inputs.items():
        target = snap/'modelbench/minimal_value_20260905/official'/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(OFFICIAL/relative,target)
        if sha(target) != expected:
            raise ValueError('Input changed while freezing')
    evidence = {}
    for name in paths:
        target = batch/'preflight'/name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(paths[name],target)
        evidence['preflight/'+name] = sha(target)
        if evidence['preflight/'+name] != evidence_input_hashes[name]:
            raise ValueError('Qualification evidence changed while freezing')
    shutil.copyfile(HERE/'pricing.json',batch/'pricing.json')
    evidence['pricing.json'] = sha(batch/'pricing.json')
    manifest = {'protocol':PROTOCOL,'created_at':utc(),'schedule':entries,
        'runtime_sources':source,'input_artifacts':inputs,'evidence_files':evidence,
        'plan_sha256':sha(HERE/'plan_contract.json'),'source_repo':str(REPO),
        'transport_policy':read(HERE/'TRANSPORT_POLICY.json'),
        'transport_policy_sha256':sha(HERE/'TRANSPORT_POLICY.json'),
        'candidate_container_cap':12,'episode_slots':12,'candidate_memory':'1g',
        'candidate_cpus':2,'generation_watchdog_seconds':7500,
        'grading_watchdog_seconds':2100,'dispatch_seconds':7*86400,
        'cooperative_stop_grace_seconds':1080,
        'coding_quota_gate':{'enabled':True,'max_poll_sleep_seconds':60,'require_current_snapshot':True},
        'final_drain_seconds':48*3600,'probe_call_count':probes['calls_issued'],
        'token_admission_sum_limit':sum(x['limits_override']['token_limit'] for x in dispatched),
        'generation_attempt_limit':len(dispatched),
        'ordinary_ticket_limit':sum(x['limits_override']['max_calls'] for x in dispatched),
        'cm_ticket_limit':sum(x['limits_override']['cm_call_allowance'] for x in dispatched),
        'evidence_origins':{name:{'path':str(path),'sha256':evidence_input_hashes[name]} for name,path in paths.items()},
        'resource_lock_path':str(REPO/'modelbench/big_budget_team_20260906/execution.lock'),
        'qualification_reuse':'byte-identical prior official inputs, adapter and image IDs; see bound evidence',
        'valuation':'frozen dated API-equivalent scenario, not subscription cash invoice',
        'automatic_generation_retry':False}
    if mvp is not None:
        manifest.update(mvp=mvp, dispatch_run_ids=mvp['dispatch_run_ids'],
            result_comparison_policy='core MVP fresh Coding attempts and prior standard attempts are separate strata')
    if continuation is not None:
        manifest.update(continuation=continuation, dispatch_run_ids=[x['run_id'] for x in dispatched],
                        result_comparison_policy='report original and streaming continuation as separate transport strata')
    atomic_json(batch/'manifest.json',manifest)
    (batch/'manifest.sha256').write_text(sha(batch/'manifest.json')+'\n',encoding='ascii')
    policy = {'provider_limits_version':3,'provider_limits_source':'operator_configured',
        'provider_limits':{'codex_account':4,'glm_coding_plan':4,'deepseek':4},
        'global_model_slots':16,'per_episode_model_slots':4}
    groupdir=batch/'group';groupdir.mkdir()
    atomic_json(groupdir/'policy.json',policy)
    atomic_json(groupdir/'group.json',{'version':1,'batch':str(batch),
        'manifest_sha256':sha(batch/'manifest.json'),
        'global_lock_dir':str(groupdir/'locks'),'policy_path':str(groupdir/'policy.json'),
        'policy_sha256':sha(groupdir/'policy.json')})
    verify(batch)
    return {'prepared':True,'batch':str(batch),'episodes':len(dispatched),'full_plan_episodes':len(entries),
            'prior_admitted_excluded':len(excluded),'snapshot':str(snap)}


def save_result(batch,entry,result):
    directory=Path(batch)/'results'/entry['run_id']
    directory.mkdir(parents=True,exist_ok=True)
    try:
        account=reporting.accounting(directory,read(Path(batch)/'pricing.json'))
    except Exception as exc:
        account={'calls':[], 'call_count':None, 'cost_computable':False,
            'api_equivalent_usd':None,'api_equivalent_known_subtotal_usd':None,
            'total_tokens_known_subtotal':None,'usage_unknown_calls':None,
            'error':{'type':type(exc).__name__,'message':str(exc)}}
        result['accounting_error']=account['error']
        result['infrastructure_error']={'type':'AccountingError','message':str(exc)}
    result['accounting']=account
    if result.get('root_budget_snapshot') is not None and not account.get('error'):
        try:
            audit=reporting.audit_accounting(directory,result,account)
            result['accounting_integrity']=audit
            if not audit.get('passed'):
                result['infrastructure_error']={'type':'AccountingIntegrityError','message':'Canonical ledger audit failed'}
        except Exception as exc:
            result['accounting_integrity']={'passed':False,'error':str(exc)}
            result['infrastructure_error']={'type':'AccountingIntegrityError','message':str(exc)}
    result.update(run_id=entry['run_id'],arm=entry['arm'],base_arm=entry['arm'],
        condition_id=entry['condition_id'],rep=entry['rep'],phase=entry['phase'],
        instance_id=entry['instance']['instance_id'],controller_completed_at=utc(),
        api_equivalent_usd=account['api_equivalent_usd'],
        api_equivalent_known_subtotal_usd=account['api_equivalent_known_subtotal_usd'])
    manifest = read(Path(batch)/'manifest.json') if (Path(batch)/'manifest.json').exists() else {}
    if manifest.get('mvp'):
        attempt = next(a for a in manifest['mvp']['fresh_attempts'] if a['source_entry']['run_id'] == entry['run_id'])
        result.update(fresh_attempt_id=attempt['fresh_attempt_id'], source_entry=attempt['source_entry'])
    result.update(reporting.result_status(result,entry))
    atomic_json(directory/'episode_result.json',result)
    return result


def generate(batch,run_id):
    from .runtime import BigBudgetRun
    from .transport import CodingPlanTransport
    from . import provider
    from modelbench.minimal_value_20260905 import runner
    from modelbench.minimal_value_20260905.budget import EpisodeBudget
    batch=Path(batch).resolve();m,snapshot=verify(batch)
    freeze.assert_import_origins(snapshot)
    entry=next((entry for entry in dispatch_schedule(m) if entry['run_id']==run_id),None)
    if entry is None:raise ValueError('Run is outside this batch dispatch subset')
    _mvp_ready(m)
    directory=batch/'results'/run_id
    if directory.exists():
        raise ValueError('Existing generation attempt; no implicit rerun')
    receipt=read(batch/'admissions'/(run_id+'.json'))
    if receipt['manifest_sha256']!=sha(batch/'manifest.json') or receipt['status']!='admitted':
        raise ValueError('No bound episode admission')
    groupdir=batch/'group';group=read(groupdir/'group.json');policy=read(groupdir/'policy.json')
    provider.install(groupdir,group,policy,runner_module=runner,run_class=BigBudgetRun,
                     transport_class=CodingPlanTransport)
    runner.CONTAINER_SLOTS=threading.BoundedSemaphore(1+entry['expected_workers'])
    limits=entry['limits_override']
    q={k:limits[k] for k in ['max_calls','token_limit','cm_call_allowance']}
    budget=EpisodeBudget(**q,deadline_seconds=limits['wall_seconds'],scopes={'solver':q})
    started=budget.root.created_at
    component=None; result={}
    try:
        component=BigBudgetRun(batch,entry,transport_factory=CodingPlanTransport,
            budget=budget.scope('solver'),start_clock=started,deadline=budget.deadline_at,
            grade_enabled=False)
        budget.path=directory/'episode-budget.json';budget._persist()
        result=component.run()
        budget.freeze()
        result.update(budget=budget.summary(),root_budget_snapshot=budget.snapshot(),grading_pending=True)
    except Exception as exc:
        if component is not None:
            component.cancel.set()
        result['infrastructure_error']={'type':type(exc).__name__,'message':str(exc)}
        cleaned=cleanup_owned_episode(directory)
        result.update(cleanup_confirmed=cleaned['confirmed'],controller_cleanup=cleaned,
                      budget=budget.summary(),root_budget_snapshot=budget.snapshot())
    result['episode_wall_seconds']=time.monotonic()-started
    result=save_result(batch,entry,result)
    atomic_json(directory/'generation_result.json',result)
    return {'run_id':run_id,'generation_finished':True,'infrastructure_error':result.get('infrastructure_error')}


def grade(batch,run_id):
    from modelbench.minimal_value_20260905.environment import rootgrade_terminal
    batch=Path(batch).resolve();m,snapshot=verify(batch)
    freeze.assert_import_origins(snapshot)
    entry=next((entry for entry in dispatch_schedule(m) if entry['run_id']==run_id),None)
    if entry is None:raise ValueError('Run is outside this batch dispatch subset')
    directory=batch/'results'/run_id
    result=read(directory/'generation_result.json')
    def grader(*args,**kwargs):
        return rootgrade_terminal(*args,**kwargs,memory='1g',cpus=2)
    started=time.monotonic()
    try:
        score,attempts=grade_frozen(result,entry,directory,grader=grader)
        result.update(score=score,grading_attempts=attempts,grading_pending=False)
        if not score.get('completed') and not (score.get('failure_kind')=='candidate_empty_patch' and score.get('resolved') is False):
            result['grading_error']={'type':'OfficialScoringIncomplete','message':'Official grading failed after bounded attempts'}
    except Exception as exc:
        result.update(grading_pending=False,grading_error={'type':type(exc).__name__,'message':str(exc)})
        cleaned=cleanup_owned_episode(directory)
        result['grading_cleanup']=cleaned
        if not cleaned['confirmed']:
            result['infrastructure_error']={'type':'GradingCleanupError','message':'Unconfirmed grader cleanup'}
    result['grading_wall_seconds']=time.monotonic()-started
    result=save_result(batch,entry,result)
    return {'run_id':run_id,'official_resolved':result['official_resolved'],
            'grading_error':result.get('grading_error')}


def child_env(snapshot):
    env=os.environ.copy()
    env['PYTHONPATH']=os.pathsep.join([str(snapshot),str(snapshot/'dpswarm-plugin')])
    env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONIOENCODING']='utf-8'
    return env


def start_child(batch,snapshot,command,run_id):
    logs=batch/'logs';logs.mkdir(exist_ok=True)
    argv=[sys.executable,'-u','-B','-m','modelbench.big_budget_team_20260906.execution',
          command,'--batch',str(batch),'--run-id',run_id]
    with (logs/(run_id+'.'+command+'.out.log')).open('xb') as out, (
          logs/(run_id+'.'+command+'.err.log')).open('xb') as err:
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        process=subprocess.Popen(argv,cwd=snapshot,env=child_env(snapshot),stdout=out,stderr=err,**options)
    atomic_json(logs/(run_id+'.'+command+'.process.json'),{'pid':process.pid,'argv':argv,'at':utc()})
    return process


def next_wave(schedule,index):
    if index>=len(schedule):return []
    block=schedule[index]['block_id']
    result=[]
    for e in schedule[index:]:
        if e['block_id']!=block:break
        result.append(e)
    return result


def summarize(batch,state):
    results=[]
    for path in (batch/'results').glob('*/episode_result.json'):
        value=read(path)
        a=value.get('accounting') or {}
        results.append({'run_id':value['run_id'],'condition_id':value['condition_id'],
            'instance_id':value['instance_id'],'resolved':value.get('official_resolved'),
            'grading_pending':value.get('grading_pending'),
            'generation_status':value.get('status'),'calls':a.get('call_count'),
            'known_tokens':a.get('total_tokens_known_subtotal'),
            'known_cost_usd':a.get('api_equivalent_known_subtotal_usd'),
            'usage_unknown_calls':a.get('usage_unknown_calls'),
            'infrastructure_error':value.get('infrastructure_error'),
            'grading_error':value.get('grading_error')})
    state['results']=results
    state['generated']=len(results)
    state['scored']=sum(r['resolved'] is not None for r in results)
    state['resolved']=sum(r['resolved'] is True for r in results)
    state['updated_at']=utc()
    atomic_json(batch/'state.json',state)



def _record_stop(state, reason, evidence=None):
    """Keep the initiating cause; subsequent cleanup symptoms are secondary."""
    if not state.get('stop_reason'):
        state['stop_reason']=reason;state['stop_detected_at']=utc()
        state['stop_monotonic']=time.monotonic()
        if evidence is not None:state['stop_evidence']=evidence
    elif reason!=state['stop_reason'] and reason not in state.setdefault('secondary_stop_reasons',[]):
        state['secondary_stop_reasons'].append(reason)


def _stop_request(batch):
    group=Path(batch)/'group'
    trip=group/'TRIP.json'
    if trip.is_file():
        record=read(trip)
        return {'reason':'resource_stop:'+str(record.get('reason','unknown')),
                'hard':True,'path':str(trip),'sha256':sha(trip)}
    if resources.stopped(group):
        providers=[]
        for path in group.glob('provider-trip-*.json'):
            value=read(path);providers.append((str(value.get('at','')),path,value))
        if providers:
            _,path,value=min(providers,key=lambda item:item[0])
            return {'reason':'provider_stop:'+str(value.get('signal') or value.get('reason','unknown')), 'hard':False,
                    'path':str(path),'sha256':sha(path),'signal':value.get('signal'),'provider_reason':value.get('reason')}
        return {'reason':'group_stopped','hard':False}
    if (Path(batch)/'STOP').exists():return {'reason':'operator_stop','hard':False}
    return None


def _signal_drain(batch,state,request):
    _record_stop(state,request['reason'],request)
    group=Path(batch)/'group';(group/'CANCEL').touch(exist_ok=True)
    path=group/'controller-stop.json'
    if not path.exists():atomic_json(path,{'at':utc(),'reason':state['stop_reason'],
        'policy':'stop new calls and allow bounded in-flight drain before hard termination'})


def _quota_wave(batch,state,manifest,candidates,deadline):
    """Only call before a subwave: no paid admission exists while quota is waiting."""
    if not (manifest.get('coding_quota_gate') or {}).get('enabled'):return candidates
    from .coding_quota import inspect_quota, assess_wave
    key=freeze.credential_environment(REPO).get('GLM_API_KEY')
    poll=(manifest.get('coding_quota_gate') or {}).get('max_poll_sleep_seconds',60)
    while True:
        request=_stop_request(batch)
        if request:_signal_drain(batch,state,request);return []
        if time.monotonic()>=deadline:_record_stop(state,'dispatch_deadline');return []
        snapshot=inspect_quota(key,timeout_seconds=20)
        decisions=[];best=[]
        for index,entry in enumerate(candidates):
            decision=(assess_wave(snapshot,candidates[:index+1]) if snapshot.get('available') is True else
                {'status':'unavailable','reason':snapshot.get('reason','quota_query_unavailable'),
                 'required_credits':None,'next_reset_ms':None})
            decisions.append(decision)
            if decision['status']=='ready':best=candidates[:index+1]
            else:break
        if not decisions:raise ValueError('Quota admission requires a nonempty schedule prefix')
        decision=decisions[len(best)-1] if best else decisions[0]
        state['quota_check_count']=state.get('quota_check_count',0)+1
        proof={'at':utc(),'snapshot':snapshot,'decision':decision,
               'dispatch_prefix':[entry['run_id'] for entry in best],
               'first_pending_run_id':candidates[0]['run_id']}
        path=Path(batch)/'quota'/('check-%05d.json'%state['quota_check_count'])
        if path.exists():raise ValueError('Quota evidence identity collision')
        atomic_json(path,proof);state['quota_evidence']={'path':str(path),'sha256':sha(path)}
        state['quota_decision']=decision
        if best:
            state['status']='running';state.pop('quota_next_reset_ms',None)
            return best
        if decision['status']!='wait' or not isinstance(decision.get('next_reset_ms'),(int,float)):
            _record_stop(state,'coding_quota_unavailable',{'reason':decision.get('reason'),'path':str(path)})
            return []
        reset=decision['next_reset_ms']/1000
        state['status']='waiting_for_coding_quota';state['quota_next_reset_ms']=decision['next_reset_ms']
        summarize(batch,state)
        # Do not repeatedly hit the quota endpoint while the reported window cannot reset.
        while time.time()<reset:
            request=_stop_request(batch)
            if request:_signal_drain(batch,state,request);return []
            remaining=deadline-time.monotonic()
            if remaining<=0:_record_stop(state,'dispatch_deadline');return []
            time.sleep(min(poll,remaining,max(.001,reset-time.time())))


def run(batch):
    batch=Path(batch).resolve();m,snapshot=verify(batch)
    freeze.assert_import_origins(snapshot)
    if (batch/'state.json').exists():raise ValueError('Existing controller state; reconcile before resume')
    _mvp_ready(m)
    schedule=dispatch_schedule(m)
    groupdir=batch/'group'
    if resources.stopped(groupdir):raise ValueError('Group has a stop marker')
    state={'status':'running','pid':os.getpid(),'started_at':utc(),'admitted':0,'token_admission':0,
           'active':[],'waves_completed':0,'stop_reason':None,'total_planned':len(schedule),
           'full_plan_total':len(m['schedule']),'transport_policy':m.get('transport_policy')}
    if m.get('continuation'):
        prior=m['continuation']
        state['prior_attempt_totals']=prior['prior_totals']
        if prior.get('lineage'):
            state['prior_batch_strata']=prior['lineage']
        else:
            state['prior_batch_stratum']={'batch':prior['source_batch'],
                'manifest_sha256':prior['source_manifest_sha256'],
                'transport_policy':prior['source_transport_policy'],**prior['prior_totals']}
            state['prior_batch_strata']=[state['prior_batch_stratum']]
        state['comparison_policy']=m['result_comparison_policy']
    start=time.monotonic();deadline=start+m['dispatch_seconds'];index=0;active={}
    hard_deadline=deadline+m.get('final_drain_seconds',48*3600)
    grader_process=None;grader_rid=None
    monitor=resources.ResourceMonitor(groupdir,[batch/'results'],interval_seconds=10,
        owned_memory_limit_bytes=12*resources.GIB,host_available_min_bytes=2*resources.GIB,
        docker_remaining_min_bytes=2*resources.GIB,candidate_cap=12,grader_cap=2,
        max_sample_failures=3,container_memory_limit_bytes=resources.GIB)
    with stage_lease(m['resource_lock_path'],batch) as lease:
        monitor.start()
        try:
            summarize(batch,state)
            while index<len(schedule) and not state['stop_reason']:
                if time.monotonic()>=deadline:
                    _record_stop(state,'dispatch_deadline');break
                wave=_quota_wave(batch,state,m,next_wave(schedule,index),deadline)
                if not wave:break
                pending=list(wave)
                wave_cleanup_confirmed=True
                state['phase']=wave[0]['phase'];state['current_block']=wave[0]['block_id']
                while pending or active:
                    if time.monotonic()>=hard_deadline:raise RuntimeError('Overall finite drain deadline exceeded')
                    request=_stop_request(batch)
                    if request:_signal_drain(batch,state,request)
                    while pending and not state['stop_reason']:
                        request=_stop_request(batch)
                        if request:_signal_drain(batch,state,request);break
                        entry=pending[0];needed=1+entry['expected_workers']
                        used=sum(x['slots'] for x in active.values())
                        if len(active)>=12 or used+needed>12:break
                        if time.monotonic()>=deadline:
                            _record_stop(state,'dispatch_deadline');break
                        cost=entry['limits_override']['token_limit']
                        if state['admitted']>=m.get('generation_attempt_limit',len(schedule)) or state['token_admission']+cost>m['token_admission_sum_limit']:
                            raise ValueError('Finite admission contract exceeded')
                        receipt={'run_id':entry['run_id'],'at':utc(),'status':'admitted',
                            'manifest_sha256':sha(batch/'manifest.json'),'candidate_slots':needed,'token_limit':cost}
                        path=batch/'admissions'/(entry['run_id']+'.json')
                        if path.exists():raise ValueError('Duplicate paid attempt admission')
                        atomic_json(path,receipt)
                        state['admitted']+=1;state['token_admission']+=cost
                        summarize(batch,state)
                        process=start_child(batch,snapshot,'generate',entry['run_id'])
                        active[entry['run_id']]={'process':process,'slots':needed,'started':time.monotonic(),'entry':entry}
                        pending.pop(0)
                    for rid,item in list(active.items()):
                        process=item['process'];expired=time.monotonic()-item['started']>m['generation_watchdog_seconds']
                        request=_stop_request(batch)
                        if request:_signal_drain(batch,state,request)
                        grace_expired=bool(state['stop_reason'] and time.monotonic()>=
                            state.get('stop_monotonic',time.monotonic())+m.get('cooperative_stop_grace_seconds',1080))
                        hard_resource=bool(request and request.get('hard'))
                        if expired or hard_resource or grace_expired:
                            reason='generation_watchdog' if expired else 'resource_safety_termination' if hard_resource else 'cooperative_stop_grace_expired'
                            _record_stop(state,reason)
                            if process.poll() is None:
                                killed=kill_owned_process_tree(process)
                                atomic_json(batch/'logs'/(rid+'.termination.json'),{**killed,'reason':reason,
                                    'initiating_stop_reason':state['stop_reason']})
                                if not killed['confirmed'] or process.poll() is None:
                                    lease['retain']=True
                                    raise RuntimeError('Generation process termination unconfirmed; no re-admission or grading')
                        code=process.poll()
                        if code is None:continue
                        directory=batch/'results'/rid
                        cleaned=cleanup_owned_episode(directory)
                        atomic_json(batch/'logs'/(rid+'.cleanup.json'),cleaned)
                        if not cleaned['confirmed']:
                            _record_stop(state,'cleanup_unconfirmed');lease['retain']=True;wave_cleanup_confirmed=False
                        resultpath=directory/'generation_result.json'
                        if code!=0 or not resultpath.exists():
                            _record_stop(state,'generation_process_failed')
                            atomic_json(batch/'logs'/(rid+'.failure.json'),{'returncode':code,'result_present':resultpath.exists()})
                        else:
                            completed=read(resultpath)
                            if completed.get('infrastructure_error'):
                                _record_stop(state,'generation_infrastructure_error')
                            elif (completed.get('budget') or {}).get('unknown_call_count') or (completed.get('budget') or {}).get('pending_call_count'):
                                _record_stop(state,'generation_usage_unreconciled')
                            elif (completed.get('accounting') or {}).get('protocol_issues'):
                                _record_stop(state,'transport_identity_or_retry_unreconciled')
                        del active[rid]
                    state['active']=[{'run_id':rid,'pid':v['process'].pid,'slots':v['slots']} for rid,v in active.items()]
                    summarize(batch,state)
                    if not active and state['stop_reason']:break
                    time.sleep(1)
                # Current controller cleanup, not a stale child flag, guards grading.
                if not wave_cleanup_confirmed or lease['retain']:
                    break
                # No generation overlaps official grading; same frozen patch only.
                for entry in wave:
                    if time.monotonic()>=hard_deadline:raise RuntimeError('Overall finite drain deadline exceeded')
                    rid=entry['run_id'];gp=batch/'results'/rid/'generation_result.json'
                    if not gp.exists():continue
                    result=read(gp)
                    if result.get('infrastructure_error') or not result.get('cleanup_confirmed'):continue
                    state['grading_run_id']=rid;summarize(batch,state)
                    process=start_child(batch,snapshot,'grade',rid)
                    grader_process,grader_rid=process,rid
                    try:process.wait(timeout=min(m['grading_watchdog_seconds'],max(.001,hard_deadline-time.monotonic())))
                    except subprocess.TimeoutExpired:
                        killed=kill_owned_process_tree(process);cleaned=cleanup_owned_episode(gp.parent)
                        atomic_json(batch/'logs'/(rid+'.grading-timeout.json'),{'process':killed,'containers':cleaned})
                        _record_stop(state,'grading_watchdog')
                        if not killed['confirmed'] or not cleaned['confirmed']:lease['retain']=True
                        break
                    if process.returncode!=0:
                        cleaned=cleanup_owned_episode(gp.parent)
                        atomic_json(batch/'logs'/(rid+'.grading-failure-cleanup.json'),cleaned)
                        if not cleaned['confirmed']:lease['retain']=True
                        grader_process=None;grader_rid=None
                        _record_stop(state,'grading_process_failed');break
                    cleaned=cleanup_owned_episode(gp.parent)
                    if not cleaned['confirmed']:
                        lease['retain']=True;_record_stop(state,'grading_cleanup_unconfirmed');break
                    grader_process=None;grader_rid=None
                    finished=read(gp.parent/'episode_result.json')
                    if finished.get('grading_error') or finished.get('infrastructure_error'):
                        _record_stop(state,'grading_or_evidence_error');break
                    summarize(batch,state)
                state.pop('grading_run_id',None)
                index+=len(wave);state['waves_completed']+=1;summarize(batch,state)
            state['status']='completed' if state['scored']==len(schedule) and not state['stop_reason'] else 'stopped_incomplete'
            summarize(batch,state)
        except BaseException as exc:
            _record_stop(state,type(exc).__name__+': '+str(exc));state['status']='controller_failed'
            state['controller_error']={'type':type(exc).__name__,'message':str(exc)}
            if grader_process is not None:
                killed=kill_owned_process_tree(grader_process) if grader_process.poll() is None else {'confirmed':True}
                cleaned=cleanup_owned_episode(batch/'results'/grader_rid)
                atomic_json(batch/'logs'/(grader_rid+'.grader-exception-cleanup.json'),{'process':killed,'containers':cleaned})
                if not killed['confirmed'] or not cleaned['confirmed']:lease['retain']=True
            for rid,item in list(active.items()):
                killed=kill_owned_process_tree(item['process']);cleaned=cleanup_owned_episode(batch/'results'/rid)
                atomic_json(batch/'logs'/(rid+'.exception-cleanup.json'),{'process':killed,'containers':cleaned})
                if not killed['confirmed'] or not cleaned['confirmed']:lease['retain']=True
                else:active.pop(rid,None)
            state['active']=[{'run_id':rid,'pid':item['process'].pid,'slots':item['slots']} for rid,item in active.items()]
            summarize(batch,state)
            raise
        finally:
            monitor.stop()
    return {k:state.get(k) for k in ['status','admitted','generated','scored','resolved','stop_reason']}


def launch(batch):
    batch=Path(batch).resolve();m,snapshot=verify(batch)
    if (batch/'launch.json').exists() or (batch/'state.json').exists():
        raise ValueError('Existing launch; no implicit duplicate controller')
    _mvp_ready(m)
    env=child_env(snapshot)
    credentials=freeze.credential_environment(REPO)
    for key in ('GLM_API_KEY','GLM_BASE_URL','DEEPSEEK_API_KEY','DEEPSEEK_BASE_URL'):
        if credentials.get(key):env[key]=credentials[key]
    argv=[sys.executable,'-u','-B','-m','modelbench.big_budget_team_20260906.execution','run','--batch',str(batch)]
    with (batch/'controller.stdout.log').open('xb') as out,(batch/'controller.stderr.log').open('xb') as err:
        options={'creationflags':subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS} if os.name=='nt' else {'start_new_session':True}
        process=subprocess.Popen(argv,cwd=snapshot,env=env,stdout=out,stderr=err,stdin=subprocess.DEVNULL,**options)
    descriptor={'pid':process.pid,'argv':argv,'at':utc(),'snapshot':str(snapshot)}
    atomic_json(batch/'launch.json',descriptor)
    return descriptor


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['prepare','launch','run','generate','grade','status'])
    parser.add_argument('--batch',required=True);parser.add_argument('--run-id')
    parser.add_argument('--continue-from');parser.add_argument('--mvp-authorization')
    parser.add_argument('--offline-validation');parser.add_argument('--offline-tests')
    parser.add_argument('--transport-probes');parser.add_argument('--source-qualification')
    args=parser.parse_args()
    if args.command!='prepare' and any(getattr(args,name) is not None for name in (
            'continue_from','mvp_authorization','offline_validation','offline_tests','transport_probes','source_qualification')):
        parser.error('Continuation and qualification input flags apply only to prepare; launch/run use the frozen manifest')
    if args.command=='prepare':
        result=prepare(args.batch,continue_from=args.continue_from,offline_validation=args.offline_validation,
            offline_tests=args.offline_tests,transport_probes=args.transport_probes,
            source_qualification=args.source_qualification,mvp_authorization=args.mvp_authorization)
    elif args.command=='status':result=read(Path(args.batch)/'state.json')
    elif args.command in ('generate','grade'):result=globals()[args.command](args.batch,args.run_id)
    else:result=globals()[args.command](args.batch)
    print(json.dumps(result,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
