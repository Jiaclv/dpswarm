"""server.py dsh 集成层修复回归。

覆盖（机制文档章节对位）：
- §8 归因重试链补全：review reject 只打回挂起（不再 prepare_retry 提前消费
  预算），重跑走 delegate item_id 模式（预算在重跑时消费；耗尽自动上交）；
- §4/§7 terminate verdict：任一非终态 item 可放弃，槽位与 lease 归还；
- §7 时间护栏②：wall-clock 超时（tick → blocked）后的归因重试执行臂；
- §2/§7 裂变规模：subtasks 超限结构化拒绝（不静默截断）；
- §7 DAG：subtask deps 0 基下标——未就绪 item 不启动、pending 回报 waiting_on；
- §9.5 分裂对 peer 通道：/api/peer queued+delivered 往返（消息账本即 evidence）。

PanelState 直接调用风格（同 test_server.py），控制面事务全走真链。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from dpswarm.control import ControlPlaneError
from dpswarm.server import Handler, PanelState
from dpswarm.types import AcceptanceState, BlockState, Level, LifecycleState, ModelFacts


@pytest.fixture()
def state(tmp_path) -> PanelState:
    return PanelState(tmp_path / "ws")


def _derive(state: PanelState, **st):
    """新建单条 derive item 并两阶段启动，返回 items[0]。"""
    ok, resp = state.delegate({"kind": "derive", "subtasks": [st]})
    assert ok, resp
    assert resp["pending"] == []
    return resp["items"][0]


def _fence(it: dict) -> dict:
    """P1-2 fence：delegate 响应携带的 (context_epoch, session_id) submit 原样回传。"""
    return {"context_epoch": it["context_epoch"], "session_id": it["session_id"]}


def _event_kinds(state: PanelState):
    return [e.kind for e in state.cp.store.read_all()]


# ---------------------------------------------------------------------------
# §8 归因重试链：reject → 重跑 → accept 全链
# ---------------------------------------------------------------------------

class TestRejectRerunChain:
    def test_reject_then_rerun_then_accept(self, state):
        it = _derive(state, provider="mock", model="b-kimi", title="解析", prompt="写解析")
        item_id, old_node = it["item_id"], it["node_id"]
        assert state.cp.proj.work_items[item_id].attempt == 1

        ok, _ = state.submit_output({"item_id": item_id, "node_id": old_node,
                                     "output": "第一版", **_fence(it)})
        assert ok

        ok, r = state.review({"item_id": item_id, "verdict": "reject",
                              "attribution": "capability", "reason": "编码能力不足",
                              "route": {"provider": "mock", "model": "a-glm"}})
        assert ok and r["outcome"] == "rejected-awaiting-rerun"
        assert r["proposed_route"] == {"provider": "mock", "model": "a-glm"}
        # reject 只打回挂起：不产生 work_item_retried（预算未被提前消费）
        assert "work_item_retried" not in _event_kinds(state)
        item = state.cp.proj.work_items[item_id]
        assert item.acceptance == AcceptanceState.REJECTED and item.attempt == 1
        old_lease = state.cp.proj.nodes[old_node].lease_id

        # 重跑（item_id 模式 + capability 换模）
        ok, r = state.delegate({"item_id": item_id,
                                "subtask": {"provider": "mock", "model": "a-glm",
                                            "title": "解析", "prompt": "写解析 v2"}})
        assert ok, r
        assert r["items"][0]["kind"] == "re-run" and r["items"][0]["level"] == "A"
        new_node = r["items"][0]["node_id"]
        assert new_node != old_node
        item = state.cp.proj.work_items[item_id]
        assert item.attempt == 2 and item.acceptance is None
        # 旧节点已 drain、旧 lease 已释放（§7 归还占点）
        assert state.cp.proj.nodes[old_node].terminated
        assert state.cp.proj.leases[old_lease].active is False

        rerun_it = r["items"][0]
        ok, _ = state.submit_output({"item_id": item_id, "node_id": new_node,
                                     "output": "第二版", **_fence(rerun_it)})
        assert ok
        ok, r = state.review({"item_id": item_id, "verdict": "accept", "output": "第二版"})
        assert ok and r["outcome"] == "accepted"
        assert state.cp.proj.work_items[item_id].acceptance == AcceptanceState.ACCEPTED

    def test_rerun_budget_exhausted_escalates(self, state):
        ok, _ = state.publish_spec({"max_attempts": 2})
        assert ok
        it = _derive(state, provider="mock", model="b-kimi", title="t")
        item_id = it["item_id"]
        node = it["node_id"]
        for _round in (1, 2):
            ok, _ = state.submit_output({"item_id": item_id, "node_id": node,
                                         "output": f"v{_round}", **_fence(it)})
            assert ok
            ok, r = state.review({"item_id": item_id, "verdict": "reject",
                                  "attribution": "context", "reason": "缺料"})
            assert ok and r["outcome"] == "rejected-awaiting-rerun"
            ok, r = state.delegate({"item_id": item_id,
                                    "subtask": {"provider": "mock", "model": "b-kimi"}})
            if _round == 1:
                assert ok and r["items"], r
                node = r["items"][0]["node_id"]
                it = r["items"][0]  # fence 语境跟随新节点（P1-2）
            else:
                # 第 3 次重跑：预算耗尽 → 控制面自动上交（§8）
                assert ok and r["outcome"] == "escalated", r
        item = state.cp.proj.work_items[item_id]
        assert item.acceptance == AcceptanceState.ESCALATED
        assert item.holds_worker_slot is False            # 槽位归还
        assert all(not l.active for l in state.cp.proj.leases.values()
                   if state.cp.proj.nodes[l.node_id].item_id == item_id)
        # 终态后不可再重跑
        ok, r = state.delegate({"item_id": item_id,
                                "subtask": {"provider": "mock", "model": "b-kimi"}})
        assert not ok and r["error"] == "ITEM_TERMINAL"

    def test_rerun_guards(self, state):
        # 未知 item
        ok, r = state.delegate({"item_id": "wi-nope",
                                "subtask": {"provider": "mock", "model": "b-kimi"}})
        assert not ok and r["error"] == "ITEM_UNKNOWN"
        # 在途（ACTIVE 节点、未提交）→ ITEM_ALREADY_RUNNING
        it = _derive(state, provider="mock", model="b-kimi", title="t")
        ok, r = state.delegate({"item_id": it["item_id"],
                                "subtask": {"provider": "mock", "model": "b-kimi"}})
        assert not ok and r["error"] == "ITEM_ALREADY_RUNNING"
        # 已提交待审 → ITEM_UNDER_REVIEW
        state.submit_output({"item_id": it["item_id"], "node_id": it["node_id"],
                             "output": "v", **_fence(it)})
        ok, r = state.delegate({"item_id": it["item_id"],
                                "subtask": {"provider": "mock", "model": "b-kimi"}})
        assert not ok and r["error"] == "ITEM_UNDER_REVIEW"
        # accepted 终态 → ITEM_TERMINAL
        state.review({"item_id": it["item_id"], "verdict": "accept", "output": "v"})
        ok, r = state.delegate({"item_id": it["item_id"],
                                "subtask": {"provider": "mock", "model": "b-kimi"}})
        assert not ok and r["error"] == "ITEM_TERMINAL"

    def test_rerun_points_shortfall_marks_reweight_wait(self, state):
        """§9.3 reweight-wait 抑制窗口：重跑换更重模型、点数差额不足 →
        事务拒绝 + 在途节点标记等待（tick 超时豁免），不消耗重试预算；
        容量归还后重跑成功，lease_reweight 同事务清除等待标记。"""
        state.cp.catalog.register(
            ModelFacts("mock", "x-heavy", Level.A, context_window=512_000))  # weight 8
        it = _derive(state, provider="mock", model="b-kimi", title="t", prompt="p")
        item_id = it["item_id"]
        ok, _ = state.submit_output({"item_id": item_id, "node_id": it["node_id"],
                                     "output": "v", **_fence(it)})
        assert ok
        ok, r = state.review({"item_id": item_id, "verdict": "reject",
                              "attribution": "capability", "reason": "能力弱"})
        assert ok and r["outcome"] == "rejected-awaiting-rerun"
        # 默认上限 8：root(1) + reweight 2→8 = 9 > 8 → 差额不足
        ok, r = state.delegate({"item_id": item_id, "subtask": {
            "provider": "mock", "model": "x-heavy", "title": "t"}})
        assert not ok and r["error"] == "POINTS_EXCEEDED" and r["reweight_wait"]
        node = state.cp.proj.nodes[it["node_id"]]
        assert node.reweight_wait                              # 进入抑制窗口
        assert state.cp.proj.work_items[item_id].attempt == 1  # 等待不耗预算
        # 容量归还（新 revision 放宽上限）后重跑成功：同事务清除等待标记
        ok, _ = state.publish_spec({"max_active_node_points": 32})
        assert ok
        ok, r = state.delegate({"item_id": item_id, "subtask": {
            "provider": "mock", "model": "x-heavy", "title": "t"}})
        assert ok and r["items"][0]["kind"] == "re-run"
        assert node.reweight_wait is False
        assert state.cp.proj.work_items[item_id].attempt == 2
        waits = [e for e in state.cp.store.read_all()
                 if e.kind == "node_reweight_wait"]
        assert [w.payload["waiting"] for w in waits] == [True, False]


# ---------------------------------------------------------------------------
# §4/§7 terminate verdict
# ---------------------------------------------------------------------------

class TestTerminateVerdict:
    def test_terminate_from_submitted_releases_resources(self, state):
        it = _derive(state, provider="mock", model="b-kimi", title="t")
        item_id, node_id = it["item_id"], it["node_id"]
        lease_id = state.cp.proj.nodes[node_id].lease_id
        assert state.cp.proj.open_worker_slots_used == 1
        state.submit_output({"item_id": item_id, "node_id": node_id, "output": "v",
                             **_fence(it)})

        ok, r = state.review({"item_id": item_id, "verdict": "terminate",
                              "reason": "manual-stopped"})
        assert ok and r["outcome"] == "terminated"
        item = state.cp.proj.work_items[item_id]
        assert item.acceptance == AcceptanceState.TERMINATED
        assert item.holds_worker_slot is False                 # 槽位归还
        assert state.cp.proj.open_worker_slots_used == 0
        assert state.cp.proj.nodes[node_id].terminated          # 节点 drain
        assert state.cp.proj.leases[lease_id].active is False   # lease 归还

    def test_terminate_from_rejected_and_none(self, state):
        # REJECTED 状态可 terminate（放弃打回项）
        it = _derive(state, provider="mock", model="b-kimi", title="t")
        state.submit_output({"item_id": it["item_id"], "node_id": it["node_id"],
                             "output": "v", **_fence(it)})
        state.review({"item_id": it["item_id"], "verdict": "reject",
                      "attribution": "description"})
        ok, r = state.review({"item_id": it["item_id"], "verdict": "terminate"})
        assert ok and r["outcome"] == "terminated"
        # None 状态（执行中、未提交）也可 terminate
        it2 = _derive(state, provider="mock", model="b-kimi", title="t2")
        ok, r = state.review({"item_id": it2["item_id"], "verdict": "terminate"})
        assert ok and r["outcome"] == "terminated"
        # 非法 verdict 结构化拒绝
        ok, r = state.review({"item_id": it2["item_id"], "verdict": "maybe"})
        assert not ok and "terminate" in r["error"]


# ---------------------------------------------------------------------------
# §7 时间护栏②：wall-clock 超时后的重跑执行臂
# ---------------------------------------------------------------------------

class TestWallclockTimeoutRerun:
    def test_timeout_blocked_then_rerun(self, state):
        ok, _ = state.publish_spec({"node_wallclock_timeout": 0.2})
        assert ok
        it = _derive(state, provider="mock", model="b-kimi", title="慢任务")
        item_id, node_id = it["item_id"], it["node_id"]
        lease_id = state.cp.proj.nodes[node_id].lease_id

        import time
        time.sleep(0.3)
        actions = state.cp.tick()
        assert any(a.get("action") == "node-blocked" and a.get("node_id") == node_id
                   for a in actions)
        node = state.cp.proj.nodes[node_id]
        assert node.blocked == BlockState.BLOCKED
        assert node.blocked_reason == "wallclock-timeout"

        ok, r = state.delegate({"item_id": item_id,
                                "subtask": {"provider": "mock", "model": "b-kimi"}})
        assert ok, r
        assert r["items"][0]["kind"] == "re-run"
        new_node = r["items"][0]["node_id"]
        assert new_node != node_id
        assert state.cp.proj.work_items[item_id].attempt == 2   # 计预算（§7/§8）
        assert state.cp.proj.nodes[node_id].terminated           # 超时节点已清理
        assert state.cp.proj.leases[lease_id].active is False
        assert state.cp.proj.nodes[new_node].lifecycle == LifecycleState.ACTIVE


# ---------------------------------------------------------------------------
# §2/§7 裂变规模与 DAG deps
# ---------------------------------------------------------------------------

class TestAdmissionFixes:
    def test_subtasks_over_limit_structured_reject(self, state):
        ok, _ = state.publish_spec({"max_team_workers": 2})
        assert ok
        subs = [{"provider": "mock", "model": "c-fast", "title": f"t{i}"}
                for i in range(3)]
        ok, r = state.delegate({"kind": "derive", "subtasks": subs})
        assert not ok
        assert r["ok"] is False and r["error"] == "SUBTASKS_OVER_LIMIT" and r["max"] == 2
        # 无 item 创建（不静默截断成 2 条）
        workers = [i for i in state.cp.proj.work_items.values() if i.kind.value == "derive"]
        assert workers == []

    def test_bad_deps_index_rejected(self, state):
        subs = [{"provider": "mock", "model": "c-fast", "title": "a"},
                {"provider": "mock", "model": "c-fast", "title": "b", "deps": [5]}]
        ok, r = state.delegate({"kind": "derive", "subtasks": subs})
        assert not ok and r["error"] == "BAD_DEPS"

    def test_deps_gate_pending_and_late_start(self, state):
        subs = [{"provider": "mock", "model": "b-kimi", "title": "上游"},
                {"provider": "mock", "model": "c-fast", "title": "下游", "deps": [0]}]
        ok, r = state.delegate({"kind": "derive", "subtasks": subs})
        assert ok, r
        assert len(r["items"]) == 1                       # 只有上游启动
        first = r["items"][0]
        assert first["subtask_index"] == 0
        assert len(r["pending"]) == 1
        pend = r["pending"][0]
        second_id = pend["item_id"]
        assert second_id != first["item_id"]
        assert pend["waiting_on"] == [first["item_id"]]
        assert not state.cp.proj.item_ready(second_id)
        # 下游未启动：名下无节点
        assert all(n.item_id != second_id for n in state.cp.proj.nodes.values())

        # 依赖未满足时直接启动 → begin_node DEPS_NOT_READY 硬门禁（§4）
        ok, r = state.delegate({"item_id": second_id, "subtask": subs[1]})
        assert not ok and r["error"] == "DEPS_NOT_READY"

        # 上游 accept → 下游解锁，冷启动（重跑模式的 plain 分支）
        ok, _ = state.submit_output({"item_id": first["item_id"],
                                     "node_id": first["node_id"], "output": "上游产出",
                                     **_fence(first)})
        assert ok
        ok, r = state.review({"item_id": first["item_id"], "verdict": "accept",
                              "output": "上游产出"})
        assert ok and r["outcome"] == "accepted"
        assert state.cp.proj.item_ready(second_id)
        ok, r = state.delegate({"item_id": second_id, "subtask": subs[1]})
        assert ok and r["items"][0]["item_id"] == second_id
        assert state.cp.proj.work_items[second_id].attempt == 1   # 冷启动不耗预算

    def test_split_rejects_caller_claimed_human_override(self, state):
        # Compatibility change: agent delegate is never a human authority channel.
        n_items = len(state.cp.proj.work_items)
        n_events = len(state.cp.store.read_all())
        ok, r = state.delegate({"kind": "split", "subtasks": [
            {"provider": "mock", "model": "a-glm", "source": "human",
             "title": "同构分片", "prompt": "写"}]})
        assert not ok and r["error"] == "BAD_SUBTASK"
        assert "source" in r["errors"]
        assert len(state.cp.proj.work_items) == n_items
        assert len(state.cp.store.read_all()) == n_events


# ---------------------------------------------------------------------------
# §9.5 分裂对 peer 通道
# ---------------------------------------------------------------------------

class TestPeerEndpoint:
    def test_peer_roundtrip(self, state):
        ok, r = state.delegate({"kind": "split", "subtasks": [
            {"provider": "mock", "model": "a-glm", "title": "分片", "prompt": "写"}]})
        assert ok, r
        it = r["items"][0]
        ok, r = state.peer({"channel_id": it["channel_id"],
                            "from_node": it["assistant_node_id"],
                            "body": "后半完成：接口 X 已对齐"})
        assert ok and r["message_id"].startswith("msg-")
        msg = state.cp.proj.messages[r["message_id"]]
        assert msg["delivered"] is True                        # queued → delivered
        assert msg["from_node"] == it["assistant_node_id"]
        assert msg["body"] == "后半完成：接口 X 已对齐"

    def test_peer_unknown_channel_400(self, state):
        ok, r = state.peer({"channel_id": "chan-nope", "from_node": "node-x",
                            "body": "hi"})
        assert not ok and r["ok"] is False and r["error"] == "CHANNEL_UNKNOWN"
        ok, r = state.peer({"from_node": "node-x", "body": "缺 channel"})
        assert not ok and r["ok"] is False


# ---------------------------------------------------------------------------
# §4 观测卫生：fence 校验先于观测记账；记账必须挂在已登记节点上
# ---------------------------------------------------------------------------

class TestSubmitOrdering:
    def test_fence_failure_leaves_no_dirty_observation(self, state):
        """伪造 fence 被拒后：事件流（唯一真源）不留该节点的 stop/token 脏记录。"""
        it = _derive(state, provider="mock", model="b-kimi", title="t")
        ok, r = state.submit_output({
            "item_id": it["item_id"], "node_id": it["node_id"], "output": "v",
            "context_epoch": 999, "session_id": "sess-forged",
            "stop_reason": "completed",
            "token_usage": {"input_tokens": 5, "output_tokens": 1}})
        assert not ok and r["error"] == "FENCE_VIOLATION"
        dirty = [e for e in state.cp.store.read_all()
                 if e.kind in ("stop_reason_recorded", "token_usage_recorded")
                 and e.payload.get("node_id") == it["node_id"]]
        assert dirty == []

    def test_record_usage_unknown_node_rejected(self, state):
        """记账方法的 node_id 必须已登记（ctx-job:* 记账账户除外）。"""
        with pytest.raises(ControlPlaneError) as e1:
            state.cp.record_token_usage("node-ghost", input_tokens=1)
        assert e1.value.code == "NO_NODE"
        with pytest.raises(ControlPlaneError) as e2:
            state.cp.record_stop_reason("node-ghost", "completed")
        assert e2.value.code == "NO_NODE"


# ---------------------------------------------------------------------------
# HTTP 层：新端点与结构化错误码
# ---------------------------------------------------------------------------

class TestHTTPLayerFixes:
    @pytest.fixture()
    def base_url(self, tmp_path):
        Handler.state = PanelState(tmp_path / "ws")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{port}"
        httpd.shutdown()

    def _post(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {Handler.state.token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_peer_and_review_over_http(self, base_url):
        st = Handler.state
        _, adm = self._post(base_url + "/api/delegate", {
            "kind": "split", "subtasks": [{"provider": "mock", "model": "a-glm",
                                           "title": "t", "prompt": "p"}]})
        assert adm["ok"] and adm["items"]
        it = adm["items"][0]
        status, r = self._post(base_url + "/api/peer", {
            "channel_id": it["channel_id"], "from_node": it["assistant_node_id"],
            "body": "回报"})
        assert status == 200 and r["ok"] and "message_id" in r
        status, r = self._post(base_url + "/api/peer", {
            "channel_id": "chan-nope", "from_node": "n", "body": "x"})
        assert status == 400 and r["error"] == "CHANNEL_UNKNOWN"
        # submit → review reject（挂起）→ 重跑 → accept，全链经 HTTP
        status, r = self._post(base_url + "/api/submit", {
            "item_id": it["item_id"], "node_id": it["node_id"], "output": "v1",
            "context_epoch": it["context_epoch"], "session_id": it["session_id"]})
        assert status == 200
        status, r = self._post(base_url + "/api/review", {
            "item_id": it["item_id"], "verdict": "reject",
            "attribution": "description", "reason": "不清"})
        assert status == 200 and r["outcome"] == "rejected-awaiting-rerun"
        status, r = self._post(base_url + "/api/delegate", {
            "item_id": it["item_id"],
            "subtask": {"provider": "mock", "model": "a-glm", "title": "t"}})
        assert status == 200 and r["items"][0]["kind"] == "re-run"
        node2 = r["items"][0]["node_id"]
        rerun = r["items"][0]  # fence 语境跟随重跑新节点（P1-2）
        status, r = self._post(base_url + "/api/submit", {
            "item_id": it["item_id"], "node_id": node2, "output": "v2",
            "context_epoch": rerun["context_epoch"], "session_id": rerun["session_id"]})
        assert status == 200
        status, r = self._post(base_url + "/api/review", {
            "item_id": it["item_id"], "verdict": "accept", "output": "v2"})
        assert status == 200 and r["outcome"] == "accepted"
        assert st.cp.proj.work_items[it["item_id"]].acceptance == AcceptanceState.ACCEPTED

    def test_delegate_over_limit_http_400(self, base_url):
        status, r = self._post(base_url + "/api/delegate", {
            "kind": "derive", "subtasks": [
                {"provider": "mock", "model": "c-fast", "title": f"t{i}"}
                for i in range(4)]})   # 默认 max_team_workers=3
        assert status == 400
        assert r["error"] == "SUBTASKS_OVER_LIMIT" and r["max"] == 3
