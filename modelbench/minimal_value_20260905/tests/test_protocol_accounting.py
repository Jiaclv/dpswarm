from modelbench.minimal_value_20260905 import reporting as r
from pathlib import Path


def test_reconnected_call_keeps_observed_cost_but_full_cost_unknown():
    card = r.read(Path(r.__file__).with_name("pricing.json"))
    call = {"model_requested": "gpt-5.6-sol", "input_tokens": 1000,
            "cached_input_tokens": 0, "cache_write_input_tokens": 0,
            "output_tokens": 50, "reconnect_detected": True, "attempt_count": None}
    quote = r.quote(call, card)
    assert quote["api_equivalent_usd"] is None
    assert quote["observed_response_api_equivalent_usd"] > 0
    assert quote["cost_status"] == "retry_usage_unreconciled"


def test_model_echo_drift_stops_following_episodes():
    calls = [{"call_id": "c1", "model_requested": "gpt-5.6-sol", "model_reported": "gpt-5.6-luna"}]
    issues = r.protocol_issues(calls)
    assert issues[0]["kind"] == "model_identity_drift"
    assert r.stop_reason({"accounting": {"protocol_issues": issues}}) == "transport_protocol_or_usage_unreconciled"


def test_missing_echo_remains_unknown_without_inventing_mismatch():
    assert r.protocol_issues([{"call_id": "c1", "model_requested": "gpt-5.6-sol", "model_reported": None}]) == []


def test_semantic_action_error_does_not_become_transport_drift():
    assert r.protocol_issues([{"call_id": "c1", "model_requested": "glm-5.3", "model_reported": "glm-5.3",
                              "protocol_error": {"code": "invalid_tool_arguments"}}]) == []
