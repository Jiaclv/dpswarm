"""控制面板服务器测试：PanelState API 语义 + 真实 HTTP 层。"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from dpswarm.server import Handler, PanelState


@pytest.fixture()
def state(tmp_path) -> PanelState:
    return PanelState(tmp_path / "ws")


class TestPanelState:
    def test_status_shape(self, state):
        s = state.status()
        assert s["spec"]["revision"] == 1
        assert s["snapshot"]["graph_revision"] == 0
        assert any(f["level"] == "S" for f in s["catalog"])

    def test_publish_spec_whitelist_and_validation(self, state):
        ok, resp = state.publish_spec({"max_open_work_items": 3, "bogus_field": 1})
        assert not ok and resp["errors"]["bogus_field"] == "unknown field"
        assert resp["errors"].get("max_open_work_items") is None or ok is False
        ok, resp = state.publish_spec({"max_open_work_items": "three"})
        assert not ok and "type mismatch" in resp["errors"]["max_open_work_items"]

    def test_publish_spec_new_revision_inherits_rest(self, state):
        before = dict(state.cp.proj.spec.__dict__)
        ok, resp = state.publish_spec({"max_open_work_items": 3})
        assert ok and resp["revision"] == 2
        after = state.cp.proj.spec.__dict__
        assert after["max_open_work_items"] == 3
        assert after["max_active_node_points"] == before["max_active_node_points"]
        assert after["spec_id"] == before["spec_id"]        # 同 spec 新 revision（§2.1）
        # config 型人工指令入链可审计
        assert "spec_published" in [e.kind for e in state.cp.store.read_all()]

    def test_directive_three_kinds(self, state):
        ok, r = state.directive({"kind": "terminal", "payload": {"scope": "root"}})
        assert ok
        ok, r = state.directive({"kind": "bad-kind"})
        assert not ok
        ok, r = state.directive({"kind": "immediate",
                                 "payload": {"op": "wakeup", "node_id": state.cp.root_lead_node}})
        assert ok
        kinds = [e.kind for e in state.cp.store.read_all()]
        assert "human_directive" in kinds and "node_wakeup" in kinds

    def test_run_task_default_mock_single_chain(self, state):
        r = state.run_task({})
        assert r["ok"] and "single" in r["result"]["actions"]
        root = state.cp._root_item_id()
        assert state.cp.proj.work_items[root].acceptance.value == "accepted"

    def test_run_task_fission_mock(self, state):
        script = [
            {"text": json.dumps({"action": "fission",
                                 "route": {"provider": "mock", "model": "b-kimi"},
                                 "subtasks": ["A", "B"]})},
            {"text": "W"},
            {"text": json.dumps({"verdict": "accept"})},
        ]
        r = state.run_task({"task": "两片", "mock_script": script})
        assert r["ok"] and len(r["result"]["items"]) == 2
        subteams = [t for t in state.cp.proj.teams if t != "root"]
        assert len(subteams) == 1

    def test_seal_and_tick(self, state):
        r = state.seal({"team": "root"})
        assert r["ok"] and r["phase"] == "completed"
        assert state.cp.proj.seal_phase["root"].value == "completed"
        r = state.tick()
        assert r["ok"]

    def test_seal_body_timed_out_field_ignored(self, state):
        """0.15：请求体 timed_out=True 不可伪造超时收尾——控制面事实推导
        （deadline 未过 → 正常 completed），防调用方跳过结算语义。"""
        r = state.seal({"team": "root", "timed_out": True})
        assert r["ok"] and r["phase"] == "completed"
        kinds = [e.kind for e in state.cp.store.read_all()]
        assert "seal_completed" in kinds and "seal_timed_out" not in kinds
        root = state.cp.proj.work_items[state.cp._root_item_id()]
        assert root.acceptance.value == "accepted"

    def test_seal_derives_timed_out_from_deadline(self, state):
        """0.15：树级 deadline 已过（控制面推导）→ 超时收尾，无需请求体声明。"""
        state.cp._root_started_at = time.time() - 20000  # 默认 deadline 4h=14400s
        r = state.seal({"team": "root"})
        assert r["ok"] and r["phase"] == "timed-out"
        kinds = [e.kind for e in state.cp.store.read_all()]
        assert "seal_timed_out" in kinds
        root = state.cp.proj.work_items[state.cp._root_item_id()]
        assert root.acceptance.value == "terminated"


class TestHTTPLayer:
    """真实端口 + urllib：路由、状态码、CORS、页面服务。"""

    @pytest.fixture()
    def base_url(self, tmp_path):
        Handler.state = PanelState(tmp_path / "ws")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{port}"
        httpd.shutdown()

    def _get(self, url, token=None, origin=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def _post(self, url, body, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_page_served(self, base_url):
        status, body, headers = self._get(base_url + "/")
        assert status == 200
        assert b"DPSwarm" in body
        assert "text/html" in headers["Content-Type"]
        # token 注入：占位符被实际 token 替换（面板 JS 持 token 调写接口）
        assert b"{{DPSWARM_TOKEN}}" not in body
        assert Handler.state.token.encode() in body

    def test_api_status_loopback_only(self, base_url):
        # 只读快照：无 Origin（本机进程）可达，不回 ACAO
        status, body, headers = self._get(base_url + "/api/status")
        assert status == 200
        assert "spec" in json.loads(body)
        assert headers.get("Access-Control-Allow-Origin") is None
        # loopback Origin（dph Web UI 状态灯）可达，ACAO 回显该 Origin
        status, _, headers = self._get(base_url + "/api/status",
                                       origin="http://127.0.0.1:3999")
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:3999"
        # 恶意网页 Origin 直接拒
        status, _, _ = self._get(base_url + "/api/status",
                                 origin="https://evil.example")
        assert status == 403

    def test_api_events_needs_token(self, base_url):
        status, body, _ = self._get(base_url + "/api/events?limit=5")
        assert status == 401
        status, body, _ = self._get(base_url + "/api/events?limit=5",
                                    token=Handler.state.token)
        data = json.loads(body)
        assert status == 200 and data["total"] >= 1 and len(data["events"]) <= 5

    def test_post_without_token_401(self, base_url):
        status, data = self._post(base_url + "/api/spec", {"max_team_workers": 2})
        assert status == 401

    def test_post_spec_validation_400(self, base_url):
        status, data = self._post(base_url + "/api/spec", {"nope": 1},
                                  token=Handler.state.token)
        assert status == 400 and data["ok"] is False

    def test_post_spec_and_directive_flow(self, base_url):
        tok = Handler.state.token
        status, data = self._post(base_url + "/api/spec", {"max_team_workers": 2}, token=tok)
        assert status == 200 and data["revision"] == 2
        status, data = self._post(base_url + "/api/task", {"task": "t"}, token=tok)
        assert status == 200 and data["ok"]
        status, data = self._post(base_url + "/api/directive",
                                  {"kind": "terminal", "payload": {"scope": "root"}}, token=tok)
        assert status == 200 and data["ok"]

    def test_unknown_route_404(self, base_url):
        status, _, _ = self._get(base_url + "/api/nothing", token=Handler.state.token)
        assert status == 404
