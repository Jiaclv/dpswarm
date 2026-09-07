"""C1 provider pools, reusing the isolated B1 lease and usage implementation."""
from __future__ import annotations
from functools import wraps
import importlib.util
from pathlib import Path
import threading
from uuid import uuid4
from modelbench.big_budget_team_20260906.transport import CODING_GLM_ENDPOINT, CodingPlanTransport

SOURCE = Path(__file__).resolve().parents[1] / 'big_budget_team_20260906/provider.py'
_spec = importlib.util.spec_from_file_location('modelbench.big_budget_team_20260906._c1_provider_' + uuid4().hex, SOURCE)
_b1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_b1)
r = _b1.r
LIMITS = {'codex_account': 4, 'glm_coding_plan': 4, 'deepseek': 4}
MODEL_BUCKETS = _b1.MODEL_BUCKETS
POLICY_VERSION = 3


def policy():
    return {'provider_limits_version': 3, 'provider_limits_source': 'operator_configured',
            'provider_limits': dict(LIMITS), 'global_model_slots': 12, 'per_episode_model_slots': 4}


def validate_policy(value):
    if value != policy() or any(type(value.get(k)) is not int for k in
            ('provider_limits_version', 'global_model_slots', 'per_episode_model_slots')):
        raise r.ResourceError('C1 requires exactly global12/local4 and shared provider caps4/4/4')
    if any(type(v) is not int for v in value['provider_limits'].values()):
        raise r.ResourceError('C1 provider caps must be integers')
    return dict(LIMITS)


_b1._old.validate_policy = validate_policy


class ProviderGate(_b1.ProviderGate):
    def __init__(self, group_dir, group, value):
        validate_policy(value)
        directory = Path(group_dir).resolve()
        locks = Path(group['global_lock_dir']).resolve()
        if not locks.is_relative_to(directory):
            raise r.ResourceError('Provider locks must be in the single C1 group')
        if r.read(directory / 'group.json') != group:
            raise r.ResourceError('Persisted provider group mismatch')
        if r.read(group['policy_path']) != value or r.sha(group['policy_path']) != group['policy_sha256']:
            raise r.ResourceError('Persisted provider policy mismatch')
        self.shared_slots = r.FileSemaphore(locks / 'models', 12, group_dir=directory,
            group_sha256=r.sha(directory / 'group.json'), name='models', validate_leases=True)
        underlying = r.CombinedSemaphore(threading.BoundedSemaphore(4), self.shared_slots)
        _b1._old.ProviderGate.__init__(self, directory, group, value, underlying)


def install(group_dir, group, value, *, runner_module=None, run_class=None, transport_class=None):
    if transport_class is None:
        from .transport import AblationTransport
        transport_class = AblationTransport
    if not issubclass(transport_class, CodingPlanTransport):
        raise r.ResourceError('C1 transport must preserve the Coding Plan channel')
    if (runner_module is None) != (run_class is None):
        raise r.ResourceError('Both runner_module and run_class are required')
    if getattr(transport_class, '_new_provider_gate', None) is not None or (
            run_class is not None and getattr(run_class, '_new_provider_gate', None) is not None):
        raise r.ResourceError('Provider gate already installed in this process')
    gate = ProviderGate(group_dir, group, value)
    if run_class is not None:
        original_call, original_cm = run_class._call, run_class._cm_call
        @wraps(original_call)
        def call(self, handle, *args, **kwargs):
            with gate.route(handle.model, run_id=self.run_id, role=handle.role):
                return original_call(self, handle, *args, **kwargs)
        @wraps(original_cm)
        def cm(self, call_id, handle, *args, **kwargs):
            with gate.route(self.limits['cm_model'], run_id=self.run_id, role='cm'):
                return original_cm(self, call_id, handle, *args, **kwargs)
        run_class._call, run_class._cm_call = call, cm
        runner_module.MODEL_SLOTS = gate
        run_class._new_provider_gate = gate
    original_complete = transport_class.complete
    @wraps(original_complete)
    def complete(self, model, messages, **kwargs):
        return gate.complete(original_complete, self, model, messages, **kwargs)
    transport_class.complete = complete
    transport_class._new_provider_gate = gate
    gate.event('installed', limits=LIMITS, model_buckets=MODEL_BUCKETS,
        global_model_slots=12, per_episode_model_slots=4, glm_endpoint=CODING_GLM_ENDPOINT,
        automatic_retry=False, limit_semantics='operator_caps_not_provider_entitlement')
    return gate
