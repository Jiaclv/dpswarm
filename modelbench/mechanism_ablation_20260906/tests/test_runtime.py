"""C1 inherited control, treatment isolation and review snapshots; no model or Docker."""
from copy import deepcopy
import json
import threading

import pytest

from modelbench.mechanism_ablation_20260906 import contracts
from modelbench.mechanism_ablation_20260906.runtime import AblationRun
from modelbench.big_budget_team_20260906.tests.test_runtime import Environment, Transport
from modelbench.minimal_value_20260905 import runner as legacy


def entry_for(condition):
    plan = contracts.load_plan()
    cell = next(row for row in contracts.load_cells() if row["condition_id"] == condition)
    task = next(task for task in plan["tasks"] if task["instance_id"] == cell["instance_id"])
    instance = {"instance_id": task["instance_id"], "repo": task["repository"], "base_commit": task["base_commit"],
                "version": task["version"], "problem_statement": "Public offline fixture issue"}
    return contracts.build_entry(cell, instance, {}, task["image"], {"fixture": True})


@pytest.fixture
def create(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "RESOURCE_FAILURE", threading.Event())
    monkeypatch.setattr(legacy, "MODEL_SLOTS", threading.BoundedSemaphore(4))
    monkeypatch.setattr(legacy, "CONTAINER_SLOTS", threading.BoundedSemaphore(3))
    runs = []

    def make(condition="FCM_ON"):
        run = AblationRun(tmp_path / str(len(runs)), entry_for(condition),
                          transport_factory=Transport, environment_factory=Environment)
        run.transport.run = run
        runs.append(run)
        return run

    yield make
    for run in runs:
        run.cancel.set()
        run.pool.shutdown(wait=True)
        if not run.control._closed:
            run.control.close(reason="Offline C1 test teardown")


@pytest.mark.parametrize("condition", contracts.CONDITIONS)
def test_both_treatments_preserve_team_and_review_evidence(create, condition):
    run = create(condition)
    result = run.run()
    assert result["infrastructure_error"] is None and result["outcome"]["status"] == "completed"
    assert result["grading_pending"] and result["cleanup_confirmed"]
    assert result["condition_id"] == condition
    assert result["lead_model"] == "gpt-5.6-sol"
    assert [w.handle.model for w in run.workers.values()] == ["glm-5.3-flash"] * 2
    assert result["cm_call_count"] == 0
    reviews = [json.loads(path.read_text(encoding="utf-8")) for path in run.folder.glob("review_observations/*/review.json")]
    assert len(reviews) == 2
    for review in reviews:
        assert review["result"]["decision"] == "adopt"
        for field in ("pre", "post"):
            from pathlib import Path
            patch = Path(review[field]["patch_path"]).read_text(encoding="utf-8")
            assert legacy.sha(patch) == review[field]["patch_sha256"]
        assert review["delivery"]["patch_sha256"] == review["result"]["patch_sha256"]
        assert review["pre"]["patch_sha256"] != review["post"]["patch_sha256"]


def cm_record(call_id, **changes):
    return {"call_id": call_id, "model_requested": "deepseek-v4-flash", "role": "cm",
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            "usage_complete": True, "error": None, "protocol_error": None,
            "stop_reason": "stop", "transport_attempt_count": 1,
            "action": {"kind": "text", "text": "Verified fixture summary", "calls": []}, **changes}


def test_cm_off_keeps_full_history_and_direct_call_is_not_admitted(create, monkeypatch):
    run = create("FCM_OFF")
    messages = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "history" * 10000}]
    before = deepcopy(messages)
    monkeypatch.setattr(run.transport, "complete", lambda *a, **k: pytest.fail("CM off may not call transport"))
    run._maybe_compress_context(run.control.lead, messages, None)
    assert messages == before
    with pytest.raises(legacy.LedgerError, match="CM_DISABLED"):
        run._cm_call("off-call", run.control.lead, messages, {})
    assert run.budget.summary()["call_count"] == 0 and run.cm_calls == []


@pytest.mark.parametrize("changes", [
    {"stop_reason": "length"}, {"error": {"code": "socket_timeout"}},
    {"usage_complete": False}, {"protocol_error": {"code": "bad_json"}},
    {"action": {"text": "", "calls": []}},
])
def test_bad_cm_response_is_accounted_but_never_replaces_history(create, monkeypatch, changes):
    run = create()
    monkeypatch.setattr(run.transport, "complete", lambda *a, **k: cm_record(k["call_id"], **changes))
    messages = [{"role": "system", "content": "fixture"}] + [
        {"role": "user", "content": "public history " * 2000} for _ in range(8)]
    before = deepcopy(messages)
    run._maybe_compress_context(run.control.lead, messages, None)
    assert messages == before
    assert len(run.cm_calls) == 1 and run.budget.summary()["call_count"] == 1
    events = [json.loads(line) for line in (run.folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "cm_response_rejected" for e in events)
    assert not any(e["event"] == "cm_compression" for e in events)


def test_complete_cm_uses_deepseek_and_shared_root_tokens(create, monkeypatch):
    run = create()
    observed = []
    def complete(model, messages, **kwargs):
        observed.append((model, kwargs))
        return cm_record(kwargs["call_id"])
    monkeypatch.setattr(run.transport, "complete", complete)
    result = run._cm_complete_fn("cm-good", run.control.lead, {})(None, [{"role": "user", "content": "fixture"}])
    assert result.text == "Verified fixture summary"
    assert observed[0][0] == "deepseek-v4-flash"
    assert observed[0][1]["tools"] == [] and observed[0][1]["max_tokens"] == 4096
    assert run.budget.summary()["total_tokens"] == 120


def test_discard_observation_does_not_change_existing_decision(create):
    run = create()
    # The real fixed-team fixture produces and adopts both workers; a repeated
    # review must stay rejected, but the actual pre/post state is still recorded.
    run.run()
    worker = next(iter(run.workers.values()))
    env = Environment(run.instance, run.folder / "review-fixture")
    env.delta = "existing lead state"
    result = run.execute_tool("review_worker", {"worker_id": worker.worker_id, "decision": "discard", "reason": "fixture"},
                              run.control.lead, env, None)
    assert result == {"error": "WORKER_ALREADY_REVIEWED"}
    assert env.delta == "existing lead state"
    reviews = [json.loads(p.read_text(encoding="utf-8")) for p in run.folder.glob("review_observations/*/review.json")]
    repeated = next(r for r in reviews if r.get("result", {}).get("error") == "WORKER_ALREADY_REVIEWED")
    assert repeated["pre"]["patch_sha256"] == repeated["post"]["patch_sha256"]
