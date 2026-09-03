"""机制层四项修复的回归锚点（P1-1/2/3/6，用户批准方案）。

①事务 envelope：单行事务 + fail-closed 回放 + seq 连续 + 跨进程文件锁
②证据链：submit 内容寻址落盘，review 只准引用（不可替换、重启可恢复）
③fence：delegate 回传 (context_epoch, session_id)，submit 不匹配即拒
④root 终局：seal 收尾推 root accepted/terminated，全树 drain + lease 归还
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from dpswarm.control import ControlPlane, ControlPlaneError
from dpswarm.server import PanelState
from dpswarm.types import AcceptanceState, DelegationKind, Level, ModelRoute, RootExecutionSpec


def _cp(tmp_path, name="e.jsonl", **kw) -> ControlPlane:
    return ControlPlane(store_path=tmp_path / name, **kw)


def _derive_ready(cp: ControlPlane, model="b-kimi") -> tuple:
    from dpswarm.server import default_catalog
    if cp.catalog.resolve("mock", model) is None:
        cp.catalog.register(default_catalog().resolve("mock", model))
    item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    node = cp.begin_node(item.item_id, ModelRoute("mock", model, level=Level.B))
    cp.confirm_node(node.node_id)
    return item, node


class TestTxnEnvelope:
    def test_each_txn_is_one_line(self, tmp_path):
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp)
        cp.submit(item.item_id, node.node_id, "交付正文")
        lines = [l for l in (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines() if l]
        assert all(json.loads(l).get("txn") for l in lines)  # 每行都是 envelope
        # submit 的 package_stored + work_item_submitted 同事务同行（P1-1）
        last = json.loads(lines[-1])
        kinds = [e["kind"] for e in last["events"]]
        assert kinds == ["package_stored", "work_item_submitted"]

    def test_crash_tail_dropped_fail_closed(self, tmp_path):
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp)
        cp.submit(item.item_id, node.node_id, "v1")
        cp.close()
        # 模拟崩溃残尾：半行 JSON
        with open(tmp_path / "e.jsonl", "a", encoding="utf-8") as f:
            f.write('{"txn": 99, "events": [{"seq": 999, "kind": "spec_pub')
        cp2 = _cp(tmp_path)
        assert cp2.proj.work_items[item.item_id].acceptance == AcceptanceState.SUBMITTED
        assert cp2.store.last_seq == cp.store.last_seq  # 残尾整事务丢弃

    def test_midfile_corruption_raises(self, tmp_path):
        cp = _cp(tmp_path)
        _derive_ready(cp)
        cp.close()
        p = tmp_path / "e.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        lines.insert(1, "{corrupt-middle")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="EVENT_LOG_CORRUPT"):
            _cp(tmp_path)

    def test_seq_gap_raises(self, tmp_path):
        cp = _cp(tmp_path)
        _derive_ready(cp)
        cp.close()
        p = tmp_path / "e.jsonl"
        obj = json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
        obj["events"][0]["seq"] = 999  # 抽屉里的 seq 空洞
        lines = p.read_text(encoding="utf-8").splitlines()
        lines[-1] = json.dumps(obj, ensure_ascii=False)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="EVENT_LOG_CORRUPT"):
            _cp(tmp_path)

    def test_second_writer_same_process_blocked(self, tmp_path):
        cp = _cp(tmp_path)
        with pytest.raises(RuntimeError, match="EVENT_LOG_LOCKED"):
            _cp(tmp_path)
        cp.close()
        _cp(tmp_path)  # close 后可重建

    def test_second_writer_cross_process_blocked(self, tmp_path):
        cp = _cp(tmp_path)
        code = ("from dpswarm.control import ControlPlane;"
                f"ControlPlane(store_path=r'{tmp_path}\\e.jsonl')")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=Path(__file__).resolve().parents[1])
        assert r.returncode != 0 and "EVENT_LOG_LOCKED" in r.stderr


class TestBootstrapCompliance:
    """修复 3.1（§9.1 校验先于落盘）：bootstrap 不再是全系统唯一不校验写路径。"""

    def test_bootstrap_is_single_validated_txn(self, tmp_path):
        """7 个 bootstrap 事件在空投影上逐一过 check_event 暂存，单次 append_txn
        一行落盘（修复前：7 次独立 append = 7 次 fsync 且绕过全部 invariant）。"""
        _cp(tmp_path)
        lines = [l for l in (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines() if l]
        assert len(lines) == 1                            # 单行 envelope = 单次 fsync
        evs = json.loads(lines[0])["events"]
        assert [e["seq"] for e in evs] == list(range(7))  # seq 连续
        assert [e["kind"] for e in evs] == [
            "root_started", "work_item_created", "lease_acquired",
            "node_provisioning", "route_resolved", "node_activated",
            "node_role_changed"]

    def test_invalid_spec_rejected_before_any_write(self, tmp_path):
        """先 _validate_spec 后落盘：非法 spec 零事件（修复前非法 spec 静默进日志）。"""
        with pytest.raises(ControlPlaneError) as ei:
            _cp(tmp_path, spec=RootExecutionSpec(max_open_work_items=0))
        assert ei.value.code == "SPEC_INVALID"
        assert not (tmp_path / "e.jsonl").exists()        # 校验先于落盘：零事件

    def test_half_bootstrap_typed_error(self, tmp_path):
        """半截态（有 root_started 但缺 root item——历史逐条 append 的崩溃窗口）：
        重开显式类型化报错 BOOTSTRAP_INCOMPLETE，不带无修复路径的账本启动
        （修复前重启后一切操作 NO_ROOT_ITEM）。"""
        line = json.dumps({"txn": 1, "events": [{
            "seq": 0, "kind": "root_started",
            "payload": {"root_id": "root-x", "spec": asdict(RootExecutionSpec()),
                        "lead_node_id": "node-x"},
            "ts": time.time()}]}, ensure_ascii=False)
        (tmp_path / "e.jsonl").write_text(line + "\n", encoding="utf-8")
        with pytest.raises(ControlPlaneError) as ei:
            _cp(tmp_path)
        assert ei.value.code == "BOOTSTRAP_INCOMPLETE"


class TestEvidenceChain:
    def test_submit_stores_artifact_and_binds_package(self, tmp_path):
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp)
        cp.submit(item.item_id, node.node_id, "ACTUAL_DELIVERY")
        wi = cp.proj.work_items[item.item_id]
        assert wi.submission_package_id
        ref = cp.proj.packages[wi.submission_package_id]["artifact_ref"]
        got = (tmp_path / "artifacts" / ref).read_text(encoding="utf-8")
        assert got == "ACTUAL_DELIVERY"  # 全文落盘（非仅 200 字预览）

    def test_review_cannot_substitute_evidence(self, tmp_path):
        st = PanelState(tmp_path / "ws")
        ok, it = st.delegate({"kind": "derive", "subtasks": [
            {"provider": "mock", "model": "b-kimi", "title": "t", "prompt": "p"}]})
        assert ok and it["items"]
        d = it["items"][0]
        ok, r = st.submit_output({"item_id": d["item_id"], "node_id": d["node_id"],
                                  "output": "ACTUAL_DELIVERY",
                                  "context_epoch": d["context_epoch"],
                                  "session_id": d["session_id"]})
        assert ok
        # review 自带"替换证据"正文——服务端必须忽略，accepted 引用的是 submit 落盘包
        ok, r = st.review({"item_id": d["item_id"], "verdict": "accept",
                           "output": "SUBSTITUTED_EVIDENCE"})
        assert ok and r["outcome"] == "accepted"
        wi = st.cp.proj.work_items[d["item_id"]]
        pkg = st.cp.proj.packages[wi.submission_package_id]
        assert pkg["content_preview"] == "ACTUAL_DELIVERY"[:200]
        art = (tmp_path / "ws" / "artifacts" / pkg["artifact_ref"]).read_text(encoding="utf-8")
        assert art == "ACTUAL_DELIVERY"

    def test_accept_without_submission_package_rejected(self, tmp_path):
        st = PanelState(tmp_path / "ws")
        # 直连控制面 submit 无正文（无包）→ review accept 必须拒
        cp = st.cp
        item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
        from dpswarm.server import default_catalog
        cp.catalog.register(default_catalog().resolve("mock", "b-kimi"))
        node = cp.begin_node(item.item_id, ModelRoute("mock", "b-kimi", level=Level.B))
        cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id)
        ok, r = st.review({"item_id": item.item_id, "verdict": "accept"})
        assert not ok and r["error"] == "PACKAGE_MISSING"

    def test_accept_wrong_package_rejected(self, tmp_path):
        st = PanelState(tmp_path / "ws")
        ok, it = st.delegate({"kind": "derive", "subtasks": [
            {"provider": "mock", "model": "b-kimi", "title": "t", "prompt": "p"}]})
        d = it["items"][0]
        st.submit_output({"item_id": d["item_id"], "node_id": d["node_id"],
                          "output": "v", "context_epoch": d["context_epoch"],
                          "session_id": d["session_id"]})
        ok, r = st.review({"item_id": d["item_id"], "verdict": "accept",
                           "package_id": "dep-forged"})
        assert not ok and r["error"] == "EVIDENCE_MISMATCH"

    def test_evidence_survives_restart(self, tmp_path):
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp)
        cp.submit(item.item_id, node.node_id, "FULL CONTENT " * 50)
        pkg_id = cp.proj.work_items[item.item_id].submission_package_id
        cp.close()
        cp2 = _cp(tmp_path)
        ref = cp2.proj.packages[pkg_id]["artifact_ref"]
        assert (tmp_path / "artifacts" / ref).read_text(encoding="utf-8").startswith("FULL CONTENT")


class TestFence:
    def test_delegate_returns_fence_fields(self, tmp_path):
        st = PanelState(tmp_path / "ws")
        ok, it = st.delegate({"kind": "derive", "subtasks": [
            {"provider": "mock", "model": "b-kimi", "title": "t", "prompt": "p"}]})
        d = it["items"][0]
        assert isinstance(d["context_epoch"], int) and d["session_id"]

    def test_submit_missing_fence_rejected(self, tmp_path):
        st = PanelState(tmp_path / "ws")
        ok, it = st.delegate({"kind": "derive", "subtasks": [
            {"provider": "mock", "model": "b-kimi", "title": "t", "prompt": "p"}]})
        d = it["items"][0]
        ok, r = st.submit_output({"item_id": d["item_id"], "node_id": d["node_id"],
                                  "output": "v"})
        assert not ok and r["error"] == "FENCE_REQUIRED"

    def test_stale_epoch_rejected_after_rollover(self, tmp_path):
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp, model="a-glm")
        old = {"context_epoch": cp.proj.nodes[node.node_id].context_epoch,
               "session_id": cp.proj.nodes[node.node_id].session_id}
        cp.begin_rollover(node.node_id, capsule_ref="cap", capsule_hash="h" * 8)
        cp.confirm_rollover(node.node_id)
        n = cp.proj.nodes[node.node_id]
        assert (n.context_epoch, n.session_id) != (old["context_epoch"], old["session_id"])
        # 旧 session（审计复现路径）提交 → 必须 FENCE_VIOLATION
        with pytest.raises(ControlPlaneError) as ei:
            cp.submit(item.item_id, node.node_id, "stale",
                      context_epoch=old["context_epoch"], session_id=old["session_id"])
        assert ei.value.code == "FENCE_VIOLATION"
        assert cp.proj.work_items[item.item_id].acceptance is None  # 未被污染


class TestRootClosure:
    def test_finish_seal_normal_closes_root(self, tmp_path):
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp)
        cp.submit(item.item_id, node.node_id, "交付")
        cp.begin_finalize(item.item_id)
        cp.complete_accept(item.item_id, package_id=cp.proj.work_items[item.item_id]
                           .submission_package_id, evidence_ready=True)
        cp.begin_seal("root")
        cp.begin_settlement("root")
        cp.finish_seal("root")
        root = cp.proj.work_items[cp._root_item_id()]
        assert root.acceptance == AcceptanceState.ACCEPTED  # root 终局（P1-6）
        assert cp.proj.active_points == 0                    # root lease 归还
        assert cp.proj.seal_phase["root"].value == "completed"

    def test_finish_seal_timed_out_terminates_and_drains(self, tmp_path):
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp)  # 在途节点留 ACTIVE
        cp.begin_seal("root")
        cp.begin_settlement("root")
        cp.finish_seal("root", timed_out=True)
        root = cp.proj.work_items[cp._root_item_id()]
        assert root.acceptance == AcceptanceState.TERMINATED
        assert cp.proj.active_points == 0                    # 全树 lease 归还（P1-6）
        assert cp.proj.nodes[node.node_id].terminated         # 滞留节点 drain

    def test_finish_seal_terminates_inflight_and_closes_channels(self, tmp_path):
        """§9.6 封存三段式收尾（修复 2.1）：回收循环对每个非终态 item 按序
        关通道 → drain → terminated(deadline-stopped) → successor 作废——
        修 CHANNEL_NOT_CLOSED 卡死 + 终态悬挂 + 槽位账面永不释放。"""
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp)                        # 在途 DERIVE，节点 ACTIVE
        assistant, chan = cp.split(node.node_id, ModelRoute("mock", "b-kimi", level=Level.B))
        cp.confirm_node(assistant.node_id)
        mid = cp.peer_send(chan, node.node_id, "在途消息不投递")
        cp.begin_seal("root")
        cp.begin_settlement("root")
        cp.finish_seal("root")
        it = cp.proj.work_items[item.item_id]
        assert it.acceptance == AcceptanceState.TERMINATED    # 终态悬挂修复
        assert it.outcome.value == "deadline-stopped"         # §4 六值词汇
        assert cp.proj.open_worker_slots_used == 0            # 槽位泄漏修复
        assert cp.proj.peer_channels[chan]["closed"]          # 通道随终态关闭（§9.5）
        assert cp.proj.nodes[assistant.node_id].terminated
        assert cp.proj.active_points == 0
        root = cp.proj.work_items[cp._root_item_id()]
        assert root.acceptance == AcceptanceState.ACCEPTED    # root 正常终局不受影响
        # 关通道与记丢弃原子完成（§9.5 不静默消失，修复 2.4）
        closed = [e for e in cp.store.read_all()
                  if e.kind == "peer_channel_closed" and e.payload["channel_id"] == chan]
        assert closed[-1].payload["dropped"] == [mid]
        # 封存完结后通道已关，peer_send 被拒（§9.5 决策 17）
        with pytest.raises(ControlPlaneError):
            cp.peer_send(chan, node.node_id, "封存后消息")

    def test_subteam_seal_does_not_touch_root_tree(self, tmp_path):
        """修复 2.2：finish_seal("team-x") 只回收该 team 子树——不得拔整棵树
        含 root（修复前回收循环无 team 过滤，子 team 封存会 drain 全树）。"""
        cp = _cp(tmp_path)
        from dpswarm.server import default_catalog
        cp.catalog.register(default_catalog().resolve("mock", "b-kimi"))
        route_b = ModelRoute("mock", "b-kimi", level=Level.B)
        # root 树内在途 derive（须不受子 team 封存影响）
        root_item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
        root_node = cp.begin_node(root_item.item_id, route_b)
        cp.confirm_node(root_node.node_id)
        # fission 建子 team + 子 team 内在途 worker
        fitem = cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id())
        sub_team = fitem.team
        assert sub_team != "root"
        sub_node = cp.begin_node(fitem.item_id, route_b)
        cp.confirm_node(sub_node.node_id)
        cp.begin_seal(sub_team)
        cp.begin_settlement(sub_team)
        cp.finish_seal(sub_team)
        # 子 team：item 终态 + 节点 drain + 相位完结
        assert cp.proj.work_items[fitem.item_id].acceptance == AcceptanceState.TERMINATED
        assert cp.proj.nodes[sub_node.node_id].terminated
        assert cp.proj.seal_phase[sub_team].value == "completed"
        # root 树不受影响：root item 未终局、root lead 与在途 derive 存活
        assert cp.proj.work_items[cp._root_item_id()].acceptance is None
        assert cp.proj.work_items[root_item.item_id].acceptance is None
        assert not cp.proj.nodes[root_node.node_id].terminated
        assert not cp.proj.nodes[cp.root_lead_node].terminated
        assert cp.proj.open_worker_slots_used == 1            # 只剩 root 树的 derive

    def test_terminate_close_event_carries_dropped(self, tmp_path):
        """§9.5 不静默消失（修复 2.4）：terminate 关通道的 peer_channel_closed
        payload 附 queued-undelivered 消息 id 列表；已投递的不在列。"""
        cp = _cp(tmp_path)
        item, node = _derive_ready(cp)
        _assistant, chan = cp.split(node.node_id, ModelRoute("mock", "b-kimi", level=Level.B))
        m1 = cp.peer_send(chan, node.node_id, "未投递-1")
        m2 = cp.peer_send(chan, node.node_id, "未投递-2")
        m3 = cp.peer_send(chan, node.node_id, "已投递")
        cp.peer_deliver(m3)
        cp.terminate(item.item_id, "manual-stopped")
        closed = [e for e in cp.store.read_all()
                  if e.kind == "peer_channel_closed" and e.payload["channel_id"] == chan]
        assert closed and closed[-1].payload["dropped"] == [m1, m2]

    def test_root_level_from_param_blocks_fission(self, tmp_path):
        cp = _cp(tmp_path, root_level=Level.B)  # 实际 root 模型为 B 级
        with pytest.raises(ControlPlaneError) as ei:
            cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id())
        assert ei.value.code in ("FISSION_FORBIDDEN", "FISSION_PERMISSION")

    def test_panel_root_level_from_aa_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DPSWARM_ROOT_MODEL", "deepseek-v4-pro")  # AA 53.2 → A
        st = PanelState(tmp_path / "ws")
        lead = st.cp.proj.nodes[st.cp.root_lead_node]
        assert lead.route.level == Level.A  # 不再永远 S（P1-5）
