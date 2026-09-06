"""Regression coverage for immutable, attempt-scoped submission evidence."""
from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from dpswarm import invariants, state
from dpswarm.control import ControlPlane, ControlPlaneError
from dpswarm.server import PanelState, default_catalog
from dpswarm.types import AcceptanceState, DelegationKind, ModelRoute, RejectAttribution


@pytest.fixture
def cp(tmp_path):
    plane = ControlPlane(store_path=tmp_path / "events.jsonl", catalog=default_catalog())
    yield plane
    plane.close()


def worker(cp):
    item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    node = cp.begin_node(item.item_id, ModelRoute("mock", "b-kimi"))
    cp.confirm_node(node.node_id)
    return item, node


def marker(cp):
    return cp.store.last_seq, cp.snapshot()


def assert_unchanged(cp, before):
    assert (cp.store.last_seq, cp.snapshot()) == before


def test_server_empty_submission_cannot_borrow_other_item_package(tmp_path):
    panel = PanelState(tmp_path / "panel")
    cp = panel.cp
    try:
        a, na = worker(cp)
        b, nb = worker(cp)
        cp.submit(a.item_id, na.node_id, "A delivery")
        cp.submit(b.item_id, nb.node_id)
        before = marker(cp)
        ok, result = panel.review({"item_id": b.item_id, "verdict": "accept",
                                  "package_id": a.submission_package_id})
        assert not ok and result["error"] == "PACKAGE_MISSING"
        assert b.acceptance == AcceptanceState.SUBMITTED
        assert_unchanged(cp, before)
    finally:
        cp.close()


def test_direct_complete_accept_rejects_other_package_without_releasing_resources(cp):
    a, na = worker(cp)
    b, nb = worker(cp)
    cp.submit(a.item_id, na.node_id, "A")
    cp.submit(b.item_id, nb.node_id, "B")
    cp.begin_finalize(b.item_id)
    before = marker(cp)
    with pytest.raises(ControlPlaneError, match="EVIDENCE_MISMATCH"):
        cp.complete_accept(b.item_id, a.submission_package_id, evidence_ready=True)
    assert_unchanged(cp, before)
    cp.complete_accept(b.item_id, b.submission_package_id, evidence_ready=True)
    assert b.acceptance == AcceptanceState.ACCEPTED
    assert not cp.proj.leases[nb.lease_id].active
    assert cp.proj.leases[na.lease_id].active


def test_accepted_event_invariant_rejects_cross_item_evidence(cp):
    a, na = worker(cp)
    b, nb = worker(cp)
    cp.submit(a.item_id, na.node_id, "A")
    cp.submit(b.item_id, nb.node_id)
    cp.begin_finalize(b.item_id)
    before = marker(cp)
    with pytest.raises(ControlPlaneError, match="PACKAGE_MISSING"):
        cp._transact(*cp._drain_pairs(b.item_id), ("work_item_accepted", {
            "item_id": b.item_id, "package_id": a.submission_package_id,
            "evidence_ready": True,
        }))
    assert_unchanged(cp, before)


def test_retry_clears_all_submission_identity_and_same_text_gets_new_package(cp):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "same delivery")
    old_pkg, old_submission = item.submission_package_id, item.submission_id
    cp.reject(item.item_id, "retry", RejectAttribution.DESCRIPTION)
    cp.prepare_retry(item.item_id)
    assert item.submission_package_id is None
    assert item.submission_sha256 == ""
    assert item.submission_id is None
    assert item.submission_node_id is None
    cp.submit(item.item_id, node.node_id, "same delivery")
    assert item.submission_package_id != old_pkg
    assert item.submission_id != old_submission
    assert cp.proj.packages[old_pkg]["attempt"] == 1
    assert cp.proj.packages[item.submission_package_id]["attempt"] == 2
    before = marker(cp)
    with pytest.raises(ControlPlaneError, match="EVIDENCE_MISMATCH"):
        cp.accept_submission(item.item_id, old_pkg)
    assert_unchanged(cp, before)
    cp.accept_submission(item.item_id)


def test_empty_retry_cannot_inherit_old_submission_or_rebind_old_package(cp):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "v1")
    old = item.submission_package_id
    cp.reject(item.item_id, "retry", RejectAttribution.DESCRIPTION)
    cp.prepare_retry(item.item_id)
    cp.submit(item.item_id, node.node_id)
    assert item.submission_package_id is None and item.submission_sha256 == ""
    before = marker(cp)
    with pytest.raises(ControlPlaneError, match="PACKAGE_MISSING"):
        cp.accept_submission(item.item_id, old)
    with pytest.raises(ControlPlaneError, match="PACKAGE_IMMUTABLE"):
        cp.store_evidence_package(item.item_id, old, "v1")
    assert_unchanged(cp, before)
    cp.store_evidence_package(item.item_id, "new-attempt-evidence", "v2")
    cp.accept_submission(item.item_id)


def test_delayed_evidence_is_bound_to_current_submission_and_replays(cp):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id)
    cp.begin_finalize(item.item_id)
    cp.store_evidence_package(item.item_id, "late-evidence", "late full delivery")
    package = cp.proj.packages["late-evidence"]
    assert package["submission_id"] == item.submission_id
    assert package["node_id"] == node.node_id
    assert package["attempt"] == item.attempt
    assert package["context_epoch"] == node.context_epoch
    assert package["session_id"] == node.session_id
    cp.store_evidence_package(item.item_id, "late-evidence", "late full delivery")
    cp.complete_accept(item.item_id, "late-evidence", evidence_ready=True)
    replay = state.Projection()
    for event in cp.store.read_all():
        replay = invariants.check_event(replay, event)
    assert replay.work_items[item.item_id].submission_id == item.submission_id
    assert replay.work_items[item.item_id].acceptance == AcceptanceState.ACCEPTED


def test_bound_package_cannot_be_replaced_renamed_or_reowned(cp):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "original")
    package_id = item.submission_package_id
    before = marker(cp)
    for new_id, body in ((package_id, "changed"), ("alias", "original")):
        with pytest.raises(ControlPlaneError):
            cp.store_evidence_package(item.item_id, new_id, body)
    package = dict(cp.proj.packages[package_id], item_id=cp._root_item_id())
    with pytest.raises(ControlPlaneError, match="PACKAGE_IMMUTABLE"):
        cp._record("package_stored", package)
    assert_unchanged(cp, before)
    cp.store_evidence_package(item.item_id, package_id, "original")
    cp.accept_submission(item.item_id)


@pytest.mark.parametrize("missing", [False, True])
def test_review_artifact_corruption_fails_atomically_and_can_retry(cp, tmp_path, missing):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "complete delivery")
    path = tmp_path / "artifacts" / cp.proj.packages[item.submission_package_id]["artifact_ref"]
    if missing:
        path.unlink()
    else:
        path.write_text("truncated", encoding="utf-8")
    before = marker(cp)
    with pytest.raises(ControlPlaneError) as exc:
        cp.accept_submission(item.item_id)
    assert exc.value.code == ("EVIDENCE_NOT_READABLE" if missing else "EVIDENCE_CORRUPT")
    assert item.acceptance == AcceptanceState.SUBMITTED
    assert_unchanged(cp, before)
    path.write_text("complete delivery", encoding="utf-8")
    cp.accept_submission(item.item_id)
    assert item.acceptance == AcceptanceState.ACCEPTED


def test_context_package_cannot_become_submission_evidence(cp):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id)
    digest = hashlib.sha256(b"context input").hexdigest()
    package = {"package_id": "shared-context", "item_id": item.item_id,
               "stage": "context", "content_hash": digest, "size": 13,
               "artifact_ref": f"{digest}.txt", "submission_id": item.submission_id,
               "node_id": node.node_id, "attempt": item.attempt,
               "context_epoch": node.context_epoch, "session_id": node.session_id,
               "bind_submission": True}
    before = marker(cp)
    with pytest.raises(ControlPlaneError, match="EVIDENCE_MISMATCH"):
        cp._record("package_stored", package)
    assert_unchanged(cp, before)


def test_rollover_invalidates_pending_submission_epoch(cp):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "old session result")
    epoch = node.context_epoch
    cp.begin_rollover(node.node_id, capsule_ref="capsule", capsule_hash="hash")
    cp.confirm_rollover(node.node_id)
    assert node.context_epoch == epoch + 1
    before = marker(cp)
    with pytest.raises(ControlPlaneError, match="EVIDENCE_MISMATCH"):
        cp.accept_submission(item.item_id)
    assert_unchanged(cp, before)
    cp.reject(item.item_id, "resubmit after rollover", RejectAttribution.CONTEXT)
    cp.prepare_retry(item.item_id)
    cp.submit(item.item_id, node.node_id, "new session result")
    cp.accept_submission(item.item_id)


def test_empty_evidence_does_not_bind_or_finalize(cp):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id)
    before = marker(cp)
    with pytest.raises(ControlPlaneError, match="EVIDENCE_NOT_READY"):
        cp.store_evidence_package(item.item_id, "empty", "")
    assert_unchanged(cp, before)


def test_memory_only_control_plane_retains_full_evidence():
    cp = ControlPlane(catalog=default_catalog())
    try:
        item, node = worker(cp)
        cp.submit(item.item_id, node.node_id, "complete in-memory delivery")
        assert cp.proj.packages[item.submission_package_id]["content"] == "complete in-memory delivery"
        cp.accept_submission(item.item_id)
        assert item.acceptance == AcceptanceState.ACCEPTED
    finally:
        cp.close()


def test_current_bound_submission_can_be_accepted_after_restart(cp, tmp_path):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "durable delivery")
    submission_id = item.submission_id
    cp.close()
    reopened = ControlPlane(store_path=tmp_path / "events.jsonl", catalog=default_catalog())
    try:
        assert reopened.proj.work_items[item.item_id].submission_id == submission_id
        reopened.accept_submission(item.item_id)
        assert reopened.proj.work_items[item.item_id].acceptance == AcceptanceState.ACCEPTED
    finally:
        reopened.close()


@pytest.mark.parametrize("content", ["line 1\r\nline 2\r\n", "line 1\nline 2\n"])
def test_artifact_hash_preserves_exact_line_endings(cp, tmp_path, content):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, content)
    package = cp.proj.packages[item.submission_package_id]
    artifact = tmp_path / "artifacts" / package["artifact_ref"]
    assert artifact.read_bytes() == content.encode("utf-8")
    cp.accept_submission(item.item_id)


def test_raw_accept_transaction_requires_readable_full_artifact(cp, tmp_path):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "real body")
    artifact = tmp_path / "artifacts" / cp.proj.packages[item.submission_package_id]["artifact_ref"]
    artifact.unlink()
    before = marker(cp)
    with pytest.raises(ControlPlaneError) as exc:
        cp._transact(("work_item_finalizing", {"item_id": item.item_id}),
                     *cp._drain_pairs(item.item_id), ("work_item_accepted", {
                         "item_id": item.item_id, "package_id": item.submission_package_id,
                         "evidence_ready": True,
                     }))
    assert exc.value.code == "EVIDENCE_NOT_READABLE"
    assert_unchanged(cp, before)


def test_evidence_without_new_submission_identity_is_not_silently_migrated(cp):
    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "historical body")
    events = deepcopy(cp.store.read_all())
    for event in events:
        if event.kind in ("work_item_submitted", "package_stored"):
            event.payload.pop("submission_id", None)
    # Replay is read-only and still understands old event structure; acceptance is fail-closed.
    cp.proj = state.replay(events)
    before = marker(cp)
    with pytest.raises(ControlPlaneError, match="EVIDENCE_MISMATCH"):
        cp.accept_submission(item.item_id)
    assert_unchanged(cp, before)


def test_benchmark_collection_reads_only_exact_accepted_package(cp, tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "modelbench"))
    from driver import _collect_accepted_texts

    item, node = worker(cp)
    cp.submit(item.item_id, node.node_id, "rejected old delivery")
    cp.reject(item.item_id, "retry", RejectAttribution.DESCRIPTION)
    cp.prepare_retry(item.item_id)
    cp.submit(item.item_id, node.node_id, "accepted current delivery")
    cp.accept_submission(item.item_id)
    assert _collect_accepted_texts(cp.store.read_all(), tmp_path / "artifacts") == ["accepted current delivery"]
    # Historical independent evidence packages remain readable through their accepted event reference.
    text = "legacy accepted delivery"
    digest = cp.write_artifact(text)
    legacy = [SimpleNamespace(kind="package_stored", payload={
                  "package_id": "legacy-pkg", "item_id": "legacy-item", "content_hash": digest,
                  "artifact_ref": f"{digest}.txt"}),
              SimpleNamespace(kind="work_item_accepted", payload={
                  "package_id": "legacy-pkg", "item_id": "legacy-item"})]
    assert _collect_accepted_texts(legacy, tmp_path / "artifacts") == [text]
