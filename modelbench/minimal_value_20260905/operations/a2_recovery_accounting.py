"""Read-only accounting boundary for the explicitly approved A2 B recovery.

Historical unknown usage stays unknown. Nothing in this module starts work,
rewrites a settlement, or authorizes a retry after another failure.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "a2_recovery_accounting_v1"
INCIDENT_AUDIT_SHA256 = "024615dee91e687b85396066d0656e940e31ce43e1a9e39ca56ebf28e2cc5146"
OLD_ADMISSION_TOKENS = 10_800_000
NEW_ATTEMPTS = 68
COMBINED_ADMISSION_CAP = 51_600_000
OLD_KNOWN_USD = 20.64064734586976


class RecoveryAccountingError(ValueError):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(condition, message):
    if not condition:
        raise RecoveryAccountingError(message)


def _read(path, references=None):
    path = Path(path).resolve()
    data = path.read_bytes()
    if references is not None:
        references[str(path)] = hashlib.sha256(data).hexdigest()
    return json.loads(data.decode("utf-8-sig"))


def _unchanged(references):
    for path, expected in references.items():
        _require(sha(path) == expected, "Evidence changed during audit: " + path)


def _reporting():
    # A standalone operations loader has no package. Reuse the caller-selected
    # reporting module; never switch sys.path or evict an existing frozen import.
    return importlib.import_module("modelbench.minimal_value_20260905.reporting")


def _belongs(identity, run_id):
    return isinstance(identity, str) and (identity == run_id or identity.startswith(run_id + "__"))


def audit_new_attempt(episode_dir, provider_event_paths, *, run_id, pricing_path=None):
    """Audit a stopped attempt; unrelated live episodes in the same group are ignored.

    Returns a finite known subtotal even when passed=False. That subtotal must
    never be interpreted as the complete cost of an incomplete attempt.
    """
    directory = Path(episode_dir).resolve()
    refs, issues, metadata, started = {}, [], {}, {}
    entered, returned = {}, {}
    tickets = {}
    unknown = set()

    def issue(value):
        if value not in issues:
            issues.append(value)

    for name, target in (("metadata.json", metadata), ("started.json", started)):
        for path in sorted(directory.rglob(name)):
            try:
                _require(path.resolve().is_relative_to(directory), "Call evidence escaped episode directory")
                record = _read(path, refs)
                identity = record.get("call_id")
                _require(isinstance(identity, str) and identity, "Missing call identity: " + str(path))
                _require(identity not in target, "Duplicate " + name + " call: " + identity)
                _require(_belongs(record.get("run_id"), run_id), "Foreign call record: " + identity)
                target[identity] = record
            except (ValueError, OSError) as exc:
                issue(str(exc))
    try:
        budget = _read(directory / "episode-budget.json", refs)
        tickets = budget["root"]["tickets"]
        _require(isinstance(tickets, dict), "Root tickets must be a mapping")
        _require(not budget.get("persistence_error"), "Budget persistence failed")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        issue("Cannot read root budget: " + str(exc))
        tickets = {}

    known_ids = set(metadata) | set(started) | set(tickets)
    related_files = 0
    for path in sorted(set(Path(p).resolve() for p in provider_event_paths)):
        try:
            data = path.read_bytes()
            parsed, malformed = [], []
            for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Event must be an object")
                    parsed.append((number, event))
                except ValueError:
                    malformed.append(number)
            related = [(n, e) for n, e in parsed if
                       _belongs(e.get("run_id"), run_id) or e.get("call_id") in known_ids]
            if not related:
                continue
            related_files += 1
            refs[str(path)] = hashlib.sha256(data).hexdigest()
            if malformed:
                issue("Malformed owned provider events: " + str(path))
            for _, event in related:
                target = entered if event.get("event") == "transport_entered" else (
                    returned if event.get("event") == "transport_returned" else None)
                if target is None:
                    continue
                identity = event.get("call_id")
                if not isinstance(identity, str) or not identity:
                    issue("Provider event has no call identity")
                    continue
                if identity in target:
                    issue("Duplicate provider " + event["event"] + ": " + identity)
                target[identity] = event
        except (ValueError, OSError) as exc:
            issue("Cannot read provider events: " + str(exc))
    if not related_files:
        issue("No provider evidence for this attempt")

    sets = {"entered": set(entered), "returned": set(returned), "started": set(started),
            "metadata": set(metadata), "tickets": set(tickets)}
    union = set().union(*sets.values())
    for kind, identities in sets.items():
        if identities != union:
            issue("Incomplete " + kind + " coverage")
    for identity in union:
        record, ticket = metadata.get(identity), tickets.get(identity)
        if record is None:
            unknown.add(identity)
        if ticket is None or ticket.get("status") != "completed":
            issue("Unsettled root ticket: " + identity)
        if record is not None and ticket is not None:
            usage = ticket.get("usage") or {}
            if any(usage.get(k) != record.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")):
                issue("Budget/metadata usage mismatch: " + identity)
        if record is not None:
            if not all(type(record.get(k)) is int and record[k] >= 0 for k in
                       ("input_tokens", "output_tokens", "total_tokens")):
                unknown.add(identity)
            elif record["input_tokens"] + record["output_tokens"] != record["total_tokens"]:
                issue("Inclusive usage arithmetic mismatch: " + identity)
            for events, label in ((entered, "entered"), (returned, "returned")):
                if identity in events and events[identity].get("model") != record.get("model_requested"):
                    issue("Provider/metadata model mismatch (" + label + "): " + identity)

    known = 0.0
    known_tokens = 0
    try:
        card_path = Path(pricing_path or HERE.parent / "pricing.json")
        account = _reporting().accounting(directory, _read(card_path, refs))
        value = account.get("api_equivalent_known_subtotal_usd")
        _require(type(value) in (float, int) and math.isfinite(value) and value >= 0,
                 "Known subtotal is not finite")
        known = float(value)
        known_tokens = account["total_tokens_known_subtotal"]
        if account.get("cost_computable") is not True or account.get("protocol_issues"):
            issue("Unpriced or protocol-invalid metadata")
        if {row["call_id"] for row in account["calls"]} != set(metadata):
            issue("Pricing/metadata call coverage mismatch")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        issue("Cannot price known metadata: " + str(exc))
    try:
        _unchanged(refs)
    except (ValueError, OSError) as exc:
        issue(str(exc))
    if unknown:
        issue("New unknown calls are not covered by historical recovery authorization")
    return {"version": VERSION, "run_id": run_id, "passed": not issues, "issues": sorted(issues),
            "new_unknown_calls": sorted(unknown), "known_api_equivalent_usd": known,
            "known_tokens": known_tokens, "overall_cost_computable": not issues,
            "reserved_tokens": sum(t.get("reserved_tokens", 0) for t in tickets.values()
                                   if t.get("status") == "reserved"),
            "counts": {key: len(value) for key, value in sets.items()},
            "references": dict(sorted(refs.items())), "automatic_retry_allowed": False}


def _snapshot(batch, manifest, refs):
    snapshot = batch / "runtime_snapshot"
    _require(bool(manifest.get("runtime_sources")) and bool(manifest.get("input_artifacts")),
             "Missing frozen source/input contract")
    for mapping, root in ((manifest["runtime_sources"], snapshot),
                          (manifest["input_artifacts"], snapshot / "modelbench/minimal_value_20260905/official")):
        for relative, expected in mapping.items():
            path = (root / relative).resolve()
            _require(path.is_relative_to(root.resolve()) and sha(path) == expected,
                     "Frozen evidence mismatch: " + relative)
            refs[str(path)] = expected
    return snapshot


def _valid_result(batch, entry, saved, reporting, card, refs):
    directory = batch / "results" / entry["run_id"]
    path = directory / "episode_result.json"
    result = _read(path, refs)
    _require(saved.get("sha256") == refs[str(path.resolve())] and
             Path(saved.get("path", "")).resolve() == path.resolve(), "Old result reference changed")
    _require(result.get("run_id") == entry["run_id"] and result.get("arm") == entry["arm"] and
             result.get("instance_id") == entry["instance"]["instance_id"], "Old result schedule mismatch")
    _require(result.get("quiesced") is True and result.get("cleanup_confirmed") is True,
             "Old result is not quiesced and cleaned")
    artifact = result.get("artifact") or result.get("lead_artifact") or {}
    patch = Path(artifact.get("path", "")).resolve()
    _require(patch.is_relative_to(directory.resolve()) and patch.is_file(), "Old patch ownership mismatch")
    refs[str(patch)] = sha(patch)
    _require(refs[str(patch)] == artifact.get("sha256") == result.get("score", {}).get("patch_sha256"),
             "Old scored patch mismatch")
    score = result.get("score") or {}
    _require(score.get("completed") is True and type(score.get("resolved")) is bool,
             "Old official score is unavailable")
    _require(score.get("instance_id") == entry["instance"]["instance_id"] and
             score.get("base_commit") == entry["instance"]["base_commit"], "Old score identity mismatch")
    _require(bool(score.get("reports")), "Old official report missing")
    grader = Path(score["grader_dir"]).resolve()
    _require(grader.is_relative_to(directory.resolve()), "Old grader ownership mismatch")
    for relative in score["reports"]:
        report_path = (grader / relative).resolve()
        _require(report_path.is_relative_to(grader), "Old report escaped grader")
        report = _read(report_path, refs)
        _require(refs[str(report_path)] == score["reports_sha256"].get(relative), "Official report changed")
        _require(report[entry["instance"]["instance_id"]]["resolved"] == score["resolved"],
                 "Official result/report disagreement")
    account = reporting.accounting(directory, card)
    integrity = reporting.audit_accounting(directory, result, account)
    _require(integrity.get("passed") is True and account.get("cost_computable") is True,
             "Old completed result accounting failed")
    reason = reporting.stop_reason({**result, "accounting": account, **reporting.result_status(result, entry)})
    _require(reason is None, "Old completed result contract failed: " + str(reason))
    for call in account["calls"]:
        path = Path(call["metadata_path"]).resolve()
        _require(path.is_relative_to(directory.resolve()) and sha(path) == call["metadata_sha256"],
                 "Old call metadata changed")
        refs[str(path)] = call["metadata_sha256"]
    return {"run_id": entry["run_id"], "instance_id": entry["instance"]["instance_id"],
            "arm": entry["arm"], "result_path": str((directory / "episode_result.json").resolve()),
            "result_sha256": saved["sha256"], "official_resolved": score["resolved"],
            "known_api_equivalent_usd": account["api_equivalent_known_subtotal_usd"]}


def build_carryover(old_batch, *, incident_dir=None):
    """Return deterministic references for approved 12-valid + 6-invalid carryover.

    This specific recovery permits the three already documented unknown calls;
    it never broadens that exception to another call or automatically retries.
    """
    batch = Path(old_batch).resolve()
    incident = Path(incident_dir or HERE / "a2-wave4-incident-v1").resolve()
    refs = {}
    manifest = _read(batch / "manifest.json", refs)
    _require((batch / "manifest.sha256").read_text("utf-8-sig").strip() == sha(batch / "manifest.json"),
             "Old manifest checksum mismatch")
    state = _read(batch / "state.json", refs)
    schedule = manifest.get("schedule", [])
    _require(manifest.get("stage") == "a2" and manifest.get("scheduled_episodes") == 80 and len(schedule) == 80,
             "Recovery must refer to the original fixed 80 episodes")
    _require(len({e["run_id"] for e in schedule}) == 80, "Duplicate original schedule identity")
    groups = {}
    for entry in schedule:
        groups.setdefault(entry["instance"]["instance_id"], []).append(entry["arm"])
        _require(entry["limits_override"]["token_limit"] == 600000, "Changed original per-attempt token limit")
    _require(len(groups) == 16 and all(sorted(arms) == ["D", "L", "R2", "S", "T"] for arms in groups.values()),
             "Original fixed 16-task/five-arm matrix changed")
    _require(state.get("completed_episodes") == 12 and len(state.get("episodes", [])) == 12 and
             not state.get("active_episodes") and state.get("completed_at") and
             state.get("stop_reason") == "parallel_trial_failed", "Old stage is not the closed 12-result prefix")
    _require(state.get("token_admission_sum") == OLD_ADMISSION_TOKENS, "Old admission must not be refunded")
    _require([r["run_id"] for r in state["episodes"]] == [e["run_id"] for e in schedule[:12]],
             "Old completed prefix does not match the frozen schedule")
    snapshot = _snapshot(batch, manifest, refs)
    reporting = _reporting()
    reporting_relative = "modelbench/minimal_value_20260905/reporting.py"
    _require(sha(reporting.__file__) == manifest["runtime_sources"].get(reporting_relative),
             "Accounting implementation differs from old frozen reporting")
    card = _read(snapshot / "modelbench/minimal_value_20260905/pricing.json", refs)
    valid = [_valid_result(batch, entry, saved, reporting, card, refs)
             for entry, saved in zip(schedule[:12], state["episodes"])]
    audit_path = incident / "ACCOUNTING_AUDIT.json"
    audit = _read(audit_path, refs)
    _require(refs[str(audit_path)] == INCIDENT_AUDIT_SHA256, "Approved incident accounting evidence changed")
    _require(Path(audit["batch_dir"]).resolve() == batch, "Incident belongs to another batch")
    group = Path(audit["group_dir"]).resolve()
    group_record = _read(group / "group.json", refs)
    _require(refs[str(group / "group.json")] == audit["group_sha256"] and
             group_record["manifest_sha256"] == sha(batch / "manifest.json"), "Incident group binding mismatch")
    trial = _read(group / "trial-state.json", refs)
    _require(trial.get("cleanup_confirmed") is True and not trial.get("active_episodes"),
             "Incident cleanup is not confirmed")
    _require(Path(state.get("parallel_trial", {}).get("group_dir", "")).resolve() == group,
             "Old state points to another incident group")
    failed_ids = [row["run_id"] for row in audit["runs"]]
    _require(len(failed_ids) == len(set(failed_ids)) == 6 and set(failed_ids) ==
             {entry["run_id"] for entry in schedule[12:18]}, "Incident failed-attempt set changed")
    failures = []
    for row in audit["runs"]:
        rid = row["run_id"]
        settlement_path = group / "settlements" / (rid + ".json")
        settlement = _read(settlement_path, refs)
        _require(state["parallel_trial"].get("settlements", {}).get(rid) == settlement and
                 trial.get("settlements", {}).get(rid) == settlement,
                 "Old state/trial settlement disagrees with durable incident record")
        budget_path = batch / "results" / rid / "episode-budget.json"
        _read(budget_path, refs)
        _require(refs[str(settlement_path)] == row["settlement_sha256"] and
                 refs[str(budget_path)] == row["budget_sha256"], "Incident settlement/budget changed")
        _require(settlement.get("cleanup_confirmed") is True and settlement.get("engineering_valid") is False and
                 not (batch / "results" / rid / "episode_result.json").exists(), "Failed attempt was reclassified or overwritten")
        observed = audit_new_attempt(batch / "results" / rid,
                                     (group / "provider-events").glob("*.jsonl"), run_id=rid,
                                     pricing_path=snapshot / "modelbench/minimal_value_20260905/pricing.json")
        approved_unknown = sorted(x["call_id"] for x in audit["unresolved_calls"] if x["run_id"] == rid)
        allowed = ({"Incomplete returned coverage", "Incomplete metadata coverage",
                    "New unknown calls are not covered by historical recovery authorization"} |
                   {"Unsettled root ticket: " + cid for cid in approved_unknown}) if approved_unknown else set()
        _require(observed["new_unknown_calls"] == approved_unknown and not (set(observed["issues"]) - allowed),
                 "New or changed incident accounting defect: " + rid)
        _require(observed["counts"]["metadata"] == row["metadata"] and
                 observed["counts"]["entered"] == row["entered"] and
                 observed["reserved_tokens"] == row["reserved_tokens"] and
                 observed["known_tokens"] == row["known_tokens"] and
                 math.isclose(observed["known_api_equivalent_usd"], row["known_api_equivalent_usd"],
                              rel_tol=0, abs_tol=1e-12), "Incident measured totals changed")
        refs.update(observed["references"])
        for call in settlement["accounting"]["calls"]:
            _require(sha(call["metadata_path"]) == call["metadata_sha256"], "Incident metadata changed")
        failures.append({"run_id": rid, "classification": "infra_invalid", "settlement_path": str(settlement_path),
                         "settlement_sha256": row["settlement_sha256"],
                         "known_api_equivalent_usd": row["known_api_equivalent_usd"]})
    for relative, expected in audit["provider_event_sha256"].items():
        path = group / relative
        _require(sha(path) == expected, "Incident provider events changed")
        refs[str(path)] = expected
    unknown = audit["unresolved_calls"]
    _require(len(unknown) == 3 and sum(x["reserved_tokens"] for x in unknown) == 118387 and
             audit["totals"]["metadata_records"] == 45 and audit["totals"]["entered_calls"] == 48,
             "Historical unknown-call exception changed")
    for item in unknown:
        path = batch / item["started_path"]
        _require(sha(path) == item["started_sha256"], "Historical unknown started record changed")
        refs[str(path)] = item["started_sha256"]
        _require(not (path.parent / "metadata.json").exists(), "Historical unknown evidence needs explicit new reconciliation")
    known = sum(row["known_api_equivalent_usd"] for row in valid + failures)
    _require(math.isclose(known, state["known_cost_usd"], rel_tol=0, abs_tol=1e-9) and
             math.isclose(known, OLD_KNOWN_USD, rel_tol=0, abs_tol=1e-9), "Old known-cost carryover does not reconcile")
    _unchanged(refs)
    return {"version": VERSION, "status": "PASS", "recovery_choice": "B", "old_batch": str(batch),
            "old_manifest_sha256": sha(batch / "manifest.json"), "old_state_sha256": sha(batch / "state.json"),
            "old_valid_count": 12, "old_infra_invalid_count": 6, "old_valid_results": valid,
            "old_failed_attempts": failures, "old_admission_tokens": OLD_ADMISSION_TOKENS,
            "new_attempt_count": NEW_ATTEMPTS, "new_admission_cap": NEW_ATTEMPTS * 600000,
            "combined_admission_cap": COMBINED_ADMISSION_CAP, "old_known_api_equivalent_usd": known,
            "historical_unknown_calls": unknown, "historical_unknown_call_count": 3,
            "historical_reserved_tokens": 118387, "reserved_is_actual_usage": False,
            "reserved_is_hard_cost_upper_bound": False, "overall_cost_computable": False,
            "actual_billed_cash_usd": None, "additional_automatic_retry_allowed": False,
            "carried_valid": [{"run_id": r["run_id"], "path": r["result_path"], "sha256": r["result_sha256"]}
                              for r in valid],
            "carried_valid12": [{"run_id": r["run_id"], "path": r["result_path"], "sha256": r["result_sha256"]}
                                for r in valid],
            "legacy_unknown_calls": unknown, "legacy_unknown_reserved_tokens": 118387,
            "legacy_token_admission_sum": OLD_ADMISSION_TOKENS, "legacy_known_cost_usd": known,
            "remaining_original_entries": schedule[12:], "references": dict(sorted(refs.items()))}
