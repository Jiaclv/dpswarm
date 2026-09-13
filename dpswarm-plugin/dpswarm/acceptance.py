"""Authoritative native DPH acceptance, projected from the root EventStore."""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from .plugin_audit import _strict_json

SCHEMA = 'dpswarm-acceptance-v1'
MAX_FILES, MAX_BYTES = 64, 3 * 1024 * 1024
ID = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')

def fail(code, message):
    from .control import ControlPlaneError
    raise ControlPlaneError(code, message)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def identifier(value, name='id'):
    if not isinstance(value, str) or not ID.fullmatch(value):
        fail('ACCEPTANCE_INVALID', f'Invalid {name}')
    return value

def text(value, name, limit=40000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        fail('ACCEPTANCE_INVALID', f'Invalid {name}')
    return value

def bounded_list(value, name, limit=256):
    if not isinstance(value, list) or len(value) > limit:
        fail('ACCEPTANCE_INVALID', f'Invalid or oversized {name}')
    return value

def path_key(value):
    text(value, 'relative path', 512)
    if ('\\' in value or value.startswith('/') or ':' in value or any(ord(c) < 32 for c in value)):
        fail('CANDIDATE_PATH_INVALID', 'Paths must be canonical relative POSIX paths')
    for part in value.split('/'):
        if (part in ('', '.', '..') or part.endswith((' ', '.')) or any(c in part for c in '<>"|?*')
                or re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', part)):
            fail('CANDIDATE_PATH_INVALID', f'Unsafe path: {value}')
    return value.casefold()

REVIEW_V1 = 'dpswarm-review-v1'
REVIEW_V2 = 'dpswarm-review-v2'
V2_DISPOSITIONS = ('pending', 'fix_required', 'verified_resolved', 'retained_suggestion',
                   'not_a_defect', 'not_applicable')
V2_EXECUTION_STATUS = ('completed', 'failed', 'unavailable', 'not_run')
V2_PASS_DISPOSITIONS = ('verified_resolved', 'retained_suggestion', 'not_a_defect', 'not_applicable')

def parse_report(raw):
    found = []
    for schema in (REVIEW_V2, REVIEW_V1):
        markers = re.findall(rf'(?m)^```{schema}\s*$', raw)
        blocks = re.findall(rf'(?ms)^```{schema}[^\S\r\n]*\r?\n(.*?)^```[^\S\r\n]*$', raw)
        if markers or blocks:
            found.append((schema, markers, blocks))
    if len(found) != 1 or len(found[0][1]) != 1 or len(found[0][2]) != 1:
        fail('REVIEW_REPORT_INVALID', 'Exactly one complete review JSON fence of one schema version is required')
    schema, _, blocks = found[0]
    try:
        record = _strict_json(blocks[0])
    except (ValueError, TypeError, RecursionError) as exc:
        fail('REVIEW_REPORT_INVALID', f'Invalid review JSON: {exc}')
    if not isinstance(record, dict) or record.get('schema') != schema:
        fail('REVIEW_REPORT_INVALID', f'Review schema must be {schema}')
    if record.get('verdict') not in ('pass', 'needs-rework', 'blocked'):
        fail('REVIEW_REPORT_INVALID', 'Invalid verdict')
    v2 = schema == REVIEW_V2
    allowed = {'schema', 'verdict', 'requirements', 'findings', 'unavailable_checks',
               'candidate_id', 'manifest_digest', 'requirement_revision', 'evidence_revision', 'consumed_manifest_refs',
               *(('verification_plan', 'check_results') if v2 else ())}
    if set(record) - allowed:
        fail('REVIEW_REPORT_INVALID', 'Unsupported structured review fields')
    outside = re.sub(r'(?ms)^```dpswarm-review-v[12][^\S\r\n]*\r?\n(.*?)^```[^\S\r\n]*$', '', raw)
    for other in re.findall(r'(?ms)^```(?:json)?[^\S\r\n]*\r?\n(.*?)^```[^\S\r\n]*$', outside):
        try:
            value = _strict_json(other)
        except ValueError:
            continue
        if isinstance(value, dict) and any(k in value for k in ('schema', 'verdict', 'requirements', 'findings')):
            fail('REVIEW_REPORT_INVALID', 'Another structured review appears outside the unique report block')
    def strings(values, name):
        for value in bounded_list(values, name):
            text(value, name + ' entry', 4000)
    reported_requirements = set()
    for row in bounded_list(record.get('requirements'), 'requirements'):
        if not isinstance(row, dict):
            fail('REVIEW_REPORT_INVALID', 'Invalid requirement result')
        rid = identifier(row.get('id'), 'requirement id')
        if rid in reported_requirements or row.get('result') not in ('met', 'failed', 'unknown'):
            fail('REVIEW_REPORT_INVALID', 'Duplicate requirement or invalid result')
        reported_requirements.add(rid)
        if set(row) - {'id', 'result', 'evidence', 'reason',
                       *(('verification_plan_revision', 'check_refs') if v2 else ())}:
            fail('REVIEW_REPORT_INVALID', 'Unsupported requirement result fields')
        strings(row.get('evidence'), 'requirement evidence')
        if row.get('reason') is not None and not isinstance(row['reason'], str):
            fail('REVIEW_REPORT_INVALID', 'Requirement reason must be text')
    seen = set()
    for row in bounded_list(record.get('findings'), 'findings'):
        if not isinstance(row, dict):
            fail('REVIEW_REPORT_INVALID', 'Invalid finding')
        fid = identifier(row.get('id'), 'finding id')
        if fid in seen or row.get('classification') not in ('defect', 'suggestion', 'unknown'):
            fail('REVIEW_REPORT_INVALID', 'Duplicate finding or invalid classification')
        seen.add(fid)
        if row.get('state') not in ('open', 'fix-claimed', 'verified-resolved', 'not-a-defect', 'not-applicable'):
            fail('REVIEW_REPORT_INVALID', 'Invalid finding state')
        text(row.get('observation'), 'finding observation')
        if set(row) - {'id', 'observation', 'requirement_ids', 'paths', 'classification', 'state', 'evidence', 'reason',
                       *(('disposition', 'disposition_reason') if v2 else ())}:
            fail('REVIEW_REPORT_INVALID', 'Unsupported finding fields')
        for key in ('requirement_ids', 'paths', 'evidence'):
            strings(row.get(key), 'finding ' + key)
        if row.get('reason') is not None and not isinstance(row['reason'], str):
            fail('REVIEW_REPORT_INVALID', 'Finding reason must be text')
        if v2:
            if row.get('disposition') not in V2_DISPOSITIONS:
                fail('REVIEW_REPORT_INVALID', 'v2 findings need an explicit disposition')
            if row.get('disposition_reason') is not None and not isinstance(row['disposition_reason'], str):
                fail('REVIEW_REPORT_INVALID', 'Finding disposition reason must be text')
            if row.get('disposition') == 'retained_suggestion':
                text(row.get('disposition_reason'), 'retained suggestion reason')
    strings(record.get('unavailable_checks', []), 'unavailable checks')
    if v2:
        _validate_v2_plan(record, reported_requirements)
    return record

def _validate_v2_plan(record, requirement_ids):
    """Structural v2 validation: the plan, its checks and every check result."""
    plan = record.get('verification_plan')
    if not isinstance(plan, dict):
        fail('REVIEW_REPORT_INVALID', 'v2 reports require a verification plan')
    if set(plan) - {'revision', 'checks', 'coverage'}:
        fail('REVIEW_REPORT_INVALID', 'Unsupported verification plan fields')
    if type(plan.get('revision')) is not int or plan['revision'] < 1:
        fail('REVIEW_REPORT_INVALID', 'Verification plan revision must be a positive integer')
    text(plan.get('coverage'), 'verification plan coverage', 20000)
    plan_ids = set()
    for row in bounded_list(plan.get('checks'), 'verification plan checks'):
        if not isinstance(row, dict):
            fail('REVIEW_REPORT_INVALID', 'Invalid verification check')
        cid = identifier(row.get('id'), 'check id')
        if cid in plan_ids:
            fail('REVIEW_REPORT_INVALID', 'Duplicate verification check id')
        plan_ids.add(cid)
        if set(row) - {'id', 'requirement_ids', 'method', 'required_for_claim', 'rationale'}:
            fail('REVIEW_REPORT_INVALID', 'Unsupported verification check fields')
        text(row.get('method'), 'verification method')
        if type(row.get('required_for_claim')) is not bool:
            fail('REVIEW_REPORT_INVALID', 'required_for_claim must be boolean')
        for ref in bounded_list(row.get('requirement_ids'), 'check requirement ids'):
            if ref not in requirement_ids:
                fail('REVIEW_REPORT_INVALID', f'Check {cid} refers to an unreported requirement')
        if row.get('rationale') is not None and not isinstance(row['rationale'], str):
            fail('REVIEW_REPORT_INVALID', 'Check rationale must be text')
    for row in record['requirements']:
        if row.get('verification_plan_revision') != plan['revision']:
            fail('VERIFICATION_PLAN_STALE', 'Requirement decisions must bind the report plan revision')
        for ref in bounded_list(row.get('check_refs'), 'requirement check refs'):
            if ref not in plan_ids:
                fail('REVIEW_REPORT_INVALID', f'Requirement {row["id"]} cites unknown check {ref}')
    seen_results = set()
    for row in bounded_list(record.get('check_results'), 'check results'):
        if not isinstance(row, dict):
            fail('REVIEW_REPORT_INVALID', 'Invalid check result')
        if set(row) - {'check_id', 'method', 'execution_status', 'evidence_refs', 'limitations',
                       'environment_ref', 'candidate_id', 'manifest_digest', 'requirement_revision',
                       'verification_plan_revision'}:
            fail('REVIEW_REPORT_INVALID', 'Unsupported check result fields')
        if row['check_id'] in seen_results:
            fail('REVIEW_REPORT_INVALID', 'Duplicate check result')
        seen_results.add(row['check_id'])
        if row['check_id'] not in plan_ids:
            fail('VERIFICATION_CHECK_UNKNOWN', f'Check result {row["check_id"]} is not in the verification plan')
        text(row.get('method'), 'check result method')
        if row.get('execution_status') not in V2_EXECUTION_STATUS:
            fail('REVIEW_REPORT_INVALID', 'Invalid check execution status')

def verify_format_repair(previous_record, record):
    """P2b/R08: a format-only continuation may normalize structure or add
    runtime bindings; semantic fields must stay identical to the readable
    prior report. Changing any of them requires the substantive path."""
    if not isinstance(previous_record, dict):
        fail('REPAIR_SEMANTIC_BASE_MISSING',
             'A format repair requires a readable prior report; use the substantive path when the old report did not parse')
    if record.get('verdict') != previous_record.get('verdict'):
        fail('REPAIR_SEMANTIC_DRIFT', 'A format repair cannot change the verdict; use a substantive review')
    prior_results = {row.get('id'): row.get('result') for row in previous_record.get('requirements', []) if isinstance(row, dict)}
    for row in record.get('requirements', []):
        if prior_results.get(row.get('id')) != row.get('result'):
            fail('REPAIR_SEMANTIC_DRIFT', f'Requirement {row.get("id")} changed its result in a format repair; use a substantive review')
    prior_findings = {row.get('id'): row for row in previous_record.get('findings', []) if isinstance(row, dict)}
    for row in record.get('findings', []):
        prior = prior_findings.get(row.get('id'))
        if not prior:
            continue
        for key in ('observation', 'classification'):
            if row.get(key) != prior.get(key):
                fail('REPAIR_SEMANTIC_DRIFT', f'Finding {row.get("id")} changed {key} in a format repair; use a substantive review')
        if row.get('disposition') is not None and prior.get('disposition') is not None and row.get('disposition') != prior.get('disposition'):
            fail('REPAIR_SEMANTIC_DRIFT', f'Finding {row.get("id")} changed its disposition in a format repair; use a substantive review')

def verify_v2_semantics(contract, record, candidate):
    """Authoritative v2 gate: claims without completed checks cannot pass, and
    suggestions need an explicit current disposition (plan P1/P2a, F1/F2)."""
    plan = record['verification_plan']
    results = {row['check_id']: row for row in record['check_results']}
    mandatory = {row['id'] for row in contract['requirements'] if row.get('mandatory')}
    for row in record['check_results']:
        if row.get('verification_plan_revision') != plan['revision']:
            fail('VERIFICATION_PLAN_STALE', f'Check result {row["check_id"]} binds a different plan revision')
        expected = {'candidate_id': candidate['candidate_id'], 'manifest_digest': candidate['manifest_digest'],
                    'requirement_revision': candidate['requirement_revision']}
        for key, value in expected.items():
            if row.get(key) != value:
                fail('VERIFICATION_CHECK_CANDIDATE_MISMATCH', f'Check result {row["check_id"]} {key} differs from the sealed candidate')
    # R01: a met claim needs at least one completed check execution behind it.
    for row in record['requirements']:
        if row['result'] != 'met':
            continue
        if row['id'] in mandatory or row.get('check_refs'):
            completed = [ref for ref in row.get('check_refs', []) if results.get(ref, {}).get('execution_status') == 'completed']
            if not completed:
                fail('VERIFICATION_CHECK_INCOMPLETE', f'Requirement {row["id"]} claims met without a completed verification check')
    # R05/R06: a passing verdict requires explicit, final dispositions; a real
    # defect cannot be laundered into a retained suggestion.
    if record['verdict'] == 'pass':
        for row in record['findings']:
            if row['disposition'] not in V2_PASS_DISPOSITIONS:
                fail('FINDING_DISPOSITION_REQUIRED', f'Finding {row["id"]} needs a final disposition before a pass')
            if row['classification'] == 'defect' and row['disposition'] == 'retained_suggestion':
                fail('FINDING_DISPOSITION_INVALID', f'Defect {row["id"]} cannot be retained as a suggestion')
            if row['classification'] == 'suggestion' and row['disposition'] == 'retained_suggestion' and not (row.get('evidence') or row.get('reason') or row.get('disposition_reason')):
                fail('FINDING_DISPOSITION_INVALID', f'Suggestion {row["id"]} needs support for its retention')

def contract_for_item(proj, item_id):
    for contract in proj.acceptance_contracts.values():
        if item_id in contract['enrolled_item_ids']:
            return contract
    return None

def validate_acceptance(proj, payload):
    """Pure common guard also applied to direct work_item_accepted events."""
    contract = contract_for_item(proj, payload['item_id'])
    if contract is None:
        return None
    auth = (payload.get('accepted_by') or {}).get('acceptance') or {}
    if auth.get('contract_id') != contract['contract_id']:
        fail('ACCEPTANCE_REQUIRED', 'Enrolled work items require their authoritative acceptance contract')
    if type(auth.get('expected_revision')) is not int or auth['expected_revision'] != contract['revision']:
        fail('ACCEPTANCE_REVISION_CONFLICT', 'Acceptance ledger changed; refresh current state')
    candidate = contract['candidates'].get(auth.get('candidate_id'))
    if not candidate or candidate['candidate_id'] != contract['current_candidate_id']:
        fail('CANDIDATE_STALE', 'Only the current candidate can be accepted')
    if payload['item_id'] not in candidate['candidate_item_ids']:
        fail('CANDIDATE_ITEM_MISMATCH', 'Work item is not in the candidate')
    if candidate['requirement_revision'] != contract['requirement_revision']:
        fail('REQUIREMENT_REVISION_STALE', 'Candidate precedes the current user amendment')
    if not candidate['snapshot']['dependency_complete'] or candidate['snapshot']['unknown_dependencies']:
        fail('CANDIDATE_DEPENDENCIES_UNKNOWN', 'Necessary dependencies are not fully sealed')
    review = contract['reviews'].get(auth.get('review_id'))
    if not review or review['review_id'] != contract.get('current_review_id'):
        fail('REVIEW_STALE', 'A current final review is required')
    if review['candidate_id'] != candidate['candidate_id'] or review['manifest_digest'] != candidate['manifest_digest']:
        fail('REVIEW_CANDIDATE_MISMATCH', 'Review and candidate differ')
    if review['evidence_revision'] != contract['evidence_revision']:
        fail('REVIEW_EVIDENCE_STALE', 'New external evidence requires a new final review')
    if review['reviewer_id'] != contract['reviewer_id']:
        fail('REVIEWER_MISMATCH', 'Review was produced by a superseded verifier')
    if review['record']['verdict'] != 'pass':
        fail('REVIEW_NOT_PASS', 'Final review is not a pass')
    for row in candidate['verification_roster']:
        evidence = contract['evidence'].get(candidate['roster_evidence'].get(row['id']))
        if not evidence or evidence.get('status') not in ('registered', 'missing'):
            fail('VERIFICATION_INCOMPLETE', f"Verification report not registered: {row['id']}")
        if evidence['status'] == 'registered' and evidence.get('parse_error'):
            fail('REVIEW_REPORT_INVALID', f"Malformed verification report: {row['id']}")
    results = {r['id']: r for r in review['record']['requirements']}
    for requirement in contract['requirements']:
        result = results.get(requirement['id'], {})
        if requirement['mandatory'] and (result.get('result') != 'met' or not (result.get('evidence') or str(result.get('reason', '')).strip())):
            fail('MANDATORY_REQUIREMENT_UNKNOWN', f"Necessary requirement lacks supported met result: {requirement['id']}")
    decisions = {f['id']: f for f in review['record']['findings']}
    active = {r['id'] for r in contract['requirements']}
    for fid, finding in contract['findings'].items():
        decision = decisions.get(fid)
        if not decision or decision['state'] not in ('verified-resolved', 'not-a-defect', 'not-applicable'):
            fail('FINDING_UNRESOLVED', f'Current review omits or leaves open retained finding {fid}')
        if not decision.get('reason') or not decision.get('evidence'):
            fail('FINDING_DECISION_UNSUPPORTED', f'Finding decision needs reason and evidence: {fid}')
        if decision['state'] == 'not-applicable':
            original_revision = next(r for r in contract['requirement_history'] if r['revision'] == finding['first_requirement_revision'])
            original = {r['id']: r for r in original_revision['requirements']}
            current = {r['id']: r for r in contract['requirements']}
            unchanged = any(rid in current and (rid not in original or current[rid]['description'] == original[rid]['description'])
                            for rid in finding['requirement_ids'])
            amendment_ref = 'user-message:' + contract['sources'][-1]['message_id']
            if (contract['requirement_revision'] <= finding['first_requirement_revision'] or unchanged
                    or amendment_ref not in decision['evidence']):
                fail('FINDING_STILL_APPLICABLE', 'Not-applicable requires changed or removed requirements and explicit user-message amendment evidence')
    if review['consumed_manifest_refs'] != candidate['snapshot']['consumed_manifest_refs']:
        fail('CONSUMED_VERSION_MISMATCH', 'Review must bind the consumed upstream manifests')
    for item_id, binding in candidate['submission_bindings'].items():
        item = proj.work_items.get(item_id)
        if not item or any(getattr(item, k) != v for k, v in binding.items()):
            fail('CANDIDATE_SUBMISSION_STALE', f'Implementation submission changed: {item_id}')
    return candidate

class AcceptanceService:
    def __init__(self, cp):
        self.cp = cp

    def read(self):
        with self.cp._lock:
            return {'ok': True, 'schema': SCHEMA, 'contracts': copy.deepcopy(list(self.cp.proj.acceptance_contracts.values()))}

    def _root_session(self):
        binding = self.cp.proj.nodes[self.cp.root_lead_node].execution_binding or {}
        if binding.get('execution_provider') != 'dsh-root':
            fail('ROOT_EXECUTION_REQUIRED', 'Bind the native DPH root before the acceptance contract')
        return binding['execution_session_id']

    def _source(self, source):
        if not isinstance(source, dict) or source.get('source') != 'dph-user-message':
            fail('USER_SOURCE_REQUIRED', 'The source must be a trusted native user message')
        if source.get('session_id') != self._root_session():
            fail('USER_SOURCE_SCOPE_MISMATCH', 'User message does not belong to this host root')
        identifier(source.get('message_id'), 'message id')
        content = source.get('content')
        if isinstance(content, str):
            raw = text(content, 'user request', 250000).encode('utf-8')
        elif isinstance(content, list) and content and all(isinstance(block, dict) for block in content):
            raw = canonical(content)
            if len(raw) > 250000:
                fail('ACCEPTANCE_INVALID', 'User content blocks exceed the source limit')
        else:
            fail('USER_SOURCE_REQUIRED', 'User content must be original text or native content blocks')
        sha = hashlib.sha256(raw).hexdigest()
        if source.get('content_hash') not in (None, sha):
            fail('USER_SOURCE_HASH_MISMATCH', 'User content and hash differ')
        return {**copy.deepcopy(source), 'content_hash': sha}

    def _requirements(self, requirements, sources):
        rows = bounded_list(requirements, 'requirements', 128)
        if not rows:
            fail('REQUIREMENTS_REQUIRED', 'At least one requirement is required')
        seen, result = set(), []
        for row in rows:
            if not isinstance(row, dict):
                fail('ACCEPTANCE_INVALID', 'Requirement must be an object')
            rid = identifier(row.get('id'), 'requirement id')
            refs = bounded_list(row.get('source_refs'), 'requirement source refs')
            if rid in seen or not refs or any(ref not in sources for ref in refs):
                fail('REQUIREMENT_SOURCE_INVALID', 'Requirements need unique ids and known user source refs')
            if type(row.get('mandatory')) is not bool:
                fail('ACCEPTANCE_INVALID', 'Requirement mandatory must be boolean')
            seen.add(rid)
            result.append({**copy.deepcopy(row), 'description': text(row.get('description'), 'requirement')})
        return result

    def _identity(self, payload, *, lead=False):
        node = self.cp.proj.nodes.get(payload.get('node_id'))
        item = self.cp.proj.work_items.get(payload.get('item_id'))
        if not node or not item or node.item_id != item.item_id or not node.execution_binding:
            fail('REPORT_IDENTITY_INVALID', 'A bound runtime item and execution node are required')
        if node.context_epoch != payload.get('context_epoch') or node.session_id != payload.get('session_id'):
            fail('FENCE_VIOLATION', 'Report runtime epoch/session does not match')
        if node.execution_binding.get('parent_session_id') != self._root_session():
            fail('REPORT_IDENTITY_INVALID', 'Report execution belongs to another host root')
        if lead and node.node_id != self.cp.root_lead_node:
            fail('REVIEWER_MISMATCH', 'Lead review requires the bound root execution')
        return node, item

    def _report(self, payload, *, lead=False):
        node, item = self._identity(payload, lead=lead)
        identity = {'item_id': item.item_id, 'node_id': node.node_id, 'session_id': node.session_id,
                    'context_epoch': node.context_epoch, 'execution_binding': copy.deepcopy(node.execution_binding)}
        if lead:
            raw = text(payload.get('report'), 'Lead review report', 500000)
            sha = self.cp.write_artifact(raw) or hashlib.sha256(raw.encode()).hexdigest()
            return raw, {**identity, 'package_id': None, 'report_hash': sha, 'report_ref': f'{sha}.txt',
                         **({'report': raw} if self.cp.store.path is None else {})}
        pkg_id = payload.get('package_id') or item.submission_package_id
        from . import invariants
        from .invariants import InvariantViolation
        try:
            invariants.bound_submission_package(self.cp.proj, item.item_id, pkg_id)
        except InvariantViolation as exc:
            fail(exc.code, str(exc))
        package = self.cp.proj.packages[pkg_id]
        if package.get('node_id') != node.node_id or package.get('session_id') != node.session_id:
            fail('REPORT_IDENTITY_INVALID', 'Report package belongs to a different execution')
        self.cp._verify_submission_artifact(package)
        raw = package.get('content') if self.cp.store.path is None else (
            Path(self.cp.store.path).parent / 'artifacts' / package['artifact_ref']).read_bytes().decode('utf-8')
        if 'report' in payload and payload['report'] != raw:
            fail('REPORT_PACKAGE_MISMATCH', 'Report must equal the immutable runtime submission')
        return raw, {**identity, 'package_id': pkg_id, 'report_hash': package['content_hash'], 'report_ref': package['artifact_ref']}

    def _write_blob(self, raw):
        sha = hashlib.sha256(raw).hexdigest()
        if self.cp.store.path is None:
            return {'sha256': sha, 'size': len(raw), 'content_base64': base64.b64encode(raw).decode()}
        directory = Path(self.cp.store.path).parent / 'acceptance' / 'blobs'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / sha
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 1024):
            fail('CANDIDATE_BLOB_CORRUPT', 'Content-addressed blobs cannot be links')
        if path.exists():
            if path.read_bytes() != raw:
                fail('CANDIDATE_BLOB_CORRUPT', 'Existing content-addressed blob is corrupt')
        else:
            with path.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != 'nt':
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        return {'sha256': sha, 'size': len(raw), 'blob_ref': sha}

    def _snapshot(self, payload):
        snapshot = copy.deepcopy(payload.get('snapshot', payload))
        files = bounded_list(snapshot.get('candidate_files', snapshot.get('files')), 'candidate files', MAX_FILES)
        if not files:
            fail('CANDIDATE_FILES_REQUIRED', 'Candidate must contain at least one file or text blob')
        total, seen, manifest_files = 0, set(), []
        for entry in files:
            if not isinstance(entry, dict):
                fail('CANDIDATE_PATH_INVALID', 'Invalid file entry')
            key = path_key(entry.get('path'))
            if key in seen:
                fail('CANDIDATE_PATH_DUPLICATE', 'Duplicate case-insensitive candidate path')
            seen.add(key)
            operation = entry.get('operation', 'file')
            if operation in ('deleted', 'delete'):
                if entry.get('content_base64') not in (None, ''):
                    fail('CANDIDATE_INVALID', 'Deleted paths cannot contain bytes')
                manifest_files.append({'path': entry['path'], 'operation': 'deleted', 'size': 0, 'sha256': None})
                continue
            if operation not in ('file', 'write'):
                fail('CANDIDATE_INVALID', 'Unknown file operation')
            try:
                encoded = entry['content_base64']
                if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 4:
                    fail('CANDIDATE_TOO_LARGE', 'Candidate exceeds the byte limit')
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, KeyError, TypeError) as exc:
                fail('CANDIDATE_INVALID', f'Invalid base64 content: {exc}')
            total += len(raw)
            if total > MAX_BYTES:
                fail('CANDIDATE_TOO_LARGE', 'Candidate exceeds the total byte limit')
            blob = self._write_blob(raw)
            if entry.get('sha256') not in (None, blob['sha256']) or entry.get('size') not in (None, len(raw)):
                fail('CANDIDATE_HASH_MISMATCH', 'Runtime bytes differ from supplied hash or size')
            manifest_files.append({'path': entry['path'], 'operation': 'file', **blob})
        for key in seen:
            if any(key.startswith(other + '/') for other in seen if other != key):
                fail('CANDIDATE_PATH_INVALID', 'Candidate file paths overlap as file and directory')
        for changed in bounded_list(snapshot.get('changed_paths', []), 'changed paths', MAX_FILES):
            path_key(changed)
        manifest_files.sort(key=lambda row: row['path'])
        file_keys = {path_key(f['path']) for f in manifest_files if f['operation'] == 'file'}
        for name in ('entry_paths', 'required_paths'):
            for value in bounded_list(snapshot.get(name, []), name, MAX_FILES):
                if path_key(value) not in file_keys:
                    fail('CANDIDATE_DEPENDENCY_MISSING', f'Required path is not sealed: {value}')
        if type(snapshot.get('dependency_complete')) is not bool:
            fail('CANDIDATE_INVALID', 'dependency_complete must be explicit')
        result = {'schema': 'dpswarm-candidate-v1', 'kind': snapshot.get('kind', 'files'),
                  'binding': snapshot.get('binding', {}), 'entry_paths': snapshot.get('entry_paths', []),
                  'changed_paths': snapshot.get('changed_paths', []), 'candidate_files': manifest_files,
                  'unknown_dependencies': bounded_list(snapshot.get('unknown_dependencies', []), 'unknown dependencies'),
                  'dependency_complete': snapshot['dependency_complete'],
                  'consumed_manifest_refs': bounded_list(snapshot.get('consumed_manifest_refs', []), 'consumed manifests')}
        if result['kind'] not in ('files', 'text'):
            fail('CANDIDATE_INVALID', 'Candidate kind must be files or text')
        metadata = copy.deepcopy(result)
        for entry in metadata['candidate_files']:
            entry.pop('content_base64', None)
            entry.pop('blob_ref', None)
        sha = digest(metadata)
        if self.cp.store.path is not None:
            # Reviewers receive an exact directory-preserving view of the blobs.
            view = Path(self.cp.store.path).parent / 'acceptance' / 'views' / sha
            view.mkdir(parents=True, exist_ok=True)
            if view.resolve() != view.absolute() or getattr(view.lstat(), 'st_file_attributes', 0) & 1024:
                fail('CANDIDATE_PATH_INVALID', 'Verification view cannot contain path aliases')
            for entry in result['candidate_files']:
                if entry['operation'] == 'deleted':
                    continue
                target = view / entry['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.resolve().is_relative_to(view.resolve()):
                    fail('CANDIDATE_PATH_INVALID', 'View path escapes the sealed candidate')
                raw = (Path(self.cp.store.path).parent / 'acceptance' / 'blobs' / entry['sha256']).read_bytes()
                if target.exists():
                    if target.read_bytes() != raw:
                        fail('CANDIDATE_VIEW_CORRUPT', 'Previously sealed verification view has changed')
                else:
                    with target.open('xb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
            result['view_path'] = str(view.resolve())
        return result, sha

    def verify_candidate(self, candidate):
        metadata = copy.deepcopy(candidate['snapshot'])
        metadata.pop('view_path', None)
        for row in metadata['candidate_files']:
            row.pop('blob_ref', None)
            row.pop('content_base64', None)
        if digest(metadata) != candidate['manifest_digest']:
            fail('CANDIDATE_MANIFEST_CORRUPT', 'Candidate metadata no longer matches its manifest digest')
        view = candidate['snapshot'].get('view_path')
        if view:
            root = Path(view)
            if root.is_symlink() or root.resolve() != root.absolute():
                fail('CANDIDATE_VIEW_CORRUPT', 'Candidate view contains a link or alias')
            expected = {path_key(row['path']) for row in metadata['candidate_files'] if row['operation'] == 'file'}
            for folder, dirs, files in os.walk(root, followlinks=False):
                for name in dirs + files:
                    path = Path(folder) / name
                    info = path.lstat()
                    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024:
                        fail('CANDIDATE_VIEW_CORRUPT', 'Candidate view contains a symlink or junction')
                for name in files:
                    relative = (Path(folder) / name).relative_to(root).as_posix()
                    if path_key(relative) not in expected:
                        fail('CANDIDATE_VIEW_CORRUPT', 'Candidate view contains an unsealed file')
        for entry in candidate['snapshot']['candidate_files']:
            if entry['operation'] == 'deleted':
                continue
            try:
                raw = (base64.b64decode(entry['content_base64'], validate=True) if self.cp.store.path is None
                       else (Path(self.cp.store.path).parent / 'acceptance' / 'blobs' / entry['sha256']).read_bytes())
                view = candidate['snapshot'].get('view_path')
                if view and (Path(view) / entry['path']).read_bytes() != raw:
                    fail('CANDIDATE_VIEW_CORRUPT', 'The verification view differs from the sealed candidate')
            except (OSError, ValueError, KeyError) as exc:
                fail('CANDIDATE_BLOB_MISSING', f'Candidate blob is unreadable: {exc}')
            if len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                fail('CANDIDATE_BLOB_CORRUPT', 'Candidate blob no longer matches its manifest')

    def verify_acceptance_artifacts(self, contract, candidate):
        self.verify_candidate(candidate)
        for binding in candidate['submission_bindings'].values():
            self.cp._verify_submission_artifact(self.cp.proj.packages[binding['submission_package_id']])
        for evidence_id in candidate['roster_evidence'].values():
            evidence = contract['evidence'][evidence_id]
            if evidence['status'] == 'registered':
                self._report(evidence['identity'])
        review = contract['reviews'][contract['current_review_id']]
        if review['reviewer_id'] is not None:
            self._report(review['identity'])
        else:
            identity = review['identity']
            self._identity(identity, lead=True)
            try:
                raw = identity['report'].encode('utf-8') if self.cp.store.path is None else (
                    Path(self.cp.store.path).parent / 'artifacts' / identity['report_ref']).read_bytes()
            except (OSError, KeyError) as exc:
                fail('EVIDENCE_NOT_READABLE', f'Lead review report is unreadable: {exc}')
            if hashlib.sha256(raw).hexdigest() != identity['report_hash']:
                fail('EVIDENCE_CORRUPT', 'Lead review report hash changed')

    def _findings(self, contract, candidate, record, actor, final=False):
        for row in record['findings']:
            fid = row['id']
            known = {r['id'] for revision in contract['requirement_history'] for r in revision['requirements']}
            if any(ref not in known for ref in row['requirement_ids']):
                fail('FINDING_REQUIREMENT_UNKNOWN', 'Finding refers to an unknown requirement')
            for path in row['paths']:
                path_key(path)
            existing = contract['findings'].get(fid)
            if not existing:
                if len(contract['findings']) >= 256:
                    fail('ACCEPTANCE_LIMIT', 'Finding limit reached')
                existing = {'id': fid, 'task_lineage': contract['task_lineage'],
                            'observation': row['observation'], 'requirement_ids': row['requirement_ids'],
                            'first_candidate_id': candidate['candidate_id'],
                            'first_requirement_revision': candidate['requirement_revision'], 'history': []}
                contract['findings'][fid] = existing
            existing['requirement_ids'] = sorted(set(existing['requirement_ids']) | set(row['requirement_ids']))
            existing['history'].append({'candidate_id': candidate['candidate_id'],
                'requirement_revision': candidate['requirement_revision'], 'actor': actor,
                'decision': copy.deepcopy(row), 'final_review': final})
            if len(existing['history']) > 128:
                fail('ACCEPTANCE_LIMIT', 'Finding history limit reached')

    def _candidate(self, contract, payload, *, allow_historical=False):
        candidate = contract['candidates'].get(payload.get('candidate_id'))
        if not candidate or (not allow_historical and candidate['candidate_id'] != contract['current_candidate_id']):
            fail('CANDIDATE_STALE', 'Report must reference the current candidate')
        if not allow_historical and candidate['requirement_revision'] != contract['requirement_revision']:
            fail('REQUIREMENT_REVISION_STALE', 'Candidate requires the amended requirements')
        if not allow_historical:
            self.verify_candidate(candidate)
        return candidate

    def _bind_record(self, record, candidate):
        expected = {'candidate_id': candidate['candidate_id'], 'manifest_digest': candidate['manifest_digest'],
                    'requirement_revision': candidate['requirement_revision']}
        for key, value in expected.items():
            if (key == 'requirement_revision' and type(record.get(key)) is not int) or record.get(key) != value:
                fail('REVIEW_CANDIDATE_MISMATCH', f'Report {key} differs from the sealed candidate')
        return {**record, **expected}

    def _evidence(self, contract, candidate, payload, *, final=False):
        roster_id = identifier(payload.get('roster_id'), 'roster id')
        entry = next((r for r in candidate['verification_roster'] if r['id'] == roster_id), None)
        if not entry:
            fail('VERIFICATION_ROSTER_MISMATCH', 'Report is not in the frozen verification roster')
        previous_id = candidate['roster_evidence'].get(roster_id)
        previous = contract['evidence'].get(previous_id)
        if previous_id and payload.get('continuation_of') != previous_id:
            # A parsed reviewer input can be finalized without replacing the
            # immutable report; no evidence or input revision is created.
            if final and previous['identity'].get('package_id') == payload.get('package_id'):
                self._report(payload)
                return previous
            fail('REPORT_CONTINUATION_REQUIRED', 'New reports require explicit prior evidence lineage')
        if len(contract['evidence']) >= 128:
            fail('ACCEPTANCE_LIMIT', 'Evidence report limit reached')
        eid = f"evidence-{len(contract['evidence']) + 1}"
        if payload.get('missing') is True:
            if final:
                fail('REVIEW_REPORT_INVALID', 'Final review cannot be missing')
            item = self.cp.proj.work_items.get(payload.get('item_id') or entry.get('item_id'))
            if not item or getattr(item.acceptance, 'value', None) not in ('terminated', 'escalated'):
                fail('VERIFICATION_STILL_RUNNING', 'Only terminal unavailable reports can be marked missing')
            evidence = {'evidence_id': eid, 'candidate_id': candidate['candidate_id'], 'roster_id': roster_id,
                        'status': 'missing', 'reason': text(payload.get('reason'), 'missing report reason'),
                        'identity': {'item_id': item.item_id}, 'record': None}
        else:
            raw, identity = self._report(payload)
            if entry.get('item_id') and entry['item_id'] != identity['item_id'] and not previous_id:
                fail('VERIFICATION_ROSTER_MISMATCH', 'Report item differs from frozen roster')
            if previous_id and identity['session_id'] == previous['identity'].get('session_id'):
                fail('REPORT_CONTINUATION_IDENTITY', 'Report-only continuation requires a new execution session')
            if any(e.get('identity', {}).get('item_id') == identity['item_id'] and e.get('roster_id') != roster_id
                   for e in contract['evidence'].values() if e['candidate_id'] == candidate['candidate_id']):
                fail('VERIFICATION_ROSTER_MISMATCH', 'One report execution cannot impersonate another verification role')
            if identity['item_id'] in candidate['candidate_item_ids']:
                fail('VERIFICATION_ROSTER_MISMATCH', 'Implementer cannot occupy verification roster')
            if final and entry['role'] != 'reviewer':
                fail('REVIEWER_MISMATCH', 'Final review requires the reviewer roster role')
            error, record = None, None
            try:
                parsed = parse_report(raw)
                if parsed.get('schema') != contract.get('review_contract', REVIEW_V1):
                    # A report of the wrong contract version is a contract-level
                    # rejection, not a stored parse error of the frozen contract.
                    fail('REVIEW_CONTRACT_MISMATCH',
                         f"This contract froze {contract.get('review_contract', REVIEW_V1)}; a report of that exact version is required")
                record = self._bind_record(parsed, candidate)
                if record.get('schema') == REVIEW_V2:
                    verify_v2_semantics(contract, record, candidate)
                if payload.get('repair_purpose') == 'format':
                    prior_id = payload.get('continuation_of')
                    prior = contract['evidence'].get(prior_id)
                    verify_format_repair(prior.get('record') if prior else None, record)
            except Exception as exc:
                from .control import ControlPlaneError
                # Contract-level gates (version mismatch, format-repair drift) are
                # transaction failures: the prior report must stay current.
                if not isinstance(exc, ControlPlaneError) or exc.code in (
                        'REVIEW_CONTRACT_MISMATCH', 'REPAIR_SEMANTIC_DRIFT', 'REPAIR_SEMANTIC_BASE_MISSING'):
                    raise
                error = {'error': exc.code, 'message': str(exc)}
            evidence = {'evidence_id': eid, 'candidate_id': candidate['candidate_id'], 'roster_id': roster_id,
                        'status': 'registered', 'identity': identity, 'record': record,
                        'parse_error': error, 'continuation_of': payload.get('continuation_of')}
            if record:
                self._findings(contract, candidate, record, identity, final=final)
        contract['evidence'][eid] = evidence
        candidate['roster_evidence'][roster_id] = eid
        if (entry['role'] != 'reviewer' or candidate['candidate_id'] != contract['current_candidate_id']
                or entry['id'] != contract['reviewer_id']):
            contract['evidence_revision'] += 1
        return evidence

    def mutate(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('payload'), dict):
            fail('ACCEPTANCE_INVALID', 'Acceptance request requires action and object payload')
        action = body.get('action')
        if action not in ('bind', 'amend', 'candidate', 'evidence', 'review', 'takeover'):
            fail('ACCEPTANCE_INVALID', 'Unknown acceptance action')
        request_id = identifier(body.get('request_id'), 'request id')
        payload, request_hash = body['payload'], digest(body)
        with self.cp._lock:
            cid = body.get('contract_id')
            if action == 'bind':
                cid = cid or 'contract-' + digest({'lineage': payload.get('task_lineage'), 'root': self.cp.proj.root_id})[:24]
            identifier(cid, 'contract id')
            existing = self.cp.proj.acceptance_contracts.get(cid)
            if existing and request_id in existing['requests']:
                old = existing['requests'][request_id]
                if old['hash'] != request_hash:
                    fail('ACCEPTANCE_REQUEST_CONFLICT', 'Request id was used for different content')
                return {**self._response(existing), **old['result'], 'replayed': True}
            if action == 'bind':
                if existing:
                    fail('ACCEPTANCE_CONTRACT_EXISTS', 'Contract already exists')
                if len(self.cp.proj.acceptance_contracts) >= 32:
                    fail('ACCEPTANCE_LIMIT', 'Contract limit reached')
                lineage = identifier(payload.get('task_lineage'), 'task lineage')
                if any(c['task_lineage'] == lineage for c in self.cp.proj.acceptance_contracts.values()):
                    fail('ACCEPTANCE_LINEAGE_EXISTS', 'Existing lineage requires an amendment')
                source = self._source(payload.get('source'))
                requirements = self._requirements(payload.get('requirements'), {source['message_id']})
                reviewer = payload.get('reviewer_id')
                if reviewer is not None:
                    identifier(reviewer, 'reviewer id')
                # The report contract freezes at bind: v2 semantics (plan-bound
                # checks and finding dispositions) only apply where negotiated.
                review_contract = payload.get('review_contract', REVIEW_V1)
                if review_contract not in (REVIEW_V1, REVIEW_V2):
                    fail('ACCEPTANCE_INVALID', 'review_contract must be dpswarm-review-v1 or dpswarm-review-v2')
                contract = {'schema': SCHEMA, 'contract_id': cid, 'task_lineage': lineage,
                    'root_id': self.cp.proj.root_id, 'host_session_id': self._root_session(),
                    'review_contract': review_contract,
                    'revision': 0, 'requirement_revision': 1, 'evidence_revision': 0, 'review_revision': 0,
                    'sources': [source], 'requirements': requirements,
                    'requirement_history': [{'revision': 1, 'source': source, 'requirements': requirements}],
                    'lead_plan': payload.get('lead_plan', ''), 'reviewer_id': reviewer,
                    'takeovers': [], 'candidates': {}, 'current_candidate_id': None, 'enrolled_item_ids': [],
                    'evidence': {}, 'reviews': {}, 'current_review_id': None, 'findings': {},
                    'accepted': [], 'requests': {}}
            else:
                if not existing:
                    fail('ACCEPTANCE_CONTRACT_UNKNOWN', 'Unknown acceptance contract')
                if type(body.get('expected_revision')) is not int or body['expected_revision'] != existing['revision']:
                    fail('ACCEPTANCE_REVISION_CONFLICT', f"Expected ledger revision {existing['revision']}")
                contract = copy.deepcopy(existing)
            result = {}
            if action == 'amend':
                if (payload.get('task_lineage') != contract['task_lineage'] or type(payload.get('parent_requirement_revision')) is not int
                        or payload['parent_requirement_revision'] != contract['requirement_revision']):
                    fail('AMENDMENT_LINEAGE_MISMATCH', 'Amendment requires this lineage and exact parent requirement revision')
                source = self._source(payload.get('source'))
                if source['message_id'] in {s['message_id'] for s in contract['sources']}:
                    fail('AMENDMENT_SOURCE_REUSED', 'Amendment requires a new trusted user message')
                requirements = self._requirements(payload.get('requirements'), {s['message_id'] for s in contract['sources']} | {source['message_id']})
                contract['sources'].append(source)
                contract['requirement_revision'] += 1
                contract['requirements'] = requirements
                contract['requirement_history'].append({'revision': contract['requirement_revision'], 'source': source,
                    'requirements': requirements, 'reason': text(payload.get('reason'), 'amendment reason')})
                contract['current_review_id'] = None
            elif action == 'candidate':
                candidate_id = identifier(payload.get('candidate_id'), 'candidate id')
                if candidate_id in contract['candidates'] or len(contract['candidates']) >= 64:
                    fail('CANDIDATE_ID_REUSED', 'Candidate id is immutable or candidate limit reached')
                if type(payload.get('requirement_revision')) is not int or payload['requirement_revision'] != contract['requirement_revision']:
                    fail('REQUIREMENT_REVISION_STALE', 'Candidate must bind current requirements')
                generation = payload.get('generation')
                if type(generation) is not int or generation < 0:
                    fail('CANDIDATE_INVALID', 'Generation must be a nonnegative integer')
                previous = contract['candidates'].get(contract['current_candidate_id'])
                if previous and generation < previous['generation']:
                    fail('CANDIDATE_GENERATION_STALE', 'Generation cannot go backwards')
                item_ids = bounded_list(payload.get('candidate_item_ids'), 'candidate item ids', 64)
                if not item_ids or len(item_ids) != len(set(item_ids)):
                    fail('CANDIDATE_ITEM_MISMATCH', 'Candidate requires distinct implementation items')
                bindings = {}
                for iid in item_ids:
                    identifier(iid, 'candidate item id')
                    item = self.cp.proj.work_items.get(iid)
                    if not item or not item.submission_package_id:
                        fail('CANDIDATE_SUBMISSION_MISSING', 'Each implementation item needs an immutable report package')
                    enrolled = contract_for_item(self.cp.proj, iid)
                    if enrolled and enrolled['contract_id'] != cid:
                        fail('CANDIDATE_ITEM_MISMATCH', 'Item belongs to another delivery lineage')
                    self._report({'item_id': iid, 'node_id': item.submission_node_id,
                        'session_id': item.submission_session_id, 'context_epoch': item.submission_context_epoch,
                        'package_id': item.submission_package_id})
                    bindings[iid] = {k: getattr(item, k) for k in ('submission_package_id', 'submission_id',
                        'submission_node_id', 'submission_attempt', 'submission_context_epoch', 'submission_session_id')}
                roster = bounded_list(payload.get('verification_roster'), 'verification roster', 64)
                seen = set()
                for row in roster:
                    if not isinstance(row, dict) or row.get('role') not in ('tester', 'reviewer'):
                        fail('VERIFICATION_ROSTER_INVALID', 'Roster roles are tester and reviewer')
                    rid = identifier(row.get('id'), 'roster id')
                    if rid in seen:
                        fail('VERIFICATION_ROSTER_INVALID', 'Duplicate roster id')
                    seen.add(rid)
                    if row.get('item_id') in item_ids:
                        fail('VERIFICATION_ROSTER_INVALID', 'Implementer cannot occupy verification roster')
                if contract['reviewer_id'] is not None and not any(r['id'] == contract['reviewer_id'] and r['role'] == 'reviewer' for r in roster):
                    fail('VERIFICATION_ROSTER_INVALID', 'Current reviewer is absent from the frozen roster')
                snapshot, sha = self._snapshot(payload)
                candidate = {'candidate_id': candidate_id, 'generation': generation,
                    'requirement_revision': contract['requirement_revision'], 'candidate_item_ids': item_ids,
                    'submission_bindings': bindings, 'manifest_digest': sha, 'snapshot': snapshot,
                    'view_path': snapshot.get('view_path'), 'candidate_files': snapshot['candidate_files'],
                    'verification_roster': copy.deepcopy(roster), 'roster_evidence': {}}
                contract['candidates'][candidate_id] = candidate
                contract['current_candidate_id'] = candidate_id
                contract['current_review_id'] = None
                contract['evidence_revision'] += 1
                contract['enrolled_item_ids'] = sorted(set(contract['enrolled_item_ids']) | set(item_ids))
                result = {'candidate_id': candidate_id, 'manifest_digest': sha}
            elif action == 'evidence':
                candidate = self._candidate(contract, payload, allow_historical=True)
                evidence = self._evidence(contract, candidate, payload)
                contract['current_review_id'] = None
                if contract['accepted']:
                    contract['needs_revalidation'] = True
                result = {'evidence_id': evidence['evidence_id'], 'parse_error': evidence.get('parse_error')}
            elif action == 'review':
                candidate = self._candidate(contract, payload)
                if type(payload.get('evidence_revision')) is not int or payload['evidence_revision'] != contract['evidence_revision']:
                    fail('REVIEW_EVIDENCE_STALE', 'Review must cite current external evidence revision')
                if contract['reviewer_id'] is None:
                    raw, identity = self._report(payload, lead=True)
                    parsed = parse_report(raw)
                    if parsed.get('schema') != contract.get('review_contract', REVIEW_V1):
                        fail('REVIEW_CONTRACT_MISMATCH',
                             f"This contract froze {contract.get('review_contract', REVIEW_V1)}; a report of that exact version is required")
                    record = self._bind_record(parsed, candidate)
                    if record.get('schema') == REVIEW_V2:
                        verify_v2_semantics(contract, record, candidate)
                    self._findings(contract, candidate, record, identity, final=True)
                else:
                    if payload.get('roster_id') != contract['reviewer_id']:
                        fail('REVIEWER_MISMATCH', 'Only the current reviewer can submit final review')
                    evidence = self._evidence(contract, candidate, payload, final=True)
                    if evidence.get('parse_error'):
                        # Surface the stored code (v2 semantic gates) with its message.
                        fail(evidence['parse_error']['error'], evidence['parse_error']['message'])
                    record, identity = evidence['record'], evidence['identity']
                if type(record.get('evidence_revision')) is not int or record['evidence_revision'] != contract['evidence_revision']:
                    fail('REVIEW_EVIDENCE_STALE', 'Final report itself must bind the current input evidence revision')
                contract['review_revision'] += 1
                review_id = f"review-{contract['review_revision']}"
                review = {'review_id': review_id, 'candidate_id': candidate['candidate_id'],
                    'manifest_digest': candidate['manifest_digest'], 'requirement_revision': contract['requirement_revision'],
                    'evidence_revision': contract['evidence_revision'], 'review_revision': contract['review_revision'],
                    'reviewer_id': contract['reviewer_id'], 'identity': identity, 'record': record,
                    'consumed_manifest_refs': record.get('consumed_manifest_refs', payload.get('consumed_manifest_refs', []))}
                if len(contract['reviews']) >= 128:
                    fail('ACCEPTANCE_LIMIT', 'Review limit reached')
                contract['reviews'][review_id] = review
                contract['current_review_id'] = review_id
                result = {'review_id': review_id}
            elif action == 'takeover':
                reviewer = payload.get('reviewer_id')
                if reviewer is not None:
                    identifier(reviewer, 'reviewer id')
                    current = contract['candidates'].get(contract['current_candidate_id'])
                    if current and not any(r['id'] == reviewer and r['role'] == 'reviewer' for r in current['verification_roster']):
                        fail('REVIEWER_MISMATCH', 'Replacement reviewer must be in the candidate roster')
                contract['takeovers'].append({'from': contract['reviewer_id'], 'to': reviewer,
                    'reason': text(payload.get('reason'), 'takeover reason'), 'actor': self._root_session()})
                contract['reviewer_id'] = reviewer
                contract['current_review_id'] = None
            if len(contract['requests']) >= 512 or len(canonical(contract)) > 8_000_000:
                fail('ACCEPTANCE_LIMIT', 'Ledger bound reached; records remain readable')
            contract['revision'] += 1
            contract['requests'][request_id] = {'hash': request_hash, 'result': result}
            self.cp._transact(('acceptance_updated', {'contract_id': cid,
                'previous_revision': existing['revision'] if existing else 0, 'contract': contract}))
            return {**self._response(contract), **result}

    @staticmethod
    def _response(contract):
        return {'ok': True, 'schema': SCHEMA, 'contract_id': contract['contract_id'],
                'revision': contract['revision'], 'evidence_revision': contract['evidence_revision'],
                'review_revision': contract['review_revision'], 'requirement_revision': contract['requirement_revision'],
                'contract_revision': contract['requirement_revision'], 'contract': copy.deepcopy(contract),
                'candidate': copy.deepcopy(contract['candidates'].get(contract['current_candidate_id'])),
                'review': copy.deepcopy(contract['reviews'].get(contract['current_review_id']))}
