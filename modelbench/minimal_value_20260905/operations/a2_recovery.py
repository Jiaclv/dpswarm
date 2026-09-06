"""Versioned A2 recovery B: 12 carried valid, 6 retained interruptions, 68 new attempts.

The existing wave execution loop is reused through an explicit inspection and
reporting adapter. state.episodes contains NEW valid results only. Admission and
known cost include historical attempts; legacy unknowns never become zero usage.
"""
from __future__ import annotations
import argparse
from datetime import timedelta
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
SELF = Path(__file__).resolve()
REVISION = "a2_recovery_b_v1"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


cont = module("_a2_recovery_continuation_helpers", HERE / "a2_continuation.py")
controller, recovery = cont.controller, cont.controller.recovery
read, write, sha, utc, require, timestamp = cont.read, cont.write, cont.sha, cont.utc, cont.require, cont.timestamp
BASE_CHILD_RESULT = recovery.child_result


def accounting_module():
    return module("_a2_recovery_evidence", HERE / "a2_recovery_accounting.py")


def operator_sources():
    paths = [HERE / name for name in controller.source_identity()]
    paths += [SELF, HERE / "a2_continuation.py", HERE / "a2_recovery_accounting.py", HERE / "idle_memory.py"]
    return {str(path): sha(path) for path in paths}


def verify_sources(expected):
    require(expected == operator_sources(), "Recovery operator source changed")


def contract(batch, cli, *, allow_stopped=False, full_evidence=True):
    batch = Path(batch).resolve()
    manifest, state = cli.load_manifest(batch), read(batch / "state.json")
    path = batch / "recovery-plan.json"
    value = read(path)
    require(value.get("version") == 1 and manifest.get("recovery_plan_sha256") == sha(path),
            "Versioned recovery plan is not bound by the manifest")
    source = Path(value["source_batch"]).resolve()
    require(source != batch and source.is_relative_to(EXPERIMENT / "batches"), "Recovery source batch is invalid")
    require(sha(source / "manifest.json") == value["source_manifest_sha256"], "Legacy manifest changed")
    old = read(source / "manifest.json")
    require(manifest.get("stage") == "a2" and manifest.get("scheduled_episodes") == 80
            and manifest["schedule"] == old["schedule"], "Original 80-episode schedule changed")
    require(manifest["runtime_sources"] == old["runtime_sources"]
            and manifest["input_artifacts"] == old["input_artifacts"]
            and manifest["transport_environment"] == old["transport_environment"], "Frozen solver/input/transport changed")
    if full_evidence:
        cli.freeze.verify_snapshot(batch, manifest)
        cli.validate_schedule(manifest["schedule"], "a2")
    require(value["total_admission_cap"] == 51_600_000 and value["legacy_token_admission_sum"] == 10_800_000
            and value["cost_stop_usd"] == 480, "Approved recovery B caps changed")
    require(manifest["stage_limits"]["token_admission_sum"] == 51_600_000
            and manifest["stage_limits"]["cost_stop_usd"] == 480
            and manifest["stage_limits"]["dispatch_seconds"] == 61*3600, "Recovery stage limits differ")
    require(state["started_at"] == value["stage_started_at"]
            and state["package_started_at"] == value["package_started_at"], "An original start clock was reset")
    for label in ("authorization", "carryover"):
        require(sha(value[label+"_path"]) == value[label+"_sha256"], "Bound "+label+" changed")
    evidence = read(value["carryover_path"])
    require(evidence.get("status")=="PASS" and evidence.get("old_valid_count")==12
            and evidence.get("old_infra_invalid_count")==6, "Historical recovery ledger is not qualified")
    if full_evidence:
        for source_path, digest in evidence.get("references",{}).items():
            require(sha(source_path)==digest, "Historical evidence changed after recovery preparation")
        if evidence.get("old_state_sha256"):
            require(sha(source/"state.json")==evidence["old_state_sha256"], "Historical state changed")
        if value.get("source_state_sha256"):
            require(sha(source/"state.json")==value["source_state_sha256"], "Bound historical state changed")
    for key in ("legacy_token_admission_sum","legacy_known_cost_usd","legacy_unknown_calls",
                "legacy_unknown_reserved_tokens"):
        require(value[key]==evidence[key], "Recovery plan differs from the independent historical ledger: "+key)
    require(value["carried_valid"]==evidence["carried_valid12"], "Carried valid references differ")
    carried = value["carried_valid"]
    require(len(carried) == 12 and len({x["run_id"] for x in carried}) == 12, "Carried valid identities differ")
    for item in carried:
        result = Path(item["path"]).resolve()
        require(result == source/"results"/item["run_id"]/"episode_result.json"
                and sha(result) == item["sha256"], "Carried result changed or escaped its original directory")
    ids = [entry["run_id"] for entry in manifest["schedule"]]
    require([item["run_id"] for item in carried] == ids[:12]
            and value["pending_run_ids"] == ids[12:] and len(value["pending_run_ids"]) == 68,
            "Recovery must retry exactly the interrupted six plus the unstarted sixty-two")
    require(len(value["legacy_unknown_calls"]) == 3 and value["legacy_unknown_reserved_tokens"] == 118387,
            "Legacy unknown calls must remain explicit")
    known = value["legacy_known_cost_usd"]
    require(type(known) in (int,float) and math.isfinite(known) and known > 0, "Legacy known cost is invalid")
    require(allow_stopped or not any((batch/marker).exists() for marker in ("STOP","CANCEL","TRIP.json")),
            "Recovery batch is stopped")
    return value, manifest, state


def deadline(value):
    return min(timestamp(value["stage_started_at"])+timedelta(hours=61),
               timestamp(value["package_started_at"])+timedelta(hours=96))


def public_summary(batch, cli):
    value, _, state = contract(batch, cli, allow_stopped=True)
    return {"revision": REVISION, "new_valid_episodes": state["completed_episodes"],
            "carried_valid_episodes": 12, "logical_valid_total": 12+state["completed_episodes"],
            "historical_admitted_attempts": 18, "historical_infrastructure_invalid_attempts": 6,
            "cumulative_token_admission_sum": state["token_admission_sum"],
            "new_admitted_attempts": (state["token_admission_sum"]-10_800_000)//600000,
            "cumulative_known_api_equivalent_usd": state["known_cost_usd"],
            "legacy_unknown_calls": value["legacy_unknown_calls"],
            "legacy_unknown_reserved_tokens": value["legacy_unknown_reserved_tokens"],
            "total_cost_computable": False, "cost_stop_applies_to_known_subtotal": True,
            "original_deadline": deadline(value).isoformat(),
            "new_results": state["episodes"], "carried_results": value["carried_valid"],
            "recovery_complete": state["completed_episodes"] == 68,
            "stop_reason": state.get("stop_reason"), "at": utc().isoformat()}


class ReportingView:
    def __init__(self, original, cli):
        self.original, self.cli = original, cli
    def __getattr__(self, name):
        return getattr(self.original, name)
    def write_report(self, batch):
        # Ordinary reporting assumes one batch contains all 80 results.
        # This explicit recovery report preserves the split and unknowns.
        value = public_summary(batch, self.cli)
        write(Path(batch)/"recovery-report.json", value)
        return value


class CliView:
    def __init__(self, original):
        self.original = original
        reporting = getattr(original,"reporting",None) or __import__(
            "modelbench.minimal_value_20260905.reporting",fromlist=["reporting"])
        self.reporting = ReportingView(reporting,self)
    def __getattr__(self,name):
        return getattr(self.original,name)


def audit_attempt(batch, entry, cli, group):
    audited = recovery.audit_episode(batch, entry, cli)
    extra = accounting_module().audit_new_attempt(Path(batch)/"results"/entry["run_id"],
        sorted((Path(group)/"provider-events").glob("*.jsonl")),run_id=entry["run_id"],
        pricing_path=Path(cli.HERE)/"pricing.json")
    require(extra.get("passed") is True and not extra.get("new_unknown_calls"),
            "New attempt has unknown or inconsistent transport accounting: "+entry["run_id"])
    require(math.isclose(audited["known_cost_usd"],extra["known_api_equivalent_usd"],rel_tol=0,abs_tol=1e-9),
            "New attempt cost audit disagrees")
    return audited


def prefix_audit(batch, cli):
    value, manifest, state = contract(batch,cli)
    count = state["completed_episodes"]
    require(type(count) is int and 0 <= count <= 68, "New valid count is invalid")
    entries = {entry["run_id"]:entry for entry in manifest["schedule"]}
    mapping = (state.get("recovery") or {}).get("run_groups",{})
    audited=[]
    for run_id in value["pending_run_ids"][:count]:
        require(run_id in mapping, "New result has no bound wave identity")
        group=Path(mapping[run_id])
        cont.group_identity(batch,group)
        audited.append(audit_attempt(batch,entries[run_id],cli,group))
    expected=[{"run_id":x["entry"]["run_id"],"path":x["path"],"sha256":x["sha256"]} for x in audited]
    require(state["episodes"] == expected, "New result SHA projection differs from its audited prefix")
    require(state["token_admission_sum"] == 10_800_000+count*600000,
            "An interrupted new attempt cannot be silently refunded or retried")
    known=value["legacy_known_cost_usd"]+sum(x["known_cost_usd"] for x in audited)
    require(math.isclose(state["known_cost_usd"],known,rel_tol=0,abs_tol=1e-9),
            "Cumulative known cost differs from carried plus new evidence")
    existing={path.name for path in (Path(batch)/"results").iterdir()} if (Path(batch)/"results").exists() else set()
    require(existing == set(value["pending_run_ids"][:count]), "Unsettled or unexpected new result directory")
    return value,manifest,state,expected,known


def prior_stop_guard(group):
    previous=read(Path(group)/"transition.json").get("previous_group")
    if previous:
        root=Path(previous)
        require(not any((root/name).exists() for name in ("STOP","CANCEL","TRIP.json"))
                and not list(root.glob("provider-trip-*.json")), "Previous recovery wave was stopped")


def inspect_wave(batch,group_dir,*,policy_path=None,cli=None,now=None,
                 process_check=None,check_previous=True):
    batch,group_dir=Path(batch).resolve(),Path(group_dir).resolve()
    cli=cli or CliView(recovery.load_frozen_cli(batch))
    value,manifest,state,prefix,known=prefix_audit(batch,cli)
    require(not state.get("active_run_id") and not state.get("active_episodes")
            and not state.get("completed_at") and not state.get("stop_reason"), "Recovery transition is not ready")
    policy_path=Path(policy_path or group_dir/"policy.json").resolve()
    policy=controller.policy_contract(read(policy_path))
    require(policy["max_parallel_episodes"]==12 and policy["candidate_memory"]=="1g"
            and policy.get("wave_watchdog_policy")=="cancel_owned_inflight_at_finite_group_deadline",
            "Recovery resources differ from the approved contract")
    for root in (batch,group_dir,Path(policy["recovery_run_dir"])):
        require(not any((root/n).exists() for n in ("STOP","CANCEL","TRIP.json")), "Recovery admission stopped")
    transition=state.get("parallel_transition") or {}
    record=Path(transition.get("record","")).resolve()
    require(transition.get("status")=="ready" and transition.get("transition_id")==group_dir.name
            and record.is_file() and record.is_relative_to(group_dir), "Recovery transition is unbound")
    require(read(record)["recovery_plan_sha256"] == sha(batch/"recovery-plan.json"), "Transition recovery plan changed")
    prior_stop_guard(group_dir)
    pending=value["pending_run_ids"][len(prefix):]
    ids=policy["run_ids"]
    require(1<=len(ids)<=12 and ids==pending[:len(ids)], "Wave is not the next original recovery identities")
    require(policy["manifest_sha256"]==sha(batch/"manifest.json"), "Wave manifest differs")
    require(state["token_admission_sum"]+len(ids)*600000<=51_600_000 and known<480,
            "Approved recovery admission/cost limit reached")
    require(not Path(manifest["resource_lock_path"]).exists(), "Shared stage lease remains")
    previous=None
    if check_previous and (batch/"launch.json").exists():
        previous=(process_check or recovery.previous_controller_status)(batch)
    elif check_previous:
        require(not prefix and not (batch/"controller.lock").exists(), "Prior recovery controller identity is missing")
        previous={"never_launched":True}
    lookup={e["run_id"]:e for e in manifest["schedule"]}
    requirements=controller.candidate_requirements([lookup[i] for i in ids])
    timing=controller.wave_time_limits(requirements,policy)
    current=now or utc()
    require((deadline(value)-current).total_seconds()>=timing["watchdog_seconds"]+300,
            "Original stage/package window cannot fit the entire recovery wave")
    return {"version":1,"batch":str(batch),"group_dir":str(group_dir),"stage":"a2",
        "manifest_sha256":sha(batch/"manifest.json"),"state_sha256":sha(batch/"state.json"),
        "policy_path":str(policy_path),"policy_sha256":sha(policy_path),
        "transition_record_path":str(record),"transition_record_sha256":sha(record),
        "completed_prefix":prefix,"run_ids":ids,"group_episode_count":len(ids),
        "candidate_peak_requirement":sum(requirements.values()),"candidate_requirements":requirements,
        "episode_admission_mode":policy["episode_admission_mode"],"provider_limits":policy["provider_limits"],
        "provider_limits_source":policy["provider_limits_source"],"provider_limits_version":policy["provider_limits_version"],
        "max_parallel_episodes":12,**timing,"wave_watchdog_policy":policy["wave_watchdog_policy"],
        "memory_admission_mode":policy["memory_admission_mode"],"prior_completed_episodes":len(prefix),
        "prior_token_admission_sum":state["token_admission_sum"],"prior_known_cost_usd":known,
        "original_started_at":state["started_at"],"original_dispatch_deadline":deadline(value).isoformat(),
        "remaining_dispatch_seconds":(deadline(value)-current).total_seconds(),"previous_controller":previous,
        "global_model_slots":8,"candidate_container_cap":12,**controller.profile_binding(policy),
        "recovery_sources":operator_sources(),"recovery_plan_sha256":sha(batch/"recovery-plan.json")}


def inspect(batch,run_dir,*,cli=None):
    batch,run_dir=Path(batch).resolve(),Path(run_dir).resolve()
    require(batch.is_relative_to(EXPERIMENT/"batches") and run_dir.is_relative_to(HERE),
            "Recovery paths are outside this experiment")
    require(not run_dir.exists() and not (batch/"continuation.lock").exists(), "Recovery supervisor already exists")
    cli=cli or CliView(recovery.load_frozen_cli(batch))
    value,manifest,state,prefix,known=prefix_audit(batch,cli)
    require(not prefix and not (batch/"launch.json").exists()
            and not state.get("active_episodes") and not state.get("stop_reason"), "Expected fresh versioned recovery batch")
    require(not Path(manifest["resource_lock_path"]).exists(), "Another stage still owns resources")
    require(utc()<deadline(value), "Original recovery window elapsed")
    return {"revision":REVISION,"batch":str(batch),"run_dir":str(run_dir),"sources":operator_sources(),
            "manifest_sha256":sha(batch/"manifest.json"),"recovery_plan_sha256":sha(batch/"recovery-plan.json"),
            "deadline":deadline(value).isoformat(),"stage_started_at":value["stage_started_at"],
            "package_started_at":value["package_started_at"],"initial_known_cost_usd":known,
            "initial_token_admission_sum":10_800_000,"new_attempt_count":68,
            "legacy_unknown_calls":value["legacy_unknown_calls"],"created_at":utc().isoformat()}


def verify_supervisor(plan,cli):
    require(plan["revision"]==REVISION,"Supervisor revision changed")
    verify_sources(plan["sources"])
    require(sha(Path(plan["batch"])/"manifest.json")==plan["manifest_sha256"]
            and sha(Path(plan["batch"])/"recovery-plan.json")==plan["recovery_plan_sha256"],"Bound recovery inputs changed")
    value,_,_=contract(plan["batch"],cli,full_evidence=False)
    require(deadline(value).isoformat()==plan["deadline"] and utc()<deadline(value), "Original deadline elapsed")
    require(not any((Path(plan["run_dir"])/n).exists() for n in ("STOP","CANCEL","TRIP.json")),
            "Recovery supervisor stopped")


def policy_for(plan,ids):
    return {"version":5,"execution_mode":"parallel_pilot","max_parallel_episodes":12,
        "global_model_slots":8,"per_episode_model_slots":4,"candidate_container_cap":12,
        "candidate_memory":"1g","candidate_cpus":2,"grader_memory":"1g","grader_max_containers":2,
        "grader_memory_per_container":"1g","auxiliary_memory":"768m","resource_profile":"resource-defaults.json",
        "memory_admission_mode":"bounded_container_limits","monitor_is_hard_limit":False,
        "episode_admission_mode":controller.ATOMIC_ADMISSION,"provider_limits":{"codex_account":4,"glm_coding":1,"deepseek":4},
        "provider_limits_source":"operator_configured","provider_limits_version":1,
        "wave_watchdog_policy":"cancel_owned_inflight_at_finite_group_deadline",
        "grading_mode":"all_candidates_quiescent_then_serial","run_ids":ids,"manifest_sha256":plan["manifest_sha256"],
        "softguard":{"sample_seconds":2,"owned_memory_limit_gib":8,"host_available_min_gib":2,
                     "docker_available_min_gib":3,"max_consecutive_sample_failures":2},
        "recovery_run_dir":plan["run_dir"],"recovery_plan_sha256":plan["recovery_plan_sha256"],
        "scope":"Approved recovery B: retain old 18 attempts, execute only remaining 68, no automatic retry",
        "package_started_at":plan["package_started_at"],"package_deadline":plan["deadline"]}


def prepare_wave(plan,cli,*,headroom=None):
    verify_supervisor(plan,cli)
    value,manifest,state,prefix,_=prefix_audit(plan["batch"],cli)
    require(not state.get("active_episodes"),"Prior wave remains active")
    if prefix:
        terminal_wave(plan,Path(read(Path(plan["batch"])/"launch.json")["parallel_group_dir"]),cli)
    ids=value["pending_run_ids"][len(prefix):len(prefix)+12]
    require(ids,"Recovery already complete")
    group=Path(plan["run_dir"])/("wave-%03d-%03d"%(len(prefix)+1,len(prefix)+len(ids)))
    policy=policy_for(plan,ids)
    lookup={e["run_id"]:e for e in manifest["schedule"]}
    timing=controller.wave_time_limits(controller.candidate_requirements([lookup[i] for i in ids]),policy)
    require((deadline(value)-utc()).total_seconds()>=timing["watchdog_seconds"]+300,"Not enough original window")
    group.mkdir(parents=True,exist_ok=False)
    before=sha(Path(plan["batch"])/"state.json")
    shutil.copyfile(Path(plan["batch"])/"state.json",group/"previous-state.json")
    require(sha(group/"previous-state.json")==before,"Recovery state changed during archival")
    helper=headroom or module("_recovery_idle",HERE/"idle_memory.py").ensure_idle_headroom
    require(helper(group/"idle-memory.json",manifest["resource_lock_path"],minimum_gib=4).get("ready") is True,
            "Idle headroom was not confirmed")
    verify_supervisor(plan,cli)
    if prefix:
        prior=Path(read(Path(plan["batch"])/"launch.json")["parallel_group_dir"])
        require(not any((prior/n).exists() for n in ("STOP","CANCEL","TRIP.json"))
                and not list(prior.glob("provider-trip-*.json")),"Previous wave stopped during preparation")
    write(group/"policy.json",policy)
    write(group/"transition.json",{"status":"ready","transition_id":group.name,
        "recovery_plan_sha256":plan["recovery_plan_sha256"],"prior_state_sha256":before,
        "new_valid_prefix":prefix,"legacy_admitted_attempts":18,
        "previous_group":str(prior) if prefix else None,"at":utc().isoformat()})
    with cli.stage_lease(manifest["resource_lock_path"],plan["batch"]):
        require(sha(Path(plan["batch"])/"state.json")==before,"Another writer changed recovery state")
        verify_supervisor(plan,cli)
        prior_stop_guard(group)
        updated=dict(state);updated.pop("completed_at",None);updated["stop_reason"]=None
        updated["recovery"]={**state.get("recovery",{}),"legacy_unknown_calls":value["legacy_unknown_calls"],
            "run_groups":{**state.get("recovery",{}).get("run_groups",{}),**{i:str(group) for i in ids}}}
        updated["parallel_transition"]={"status":"ready","transition_id":group.name,
            "record":str(group/"transition.json"),"group_dir":str(group),"policy_sha256":sha(group/"policy.json")}
        write(Path(plan["batch"])/"state.json",updated)
    inspect_wave(plan["batch"],group,cli=cli)
    return group


def terminal_wave(plan,group,cli):
    verify_supervisor(plan,cli)
    value,_,state,prefix,_=prefix_audit(plan["batch"],cli)
    require(cont.controller_process(plan["batch"],group) is None,"Wave controller still running")
    definition,_=cont.group_identity(plan["batch"],group)
    require(not any((group/n).exists() for n in ("STOP","CANCEL","TRIP.json"))
            and not list(group.glob("provider-trip-*.json")),"Wave has a stop/provider trip")
    trial=state.get("parallel_trial") or {}
    require(trial.get("status")=="completed_awaiting_review" and trial.get("cleanup_confirmed") is True
            and trial.get("completed_count")==len(definition["run_ids"]) and not trial.get("report_error"),
            "Wave did not finish with complete engineering/accounting evidence")
    require(Path(trial["group_dir"])==group and not state.get("active_episodes"),"Wave terminal identity is inconsistent")
    cont.check_idle(cli,plan["batch"],group,definition)
    return {"group_dir":str(group),"new_valid_episodes":len(prefix),"logical_valid_total":12+len(prefix),
            "token_admission_sum":state["token_admission_sum"],"known_cost_usd":state["known_cost_usd"],
            "cleanup_confirmed":True,"group_sha256":sha(group/"group.json"),"at":utc().isoformat()}


def launch_wave(plan,group,tracked,*,popen=subprocess.Popen):
    verify_supervisor(plan,CliView(recovery.load_frozen_cli(plan["batch"])))
    controller.inspect_trial=inspect_wave
    original=controller.subprocess
    def capture(argv,**kwargs):
        verify_sources(plan["sources"])
        prior_stop_guard(group)
        require(not any((Path(plan["run_dir"])/name).exists() or (Path(plan["batch"])/name).exists()
                        or (Path(group)/name).exists() for name in ("STOP","CANCEL","TRIP.json")),
                "Recovery stopped before Popen")
        import psutil
        argv[2]=str(SELF)  # _run installs this adapter before reusing run_group.
        process=popen(argv,**kwargs)
        tracked.update(pid=process.pid,argv=argv,started_at=utc().isoformat(),
            parallel_group_dir=str(group),operations_sources=controller.source_identity())
        try: tracked["created_at"]=psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess: require(process.poll() is not None,"Controller identity unavailable")
        write(group/"recovery-launch-intent.json",tracked)
        return process
    controller.subprocess=SimpleNamespace(Popen=capture,CREATE_NO_WINDOW=getattr(subprocess,"CREATE_NO_WINDOW",0))
    try:
        return controller.launch(plan["batch"],group)
    finally:
        controller.subprocess=original


def run_wave(batch,group,*,cli=None,runner=None):
    cli=cli or CliView(recovery.load_frozen_cli(batch))
    plan=read(Path(group)/"controller-launch/plan.json")
    verify_sources(plan["recovery_sources"])
    require(sha(Path(batch)/"recovery-plan.json")==plan["recovery_plan_sha256"],"Recovery contract changed")
    controller.inspect_trial=inspect_wave
    def child_result(_batch,entry,supervision,stderr,_cli):
        audited,explanation=BASE_CHILD_RESULT(_batch,entry,supervision,stderr,_cli)
        audit_attempt(_batch,entry,_cli,group)
        return audited,explanation
    recovery.child_result=child_result
    try:
        return (runner or controller.run_group)(batch,group,cli=cli)
    finally:
        recovery.child_result=BASE_CHILD_RESULT


def wait_wave(plan,group,cli,*,sleep=time.sleep):
    definition,_=cont.group_identity(plan["batch"],group)
    until=min(timestamp(plan["deadline"]),timestamp(read(Path(plan["batch"])/"launch.json")["started_at"])
              +timedelta(seconds=definition["watchdog_seconds"]+300))
    while True:
        verify_supervisor(plan,cli)
        require(not any((group/n).exists() for n in ("STOP","CANCEL","TRIP.json"))
                and not list(group.glob("provider-trip-*.json")),"Recovery wave stopped or rate-limited")
        process=cont.controller_process(plan["batch"],group)
        if process is None:return terminal_wave(plan,group,cli)
        tracked=plan.setdefault("_observed_descendants",{}).setdefault(str(group),{})
        for child in process.children(recursive=True):
            try:tracked[child.pid]={"pid":child.pid,"created_at":child.create_time()}
            except Exception:pass
        require(utc()<until,"Recovery wave finite watchdog expired")
        sleep(2)


def supervise(plan,*,cli=None,preparer=prepare_wave,launcher=launch_wave,waiter=wait_wave,aborter=cont.abort_owned):
    cli=cli or CliView(recovery.load_frozen_cli(plan["batch"]))
    root=Path(plan["run_dir"])
    state={"revision":REVISION,"status":"starting","waves":[],"deadline":plan["deadline"],
           "legacy_unknown_calls":plan["legacy_unknown_calls"],"automatic_retry_allowed":False}
    group,tracked,safe=None,{},False
    with cont.continuation_lease(plan["batch"],root):
        try:
            while True:
                verify_supervisor(plan,cli)
                group=preparer(plan,cli);tracked={};safe=False
                state.update(status="dispatching",active_group=str(group));write(root/"recovery-state.json",state)
                state["controller"]=launcher(plan,group,tracked)
                state["status"]="running";write(root/"recovery-state.json",state)
                result=waiter(plan,group,cli);safe=True
                state["waves"].append(result)
                state.update(status="between_waves",new_valid_episodes=result["new_valid_episodes"])
                write(root/"recovery-state.json",state)
                if result["new_valid_episodes"]==68:
                    state.update(status="completed",logical_valid_total=80,total_admitted_attempts=86,
                                 active_group=None,completed_at=utc().isoformat(),cleanup_confirmed=True)
                    write(root/"recovery-state.json",state)
                    return state
        except BaseException as exc:
            state.update(status="stopped",error={"type":type(exc).__name__,"message":str(exc)},
                         completed_at=utc().isoformat())
            if group is not None and not safe:
                try:state["failure_cleanup"]=aborter(plan,group,cli,descriptor=tracked or None)
                except BaseException as error:state["failure_cleanup"]={"confirmed":False,"error":str(error)}
            try:write(root/"recovery-state.json",state)
            except BaseException:print(json.dumps(state,ensure_ascii=True),file=sys.stderr,flush=True)
            return state


def launch(batch,run_dir,*,popen=subprocess.Popen):
    plan=inspect(batch,run_dir)
    root=Path(plan["run_dir"]);root.mkdir(parents=True,exist_ok=False)
    write(root/"plan.json",plan)
    argv=[sys.executable,"-B",str(SELF),"_supervise","--plan",str(root/"plan.json")]
    options={"creationflags":subprocess.CREATE_NO_WINDOW} if os.name=="nt" else {"start_new_session":True}
    process=None
    try:
        with (root/"stdout.log").open("xb") as out,(root/"stderr.log").open("xb") as err:
            process=popen(argv,cwd=cont.REPO,env=cont.child_environment(),stdout=out,stderr=err,**options)
            import psutil
            descriptor={"pid":process.pid,"created_at":psutil.Process(process.pid).create_time(),
                        "argv":argv,"plan_sha256":sha(root/"plan.json"),"at":utc().isoformat()}
            write(root/"launch.json",descriptor)
        return descriptor
    except BaseException:
        if process is not None:
            import psutil
            try:
                owner=psutil.Process(process.pid)
                require(owner.cmdline()==argv,"Cannot claim a changed supervisor PID")
                targets=owner.children(recursive=True)+[owner]
                for child in targets:
                    try:child.kill()
                    except psutil.NoSuchProcess:pass
                _,alive=psutil.wait_procs(targets,timeout=30)
                require(not alive,"Supervisor stop remains unconfirmed")
            except psutil.NoSuchProcess:pass
            path=Path(plan["batch"])/"launch.json"
            if path.exists():
                group=Path(read(path)["parallel_group_dir"])
                if group.is_relative_to(root):
                    cont.abort_owned(plan,group,CliView(recovery.load_frozen_cli(plan["batch"])))
        raise


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=("inspect","launch","_supervise","_run"))
    for name in ("batch","run-dir","group-dir","snapshot-root","plan"):parser.add_argument("--"+name,type=Path)
    args=parser.parse_args(argv)
    if args.command=="_run":
        require(args.snapshot_root==args.batch/"runtime_snapshot","Snapshot root differs")
        result=run_wave(args.batch,args.group_dir)
    elif args.command=="_supervise":
        plan=read(args.plan)
        try:
            cont.await_registration(plan)
            result=supervise(plan)
        except BaseException as exc:
            result={"status":"stopped","phase":"startup","error":{"type":type(exc).__name__,"message":str(exc)}}
            write(Path(plan["run_dir"])/"recovery-state.json",result)
    else:
        result=inspect(args.batch,args.run_dir) if args.command=="inspect" else launch(args.batch,args.run_dir)
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return int(args.command in ("_run","_supervise") and result.get("status") not in ("completed","completed_awaiting_review"))


if __name__=="__main__":
    raise SystemExit(main())
