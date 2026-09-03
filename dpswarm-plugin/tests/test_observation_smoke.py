"""最小观察冒烟（§4 + §6）：3 个事件 → summarize_events 输出非空。"""
from dpswarm.events import DelegationRecord, Event
from dpswarm.observation import ObservationSink, summarize_events


def _sample_events():
    record = DelegationRecord(
        record_id="rec-1", item_id="item-1", node_id="node-w1",
        lead_node_id="node-lead",
        route={"provider": "mock", "model": "m1", "level": "B"},
        topology="derive", team="root",
        stop_reason="completed", outcome="accepted",
        token_input=100, token_output=50, cost_usd=0.01,
    )
    return [
        Event(seq=1, kind="observation_recorded", payload=record.to_payload()),
        Event(seq=2, kind="token_usage_recorded", payload={
            "node_id": "node-w1", "input": 100, "output": 50,
            "cache_read": 0, "cache_write": 0, "cost": 0.01}),
        Event(seq=3, kind="delegation_economics_recorded", payload={
            "item_id": "item-1", "lead_tokens": 800, "estimated_savings": 650}),
    ]


def test_summarize_events_nonempty():
    report = summarize_events(_sample_events())
    assert report["events"] == 3
    assert report["delegations"] == 1
    assert report["outcome_distribution"] == {"accepted": 1}
    assert report["stop_reason_distribution"] == {"completed": 1}
    assert report["token_totals"]["input"] == 100
    assert report["token_totals"]["output"] == 50
    assert report["token_by_node"]["node-w1"]["input"] == 100
    assert report["failures"] == 0


def test_observation_sink_economics_and_records():
    sink = ObservationSink(_sample_events())
    records = sink.delegation_records()
    assert len(records) == 1
    assert records[0].node_id == "node-w1"
    assert records[0].lead_node_id == "node-lead"
    assert sink.token_ledger()["node-w1"]["input"] == 100
    econ = sink.economics_summary()
    # node-lead 由 lead_node_id 推断为调度者；node-w1 为 worker；CM 无样本
    assert econ["worker_tokens"] == 150
    assert econ["lead_tokens"] == 0
    assert econ["cm_tokens"] == 0
    assert econ["est_saved"] == 650
    assert econ["events"][0]["item_id"] == "item-1"
