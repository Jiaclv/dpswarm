import pytest
from dpswarm.plugin_audit import EVENT_TYPES, PluginAuditError, PluginAuditStore


RECOVERY_EVENTS = (
    "dpswarm/worker-budget-team-run-resumed",
    "dpswarm/worker-budget-team-run-resume-ended",
    "dpswarm/verification-recovery",
)


def test_recovery_events_are_in_the_published_vocabulary_and_roundtrip(tmp_path):
    store = PluginAuditStore(tmp_path / "audit", "root", create=True)
    events = [{"type": name, "data": {"root_session_id": "root", "run_id": "original-run",
               "resume_id": "resume-1", "roles": ["tester", "reviewer"], "candidate_id": None}}
              for name in RECOVERY_EVENTS]
    assert all(name in EVENT_TYPES for name in RECOVERY_EVENTS)
    body = {"root_session_id": "root", "expected_revision": 0, "transaction_id": "resume-events", "events": events}
    result = store.append(body)
    assert [event["type"] for event in result["events"]] == list(RECOVERY_EVENTS)
    assert [event["data"] for event in result["events"]] == [event["data"] for event in events]
    store.close()
    restored = PluginAuditStore(tmp_path / "audit", "root")
    assert restored.read()["events"] == result["events"]
    restored.close()


@pytest.mark.parametrize("event_type", RECOVERY_EVENTS)
def test_recovery_event_cannot_cross_root(event_type, tmp_path):
    store = PluginAuditStore(tmp_path / "audit", "root", create=True)
    with pytest.raises(PluginAuditError) as caught:
        store.append({"root_session_id": "root", "expected_revision": 0, "transaction_id": "foreign",
                      "events": [{"type": event_type, "data": {"root_session_id": "foreign"}}]})
    assert caught.value.code == "SESSION_SCOPE_MISMATCH"
    assert store.read()["revision"] == 0
    store.close()


def test_recovery_vocabulary_does_not_accept_arbitrary_new_operations(tmp_path):
    store = PluginAuditStore(tmp_path / "audit", "root", create=True)
    with pytest.raises(PluginAuditError) as caught:
        store.append({"root_session_id": "root", "expected_revision": 0, "transaction_id": "unrecognized",
                      "events": [{"type": "dpswarm/worker-budget-team-run-recharge", "data": {}}]})
    assert caught.value.code == "PLUGIN_AUDIT_INVALID_EVENT"
    assert store.read()["revision"] == 0
    store.close()
