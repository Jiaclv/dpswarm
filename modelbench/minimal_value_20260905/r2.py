"""Two isolated Solo candidates followed by bounded, non-oracle selection."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from .budget import EpisodeBudget, R2_SCOPES
from .selector import RestrictedSelector, FrozenCandidate, CandidateIntegrityError, sha_bytes
from modelbench.swe_fixed_team_20260903.runtime_integrity import atomic_json


def utc():
    return datetime.now(timezone.utc).isoformat()


def fallback(candidates):
    for candidate in sorted(candidates, key=lambda c: c.candidate_id):
        candidate.read()
        if candidate.size > 0 and candidate.applicable is True:
            return {'selected': candidate.candidate_id, 'sha256': candidate.sha256,
                    'reason': 'First saved nonempty applicable candidate in frozen order'}
    return {'selected': None, 'sha256': sha_bytes(b''), 'reason': 'No saved eligible candidate; empty patch'}


class R2Run:
    def __init__(self, batch_dir, entry, *, run_factory=None, transport_factory=None,
                 environment_factory=None, control_factory=None, selector_factory=RestrictedSelector,
                 model_slots=None, container_slots=None, resource_failure=None,
                 clock=time.monotonic):
        if run_factory is None or model_slots is None or container_slots is None or resource_failure is None:
            from .runner import ValueRun, MODEL_SLOTS, CONTAINER_SLOTS, RESOURCE_FAILURE
        if environment_factory is None:
            from .environment import ValueEnvironment
        if transport_factory is None:
            from modelbench.swe_verified_20260903.transport import SweTransport
        self.entry = deepcopy(entry)
        self.run_id = entry['run_id']
        if not isinstance(self.run_id, str) or not self.run_id or self.run_id in ('.', '..') or any(c in self.run_id for c in '/\\:'):
            raise ValueError('run_id must be one directory component')
        if entry.get('lead_model', 'gpt-5.6-sol') != 'gpt-5.6-sol':
            raise ValueError('R2 freezes all solver/selector models to gpt-5.6-sol')
        if entry.get('expected_candidates', 2) != 2:
            raise ValueError('R2 requires exactly two candidate opportunities')
        if not isinstance(entry.get('public_checks'), dict) or not entry['public_checks']:
            raise ValueError('R2 requires frozen public check commands')
        limits = entry.get('limits_override', {})
        for key, expected in {'max_calls': 28, 'token_limit': 600000,
                              'cm_call_allowance': 12, 'wall_seconds': 1800}.items():
            if limits.get(key, expected) != expected:
                raise ValueError('R2 root allocation is frozen: ' + key)
        expected_allocation = {name: {'tokens': v['token_limit'], 'work_calls': v['max_calls'],
                                      'cm_calls': v['cm_call_allowance']} for name, v in R2_SCOPES.items()}
        if entry.get('r2_allocation', expected_allocation) != expected_allocation:
            raise ValueError('R2 component allocation is frozen')
        self.batch_dir = Path(batch_dir)
        self.folder = self.batch_dir / 'results' / self.run_id
        self.folder.mkdir(parents=True, exist_ok=False)
        self.clock, self.start_clock, self.started_at = clock, clock(), utc()
        self.deadline = self.start_clock + 1800
        self.budget = EpisodeBudget(path=self.folder / 'episode-budget.json', clock=clock)
        self.budget.root.created_at = self.start_clock
        self.budget.root.deadline_at = self.deadline
        self.budget._persist()
        self.cancel = threading.Event()
        self.run_factory = run_factory or ValueRun
        self.transport_factory = transport_factory or SweTransport
        self.environment_factory = environment_factory or ValueEnvironment
        self.control_factory = control_factory
        self.selector_factory = selector_factory
        self.model_slots = model_slots if model_slots is not None else MODEL_SLOTS
        self.container_slots = container_slots if container_slots is not None else CONTAINER_SLOTS
        self.resource_failure = resource_failure if resource_failure is not None else RESOURCE_FAILURE
        self.components, self._lock = {}, threading.RLock()

    def _entry(self, scope):
        entry = deepcopy(self.entry)
        entry.update(run_id=f'{self.run_id}__{scope}', arm='R2-candidate', strategy_id='R2-candidate',
                     condition='solo', lead_model='gpt-5.6-sol', worker_model=None,
                     worker_models=[], worker_specs=[], expected_workers=0,
                     parent_episode_id=self.entry.get('episode_id', self.run_id), scope_id=scope)
        entry.pop('effective_limits', None)
        entry.pop('expected_candidates', None)
        entry.pop('r2_allocation', None)
        entry.pop('configuration_sha256', None)
        entry['limits_override'] = {**entry.get('limits_override', {}), **R2_SCOPES[scope]}
        from .contracts import digest
        entry['configuration_sha256'] = digest(entry)
        return entry

    def _run_candidate(self, scope):
        entry = self._entry(scope)
        kwargs = {'budget': self.budget.scope(scope), 'deadline': self.deadline,
                  'start_clock': self.start_clock, 'grade_enabled': False,
                  'transport_factory': self.transport_factory, 'environment_factory': self.environment_factory}
        if self.control_factory is not None:
            kwargs['control_factory'] = self.control_factory
        component = self.run_factory(self.folder, entry, **kwargs)
        with self._lock:
            self.components[scope] = component
        if self.cancel.is_set() or self.clock() >= self.deadline:
            component.cancel.set()
        result = component.run()
        # Candidate execution must not have consulted the official grader.
        score = result.get('score')
        if score and (score.get('completed') or score.get('resolved') is not None):
            raise CandidateIntegrityError('Candidate performed official scoring before selection')
        return result

    def _freeze_descriptor(self, scope, result):
        artifact = result.get('artifact') or result.get('lead_artifact') or {}
        if artifact.get('status') != 'present':
            return None
        path = Path(artifact.get('path', '')).resolve()
        expected_dir = (self.folder / 'results' / self._entry(scope)['run_id']).resolve()
        if not path.is_relative_to(expected_dir):
            raise CandidateIntegrityError('Candidate artifact escaped its isolated directory')
        data = path.read_bytes()
        expected_hash = artifact.get('sha256') or result.get('patch_sha256')
        if expected_hash != sha_bytes(data) or artifact.get('bytes') != len(data):
            raise CandidateIntegrityError('Candidate artifact identity does not match frozen bytes')
        applicability = artifact.get('applicable')
        if applicability is not None and type(applicability) is not bool:
            raise CandidateIntegrityError('Invalid saved applicability state')
        public = result.get('public_evidence') or {}
        if not isinstance(public, dict):
            public = {'summary': str(public)}
        # Never hand the component result (which may later gain score fields)
        # wholesale to a model. Only the explicitly public evidence field flows.
        return FrozenCandidate(scope, path, expected_hash, len(data), applicability, public)

    def run(self):
        results, candidates, errors = {}, [], []
        selector_result = None
        selection = None
        selection_source = 'fallback'
        cleanup_confirmed = True
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='r2-candidate') as executor:
                pending = {executor.submit(self._run_candidate, scope): scope
                           for scope in ('candidate_1', 'candidate_2')}
                while pending:
                    if self.cancel.is_set() or self.clock() >= self.deadline or self.resource_failure.is_set():
                        with self._lock:
                            for component in self.components.values():
                                component.cancel.set()
                    done, _ = wait(pending, timeout=0.1)
                    for future in done:
                        scope = pending.pop(future)
                        try:
                            results[scope] = future.result()
                        except Exception as exc:
                            cleanup_confirmed = False
                            errors.append({'scope_id': scope, 'type': type(exc).__name__, 'message': str(exc)})
                            self.cancel.set()
            for scope, result in sorted(results.items()):
                if result.get('cleanup_confirmed') is not True or result.get('quiesced') is not True:
                    cleanup_confirmed = False
                    errors.append({'scope_id': scope, 'type': 'CleanupUnconfirmed'})
                if result.get('infrastructure_error'):
                    errors.append({'scope_id': scope, 'type': 'CandidateInfrastructureError',
                                   'message': str(result['infrastructure_error'])})
                candidate = self._freeze_descriptor(scope, result)
                if candidate is not None:
                    candidates.append(candidate)
            atomic_json(self.folder / 'frozen-candidates.json', [c.descriptor() for c in candidates])
            if not errors and not self.cancel.is_set() and self.clock() < self.deadline:
                selector = self.selector_factory(directory=self.folder / 'selector', entry=self.entry,
                    candidates=candidates, budget=self.budget.scope('selector'), deadline=self.deadline,
                    transport_factory=self.transport_factory, environment_factory=self.environment_factory,
                    cancel=self.cancel, model_slots=self.model_slots, container_slots=self.container_slots,
                    resource_failure=self.resource_failure, clock=self.clock)
                selector_result = selector.run()
                cleanup_confirmed &= selector_result.get('cleanup_confirmed') is True
                if selector_result.get('infrastructure_error') or not cleanup_confirmed:
                    errors.append({'scope_id': 'selector', 'type': 'SelectorInfrastructureError',
                                   'message': str(selector_result.get('infrastructure_error'))})
                selection = selector_result.get('selection')
                if selection is not None:
                    source = next((c for c in candidates if c.candidate_id == selection.get('selected')), None)
                    if source is None or source.sha256 != selection.get('sha256') or not source.size or source.applicable is not True:
                        selector_result['selection_protocol_error'] = 'Unbound or ineligible selector response; frozen fallback applies'
                        selection = None
                    else:
                        selection_source = 'selector'
            for candidate in candidates:
                candidate.read()
            if selection is None:
                selection = fallback(candidates)
        except Exception as exc:
            errors.append({'type': type(exc).__name__, 'message': str(exc)})
            selection = {'selected': None, 'sha256': sha_bytes(b''), 'reason': 'Integrity/host failure; no deployable selection'}
        finally:
            self.cancel.set()
            try:
                self.budget.freeze()
            except Exception as exc:
                errors.append({'type': type(exc).__name__, 'message': str(exc)})
        selected = next((c for c in candidates if c.candidate_id == selection.get('selected')), None)
        try:
            patch = selected.read() if selected is not None and not errors else b''
        except Exception as exc:
            errors.append({'type': type(exc).__name__, 'message': str(exc)})
            patch = b''
        patch_path = self.folder / 'model.patch'
        patch_path.write_bytes(patch)
        artifact = {'status': 'present', 'path': str(patch_path), 'sha256': sha_bytes(patch),
                    'bytes': len(patch), 'applicable': bool(patch), 'source_candidate_id': selected.candidate_id if selected and not errors else None}
        result = {'schema_version': 12, 'run_id': self.run_id, 'episode_id': self.entry.get('episode_id', self.run_id),
                  'arm': 'R2', 'strategy_id': 'R2', 'condition': 'r2',
                  'instance_id': self.entry['instance']['instance_id'], 'lead_model': 'gpt-5.6-sol',
                  'started_at': self.started_at, 'patch_frozen_at': utc(),
                  'inference_wall_seconds': self.clock() - self.start_clock,
                  'artifact': artifact, 'lead_artifact': artifact, 'patch_sha256': artifact['sha256'],
                  'selection': selection, 'selection_source': selection_source,
                  'candidates': results, 'candidate_count': len(results), 'expected_candidates': 2,
                  'selector': selector_result, 'budget': self.budget.summary(),
                  'root_budget_snapshot': self.budget.snapshot(), 'cleanup_confirmed': cleanup_confirmed,
                  'quiesced': cleanup_confirmed, 'infrastructure_error': errors or None,
                  'score': None, 'grading_pending': not bool(errors),
                  'outcome': {'status': 'infrastructure_error' if errors else 'selected',
                              'summary': selection.get('reason', '')},
                  'official_scoring_owner': 'episode_terminal_grader',
                  **self.usage_summary()}
        atomic_json(self.folder / 'result.json', result)
        return result

    @staticmethod
    def _usage(records):
        fields = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')
        values = {'calls': len(records), 'call_ids': [r['call_id'] for r in records],
                  'transport_record_count': len(records), 'known_subtotals': {}, 'unknown_counts': {},
                  'sum_call_wall_seconds': sum(r.get('wall_seconds') or 0 for r in records), 'cost_usd': None}
        attempts = [r.get('transport_attempt_count') for r in records]
        measured = [a for a in attempts if type(a) is int and a >= 0]
        values.update(transport_attempt_count=sum(measured) if len(measured) == len(records) else None,
                      transport_attempt_count_known_subtotal=sum(measured),
                      transport_attempt_count_unknown_records=len(records) - len(measured),
                      calls_with_transport_attempts=sum(a > 0 for a in measured),
                      calls_with_measured_usage=sum(any(type(r.get(k)) is int and r[k] >= 0 for k in fields) for r in records),
                      calls_with_complete_usage=sum(all(type(r.get(k)) is int and r[k] >= 0
                          for k in ('input_tokens', 'output_tokens', 'total_tokens')) for r in records))
        for key in fields:
            known = [r[key] for r in records if type(r.get(key)) is int and r[key] >= 0]
            values[key] = sum(known) if len(known) == len(records) else None
            values['known_subtotals'][key] = sum(known)
            values['unknown_counts'][key] = len(records) - len(known)
            values[key + '_known_subtotal'] = sum(known)
        return values

    def usage_summary(self):
        # Read completed actual records once from the canonical root ledger.
        # Pending calls remain visible as reservations in the separate budget.
        tickets = self.budget.snapshot()['root']['tickets']
        completed = [(self.budget.ticket_scopes[key], ticket['record']) for key, ticket in tickets.items()
                     if ticket['status'] == 'completed']
        work = [record for _, record in completed if record['role'] != 'cm']
        cm = [record for _, record in completed if record['role'] == 'cm']
        agent_usage = {scope: {'agent_id': scope, 'role': 'selector' if scope == 'selector' else 'lead',
                              'model': 'gpt-5.6-sol', **self._usage([r for owner, r in completed
                              if owner == scope and r['role'] != 'cm'])} for scope in R2_SCOPES}
        model_usage = {model: self._usage([r for r in work if r.get('model_requested', 'unknown') == model])
                       for model in sorted({r.get('model_requested', 'unknown') for r in work})}
        totals = self._usage(work)
        return {**totals, 'call_count': len(work), 'cm_call_count': len(cm), 'cm_usage': self._usage(cm),
                'cm_call_ids': [r['call_id'] for r in cm], 'agent_usage': agent_usage, 'model_usage': model_usage,
                'cost_usd': None, 'cost_status': 'No verified monetary price/charge source',
                'accounting_source': 'one canonical root ledger; CM separate from work totals',
                'component_allocation': deepcopy(R2_SCOPES)}

    generate = run


R2Controller = R2Run
