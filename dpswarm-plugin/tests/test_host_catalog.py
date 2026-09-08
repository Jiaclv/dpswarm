"""Host-resolved projection: real HTTP/control plane and no model calls."""
import copy
import json
import threading
import urllib.error
import urllib.request
import pytest
from dpswarm.aa import AASnapshot
from dpswarm.control import ControlPlaneError
from dpswarm.server import PanelState
from dpswarm.session_server import SessionHub, create_server
from dpswarm.types import Level


def catalog(*models):
    return {"source": "dph", "models": [{"provider": "host", "model": m} for m in models]}


def root(p, model="novel-v4.1-temp"):
    return p.bind_execution_root({"parent_session_id": getattr(p, "host_session_id", "root"),
        "delegation_depth": 0, "provider": "host", "model": model})


def derive(p, model="novel-v4.1-temp", **kw):
    return p.delegate({"kind": "derive", "subtasks": [{"provider": "host", "model": model,
        "title": "offline", "prompt": "No model execution", **kw}]})


@pytest.fixture
def panel(tmp_path):
    p = PanelState(tmp_path / "panel"); p.aa = None
    yield p
    p.cp.close()


@pytest.fixture
def server(tmp_path):
    s = create_server(tmp_path / "server", 0)
    t = threading.Thread(target=s.serve_forever, daemon=True); t.start()
    yield s
    s.shutdown(); s.server_close(); s.hub.close(); t.join(timeout=3)


def request(s, path, body=None, session="one", auth=True, origin=None, raw=None):
    headers = {"Content-Type": "application/json"}
    if auth: headers["Authorization"] = "Bearer " + s.hub.root.token
    if session is not None: headers["X-DPSwarm-Session"] = session
    if origin: headers["Origin"] = origin
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(f"http://127.0.0.1:{s.server_port}{path}", data=data, headers=headers)
    try: response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error: response = error
    with response: return response.status, json.loads(response.read())


def test_unknown_model_root_worker_and_unknown_facts(panel):
    assert root(panel)[1]["error"] == "MODEL_UNAVAILABLE"
    result = panel.sync_host_catalog(catalog("novel-v4.1-temp"))
    assert result["availability_source"] == "host-resolved"
    assert result["level_source"] == "fixed-team-policy" and result["worker_point_weight"] == 2
    assert root(panel)[0]
    rn = panel.cp.proj.nodes[panel.cp.root_lead_node]
    assert rn.level == Level.B and panel.cp.proj.leases[rn.lease_id].points == 1
    ok, result = derive(panel); assert ok, result
    node = panel.cp.proj.nodes[result["items"][0]["node_id"]]
    assert node.level == Level.B and node.route.point_weight == 2
    assert panel.cp.proj.leases[node.lease_id].points == 2
    f = panel.cp.catalog.resolve("host", "novel-v4.1-temp")
    assert f.context_window is f.input_price_per_mtok is f.output_price_per_mtok is None
    assert f.aa_source == "unknown" and not f.aa_dimensional
    text = panel.cp.catalog.fact_sheet(8, 3)
    assert all(word in text for word in ["unknown", "非能力评级", "运营级别 B"])


def test_aa_cannot_supply_availability_or_change_policy(panel):
    panel.aa = AASnapshot({"models": {"known-high": {"overall": 99}}})
    panel.sync_host_catalog(catalog("novel-v4.1-temp")); assert root(panel)[0]
    before = panel.cp.snapshot()
    assert derive(panel, "known-high")[1]["error"] == "MODEL_UNAVAILABLE"
    assert panel.cp.snapshot() == before
    panel.sync_host_catalog(catalog("novel-v4.1-temp", "known-high"))
    assert derive(panel, "known-high")[0]
    assert panel.cp.catalog.resolve("host", "known-high").level == Level.B


def test_precise_aa_metadata_keeps_temporary_ids_separate(panel):
    panel.aa = AASnapshot({"snapshot_date": "fixture", "models": {
        "deepseek-v4-flash": {"overall": 70, "aliases": ["DeepSeek V4 Flash"]}}})
    panel.sync_host_catalog(catalog("deepseek-v4-flash", "deepseek-v4-flash-0908", "deepseek-v4.1-temp"))
    assert panel.cp.catalog.resolve("host", "deepseek-v4-flash").aa_dimensional == {"overall": 70.0}
    for m in ["deepseek-v4-flash-0908", "deepseek-v4.1-temp"]:
        assert panel.cp.catalog.resolve("host", m).aa_dimensional == {}


@pytest.mark.parametrize("body", [None, [], {}, {"source": "agent", "models": []},
    {"source": "dph", "models": [], "level": "S"},
    *[{"source": "dph", "models": [{"provider": "host", "model": "x", key: 999}]}
      for key in ["level", "price", "aa", "context_window", "point_weight"]],
    {"source": "dph", "models": [{"provider": "host"}]},
    *[{"source": "dph", "models": [{"provider": "host", "model": value}]} for value in [1, " x", ""]],
    {"source": "dph", "models": [{"provider": "host/other", "model": "x"}]},
    catalog("x", "x"), {"source": "dph", "models": [{}] * 4097}])
def test_bad_projection_no_mutation(panel, body):
    before = copy.deepcopy(panel.cp.catalog.facts)
    with pytest.raises(ControlPlaneError) as err: panel.sync_host_catalog(body)
    assert err.value.code == "HOST_CATALOG_INVALID"
    assert panel.host_catalog is None and panel.cp.catalog.facts == before
    assert not (panel.workspace / "host-model-catalog.required").exists()


def test_removal_blocks_admission_but_review_releases_existing_lease(panel):
    panel.sync_host_catalog(catalog("novel-v4.1-temp", "worker")); assert root(panel)[0]
    ok, result = derive(panel, "worker"); assert ok
    item = result["items"][0]
    ok, bound = panel.bind_execution({**item, "reservation_session_id": item["session_id"],
        "execution_session_id": "real-child", "parent_session_id": "root", "execution_provider": "dsh"})
    assert ok, bound
    ok, submitted = panel.submit_output({**item, **bound, "output": "already delivered"})
    assert ok, submitted
    panel.sync_host_catalog(catalog())
    before = panel.cp.snapshot()
    assert derive(panel, "worker")[1]["error"] == "MODEL_UNAVAILABLE"
    assert panel.cp.snapshot() == before
    ok, result = panel.review({"item_id": item["item_id"], "verdict": "accept"}); assert ok, result
    assert panel.cp.proj.leases[panel.cp.proj.nodes[item["node_id"]].lease_id].active is False


@pytest.mark.parametrize("kind", ["fission", "split"])
def test_no_topology_or_forged_authority(panel, kind):
    panel.sync_host_catalog(catalog("novel-v4.1-temp")); assert root(panel)[0]
    assert panel.delegate({"kind": kind, "subtasks": [{"provider": "host", "model": "novel-v4.1-temp"}]})[1]["error"] == "HOST_CATALOG_FIXED_TEAM_ONLY"
    assert derive(panel, source="human")[1]["error"] == "BAD_SUBTASK"
    assert derive(panel, level="S")[1]["error"] == "BAD_SUBTASK"


def test_restore_and_idempotency(tmp_path):
    hub = SessionHub(tmp_path / "state"); p = hub.get("one", create=True); p.aa = None
    assert p.sync_host_catalog(catalog("novel-v4.1-temp"))["revision"] == 1
    assert root(p)[0]
    before = (p.workspace / "host-model-catalog.json").read_bytes()
    assert p.sync_host_catalog(catalog("novel-v4.1-temp"))["revision"] == 1
    assert (p.workspace / "host-model-catalog.json").read_bytes() == before
    hub.close(); hub = SessionHub(tmp_path / "state")
    try:
        p = hub.get("one"); assert p.host_catalog["revision"] == 1
        assert root(p)[0] and derive(p)[0]
        assert p.sync_host_catalog(catalog())["revision"] == 2
    finally: hub.close()


@pytest.mark.parametrize("damage", ["missing_snapshot", "missing_marker", "bad_json", "bad_hash", "wrong_session"])
def test_damaged_projection_refuses_legacy_fallback(tmp_path, damage):
    hub = SessionHub(tmp_path / "state"); p = hub.get("one", create=True)
    p.sync_host_catalog(catalog("novel-v4.1-temp")); directory = p.workspace; hub.close()
    path = directory / "host-model-catalog.json"
    if damage == "missing_snapshot": path.unlink()
    elif damage == "missing_marker": (directory / "host-model-catalog.required").unlink()
    elif damage == "bad_json": path.write_text("{", encoding="utf-8")
    else:
        v = json.loads(path.read_text())
        if damage == "bad_hash": v["sha256"] = "0" * 64
        else: v["snapshot"]["scope_session_id"] = "other"
        path.write_text(json.dumps(v))
    hub = SessionHub(tmp_path / "state")
    try:
        for _ in range(2):
            with pytest.raises(ControlPlaneError) as err: hub.get("one")
            assert err.value.code == "HOST_CATALOG_STORAGE_UNAVAILABLE"
    finally: hub.close()


def test_http_auth_scope_strict_json_and_discovery(server):
    url = "/api/models/host-catalog"
    assert request(server, url, catalog("x"), auth=False)[0] == 401
    assert request(server, url, catalog("x"), origin="https://example.com")[0] == 403
    assert request(server, url, catalog("x"), session=None)[1]["error"] == "SESSION_REQUIRED"
    assert request(server, url, catalog("x"), session="../bad")[1]["error"] == "INVALID_SESSION"
    assert request(server, url, {"source": "dph", "models": [], "root_session_id": "other"})[0] == 400
    assert request(server, url, raw=b'{"source":"dph","source":"dph","models":[]}')[0] == 400
    assert not server.hub._states
    assert request(server, "/api/status")[1]["bridge"]["host_catalog_v1"] is True


def test_http_unknown_model_and_cross_session_isolation(server):
    code, result = request(server, "/api/models/host-catalog", catalog("novel-v4.1-temp"))
    assert code == 200 and result["session_id"] == "one"
    bind = {"parent_session_id": "one", "delegation_depth": 0, "provider": "host", "model": "novel-v4.1-temp"}
    assert request(server, "/api/execution/root", bind)[0] == 200
    assert request(server, "/api/delegate", {"kind": "derive", "subtasks": [{"provider": "host", "model": "novel-v4.1-temp"}]})[0] == 200
    assert request(server, "/api/models/host-catalog", catalog(), session="two")[0] == 200
    bind["parent_session_id"] = "two"
    assert request(server, "/api/execution/root", bind, session="two")[1]["error"] == "MODEL_UNAVAILABLE"
    assert server.hub.get("one").host_catalog["models"] != server.hub.get("two").host_catalog["models"]


def test_removal_and_admission_serialize(panel, monkeypatch):
    panel.sync_host_catalog(catalog("novel-v4.1-temp")); assert root(panel)[0]
    entered, release, removed = threading.Event(), threading.Event(), threading.Event()
    original = panel._route_from_subtask
    def held(*args, **kwargs):
        entered.set(); assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(panel, "_route_from_subtask", held); results = []
    a = threading.Thread(target=lambda: results.append(derive(panel)))
    z = threading.Thread(target=lambda: (panel.sync_host_catalog(catalog()), removed.set()))
    a.start(); assert entered.wait(5); z.start(); assert not removed.wait(.05)
    release.set(); a.join(5); z.join(5)
    assert results[0][0] and removed.is_set()
    monkeypatch.setattr(panel, "_route_from_subtask", original)
    assert derive(panel)[1]["error"] == "MODEL_UNAVAILABLE"
