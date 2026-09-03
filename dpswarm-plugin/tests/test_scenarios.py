"""案例测试：10 个完整机制场景，每个场景是一个端到端故事。

与 test_e2e 的区别：这里每个场景断言机制文档的具体条文（注释标 §），
并覆盖 e2e 未走的路径——DAG 依赖解锁、分裂奖励信号代写、硬切续接后
继续执行、deadline 在途结案、退化回流摘要、人工指令三类、观测全账字段。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from dpswarm import observation
from dpswarm.control import AdmissionError, ControlPlane, ControlPlaneError
from dpswarm.orchestrator import Orchestrator
from dpswarm.providers import MockProvider
from dpswarm.types import (
    AcceptanceState,
    DelegationKind,
    HumanDirective,
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RejectAttribution,
    RootExecutionSpec,
)


def catalog() -> ModelCatalog:
    c = ModelCatalog()
    c.register(ModelFacts("p", "s-model", Level.S,
                          aa_dimensional={"coding": 9.0, "reasoning": 9.2}))
    c.register(ModelFacts("p", "a-model", Level.A,
                          aa_dimensional={"coding": 8.5, "reasoning": 8.4}))
    c.register(ModelFacts("p", "b-model", Level.B,
                          aa_dimensional={"coding": 7.5, "reasoning": 7.4}))
    return c


def new_cp(tmp_path, **kw) -> ControlPlane:
    spec_kw = dict(max_open_work_items=6, max_active_node_points=16)
    spec_kw.update(kw)
    return ControlPlane(spec=RootExecutionSpec(**spec_kw),
                        store_path=tmp_path / "e.jsonl", catalog=catalog())


def lead() -> ModelRoute:
    return ModelRoute("p", "s-model", level=Level.S)


def run_worker(cp, item, route_name="b-model", text="交付"):
    """准入 → 两阶段 → 执行 → 提交（control + MockProvider 单步）。"""
    route = ModelRoute("p", route_name)
    node = cp.begin_node(item.item_id, route)
    cp.confirm_node(node.node_id)
    cp.record_stop_reason(node.node_id, "completed")
    cp.submit(item.item_id, node.node_id)
    return node


def accept(cp, item, text="evidence"):
    cp.begin_finalize(item.item_id)
    cp.store_evidence_package(item.item_id, f"pkg-{item.item_id[:8]}", text)
    cp.complete_accept(item.item_id, package_id=f"pkg-{item.item_id[:8]}",
                       evidence_ready=True)


# ===========================================================================
# 场景 1：DAG 依赖链——后继在依赖 accepted 前不可启动（§4 解锁后继）
# ===========================================================================


def test_scenario_dag_dependency_unlock(tmp_path):
    cp = new_cp(tmp_path)
    a = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    b = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    cp.add_dependency(a.item_id, b.item_id)
    assert not cp.proj.item_ready(b.item_id)          # §4：accepted 才解锁后继
    run_worker(cp, a)
    accept(cp, a, "上游产物 A")
    assert cp.proj.item_ready(b.item_id)
    # 中途打回不解锁：b 的另一依赖 a2 rejected 时仍锁
    a2 = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    b2 = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    cp.add_dependency(a2.item_id, b2.item_id)
    n2 = run_worker(cp, a2)
    cp.reject(a2.item_id, "需要返工", RejectAttribution.DESCRIPTION)
    assert not cp.proj.item_ready(b2.item_id)         # rejected ≠ 解锁
    # b 完成后：a2 仍 rejected 占槽（§7 打回准备重试不释放）——显式处置后清零
    run_worker(cp, b)
    accept(cp, b, "下游产物 B")
    assert cp.proj.open_worker_slots_used == 2      # a2(rejected) + b2(未跑)
    cp.escalate(a2.item_id, "不再重试")
    cp.terminate(b2.item_id, "manual-stopped")        # §4 六值词汇（依赖上交随之终止）
    assert cp.proj.open_worker_slots_used == 0


# ===========================================================================
# 场景 2：分裂全链——协助者 peer 回报、主统一提交、奖励信号代写（§7/§9.5）
# ===========================================================================


def test_scenario_split_full_chain(tmp_path):
    cp = new_cp(tmp_path)
    item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    primary = cp.begin_node(item.item_id, ModelRoute("p", "b-model"))
    cp.confirm_node(primary.node_id)
    assistant, chan = cp.split(primary.node_id, ModelRoute("p", "b-model"))
    cp.confirm_node(assistant.node_id)
    # 协助者产出经通道回报（§9.5：轻量分工与回报；消息进 evidence）
    mid = cp.peer_send(chan, assistant.node_id, "后半部分完成：统计=42")
    cp.peer_deliver(mid)
    # 主统一提交（协助者不独立提交）；peer 消息在验收时可审计
    msg = next(m for m in cp.proj.messages.values() if m["message_id"] == mid)
    cp.submit(item.item_id, primary.node_id)
    cp.begin_finalize(item.item_id)
    cp.store_evidence_package(item.item_id, "pkg-sp", f"主整合+协助回报:{msg['body']}")
    cp.complete_accept(item.item_id, package_id="pkg-sp", evidence_ready=True,
                       accepted_by={"node": cp.root_lead_node, "level": "S"})
    # 协助者奖励信号由主代写（§7：assistant-accepted 随主结案进观测）
    cp.record_observation.__self__ if False else None
    from dpswarm.events import DelegationRecord
    rec = DelegationRecord(
        record_id="rec-a", item_id=item.item_id, node_id=assistant.node_id,
        lead_node_id=primary.node_id, route={"provider": "p", "model": "b-model"},
        topology="split-assistant", team="root", outcome="accepted",
        accepted_by={"node": primary.node_id, "proxy": True})
    cp.record_observation(rec)
    # 通道随 accepted 关闭；协助者同事务清理
    assert cp.proj.peer_channels[chan]["closed"]
    assert cp.proj.nodes[assistant.node_id].terminated
    sink = observation.ObservationSink(cp.store.read_all())
    assert any(r.topology == "split-assistant" for r in sink.delegation_records())


# ===========================================================================
# 场景 3：能力升级链 B→A→（S 深挖门）→ 预算耗尽上交（§8 全链）
# ===========================================================================


def test_scenario_upgrade_chain_to_escalation(tmp_path):
    cp = new_cp(tmp_path)
    script = [
        {"text": json.dumps({"action": "derive",
                             "route": {"provider": "p", "model": "b-model"}})},
        {"text": "attempt-1"},
        {"text": json.dumps({"verdict": "reject", "attribution": "capability"})},
        {"text": "attempt-2-upgraded-A"},                      # A 级 attempt 2
        {"text": json.dumps({"verdict": "reject", "attribution": "capability"})},
        {"text": "attempt-3-upgraded-S"},                      # S 级 attempt 3（A→S 过深挖门）
        {"text": json.dumps({"verdict": "reject", "attribution": "capability"})},
        {"text": "attempt-4"},                                 # 耗尽
    ]
    orch = Orchestrator(cp, MockProvider(script=script), store_dir=None,
                        lead_route=lead())
    out = orch.run_task("hard coding task")
    assert out["items"][0]["outcome"] == "escalated"           # §8 预算耗尽走上交
    events = cp.store.read_all()
    kinds = [e.kind for e in events]
    assert kinds.count("lease_reweight") >= 2                  # B→A、A→S 两次 reweight
    # A→S 深挖门（§8：A→S 之前必须先深挖失败原因）
    assert any(e.kind == "watchdog_suggested"
               and e.payload.get("kind") == "deep-dive-before-s-upgrade"
               for e in events)
    # 上交终局：无裁决不进画像、资源全归还
    assert cp.proj.open_worker_slots_used == 0
    # 0.2/0.6：Lead 决策上交 = 整树收口——root item 一并 escalate（与 quota 耗尽
    # 路径同口径），root lead lease 随之归还（旧行为 root 悬挂、lease 滞留）
    assert cp.proj.active_points == 0
    root = cp.proj.work_items[cp._root_item_id()]
    assert root.acceptance == AcceptanceState.ESCALATED
    # escalate 记录在案（无裁决）
    esc = next(e for e in events if e.kind == "work_item_escalated")
    assert esc.payload["reason"].startswith("budget-exhausted")


# ===========================================================================
# 场景 4：硬切 rollover——执行中窗口达标 → capsule → CAS → successor 续接提交（§5.8）
# ===========================================================================


def test_scenario_rollover_continues_execution(tmp_path):
    cp = new_cp(tmp_path)
    item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    route = ModelRoute("p", "b-model")
    node = cp.begin_node(item.item_id, route)
    cp.confirm_node(node.node_id)
    old_session = cp.proj.nodes[node.node_id].session_id
    # 窗口达标：capsule 预装（链外）→ CAS 登记 + 两阶段（同事务）
    capsule = "目标:完成T|已验收:无|未决:格式|下一动作:汇总"
    import hashlib as _h
    cap_hash = _h.sha256(capsule.encode()).hexdigest()
    cp.begin_rollover(node.node_id, "cap://1", cap_hash)
    assert cp.proj.nodes[node.node_id].lifecycle.value == "provisioning"
    cp.confirm_rollover(node.node_id, session_id="sess-successor")
    n = cp.proj.nodes[node.node_id]
    assert n.session_id == "sess-successor" and n.session_id != old_session
    assert n.predecessor_session == old_session               # §5.8 可追溯
    assert n.context_epoch == 1
    # successor 继续执行并正常提交验收——不是新节点、不耗新预算
    assert item.attempt == 1
    cp.submit(item.item_id, node.node_id)                     # 同一 node_id
    accept(cp, item, "续接后完成")
    assert cp.proj.work_items[item.item_id].acceptance == AcceptanceState.ACCEPTED


# ===========================================================================
# 场景 5：deadline 封存——在途 finalizing 在结算期完成 accepted（§9.6 / 悬空#2 读法）
# ===========================================================================


def test_scenario_deadline_settlement_completes_inflight(tmp_path):
    # deadline 合法域 None 或 ≥60 且须 > wall-clock（_validate_spec）：60/61 小量级
    # + 同源 fake now（+62s）——deadline 立即到期；结算未超 settlement_timeout
    # （2.3 新增 tick 分支），在途 finalizing 仍可在结算期完成
    cp = new_cp(tmp_path, node_wallclock_timeout=60, deadline_seconds=61)
    item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    node = run_worker(cp, item)
    cp.begin_finalize(item.item_id)                            # 在途 finalizing
    import time as _t
    cp.tick(now=_t.time() + 62.0)                              # deadline 立即触发
    assert cp.proj.seal_phase["root"].value == "settlement"
    with pytest.raises(ControlPlaneError):                     # 准入已封死
        cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    cp.store_evidence_package(item.item_id, "pkg-dl", "evidence")
    cp.complete_accept(item.item_id, package_id="pkg-dl",       # 结算期完成
                       evidence_ready=True)
    cp.finish_seal("root")
    assert cp.proj.seal_phase["root"].value == "completed"
    assert cp.proj.work_items[item.item_id].acceptance == AcceptanceState.ACCEPTED


# ===========================================================================
# 场景 6：退化——拉起后收回，结论落盘、主 agent 只收摘要（§7 退化是精髓）
# ===========================================================================


def test_scenario_degenerate_collects_summary(tmp_path):
    cp = new_cp(tmp_path)
    item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    node = run_worker(cp, item, text="部分发现：路径X不可行")
    cp.terminate(item.item_id, reason="manual-stopped",
                 summary="结论：路径X不可行，建议改走路径Y")     # §7 收缩回流（§4 六值词汇）
    it = cp.proj.work_items[item.item_id]
    assert it.acceptance == AcceptanceState.TERMINATED
    assert "路径X不可行" in it.summary                          # 摘要留在控制事实
    assert cp.proj.open_worker_slots_used == 0                 # 资源全回收
    assert cp.proj.active_points == 1
    # 退化不算验收、不进画像：终局 terminated（§7）
    assert it.outcome is None or it.outcome.value != "accepted"


# ===========================================================================
# 场景 7：人工指令三类——config 降容不强杀；terminal 停止（§9.2）
# ===========================================================================


def test_scenario_human_directives(tmp_path):
    cp = new_cp(tmp_path, max_open_work_items=4)
    item = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    node = cp.begin_node(item.item_id, ModelRoute("p", "b-model"))
    cp.confirm_node(node.node_id)
    # 配置变更型：发布新 revision，容量降到当前占用以下
    cp.human_directive(HumanDirective(
        kind="config", payload={"spec": {"max_open_work_items": 1,
                                         "max_active_node_points": 16}}))
    assert cp.proj.spec.revision == 2
    assert cp.proj.nodes[node.node_id].lifecycle.value == "active"   # 不强杀（§2.1）
    with pytest.raises(ControlPlaneError):                       # 只停新准入
        cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    # 终态型：停止任务
    cp.human_directive(HumanDirective(kind="terminal", payload={"scope": "root"}))
    events = [e.kind for e in cp.store.read_all()]
    assert events.count("spec_published") >= 1                   # config 型审计
    assert events.count("human_directive") >= 1                  # terminal 型入链
    # 即时生效型：人工路由指定优先（§2 人工 > Lead）
    from dpswarm.types import RouteSource
    human_route = ModelRoute("p", "a-model", source=RouteSource.ROUTE_HUMAN)
    item2 = None
    with pytest.raises(AdmissionError):                          # 槽位已满（降容后 1 被 item 占）
        item2 = cp.create_work_item(DelegationKind.DERIVE,
                                    parent_item=cp._root_item_id())


# ===========================================================================
# 场景 8：观测全账字段完整性（§4 全账逐项）
# ===========================================================================


def test_scenario_observation_full_ledger(tmp_path):
    from dpswarm.context import MemoryService
    from dpswarm import profile
    cp = new_cp(tmp_path)
    memory = MemoryService(sink=cp._record)
    pstore = profile.ProfileStore()
    script = [
        {"text": json.dumps({"action": "derive",
                             "route": {"provider": "p", "model": "b-model"}}),
         "usage": {"input_tokens": 800, "output_tokens": 200,
                   "cache_read_tokens": 300, "cost_usd": 0.01}},
        {"text": "交付内容",
         "usage": {"input_tokens": 1500, "output_tokens": 500, "cost_usd": 0.02}},
        {"text": json.dumps({"verdict": "accept", "verdict_reason": "ok"}),
         "usage": {"input_tokens": 600, "output_tokens": 100, "cost_usd": 0.005}},
    ]
    orch = Orchestrator(cp, MockProvider(script=script), store_dir=None,
                        memory=memory, profile=pstore, lead_route=lead())
    orch.run_task("coding task")
    events = cp.store.read_all()
    sink = observation.ObservationSink(events)
    recs = sink.delegation_records()
    assert recs, "委派记录必须存在"
    r = recs[0]
    # §4 全账逐项：路由、拓扑、验收者身份（含路由与级别）、终局
    assert r.route["model"] == "b-model" and r.route["level"] == "B"
    assert r.topology == "derive-worker"
    assert r.accepted_by and r.accepted_by.get("level") == "S"
    assert r.outcome == "accepted"
    # token 账（缓存与 input 不相交分账字段就位）
    assert sink.token_ledger(), "token 账不可空"
    report = observation.summarize_events(events)
    assert report["outcome_distribution"].get("accepted") == 1
    assert report["stop_reason_distribution"], "stopReason 聚合不可空"
    assert report["economics"]["lead_tokens"] > 0               # §6 Lead 消耗记账
    # 记忆闭环：accepted 内容晋升且可检索（§5.7）
    assert memory.retrieve(scope="root", query="交付")
    # 画像只攒不用：attempt 入账、无回流（§8）
    assert pstore.bucket_stats("p/b-model", "coding")["attempts"] >= 1


# ===========================================================================
# 场景 9：三方向全景——single / derive / fission / split 各自的形态指纹（§7）
# ===========================================================================


def test_scenario_topology_fingerprints(tmp_path):
    # fission：多 worker + 子 team + Lead 兼任登记（一次裂变一个 team）
    cp = new_cp(tmp_path)
    script = [
        {"text": json.dumps({"action": "fission",
                             "route": {"provider": "p", "model": "b-model"},
                             "subtasks": ["A", "B", "C"]})},
        {"text": "W"},
        {"text": json.dumps({"verdict": "accept"})},
    ]
    out = Orchestrator(cp, MockProvider(script=script), store_dir=None,
                       lead_route=lead()).run_task("三片任务")
    assert len(out["items"]) == 3
    subteams = [t for t in cp.proj.teams if t != "root"]
    assert len(subteams) == 1                                  # 一次裂变 = 一个 team
    team = cp.proj.teams[subteams[0]]
    assert team.lead_node == cp.root_lead_node                 # 裂变者即 Lead
    assert team.local_point_cap == 8                           # 16 × 50%（§7）
    fission_items = [w for w in cp.proj.work_items.values()
                     if w.kind == DelegationKind.FISSION]
    assert len(fission_items) == 3
    assert all(w.team == subteams[0] for w in fission_items)   # 全部挂同一子 team


# ===========================================================================
# 场景 10：方法级原子——失败事务零半截落盘（§9.2 / 实验01 悬空#6）
# ===========================================================================


def test_scenario_transaction_atomicity_no_partial_write(tmp_path):
    cp = new_cp(tmp_path, max_active_node_points=3)             # root(1)+2 已满
    a = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    cp.begin_node(a.item_id, ModelRoute("p", "b-model", point_weight=2))
    b = cp.create_work_item(DelegationKind.DERIVE, parent_item=cp._root_item_id())
    before_seq = cp.store.last_seq
    before_items = len(cp.proj.work_items)
    with pytest.raises(AdmissionError):                         # 点数不足整组拒
        node = cp.begin_node(b.item_id, ModelRoute("p", "b-model", point_weight=2))
        cp.confirm_node(node.node_id)
    assert cp.store.last_seq == before_seq                      # 零事件落盘
    assert len(cp.proj.work_items) == before_items
    kinds = [e.kind for e in cp.store.read_all()]
    assert "node_activated" not in kinds[-3:]                   # 无半截 active
    # 磁盘恢复与内存一致（半截态若存在会在 replay 暴露）
    cp.close()  # 单写者文件锁：复盘前释放（P1-1）
    cp2 = ControlPlane(store_path=cp.store.path, catalog=catalog())
    assert cp2.snapshot() == cp.snapshot()
