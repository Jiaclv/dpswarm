"""No-network tests for cooperative provider stop and Coding quota scheduling."""
from pathlib import Path
import pytest
from modelbench.big_budget_team_20260906 import execution as e
from modelbench.big_budget_team_20260906 import coding_quota as quota
from modelbench.big_budget_team_20260906.tests.test_execution import make_entries
from modelbench.big_budget_team_20260906.tests.test_execution_failures import harness, Process


def provider_stop(batch):
    e.atomic_json(batch/'group/provider-trip-fixture.json',{'at':'2026-09-06T00:00:00Z',
        'reason':'coding_quota_exhausted','signal':'coding_quota_exhausted'})
    (batch/'group/CANCEL').touch()


def test_provider_stop_allows_inflight_to_export_before_cleanup(harness):
    h=harness;h.manifest['cooperative_stop_grace_seconds']=1080
    class Natural(Process):
        def poll(self):
            self.polls+=1
            if self.polls>=3:
                h.generation();self.returncode=0
            return self.returncode
    process=Natural(None)
    def child(batch,snapshot,command,rid):
        h.started.append(command)
        if command=='generate':provider_stop(batch);return process
        result=e.read(h.directory/'episode_result.json');result.update(official_resolved=False,grading_pending=False)
        e.atomic_json(h.directory/'episode_result.json',result);return Process()
    h.monkeypatch.setattr(e,'start_child',child)
    h.monkeypatch.setattr(e,'kill_owned_process_tree',lambda process:pytest.fail('Provider stop killed before natural export'))
    result=e.run(h.batch)
    assert result['stop_reason']=='provider_stop:coding_quota_exhausted'
    assert h.started==['generate','grade'] and result['scored']==1
    assert e.read(h.directory/'generation_result.json')['cleanup_confirmed'] is True
    assert not (h.batch/'logs'/(h.entry['run_id']+'.termination.json')).exists()


def test_provider_grace_is_finite_and_preserves_initiating_reason(harness):
    h=harness;h.manifest['cooperative_stop_grace_seconds']=1080
    clock=[0.0]
    def now():clock[0]+=100;return clock[0]
    process=Process(None);killed=[]
    def child(batch,snapshot,command,rid):
        h.started.append(command);provider_stop(batch);return process
    def kill(process):killed.append(process.pid);process.returncode=15;return {'confirmed':True}
    h.monkeypatch.setattr(e.time,'monotonic',now)
    h.monkeypatch.setattr(e,'start_child',child);h.monkeypatch.setattr(e,'kill_owned_process_tree',kill)
    result=e.run(h.batch);state=e.read(h.batch/'state.json')
    assert len(killed)==1 and h.started==['generate']
    assert result['stop_reason']=='provider_stop:coding_quota_exhausted'
    assert 'cooperative_stop_grace_expired' in state['secondary_stop_reasons']
    assert 'generation_process_failed' in state['secondary_stop_reasons']
    termination=e.read(h.batch/'logs'/(h.entry['run_id']+'.termination.json'))
    assert termination['reason']=='cooperative_stop_grace_expired'
    assert termination['initiating_stop_reason']==result['stop_reason']
    assert result['scored']==0 and result['status']=='stopped_incomplete'


def test_resource_safety_trip_does_not_wait_for_provider_grace(harness):
    h=harness;process=Process(None);kills=[]
    def child(batch,snapshot,command,rid):
        e.atomic_json(batch/'group/TRIP.json',{'reason':'host_memory_threshold'})
        (batch/'group/CANCEL').touch();return process
    def kill(process):kills.append(process.pid);process.returncode=15;return {'confirmed':True}
    h.monkeypatch.setattr(e,'start_child',child);h.monkeypatch.setattr(e,'kill_owned_process_tree',kill)
    result=e.run(h.batch)
    assert result['stop_reason']=='resource_stop:host_memory_threshold'
    assert len(kills)==1 and process.polls<5


@pytest.fixture
def quota_harness(tmp_path,monkeypatch):
    batch=tmp_path/'batch';(batch/'group').mkdir(parents=True)
    state={'stop_reason':None,'admitted':0,'active':[]}
    manifest={'coding_quota_gate':{'enabled':True,'max_poll_sleep_seconds':60}}
    entries=make_entries()[:10]
    monkeypatch.setattr(e.freeze,'credential_environment',lambda repo:{'GLM_API_KEY':'fixture-quota-secret'})
    return batch,state,manifest,entries


def test_quota_authorizes_full_block_without_limiting_total_container_slots(quota_harness,monkeypatch):
    batch,state,manifest,entries=quota_harness
    assert sum(1+x['expected_workers'] for x in entries)>12
    monkeypatch.setattr(quota,'inspect_quota',lambda key,timeout_seconds:{'available':True})
    monkeypatch.setattr(quota,'assess_wave',lambda snapshot,wave:{'status':'ready','required_credits':len(wave),'next_reset_ms':None})
    selected=e._quota_wave(batch,state,manifest,entries,e.time.monotonic()+100)
    assert selected==entries and len(selected)==10
    assert not (batch/'admissions').exists()
    assert 'fixture-quota-secret' not in Path(state['quota_evidence']['path']).read_text(encoding='utf-8')


def test_quota_selects_longest_affordable_original_prefix(quota_harness,monkeypatch):
    batch,state,manifest,entries=quota_harness
    monkeypatch.setattr(quota,'inspect_quota',lambda key,timeout_seconds:{'available':True})
    monkeypatch.setattr(quota,'assess_wave',lambda snapshot,wave:{'status':'ready' if len(wave)<=3 else 'wait',
        'required_credits':len(wave),'next_reset_ms':999999})
    selected=e._quota_wave(batch,state,manifest,entries,e.time.monotonic()+100)
    assert selected==entries[:3] and state['quota_decision']['required_credits']==3


def test_quota_wait_has_no_admission_and_rechecks_only_at_reset(quota_harness,monkeypatch):
    batch,state,manifest,entries=quota_harness;clock=[100.0];queries=[];slept=[]
    monkeypatch.setattr(e.time,'time',lambda:clock[0]);monkeypatch.setattr(e.time,'monotonic',lambda:clock[0])
    def inspect(key,timeout_seconds):queries.append(clock[0]);return {'available':True,'ready':len(queries)>1}
    def assess(snapshot,wave):return {'status':'ready' if snapshot['ready'] else 'wait',
        'required_credits':len(wave),'next_reset_ms':220000}
    def sleep(seconds):
        assert not (batch/'admissions').exists() and state['admitted']==0
        slept.append(seconds);clock[0]+=seconds
    monkeypatch.setattr(quota,'inspect_quota',inspect);monkeypatch.setattr(quota,'assess_wave',assess)
    monkeypatch.setattr(e.time,'sleep',sleep)
    selected=e._quota_wave(batch,state,manifest,entries,400)
    assert selected==entries and queries==[100,220] and slept==[60,60]
    assert state['status']=='running' and state['stop_reason'] is None


def test_quota_wait_stops_at_finite_dispatch_deadline(quota_harness,monkeypatch):
    batch,state,manifest,entries=quota_harness;clock=[100.0]
    monkeypatch.setattr(e.time,'time',lambda:clock[0]);monkeypatch.setattr(e.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(e.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    monkeypatch.setattr(quota,'inspect_quota',lambda key,timeout_seconds:{'available':True})
    monkeypatch.setattr(quota,'assess_wave',lambda snapshot,wave:{'status':'wait','required_credits':100,'next_reset_ms':999999})
    assert e._quota_wave(batch,state,manifest,entries,130)==[]
    assert state['stop_reason']=='dispatch_deadline' and clock[0]==130
    assert not (batch/'admissions').exists()


def test_unavailable_quota_closes_admission_without_cancel_or_retry(quota_harness,monkeypatch):
    batch,state,manifest,entries=quota_harness;seen=[]
    def inspect(key,timeout_seconds):seen.append(1);return {'available':False,'reason':'quota_network_error'}
    monkeypatch.setattr(quota,'inspect_quota',inspect)
    assert e._quota_wave(batch,state,manifest,entries,e.time.monotonic()+100)==[]
    assert seen==[1] and state['stop_reason']=='coding_quota_unavailable'
    assert not (batch/'group/CANCEL').exists() and not (batch/'admissions').exists()
