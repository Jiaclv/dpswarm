"""产物（artifact）状态板（fixed-team-v3，2026-09-09，测试先行）。

产物 = 带状态与版本的具名交付物，供"分阶段多 worker 协调"用：
- 登记校验：缺字段 / 重复 id / 未知 deps / 重复 deps / 自环 CYCLE /
  未知 owner_item / write_globs 非空字符串数组 / state 必须为 pending。
- 状态机全转换表正反：合法 8 条通过（version 由投影 +1），其余 41 对
  全部 ARTIFACT_TRANSITION 结构化拒绝且不产生半截态。
- 事件溯源：事件写盘后新建 ControlPlane 重放，投影与落盘前一致
  （回放与落盘同路径）。
- /api/status 透出 artifacts 列表（id/title/state/version/phase/
  owner_item/write_globs/deps）。
- HTTP 层：写接口 bearer 认证拒绝、Origin 只认 loopback、
  X-DPSwarm-Session 会话隔离。

风格对齐 test_p2_recovery_fixes_20260909.py（控制面直连）与
test_session_server_20260907.py（session server HTTP 层）。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from dpswarm import invariants, state as state_module
from dpswarm.control import ControlPlane, ControlPlaneError
from dpswarm.events import Event
from dpswarm.server import PanelState
from dpswarm.session_server import create_server
from dpswarm.types import Level, ModelCatalog, ModelFacts, RootExecutionSpec


def make_catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


@pytest.fixture()
def cp(tmp_path):
    return ControlPlane(spec=RootExecutionSpec(max_open_work_items=2,
                                               max_active_node_points=6,
                                               max_attempts=3),
                        store_path=tmp_path / "events.jsonl",
                        catalog=make_catalog())


def register(control, artifact_id="art-1", title="接口契约", globs=None,
             phase="实现", owner=None, deps=None, state=None, version=None):
    return control.register_artifact(
        artifact_id, title,
        ["src/api/**"] if globs is None else globs,
        phase, owner_item=owner, deps=deps, state=state, version=version)


def replay_checked(events):
    """全量回放过 check_event（invariant 层与事件流一致性）。"""
    proj = state_module.Projection()
    for ev in events:
        proj = invariants.check_event(proj, ev)
    return proj


ARTIFACT_STATES = ["pending", "claimed", "draft", "ready", "adjusting", "frozen", "done"]
LEGAL_ARTIFACT_TRANSITIONS = {
    ("pending", "claimed"), ("claimed", "draft"), ("draft", "ready"),
    ("ready", "adjusting"), ("ready", "frozen"), ("ready", "done"),
    ("adjusting", "ready"), ("frozen", "done"),
}
#: 把产物推进到某状态的最短合法路径（登记后从 pending 出发）。
DRIVE_PATHS = {
    "pending": (),
    "claimed": ("claimed",),
    "draft": ("claimed", "draft"),
    "ready": ("claimed", "draft", "ready"),
    "adjusting": ("claimed", "draft", "ready", "adjusting"),
    "frozen": ("claimed", "draft", "ready", "frozen"),
    "done": ("claimed", "draft", "ready", "frozen", "done"),
}


def drive_to(control, artifact_id, target):
    for step in DRIVE_PATHS[target]:
        control.set_artifact_state(artifact_id, step)


# ---------------------------------------------------------------------------
# 登记校验
# ---------------------------------------------------------------------------


class TestRegisterValidation:
    def test_register_defaults_to_pending_version_one(self, cp):
        root_item = cp._root_item_id()
        art = register(cp, "art-1", owner=root_item)
        assert art.state.value == "pending"
        assert art.version == 1
        assert art.title == "接口契约"
        assert art.write_globs == ["src/api/**"]
        assert art.phase == "实现"
        assert art.owner_item == root_item
        assert art.deps == []
        assert cp.proj.artifacts["art-1"] is art

    @pytest.mark.parametrize("missing", ["id", "title", "write_globs", "phase"])
    def test_register_missing_required_field(self, cp, missing):
        body = {"id": "art-x", "title": "契约", "write_globs": ["a/**"], "phase": "实现"}
        body.pop(missing)
        with pytest.raises(ControlPlaneError) as ei:
            cp.register_artifact(body.get("id"), body.get("title"),
                                 body.get("write_globs"), body.get("phase"))
        assert ei.value.code == "BAD_PAYLOAD"
        assert missing in str(ei.value)
        assert cp.proj.artifacts == {}

    @pytest.mark.parametrize("globs", [[], ["ok", ""], ["ok", 3], "src/**", None])
    def test_register_write_globs_must_be_nonempty_string_array(self, cp, globs):
        with pytest.raises(ControlPlaneError) as ei:
            cp.register_artifact("art-g", "契约", globs, "实现")
        assert ei.value.code == "BAD_PAYLOAD"
        assert "write_globs" in str(ei.value)

    def test_register_duplicate_id_rejected(self, cp):
        register(cp, "art-1")
        with pytest.raises(ControlPlaneError) as ei:
            register(cp, "art-1", title="重名产物")
        assert ei.value.code == "ARTIFACT_EXISTS"
        assert cp.proj.artifacts["art-1"].title == "接口契约"

    def test_register_unknown_dep_rejected(self, cp):
        with pytest.raises(ControlPlaneError) as ei:
            register(cp, "art-1", deps=["art-ghost"])
        assert ei.value.code == "DEP_MISSING"
        assert cp.proj.artifacts == {}

    def test_register_duplicate_dep_rejected(self, cp):
        register(cp, "art-a")
        with pytest.raises(ControlPlaneError) as ei:
            register(cp, "art-b", deps=["art-a", "art-a"])
        assert ei.value.code == "DUPLICATE_EDGE"

    def test_register_self_dep_rejected_as_cycle(self, cp):
        with pytest.raises(ControlPlaneError) as ei:
            register(cp, "art-1", deps=["art-1"])
        assert ei.value.code == "CYCLE"
        assert cp.proj.artifacts == {}

    def test_register_unknown_owner_rejected(self, cp):
        with pytest.raises(ControlPlaneError) as ei:
            register(cp, "art-1", owner="wi-nope")
        assert ei.value.code == "ITEM_UNKNOWN"
        assert cp.proj.artifacts == {}

    def test_register_rejects_non_pending_state(self, cp):
        payload = {"id": "art-s", "title": "契约", "write_globs": ["a/**"],
                   "phase": "实现", "owner_item": None, "deps": [], "state": "ready"}
        event = Event(seq=cp.store.last_seq + 1,
                      kind="artifact_registered", payload=payload)
        with pytest.raises(invariants.InvariantViolation) as ei:
            invariants.check_event(cp.proj, event)
        assert ei.value.code == "BAD_PAYLOAD"

    def test_register_rejects_non_initial_version(self, cp):
        payload = {"id": "art-s", "title": "契约", "write_globs": ["a/**"],
                   "phase": "实现", "owner_item": None, "deps": [], "version": 3}
        event = Event(seq=cp.store.last_seq + 1,
                      kind="artifact_registered", payload=payload)
        with pytest.raises(invariants.InvariantViolation) as ei:
            invariants.check_event(cp.proj, event)
        assert ei.value.code == "BAD_PAYLOAD"

    def test_register_chained_deps_stay_acyclic(self, cp):
        register(cp, "art-a")
        register(cp, "art-b", deps=["art-a"])
        register(cp, "art-c", deps=["art-b", "art-a"])
        assert sorted(cp.proj.artifacts) == ["art-a", "art-b", "art-c"]
        assert cp.proj.artifacts["art-c"].deps == ["art-b", "art-a"]


# ---------------------------------------------------------------------------
# 状态机全转换表正反
# ---------------------------------------------------------------------------


class TestArtifactTransitions:
    @pytest.mark.parametrize("frm,to", sorted(LEGAL_ARTIFACT_TRANSITIONS))
    def test_legal_transition_advances_state_and_version(self, cp, frm, to):
        register(cp, "art-1")
        drive_to(cp, "art-1", frm)
        before = cp.proj.artifacts["art-1"].version
        assert cp.proj.artifacts["art-1"].state.value == frm
        art = cp.set_artifact_state("art-1", to, note=f"{frm}->{to}")
        assert art.state.value == to
        assert art.version == before + 1

    def test_illegal_transitions_rejected_without_mutation(self, cp):
        snapshots = {}
        for frm in ARTIFACT_STATES:
            register(cp, f"art-{frm}")
            drive_to(cp, f"art-{frm}", frm)
            snapshots[frm] = cp.proj.artifacts[f"art-{frm}"].version
        illegal = [(f, t) for f in ARTIFACT_STATES for t in ARTIFACT_STATES
                   if (f, t) not in LEGAL_ARTIFACT_TRANSITIONS]
        assert len(illegal) == len(ARTIFACT_STATES) ** 2 - len(LEGAL_ARTIFACT_TRANSITIONS)
        for frm, to in illegal:
            with pytest.raises(ControlPlaneError) as ei:
                cp.set_artifact_state(f"art-{frm}", to)
            assert ei.value.code == "ARTIFACT_TRANSITION", (frm, to)
            assert cp.proj.artifacts[f"art-{frm}"].state.value == frm, (frm, to)
            assert cp.proj.artifacts[f"art-{frm}"].version == snapshots[frm]

    def test_state_change_unknown_artifact_rejected(self, cp):
        with pytest.raises(ControlPlaneError) as ei:
            cp.set_artifact_state("art-ghost", "claimed")
        assert ei.value.code == "ARTIFACT_UNKNOWN"

    @pytest.mark.parametrize("to", ["hoge", "", None, 3])
    def test_state_change_invalid_target_rejected(self, cp, to):
        register(cp, "art-1")
        with pytest.raises(ControlPlaneError) as ei:
            cp.set_artifact_state("art-1", to)
        assert ei.value.code == "BAD_PAYLOAD"
        assert cp.proj.artifacts["art-1"].state.value == "pending"

    def test_full_chain_with_deps_and_notes(self, cp):
        root_item = cp._root_item_id()
        register(cp, "art-spec", title="规格产物", phase="设计", owner=root_item)
        register(cp, "art-impl", title="实现产物", phase="实现", owner=root_item,
                 deps=["art-spec"])
        chain = ["claimed", "draft", "ready", "adjusting", "ready", "frozen", "done"]
        for i, step in enumerate(chain, start=1):
            art = cp.set_artifact_state("art-impl", step, note=f"step {i}")
            assert art.state.value == step
            assert art.version == 1 + i
        assert cp.proj.artifacts["art-spec"].version == 1  # 未变更不加版本
        assert [e.kind for e in cp.store.read_all()
                if e.kind.startswith("artifact_")] == (
            ["artifact_registered", "artifact_registered"]
            + ["artifact_state_changed"] * len(chain))
        notes = [e.payload.get("note") for e in cp.store.read_all()
                 if e.kind == "artifact_state_changed"]
        assert notes == [f"step {i}" for i in range(1, len(chain) + 1)]


# ---------------------------------------------------------------------------
# 回放一致（事件写盘后新建 ControlPlane 重放投影一致）
# ---------------------------------------------------------------------------


class TestReplayConsistency:
    def test_replay_after_restart_matches_live_projection(self, tmp_path):
        path = tmp_path / "events.jsonl"
        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=2,
                                                 max_active_node_points=6),
                          store_path=path, catalog=make_catalog())
        root_item = cp._root_item_id()
        cp.register_artifact("art-a", "契约定义", ["docs/contract.md"], "阶段一",
                             owner_item=root_item)
        cp.register_artifact("art-b", "实现交付", ["src/**"], "阶段二",
                             owner_item=root_item, deps=["art-a"])
        for step in ("claimed", "draft", "ready"):
            cp.set_artifact_state("art-b", step)
        live = cp.proj.artifacts
        cp.close()

        reopened = ControlPlane(store_path=path, catalog=make_catalog())
        try:
            assert reopened.proj.artifacts == live
            assert reopened.proj.artifacts["art-b"].state.value == "ready"
            assert reopened.proj.artifacts["art-b"].version == 4
            # 回放与落盘同路径：事件流逐条过 check_event 也得到同一投影
            assert replay_checked(reopened.store.read_all()).artifacts == live
        finally:
            reopened.close()

    def test_replay_events_are_transaction_enveloped(self, tmp_path):
        path = tmp_path / "events.jsonl"
        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=2,
                                                 max_active_node_points=6),
                          store_path=path, catalog=make_catalog())
        cp.register_artifact("art-a", "契约定义", ["docs/*.md"], "阶段一")
        cp.set_artifact_state("art-a", "claimed")
        cp.close()
        lines = [json.loads(line) for line in
                 path.read_text(encoding="utf-8").splitlines() if line.strip()]
        artifact_rows = [row for row in lines
                         if any(e["kind"].startswith("artifact_")
                                for e in row["events"])]
        assert len(artifact_rows) == 2  # 登记与状态变更各一行 envelope
        kinds = [e["kind"] for row in artifact_rows for e in row["events"]]
        assert kinds == ["artifact_registered", "artifact_state_changed"]


# ---------------------------------------------------------------------------
# /api/status 透出
# ---------------------------------------------------------------------------


class TestStatusExposure:
    def test_status_exposes_artifact_fields(self, tmp_path):
        panel = PanelState(tmp_path / "ws")
        try:
            root_item = panel.cp._root_item_id()
            panel.cp.register_artifact("art-s", "状态板交付", ["reports/*.md"],
                                       "阶段九", owner_item=root_item,
                                       deps=None)
            panel.cp.set_artifact_state("art-s", "claimed")
            artifacts = panel.status()["artifacts"]
            assert artifacts == [{
                "id": "art-s", "title": "状态板交付", "state": "claimed",
                "version": 2, "phase": "阶段九", "owner_item": root_item,
                "write_globs": ["reports/*.md"], "deps": [],
            }]
        finally:
            panel.cp.close()

    def test_status_artifacts_empty_by_default(self, tmp_path):
        panel = PanelState(tmp_path / "ws")
        try:
            assert panel.status()["artifacts"] == []
        finally:
            panel.cp.close()


# ---------------------------------------------------------------------------
# HTTP 层：认证拒绝 / Origin / 会话隔离（session server）
# ---------------------------------------------------------------------------


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
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}{path}", data=data, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def bind(server, session):
    return call(server, "/api/execution/root", {"parent_session_id": session,
                "delegation_depth": 0, "provider": "mock", "model": "b-kimi"},
                session)


def artifact_body(artifact_id="art-api", **overrides):
    body = {"id": artifact_id, "title": "接口契约", "write_globs": ["src/api/**"],
            "phase": "实现", "owner_item": None, "deps": []}
    body.update(overrides)
    return body


class TestHTTPLayer:
    def test_write_requires_bearer_token(self, server):
        assert call(server, "/api/artifact/register", artifact_body(),
                    "sess", authorized=False)[0] == 401
        assert call(server, "/api/artifact/state",
                    {"artifact_id": "art-api", "to": "claimed"},
                    "sess", authorized=False)[0] == 401

    def test_write_rejects_foreign_origin(self, server):
        assert call(server, "/api/artifact/register", artifact_body(),
                    "sess", origin="https://example.com")[0] == 403
        assert call(server, "/api/artifact/state",
                    {"artifact_id": "art-api", "to": "claimed"},
                    origin="https://example.com")[0] == 403

    def test_write_requires_session_header(self, server):
        assert call(server, "/api/artifact/register",
                    artifact_body())[1]["error"] == "SESSION_REQUIRED"
        assert call(server, "/api/artifact/state",
                    {"artifact_id": "art-api", "to": "claimed"})[1]["error"] \
            == "SESSION_REQUIRED"

    def test_register_and_state_roundtrip_over_http(self, server):
        assert bind(server, "sess-art")[0] == 200
        code, r = call(server, "/api/artifact/register", artifact_body(), "sess-art")
        assert code == 200 and r["ok"], r
        assert r["artifact"]["state"] == "pending"
        assert r["artifact"]["version"] == 1

        code, r = call(server, "/api/artifact/state",
                       {"artifact_id": "art-api", "to": "claimed"}, "sess-art")
        assert code == 200 and r["ok"], r
        assert r["artifact"]["state"] == "claimed"
        assert r["artifact"]["version"] == 2

        # /api/status 透出（只读免 bearer）
        code, st = call(server, "/api/status", session="sess-art", authorized=False)
        assert code == 200
        assert st["artifacts"] == [{
            "id": "art-api", "title": "接口契约", "state": "claimed",
            "version": 2, "phase": "实现", "owner_item": None,
            "write_globs": ["src/api/**"], "deps": [],
        }]

        # 非法转换在 HTTP 层同样结构化拒绝
        code, r = call(server, "/api/artifact/state",
                       {"artifact_id": "art-api", "to": "frozen"}, "sess-art")
        assert code == 400 and r["error"] == "ARTIFACT_TRANSITION"

    def test_register_rejects_non_pending_state_over_http(self, server):
        """请求体声称 state=ready 不得被静默改成 pending：结构化拒绝。"""
        assert bind(server, "sess-art")[0] == 200
        code, r = call(server, "/api/artifact/register",
                       artifact_body(state="ready"), "sess-art")
        assert code == 400 and r["error"] == "BAD_PAYLOAD"

    def test_register_unknown_owner_over_http(self, server):
        assert bind(server, "sess-art")[0] == 200
        code, r = call(server, "/api/artifact/register",
                       artifact_body(owner_item="wi-nope"), "sess-art")
        assert code == 400 and r["error"] == "ITEM_UNKNOWN"

    def test_session_isolation(self, server):
        assert bind(server, "sess-a")[0] == 200
        assert bind(server, "sess-b")[0] == 200
        code, r = call(server, "/api/artifact/register", artifact_body(), "sess-a")
        assert code == 200 and r["ok"]
        code, r = call(server, "/api/artifact/state",
                       {"artifact_id": "art-api", "to": "claimed"}, "sess-a")
        assert code == 200

        # B 会话看不到 A 的产物，也不能改它的状态
        code, st = call(server, "/api/status", session="sess-b")
        assert code == 200 and st["artifacts"] == []
        code, r = call(server, "/api/artifact/state",
                       {"artifact_id": "art-api", "to": "claimed"}, "sess-b")
        assert code == 400 and r["error"] == "ARTIFACT_UNKNOWN"

        # A 会话状态不受 B 的失败影响
        code, st = call(server, "/api/status", session="sess-a")
        assert st["artifacts"][0]["state"] == "claimed"
