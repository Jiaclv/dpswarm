"""One shared resource group for the new experiment, preserving fenced leases."""
from __future__ import annotations

from functools import wraps
import importlib.util
from pathlib import Path
import threading
from uuid import uuid4

from .transport import GLM_ADAPTER, CODING_GLM_ENDPOINT, CodingPlanTransport

SOURCE = Path(__file__).resolve().parents[1] / 'minimal_value_20260905/operations/provider_limits.py'
_spec = importlib.util.spec_from_file_location('_big_budget_provider_' + uuid4().hex, SOURCE)
_old = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_old)
r = _old.r
POLICY_VERSION = 3
# Operator-selected caps, not provider-guaranteed Coding Plan concurrency.
LIMITS = {'codex_account': 4, 'glm_coding_plan': 4, 'deepseek': 4}
MODEL_BUCKETS = {'gpt-5.6-sol': 'codex_account', 'gpt-5.6-terra': 'codex_account',
                'gpt-5.6-luna': 'codex_account', 'glm-5.3': 'glm_coding_plan',
                'glm-5.3-flash': 'glm_coding_plan', 'deepseek-v4-flash': 'deepseek'}
ADAPTERS = {'codex_account': 'codex_text_tools_swe', 'glm_coding_plan': GLM_ADAPTER,
            'deepseek': 'deepseek_native_tools_swe'}
_old.MODEL_BUCKETS = MODEL_BUCKETS
_old.ADAPTERS = ADAPTERS


def validate_policy(policy):
    limits = policy.get('provider_limits')
    if (type(policy.get('provider_limits_version')) is not int
            or policy.get('provider_limits_version') != POLICY_VERSION
            or policy.get('provider_limits_source') != 'operator_configured'
            or not isinstance(limits, dict) or limits != LIMITS
            or any(type(value) is not int for value in limits.values())
            or type(policy.get('global_model_slots')) is not int
            or policy.get('global_model_slots') != 16
            or type(policy.get('per_episode_model_slots')) is not int
            or policy.get('per_episode_model_slots') != 4):
        raise r.ResourceError('New experiment requires frozen model caps and global16/local4 policy')
    return dict(LIMITS)


_old.validate_policy = validate_policy
_old_limit_signal = _old.limit_signal


def limit_signal(record):
    # Only transport envelopes count. Never reinterpret task/model text.
    # The original record and business code remain untouched.
    code = str(record.get('provider_error_code', ''))
    signals = {'1113': 'account_billing_unavailable',
               '1302': 'account_rate_limit_1302',
               '1305': 'platform_overload_1305',
               '1308': 'coding_plan_window_quota_exhausted',
               '1309': 'coding_plan_expired',
               '1310': 'coding_plan_weekly_or_monthly_quota_exhausted',
               '1311': 'coding_plan_model_not_entitled',
               '1313': 'coding_plan_fair_use_limited'}
    if code in signals:
        return signals[code]
    return _old_limit_signal(record)


_old.limit_signal = limit_signal


class ProviderGate(_old.ProviderGate):
    def __init__(self, group_dir, group, policy):
        validate_policy(policy)
        directory = Path(group_dir).resolve()
        locks = Path(group['global_lock_dir']).resolve()
        if not locks.is_relative_to(directory):
            raise r.ResourceError('Provider locks must belong to the single resource group')
        if r.read(directory / 'group.json') != group:
            raise r.ResourceError('Resource group does not match persisted group.json')
        if r.read(group['policy_path']) != policy or r.sha(group['policy_path']) != group['policy_sha256']:
            raise r.ResourceError('Resource policy does not match its frozen file')
        self.shared_slots = r.FileSemaphore(locks / 'models', 16, group_dir=directory,
            group_sha256=r.sha(directory/'group.json'), name='models', validate_leases=True)
        underlying = r.CombinedSemaphore(threading.BoundedSemaphore(4), self.shared_slots)
        super().__init__(directory, group, policy, underlying)

    def complete(self, original, transport, model, messages, **kwargs):
        if model in ('glm-5.3', 'glm-5.3-flash') and (
                not isinstance(transport, CodingPlanTransport)
                or transport.glm_base_url + '/chat/completions' != CODING_GLM_ENDPOINT):
            self.trip('actual_provider_channel_mismatch')
            raise r.ResourceError('GLM transport is not the frozen Coding Plan channel')
        try:
            return super().complete(original, transport, model, messages, **kwargs)
        except BaseException as exc:
            # Leave immutable transport and budget evidence to their owners;
            # a raised local fault must still stop every later admission.
            if not r.stopped(self.directory):
                self.trip('transport_raised', error_type=type(exc).__name__,
                          call_id=kwargs.get('call_id'))
            raise

    def summary(self):
        return {**super().summary(), 'global': self.shared_slots.summary(),
                'per_episode_model_slots': 4, 'provider_limits_version': POLICY_VERSION,
                'glm_endpoint': CODING_GLM_ENDPOINT,
                'limit_semantics': 'operator_caps_not_provider_entitlement'}


def install(group_dir, group, policy, *, runner_module=None, run_class=None,
            transport_class=CodingPlanTransport):
    """Install once per fresh episode process, or return a gate for direct routing.

    runner_module must be the module owning inherited _call/_cm_call globals;
    run_class is the new BigBudgetRun, never the historical ValueRun class.
    Construction of each run must pass transport_factory=CodingPlanTransport.
    """
    if (runner_module is None) != (run_class is None):
        raise r.ResourceError('Both runner_module and run_class are required for method installation')
    if getattr(transport_class, '_new_provider_gate', None) is not None:
        raise r.ResourceError('Transport gate already installed in this process')
    if run_class is not None and getattr(run_class, '_new_provider_gate', None) is not None:
        raise r.ResourceError('Provider gate already installed in this episode process')
    gate = ProviderGate(group_dir, group, policy)
    if runner_module is not None:
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
               global_model_slots=16, per_episode_model_slots=4,
               glm_endpoint=CODING_GLM_ENDPOINT, automatic_retry=False,
               limit_semantics='operator_caps_not_provider_entitlement')
    return gate
