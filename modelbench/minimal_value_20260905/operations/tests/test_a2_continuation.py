"""Continuation regressions use only fake budgets, processes, and resources."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "a2_continuation.py"
spec = importlib.util.spec_from_file_location("_continuation_fixture", PATH)
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    experiment = tmp_path / "experiment"
    ops, batch = experiment / "operations", experiment / "batches/a2"
    ops.mkdir(parents=True)
    batch.mkdir(parents=True)
    now = datetime(2026, 9, 5, 14, tzinfo=timezone.utc)
    monkeypatch.setattr(p, "HERE", ops)
    monkeypatch.setattr(p, "EXPERIMENT", experiment)
    monkeypatch.setattr(p, "utc", lambda: now)
    doc = ops / "plan.md"
    doc.write_text("Explicit asynchronous remaining-original-episodes authorization", encoding="utf-8")
    monkeypatch.setattr(p, "DOC", doc)
    source = ops / "source.py"
    source.write_text("# bound", encoding="utf-8")
    monkeypatch.setattr(p, "sources", lambda: {str(source): p.sha(source)})
    monkeypatch.setattr(p.controller, "source_identity", lambda: {"operator.py": "operator-sha"})
    schedule = [{"run_id": f"a2-{i:03d}", "arm": ["S","L","D","T","R2"][i%5],
                 "instance": {"instance_id": f"task-{i//5}"}} for i in range(80)]
    manifest = {"stage": "a2", "scheduled_episodes": 80, "schedule": schedule,
                "resource_lock_path": str(experiment / "lease"),
                "stage_limits": {"dispatch_seconds": 61*3600, "token_admission_sum": 48_000_000,
                                 "cost_stop_usd": 200}}
    p.write(batch / "manifest.json", manifest)
    @contextmanager
    def lease(path, _batch):
        path = Path(path)
        path.touch(exist_ok=False)
        try:
            yield {}
        finally:
            path.unlink()
    cli = SimpleNamespace(load_manifest=lambda _: p.read(batch/"manifest.json"),
        freeze=SimpleNamespace(verify_snapshot=lambda *a: None),
        validate_schedule=lambda *a: None, stage_lease=lease)
    monkeypatch.setattr(p, "controller_process", lambda *a, **k: None)
    monkeypatch.setattr(p.controller, "confirm_candidate_absence", lambda *a: [])
    def audit(_batch, entry, _cli):
        path = batch / "results" / entry["run_id"] / "episode_result.json"
        value = p.read(path)
        if value.get("corrupt"):
            raise p.ContinuationError("accounting corrupt")
        return {"entry": entry, "path": str(path), "sha256": p.sha(path), "known_cost_usd": 1.5,
                "accounting": {"calls": [{"call_id": entry["run_id"]}]}}
    monkeypatch.setattr(p.recovery, "audit_episode", audit)
    policy = {"version": 4, "execution_mode": "parallel_pilot", "max_parallel_episodes": 12,
              "global_model_slots": 8, "candidate_container_cap": 12, "candidate_cpus": 2,
              "candidate_memory": "1g", "grader_memory": "1g", "memory_admission_mode": "bounded_container_limits",
              "resource_profile": "resource-defaults.json", "episode_admission_mode": p.controller.ATOMIC_ADMISSION,
              "provider_limits": {"codex_account":4, "glm_coding":1, "deepseek":4},
              "provider_limits_source": "operator_configured", "provider_limits_version":1,
              "wave_watchdog_policy":"cancel_owned_inflight_at_finite_group_deadline",
              "manifest_sha256": p.sha(batch/"manifest.json")}
    def group_files(group, ids):
        pol = policy | {"run_ids": ids}
        p.write(group/"policy.json",pol)
        val = {"batch":str(batch),"manifest_sha256":p.sha(batch/"manifest.json"),
               "operations_sources":p.controller.source_identity(), "policy_path":str(group/"policy.json"),
               "policy_sha256":p.sha(group/"policy.json"), "run_ids":ids, "global_lock_dir":str(group/"global-locks"),
               "watchdog_seconds":27000}
        p.write(group/"group.json",val)
        digest=p.sha(group/"group.json")
        events=[]
        for i, call in enumerate(ids):
            for name in ("provider_acquired","transport_entered","transport_returned","provider_released"):
                events.append({"group_sha256":digest,"event":name,"pid":42,"request_token":str(i),"call_id":call})
        path=group/"provider-events/process-42.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in events)+"\n",encoding="utf-8")
        return val
    def finish(group, count, ids):
        for entry in schedule[:count]:
            target=batch/"results"/entry["run_id"]/"episode_result.json"
            if not target.exists():
                p.write(target, {"run_id":entry["run_id"]})
        state={"stage":"a2","started_at":(now-timedelta(hours=3)).isoformat(),
               "package_started_at":(now-timedelta(hours=12)).isoformat(),
               "completed_episodes":count,"token_admission_sum":count*600000,"known_cost_usd":count*1.5,
               "episodes":[{"run_id":e["run_id"],"path":str(batch/"results"/e["run_id"]/"episode_result.json"),
                            "sha256":p.sha(batch/"results"/e["run_id"]/"episode_result.json")} for e in schedule[:count]],
               "completed_at":now.isoformat(),"active_episodes":{},
               "stop_reason":None if count==80 else "parallel_trial_complete_review_required",
               "parallel_trial":{"group_dir":str(group),"status":"completed_awaiting_review",
                   "cleanup_confirmed":True,"terminal_episodes":len(ids),"completed_count":len(ids)}}
        p.write(batch/"state.json",state)
        p.write(batch/"launch.json", {"pid":876543,"started_at":now.isoformat(),"parallel_group_dir":str(group),
                                   "operations_sources":p.controller.source_identity()})
    first=ops/"existing-wave"
    ids=[e["run_id"] for e in schedule[:12]]
    group_files(first,ids)
    finish(first,12,ids)
    run_dir=ops/"continue"
    plan=p.inspect(batch,run_dir,cli=cli)
    p.write(run_dir/"plan.json",plan)
    def checked(_batch, group, **kwargs):
        state=p.read(batch/"state.json")
        assert state["stop_reason"] is None and "completed_at" not in state
        return {"run_ids":p.read(group/"policy.json")["run_ids"]}
    monkeypatch.setattr(p.controller,"inspect_trial",checked)
    def headroom(path,*a,**k):
        p.write(path,{"ready":True})
        return {"ready":True}
    return SimpleNamespace(**locals())


def test_inspect_preserves_evidence_and_both_deadlines(rig):
    assert rig.plan["initial_completed"]==12
    assert p.timestamp(rig.plan["stage_deadline"])==rig.now+timedelta(hours=58)
    assert p.timestamp(rig.plan["package_deadline"])==rig.now+timedelta(hours=84)
    assert rig.plan["deadline"]==rig.plan["stage_deadline"]
    assert p.read(rig.batch/"state.json")["token_admission_sum"]==7_200_000


def test_prepare_archives_exact_prefix_and_never_resets_anchors(rig):
    before=(rig.batch/"state.json").read_bytes()
    result=p.prepare_next_wave(rig.plan,cli=rig.cli,headroom=rig.headroom)
    group=Path(result["group_dir"])
    state=p.read(rig.batch/"state.json")
    assert (group/"before/state.json").read_bytes()==before
    assert result["run_ids"]==[e["run_id"] for e in rig.schedule[12:24]]
    assert state["token_admission_sum"]==7_200_000 and state["known_cost_usd"]==18
    assert state["started_at"]==rig.plan["stage_started_at"]
    assert state["package_started_at"]==rig.plan["package_started_at"]
    assert not (rig.batch/"results"/rig.schedule[12]["run_id"]).exists()


@pytest.mark.parametrize("defect",["source","stop","trip","cost","tokens","result","partial","slots","providertrip","eventmissing"])
def test_stop_corruption_or_partial_attempt_never_advances(rig,defect):
    state=p.read(rig.batch/"state.json")
    if defect=="source": rig.source.write_text("changed",encoding="utf-8")
    elif defect=="stop": (rig.first/"STOP").touch()
    elif defect=="trip": (rig.first/"CANCEL").touch()
    elif defect=="cost": state["known_cost_usd"]+=1
    elif defect=="tokens": state["token_admission_sum"]+=600000
    elif defect=="result": state["episodes"][0]["sha256"]="wrong"
    elif defect=="partial": (rig.batch/"results"/rig.schedule[12]["run_id"]).mkdir()
    elif defect=="slots":
        path=rig.first/"global-locks/providers/glm/slot-0.lock"
        path.parent.mkdir(parents=True);path.touch()
    elif defect=="providertrip": p.write(rig.first/"provider-trip-1.json",{"reason":"http429"})
    else:
        path=rig.first/"provider-events/process-42.jsonl"
        lines=path.read_text().splitlines()
        path.write_text("\n".join(lines[:-1])+"\n")
    p.write(rig.batch/"state.json",state)
    before=(rig.batch/"state.json").read_bytes()
    with pytest.raises(p.ContinuationError):
        p.prepare_next_wave(rig.plan,cli=rig.cli,headroom=rig.headroom)
    assert (rig.batch/"state.json").read_bytes()==before


def test_stop_arriving_during_headroom_does_not_clear_stage(rig):
    before=(rig.batch/"state.json").read_bytes()
    def headroom(*a,**k):
        (rig.first/"STOP").touch()
        return {"ready":True}
    with pytest.raises(p.ContinuationError,match="STOP"):
        p.prepare_next_wave(rig.plan,cli=rig.cli,headroom=headroom)
    assert (rig.batch/"state.json").read_bytes()==before


@pytest.mark.parametrize("boundary",["time","cost","tokens"])
def test_original_budget_or_deadline_stops_before_mutation(rig,boundary):
    if boundary=="time":
        rig.plan["deadline"]=(rig.now+timedelta(minutes=30)).isoformat()
    else:
        manifest=p.read(rig.batch/"manifest.json")
        manifest["stage_limits"]["cost_stop_usd" if boundary=="cost" else "token_admission_sum"]=18 if boundary=="cost" else 7_200_000
        p.write(rig.batch/"manifest.json",manifest)
        rig.plan["manifest_sha256"]=p.sha(rig.batch/"manifest.json")
        # Keep the old-group identity valid for this artificial budget fixture.
        val=p.read(rig.first/"group.json");val["manifest_sha256"]=rig.plan["manifest_sha256"]
        pol=p.read(rig.first/"policy.json");pol["manifest_sha256"]=rig.plan["manifest_sha256"]
        p.write(rig.first/"policy.json",pol);val["policy_sha256"]=p.sha(rig.first/"policy.json")
        old=p.sha(rig.first/"group.json");p.write(rig.first/"group.json",val)
        path=rig.first/"provider-events/process-42.jsonl"
        path.write_text(path.read_text().replace(old,p.sha(rig.first/"group.json")))
    before=(rig.batch/"state.json").read_bytes()
    with pytest.raises(p.ContinuationError):
        p.prepare_next_wave(rig.plan,cli=rig.cli,headroom=rig.headroom)
    assert (rig.batch/"state.json").read_bytes()==before


def test_provider_http429_envelope_and_missing_accounted_call_stop(rig):
    path=rig.first/"provider-events/process-42.jsonl"
    values=[json.loads(x) for x in path.read_text().splitlines()]
    values[2]["rate_limit_signal"]="http_429"
    path.write_text("\n".join(json.dumps(x) for x in values))
    with pytest.raises(p.ContinuationError,match="rate limit"):
        p.audit_terminal(rig.plan,rig.first,rig.cli)


def test_attach_running_wave_waits_without_repreparing(rig,monkeypatch):
    new=rig.ops/"observer"
    before=(rig.batch/"state.json").read_bytes()
    monkeypatch.setattr(p,"controller_process",lambda *a,**k:SimpleNamespace(pid=100,create_time=lambda:123))
    plan=p.inspect(rig.batch,new,cli=rig.cli)
    assert plan["observed_active_controller"]["pid"]==100
    assert "initial_audit" not in plan
    assert (rig.batch/"state.json").read_bytes()==before and not new.exists()


def test_all_remaining_68_dispatch_in_contiguous_waves_then_stop_at_80(rig):
    launches=[]
    def prepare(plan,**kwargs):
        return p.prepare_next_wave(plan,headroom=rig.headroom,**kwargs)
    def launch(plan,group,tracked):
        ids=p.read(group/"policy.json")["run_ids"]
        launches.append(ids)
        rig.group_files(group,ids)
        count=p.read(rig.batch/"state.json")["completed_episodes"]+len(ids)
        rig.finish(group,count,ids)
        return {"pid":42}
    result=p.run(rig.plan,cli=rig.cli,preparer=prepare,launcher=launch)
    assert result["status"]=="completed", result
    assert [len(x) for x in launches]==[12,12,12,12,12,8]
    assert sum(launches,[])==[e["run_id"] for e in rig.schedule[12:]]
    state=p.read(rig.batch/"state.json")
    assert state["completed_episodes"]==80 and state["token_admission_sum"]==48_000_000
    assert state["known_cost_usd"]==120 and state["started_at"]==rig.plan["stage_started_at"]
    assert not (rig.batch/"continuation.lock").exists()


def test_failed_existing_wave_does_not_launch_and_cleans_even_dead_controller(rig):
    called=[]
    def wait(*a,**k): raise p.ContinuationError("controller exited without terminal results")
    result=p.run(rig.plan,cli=rig.cli,waiter=wait,launcher=lambda *a:pytest.fail("launch"),
                 aborter=lambda *a,**k:called.append("cleanup") or {"confirmed":True})
    assert result["status"]=="stopped" and called==["cleanup"]
    assert p.read(rig.batch/"state.json")["completed_episodes"]==12


@pytest.mark.parametrize("write_number",[4,5])
def test_post_popen_persistence_failure_stops_owned_wave(rig,monkeypatch,write_number):
    counter=[0];cleaned=[]
    original=p.write
    def flaky(path,value):
        if Path(path).name=="continuation_state.json":
            counter[0]+=1
            if counter[0]==write_number: raise OSError("projection write failed")
        return original(path,value)
    monkeypatch.setattr(p,"write",flaky)
    def prepare(plan,**kwargs):
        return p.prepare_next_wave(plan,headroom=rig.headroom,**kwargs)
    def launch(plan,group,tracked):
        ids=p.read(group/"policy.json")["run_ids"]
        rig.group_files(group,ids)
        tracked.update(pid=999,parallel_group_dir=str(group))
        return {"pid":999}
    def waiter(plan,group,cli):
        if group!=rig.first: raise p.ContinuationError("fake wait failure")
        return p.audit_terminal(plan,group,cli)
    result=p.run(rig.plan,cli=rig.cli,preparer=prepare,launcher=launch,waiter=waiter,
                 aborter=lambda *a,**k:cleaned.append(k.get("descriptor")) or {"confirmed":True})
    assert result["status"]=="stopped" and len(cleaned)==1 and cleaned[0]["pid"]==999


def test_source_drift_stops_observer_without_new_admission(rig):
    rig.source.write_text("changed")
    result=p.run(rig.plan,cli=rig.cli,launcher=lambda *a:pytest.fail("launch"),
                 aborter=lambda *a,**k:{"confirmed":True})
    assert result["status"]=="stopped" and "source/input" in result["error"]["message"]


def test_second_continuation_cannot_take_writer_lease(rig):
    with p.continuation_lease(rig.batch,rig.run_dir):
        with pytest.raises(FileExistsError):
            with p.continuation_lease(rig.batch,rig.run_dir): pass


def test_registration_write_failure_kills_owned_supervisor_without_stop_file(rig,monkeypatch):
    new=rig.ops/"new-launch"
    plan=rig.plan | {"run_dir":str(new)}
    monkeypatch.setattr(p,"inspect",lambda *a,**k:plan)
    killed=[]; spawned=[]
    class Gone(Exception): pass
    class Proc:
        pid=981234
        def create_time(self): return rig.now.timestamp()
        def cmdline(self): return spawned[0]
        def children(self,recursive=True): return []
        def kill(self): killed.append(self.pid)
    proc=Proc()
    fake=SimpleNamespace(Process=lambda pid:proc,NoSuchProcess=Gone,
                         wait_procs=lambda items,timeout:(items,[]))
    monkeypatch.setitem(sys.modules,"psutil",fake)
    original=p.write
    def bad_write(path,value):
        if Path(path)==new/"launch.json" or Path(path).name=="CANCEL":
            raise OSError("filesystem unavailable")
        return original(path,value)
    monkeypatch.setattr(p,"write",bad_write)
    def popen(argv,**kwargs):
        spawned.append(argv)
        assert kwargs["env"]["PYTHONIOENCODING"]=="utf-8"
        return proc
    with pytest.raises(OSError,match="filesystem unavailable"):
        p.launch(rig.batch,new,popen=popen)
    assert killed==[proc.pid] and len(spawned)==1
    assert not (new/"CANCEL").exists()


def test_child_cannot_dispatch_before_durable_supervisor_registration(rig):
    clock=[0]
    def sleep(seconds): clock[0]+=seconds
    with pytest.raises(p.ContinuationError,match="registration never"):
        p.await_registration(rig.plan,sleep=sleep,clock=lambda:clock[0])
    assert not list(rig.run_dir.glob("wave-*"))


def test_stop_immediately_before_wave_launch_blocks_popen(rig,monkeypatch):
    prepared=p.prepare_next_wave(rig.plan,cli=rig.cli,headroom=rig.headroom)
    (rig.first/"STOP").touch()
    with pytest.raises(p.ContinuationError,match="STOP"):
        p.launch_wave(rig.plan,Path(prepared["group_dir"]),{},
                      popen=lambda *a,**k:pytest.fail("should not start"))
