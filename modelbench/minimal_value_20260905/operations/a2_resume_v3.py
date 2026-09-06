"""Resume only the 33 never-started A2 entries after scoring preserved candidates.

Old attempts and artifacts remain at their original paths. This revision keeps
53 admissions and ten unknown calls, adds at most 33 admissions, and reports
scope completion with a permanently incomplete original 80-entry matrix.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys

HERE=Path(__file__).resolve().parent
EXPERIMENT=HERE.parent
SELF=Path(__file__).resolve()
REVISION="a2_resume_v3"


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value)
    return value


base=module("_a2_resume_v3_recovery_adapter",HERE/"a2_recovery.py")
controller,recovery,cont=base.controller,base.recovery,base.cont
read,write,sha,utc,require=base.read,base.write,base.sha,base.utc,base.require
BASE_INSPECT,BASE_TERMINAL,BASE_LAUNCH=base.inspect,base.terminal_wave,base.launch


def operator_sources():
    paths=[HERE/name for name in controller.source_identity()]
    paths += [SELF,HERE/"a2_recovery.py",HERE/"a2_continuation.py",
              HERE/"a2_recovery_accounting.py",HERE/"idle_memory.py",HERE/"salvage_scoring.py",HERE/"salvage_stats_scoring.py",HERE/"a2_resume.py"]
    return {str(path):sha(path) for path in paths}


def bind_reference(refs,path,digest=None):
    path=Path(path).resolve()
    actual=sha(path)
    require(digest is None or actual==digest,"Bound evidence changed: "+path.name)
    refs[str(path)]=actual
    return actual


def authorization(path):
    value=read(path)
    require(value.get("revision")==REVISION and value.get("user_confirmation")=="那就继续啊",
            "Explicit resume authorization is missing")
    exact={"total_admission_cap":51_600_000,"already_admitted_attempts":53,
           "already_admitted_tokens":31_800_000,"remaining_admission_tokens":19_800_000,
           "new_attempt_count":33,"new_attempt_token_admission":600000,"unknown_call_count":10,
           "salvage_candidate_count":7,"carried_original_valid_count":33,
           "maximum_logical_valid_if_all_salvage_qualify":73,"cost_stop_usd":480,
           "cost_warning_usd":320,"stage_dispatch_seconds":61*3600,"package_max_seconds":96*3600,
           "global_model_slots":8,"candidate_container_cap":12,"candidate_memory":"1g"}
    require(all(value.get(k)==v for k,v in exact.items()),"Approved scope, budget or resource limits changed")
    require(value.get("automatic_additional_retry") is False
            and value.get("unknown_usage_not_zero") is True
            and value.get("provider_limits")=={"codex_account":4,"glm_coding":1,"deepseek":4},
            "Retry/unknown/provider contract changed")
    return value


def prepare(batch,*,authorization_path,salvage_report):
    """Fresh versioned batch; copying frozen bytes performs no model or grading call."""
    batch=Path(batch).resolve();authorization_path=Path(authorization_path).resolve()
    salvage_report=Path(salvage_report).resolve()
    require(batch.is_relative_to(EXPERIMENT/"batches") and not batch.exists(),
            "Resume needs a fresh directory within this experiment")
    auth=authorization(authorization_path)
    source=Path(auth["source_batch"]).resolve()
    require(source!=batch and source.is_relative_to(EXPERIMENT/"batches"),"Source batch identity is invalid")
    refs={}
    bind_reference(refs,authorization_path)
    bind_reference(refs,source/"manifest.json",auth["source_manifest_sha256"])
    bind_reference(refs,source/"state.json",auth["source_state_sha256"])
    old_manifest=read(source/"manifest.json")
    state=read(source/"state.json")
    require(state["token_admission_sum"]==31_800_000,"Historical admission total changed")
    launch_path=source/"launch.json";bind_reference(refs,launch_path)
    prior_group=Path(read(launch_path)["parallel_group_dir"]).resolve()
    supervisor=prior_group.parent/"resume-state.json"
    bind_reference(refs,supervisor,auth["source_supervisor_sha256"])
    require(read(supervisor).get("status")=="stopped","Historical supervisor has not stopped")
    report=read(salvage_report);bind_reference(refs,salvage_report)
    require(report.get("status")=="PASS" and report.get("cleanup_confirmed") is True,
            "All salvage grading and cleanup must finish before resume preparation")
    require(Path(report["source_batch"]).resolve()==source
            and report["source_manifest_sha256"]==auth["source_manifest_sha256"],
            "Salvage report belongs to another batch")
    require(report.get("overall_unknown_count")==10 and report.get("overall_cost_computable") is False,
            "Ten historical unknown calls must remain explicit")
    require(math.isclose(float(auth["legacy_known_cost_usd"]),
            float(report["accounting"]["combined_known_api_equivalent_usd"]),rel_tol=0,abs_tol=1e-9),
            "Authorized known subtotal differs from audited generation costs")
    old_plan_path=source/"recovery-plan.json";bind_reference(refs,old_plan_path,old_manifest["recovery_plan_sha256"])
    old_plan=read(old_plan_path)
    for path,digest in old_plan["references"].items():
        bind_reference(refs,path,digest)
    schedule=old_manifest["schedule"];ids=[entry["run_id"] for entry in schedule]
    require(old_manifest["stage"]=="a2" and old_manifest["scheduled_episodes"]==80,
            "Original A2 matrix changed")
    require(auth["pending_run_ids"]==ids[47:] and len(ids[47:])==33,"Only never-started entries may be resumed")
    require(not any((source/"results"/rid).exists() for rid in ids[47:]),
            "An allegedly unstarted episode already has evidence")
    carried=list(old_plan["carried_valid"])
    prior_completed=list(state["episodes"])
    require(len(carried)==21 and state["completed_episodes"]==12
            and [item["run_id"] for item in prior_completed]==ids[24:36],
            "Historical carried set or twelve newly completed identities changed")
    require({item["run_id"] for item in carried}==set(ids[:24])-set(old_plan["incomplete_runs_not_retried"]),
            "Historical 21 valid identities changed")
    carried += prior_completed
    require(len(carried)==33 and len({item["run_id"] for item in carried})==33,
            "Historical valid set must contain 33 unique entries")
    require({item["run_id"] for item in report["carried_valid"]}=={item["run_id"] for item in carried}
            and all(any(other["run_id"]==item["run_id"] and other["path"]==item["path"]
                        and other["sha256"]==item["sha256"] for other in report["carried_valid"])
                    for item in carried),"Scoring report and historical valid references disagree")
    require(len(auth["incomplete_runs_not_retried"])==7
            and len(set(auth["incomplete_runs_not_retried"]))==7
            and set(old_plan["incomplete_runs_not_retried"]).issubset(auth["incomplete_runs_not_retried"])
            and len(set(auth["incomplete_runs_not_retried"])-set(old_plan["incomplete_runs_not_retried"]))==4,
            "Historical seven incomplete entries changed")
    salvaged=report["salvaged_valid"]
    expected_salvage=set(ids[36:47])-set(auth["incomplete_runs_not_retried"])
    require(len(salvaged)==7 and {item["run_id"] for item in salvaged}==expected_salvage,
            "Salvage set does not match the seven eligible frozen artifacts")
    for item in carried:
        bind_reference(refs,item["path"],item["sha256"])
    for item in salvaged:
        bind_reference(refs,item["path"],item["sha256"])
        bind_reference(refs,item["source_result_path"],item["source_result_sha256"])
        result=read(item["path"]);score=result.get("score") or {}
        require(score.get("completed") is True and type(score.get("resolved")) is bool
                and score["resolved"]==item["official_resolved"] and result.get("cleanup_confirmed") is True
                and result.get("quiesced") is True,"Salvage result lacks a complete official score and cleanup")
        artifact=result.get("artifact") or result.get("lead_artifact") or {}
        bind_reference(refs,artifact["path"],item["patch_sha256"])
        require(artifact["sha256"]==item["patch_sha256"],"Salvage patch identity differs")
        require(score.get("patch_sha256")==item["patch_sha256"] and score.get("reports_sha256"),
                "Salvage score lacks bound official reports or patch identity")
        grader_root=Path(score["grader_dir"]).resolve()
        for relative,digest in score["reports_sha256"].items():
            report_path=(grader_root/relative).resolve()
            require(report_path.is_relative_to(grader_root),"Official report escaped its grader directory")
            bind_reference(refs,report_path,digest)
        carried.append({"run_id":item["run_id"],"path":str(Path(item["path"]).resolve()),"sha256":item["sha256"],
                        "kind":"salvaged_scored"})
    carried.sort(key=lambda x:ids.index(x["run_id"]))
    unknown=list(report["legacy_unknown_calls"])+list(report["new_unknown_calls"])
    require(len(report["legacy_unknown_calls"])==7 and len(report["new_unknown_calls"])==3
            and len(unknown)==10,"Legacy unknown identities changed")
    unknown_ids=[item.get("call_id") for item in unknown]
    require(all(isinstance(cid,str) and cid for cid in unknown_ids) and len(set(unknown_ids))==10
            and all(type(item.get("reserved_tokens")) is int and item["reserved_tokens"]>=0 for item in unknown)
            and sum(item["reserved_tokens"] for item in unknown)==308099,
            "Unknown identities or reserved token total differ from historical evidence")
    require(report["legacy_unknown_calls"]==old_plan["legacy_unknown_calls"],"Historical seven unknown calls changed")
    for path,digest in report.get("references",{}).items():bind_reference(refs,path,digest)
    # The fixed known subtotal already includes the latest 11 interrupted-wave generation attempts.
    # Salvaging seven scores incurs no model calls and does not add those costs again.
    value={"version":3,"revision":REVISION,"source_batch":str(source),
        "source_manifest_sha256":auth["source_manifest_sha256"],"source_state_sha256":auth["source_state_sha256"],
        "authorization_path":str(authorization_path),"authorization_sha256":sha(authorization_path),
        "salvage_report_path":str(salvage_report),"salvage_report_sha256":sha(salvage_report),
        "carried_valid":carried,"pending_run_ids":auth["pending_run_ids"],
        "incomplete_runs_not_retried":auth["incomplete_runs_not_retried"],
        "legacy_admitted_attempts":53,"legacy_token_admission_sum":31_800_000,
        "legacy_known_cost_usd":auth["legacy_known_cost_usd"],
        "legacy_unknown_calls":unknown,"legacy_unknown_reserved_tokens":308099,
        "total_admission_cap":51_600_000,"cost_stop_usd":480,"cost_warning_usd":320,
        "stage_started_at":auth["stage_started_at"],"package_started_at":auth["package_started_at"],
        "maximum_logical_valid":73,"new_attempt_count":33,"references":refs,
        "overall_cost_computable":False,"automatic_additional_retry":False}
    require(len(carried)==40 and len({x["run_id"] for x in carried})==40,"Carried valid set is not 40 unique entries")
    require(base.deadline(value)>utc(),"Original deadline already elapsed")
    require(old_manifest["stage_limits"]["token_admission_sum"]==51_600_000
            and old_manifest["stage_limits"]["cost_stop_usd"]==480,"Source recovery caps differ")
    # Build an exclusive snapshot from only the already frozen source/input maps.
    files=[(source/"runtime_snapshot"/relative,batch/"runtime_snapshot"/relative,digest)
           for relative,digest in old_manifest["runtime_sources"].items()]
    official=Path("modelbench/minimal_value_20260905/official")
    files += [(source/"runtime_snapshot"/official/relative,batch/"runtime_snapshot"/official/relative,digest)
              for relative,digest in old_manifest["input_artifacts"].items()]
    source_root=(source/"runtime_snapshot").resolve();destination_root=(batch/"runtime_snapshot").resolve()
    files=[(src.resolve(),dest.resolve(),digest) for src,dest,digest in files]
    for src,dest,digest in files:
        require(src.is_relative_to(source_root) and dest.is_relative_to(destination_root),
                "Snapshot source or destination escaped its frozen root")
        require(sha(src)==digest,"Source snapshot drifted before copying")
    batch.mkdir(parents=True,exist_ok=False)
    for src,dest,digest in files:
        dest.parent.mkdir(parents=True,exist_ok=True)
        with dest.open("xb") as stream:stream.write(src.read_bytes())
        require(sha(dest)==digest and sha(src)==digest,"Snapshot bytes changed while copying")
    write(batch/"recovery-plan.json",value)
    manifest={**old_manifest,"root":str(batch/"runtime_snapshot"),
              "recovery_plan_sha256":sha(batch/"recovery-plan.json")}
    write(batch/"manifest.json",manifest)
    (batch/"manifest.sha256").write_text(sha(batch/"manifest.json")+"\n",encoding="ascii")
    new_state={"stage":"a2","started_at":value["stage_started_at"],"package_started_at":value["package_started_at"],
        "completed_episodes":0,"episodes":[],"token_admission_sum":31_800_000,
        "known_cost_usd":value["legacy_known_cost_usd"],"stop_reason":None,"active_episodes":{},
        "recovery":{"legacy_unknown_calls":unknown,"run_groups":{}},
        "carried_valid_episodes":40,"new_attempts_scheduled":33,"maximum_logical_valid":73,
        "overall_cost_computable":False,"recovery_plan_sha256":sha(batch/"recovery-plan.json")}
    write(batch/"state.json",new_state)
    for path,digest in refs.items():require(sha(path)==digest,"Bound evidence changed during preparation")
    for key in set(old_manifest)-{"root","recovery_plan_sha256"}:
        require(manifest[key]==old_manifest[key],"Unexpected manifest variation")
    cli=base.CliView(recovery.load_frozen_cli(batch))
    contract(batch,cli)
    receipt={"prepared":True,"batch":str(batch),"manifest_sha256":sha(batch/"manifest.json"),
             "recovery_plan_sha256":sha(batch/"recovery-plan.json"),
             "runtime_sources":len(old_manifest["runtime_sources"]),"inputs":len(old_manifest["input_artifacts"]),
             "carried_valid_count":40,"new_attempt_count":33,"maximum_logical_valid":73,"models_started":0}
    write(batch/"preparation.json",receipt)
    return receipt


def contract(batch,cli,*,allow_stopped=False,full_evidence=True):
    batch=Path(batch).resolve();manifest=cli.load_manifest(batch);state=read(batch/"state.json")
    value=read(batch/"recovery-plan.json")
    require(value.get("revision")==REVISION and value.get("version")==3
            and manifest["recovery_plan_sha256"]==sha(batch/"recovery-plan.json"),"Resume contract changed")
    auth=authorization(value["authorization_path"])
    require(sha(value["authorization_path"])==value["authorization_sha256"],"Resume authorization changed")
    for k in ("pending_run_ids","incomplete_runs_not_retried","legacy_known_cost_usd",
              "stage_started_at","package_started_at"):
        require(value[k]==auth[k],"Resume plan changed an authorized field: "+k)
    require(value["legacy_token_admission_sum"]==31_800_000 and value["legacy_admitted_attempts"]==53
            and value["total_admission_cap"]==51_600_000 and value["new_attempt_count"]==33
            and value["maximum_logical_valid"]==73 and len(value["legacy_unknown_calls"])==10
            and value["legacy_unknown_reserved_tokens"]==308099,"Resume counts or unknowns changed")
    source=Path(value["source_batch"])
    require(sha(source/"manifest.json")==value["source_manifest_sha256"]
            and sha(source/"state.json")==value["source_state_sha256"],"Source manifest or state changed")
    old=read(source/"manifest.json")
    require(all(manifest[k]==v for k,v in old.items() if k not in ("root","recovery_plan_sha256")),
            "Frozen schedule/runtime/input/transport/gate/limits changed")
    require(state["started_at"]==value["stage_started_at"] and state["package_started_at"]==value["package_started_at"],
            "Original clock reset")
    require(not any((batch/n).exists() for n in ("STOP","CANCEL","TRIP.json")) or allow_stopped,"Resume batch stopped")
    if full_evidence:
        for path,digest in value["references"].items():require(sha(path)==digest,"Historical or salvage evidence changed")
        cli.freeze.verify_snapshot(batch,manifest);cli.validate_schedule(manifest["schedule"],"a2")
    require(sha(value["salvage_report_path"])==value["salvage_report_sha256"],"Scoring report changed")
    require(len(value["carried_valid"])==40 and len({x["run_id"] for x in value["carried_valid"]})==40,
            "Carried valid identities changed")
    return value,manifest,state


def prefix_audit(batch,cli):
    value,manifest,state=contract(batch,cli)
    count=state["completed_episodes"]
    require(type(count) is int and 0<=count<=33,"New valid count is invalid")
    by_id={e["run_id"]:e for e in manifest["schedule"]}
    mapping=state.get("recovery",{}).get("run_groups",{})
    audited=[]
    for rid in value["pending_run_ids"][:count]:
        require(rid in mapping,"New attempt has no bound group")
        cont.group_identity(batch,Path(mapping[rid]))
        audited.append(base.audit_attempt(batch,by_id[rid],cli,mapping[rid]))
    expected=[{"run_id":x["entry"]["run_id"],"path":x["path"],"sha256":x["sha256"]} for x in audited]
    require(state["episodes"]==expected,"New valid prefix or result hash changed")
    require(state["token_admission_sum"]==31_800_000+count*600000,"New interrupted admission cannot be refunded")
    known=value["legacy_known_cost_usd"]+sum(x["known_cost_usd"] for x in audited)
    require(math.isclose(state["known_cost_usd"],known,rel_tol=0,abs_tol=1e-9),"Known cost differs or salvage was counted twice")
    root=Path(batch)/"results";existing={p.name for p in root.iterdir()} if root.exists() else set()
    require(existing==set(value["pending_run_ids"][:count]),"Unexpected or incomplete new attempt exists")
    return value,manifest,state,expected,known


def public_summary(batch,cli):
    value,_,state=contract(batch,cli,allow_stopped=True)
    count=state["completed_episodes"]
    return {"revision":REVISION,"new_valid_episodes":count,"carried_valid_episodes":40,"logical_valid_total":40+count,
        "new_admitted_attempts":(state["token_admission_sum"]-31_800_000)//600000,
        "historical_admitted_attempts":53,"cumulative_token_admission_sum":state["token_admission_sum"],
        "cumulative_known_api_equivalent_usd":state["known_cost_usd"],"legacy_unknown_calls":value["legacy_unknown_calls"],
        "legacy_unknown_reserved_tokens":308099,"total_cost_computable":False,
        "known_cost_warning":state["known_cost_usd"]>=320,"known_cost_stop_usd":480,
        "original_matrix_complete":False,"original_matrix_size":80,"maximum_logical_valid":73,
        "incomplete_runs_not_retried":value["incomplete_runs_not_retried"],
        "scope_complete":count==33,"new_results":state["episodes"],"carried_results":value["carried_valid"],
        "status":"scope_complete_original_matrix_incomplete" if count==33 else "in_progress_or_stopped",
        "original_deadline":base.deadline(value).isoformat(),"stop_reason":state.get("stop_reason"),"at":utc().isoformat()}


def inspect(batch,run_dir,*,cli=None):
    result=BASE_INSPECT(batch,run_dir,cli=cli)
    result.update(initial_token_admission_sum=31_800_000,new_attempt_count=33,
                  maximum_logical_valid=73,carried_valid_count=40)
    return result


def terminal_wave(plan,group,cli):
    result=BASE_TERMINAL(plan,group,cli)
    result["logical_valid_total"]=40+result["new_valid_episodes"]
    return result


def prepare_wave(plan,cli,*,headroom=None):
    base.verify_supervisor(plan,cli)
    value,manifest,state,prefix,_=prefix_audit(plan["batch"],cli)
    require(not state.get("active_episodes"),"Previous wave remains active")
    previous=None
    if prefix:
        previous=Path(read(Path(plan["batch"])/"launch.json")["parallel_group_dir"])
        terminal_wave(plan,previous,cli)
    ids=value["pending_run_ids"][len(prefix):len(prefix)+12]
    require(ids,"Authorized never-started queue already complete")
    group=Path(plan["run_dir"])/("wave-%03d-%03d"%(len(prefix)+1,len(prefix)+len(ids)))
    policy=base.policy_for(plan,ids)
    policy.update(scope="Only 33 never-started entries; retain 53 prior admissions and ten unknown calls")
    lookup={e["run_id"]:e for e in manifest["schedule"]}
    timing=controller.wave_time_limits(controller.candidate_requirements([lookup[i] for i in ids]),policy)
    require((base.deadline(value)-utc()).total_seconds()>=timing["watchdog_seconds"]+300,"Original window cannot fit wave")
    group.mkdir(parents=True,exist_ok=False)
    before=sha(Path(plan["batch"])/"state.json")
    shutil.copyfile(Path(plan["batch"])/"state.json",group/"previous-state.json")
    require(sha(group/"previous-state.json")==before,"State changed during archive")
    helper=headroom or module("_resume_idle",HERE/"idle_memory.py").ensure_idle_headroom
    require(helper(group/"idle-memory.json",manifest["resource_lock_path"],minimum_gib=4).get("ready") is True,
            "Idle memory or resource lease not ready")
    write(group/"policy.json",policy)
    write(group/"transition.json",{"status":"ready","transition_id":group.name,
        "recovery_plan_sha256":plan["recovery_plan_sha256"],"prior_state_sha256":before,
        "new_valid_prefix":prefix,"legacy_admitted_attempts":53,
        "previous_group":str(previous) if previous else None,"at":utc().isoformat()})
    with cli.stage_lease(manifest["resource_lock_path"],plan["batch"]):
        base.verify_supervisor(plan,cli);base.prior_stop_guard(group)
        require(sha(Path(plan["batch"])/"state.json")==before,"Another writer changed resume state")
        updated=dict(state);updated.pop("completed_at",None);updated["stop_reason"]=None
        updated["recovery"]={**state.get("recovery",{}),"legacy_unknown_calls":value["legacy_unknown_calls"],
            "run_groups":{**state.get("recovery",{}).get("run_groups",{}),**{rid:str(group) for rid in ids}}}
        updated["parallel_transition"]={"status":"ready","transition_id":group.name,
            "record":str(group/"transition.json"),"group_dir":str(group),"policy_sha256":sha(group/"policy.json")}
        write(Path(plan["batch"])/"state.json",updated)
    base.inspect_wave(plan["batch"],group,cli=cli)
    return group


def supervise(plan,*,cli=None,preparer=prepare_wave,launcher=None,waiter=None,aborter=None):
    cli=cli or base.CliView(recovery.load_frozen_cli(plan["batch"]))
    launcher=launcher or base.launch_wave;waiter=waiter or base.wait_wave;aborter=aborter or cont.abort_owned
    root=Path(plan["run_dir"]);group=None;tracked={};safe=False
    state={"revision":REVISION,"status":"starting","waves":[],"deadline":plan["deadline"],
           "legacy_unknown_calls":plan["legacy_unknown_calls"],"automatic_retry_allowed":False,
           "maximum_logical_valid":73}
    with cont.continuation_lease(plan["batch"],root):
        try:
            while True:
                base.verify_supervisor(plan,cli)
                group=preparer(plan,cli);tracked={};safe=False
                state.update(status="dispatching",active_group=str(group));write(root/"resume-state.json",state)
                state["controller"]=launcher(plan,group,tracked)
                state["status"]="running";write(root/"resume-state.json",state)
                result=waiter(plan,group,cli);safe=True
                state["waves"].append(result)
                state.update(status="between_waves",new_valid_episodes=result["new_valid_episodes"])
                write(root/"resume-state.json",state)
                if result["new_valid_episodes"]==33:
                    state.update(status="scope_complete_original_matrix_incomplete",logical_valid_total=73,
                        original_matrix_size=80,original_matrix_complete=False,total_admitted_attempts=86,
                        active_group=None,completed_at=utc().isoformat(),cleanup_confirmed=True)
                    write(root/"resume-state.json",state)
                    return state
        except BaseException as exc:
            state.update(status="stopped",error={"type":type(exc).__name__,"message":str(exc)},completed_at=utc().isoformat())
            if group is not None and not safe:
                try:state["failure_cleanup"]=aborter(plan,group,cli,descriptor=tracked or None)
                except BaseException as cleanup_error:state["failure_cleanup"]={"confirmed":False,"error":str(cleanup_error)}
            try:write(root/"resume-state.json",state)
            except BaseException:print(json.dumps(state,ensure_ascii=True),file=sys.stderr,flush=True)
            return state


def install():
    # This changes only the newly loaded operator module in this fresh process.
    # The old files, snapshots, manifests, state projections and failed attempts
    # are never edited. The frozen single-episode and wave loops remain intact.
    base.SELF=SELF;base.REVISION=REVISION;base.operator_sources=operator_sources
    base.contract=contract;base.prefix_audit=prefix_audit;base.public_summary=public_summary
    base.inspect=inspect;base.prepare_wave=prepare_wave;base.terminal_wave=terminal_wave
    base.supervise=supervise


install()


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=("prepare","inspect","launch","_supervise","_run"))
    for key in ("batch","run-dir","authorization","salvage-report","plan","group-dir","snapshot-root"):
        parser.add_argument("--"+key,type=Path)
    args=parser.parse_args(argv)
    if args.command=="prepare":
        result=prepare(args.batch,authorization_path=args.authorization,salvage_report=args.salvage_report)
    elif args.command=="inspect":result=inspect(args.batch,args.run_dir)
    elif args.command=="launch":result=BASE_LAUNCH(args.batch,args.run_dir)
    elif args.command=="_run":
        require(args.snapshot_root==args.batch/"runtime_snapshot","Snapshot root changed")
        result=base.run_wave(args.batch,args.group_dir)
    else:
        plan=read(args.plan)
        try:
            cont.await_registration(plan)
            result=supervise(plan)
        except BaseException as exc:
            result={"status":"stopped","phase":"startup","error":{"type":type(exc).__name__,"message":str(exc)}}
            write(Path(plan["run_dir"])/"resume-state.json",result)
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return int(args.command in ("_run","_supervise") and result.get("status") not in
               ("completed_awaiting_review","scope_complete_original_matrix_incomplete"))


if __name__=="__main__":
    raise SystemExit(main())
