import json

from dpswarm.worker_status import worker_status
from test_session_server_20260907 import server, call


def event(suffix, **data):
    return {"type": "dpswarm/worker-budget-" + suffix, "data": {
        "root_session_id": "root", "worker_session_id": "child", **data}}


def frozen():
    return event("frozen", profile={"mode": "manual", "tokenLimit": 1000, "callLimit": 4},
                 policy_binding={"label": "implementer"})


def test_reserved_unknown_is_not_free_and_other_roots_are_excluded():
    events = [frozen(), event("admitted", call_id="a", reserved_tokens=400),
              event("admitted", call_id="b", reserved_tokens=300),
              event("settled", call_id="a", observed_tokens=200, usage_complete=True),
              event("settled", call_id="b", observed_tokens=50, usage_complete=False),
              event("settled", call_id="a", observed_tokens=900, usage_complete=True, root_session_id="other")]
    row = worker_status(events, "root")["workers"][0]
    assert row["observed_tokens_lower_bound"] == 250
    assert row["remaining_tokens"] == 500
    assert row["remaining_calls"] == 2
    assert row["unknown_usage_calls"] == 1


def test_closeout_denial_is_not_automatically_a_terminal_worker_failure():
    row = worker_status([frozen(), event("closeout", mode="final_only"),
                         event("denied", code="WORKER_TOKEN_RESERVATION_DENIED")], "root")["workers"][0]
    assert row["phase"] == "closing"
    assert row["code"] == "WORKER_TOKEN_RESERVATION_DENIED"


def test_public_projection_does_not_leak_paths_prompts_messages_or_credentials():
    diagnostic = {"type": "dpswarm/worker-diagnostic", "data": {
        "root_session_id": "root", "worker_session_id": "child", "diagnostic": {
            "native_stop_reason": "error", "failure": {"code": "WORKER_TOKEN_RESERVATION_DENIED", "message": "secret-key"},
            "budget": {"prompt": "private-task"}, "closeout": {"report_available": False,
                "candidates": [{"path": "C:/private-customer-file", "operation": "write"}]}}}}
    projected = worker_status([frozen(), diagnostic], "root")
    row = projected["workers"][0]
    assert row["phase"] == "failed" and row["candidate_count"] == 1 and row["report_available"] is False
    serialized = json.dumps(projected)
    assert all(secret not in serialized for secret in ["secret-key", "private-task", "private-customer-file"])


def test_untrusted_error_text_is_not_a_public_error_code():
    row = worker_status([frozen(), event("denied", code="<script>secret</script>")], "root")["workers"][0]
    assert row["code"] is None


def test_public_status_recovers_metadata_without_exposing_audit(server):
    body = {"root_session_id": "root", "expected_revision": 0, "transaction_id": "new-status",
            "events": [frozen(), event("admitted", call_id="a", reserved_tokens=400),
                       event("closeout", mode="final_only", secret="not-public")]}
    assert call(server, "/api/plugin-audit", body, "root")[0] == 200
    code, result = call(server, "/api/status", session="root", authorized=False)
    assert code == 200 and result["state"] == "not_started"
    assert result["worker_diagnostics"]["workers"][0]["remaining_tokens"] == 600
    assert "not-public" not in json.dumps(result)
    assert call(server, "/api/plugin-audit", session="root", authorized=False)[0] == 401
    assert server.hub._states == {}


def test_historical_terminated_worker_is_not_claimed_running_or_completed():
    row = worker_status([frozen()], "root", {"nodes": {"n": {
        "execution_session_id": "child", "terminated": True}}})["workers"][0]
    assert row["phase"] == "ended"


def test_native_completed_without_confirmed_cleanup_is_not_a_completed_handoff():
    diagnostic = {"type": "dpswarm/worker-diagnostic", "data": {
        "root_session_id": "root", "worker_session_id": "child", "diagnostic": {
            "native_stop_reason": "completed", "failure": None,
            "closeout": {"completion": "partial", "report_available": True},
            "cleanup": {"physical_cleanup_confirmed": False}}}}
    assert worker_status([frozen(), diagnostic], "root")["workers"][0]["phase"] == "failed"
    diagnostic["data"]["diagnostic"]["closeout"]["completion"] = "completed"
    diagnostic["data"]["diagnostic"]["cleanup"]["physical_cleanup_confirmed"] = True
    assert worker_status([frozen(), diagnostic], "root")["workers"][0]["phase"] == "completed"


def test_missing_cleanup_metadata_is_unknown_not_a_public_status_error():
    diagnostic = {"type": "dpswarm/worker-diagnostic", "data": {
        "root_session_id": "root", "worker_session_id": "child", "diagnostic": {
            "native_stop_reason": "completed", "closeout": {"completion": "completed"},
            "cleanup": None}}}
    assert worker_status([frozen(), diagnostic], "root")["workers"][0]["phase"] == "failed"
