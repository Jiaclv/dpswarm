import concurrent.futures
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest
from dpswarm.plugin_audit import PluginAuditError, PluginAuditStore
from dpswarm.session_server import SessionHub, create_server


def transaction(root="root", revision=0, identity="txn-1", events=None):
    return {"root_session_id": root, "expected_revision": revision, "transaction_id": identity,
            "events": events or [{"type": "dpswarm/worker-budget-admitted", "data": {
                "root_session_id": root, "worker_session_id": "worker", "call_id": identity,
                "reserved_tokens": 20000, "usage_complete": False}}]}


def test_atomic_multi_event_replay_preserves_unknown_reservations(tmp_path):
    directory = tmp_path / "audit"
    store = PluginAuditStore(directory, "root", create=True)
    body = transaction(events=[
        {"type": "dpswarm/worker-budget-allocation-bound", "data": {"allocation_id": "a", "worker_session_id": "w"}},
        {"type": "dpswarm/worker-budget-frozen", "data": {"worker_session_id": "w", "profile": {"mode": "auto", "tokenLimit": 20000, "callLimit": 10}}},
        {"type": "dpswarm/worker-budget-admitted", "data": {"worker_session_id": "w", "call_id": "c", "reserved_tokens": 20000, "usage": None}},
    ])
    result = store.append(body)
    assert result["revision"] == 1 and [e["seq"] for e in result["events"]] == [1, 2, 3]
    assert {e["revision"] for e in result["events"]} == {1}
    assert len(store.path.read_bytes().splitlines()) == 2
    result["events"][2]["data"]["reserved_tokens"] = 0
    body["events"][2]["data"]["reserved_tokens"] = 0
    frozen = store.read()
    store.close()
    restored = PluginAuditStore(directory, "root")
    assert restored.read() == frozen
    assert restored.read()["events"][2]["data"]["reserved_tokens"] == 20000
    assert not any(e["type"].endswith("settled") for e in restored.read()["events"])
    restored.close()


def test_concurrent_cas_has_one_winner(tmp_path):
    store = PluginAuditStore(tmp_path / "audit", "root", create=True)
    barrier = threading.Barrier(2)
    def append(i):
        barrier.wait()
        try: return store.append(transaction(identity=f"race-{i}"))
        except PluginAuditError as error: return error
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(append, [1, 2]))
    assert sum(isinstance(r, dict) for r in results) == 1
    denied = next(r for r in results if isinstance(r, PluginAuditError))
    assert denied.code == "PLUGIN_AUDIT_REVISION_CONFLICT" and denied.details["revision"] == 1
    assert len(store.read()["events"]) == 1
    store.close()


def test_idempotency_survives_later_commits_and_restart_without_rewriting(tmp_path):
    directory = tmp_path / "audit"
    store = PluginAuditStore(directory, "root", create=True)
    body = transaction()
    store.append(body)
    store.append(transaction(revision=1, identity="second"))
    original = store.path.read_bytes()
    duplicate = store.append(body)
    assert duplicate["idempotent"] and duplicate["transaction_revision"] == 1 and duplicate["revision"] == 2
    assert store.path.read_bytes() == original
    changed = copy.deepcopy(body)
    changed["events"][0]["data"]["reserved_tokens"] = 1
    with pytest.raises(PluginAuditError) as caught: store.append(changed)
    assert caught.value.code == "PLUGIN_AUDIT_TRANSACTION_MISMATCH"
    assert store.path.read_bytes() == original
    store.close()
    store = PluginAuditStore(directory, "root")
    assert store.append(body)["idempotent"] is True and store.path.read_bytes() == original
    store.close()


@pytest.mark.parametrize("change", ["partial_tail", "whole_tail", "missing_journal", "missing_head", "missing_directory", "bad_bytes", "pending_head", "foreign_root"])
def test_corruption_loss_and_incomplete_commit_never_reset_or_truncate(tmp_path, change):
    directory = tmp_path / "audit"
    store = PluginAuditStore(directory, "root", create=True)
    store.append(transaction())
    path, head = store.path, store.head_path
    store.close()
    if change == "partial_tail": path.write_bytes(path.read_bytes()[:-4])
    elif change == "whole_tail": path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
    elif change == "missing_journal": path.unlink()
    elif change == "missing_head": head.unlink()
    elif change == "missing_directory": shutil.rmtree(directory)
    elif change == "bad_bytes": path.write_bytes(path.read_bytes().replace(b'20000', b'00001'))
    elif change == "pending_head": (directory / "head.pending").write_text("{}\n")
    elif change == "foreign_root":
        identity = json.loads(store.identity_path.read_text())
        identity["root_session_id"] = "elsewhere"
        store.identity_path.write_text(json.dumps(identity))
    original = path.read_bytes() if path.exists() else None
    with pytest.raises(PluginAuditError): PluginAuditStore(directory, "root", create=True)
    assert (path.read_bytes() if path.exists() else None) == original


def test_valid_hashes_cannot_hide_noncontiguous_revision_or_cross_root_copy(tmp_path):
    directory = tmp_path / "audit"
    store = PluginAuditStore(directory, "root", create=True)
    store.append(transaction())
    store.close()
    with pytest.raises(PluginAuditError): PluginAuditStore(directory, "other")
    lines = [json.loads(x) for x in store.path.read_bytes().splitlines()]
    lines[1]["revision"] = 3
    canonical = lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    lines[1]["hash"] = hashlib.sha256(canonical({k:v for k,v in lines[1].items() if k != "hash"})).hexdigest()
    raw = b"".join(canonical(x) + b"\n" for x in lines)
    store.path.write_bytes(raw)
    head = json.loads(store.head_path.read_bytes())
    head.update(revision=3, head_hash=lines[1]["hash"], byte_length=len(raw), file_sha256=hashlib.sha256(raw).hexdigest())
    store.head_path.write_bytes(canonical(head) + b"\n")
    with pytest.raises(PluginAuditError, match="chain"): PluginAuditStore(directory, "root")


def test_mutation_after_open_detected_before_read_or_append(tmp_path):
    store = PluginAuditStore(tmp_path / "audit", "root", create=True)
    store.append(transaction())
    store.path.write_bytes(store.path.read_bytes().splitlines(keepends=True)[0])
    with pytest.raises(PluginAuditError): store.read()
    with pytest.raises(PluginAuditError): store.append(transaction(revision=1, identity="later"))
    store.close()


def test_second_process_writer_lock_refused_until_owner_closes(tmp_path):
    directory = tmp_path / "audit"
    store = PluginAuditStore(directory, "root", create=True)
    code = """import sys
from pathlib import Path
from dpswarm.plugin_audit import PluginAuditStore, PluginAuditError
try:
    s=PluginAuditStore(Path(sys.argv[1]), 'root')
except PluginAuditError as e:
    print(e.code)
    raise SystemExit(7)
s.close()
"""
    run = subprocess.run([sys.executable, "-c", code, str(directory)], text=True, capture_output=True, timeout=10)
    assert run.returncode == 7 and "PLUGIN_AUDIT_LOCKED" in run.stdout
    store.close()
    run = subprocess.run([sys.executable, "-c", code, str(directory)], text=True, capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr


def test_failed_head_commit_not_acknowledged_and_never_frees_reservation(tmp_path, monkeypatch):
    directory = tmp_path / "audit"
    store = PluginAuditStore(directory, "root", create=True)
    monkeypatch.setattr(store, "_write_head", lambda _: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError): store.append(transaction())
    raw = store.path.read_bytes()
    assert b'"reserved_tokens":20000' in raw
    with pytest.raises(PluginAuditError, match="unresolved"): store.read()
    store.close()
    with pytest.raises(PluginAuditError): PluginAuditStore(directory, "root")
    assert store.path.read_bytes() == raw


def test_acknowledgment_only_after_both_file_fsyncs(tmp_path, monkeypatch):
    store = PluginAuditStore(tmp_path / "audit", "root", create=True)
    seen = []
    native = os.fsync
    def fsync(fd):
        seen.append(fd)
        return native(fd)
    monkeypatch.setattr(os, "fsync", fsync)
    result = store.append(transaction())
    assert result["revision"] == 1 and len(seen) >= 2
    store.close()


@pytest.mark.parametrize("change", ["bad_event", "foreign_event", "negative_revision", "bool_revision", "fractional_revision", "empty_events", "unsafe_number", "nan", "extra_field"])
def test_invalid_transaction_never_touches_ledger(tmp_path, change):
    store = PluginAuditStore(tmp_path / "audit", "root", create=True)
    body = transaction()
    if change == "bad_event": body["events"][0]["type"] = "work_item_submitted"
    elif change == "foreign_event": body["events"][0]["data"]["root_session_id"] = "other"
    elif change == "negative_revision": body["expected_revision"] = -1
    elif change == "bool_revision": body["expected_revision"] = True
    elif change == "fractional_revision": body["expected_revision"] = 1.5
    elif change == "empty_events": body["events"] = []
    elif change == "unsafe_number": body["expected_revision"] = 2**53
    elif change == "nan": body["events"][0]["data"]["usage"] = float("nan")
    elif change == "extra_field": body["mode"] = "unlimited"
    original = store.path.read_bytes()
    with pytest.raises(PluginAuditError): store.append(body)
    assert store.path.read_bytes() == original and store.read()["revision"] == 0
    store.close()


@pytest.fixture
def server(tmp_path):
    httpd = create_server(tmp_path / "state", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    httpd.hub.close()
    thread.join(timeout=3)


def call(server, body=None, root="root", auth=True, origin=None, path="/api/plugin-audit"):
    headers = {}
    if root is not None: headers["X-DPSwarm-Session"] = root
    if auth: headers["Authorization"] = "Bearer " + server.hub.root.token
    if origin: headers["Origin"] = origin
    raw = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=raw, headers=headers)
    try: response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error: response = error
    with response: return response.status, json.loads(response.read())


def test_http_auth_scope_get_missing_create_no_state(server):
    assert call(server, auth=False)[0] == 401
    assert call(server, origin="https://example.com")[0] == 403
    assert call(server, root=None)[0] == 400
    assert call(server, root="../outside")[0] == 400
    assert call(server)[1]["error"] == "PLUGIN_AUDIT_NOT_FOUND"
    assert call(server, transaction(root="other"))[1]["error"] == "SESSION_SCOPE_MISMATCH"
    assert call(server, transaction(revision=1))[1]["error"] == "PLUGIN_AUDIT_NOT_FOUND"
    assert not server.hub._states and not server.hub._audits
    assert not (server.hub.root.workspace / "sessions").exists()
    assert call(server, path="/api/status")[1]["bridge"]["plugin_audit_v1"] is True


def test_http_audit_roots_separate_from_business_and_each_other(server):
    assert call(server, transaction())[0] == 200
    assert call(server, transaction(root="other"), root="other")[0] == 200
    a, b = call(server)[1], call(server, root="other")[1]
    assert a["root_session_id"] == "root" and b["root_session_id"] == "other"
    assert a["head_hash"] != b["head_hash"]
    assert not server.hub._states
    assert not list((server.hub.root.workspace / "sessions").glob("*/events.jsonl"))
    assert call(server, path="/api/status")[1]["state"] == "not_started"
    bad = transaction(revision=1, identity="cross")
    bad["events"][0]["data"]["root_session_id"] = "other"
    assert call(server, bad)[0] == 400 and call(server)[1] == a


def test_http_cas_idempotency_and_hub_restart(server):
    barrier = threading.Barrier(2)
    def append(i):
        barrier.wait()
        return call(server, transaction(identity=f"parallel-{i}"))
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(append, [1, 2]))
    assert sorted(code for code, _ in results) == [200, 409]
    previous = call(server)[1]
    workspace = server.hub.root.workspace
    server.hub.close()
    server.hub = SessionHub(workspace)
    assert call(server)[1] == previous
    winner = previous["events"][0]["transaction_id"]
    assert call(server, transaction(identity=winner))[1]["idempotent"] is True
    assert call(server, transaction(identity="loser"))[1]["error"] == "PLUGIN_AUDIT_REVISION_CONFLICT"


def test_http_missing_initialized_directory_never_recreated(server):
    assert call(server, transaction())[0] == 200
    audit = server.hub._audits["root"]
    directory = audit.directory
    audit.close()
    server.hub._audits.clear()
    shutil.rmtree(directory)
    assert call(server)[1]["error"] == "PLUGIN_AUDIT_MISSING"
    assert call(server, transaction())[1]["error"] == "PLUGIN_AUDIT_MISSING"
    assert not directory.exists()


def test_http_audit_and_business_root_coexist_without_shared_vocabulary(server):
    assert call(server, transaction())[0] == 200
    before = call(server)[1]
    body = {"parent_session_id": "root", "delegation_depth": 0, "provider": "mock", "model": "b-kimi"}
    assert call(server, body, path="/api/execution/root")[0] == 200
    state = server.hub.get("root")
    assert state is not None and not any(e.kind.startswith("dpswarm/") for e in state.cp.store.read_all())
    assert call(server)[1] == before



def test_new_process_replays_unsettled_reservation_without_synthesizing_usage(tmp_path):
    directory = tmp_path / "audit"
    store = PluginAuditStore(directory, "root", create=True)
    expected = store.append(transaction())
    store.close()
    code = """import json,sys
from pathlib import Path
from dpswarm.plugin_audit import PluginAuditStore
s=PluginAuditStore(Path(sys.argv[1]), 'root')
print(json.dumps(s.read()))
s.close()
"""
    run = subprocess.run([sys.executable, "-c", code, str(directory)], text=True, capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr
    loaded = json.loads(run.stdout)
    assert loaded["events"] == expected["events"] and loaded["head_hash"] == expected["head_hash"]
    assert loaded["events"][0]["data"]["reserved_tokens"] == 20000
    assert loaded["events"][0]["data"]["usage_complete"] is False


@pytest.mark.parametrize("raw", [b'{"root_session_id":"root","root_session_id":"other"}', b'{"root_session_id":NaN}', b'\xff', b'[]'])
def test_http_malformed_json_does_not_initialize_audit(server, raw):
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/plugin-audit", data=raw,
        headers={"X-DPSwarm-Session":"root", "Authorization":"Bearer " + server.hub.root.token})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=5)
    assert caught.value.code == 400
    caught.value.close()
    assert not server.hub._audits and not (server.hub.root.workspace / "sessions").exists()


def route_transaction():
    return transaction(events=[{"type":"dpswarm/route-bound","data":{
        "protocol":"fixed-role-route-v1", "root_session_id":"root", "parent_session_id":"root",
        "child_session_id":"child", "owner_session_id":"child", "label":"dpswarm:DPswarm implementer",
        "route":{"provider":"deepseek", "model":"deepseek-v4-flash", "reasoningEffort":"max"}}}])


def test_route_binding_is_independent_and_survives_cold_store(tmp_path):
    directory = tmp_path / "route"
    store = PluginAuditStore(directory, "root", create=True)
    saved = store.append(route_transaction())
    store.close()
    restored = PluginAuditStore(directory, "root")
    assert restored.read()["events"] == saved["events"]
    assert len(saved["events"]) == 1 and saved["events"][0]["type"] == "dpswarm/route-bound"
    assert not any("budget" in e["type"] or "/cm-" in e["type"] for e in saved["events"])
    restored.close()


@pytest.mark.parametrize("field,value", [("root_session_id","foreign"), ("parent_session_id","foreign"),
    ("owner_session_id","sibling"), ("child_session_id","root"), ("protocol","old"),
    ("route",{"provider":"p","model":"m","reasoningEffort":None}),
    ("route",{"provider":"p","model":"m","unexpected":True}), ("label","ordinary")])
def test_invalid_route_binding_never_writes(tmp_path, field, value):
    store = PluginAuditStore(tmp_path / "route", "root", create=True)
    body = route_transaction(); body["events"][0]["data"][field]=value
    before=store.path.read_bytes()
    with pytest.raises(PluginAuditError) as caught: store.append(body)
    assert caught.value.code in {"PLUGIN_AUDIT_INVALID_ROUTE","SESSION_SCOPE_MISMATCH"}
    assert store.path.read_bytes() == before and store.read()["revision"] == 0
    store.close()
