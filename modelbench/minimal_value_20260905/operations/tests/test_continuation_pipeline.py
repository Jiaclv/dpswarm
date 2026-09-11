"""Finite completion-observer tests: no models, Docker, scoring or A2 actions."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

REPO=Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:sys.path.insert(0,str(REPO))
from modelbench.minimal_value_20260905.operations import continuation_pipeline as p


@pytest.fixture
def rig(tmp_path):
    batch=tmp_path/'a1';batch.mkdir();directory=tmp_path/'observer'
    anchor=datetime(2026,9,5,3,35,43,tzinfo=timezone.utc)
    entries=[{'run_id':f'a1-{i:02}-T' if i==6 else f'a1-{i:02}-S','arm':'T' if i==6 else 'S'} for i in range(10)]
    manifest={'stage':'a1','scheduled_episodes':10,'schedule':entries,'resource_lock_path':str(tmp_path/'stage.lock')}
    state={'started_at':anchor.isoformat(),'completed_episodes':10,'completed_at':'done','stop_reason':None,'active_episodes':{}}
    p.write(batch/'manifest.json',manifest);p.write(batch/'state.json',state)
    for entry in entries:
        result={'run_id':entry['run_id'],'budget':{'total_tokens':100,'over_token_limit':False},
                'accounting_integrity':{'passed':True},'workers':[]}
        if entry['arm']=='T':
            result.update(workers_with_actual_calls=2,team_execution_valid=False,
                          workers=[{'worker_id':'worker-1','worker_role':'implementation','status':'blocked','delta_status':'present','delta_bytes':0},
                                   {'worker_id':'worker-2','worker_role':'test','status':'blocked','delta_status':'present','delta_bytes':1752}])
        p.write(batch/'results'/entry['run_id']/'episode_result.json',result)
    called=[]
    kwargs={'runtime':lambda _:(manifest,{'frozen':True},{'executor':'same'}),'alive':lambda _:False,
            'absent':lambda *args: {'confirmed':True},'now':lambda:anchor+timedelta(hours=2)}
    def validate(*args,**kw):called.append('gate');return {'passed':True}
    kwargs['validate']=validate
    return batch,directory,manifest,state,called,kwargs


def tree_hashes(root):
    return {str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob('*') if path.is_file()}


def test_gate_failure_is_saved_verbatim_without_modifying_a1_or_starting_a2(rig):
    batch,directory,manifest,state,called,kwargs=rig
    before=tree_hashes(batch)
    def fail(*args,**kw):called.append('gate');raise ValueError('A1 T did not start and deliver both workers')
    result=p.run(batch,directory,**(kwargs|{'validate':fail}))
    assert result['status']=='blocked' and result['phase']=='a1_gate_blocked'
    assert result['reason']=='A1 T did not start and deliver both workers'
    assert result['a2_started'] is result['qualification_started'] is False
    assert tree_hashes(batch)==before and called==['gate']
    evidence=p.read(directory/'a1-gate-evidence.json')
    workers=evidence['episodes'][6]
    assert workers['noncompleted_worker_ids']==['worker-1','worker-2']
    assert len(evidence['episode_result_hashes'])==10


def test_passing_gate_still_only_completes_observer(rig):
    batch,directory,manifest,state,called,kwargs=rig
    result=p.run(batch,directory,**kwargs)
    assert result['status']=='passed' and result['original_evidence_unchanged']
    assert result['a2_started'] is False and not (batch.parent/'a2').exists()


def test_observer_waits_until_controller_exits(rig):
    batch,directory,manifest,state,called,kwargs=rig
    checks=iter([True,False]);sleeps=[]
    result=p.run(batch,directory,**(kwargs|{'alive':lambda _:next(checks),'sleep':sleeps.append}))
    assert result['status']=='passed' and len(sleeps)==1


@pytest.mark.parametrize('defect',['incomplete','no_completed_at','active','lease','cleanup_unknown'])
def test_terminal_or_cleanup_defects_prevent_gate(rig,defect):
    batch,directory,manifest,state,called,kwargs=rig
    if defect=='incomplete':state['completed_episodes']=9
    if defect=='no_completed_at':state.pop('completed_at')
    if defect=='active':state['active_episodes']={'task':{}}
    if defect=='lease':Path(manifest['resource_lock_path']).touch()
    if defect=='cleanup_unknown':kwargs['absent']=lambda *a:{'confirmed':False}
    p.write(batch/'state.json',state)
    result=p.run(batch,directory,**kwargs)
    assert result['status']=='blocked' and called==[]


def test_original_deadline_never_resets_at_observer_launch(rig):
    batch,directory,manifest,state,called,kwargs=rig
    anchor=p.parse_time(state['started_at'])
    result=p.run(batch,directory,**(kwargs|{'now':lambda:anchor+timedelta(hours=97)}))
    assert result['reason']=='original_96_hour_package_deadline_elapsed' and called==[]


def test_changed_admission_anchor_is_rejected(rig):
    batch,directory,manifest,state,called,kwargs=rig
    result=p.run(batch,directory,package_started_at='2026-09-05T04:35:43Z',**kwargs)
    assert result['status']=='blocked' and 'anchor changed' in result['reason'] and called==[]


def test_gate_side_effect_or_concurrent_result_mutation_is_detected(rig):
    batch,directory,manifest,state,called,kwargs=rig
    def mutated(*args,**kw):
        p.write(batch/'results'/manifest['schedule'][0]['run_id']/'episode_result.json',{'changed':True})
        return {'passed':True}
    result=p.run(batch,directory,**(kwargs|{'validate':mutated}))
    assert result['status']=='blocked' and 'evidence changed' in result['reason']


def test_observer_stop_marker_does_not_mutate_original_stage(rig):
    batch,directory,manifest,state,called,kwargs=rig
    directory.mkdir();(directory/'STOP').touch();before=tree_hashes(batch)
    result=p.run(batch,directory,**kwargs)
    assert result['reason']=='observer_stop_requested' and tree_hashes(batch)==before and called==[]


def test_no_implicit_observer_resume(rig):
    batch,directory,manifest,state,called,kwargs=rig
    p.run(batch,directory,**kwargs)
    with pytest.raises(p.ObserverError,match='implicitly resumed'):
        p.run(batch,directory,**kwargs)


def test_readonly_absence_does_not_confuse_missing_with_docker_failure(tmp_path):
    cli=SimpleNamespace(_recorded_containers=lambda _: {'owned':'owner'})
    manifest={'schedule':[{'run_id':'task'}]}
    assert p.resource_absence(tmp_path,manifest,cli=cli,inspect=lambda *a:SimpleNamespace(returncode=1,stderr='No such container'))['confirmed']
    for output in [SimpleNamespace(returncode=0,stderr=''),SimpleNamespace(returncode=1,stderr='Docker unavailable')]:
        with pytest.raises(p.ObserverError,match='absence is unknown'):
            p.resource_absence(tmp_path,manifest,cli=cli,inspect=lambda *a:output)
