"""P0 安全层 + §2.1 Spec 合法域测试（审查修复回归锚点）。

- 鉴权：写操作/敏感读必须带 workspace/.dpswarm-token 的 bearer capability
- Origin：只认 loopback（任意端口）；恶意网页 Origin 403，预检同样拒绝
- 请求体上限 5MB；面板 HTML 注入 token
- Spec：越界值发布即拒（fail-fast），含 deadline > wall-clock 交叉校验
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest

from dpswarm.control import ControlPlane, ControlPlaneError
from dpswarm.server import MAX_BODY_BYTES, Handler, PanelState
from dpswarm.types import RootExecutionSpec


@pytest.fixture()
def base_url(tmp_path):
    Handler.state = PanelState(tmp_path / "ws")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def _req(method, url, headers=None, body=None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class TestAuth:
    def test_post_without_token_401(self, base_url):
        status, body, _ = _req("POST", base_url + "/api/spec",
                               {"Content-Type": "application/json"},
                               json.dumps({"max_team_workers": 2}).encode())
        assert status == 401

    def test_post_wrong_token_401(self, base_url):
        status, _, _ = _req("POST", base_url + "/api/spec",
                            {"Content-Type": "application/json",
                             "Authorization": "Bearer wrong-token"},
                            json.dumps({"max_team_workers": 2}).encode())
        assert status == 401

    def test_post_valid_token_200(self, base_url):
        status, body, _ = _req("POST", base_url + "/api/spec",
                               {"Content-Type": "application/json",
                                "Authorization": f"Bearer {Handler.state.token}"},
                               json.dumps({"max_team_workers": 2}).encode())
        assert status == 200 and json.loads(body)["revision"] == 2

    def test_get_events_requires_token(self, base_url):
        assert _req("GET", base_url + "/api/events")[0] == 401
        assert _req("GET", base_url + "/api/events",
                    {"Authorization": f"Bearer {Handler.state.token}"})[0] == 200

    def test_token_file_persisted(self, tmp_path):
        st = PanelState(tmp_path / "ws")
        st.cp.close()  # 单写者文件锁：重建前释放
        # 重启（同 workspace）读到同一 token：Host 半约定路径读取的前提
        assert PanelState(tmp_path / "ws").token == st.token


class TestOriginGate:
    def test_evil_origin_status_403(self, base_url):
        status, _, _ = _req("GET", base_url + "/api/status",
                            {"Origin": "https://evil.example"})
        assert status == 403

    def test_evil_origin_preflight_403(self, base_url):
        status, _, _ = _req("OPTIONS", base_url + "/api/spec",
                            {"Origin": "https://evil.example",
                             "Access-Control-Request-Method": "POST"})
        assert status == 403

    def test_loopback_preflight_echoes_origin(self, base_url):
        status, _, headers = _req("OPTIONS", base_url + "/api/spec",
                                  {"Origin": "http://127.0.0.1:3999",
                                   "Access-Control-Request-Method": "POST"})
        assert status == 204
        assert headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:3999"
        assert "Authorization" in headers.get("Access-Control-Allow-Headers", "")

    def test_evil_origin_post_403_even_with_token(self, base_url):
        # token 泄漏场景的第二道门：非 loopback Origin 一律拒
        status, _, _ = _req("POST", base_url + "/api/spec",
                            {"Content-Type": "application/json",
                             "Origin": "https://evil.example",
                             "Authorization": f"Bearer {Handler.state.token}"},
                            json.dumps({"max_team_workers": 2}).encode())
        assert status == 403

    def test_no_acao_header_for_cross_site(self, base_url):
        # 非 loopback 即便走到响应（如 403），也没有可读的 CORS 头
        _, _, headers = _req("GET", base_url + "/api/status",
                             {"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in headers


class TestBodyLimit:
    def test_oversized_body_413(self, base_url):
        conn = http.client.HTTPConnection("127.0.0.1", int(base_url.rsplit(":", 1)[1]),
                                          timeout=5)
        conn.putrequest("POST", "/api/spec")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Authorization", f"Bearer {Handler.state.token}")
        conn.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        conn.endheaders()
        conn.send(b"{}")  # 服务端按头先判，无需真发 5MB
        resp = conn.getresponse()
        assert resp.status == 413
        conn.close()


class TestPanelTokenInjection:
    def test_index_html_has_real_token(self, base_url):
        _, body, _ = _req("GET", base_url + "/")
        assert b"{{DPSWARM_TOKEN}}" not in body
        assert Handler.state.token.encode() in body

    def test_cross_port_origin_gets_tokenless_page(self, base_url):
        """P2 收口：带 Origin 且与 Host 不同源（端口不同即不同源）时返回不含
        token 的 HTML——loopback 任意端口页面过 Origin 门且有 ACAO 回显，
        但读到的页面不含 token 本体。"""
        port = int(base_url.rsplit(":", 1)[1])
        status, body, _ = _req("GET", base_url + "/",
                               {"Origin": f"http://127.0.0.1:{port + 1}"})
        assert status == 200
        assert Handler.state.token.encode() not in body
        assert b"{{DPSWARM_TOKEN}}" not in body

    def test_same_origin_origin_keeps_token(self, base_url):
        """Origin 与 Host 完全同源（含端口）= 面板自身页面的 fetch，保持注入。"""
        port = int(base_url.rsplit(":", 1)[1])
        status, body, _ = _req("GET", base_url + "/",
                               {"Origin": f"http://127.0.0.1:{port}"})
        assert status == 200
        assert Handler.state.token.encode() in body


class TestMalformedInput:
    """坏请求体返回结构化 400，不炸连接、不泄 traceback（漏网 500）。"""

    def _post(self, base_url, path, body):
        req = urllib.request.Request(
            base_url + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {Handler.state.token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_bad_model_string_400(self, base_url):
        status, body = self._post(base_url, "/api/task", {"model": "no-slash"})
        assert status == 400 and body["error"] == "BAD_REQUEST"

    def test_bad_level_400(self, base_url):
        status, body = self._post(base_url, "/api/delegate", {
            "kind": "derive",
            "subtasks": [{"provider": "mock", "model": "b-kimi", "level": "X"}]})
        assert status == 400 and body["error"] == "BAD_SUBTASK"
        assert body["errors"]["level"] == "unknown or server-owned field"

    def test_bad_float_400_and_server_alive(self, base_url):
        status, body = self._post(base_url, "/api/delegate", {
            "kind": "derive",
            "subtasks": [{"provider": "mock", "model": "b-kimi",
                          "aa_coding": "not-a-float"}]})
        assert status == 400 and body["error"] == "BAD_SUBTASK"
        assert body["errors"]["aa_coding"] == "unknown or server-owned field"
        # 服务存活：坏请求之后正常读请求照常（此前连接直接被重置）
        status, _, _ = _req("GET", base_url + "/api/status")
        assert status == 200


class TestSpecValidation:
    """§2.1 合法域：发布时 fail-fast（修复"非法 Spec 可发布"实测缺陷）。"""

    @pytest.fixture()
    def cp(self, tmp_path):
        return ControlPlane(store_path=tmp_path / "ev.jsonl")

    def _publish(self, cp, **kw):
        return cp.publish_spec(replace(cp.proj.spec, **kw))

    def _assert_invalid(self, cp, **kw):
        with pytest.raises(ControlPlaneError) as ei:
            self._publish(cp, **kw)
        assert ei.value.code == "SPEC_INVALID"

    def test_zero_attempts_rejected(self, cp):
        self._assert_invalid(cp, max_attempts=0)

    def test_negative_capacity_rejected(self, cp):
        self._assert_invalid(cp, max_active_node_points=-5)
        self._assert_invalid(cp, max_open_work_items=-2)

    def test_ratio_above_one_rejected(self, cp):
        self._assert_invalid(cp, subteam_point_ratio=2.0)

    def test_ratio_one_rejected(self, cp):
        # §7：子 Team 本地上限必须**小于**父级（=1 会让配额沿裂变链不衰减）
        self._assert_invalid(cp, subteam_point_ratio=1.0)

    def test_deadline_not_after_wallclock_rejected(self, cp):
        # 两道时间闸的先后是 §2.1/§7 明文约束
        self._assert_invalid(cp, deadline_seconds=100.0, node_wallclock_timeout=200.0)

    def test_valid_publish_still_works(self, cp):
        ev = self._publish(cp, max_team_workers=2)
        assert ev.kind == "spec_published"
        assert cp.proj.spec.revision == 2 and cp.proj.spec.max_team_workers == 2

    def test_over_http_returns_400(self, base_url):
        status, body, _ = _req("POST", base_url + "/api/spec",
                               {"Content-Type": "application/json",
                                "Authorization": f"Bearer {Handler.state.token}"},
                               json.dumps({"max_attempts": 0}).encode())
        assert status == 400
        assert json.loads(body)["error"] == "SPEC_INVALID"
