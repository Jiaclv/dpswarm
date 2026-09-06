"""Evidence-only A1 engineering gate, aligned to plan sections 7.3 and 14.

This revision does not rewrite the frozen gate, results, scores, or budgets.
Normal solver failures are retained. Coverage is required across the batch.
No candidate, grader, repair, or qualification can be started by this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

REVISION = "a1_engineering_coverage_20260905_v2"
NORMAL_PROTOCOL_ERRORS = {"invalid_json", "schema_validation_failed"}
NORMAL_TERMINAL = {"completed", "blocked", "budget_exhausted", "call_limit",
                   "cancelled_or_deadline", "no_action_exhausted"}


class GateError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GateError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Evidence:
    def __init__(self, batch):
        self.batch = Path(batch).resolve()
        self.hashes = {}

    def bind(self, path):
        path = Path(path).resolve()
        require(path.is_relative_to(self.batch), "Evidence escapes the A1 batch")
        require(path.is_file(), "Evidence file is missing: " + str(path))
        digest = sha(path)
        require(self.hashes.get(str(path), digest) == digest, "Evidence changed during audit")
        self.hashes[str(path)] = digest
        return path

    def json(self, path):
        return read(self.bind(path))

    def unchanged(self):
        require(all(Path(p).is_file() and sha(p) == h for p, h in self.hashes.items()),
                "A1 evidence changed during audit")


def admission_replay(snapshot):
    """Check every original root AND component reservation, using saved clocks.

    Actual usage may exceed a reservation. After its settlement, no new ticket
    can use an exhausted scope/root. Equal clock ticks use the larger commitment
    so an ambiguous ordering cannot silently grant extra budget.
    """
    root = snapshot["root"]
    require(root.get("frozen") is True, "Root ledger is not frozen")
    require(snapshot.get("persistence_error") is None, "Ledger persistence failed")
    scopes, owners = snapshot["scopes"], snapshot["ticket_scopes"]
    tickets = list(root["tickets"].values())
    require(set(root["tickets"]) == set(owners), "Ticket ownership differs from root ledger")
    for t in tickets:
        require(t.get("status") == "completed" and t.get("usage", {}).get("total_tokens") is not None,
                "Unsettled or unknown usage cannot pass")
        require(t["ticket_id"] in owners and owners[t["ticket_id"]] in scopes, "Unknown ticket scope")
        for key in ("reserved_at", "completed_at", "reserved_tokens"):
            require(type(t.get(key)) in (int, float) and math.isfinite(t[key]) and t[key] >= 0,
                    "Invalid saved ticket clock or reservation")
        require(t["completed_at"] >= t["reserved_at"], "Ticket settled before admission")
        require(root["created_at"] <= t["reserved_at"] < root["deadline_at"],
                "Ticket admitted outside original deadline")
    prior, rows = [], []
    for t in sorted(tickets, key=lambda x: (x["reserved_at"], x["ticket_id"])):
        scope = owners[t["ticket_id"]]
        row = {"call_id": t["call_id"], "scope": scope, "reserved_tokens": t["reserved_tokens"],
               "actual_tokens": t["usage"]["total_tokens"], "reserved_at": t["reserved_at"],
               "completed_at": t["completed_at"], "levels": {}}
        for level, limits, previous in (("root", root, prior),
                (scope, scopes[scope], [x for x in prior if owners[x["ticket_id"]] == scope])):
            def commitment(x):
                held, actual = x["reserved_tokens"], x["usage"]["total_tokens"]
                if x["completed_at"] == t["reserved_at"]:
                    return max(held, actual)
                return actual if x["completed_at"] < t["reserved_at"] else held
            committed = sum(commitment(x) for x in previous)
            cm = sum(x["role"] == "cm" for x in previous)
            allowance = limits["cm_call_allowance"]
            pool_count = cm if t["role"] == "cm" else len(previous) - cm
            pool_limit = allowance if t["role"] == "cm" else limits["max_calls"]
            require(pool_count < pool_limit, "Call pool exceeded at admission: " + t["call_id"])
            require(committed + t["reserved_tokens"] <= limits["token_limit"],
                    "Token quota exceeded at admission: " + t["call_id"] + "/" + level)
            row["levels"][level] = {"committed_before": committed,
                "committed_after_reserve": committed + t["reserved_tokens"], "limit": limits["token_limit"]}
        prior.append(t)
        rows.append(row)
    totals = {name: sum(t["usage"]["total_tokens"] for t in tickets if owners[t["ticket_id"]] == name)
              for name in scopes}
    total = sum(totals.values())
    return {"passed": True, "tickets": rows, "total_tokens": total,
            "root_overshoot_tokens": max(0, total - root["token_limit"]),
            "scope_overshoot_tokens": {n: max(0, total - scopes[n]["token_limit"]) for n, total in totals.items()},
            "admission_only_not_hard_consumption_cap": True}


def event_records(directory, evidence):
    rows = []
    for path in sorted(Path(directory).rglob("events.jsonl")):
        evidence.bind(path)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                rows.append({"path": str(path.resolve()), "line": number, **json.loads(line)})
    return rows


def protocol_audit(calls, events):
    executed = {e.get("call_id") for e in events if e.get("event") == "tool_started"}
    issues = []
    for call in calls:
        require(not call.get("error"), "Transport error: " + call["call_id"])
        require(call.get("transport_attempt_count") == 1 and call.get("attempt_count") == 1
                and call.get("retry_attempted") is False and call.get("reconnect_detected") is False,
                "Unreconciled or unexpected transport attempts: " + call["call_id"])
        problem = call.get("protocol_error")
        if problem:
            require(problem.get("code") in NORMAL_PROTOCOL_ERRORS
                    and call.get("history_continuation_safe") is True and call["call_id"] not in executed,
                    "Unclassified or executed invalid protocol output: " + call["call_id"])
            issues.append({"call_id": call["call_id"], "code": problem["code"],
                "message": problem.get("message"), "classification": "rejected_model_output",
                "executed": False, "usage_charged": True})
    return {"passed": True, "rejected_model_outputs": issues}


def artifact_check(artifact, evidence):
    require(artifact.get("status") == "present", "Frozen patch is missing")
    path = evidence.bind(artifact["path"])
    require(sha(path) == artifact.get("sha256") and path.stat().st_size == artifact.get("bytes"),
            "Frozen patch hash or byte length differs")
    return str(path)


def closure_check(result):
    require(result.get("quiesced") is True and result.get("cleanup_confirmed") is True,
            "Candidate quiescence or cleanup is unconfirmed")
    quiescence, closures = result.get("quiescence") or {}, result.get("environment_closures") or {}
    require("lead" in quiescence and set(quiescence) == set(closures), "Environment evidence is incomplete")
    for actor, q in quiescence.items():
        c = closures[actor]
        require(q.get("quiesced") is True and q.get("candidate_execution_fenced") is True
                and q.get("stopped_running") is False and q.get("stopped_pid") == 0
                and c.get("closed") is True and c.get("removed") is True and c.get("errors") == []
                and q.get("container_id") and q["container_id"] == c.get("container_id"),
                "Incomplete physical close evidence: " + actor)
    return {"passed": True, "actors": sorted(closures)}


def worker_coverage(result, events, evidence):
    workers, covered = result.get("workers") or [], []
    require(len({w["worker_id"] for w in workers}) == len(workers), "Duplicate worker identity")
    actual = 0
    for worker in workers:
        status = worker.get("status")
        require(status in NORMAL_TERMINAL, "Worker has a non-normal terminal status")
        node = worker["handle"]["node_id"]
        call_ids = {e.get("call_id") for e in events if e.get("event") == "call_settled"
                    and (e.get("handle") or {}).get("node_id") == node}
        actual += bool(call_ids)
        if worker.get("delta_status") == "present":
            path = evidence.bind(worker["patch_path"])
            require(sha(path) == worker.get("patch_sha256") and path.stat().st_size == worker.get("delta_bytes"),
                    "Worker delta differs from its frozen artifact")
        else:
            require(status != "completed", "Completed worker lacks frozen delivery")
        require(worker["worker_id"] in (result.get("quiescence") or {}), "Worker close evidence missing")
        if status == "completed" and worker.get("delta_status") == "present" and call_ids \
                and worker.get("reviewed") is True and worker.get("review_decision") == "adopt":
            covered.append(worker["worker_role"])
    require(actual == result.get("workers_with_actual_calls"), "Actual worker call projection differs")
    return covered


def score_check(directory, result, entry, evidence):
    score = result.get("score") or {}
    if score.get("failure_kind") == "candidate_empty_patch":
        require(result["artifact"]["bytes"] == 0 and result.get("official_resolved") is False,
                "Empty-patch outcome does not match artifact")
        return {"passed": True, "kind": "known_empty_patch"}
    require(score.get("completed") is True and type(score.get("resolved")) is bool,
            "Official grading is incomplete")
    require(score.get("patch_sha256") == result["artifact"]["sha256"]
            and result.get("official_resolved") is score["resolved"], "Official score/patch mismatch")
    require(score.get("process_exit_code") == 0 and score.get("cleanup_errors") == [], "Grader did not close cleanly")
    matches = []
    for path in sorted(Path(directory).glob("grade-*/grader/result.json")):
        saved = evidence.json(path)
        if all(saved.get(k) == score.get(k) for k in ("completed", "resolved", "patch_sha256", "reports_sha256")):
            matches.append((path, saved))
    require(matches, "No original grading result matches the terminal score")
    path, saved = matches[-1]
    request = evidence.json(path.parent / "request.json")
    for key, value in {"instance_id": entry["instance"]["instance_id"],
                       "base_commit": entry["instance"]["base_commit"], "image_id": entry["image"],
                       "patch_sha256": result["artifact"]["sha256"]}.items():
        require(score.get(key) == value and request.get(key) == value, "Grader identity mismatch: " + key)
    require(request.get("grader_contract") == entry["grader_contract"], "Grader contract differs")
    for key in ("scope", "input_artifacts", "environment_sha"):
        require(score.get("binding", {}).get(key) == entry["grader_contract"].get(key), "Grader binding differs")
    require(sha(evidence.bind(request["patch_path"])) == result["artifact"]["sha256"], "Grader patch copy differs")
    reports = saved.get("reports_sha256") or {}
    require(reports and set(reports) == set(saved.get("reports") or {}), "Official reports are not hash-bound")
    for relative, expected in reports.items():
        report_path = evidence.bind(path.parent / relative)
        require(sha(report_path) == expected, "Official report changed")
        item = read(report_path)[entry["instance"]["instance_id"]]
        require(item.get("resolved") is score["resolved"], "Official report verdict differs")
    return {"passed": True, "kind": "official_report", "report_count": len(reports)}


def coverage_check(rows):
    observed = {"D_test_handoff_adopt": [], "T_both_roles_handoff_adopt": [],
                "R2_independent_frozen_candidates": [], "R2_verified_selection": [], "budget_fallback": []}
    for row in rows:
        run_id, arm = row["run_id"], row["arm"]
        if arm == "D" and "test" in row.get("adopted_roles", []):
            observed["D_test_handoff_adopt"].append(run_id)
        if arm == "T" and set(row.get("adopted_roles", [])) == {"implementation", "test"}:
            observed["T_both_roles_handoff_adopt"].append(run_id)
        if row.get("r2_isolation"):
            observed["R2_independent_frozen_candidates"].append(run_id)
        if row.get("verified_selection"):
            observed["R2_verified_selection"].append(run_id)
        if row.get("budget_fallback"):
            observed["budget_fallback"].append(run_id)
    return {key: {"status": "observed" if ids else "not-covered", "run_ids": ids} for key, ids in observed.items()}


def evaluate(batch, *, expected_sources=None, expected_transport=None, resources=True):
    from .. import cli, freeze, reporting
    from .continuation_pipeline import resource_absence
    batch = Path(batch).resolve()
    evidence = Evidence(batch)
    manifest = cli.load_manifest(batch)
    evidence.bind(batch / "manifest.json")
    evidence.bind(batch / "manifest.sha256")
    freeze.verify_snapshot(batch, manifest)
    require(freeze.runtime_sources() == manifest["runtime_sources"], "Current audit runtime differs from frozen A1")
    require(expected_sources is None or manifest["runtime_sources"] == expected_sources, "Expected runtime differs from A1")
    require(expected_transport is None or manifest["transport_environment"] == expected_transport, "Expected transport differs from A1")
    require(manifest["stage"] == "a1" and manifest["scheduled_episodes"] == 10, "Ten-episode A1 is required")
    from ..contracts import validate_schedule
    validate_schedule(manifest["schedule"], "a1")
    state = evidence.json(batch / "state.json")
    require(state.get("completed_episodes") == 10 and state.get("completed_at") and not state.get("stop_reason")
            and not state.get("active_run_id") and not state.get("active_episodes"), "A1 is not cleanly terminal")
    require(not Path(manifest["resource_lock_path"]).exists(), "A shared experiment lease remains")
    original = cli.validate_a1
    try:
        original(batch, expected_sources=expected_sources, expected_transport=expected_transport)
        old = {"passed": True}
    except ValueError as exc:
        old = {"passed": False, "error": str(exc)}
    rows, failures = [], []
    for entry in manifest["schedule"]:
        directory = batch / "results" / entry["run_id"]
        row = {"run_id": entry["run_id"], "arm": entry["arm"]}
        try:
            result = evidence.json(directory / "episode_result.json")
            require(result.get("run_id") == entry["run_id"] and result.get("arm") == entry["arm"]
                    and result.get("instance_id") == entry["instance"]["instance_id"], "Episode identity differs")
            account = reporting.accounting(directory, read(cli.HERE / "pricing.json"))
            row["accounting"] = reporting.audit_accounting(directory, result, account)
            require(row["accounting"].get("passed") is True and reporting.stop_reason(result) is None,
                    "Original per-episode engineering contract failed")
            require(not account.get("protocol_issues"), "Model identity/retry accounting failed")
            calls = []
            for call in account["calls"]:
                calls.append(evidence.json(call["metadata_path"]))
                if call.get("raw_usage_source"):
                    evidence.bind(call["raw_usage_source"]["path"])
            events = event_records(directory, evidence)
            row["protocol"] = protocol_audit(calls, events)
            snapshot = evidence.json(directory / "episode-budget.json")
            require(all(snapshot["root"].get(k) == v for k, v in
                        {"max_calls": 28, "token_limit": 600000, "cm_call_allowance": 12}.items()),
                    "Root quota differs from the frozen A1 contract")
            from ..budget import R2_SCOPES
            wanted_scopes = R2_SCOPES if entry["arm"] == "R2" else {
                "solver": {"max_calls": 28, "token_limit": 600000, "cm_call_allowance": 12}}
            require(snapshot["scopes"] == wanted_scopes, "Component quotas differ from the A1 contract")
            row["admission"] = admission_replay(snapshot)
            exceeded_calls = {t["call_id"] for t in snapshot["root"]["tickets"].values()
                              if t["usage"]["total_tokens"] > t["reserved_tokens"]}
            row["tools_returned_by_overshooting_calls"] = [
                {"call_id": e["call_id"], "tool": e["tool_call"].get("name"), "at": e.get("at"),
                 "evidence_path": e["path"], "line": e["line"]} for e in events
                if e.get("event") == "tool_started" and e.get("call_id") in exceeded_calls]
            artifact_check(result["artifact"], evidence)
            row["score_binding"] = score_check(directory, result, entry, evidence)
            row["official_resolved"] = result["official_resolved"]
            row["normal_outcome"] = (result.get("outcome") or {}).get("status")
            row["budget_fallback"] = row["normal_outcome"] == "budget_exhausted"
            if entry["arm"] != "R2":
                row["closure"] = closure_check(result)
                row["adopted_roles"] = worker_coverage(result, events, evidence)
                roles = [w.get("worker_role") for w in result.get("workers", [])]
                allowed = {"D": {"test"}, "T": {"implementation", "test"}}.get(entry["arm"], set())
                require(set(roles).issubset(allowed) and len(roles) == len(set(roles)), "Unexpected worker topology")
                if set(roles) != allowed:
                    require(row["normal_outcome"] == "budget_exhausted", "Missing worker has no legal budget terminal")
            else:
                candidates = result.get("candidates") or {}
                require(set(candidates) == {"candidate_1", "candidate_2"}, "R2 candidate set is incomplete")
                paths, containers = [], []
                for candidate in candidates.values():
                    paths.append(artifact_check(candidate["artifact"], evidence))
                    closure_check(candidate)
                    require(not candidate.get("infrastructure_error"), "R2 candidate infrastructure failure")
                    containers.extend(c["container_id"] for c in candidate["environment_closures"].values())
                require(len(set(paths)) == 2 and len(set(containers)) == len(containers), "R2 candidates share workspace/container")
                selector = result.get("selector") or {}
                require(selector.get("cleanup_confirmed") is True and not selector.get("infrastructure_error")
                        and not selector.get("error"), "Selector engineering failure")
                require(result.get("quiesced") is True and result.get("cleanup_confirmed") is True, "R2 root not closed")
                selector_calls = [c for c in calls if c.get("role") == "selector"]
                require(selector_calls, "R2 selector was not actually admitted")
                verification = any(v.get("candidate_id") in candidates and v.get("check_id") in entry["public_checks"]
                    and v.get("command") == entry["public_checks"][v["check_id"]]
                    and type(v.get("exit_code")) is int and v.get("timed_out") is False
                    and v.get("official_grading") is False for v in selector.get("verifications", []))
                selected = result.get("selection") or {}
                choice = candidates.get(selected.get("selected")) or {}
                if result.get("selection_source") == "selector":
                    require(selector.get("status") == "selected" and verification
                            and selected.get("sha256") == (choice.get("artifact") or {}).get("sha256")
                            and result["artifact"]["sha256"] == selected.get("sha256"), "Selector hash/verification binding failed")
                    row["verified_selection"] = True
                else:
                    require(result.get("selection_source") == "fallback", "Unknown R2 selection source")
                    row["budget_fallback"] = selector.get("status") in {"call_limit", "budget_exhausted"}
                row["r2_isolation"] = True
            row["passed"] = True
        except Exception as exc:
            row.update(passed=False, error=type(exc).__name__ + ": " + str(exc))
            failures.append({"run_id": entry["run_id"], "error": row["error"]})
        rows.append(row)
    coverage = coverage_check(rows)
    for name, value in coverage.items():
        if value["status"] != "observed":
            failures.append({"coverage": name, "error": "Necessary actual path not covered"})
    absent = resource_absence(batch, manifest) if resources else {"confirmed": False, "status": "offline-only"}
    require(absent.get("confirmed") is True, "Physical resource absence has not been verified")
    evidence.unchanged()
    return {"revision": REVISION, "created_at": datetime.now(timezone.utc).isoformat(),
        "passed": not failures, "status": "PASS" if not failures else "BLOCKED", "batch": str(batch),
        "module_sha256": sha(__file__), "runtime_sources": manifest["runtime_sources"],
        "transport_environment": manifest["transport_environment"], "old_gate": old,
        "coverage": coverage, "episodes": rows, "failures": failures,
        "resource_absence": absent, "evidence_sha256": evidence.hashes,
        "boundaries": ["Scores and historical blocked statuses are unchanged.",
            "Coverage proves exercised paths, not collaboration benefit or reliability on every task.",
            "Token limits are admission limits; all actual overshoot remains charged.",
            "A1 includes documented resource changes; this is not proof of one uniform 1 GiB configuration.",
            "Restored deadline projections do not replace original execution timing.",
            "No A2/model/grader/qualification is launched by this gate."]}


def write_report(batch, report_dir=None, *, report_path=None, **kwargs):
    require((report_dir is None) != (report_path is None), "Select report_dir or report_path")
    path = Path(report_path).resolve() if report_path else Path(report_dir).resolve() / "gate.json"
    sidecar = path.with_suffix(".sha256")
    require(not path.exists() and not sidecar.exists(), "A new revision report path is required")
    result = evaluate(batch, **kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    with sidecar.open("x", encoding="ascii") as stream:
        stream.write(sha(path) + "\n")
    return {"passed": result["passed"], "revision": REVISION, "report_path": str(path), "report_sha256": sha(path),
            "failures": result["failures"], "coverage": result["coverage"]}

def verify_report(path, batch, *, expected_sources=None, expected_transport=None):
    path, batch = Path(path).resolve(), Path(batch).resolve()
    require(path.with_suffix(".sha256").read_text(encoding="ascii").strip() == sha(path), "Gate report hash differs")
    report = read(path)
    require(report.get("revision") == REVISION and report.get("module_sha256") == sha(__file__)
            and report.get("passed") is True and report.get("batch") == str(batch), "Gate report does not admit this A1")
    require(expected_sources is None or report["runtime_sources"] == expected_sources, "Gate runtime differs")
    require(expected_transport is None or report["transport_environment"] == expected_transport, "Gate transport differs")
    for item, digest in report["evidence_sha256"].items():
        require(Path(item).is_file() and sha(item) == digest, "A1 evidence changed since gate")
    from .. import cli, freeze
    manifest = cli.load_manifest(batch)
    freeze.verify_snapshot(batch, manifest)
    require(not Path(manifest["resource_lock_path"]).exists(), "An experiment resource lease remains")
    return {"passed": True, "batch": str(batch), "revision": REVISION,
            "report_path": str(path), "report_sha256": sha(path),
            "manifest_sha256": sha(batch / "manifest.json"), "state_sha256": sha(batch / "state.json")}


DEFAULT_REPORT = Path(__file__).resolve().parent / "a2-parallel-12-v1" / "a1-engineering-gate.json"


def validate_a1(batch, *, expected_sources=None, expected_transport=None):
    """Explicit alias target for a future operation process; never self-installs.

    Returns only deterministic, hash-bound evidence, so repeated publication
    preconditions do not change merely because this validator ran later.
    """
    return verify_report(DEFAULT_REPORT, batch, expected_sources=expected_sources,
                         expected_transport=expected_transport)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report-dir", type=Path)
    group.add_argument("--report-path", type=Path)
    args = parser.parse_args(argv)
    result = write_report(args.batch, args.report_dir, report_path=args.report_path)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
