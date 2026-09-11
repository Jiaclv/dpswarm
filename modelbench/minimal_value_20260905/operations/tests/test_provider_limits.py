"""Offline provider admission tests, including independent real OS processes."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tested_provider_limits', ROOT/'provider_limits.py')
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
r = p.r


@pytest.fixture
def group(tmp_path):
    policy = {'provider_limits_version': 1, 'provider_limits_source': 'operator_configured',
              'provider_limits': {'codex_account': 2, 'glm_coding': 1, 'deepseek': 4}}
    pp = tmp_path/'policy.json'; r.atomic_json(pp, policy)
    group = {'global_lock_dir': str(tmp_path/'locks'), 'policy_path': str(pp), 'policy_sha256': r.sha(pp)}
    r.atomic_json(tmp_path/'group.json', group)
    return tmp_path, group, policy


def gate(group, capacity=3):
    directory, value, policy = group
    return p.ProviderGate(directory, value, policy,
        r.FileSemaphore(directory/'locks'/'models', capacity, group_dir=directory, name='models'))


def test_all_gpt_models_share_account_bucket_and_other_providers_are_independent(group):
    assert {p.MODEL_BUCKETS[name] for name in ('gpt-5.6-sol','gpt-5.6-luna','gpt-5.6-terra')} == {'codex_account'}
    a, b, c, d = [gate(group) for _ in range(4)]
    with a.route('gpt-5.6-sol', run_id='one', role='lead'):
        assert a.acquire(timeout=0)
        with b.route('gpt-5.6-terra', run_id='two', role='worker'):
            assert b.acquire(timeout=0)
            with c.route('gpt-5.6-luna', run_id='three', role='lead'):
                assert not c.acquire(timeout=.04)
                with d.route('glm-5.3', run_id='four', role='worker'):
                    # The queued GPT request holds no global slot.
                    assert d.acquire(timeout=0)
                    d.release()
            b.release()
        a.release()
    assert not list(group[0].rglob('*.lock'))


def test_real_processes_bound_shared_gpt_account_without_serializing_glm(group):
    script = '''import importlib.util,json,sys,time
from pathlib import Path
s=importlib.util.spec_from_file_location('limiter',sys.argv[1]);p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
d=Path(sys.argv[2]);g=p.r.read(d/'group.json');policy=p.r.read(g['policy_path'])
gate=p.ProviderGate(d,g,policy,p.r.FileSemaphore(d/'locks'/'models',3,group_dir=d,name='models'))
model=sys.argv[3]
with gate.route(model,run_id=model,role='worker'):
    assert gate.acquire(timeout=8)
    start=time.time();time.sleep(.45);end=time.time()
    gate.release()
print(json.dumps({'model':model,'start':start,'end':end}))
'''
    processes = [subprocess.Popen([sys.executable,'-B','-c',script,str(ROOT/'provider_limits.py'),str(group[0]),model],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for model in ('gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','glm-5.3')]
    records = []
    for child in processes:
        out, err = child.communicate(timeout=15)
        assert child.returncode == 0, err
        records.append(json.loads(out))
    events = sorted([(v['start'],1,v['model']) for v in records] + [(v['end'],-1,v['model']) for v in records])
    all_active=gpt_active=all_peak=gpt_peak=0
    for at, delta, model in events:
        all_active += delta
        gpt_active += delta if model.startswith('gpt-') else 0
        all_peak=max(all_peak,all_active);gpt_peak=max(gpt_peak,gpt_active)
    assert gpt_peak == 2 and all_peak == 3
    assert not list(group[0].rglob('*.lock'))
    logged = [json.loads(line) for path in (group[0]/'provider-events').glob('*.jsonl') for line in path.read_text().splitlines()]
    assert sum(row['event']=='provider_acquired' for row in logged)==4
    assert sum(row['event']=='provider_released' for row in logged)==4
    assert any(row.get('provider_queue_seconds',0)>.1 for row in logged)


def test_real_dead_owner_stops_group_without_reclaiming_lease(group):
    script = '''import importlib.util,sys,os
from pathlib import Path
s=importlib.util.spec_from_file_location('limiter',sys.argv[1]);p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
d=Path(sys.argv[2]);g=p.r.read(d/'group.json');policy=p.r.read(g['policy_path'])
gate=p.ProviderGate(d,g,policy,p.r.FileSemaphore(d/'locks'/'models',3,group_dir=d,name='models'))
with gate.route('gpt-5.6-sol',run_id='dead',role='lead'):
    assert gate.acquire(timeout=1)
    os._exit(0)
'''
    child = subprocess.run([sys.executable,'-B','-c',script,str(ROOT/'provider_limits.py'),str(group[0])],
                           capture_output=True,text=True,timeout=10)
    assert child.returncode==0, child.stderr
    observer=gate(group)
    with observer.route('gpt-5.6-terra',run_id='next',role='lead'):
        with pytest.raises(r.ResourceError,match='owner exited'):
            observer.acquire(timeout=0)
    assert (group[0]/'CANCEL').exists()
    assert list((group[0]/'locks'/'providers').rglob('*.lock'))
    assert list(group[0].glob('provider-trip-*.json'))


@pytest.mark.parametrize('record,want', [
    ({'http_status':429},'http_429'),
    ({'error':{'code':'rate_limit_exceeded'}},'explicit_provider_limit'),
    ({'provider_error_events':[{'type':'error','message':'Too many requests'}]},'explicit_provider_limit'),
    ({'error':{'message':'达到并发限制'}},'explicit_provider_limit'),
    ({'assistant_message':{'content':'HTTP 429 rate limit'},'text':'rate_limit_exceeded'},None),
    ({'protocol_error':{'message':'argument contains 429'},'error':None},None),
    ({'http_status':500,'error':{'message':'server error'}},None),
])
def test_limit_detection_uses_only_transport_envelopes(record,want):
    assert p.limit_signal(record)==want


def test_429_preserves_returned_record_stops_new_calls_and_never_retries(group):
    limiter=gate(group); calls=[]
    record={'http_status':429,'error':{'code':'rate_limit_exceeded'},'transport_attempt_count':1,
            'adapter_mode':'codex_text_tools_swe'}
    def original(*args,**kwargs):calls.append(kwargs['call_id']);return record
    with limiter.route('gpt-5.6-sol',run_id='one',role='lead'):
        assert limiter.acquire(timeout=0)
        assert limiter.complete(original,None,'gpt-5.6-sol',[],call_id='call-1') is record
        limiter.release()
    with pytest.raises(r.ResourceError,match='stopped'):
        with limiter.route('gpt-5.6-luna',run_id='two',role='lead'):
            pytest.fail('new admission after 429')
    assert calls==['call-1'] and record['transport_attempt_count']==1
    diagnostic=r.read(next(group[0].glob('provider-trip-*.json')))
    assert diagnostic['reason']=='provider_rate_limit' and diagnostic['automatic_retry'] is False


def test_missing_transport_lease_fails_before_external_call(group):
    limiter=gate(group)
    with pytest.raises(r.ResourceError,match='does not match'):
        limiter.complete(lambda *a,**k:pytest.fail('transport called'),None,'glm-5.3',[],call_id='bad')
    assert (group[0]/'CANCEL').exists()


def test_install_covers_work_cm_selector_and_captured_r2_slots(monkeypatch,group):
    observations=[]
    class Transport:
        def complete(self,model,messages,**kwargs):
            observations.append((model,kwargs['role']))
            return {'adapter_mode':p.ADAPTERS[p.MODEL_BUCKETS[model]],'transport_attempt_count':1}
    class Run:
        run_id='run';limits={'cm_model':'deepseek-v4-flash'}
        transport=Transport()
        def _call(self,handle,*a,**k):
            assert runner.MODEL_SLOTS.acquire(timeout=0)
            try:return self.transport.complete(handle.model,[],call_id='work',role=handle.role)
            finally:runner.MODEL_SLOTS.release()
        def _cm_call(self,call_id,handle,*a,**k):
            assert runner.MODEL_SLOTS.acquire(timeout=0)
            try:return self.transport.complete(self.limits['cm_model'],[],call_id=call_id,role='cm')
            finally:runner.MODEL_SLOTS.release()
    class Selector:
        entry={'run_id':'run'};model_slots=None;transport=Transport()
        def run(self):
            assert self.model_slots.acquire(timeout=0)
            try:return self.transport.complete('gpt-5.6-sol',[],call_id='select',role='selector')
            finally:self.model_slots.release()
    runner=SimpleNamespace(ValueRun=Run,MODEL_SLOTS='old',CONTAINER_SLOTS='containers')
    r2=SimpleNamespace(MODEL_SLOTS='captured-old',CONTAINER_SLOTS='captured-containers')
    modules={'modelbench.minimal_value_20260905.selector':SimpleNamespace(RestrictedSelector=Selector),
             'modelbench.minimal_value_20260905.r2':r2,
             'modelbench.swe_verified_20260903.transport':SimpleNamespace(SweTransport=Transport)}
    monkeypatch.setattr(p.importlib,'import_module',lambda name:modules[name])
    directory,value,policy=group
    limiter=p.install(directory,value,policy,runner,threading.BoundedSemaphore(4))
    for model,role in [('gpt-5.6-sol','lead'),('gpt-5.6-terra','worker'),('glm-5.3','worker')]:
        Run()._call(SimpleNamespace(model=model,role=role))
    Run()._cm_call('cm',SimpleNamespace(model='gpt-5.6-sol',role='lead'))
    Selector().run()
    assert observations==[('gpt-5.6-sol','lead'),('gpt-5.6-terra','worker'),('glm-5.3','worker'),
                          ('deepseek-v4-flash','cm'),('gpt-5.6-sol','selector')]
    assert runner.MODEL_SLOTS is r2.MODEL_SLOTS is limiter
    assert r2.CONTAINER_SLOTS=='containers'


def test_forged_lease_identity_stops_group_and_is_not_removed(group):
    limiter=gate(group)
    directory=group[0]/'locks'/'providers'/'codex_account'
    path=directory/'slot-0.lock'
    r.atomic_json(path, {'pid':123, 'native_thread_id':123, 'token':'forged',
        'process_created_at':0, 'capacity':99, 'resource':'provider:codex_account',
        'group_sha256':limiter.group_hash})
    with limiter.route('gpt-5.6-sol',run_id='one',role='lead'):
        with pytest.raises(r.ResourceError,match='identity mismatch'):
            limiter.acquire(timeout=0)
    assert path.exists() and (group[0]/'CANCEL').exists()


def test_exited_thread_lease_is_not_reused_within_a_live_process(group):
    limiter=gate(group)
    slot=limiter.buckets['codex_account']
    worker=threading.Thread(target=lambda:slot.acquire(timeout=1))
    worker.start();worker.join(timeout=3)
    assert not worker.is_alive()
    with limiter.route('gpt-5.6-terra',run_id='next',role='lead'):
        with pytest.raises(r.ResourceError,match='owner exited'):
            limiter.acquire(timeout=0)
    assert (group[0]/'CANCEL').exists()


def test_stop_after_acquisition_passes_cancellation_to_frozen_transport(group):
    limiter=gate(group);observed=[]
    def original(transport,model,messages,**kwargs):
        observed.append(kwargs['cancel_event'].is_set())
        return {'transport_attempt_count':0, 'error':{'code':'cancelled'}}
    with limiter.route('gpt-5.6-sol',run_id='one',role='lead'):
        assert limiter.acquire(timeout=0)
        (group[0]/'CANCEL').touch()
        record=limiter.complete(original,None,'gpt-5.6-sol',[],call_id='cancelled-before-launch')
        limiter.release()
    assert observed==[True] and record['transport_attempt_count']==0
