"""Versioned recovery tests run the real existing wave loop with fake resources."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PATH=Path(__file__).resolve().parents[1]/"a2_recovery.py"
spec=importlib.util.spec_from_file_location("_a2_recovery_fixture",PATH)
p=importlib.util.module_from_spec(spec);sys.modules[spec.name]=p;spec.loader.exec_module(p)


class FakeProcess:
    pid=800001
    def poll(self):return 0


@pytest.fixture
def rig(tmp_path,monkeypatch):
    exp=tmp_path/"experiment";ops=exp/"operations";old=exp/"batches/old";batch=exp/"batches/new"
    for path in (ops,old,batch):path.mkdir(parents=True)
    now=datetime(2026,9,6,0,tzinfo=timezone.utc)
    monkeypatch.setattr(p,"HERE",ops);monkeypatch.setattr(p,"EXPERIMENT",exp)
    monkeypatch.setattr(p.cont,"HERE",ops);monkeypatch.setattr(p,"utc",lambda:now)
    source=ops/"operator.py";source.write_text("# fixed",encoding="utf-8")
    monkeypatch.setattr(p,"operator_sources",lambda:{str(source):p.sha(source)})
    monkeypatch.setattr(p.controller,"source_identity",lambda:{"operator.py":p.sha(source)})
    monkeypatch.setattr(p.controller,"profile_binding",lambda policy:{"profile_path":None,"profile_sha256":None})
    monkeypatch.setattr(p.recovery,"previous_controller_status",lambda batch:{"stopped":True})
    monkeypatch.setattr(p.cont,"controller_process",lambda *a,**k:None)
    monkeypatch.setattr(p.controller,"confirm_candidate_absence",lambda *a:[])
    schedule=[{"run_id":f"a2-{i:03d}","arm":["S","L","D","T","R2"][i%5],
               "instance":{"instance_id":f"task-{i//5}"}} for i in range(80)]
    manifest={"stage":"a2","scheduled_episodes":80,"schedule":schedule,
              "runtime_sources":{},"input_artifacts":{},"transport_environment":{"frozen":True},
              "resource_lock_path":str(exp/"stage.lock"),"stage_limits":{"token_admission_sum":48_000_000,
               "cost_stop_usd":480,"dispatch_seconds":61*3600}}
    p.write(old/"manifest.json",manifest)
    carried=[]
    for entry in schedule[:12]:
        path=old/"results"/entry["run_id"]/"episode_result.json"
        p.write(path,{"valid":True})
        carried.append({"run_id":entry["run_id"],"path":str(path),"sha256":p.sha(path)})
    auth,carry=ops/"authorization.json",ops/"carryover.json"
    p.write(auth,{"approved":"B"});p.write(carry,{"historical_failed":6,"unknown":3})
    unknown=[{"call_id":f"legacy-{i}","reserved_tokens":n} for i,n in enumerate((40000,40000,38387))]
    value={"version":1,"source_batch":str(old),"source_manifest_sha256":p.sha(old/"manifest.json"),
           "carried_valid":carried,"pending_run_ids":[e["run_id"] for e in schedule[12:]],
           "legacy_token_admission_sum":10_800_000,"legacy_known_cost_usd":20.6406,
           "legacy_unknown_calls":unknown,"legacy_unknown_reserved_tokens":118387,
           "total_admission_cap":51_600_000,"cost_stop_usd":480,
           "stage_started_at":(now-timedelta(hours=5)).isoformat(),
           "package_started_at":(now-timedelta(hours=13)).isoformat(),
           "authorization_path":str(auth),"authorization_sha256":p.sha(auth),
           "carryover_path":str(carry),"carryover_sha256":p.sha(carry)}
    p.write(carry,{"status":"PASS","old_valid_count":12,"old_infra_invalid_count":6,
        "references":{},"carried_valid12":carried,
        **{key:value[key] for key in ("legacy_token_admission_sum","legacy_known_cost_usd",
                                     "legacy_unknown_calls","legacy_unknown_reserved_tokens")}})
    value["carryover_sha256"]=p.sha(carry)
    p.write(batch/"recovery-plan.json",value)
    manifest={**manifest,"stage_limits":{**manifest["stage_limits"],"token_admission_sum":51_600_000},
              "recovery_plan_sha256":p.sha(batch/"recovery-plan.json")}
    p.write(batch/"manifest.json",manifest)
    state={"stage":"a2","started_at":value["stage_started_at"],"package_started_at":value["package_started_at"],
           "completed_episodes":0,"episodes":[],"token_admission_sum":10_800_000,
           "known_cost_usd":20.6406,"stop_reason":None,"active_episodes":{}}
    p.write(batch/"state.json",state)
    p.write(ops/"pricing.json",{})
    cleaned=[];strict_unknown=set()
    def complete(entry):
        directory=batch/"results"/entry["run_id"]
        patch=directory/"model.patch"
        patch.parent.mkdir(parents=True,exist_ok=True)
        patch.write_text("immutable patch\n",encoding="utf-8")
        account={"cost_computable":True,"api_equivalent_known_subtotal_usd":.25,
                 "api_equivalent_usd":.25,"calls":[{"call_id":entry["run_id"]}]}
        p.write(directory/"account.json",account)
        result={"run_id":entry["run_id"],"arm":entry["arm"],"instance_id":entry["instance"]["instance_id"],
                "artifact":{"path":str(patch),"sha256":p.sha(patch)},"quiesced":True,"cleanup_confirmed":True,
                "accounting":account,"score":{"completed":True,"resolved":False},
                "budget":{"unknown_call_count":0,"pending_call_count":0}}
        p.write(directory/"episode_result.json",result)
    reporting=SimpleNamespace(accounting=lambda directory,card:p.read(Path(directory)/"account.json"),
        audit_accounting=lambda directory,result,account:{"passed":result["accounting"]==account},
        result_status=lambda result,entry:{"artifact_execution_integrity":True},
        stop_reason=lambda result:"infrastructure" if result.get("infrastructure_error") else None)
    @contextmanager
    def lease(path,_batch):
        path=Path(path);path.touch(exist_ok=False)
        value={"retain":False}
        try:yield value
        finally:
            if not value["retain"]:path.unlink()
    raw=SimpleNamespace(HERE=ops,reporting=reporting,load_manifest=lambda batch:p.read(Path(batch)/"manifest.json"),
        freeze=SimpleNamespace(verify_snapshot=lambda *a:None),validate_schedule=lambda *a:True,
        stage_lease=lease,cleanup_owned_episode=lambda directory:cleaned.append(str(directory)) or {"confirmed":True})
    cli=p.CliView(raw)
    monkeypatch.setattr(p.recovery,"load_frozen_cli",lambda batch:raw)
    monkeypatch.setattr(p.recovery,"child_environment",lambda *a,**k:{"PYTHONIOENCODING":"utf-8"})
    def strict(directory,paths,*,run_id,pricing_path):
        return {"passed":run_id not in strict_unknown,"new_unknown_calls":[run_id] if run_id in strict_unknown else [],
                "known_api_equivalent_usd":.25}
    monkeypatch.setattr(p,"accounting_module",lambda:SimpleNamespace(audit_new_attempt=strict))
    root=ops/"auto"
    plan=p.inspect(batch,root,cli=cli)
    p.write(root/"plan.json",plan)
    def headroom(*a,**k):return {"ready":True}
    def prepare():return p.prepare_wave(plan,cli,headroom=headroom)
    def bind(group):
        inspection=p.inspect_wave(batch,group,cli=cli)
        inspection["operations_sources"]=p.controller.source_identity()
        keys=("batch","run_ids","manifest_sha256","operations_sources","global_model_slots","candidate_container_cap",
              "candidate_requirements","episode_admission_mode","max_parallel_episodes","provider_limits",
              "provider_limits_source","provider_limits_version","wait_timeout_seconds","grader_wait_timeout_seconds",
              "watchdog_seconds","policy_path","policy_sha256")
        definition={k:inspection[k] for k in keys}
        definition.update(version=1,global_lock_dir=str(group/"global-locks"))
        p.write(group/"group.json",definition)
        inspection["group_sha256"]=p.sha(group/"group.json")
        p.write(group/"controller-launch/plan.json",inspection)
        p.write(batch/"launch.json",{"pid":800000,"started_at":now.isoformat(),"parallel_group_dir":str(group),
                                   "operations_sources":p.controller.source_identity()})
        return inspection
    def execute(group):
        bound=bind(group);admitted=[];ticks=[0]
        def spawn(argv,**kwargs):
            run_id=argv[argv.index("--run-id")+1]
            admitted.append(run_id)
            assert run_id not in [e["run_id"] for e in schedule[:12]]
            complete(next(e for e in schedule if e["run_id"]==run_id))
            p.write(group/"candidates"/(run_id+".json"),{"run_id":run_id,"group_sha256":bound["group_sha256"],
                "phase":"candidate_closed","cleanup_confirmed":True})
            return FakeProcess()
        def sleep(_):ticks[0]+=.1
        guard=SimpleNamespace(tick=lambda:{},start=lambda:None,stop=lambda:{"stopped":True,"tripped":False})
        def run(batch,group,cli):
            return p.controller.run_group(batch,group,cli=cli,now=lambda:now,clock=lambda:ticks[0],sleep=sleep,
                popen=spawn,guard_factory=lambda _:guard,observe_tree=lambda _:[],
                stop_tree=lambda *a:{"confirmed":True},candidate_absence=lambda *a:[])
        return p.run_wave(batch,group,cli=cli,runner=run),admitted
    return SimpleNamespace(**locals())


def test_inspect_zero_new_valid_keeps_legacy_admission_and_unknown(rig):
    assert rig.plan["initial_token_admission_sum"]==10_800_000
    assert rig.plan["initial_known_cost_usd"]==20.6406
    assert len(rig.plan["legacy_unknown_calls"])==3
    assert p.read(rig.batch/"state.json")["completed_episodes"]==0
    assert not (rig.batch/"results").exists()
    assert rig.plan["deadline"]==(rig.now+timedelta(hours=56)).isoformat()


def test_real_existing_wave_loop_preserves_legacy_and_counts_only_new_valid(rig):
    group=rig.prepare()
    before={x["path"]:Path(x["path"]).read_bytes() for x in rig.carried}
    result,ids=rig.execute(group)
    assert result["status"]=="completed_awaiting_review",result
    state=p.read(rig.batch/"state.json")
    assert ids==rig.value["pending_run_ids"][:12]
    assert state["completed_episodes"]==12 and state["token_admission_sum"]==18_000_000
    assert state["known_cost_usd"]==pytest.approx(23.6406)
    assert state["started_at"]==rig.value["stage_started_at"]
    report=p.read(rig.batch/"recovery-report.json")
    assert report["new_valid_episodes"]==12 and report["logical_valid_total"]==24
    assert report["historical_infrastructure_invalid_attempts"]==6
    assert report["total_cost_computable"] is False and len(report["legacy_unknown_calls"])==3
    assert all(Path(path).read_bytes()==content for path,content in before.items())


def test_all_68_use_existing_loop_in_six_waves_without_repeating_legacy(rig):
    sizes=[];all_ids=[]
    for expected in (12,12,12,12,12,8):
        group=rig.prepare()
        # frozen launch normally archives/removes the dead controller lock.
        lock=rig.batch/"controller.lock"
        if lock.exists():lock.unlink()
        result,ids=rig.execute(group)
        assert result["status"]=="completed_awaiting_review",result
        assert len(ids)==expected
        terminal=p.terminal_wave(rig.plan,group,rig.cli)
        sizes.append(len(ids));all_ids+=ids
    state=p.read(rig.batch/"state.json")
    assert all_ids==rig.value["pending_run_ids"]
    assert len(set(all_ids))==68 and state["completed_episodes"]==68
    assert state["token_admission_sum"]==51_600_000
    assert state["known_cost_usd"]==pytest.approx(37.6406)
    report=p.read(rig.batch/"recovery-report.json")
    assert report["logical_valid_total"]==80 and report["new_admitted_attempts"]==68
    assert report["historical_admitted_attempts"]==18 and report["recovery_complete"]


@pytest.mark.parametrize("defect",["legacysha","legacyunknown","resetclock","resetadmission","partial","cost","source","stop","cap"])
def test_invalid_recovery_never_admits(rig,defect):
    state=p.read(rig.batch/"state.json")
    if defect=="legacysha":Path(rig.carried[0]["path"]).write_text("changed")
    elif defect=="legacyunknown":
        value=p.read(rig.batch/"recovery-plan.json");value["legacy_unknown_calls"]=[]
        p.write(rig.batch/"recovery-plan.json",value)
    elif defect=="resetclock":state["started_at"]=rig.now.isoformat()
    elif defect=="resetadmission":state["token_admission_sum"]=0
    elif defect=="partial":(rig.batch/"results"/rig.value["pending_run_ids"][0]).mkdir(parents=True)
    elif defect=="cost":state["known_cost_usd"]=0
    elif defect=="source":rig.source.write_text("changed")
    elif defect=="stop":(rig.batch/"CANCEL").touch()
    else:
        manifest=p.read(rig.batch/"manifest.json");manifest["stage_limits"]["token_admission_sum"]=99_000_000
        p.write(rig.batch/"manifest.json",manifest)
    p.write(rig.batch/"state.json",state)
    with pytest.raises(p.cont.ContinuationError):
        rig.prepare()
    assert not list(rig.root.glob("wave-*"))


def test_new_unknown_stops_the_reused_wave_without_refunding(rig):
    rig.strict_unknown.add(rig.value["pending_run_ids"][0])
    group=rig.prepare()
    result,ids=rig.execute(group)
    assert result["status"]=="failed"
    state=p.read(rig.batch/"state.json")
    assert state["token_admission_sum"]==10_800_000+len(ids)*600000
    assert rig.value["pending_run_ids"][0] not in [x["run_id"] for x in state["episodes"]]
    with pytest.raises(p.cont.ContinuationError):
        rig.prepare()


def test_original_deadline_blocks_whole_wave(rig,monkeypatch):
    monkeypatch.setattr(p,"utc",lambda:rig.now+timedelta(hours=55))
    with pytest.raises(p.cont.ContinuationError,match="window"):
        rig.prepare()
    assert not list(rig.root.glob("wave-*"))


def test_supervisor_stops_after_68_and_declares_86_attempts(rig,monkeypatch):
    results=iter([12,24,36,48,60,68])
    def prepare(plan,cli):
        group=rig.root/("fake-"+str(len(list(rig.root.glob("fake-*")))))
        group.mkdir()
        return group
    result=p.supervise(rig.plan,cli=rig.cli,preparer=prepare,
        launcher=lambda *a:{"pid":42},
        waiter=lambda *a:{"new_valid_episodes":next(results),"cleanup_confirmed":True})
    assert result["status"]=="completed" and result["logical_valid_total"]==80
    assert result["total_admitted_attempts"]==86 and len(result["waves"])==6


def test_post_launch_persistence_failure_calls_owned_cleanup(rig,monkeypatch):
    calls=[];writes=[0];original=p.write
    def flaky(path,value):
        if Path(path).name=="recovery-state.json":
            writes[0]+=1
            if writes[0]==2:raise OSError("disk write error")
        return original(path,value)
    monkeypatch.setattr(p,"write",flaky)
    def launch(plan,group,tracked):
        tracked["pid"]=999;return {"pid":999}
    result=p.supervise(rig.plan,cli=rig.cli,
        preparer=lambda plan,cli:rig.root/"fake-wave",launcher=launch,
        waiter=lambda *a:pytest.fail("must stop before wait"),
        aborter=lambda *a,**k:calls.append(k["descriptor"]) or {"confirmed":True})
    assert result["status"]=="stopped" and calls==[{"pid":999}]


def test_previous_recovery_stop_during_headroom_prevents_next_transition(rig):
    first=rig.prepare();rig.execute(first)
    before=(rig.batch/"state.json").read_bytes()
    def headroom(*a,**k):
        (first/"STOP").touch()
        return {"ready":True}
    with pytest.raises(p.cont.ContinuationError,match="stopped"):
        p.prepare_wave(rig.plan,rig.cli,headroom=headroom)
    assert (rig.batch/"state.json").read_bytes()==before


def test_historical_ledger_cannot_change_unknown_to_zero(rig):
    value=p.read(rig.batch/"recovery-plan.json")
    value["legacy_unknown_calls"][0]["reserved_tokens"]=0
    p.write(rig.batch/"recovery-plan.json",value)
    manifest=p.read(rig.batch/"manifest.json");manifest["recovery_plan_sha256"]=p.sha(rig.batch/"recovery-plan.json")
    p.write(rig.batch/"manifest.json",manifest)
    with pytest.raises(p.cont.ContinuationError,match="historical ledger"):
        p.contract(rig.batch,rig.cli)


def test_real_launcher_redirects_only_controller_script_and_binds_new_sources(rig):
    group=rig.prepare();seen=[];tracked={}
    def spawn(argv,**kwargs):
        seen.append(list(argv))
        assert kwargs["env"]["PYTHONIOENCODING"]=="utf-8"
        return FakeProcess()
    descriptor=p.launch_wave(rig.plan,group,tracked,popen=spawn)
    assert len(seen)==1 and seen[0][2]==str(p.SELF) and seen[0][3]=="_run"
    assert descriptor["argv"]==seen[0] and tracked["argv"]==seen[0]
    bound=p.read(group/"controller-launch/plan.json")
    assert bound["recovery_sources"]==p.operator_sources()
    assert bound["recovery_plan_sha256"]==rig.plan["recovery_plan_sha256"]
    assert p.read(group/"group.json")["operations_sources"]==p.controller.source_identity()
    assert not (rig.batch/"results").exists()


def test_actual_launcher_checks_prior_stop_after_expensive_inspection(rig,monkeypatch):
    group=rig.prepare()
    transition=p.read(group/"transition.json")
    previous=rig.root/"previous";previous.mkdir()
    transition["previous_group"]=str(previous);p.write(group/"transition.json",transition)
    original=p.inspect_wave
    def inspected(*a,**k):
        result=original(*a,**k)
        (previous/"STOP").touch()
        return result
    monkeypatch.setattr(p,"inspect_wave",inspected)
    with pytest.raises(p.cont.ContinuationError,match="stopped"):
        p.launch_wave(rig.plan,group,{},popen=lambda *a,**k:pytest.fail("must not Popen"))


def test_source_change_between_prepare_and_execution_stops_before_loop(rig):
    group=rig.prepare();rig.bind(group)
    rig.source.write_text("changed")
    with pytest.raises(p.cont.ContinuationError,match="source changed"):
        p.run_wave(rig.batch,group,cli=rig.cli,runner=lambda *a,**k:pytest.fail("must not execute"))
