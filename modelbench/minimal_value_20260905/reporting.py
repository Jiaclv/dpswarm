"""Read-only accounting inputs; reports never feed official results to solvers."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
import statistics

from .contracts import ARMS, STAGES
from .accounting_audit import audit_accounting
from .selection import choose


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def integer(value):
    return type(value) is int and value >= 0


def collect_calls(episode_dir):
    calls = []
    seen = set()
    for path in sorted(Path(episode_dir).rglob("metadata.json")):
        item = read(path)
        if "call_id" not in item or "model_requested" not in item:
            continue
        identity = item["call_id"]
        if identity in seen:
            raise ValueError("Duplicate call record: " + str(identity))
        seen.add(identity)
        usage = {key: item.get(key) for key in (
            "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")}
        reported_total = usage["total_tokens"]
        if integer(usage["input_tokens"]) and integer(usage["output_tokens"]):
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        writes = item.get("cache_write_input_tokens")
        source = None
        if str(item["model_requested"]).startswith("gpt-"):
            raw = item.get("raw_artifacts") or {}
            candidates = [Path(raw[key]) for key in ("stdout", "stdout_jsonl") if raw.get(key)]
            candidates.append(path.parent / "stdout.jsonl")
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                completed = []
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "turn.completed":
                        completed.append(event.get("usage") or {})
                if len(completed) == 1:
                    recovered = completed[0]
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        if integer(usage[key]) and recovered.get(key) != usage[key]:
                            raise ValueError("Raw/normalized token mismatch: " + identity)
                    writes = recovered.get("cache_write_input_tokens")
                    if integer(recovered.get("reasoning_output_tokens")):
                        usage["reasoning_tokens"] = recovered["reasoning_output_tokens"]
                    source = {"path": str(candidate), "sha256": sha(candidate)}
                    break
        calls.append({"call_id": identity, "role": item.get("role"),
                      "model_requested": item["model_requested"], "model_reported": item.get("model_reported"),
                      "service_tier_requested": item.get("service_tier_requested"),
                      "service_tier_reported": item.get("service_tier_reported"),
                      "effort_requested": item.get("effort_requested"), "effort_reported": item.get("effort_reported"),
                      "adapter_mode": item.get("adapter_mode"), "transport_attempt_count": item.get("transport_attempt_count"),
                      "attempt_count": item.get("attempt_count"), "retry_attempted": item.get("retry_attempted"),
                      "reconnect_detected": item.get("reconnect_detected"),
                      "started_at": item.get("started_at"), "completed_at": item.get("completed_at"),
                      "wall_seconds": item.get("wall_seconds"), "error": item.get("error"),
                      "protocol_error": item.get("protocol_error"), "cache_write_input_tokens": writes,
                      "reported_total_tokens": reported_total,
                      "metadata_path": str(path), "metadata_sha256": sha(path), "raw_usage_source": source, **usage})
    return calls


def quote(call, card):
    """Fixed dated API-equivalent scenario, never a subscription cash invoice."""
    output = {"api_equivalent_usd": None, "standard_api_equivalent_usd": None,
              "cost_status": "unknown", "valuation_kind": card["valuation_kind"]}
    spec = card["models"].get(call["model_requested"])
    if spec is None:
        return output | {"cost_status": "model_unpriced"}
    if not all(integer(call.get(key)) for key in ("input_tokens", "cached_input_tokens", "output_tokens")):
        return output | {"cost_status": "usage_unknown"}
    inp, cache, out = (call[key] for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
    if cache > inp:
        raise ValueError("Cached input exceeds total input")
    if inp > spec.get("short_context_max_input", float("inf")):
        return output | {"cost_status": "outside_frozen_context_tariff"}
    write_multiplier = spec.get("cache_write_multiplier", 1)
    writes = call.get("cache_write_input_tokens")
    if write_multiplier != 1 and not integer(writes):
        return output | {"cost_status": "cache_write_split_unknown"}
    writes = writes if integer(writes) else 0  # Split irrelevant only when rates are identical.
    if writes + cache > inp:
        raise ValueError("Input token components overlap")
    exchange = 1 if spec["currency"] == "USD" else 1 / card["fx"]["cny_per_usd"]
    multiplier = 1
    crosses = False
    schedule = spec.get("peak_schedule")
    if schedule:
        def peak(value):
            at = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            hour = at.hour + at.minute / 60 + at.second / 3600
            return at.weekday() in schedule["weekdays_utc"] and any(a <= hour < b for a, b in schedule["hours_utc"])
        try:
            started_peak, completed_peak = peak(call["started_at"]), peak(call["completed_at"])
        except (ValueError, TypeError, AttributeError, KeyError):
            return output | {"cost_status": "price_time_unknown"}
        multiplier = schedule["multiplier"] if started_peak else 1
        crosses = started_peak != completed_peak
    requested = str(call.get("service_tier_requested", "")).lower()
    selected = spec.get("fast", spec["standard"]) if requested in ("fast", "priority") else spec["standard"]
    def total(rates):
        return ((inp - cache - writes) * rates["input"] + cache * rates["cached_input"]
                + writes * rates["input"] * write_multiplier + out * rates["output"]) * multiplier * exchange / 1e6
    retried = call.get("reconnect_detected") or call.get("retry_attempted") or (integer(call.get("attempt_count")) and call["attempt_count"] > 1)
    return output | {"api_equivalent_usd": None if retried else total(selected),
                     "observed_response_api_equivalent_usd": total(selected),
                     "standard_api_equivalent_usd": total(spec["standard"]),
                     "cost_status": "retry_usage_unreconciled" if retried else "fixed_dated_scenario",
                     "price_time_multiplier": multiplier, "crosses_price_boundary": crosses,
                     "pricing_tier_is_observed": False}


def accounting(episode_dir, card):
    calls = [call | quote(call, card) for call in collect_calls(episode_dir)]
    known = [c["api_equivalent_usd"] for c in calls if c["api_equivalent_usd"] is not None]
    unknown = len(calls) - len(known)
    return {"calls": calls, "call_count": len(calls), "cost_computable": unknown == 0,
            "api_equivalent_usd": sum(known) if unknown == 0 else None,
            "api_equivalent_known_subtotal_usd": sum(c.get("observed_response_api_equivalent_usd") or c.get("api_equivalent_usd") or 0 for c in calls), "unpriced_calls": unknown,
            "protocol_issues": protocol_issues(calls),
            "total_tokens_known_subtotal": sum(c["total_tokens"] for c in calls if integer(c.get("total_tokens"))),
            "usage_unknown_calls": sum(not all(integer(c.get(k)) for k in ("input_tokens", "output_tokens", "total_tokens")) for c in calls),
            "valuation_kind": card["valuation_kind"], "actual_billed_cash_usd": None}


def result_status(result, entry):
    artifact = result.get("artifact") or result.get("lead_artifact") or {}
    path = artifact.get("path") or result.get("patch_path")
    expected = artifact.get("sha256") or result.get("patch_sha256")
    bound = bool(path and expected and Path(path).is_file() and sha(path) == expected)
    health = result.get("execution_health") or {}
    clean = result.get("cleanup_confirmed") is True
    integrity = bound and clean and not result.get("infrastructure_error") and health.get("status") != "host_error"
    score = result.get("score") or {}
    official = score.get("resolved") if score.get("completed") else None
    if score.get("failure_kind") == "candidate_empty_patch" and score.get("resolved") is False:
        official = False
    budget = result.get("budget") or {}
    return {"official_resolved": official, "artifact_execution_integrity": integrity,
            "end_to_end_success": int(official is True and integrity),
            "usage_integrity": not budget.get("unknown_call_count") and not budget.get("pending_call_count"),
            "expected_workers": entry.get("expected_workers", 0)}


def protocol_issues(calls):
    issues = []
    for call in calls:
        wanted, reported = call.get("model_requested"), call.get("model_reported")
        matches = (reported == wanted or (str(wanted).startswith("deepseek-") and str(reported).startswith(wanted)))
        if reported and not matches:
            issues.append({"call_id": call["call_id"], "kind": "model_identity_drift"})
        if call.get("reconnect_detected") or call.get("retry_attempted") or (integer(call.get("attempt_count")) and call["attempt_count"] > 1):
            issues.append({"call_id": call["call_id"], "kind": "retry_usage_unreconciled"})
    return issues


def stop_reason(result):
    budget = result.get("budget") or {}
    if (result.get("accounting") or {}).get("protocol_issues"):
        return "transport_protocol_or_usage_unreconciled"
    if result.get("infrastructure_error") or (result.get("execution_health") or {}).get("status") == "host_error":
        return "infrastructure_failure"
    if result.get("cleanup_confirmed") is not True:
        return "cleanup_unconfirmed"
    if budget.get("unknown_call_count") or budget.get("pending_call_count"):
        return "unsettled_usage"
    if not result.get("artifact_execution_integrity"):
        return "artifact_integrity_failure"
    score = result.get("score") or {}
    if not score.get("completed") and score.get("failure_kind") != "candidate_empty_patch":
        return "grading_unavailable"
    return None


def write_report(batch):
    from .runtime_integrity import atomic_json
    batch = Path(batch)
    manifest = read(batch / "manifest.json")
    rows, all_calls = [], []
    for entry in manifest["schedule"]:
        path = batch / "results" / entry["run_id"] / "episode_result.json"
        if not path.is_file():
            continue
        result = read(path)
        rows.append({"run_id": entry["run_id"], "instance_id": entry["instance"]["instance_id"],
                     "repo": entry["instance"]["repo"], "arm": entry["arm"],
                     **{k: result.get(k) for k in ("official_resolved", "end_to_end_success", "inference_wall_seconds",
                                                  "api_equivalent_usd", "api_equivalent_known_subtotal_usd", "call_count")}})
        for call in (result.get("accounting") or {}).get("calls", []):
            all_calls.append({"episode_id": entry["run_id"], "arm": entry["arm"], **call})
    state = read(batch / "state.json") if (batch / "state.json").is_file() else {}
    complete = len(rows) == manifest["scheduled_episodes"] and state.get("stop_reason") is None
    summary = {"stage": manifest["stage"], "completed_episodes": len(rows), "scheduled_episodes": manifest["scheduled_episodes"],
               "stage_complete": complete, "stop_reason": state.get("stop_reason"),
               "api_equivalent_known_subtotal_usd": sum(r.get("api_equivalent_known_subtotal_usd") or 0 for r in rows),
               "calls": len(all_calls), "arms": {arm: {"episodes": sum(r["arm"] == arm for r in rows),
                    "successes": sum(r["end_to_end_success"] or 0 for r in rows if r["arm"] == arm)} for arm in ARMS}}
    if complete and manifest["stage"] == "a2":
        summary["selection"] = choose(rows)
    atomic_json(batch / "summary.json", summary)
    atomic_json(batch / "calls.json", all_calls)
    if rows:
        with (batch / "episodes.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    lines = ["# 最小协作实验记录", "", f"阶段：{manifest['stage']}；完成 {len(rows)}/{manifest['scheduled_episodes']}。", "",
             "| 策略 | 已完成 | 端到端成功 |", "|---|---:|---:|"]
    for arm, value in summary["arms"].items():
        lines.append(f"| {arm} | {value['episodes']} | {value['successes']} |")
    lines += ["", f"停止原因：{summary['stop_reason'] or '无'}。",
              f"已知用量在固定价目情景下的API等价费用：USD {summary['api_equivalent_known_subtotal_usd']:.4f}。",
              "费用是固定历史价目情景，不是订阅真实账单；未知不计为0。A1仅验证工程路径，A2仅开发筛选。", ""]
    (batch / "REPORT_ZH.md").write_text("\n".join(lines), encoding="utf-8")
    return summary
