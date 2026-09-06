"""Reconcile immutable call evidence against the single root budget ledger."""
from __future__ import annotations

import json
from pathlib import Path

from .budget import EpisodeBudget, RunBudget
from .contracts import digest


def audit_accounting(directory, result, account):
    directory = Path(directory)
    nested = directory / "episode-budget.json"
    path = nested if nested.is_file() else directory / "budget.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if nested.is_file():
        restored = EpisodeBudget.from_snapshot(snapshot)
        ledger = restored.root
        if result.get("root_budget_snapshot") != snapshot:
            raise ValueError("R2 result and durable root budget differ")
    else:
        ledger = RunBudget.from_snapshot(snapshot)
    completed = {ticket["call_id"]: ticket for ticket in ledger.tickets.values()
                 if ticket["status"] == "completed"}
    calls = account["calls"]
    actual = {call["call_id"]: call for call in calls}
    if len(actual) != len(calls) or set(actual) != set(completed):
        raise ValueError("Completed root tickets and metadata call IDs differ")
    for identity, ticket in completed.items():
        metadata = json.loads(Path(actual[identity]["metadata_path"]).read_text(encoding="utf-8"))
        if digest(metadata) != ticket["record_hash"]:
            raise ValueError("Metadata differs from settled call record: " + identity)
    summary = ledger.summary()
    for key in ("call_count", "completed_call_count", "pending_call_count", "unknown_call_count",
                "known_subtotal", "reserved_tokens", "committed_tokens"):
        if (result.get("budget") or {}).get(key) != summary[key]:
            raise ValueError("Terminal budget summary differs from root ledger: " + key)
    if account["total_tokens_known_subtotal"] != summary["known_subtotal"]:
        raise ValueError("Inclusive input/output total differs from root accounting")
    return {"passed": True, "ledger_path": str(path), "snapshot_hash": snapshot["snapshot_hash"],
            "completed_call_count": len(completed), "pending_call_count": summary["pending_call_count"],
            "unknown_call_count": summary["unknown_call_count"]}
