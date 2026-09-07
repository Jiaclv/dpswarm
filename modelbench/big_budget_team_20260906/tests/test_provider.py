"""Offline shared lease tests; no model or external API calls."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from modelbench.big_budget_team_20260906 import provider as p
from modelbench.big_budget_team_20260906.transport import CodingPlanTransport


@pytest.fixture
def group(tmp_path):
    policy = {'provider_limits_version': p.POLICY_VERSION, 'provider_limits_source': 'operator_configured',
              'provider_limits': dict(p.LIMITS), 'global_model_slots': 16,
              'per_episode_model_slots': 4}
    pp = tmp_path/'policy.json'; p.r.atomic_json(pp, policy)
    group = {'global_lock_dir': str(tmp_path/'locks'), 'policy_path': str(pp),
             'policy_sha256': p.r.sha(pp)}
    p.r.atomic_json(tmp_path/'group.json', group)
    return tmp_path, group, policy


def hold(stack, group, model):
    gate = p.ProviderGate(*group)
    stack.enter_context(gate.route(model, run_id='fixture', role='worker'))
    assert gate.acquire(timeout=0)
    stack.callback(gate.release)
    return gate


def test_codex_models_share_four_and_both_glm_models_share_four(group):
    with ExitStack() as stack:
        for model in ['gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.6-sol']:
            hold(stack, group, model)
        gate = p.ProviderGate(*group)
        with gate.route('gpt-5.6-luna', run_id='fifth', role='lead'):
            assert not gate.acquire(timeout=.01)
        for _ in range(4):
            hold(stack, group, 'glm-5.3')
        with gate.route('glm-5.3', run_id='fifth-glm', role='worker'):
            assert not gate.acquire(timeout=.01)
        with gate.route('glm-5.3-flash', run_id='fifth-flash', role='worker'):
            assert not gate.acquire(timeout=.01)
    assert not list(group[0].rglob('*.lock'))


def test_global_sixteen_shared_semaphore_also_guards_provider_acquisition(group):
    # Current provider caps sum to 12. Fill global leases directly to exercise
    # the independently frozen global ceiling and provider-acquire rollback.
    with ExitStack() as stack:
        for _ in range(16):
            occupied = p.ProviderGate(*group)
            assert occupied.shared_slots.acquire(timeout=0)
            stack.callback(occupied.shared_slots.release)
        gate = p.ProviderGate(*group)
        with gate.route('deepseek-v4-flash', run_id='cm', role='cm'):
            assert not gate.acquire(timeout=.01)
        assert not list((group[0]/'locks/providers/deepseek').glob('*.lock'))
    assert not list(group[0].rglob('*.lock'))


def test_one_episode_is_limited_to_four_calls(group):
    gate = p.ProviderGate(*group)
    barrier = threading.Barrier(5)
    release = threading.Event()
    def running():
        with gate.route('glm-5.3-flash', run_id='one', role='worker'):
            assert gate.acquire(timeout=3)
            barrier.wait(timeout=3)
            assert release.wait(timeout=3)
            gate.release()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(running) for _ in range(4)]
        barrier.wait(timeout=3)
        try:
            with gate.route('glm-5.3-flash', run_id='one', role='cm'):
                assert not gate.acquire(timeout=.02)
        finally:
            release.set()
        for job in jobs:
            job.result()
    assert not list(group[0].rglob('*.lock'))


@pytest.mark.parametrize('field,value', [('global_model_slots', 17),
    ('per_episode_model_slots', 5), ('provider_limits_version', 1),
    ('global_model_slots', 16.0), ('per_episode_model_slots', 4.0)])
def test_changed_caps_rejected(group, field, value):
    policy = deepcopy(group[2]); policy[field] = value
    with pytest.raises(p.r.ResourceError):
        p.validate_policy(policy)


@pytest.mark.parametrize('code,signal', [('1302', 'account_rate_limit_1302'),
                                       ('1305', 'platform_overload_1305'),
    ('1113', 'account_billing_unavailable'),
    ('1308', 'coding_plan_window_quota_exhausted'),
    ('1309', 'coding_plan_expired'),
    ('1310', 'coding_plan_weekly_or_monthly_quota_exhausted'),
    ('1311', 'coding_plan_model_not_entitled'),
    ('1313', 'coding_plan_fair_use_limited')])
def test_limit_response_preserved_but_stops_next_admission(group, code, signal):
    gate = p.ProviderGate(*group)
    record = {'provider_error_code': code, 'error': {'code': 'provider_rejected'},
              'adapter_mode': p.GLM_ADAPTER, 'transport_attempt_count': 1, 'total_tokens': None}
    calls = []
    def original(*args, **kwargs):
        calls.append(kwargs['call_id']); return record
    transport = CodingPlanTransport(group[0]/'fixture-calls')
    with gate.route('glm-5.3-flash', run_id='one', role='worker'):
        assert gate.acquire(timeout=0)
        assert gate.complete(original, transport, 'glm-5.3-flash', [], call_id='one') is record
        gate.release()
    with pytest.raises(p.r.ResourceError, match='stopped'):
        with gate.route('glm-5.3', run_id='two', role='worker'):
            pytest.fail('admitted after provider rejection')
    assert calls == ['one'] and record['total_tokens'] is None
    assert record['provider_error_code'] == code
    trip = p.r.read(next(group[0].glob('provider-trip-*.json')))
    assert trip['signal'] == signal and trip['automatic_retry'] is False


def test_coding_plan_channel_is_required_before_any_dispatch(group):
    gate = p.ProviderGate(*group)
    with pytest.raises(p.r.ResourceError, match='Coding Plan'):
        gate.complete(lambda *a, **k: pytest.fail('external dispatch'), None,
                      'glm-5.3', [], call_id='bad')
    assert (group[0]/'CANCEL').exists()


def test_dead_process_lease_is_never_reclaimed(group):
    script = '''import os,sys
from pathlib import Path
from modelbench.big_budget_team_20260906 import provider as p
d=Path(sys.argv[1]);g=p.r.read(d/'group.json');policy=p.r.read(g['policy_path'])
gate=p.ProviderGate(d,g,policy)
with gate.route('gpt-5.6-sol',run_id='dead',role='lead'):
    assert gate.acquire(timeout=2)
    os._exit(0)
'''
    child = subprocess.run([sys.executable, '-B', '-c', script, str(group[0])],
        cwd=REPO, capture_output=True, text=True, timeout=12)
    assert child.returncode == 0, child.stderr
    gate = p.ProviderGate(*group)
    with gate.route('gpt-5.6-terra', run_id='next', role='worker'):
        with pytest.raises(p.r.ResourceError, match='owner exited'):
            gate.acquire(timeout=0)
    assert (group[0]/'CANCEL').exists()
    assert list((group[0]/'locks/providers').rglob('*.lock'))


def test_model_text_cannot_trigger_provider_limit():
    assert p.limit_signal({'text': '429 and 1302 and 1305'}) is None
    assert p.limit_signal({'http_status': 429}) == 'http_429'


def test_float_provider_capacity_is_rejected(group):
    policy = deepcopy(group[2]); policy['provider_limits']['glm_coding_plan'] = 4.0
    with pytest.raises(p.r.ResourceError):
        p.validate_policy(policy)


def test_install_wraps_inherited_ordinary_and_cm_calls_only(group):
    runner_module = SimpleNamespace(MODEL_SLOTS=None)
    seen = []
    class FakeTransport(CodingPlanTransport):
        def complete(self, model, messages, **kwargs):
            seen.append((model, kwargs['call_id']))
            return {'adapter_mode': p.ADAPTERS[p.MODEL_BUCKETS[model]],
                    'transport_attempt_count': 1, 'total_tokens': 123}
    class ParentRun:
        run_id = 'fixture-run'
        limits = {'cm_model': 'deepseek-v4-flash'}
        def _call(self, handle, *args, **kwargs):
            assert runner_module.MODEL_SLOTS.acquire(timeout=0)
            try:
                return self.transport.complete(handle.model, [], call_id='ordinary')
            finally:
                runner_module.MODEL_SLOTS.release()
        def _cm_call(self, call_id, handle, *args, **kwargs):
            assert runner_module.MODEL_SLOTS.acquire(timeout=0)
            try:
                return self.transport.complete(self.limits['cm_model'], [], call_id=call_id)
            finally:
                runner_module.MODEL_SLOTS.release()
    class ChildRun(ParentRun):
        pass
    original_call, original_cm = ParentRun._call, ParentRun._cm_call
    gate = p.install(*group, runner_module=runner_module, run_class=ChildRun,
                     transport_class=FakeTransport)
    run = ChildRun(); run.transport = FakeTransport(group[0]/'calls')
    handle = SimpleNamespace(model='glm-5.3-flash', role='implementer')
    assert run._call(handle)['total_tokens'] == 123
    assert run._cm_call('cm-once', handle)['total_tokens'] == 123
    assert seen == [('glm-5.3-flash', 'ordinary'), ('deepseek-v4-flash', 'cm-once')]
    events = [json.loads(line) for line in gate.event_path.read_text(encoding='utf-8').splitlines()]
    assert [(v['model'], v['role']) for v in events if v['event'] == 'transport_entered'] == [
        ('glm-5.3-flash', 'implementer'), ('deepseek-v4-flash', 'cm')]
    assert ParentRun._call is original_call and ParentRun._cm_call is original_cm
    assert not hasattr(CodingPlanTransport, '_new_provider_gate')
    assert not list(group[0].rglob('*.lock'))
    with pytest.raises(p.r.ResourceError, match='already installed'):
        p.install(*group, runner_module=runner_module, run_class=ChildRun,
                  transport_class=FakeTransport)


def test_uncaught_local_transport_fault_stops_admission(group):
    gate = p.ProviderGate(*group)
    def raises(*args, **kwargs):
        raise OSError('fixture local failure')
    with gate.route('gpt-5.6-sol', run_id='one', role='lead'):
        assert gate.acquire(timeout=0)
        try:
            with pytest.raises(OSError, match='fixture'):
                gate.complete(raises, None, 'gpt-5.6-sol', [], call_id='failed-call')
        finally:
            gate.release()
    assert (group[0]/'CANCEL').exists()
    trip = p.r.read(next(group[0].glob('provider-trip-*.json')))
    assert trip['reason'] == 'transport_raised' and trip['call_id'] == 'failed-call'
    assert not list(group[0].rglob('*.lock'))


def test_standard_api_endpoint_is_rejected_even_on_coding_transport(group):
    gate = p.ProviderGate(*group)
    transport = CodingPlanTransport(group[0]/'fixture')
    transport.glm_base_url = 'https://open.bigmodel.cn/api/paas/v4'
    with pytest.raises(p.r.ResourceError, match='Coding Plan'):
        gate.complete(lambda *a, **k: pytest.fail('unexpected external dispatch'),
                      transport, 'glm-5.3', [], call_id='bad-channel')


def test_legacy_independent_model_caps_are_rejected(group):
    policy = deepcopy(group[2])
    policy['provider_limits'] = {'codex_account': 4, 'glm_53_api': 4,
                                 'glm_flash_api': 12, 'deepseek': 4}
    with pytest.raises(p.r.ResourceError):
        p.validate_policy(policy)
