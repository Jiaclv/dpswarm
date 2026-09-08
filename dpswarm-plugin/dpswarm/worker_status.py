"""Public worker status: metadata only, never prompts, paths or provider text."""

from .plugin_audit import valid_session_id


def _number(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _code(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    return value if value and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in value) else None


def worker_status(events, root_session_id, snapshot=None):
    """Fold scoped audit facts into an explicitly non-delivery UI projection."""
    workers = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            continue
        data, kind = event["data"], event.get("type")
        sid = data.get("worker_session_id")
        if data.get("root_session_id") != root_session_id or not valid_session_id(sid) or sid == root_session_id:
            continue
        if kind == "dpswarm/worker-budget-frozen":
            profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
            binding = data.get("policy_binding") if isinstance(data.get("policy_binding"), dict) else {}
            role = binding.get("label")
            workers.setdefault(sid, {
                "session_id": sid, "role": role if role in ("implementer", "tester", "reviewer") else "worker",
                "mode": profile.get("mode") if profile.get("mode") in ("manual", "auto", "unlimited") else "unknown",
                "token_limit": _number(profile.get("tokenLimit")), "call_limit": _number(profile.get("callLimit")),
                "phase": "working", "code": None, "calls": {}, "candidate_count": 0,
            })
            continue
        row = workers.get(sid)
        if row is None:
            continue
        if kind == "dpswarm/worker-budget-admitted" and valid_session_id(data.get("call_id")):
            row["calls"].setdefault(data["call_id"], {"reserved": _number(data.get("reserved_tokens")), "observed": None, "complete": False})
        elif kind == "dpswarm/worker-budget-settled" and data.get("call_id") in row["calls"]:
            call = row["calls"][data["call_id"]]
            call.update(observed=_number(data.get("observed_tokens")), complete=data.get("usage_complete") is True)
        elif kind == "dpswarm/worker-budget-closeout":
            row["phase"] = "closing"
        elif kind == "dpswarm/worker-budget-denied":
            row["code"] = _code(data.get("code"))
        elif kind == "dpswarm/worker-budget-failure":
            failure = data.get("failure")
            row["phase"] = "failed"
            row["code"] = _code(failure if isinstance(failure, str) else failure.get("code") if isinstance(failure, dict) else None)
        elif kind == "dpswarm/worker-diagnostic":
            diagnostic = data.get("diagnostic") if isinstance(data.get("diagnostic"), dict) else {}
            failure = diagnostic.get("failure") if isinstance(diagnostic.get("failure"), dict) else {}
            closeout = diagnostic.get("closeout") if isinstance(diagnostic.get("closeout"), dict) else {}
            cleanup = diagnostic.get("cleanup") if isinstance(diagnostic.get("cleanup"), dict) else {}
            row["phase"] = "completed" if (diagnostic.get("native_stop_reason") == "completed"
                and closeout.get("completion") == "completed" and not failure
                and cleanup.get("physical_cleanup_confirmed") is True) else "failed"
            row["code"] = _code(failure.get("code"))
            candidates = closeout.get("candidates")
            row["candidate_count"] = len(candidates) if isinstance(candidates, list) else 0
            row["report_available"] = closeout.get("report_available") is True
    # Old runs have no terminal diagnostic. Use only their durable node binding,
    # never infer success or label a terminated historical worker as still running.
    nodes = snapshot.get("nodes", {}) if isinstance(snapshot, dict) else {}
    if isinstance(nodes, dict):
        for node in nodes.values():
            if not isinstance(node, dict) or node.get("terminated") is not True:
                continue
            row = workers.get(node.get("execution_session_id"))
            if row and row["phase"] in ("working", "closing"):
                row["phase"] = "ended"
    result = []
    for row in workers.values():
        calls = list(row.pop("calls").values())
        observed = sum(c["observed"] or 0 for c in calls)
        committed = sum((c["observed"] or 0) if c["complete"] else max(c["reserved"] or 0, c["observed"] or 0) for c in calls)
        unknown = sum(not c["complete"] for c in calls)
        row.update(calls_used=len(calls), observed_tokens_lower_bound=observed, unknown_usage_calls=unknown,
                   remaining_tokens=max(0, row["token_limit"] - committed) if row["token_limit"] is not None else None,
                   remaining_calls=max(0, row["call_limit"] - len(calls)) if row["call_limit"] is not None else None)
        result.append(row)
    return {"available": True, "workers": result, "scope": "recorded worker state; saved candidates require Lead verification"}
