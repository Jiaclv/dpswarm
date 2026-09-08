import json
import threading
import urllib.error
import urllib.request

import pytest
from dpswarm.session_server import SessionHub, create_server


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


def call(server, path, body=None, session=None, authorized=True, origin=None):
    headers = {}
    if authorized:
        headers["Authorization"] = "Bearer " + server.hub.root.token
    if session:
        headers["X-DPSwarm-Session"] = session
    if origin:
        headers["Origin"] = origin
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=data, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def bind(server, session, parent=None):
    return call(server, "/api/execution/root", {"parent_session_id": parent or session,
        "delegation_depth": 0, "provider": "mock", "model": "b-kimi"}, session)


def test_discovery_and_unknown_status_do_not_create_sessions(server):
    code, result = call(server, "/api/status", session="new", authorized=False)
    assert code == 200 and result["state"] == "not_started"
    assert result["bridge"]["session_isolation"] is True
    assert server.hub._states == {}
    assert not (server.hub.root.workspace / "sessions").exists()


def test_activation_scopes_cannot_share_roots_or_rename_execution_parent(server):
    assert bind(server, "a")[0] == 200
    assert bind(server, "b")[0] == 200
    a, b = server.hub.get("a"), server.hub.get("b")
    assert a is not b and a.workspace != b.workspace
    code, result = bind(server, "b", parent="a")
    assert code == 400 and result["error"] == "SESSION_SCOPE_MISMATCH"
    code, result = call(server, "/api/delegate", {"kind": "derive", "subtasks": [
        {"title": "fixture", "prompt": "no model", "provider": "mock", "model": "b-kimi"}]}, "a")
    assert code == 200 and len(result["items"]) == 1
    assert call(server, "/api/status", session="a")[1]["snapshot"]["open_worker_slots_used"] == 1
    assert call(server, "/api/status", session="b")[1]["snapshot"]["open_worker_slots_used"] == 0


def test_unauthorized_and_foreign_origin_requests_cannot_create_state(server):
    assert call(server, "/api/execution/root", {}, "new", authorized=False)[0] == 401
    assert call(server, "/api/execution/root", {}, "new", origin="https://example.com")[0] == 403
    assert not server.hub._states
    assert call(server, "/api/delegate", {})[1]["error"] == "SESSION_REQUIRED"


def test_invalid_identifier_cannot_escape_state_directory(server):
    code, result = call(server, "/api/execution/root", {}, "../outside")
    assert code == 400 and result["error"] == "INVALID_SESSION"
    assert not server.hub._states


def test_scoped_state_survives_sidecar_restart(server):
    assert bind(server, "persisted")[0] == 200
    workspace = server.hub.root.workspace
    original = server.hub.get("persisted").cp.root_lead_node
    server.hub.close()
    server.hub = SessionHub(workspace)
    code, result = call(server, "/api/status", session="persisted")
    assert code == 200 and original in result["snapshot"]["nodes"]
    assert bind(server, "persisted")[0] == 200


def test_sensitive_unknown_session_read_still_requires_bearer(server):
    assert call(server, "/api/events", session="missing", authorized=False)[0] == 401
    assert not server.hub._states
