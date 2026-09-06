"""One canonical episode ledger with non-transferable component limits."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import threading
import time

_PLUGIN = Path(__file__).resolve().parents[2] / 'dpswarm-plugin'
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))
from dpswarm.team_runtime.ledger import RunBudget, LedgerError, digest, nonnegative
from modelbench.swe_fixed_team_20260903.runtime_integrity import atomic_json


R2_SCOPES = {
    'candidate_1': {'max_calls': 12, 'token_limit': 270000, 'cm_call_allowance': 6},
    'candidate_2': {'max_calls': 12, 'token_limit': 270000, 'cm_call_allowance': 6},
    'selector': {'max_calls': 4, 'token_limit': 60000, 'cm_call_allowance': 0},
}


class BudgetPersistenceError(RuntimeError):
    pass


class EpisodeBudget:
    """Scope totals are projections, never a second copy of actual charges.

    Reservation is checked at both levels under one lock, persisted, and only
    then returned to the transport caller. A failed write poisons admission;
    received responses may still settle in memory and remain inspectable.
    """
    def __init__(self, *, max_calls=28, token_limit=600000, cm_call_allowance=12,
                 deadline_seconds=1800, scopes=None, path=None, clock=time.monotonic):
        self._lock = threading.RLock()
        self.clock = clock
        self.root = RunBudget(max_calls, token_limit, deadline_seconds,
                              cm_call_allowance=cm_call_allowance, clock=clock)
        self.scopes = deepcopy(R2_SCOPES if scopes is None else scopes)
        if not isinstance(self.scopes, dict) or not self.scopes:
            raise LedgerError('INVALID_SCOPES', 'At least one scope is required')
        for name, limits in self.scopes.items():
            if not isinstance(name, str) or not name or not isinstance(limits, dict) or set(limits) != set(R2_SCOPES['selector']):
                raise LedgerError('INVALID_SCOPES', 'Invalid scope name or limit keys')
            for key, value in limits.items():
                nonnegative(value, key)
        self.ticket_scopes = {}
        self.frozen_scopes = set()
        self.persistence_error = None
        self.path = Path(path) if path is not None else None
        if self.path is not None and self.path.exists():
            raise FileExistsError('Use from_snapshot to restore an existing episode budget')
        self._persist()

    @property
    def deadline_at(self):
        return self.root.deadline_at

    def scope(self, name):
        if name not in self.scopes:
            raise LedgerError('UNKNOWN_SCOPE', str(name))
        return BudgetScopeView(self, name)

    def _projection(self, scope):
        limits = self.scopes[scope]
        projected = RunBudget(**limits, clock=self.clock)
        projected.created_at = self.root.created_at
        projected.deadline_at = self.root.deadline_at
        projected.frozen = self.root.frozen or scope in self.frozen_scopes
        projected.tickets = {key: deepcopy(value) for key, value in self.root.tickets.items()
                             if self.ticket_scopes[key] == scope}
        return projected

    def reserve(self, scope, ticket_id, role, reserved_tokens=32768):
        with self._lock:
            if scope not in self.scopes:
                raise LedgerError('UNKNOWN_SCOPE', str(scope))
            if self.persistence_error:
                raise BudgetPersistenceError(self.persistence_error)
            existing_scope = self.ticket_scopes.get(ticket_id)
            if existing_scope is not None and existing_scope != scope:
                raise LedgerError('SCOPE_CONFLICT', 'Ticket already belongs to another scope')
            # The projection mutates only a throwaway view. Root is untouched
            # when either the local quota or the root quota rejects admission.
            self._projection(scope).reserve(ticket_id, role, reserved_tokens)
            value = self.root.reserve(ticket_id, role, reserved_tokens)
            self.ticket_scopes[ticket_id] = scope
            self._persist()
            return {**value, 'scope_id': scope}

    def complete(self, scope, ticket_id, record):
        with self._lock:
            if self.ticket_scopes.get(ticket_id) != scope:
                raise LedgerError('SCOPE_CONFLICT', 'Completion does not own this ticket')
            value = self.root.complete(ticket_id, record)
            self._persist()
            return {**value, 'scope_id': scope}

    def summary(self):
        with self._lock:
            return {**self.root.summary(), 'scope_summaries': {
                name: self._projection(name).summary() for name in self.scopes},
                'persistence_error': self.persistence_error}

    def scope_summary(self, scope):
        with self._lock:
            root, local = self.root.summary(), self._projection(scope).summary()
            local.update(scope_id=scope, root_summary=root,
                         remaining_calls=min(local['remaining_calls'], root['remaining_calls']),
                         remaining_tokens=min(local['remaining_tokens'], root['remaining_tokens']))
            if local['remaining_cm_calls'] is not None and root['remaining_cm_calls'] is not None:
                local['remaining_cm_calls'] = min(local['remaining_cm_calls'], root['remaining_cm_calls'])
            return local

    def freeze(self, scope=None):
        with self._lock:
            if scope is None:
                self.root.freeze()
            elif scope in self.scopes:
                self.frozen_scopes.add(scope)
            else:
                raise LedgerError('UNKNOWN_SCOPE', str(scope))
            self._persist()

    def snapshot(self):
        with self._lock:
            value = {'version': 1, 'root': self.root.snapshot(), 'scopes': deepcopy(self.scopes),
                     'ticket_scopes': dict(self.ticket_scopes),
                     'frozen_scopes': sorted(self.frozen_scopes),
                     'persistence_error': self.persistence_error}
            return {**value, 'snapshot_hash': digest(value)}

    def _persist(self):
        if self.path is None:
            return
        try:
            atomic_json(self.path, self.snapshot())
        except Exception as exc:
            self.persistence_error = f'{type(exc).__name__}: {exc}'
            self.root.freeze()
            raise BudgetPersistenceError(self.persistence_error) from exc

    @classmethod
    def from_snapshot(cls, snapshot, *, path=None, clock=time.monotonic):
        value = deepcopy(snapshot)
        saved_hash = value.pop('snapshot_hash', None)
        if value.get('version') != 1 or digest(value) != saved_hash:
            raise LedgerError('SNAPSHOT_CORRUPT', 'Episode snapshot hash/version mismatch')
        obj = cls(scopes=value['scopes'], clock=clock)
        obj.root = RunBudget.from_snapshot(value['root'], clock=clock)
        obj.ticket_scopes = value['ticket_scopes']
        obj.frozen_scopes = set(value['frozen_scopes'])
        obj.persistence_error = value.get('persistence_error')
        if (set(obj.ticket_scopes) != set(obj.root.tickets)
                or not set(obj.ticket_scopes.values()) <= set(obj.scopes)
                or not obj.frozen_scopes <= set(obj.scopes)):
            raise LedgerError('SNAPSHOT_CORRUPT', 'Scope ownership is incomplete or invalid')
        for name, limits in obj.scopes.items():
            if any(ticket['reserved_tokens'] > limits['token_limit'] for key, ticket in obj.root.tickets.items()
                   if obj.ticket_scopes[key] == name):
                raise LedgerError('SNAPSHOT_CORRUPT', 'Scope reservation exceeds its admission quota')
            view = obj._projection(name).summary()
            work = view['call_count'] - view['cm_call_count']
            if work > limits['max_calls'] or view['cm_call_count'] > limits['cm_call_allowance']:
                raise LedgerError('SNAPSHOT_CORRUPT', 'Scope call quota exceeded')
        obj.path = Path(path) if path is not None else None
        return obj


class BudgetScopeView:
    def __init__(self, episode, scope_id):
        self.episode, self.scope_id = episode, scope_id

    @property
    def max_calls(self):
        return self.episode.scopes[self.scope_id]['max_calls']

    @property
    def token_limit(self):
        return self.episode.scopes[self.scope_id]['token_limit']

    @property
    def cm_call_allowance(self):
        return self.episode.scopes[self.scope_id]['cm_call_allowance']

    @property
    def deadline_at(self):
        return self.episode.deadline_at

    def reserve(self, ticket_id, role, reserved_tokens=32768):
        return self.episode.reserve(self.scope_id, ticket_id, role, reserved_tokens)

    def complete(self, ticket_id, record):
        return self.episode.complete(self.scope_id, ticket_id, record)

    def summary(self):
        return self.episode.scope_summary(self.scope_id)

    def snapshot(self):
        with self.episode._lock:
            value = self.episode._projection(self.scope_id).snapshot()
            return {'scope_id': self.scope_id, 'scope_budget': value,
                    'episode_snapshot_hash': self.episode.snapshot()['snapshot_hash']}

    to_dict = snapshot

    def freeze(self):
        self.episode.freeze(self.scope_id)
