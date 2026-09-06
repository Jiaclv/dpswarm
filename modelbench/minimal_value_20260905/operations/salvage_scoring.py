"""Score already frozen generation artifacts in a new, isolated evidence tree.

This entry point never calls a solver, selector, or context-manager model.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
VERSION = "sampling_incident_salvage_v1"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamp is not timezone-aware")
    return parsed


def unchanged(references):
    for path, expected in references.items():
        if sha(path) != expected:
            raise ValueError("Original evidence changed: " + path)


def load_frozen(batch):
    batch = Path(batch).resolve()
    manifest = read(batch / "manifest.json")
    if (batch / "manifest.sha256").read_text("ascii").strip() != sha(batch / "manifest.json"):
        raise ValueError("Source manifest hash mismatch")
    snapshot = batch / "runtime_snapshot"
    for name, expected in manifest["runtime_sources"].items():
        if sha(snapshot / name) != expected:
            raise ValueError("Source frozen runtime mismatch: " + name)
    for name, module in tuple(sys.modules.items()):
        if name == "modelbench" or name.startswith("modelbench.") or name == "dpswarm" or name.startswith("dpswarm."):
            origin = getattr(module, "__file__", None)
            if origin and not Path(origin).resolve().is_relative_to(snapshot):
                raise ValueError("Use a fresh standalone salvage process")
    sys.path[:0] = [str(snapshot), str(snapshot / "dpswarm-plugin")]
    from modelbench.minimal_value_20260905 import cli
    cli.freeze.verify_snapshot(batch, manifest)
    cli.freeze.assert_import_origins(snapshot)
    return cli, manifest


def reporting():
    from modelbench.minimal_value_20260905 import reporting as module
    return module


def load_accounting(path):
    spec = importlib.util.spec_from_file_location("_salvage_accounting", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def qualify_generation(result, call_audit, provider_events, trip_at):
    """A post-TRIP bookkeeping timestamp alone does not invalidate a frozen patch."""
    if call_audit.get("passed") is not True:
        raise ValueError("Generation call accounting is incomplete")
    if result.get("quiesced") is not True or result.get("cleanup_confirmed") is not True:
        raise ValueError("Generation is not quiesced and cleaned")
    if result.get("infrastructure_error") or (result.get("execution_health") or {}).get("status") == "host_error":
        raise ValueError("Generation has an infrastructure failure")
    cutoff = timestamp(trip_at)
    frozen = timestamp(result["patch_frozen_at"])
    if frozen > cutoff:
        raise ValueError("Patch was frozen after the resource stop")
    calls = [e for e in provider_events if e.get("event") in ("transport_entered", "transport_returned")]
    if any(timestamp(e["at"]) > cutoff for e in calls):
        raise ValueError("Provider activity continued after the resource stop")
    latest = max((e["at"] for e in calls), key=timestamp, default=None)
    return {"eligible": True, "patch_frozen_at": result["patch_frozen_at"], "trip_at": trip_at,
            "result_completed_at": result.get("completed_at"), "latest_provider_event_at": latest,
            "post_trip_bookkeeping": bool(result.get("completed_at") and timestamp(result["completed_at"]) > cutoff),
            "rule": "complete calls and quiesced frozen patch before TRIP; final bookkeeping may follow"}


def prepare_sources(batch, wave, destination, cli, manifest):
    batch, wave, destination = map(lambda p: Path(p).resolve(), (batch, wave, destination))
    if destination.exists():
        raise ValueError("Salvage destination already exists; no automatic retry or overwrite")
    trip = read(wave / "TRIP.json")
    group = read(wave / "group.json")
    if group["manifest_sha256"] != sha(batch / "manifest.json") or Path(group["batch"]).resolve() != batch:
        raise ValueError("Wave and source manifest disagree")
    references = {}

    def bind(path):
        path = Path(path).resolve()
        references[str(path)] = sha(path)
        return path

    for p in (batch / "manifest.json", batch / "carryover.json", batch / "recovery-plan.json", wave / "TRIP.json", wave / "group.json"):
        bind(p)
    events, event_paths = [], sorted((wave / "provider-events").glob("*.jsonl"))
    for path in event_paths:
        bind(path)
        events.extend(json.loads(line) for line in path.read_text("utf-8-sig").splitlines())
    archived = destination.parent / "source-archive/a2_recovery_accounting.py"
    bind(archived)
    auditor = load_accounting(archived)
    pricing = batch / "runtime_snapshot/modelbench/minimal_value_20260905/pricing.json"
    card = read(pricing)
    known, tokens, metadata_count = 0.0, 0, 0
    new_unknown, copies, candidates = [], [], []
    entries = {e["run_id"]: e for e in manifest["schedule"]}
    entered = {e["call_id"]: e for e in events if e.get("event") == "transport_entered"}
    returned = {e["call_id"]: e for e in events if e.get("event") == "transport_returned"}
    all_metadata, all_started, all_tickets = set(), set(), set()
    for rid in group["run_ids"]:
        directory = batch / "results" / rid
        budget_path = bind(directory / "episode-budget.json")
        tickets = read(budget_path)["root"]["tickets"]
        all_tickets.update(tickets)
        metadata, started = {}, {}
        for name, target in (("metadata.json", metadata), ("started.json", started)):
            for path in sorted(directory.rglob(name)):
                record = read(bind(path))
                if record["call_id"] in target:
                    raise ValueError("Duplicate source call identity")
                target[record["call_id"]] = (path, record)
                copies.append((path, Path("evidence") / rid / record["call_id"] / name))
        all_metadata.update(metadata); all_started.update(started)
        account = reporting().accounting(directory, card)
        metadata_count += len(metadata)
        known += account["api_equivalent_known_subtotal_usd"]
        tokens += account["total_tokens_known_subtotal"]
        for cid in sorted(set(tickets) - set(metadata)):
            path, record = started[cid]
            if cid not in entered or cid in returned or tickets[cid]["status"] != "reserved":
                raise ValueError("Unexpected missing-call evidence")
            new_unknown.append({"run_id": rid, "call_id": cid, "model": record["model_requested"],
                                "role": record["role"], "reserved_tokens": tickets[cid]["reserved_tokens"],
                                "started_path": str(path.resolve()), "started_sha256": sha(path),
                                "actual_tokens": None, "actual_cost_usd": None})
        source_result = directory / "result.json"
        if not source_result.exists():
            continue
        result = read(bind(source_result))
        artifact = result.get("artifact") or result.get("lead_artifact") or {}
        patch = bind(directory / "model.patch")
        if result.get("run_id") != rid or result.get("arm") != entries[rid]["arm"] or result.get("instance_id") != entries[rid]["instance"]["instance_id"]:
            raise ValueError("Source generation identity mismatch")
        if artifact.get("status") != "present" or artifact.get("sha256") != sha(patch):
            raise ValueError("Source generation patch mismatch")
        call_audit = auditor.audit_new_attempt(directory, event_paths, run_id=rid, pricing_path=pricing)
        references.update(call_audit["references"])
        relevant = [e for e in events if e.get("call_id") in tickets]
        eligibility = qualify_generation(result, call_audit, relevant, trip["at"])
        snapshot = read(budget_path)
        if snapshot["root"].get("frozen") is not True:
            raise ValueError("Root episode budget was not durably frozen")
        projected = deepcopy(result)
        if projected.get("root_budget_snapshot") is None and entries[rid]["arm"] != "R2":
            projected["root_budget_snapshot"] = snapshot
        integrity = reporting().audit_accounting(directory, projected, account)
        if integrity.get("passed") is not True:
            raise ValueError("Root generation budget audit failed")
        # Preserve all original tool event bytes and explicitly require the
        # environment fence before TRIP when final bookkeeping crosses it.
        fences = []
        for p in directory.rglob("events.jsonl"):
            bind(p)
            rows = [json.loads(line) for line in p.read_text("utf-8-sig").splitlines()]
            fences += [e["at"] for e in rows if e.get("event") == "environment_quiesced"]
        if eligibility["post_trip_bookkeeping"]:
            if not fences or max(map(timestamp, fences)) > timestamp(trip["at"]):
                raise ValueError("Boundary generation lacks a pre-TRIP environment fence")
            eligibility["latest_environment_quiesced_at"] = max(fences, key=timestamp)
        candidates.append({"run_id": rid, "entry": entries[rid], "source_result": str(source_result.resolve()),
                           "source_result_sha256": sha(source_result), "source_patch": str(patch),
                           "patch_sha256": sha(patch), "eligibility": eligibility, "accounting": account,
                           "accounting_integrity": integrity, "root_budget_snapshot": snapshot})
    if not (set(entered) == all_started == all_tickets and set(returned) == all_metadata):
        raise ValueError("Whole-wave call evidence does not reconcile")
    if (len(candidates), metadata_count, len(new_unknown), sum(x["reserved_tokens"] for x in new_unknown)) != (9, 270, 4, 94377):
        raise ValueError("Source incident does not match the authorized nine-artifact salvage")
    carry = read(batch / "carryover.json")
    legacy_unknown = carry["legacy_unknown_calls"]
    if len(legacy_unknown) != 3 or sum(x["reserved_tokens"] for x in legacy_unknown) != 118387:
        raise ValueError("Historical unknown-call carryover changed")
    unchanged(references)
    destination.mkdir(parents=True, exist_ok=False)
    for source, relative in copies:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha(target) != sha(source):
            raise ValueError("Copied call evidence changed")
    for item in candidates:
        target = destination / "results" / item["run_id"]
        target.mkdir(parents=True)
        shutil.copyfile(item["source_result"], target / "source-result.json")
        shutil.copyfile(item["source_patch"], target / "model.patch")
        write(target / "entry.json", item["entry"])
    summary = {"version": VERSION, "source_batch": str(batch), "source_manifest_sha256": sha(batch / "manifest.json"),
               "source_wave": str(wave), "trip": trip, "candidates": candidates, "references": references,
               "legacy_unknown_calls": legacy_unknown, "new_unknown_calls": new_unknown,
               "overall_unknown_count": 7, "overall_reserved_tokens": 212764,
               "overall_cost_computable": False, "accounting": {"known_call_count": metadata_count,
               "known_tokens": tokens, "known_api_equivalent_usd": known,
               "legacy_known_api_equivalent_usd": carry["legacy_known_cost_usd"],
               "combined_known_api_equivalent_usd": carry["legacy_known_cost_usd"] + known},
               "metadata_copies": 270, "started_copies": 274}
    write(destination / "SOURCE_AUDIT.json", summary)
    return summary


def score_one(cli, item, destination):
    directory = Path(destination) / "results" / item["run_id"]
    result = read(directory / "source-result.json")
    for key in ("artifact", "lead_artifact"):
        if isinstance(result.get(key), dict):
            result[key]["path"] = str((directory / "model.patch").resolve())
    from modelbench.minimal_value_20260905.environment import rootgrade_terminal

    def one_gib(*args, **kwargs):
        return rootgrade_terminal(*args, **kwargs, memory="1g")

    score, attempts = cli.grade_frozen(result, item["entry"], directory, grader=one_gib)
    if score.get("completed") is not True or type(score.get("resolved")) is not bool:
        raise ValueError("Official grading did not complete: " + item["run_id"])
    if score.get("patch_sha256") != item["patch_sha256"]:
        raise ValueError("Official scoring patch hash mismatch")
    for relative, expected in score.get("reports_sha256", {}).items():
        if sha(Path(score["grader_dir"]) / relative) != expected:
            raise ValueError("Official report hash mismatch")
    result["root_budget_snapshot"] = item["root_budget_snapshot"]
    result["budget"]["frozen"] = True
    result["budget_projection_note"] = "Source numeric budget and runtime deadline flags preserved; durable root freeze and snapshot projected as frozen CLI would after generation. No current-clock deadline recomputation."
    result.update(score=score, grading_pending=False, grading_attempts=attempts,
                  accounting=item["accounting"], accounting_integrity=item["accounting_integrity"],
                  source_generation={"path": item["source_result"], "sha256": item["source_result_sha256"],
                                     "patch_sha256": item["patch_sha256"], "eligibility": item["eligibility"]},
                  salvage_scored_at=now(), salvage_only=True)
    result.update(reporting().result_status(result, item["entry"]))
    result["api_equivalent_known_subtotal_usd"] = item["accounting"]["api_equivalent_known_subtotal_usd"]
    result["api_equivalent_usd"] = result["api_equivalent_known_subtotal_usd"]
    output = directory / "episode_result.json"
    write(output, result)
    return {"run_id": item["run_id"], "path": str(output.resolve()), "sha256": sha(output),
            "source_result_path": item["source_result"], "source_result_sha256": item["source_result_sha256"],
            "patch_sha256": item["patch_sha256"], "official_resolved": score["resolved"],
            "known_tokens": item["accounting"]["total_tokens_known_subtotal"],
            "known_api_equivalent_usd": result["api_equivalent_known_subtotal_usd"]}


def run(batch, wave, destination):
    batch, wave, destination = map(lambda p: Path(p).resolve(), (batch, wave, destination))
    cli, manifest = load_frozen(batch)
    source = prepare_sources(batch, wave, destination, cli, manifest)
    report = {k: deepcopy(v) for k, v in source.items() if k != "candidates"}
    report.update(status="RUNNING", started_at=now(), salvaged_valid=[], cleanup_confirmed=False,
                  model_calls_started=0, evaluation_memory="1g", auxiliary_memory="768m",
                  maximum_concurrent_grader_containers=2, grading_mode="serial")
    try:
        with cli.stage_lease(manifest["resource_lock_path"], destination) as lease:
            lease["retain"] = True
            try:
                for item in source["candidates"]:
                    if (destination / "STOP").exists() or (destination / "CANCEL").exists():
                        raise ValueError("Salvage stop requested")
                    unchanged(source["references"])
                    print(json.dumps({"event": "grading_started", "run_id": item["run_id"], "at": now()}), flush=True)
                    record = score_one(cli, item, destination)
                    unchanged(source["references"])
                    report["salvaged_valid"].append(record)
                    print(json.dumps({"event": "grading_completed", "run_id": item["run_id"],
                                      "resolved": record["official_resolved"], "at": now()}), flush=True)
            finally:
                cleanup = cli.cleanup_owned_episode(destination)
                write(destination / "cleanup.json", cleanup)
                report["cleanup_confirmed"] = cleanup.get("confirmed") is True
                lease["retain"] = not report["cleanup_confirmed"]
            if len(report["salvaged_valid"]) != 9 or not report["cleanup_confirmed"]:
                raise ValueError("Salvage scoring or cleanup is incomplete")
            unchanged(source["references"])
            report["status"] = "PASS"
    except BaseException as exc:
        report.update(status="FAILED", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        report["completed_at"] = now()
        report["valid_count"] = len(report["salvaged_valid"])
        report["resolved_count"] = sum(x["official_resolved"] for x in report["salvaged_valid"])
        write(destination / "SALVAGE_REPORT.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--wave", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    value = run(args.batch, args.wave, args.destination)
    print(json.dumps({"status": value["status"], "valid_count": value["valid_count"],
                      "resolved_count": value["resolved_count"]}), flush=True)


if __name__ == "__main__":
    main()
