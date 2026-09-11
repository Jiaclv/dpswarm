"""Regression from the real Docker EOF captured during container turnover."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

OPS=Path(__file__).resolve().parents[1]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
helpers=module("eof_sampling_test_helpers",OPS/"tests/test_sampling_recovery.py")
r=helpers.r
observation=OPS/"a2-stats-incident-v1/diagnostics/observations-20260905T162859Z/014.json"

def observed():
    d=json.loads(observation.read_text("utf-8"))
    assert d["selection"]=="current_ps" and d["stats"]["returncode"]==1 and d["stats"]["stderr"].strip()=="EOF"
    initial=[x["Id"] for x in d["inspect_before"]["stdout_projected"]]
    survivors=[x["Id"] for x in d["inspect_after"]["stdout_projected"]]
    assert len(initial)==2 and len(survivors)==1 and set(survivors)<set(initial)
    error=subprocess.CalledProcessError(1,d["stats"]["argv"],output=d["stats"]["stdout"],stderr=d["stats"]["stderr"])
    return initial,survivors,error

def test_captured_eof_reproduces_old_failure_and_new_reconciliation(tmp_path):
    initial,survivors,error=observed()
    old=module("old_stats_resources",OPS/"a2-stats-incident-v1/source-archive/parallel_resources.py")
    old_docker=helpers.Docker([initial],stats=[error])
    with pytest.raises(old.SamplingError) as exc:
        old.sample_resources(helpers.owned(tmp_path),docker=old_docker,**helpers.HOST)
    assert exc.value.diagnostics["reason"]=="docker_command_failed"
    docker=helpers.Docker([initial,survivors,survivors,survivors],stats=[error,[helpers.stat(survivors[0])]])
    value=r.sample_resources(helpers.owned(tmp_path),docker=docker,**helpers.HOST)
    assert value["sampling_consistency"]["attempts"]==2
    assert value["sampling_consistency"]["retry_events"][0]["trigger_reason"]=="stats_stream_eof"
    assert value["candidate_container_count"]==1 and value["owned_memory_bytes"]==12*1024**2

def test_eof_for_unchanged_running_set_is_not_ignored(tmp_path):
    initial,_,error=observed()
    docker=helpers.Docker([initial,initial],stats=[error])
    with pytest.raises(r.SamplingError) as exc:
        r.sample_resources(helpers.owned(tmp_path),docker=docker,**helpers.HOST)
    assert exc.value.diagnostics["reason"]=="stats_stream_eof"
    assert [c[0] for c in docker.calls].count("stats")==1

@pytest.mark.parametrize("stage,stderr",[("inspect","EOF\n"),("stats","unexpected EOF"),("stats","EOF\nprivate-secret-payload"),("info","EOF")])
def test_other_or_mixed_errors_do_not_become_lifecycle_retries(stage,stderr):
    error=r._docker_sample_failure(stage,[stage,helpers.A],1,stderr)
    assert error.diagnostics["reason"]=="docker_command_failed"
    assert "private-secret-payload" not in json.dumps(error.diagnostics)

def test_daemon_failure_during_reconciliation_still_fails(tmp_path):
    initial,_,error=observed();calls=0
    inner=helpers.Docker([initial],stats=[error])
    def docker(args):
        nonlocal calls
        if args[0]=="ps":
            calls+=1
            if calls==2:
                raise subprocess.CalledProcessError(1,args,stderr="Cannot connect to the Docker daemon")
        return inner(args)
    with pytest.raises(r.SamplingError) as exc:
        r.sample_resources(helpers.owned(tmp_path),docker=docker,**helpers.HOST)
    assert exc.value.diagnostics["stage"]=="ps_reconcile" and exc.value.diagnostics["reason"]=="daemon_unavailable"

def test_raw_failure_evidence_is_separate_and_second_failure_still_trips(tmp_path):
    error=r._docker_sample_failure("stats",["stats",helpers.A],1,"EOF\n","")
    def failed(_):raise error
    monitor=r.ResourceMonitor(tmp_path,[],sample=failed,max_sample_failures=2)
    first=monitor.tick()
    assert not (tmp_path/"TRIP.json").exists()
    evidence=first["sample_error_evidence"]
    assert r.sha(evidence["path"])==evidence["sha256"]
    assert r.read(evidence["path"])["stderr"]=="EOF\n"
    assert "stderr" not in json.dumps(first)
    second=monitor.tick()
    assert second["consecutive_failures"]==2
    assert r.read(tmp_path/"TRIP.json")["reason"]=="resource_sampling_unavailable"

def test_large_error_output_is_bounded_in_its_artifact(tmp_path):
    error=r._docker_sample_failure("stats",["stats",helpers.A],1,"E"*70000,"O"*70000)
    def failed(_):raise error
    sample=r.ResourceMonitor(tmp_path,[],sample=failed).tick()
    value=r.read(sample["sample_error_evidence"]["path"])
    assert len(value["stderr"])==65536 and len(value["stdout"])==65536
    assert value["stderr_truncated"] is True and value["stdout_truncated"] is True

def test_reconciled_sample_still_enforces_container_creation_limit(tmp_path):
    initial,survivors,error=observed()
    docker=helpers.Docker([initial,survivors,survivors,survivors],
        inspect=[[helpers.record(x) for x in initial],[helpers.record(survivors[0],memory=2*r.GIB)]],
        stats=[error,[helpers.stat(survivors[0])]])
    roots=helpers.owned(tmp_path/"owners")
    monitor=r.ResourceMonitor(tmp_path/"monitor",roots,
        sample=lambda roots:r.sample_resources(roots,docker=docker,**helpers.HOST),container_memory_limit_bytes=r.GIB)
    monitor.tick()
    assert r.read(tmp_path/"monitor/TRIP.json")["reason"]=="container_memory_limit_mismatch"
