"""编排器/观测层修复回归（机制文档 §2/§4/§5/§6/§7 对齐）：

- §4 背压包装覆盖全部调用面：429 退避重试（retry_after 优先、1s/2s/4s、封顶 8s）；
- §4 QUOTA 不炸穿 run_task：明确终止/上交人工，open item 有弃置、观测不断账；
- §7 裂变 = DAG 协调：subtasks 可选 deps（0 基下标）→ add_dependency 建边 →
  deferred 晋级调度（上游 accepted 后才 begin，不触发 DEPS_NOT_READY 门禁）；
- §4/§7 分裂拓扑观测标注（split-primary/split-assistant）+ 协助者奖励信号
  由主执行者代写（assistant-accepted/assistant-rejected 随主结案进观测）；
- §5.5/§7 CM 成本记触发方账下（ctx-job:<item_id>，economics cm_tokens 聚合）；
- §2 subtasks 超限硬准入：结构化拒绝（admission-rejected:SUBTASKS_OVER_LIMIT）。
"""
from __future__ import annotations

import json

import pytest

from dpswarm.control import ControlPlane
from dpswarm.context import MemoryService
from dpswarm.context.assembler import ContextAssembler
from dpswarm.context.manager import ContextManagerLLM
from dpswarm.observation import ObservationSink
from dpswarm.orchestrator import Orchestrator
from dpswarm.providers import MockProvider
from dpswarm.types import (
    AcceptanceState,
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
    StopReason,
    WorkItemOutcome,
)


def catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    cat.register(ModelFacts("p", "a-model", Level.A, aa_dimensional={"coding": 8.5}))
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


def make_cp(tmp_path, **spec_kw):
    return ControlPlane(spec=RootExecutionSpec(
        max_open_work_items=spec_kw.pop("max_open_work_items", 4),
        max_active_node_points=spec_kw.pop("max_active_node_points", 8), **spec_kw),
        store_path=tmp_path / "events.jsonl", catalog=catalog())


def make_orch(cp, script, **kw):
    kw.setdefault("lead_route", ModelRoute("p", "s-model", level=Level.S))
    return Orchestrator(cp, MockProvider(script=script), store_dir=None, **kw)


def _decision(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


B_ROUTE = {"provider": "p", "model": "b-model"}


class TestRateLimitBackoff:
    """§4：RATE_LIMIT 是背压信号——退避重试后成功，run_task 正常完成。"""

    def test_lead_call_backs_off_then_succeeds(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"raise": "rate-limit", "retry_after": 0},   # 首次 Lead 决策调用 429
            {"text": _decision("single")},
            {"text": "最终答案：42"},
        ])
        out = orch.run_task("the answer task")
        assert out["final"] == "single"
        root = cp._root_item_id()
        assert cp.proj.work_items[root].acceptance.value == "accepted"
        # raise 项同样推进脚本索引：退避后的重试读到第 2 条脚本
        calls = orch.provider.calls
        assert len(calls) == 3
        assert calls[0]["script_index"] == 0 and calls[1]["script_index"] == 1
        # 429 不记 aborted（不是终局，不进 failure audit）
        aborted = [e for e in cp.store.read_all()
                   if e.kind == "stop_reason_recorded"
                   and e.payload.get("stop_reason") == "aborted"]
        assert not aborted

    def test_persistent_429_settles_without_raising(self, tmp_path):
        """持续 429（1s/2s/4s 退避耗尽）：不炸穿 run_task——open item 按 QUOTA
        同构矩阵有弃置（非终态 escalate / FINALIZING terminate），槽位全归还。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"raise": "rate-limit", "retry_after": 0},   # 脚本耗尽后重复此条：持续 429
        ])
        out = orch.run_task("persistent 429 task")       # 不抛
        assert out["final"] == "rate-limit-exhausted"
        for w in cp.proj.work_items.values():
            assert w.acceptance in (AcceptanceState.ESCALATED,
                                    AcceptanceState.TERMINATED)
        assert cp.proj.open_worker_slots_used == 0


class TestQuotaTerminal:
    """§4：QUOTA 不是背压——明确终止/上交人工，不炸穿 run_task、观测不断账。"""

    def test_worker_quota_escalates_open_item(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"raise": "quota"},                          # worker 执行中配额耗尽
            {"raise": "quota"},                          # 下一轮 Lead 决策仍配额耗尽
        ])
        out = orch.run_task("hard quota task")           # 不抛
        assert out["final"] == "quota-exhausted"
        events = cp.store.read_all()
        kinds = [e.kind for e in events]
        # 运维审计（§4）：stop_reason_recorded 带 quota_exhausted
        quota_recs = [e for e in events if e.kind == "stop_reason_recorded"
                      and e.payload.get("quota_exhausted")]
        assert quota_recs
        # 0.2：open item 的弃置矩阵改为 escalate（未提交也可上交，含 root item）——
        # 不再产 work_item_terminated；终局由人工在上一级裁决（§8 异常路径）
        esc = [e for e in events if e.kind == "work_item_escalated"]
        assert any(e.payload["reason"] == "quota-exhausted" for e in esc)
        for w in cp.proj.work_items.values():
            assert w.acceptance.value == "escalated"
        # 资源收口：槽全归还
        assert cp.proj.open_worker_slots_used == 0
        # 观测不断账：worker 的委派记录照发（escalated，stop=aborted）
        sink = ObservationSink(events)
        recs = sink.delegation_records()
        assert recs and recs[0].outcome == "escalated"
        assert recs[0].stop_reason == "aborted"

    def test_review_quota_escalates_submitted_item(self, tmp_path):
        """验收调用耗尽配额：item 已 SUBMITTED → escalate（合法转换）。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付内容"},
            {"raise": "quota"},                          # _ask_lead_review 耗尽
        ])
        out = orch.run_task("review quota task")
        assert out["final"] == "quota-exhausted"
        events = cp.store.read_all()
        esc = [e for e in events if e.kind == "work_item_escalated"]
        assert esc and esc[0].payload["reason"] == "quota-exhausted"
        item = esc[0].payload["item_id"]
        assert cp.proj.work_items[item].acceptance.value == "escalated"
        assert cp.proj.open_worker_slots_used == 0


class TestLeadTerminalDecisions:
    """§7/§8：Lead 终局决策（escalate/degenerate）覆盖投影中全部非终态 item——
    outcome["items"] 只含已结案项，deps-stuck 悬挂项不在其中、不得漏收。"""

    def _stuck_script(self, final_action: str):
        return [
            {"text": _decision("derive", route=B_ROUTE, subtasks=[
                "part A", {"title": "part B", "deps": [0]}])},
            {"text": "A 交付"},
            # A 被打回且归因矛盾 → 直接上交；B 依赖永不可解锁（deps-stuck）
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "任务矛盾",
                                 "attribution": "contradiction"})},
            {"text": _decision(final_action)},
        ]

    def test_lead_escalate_covers_unsubmitted_items(self, tmp_path):
        """escalate：未提交（acceptance None）的悬挂项也合法上交（§5.7
        (None, ESCALATED)），不炸、不悬挂。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, self._stuck_script("escalate"))
        out = orch.run_task("含悬挂项的上交")
        assert "deps-stuck" in out["actions"]
        assert out["final"] == "escalated"
        for w in cp.proj.work_items.values():
            assert w.acceptance == AcceptanceState.ESCALATED
        assert cp.proj.open_worker_slots_used == 0

    def test_lead_degenerate_terminates_stuck_items(self, tmp_path):
        """degenerate（§7 可逆收缩）：悬挂项 terminate 收口、结论落盘、
        槽位归还；终态项（A 已上交）不动。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, self._stuck_script("degenerate"))
        out = orch.run_task("依赖卡死任务")
        assert "deps-stuck" in out["actions"]
        assert out["final"] == "degenerated"
        derives = [w for w in cp.proj.work_items.values() if w.kind.value == "derive"]
        a = next(w for w in derives if not w.deps)
        b = next(w for w in derives if w.deps)
        assert a.acceptance == AcceptanceState.ESCALATED      # 已是终态，不动
        assert b.acceptance == AcceptanceState.TERMINATED     # 悬挂项收口
        assert b.outcome == WorkItemOutcome.MANUAL_STOPPED    # §4 六值词汇
        root = cp.proj.work_items[cp._root_item_id()]
        assert root.acceptance == AcceptanceState.TERMINATED
        assert cp.proj.open_worker_slots_used == 0
        assert cp.proj.active_points == 0                     # lease 全归还


class TestAttributionFallback:
    """§8：打回归因缺省/非法 → 回退 DESCRIPTION（与验收保守默认对齐；
    capability 是最贵路径，不作兜底），回退记审计事件。"""

    def _run_reject(self, tmp_path, verdict):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "坏交付"},
            {"text": json.dumps(verdict)},
            {"text": "修述后交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("归因兜底任务")
        return cp, out

    def test_missing_attribution_falls_back_to_description(self, tmp_path):
        cp, out = self._run_reject(
            tmp_path, {"verdict": "reject", "verdict_reason": "不行"})
        assert out["items"][0]["outcome"] == "accepted"
        rejected = next(e for e in cp.store.read_all()
                        if e.kind == "work_item_rejected")
        assert rejected.payload["attribution"] == "description"
        audit = [e for e in cp.store.read_all()
                 if e.kind == "watchdog_suggested"
                 and e.payload.get("kind") == "attribution-fallback"]
        assert audit                                       # 回退可审计

    def test_invalid_attribution_falls_back_to_description(self, tmp_path):
        cp, out = self._run_reject(
            tmp_path, {"verdict": "reject", "verdict_reason": "不行",
                       "attribution": "bogus"})
        rejected = next(e for e in cp.store.read_all()
                        if e.kind == "work_item_rejected")
        assert rejected.payload["attribution"] == "description"
        audit = [e for e in cp.store.read_all()
                 if e.kind == "watchdog_suggested"
                 and e.payload.get("kind") == "attribution-fallback"]
        assert audit


class TestDagDepsScheduling:
    """§7 裂变 = DAG 协调：deps 边 + deferred 晋级调度。"""

    def test_second_subtask_waits_for_first_accept(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE, subtasks=[
                "part A", {"title": "part B", "deps": [0]}])},
            {"text": "A 完成"},
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "ok"})},
            {"text": "B 完成"},
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "ok"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("有依赖的两步任务")
        assert out["final"] == "accepted-by-lead"
        assert len(out["items"]) == 2
        assert all(i["outcome"] == "accepted" for i in out["items"])
        events = cp.store.read_all()
        # 依赖边已接线（执行层首次构造 DAG 边）
        deps = [e for e in events if e.kind == "work_item_dependency_added"]
        assert len(deps) == 1
        # A（无依赖）与 B（依赖 A）按 item 定位
        derive_items = [w for w in cp.proj.work_items.values() if w.kind.value == "derive"]
        a = next(w for w in derive_items if not w.deps)
        b = next(w for w in derive_items if w.deps)
        assert deps[0].payload["before"] == a.item_id
        assert deps[0].payload["after"] == b.item_id
        # B 的 provisioning 晚于 A 的 accepted（依赖门禁不触发、按序调度）
        acc_a = next(e for e in events if e.kind == "work_item_accepted"
                     and e.payload["item_id"] == a.item_id)
        prov_b = [e for e in events if e.kind == "node_provisioning"
                  and e.payload["item_id"] == b.item_id]
        assert prov_b and prov_b[0].seq > acc_a.seq

    def test_independent_subtasks_still_single_parallel_round(self, tmp_path):
        """无 deps 时行为与现状一致：单轮全并行、不产生依赖事件。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE,
                               subtasks=["s1", "s2", "s3"])},
            {"text": "out-1"},
            {"text": "out-2"},
            {"text": "out-3"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("独立三任务")
        assert len(out["items"]) == 3
        assert all(i["outcome"] == "accepted" for i in out["items"])
        kinds = [e.kind for e in cp.store.read_all()]
        assert "work_item_dependency_added" not in kinds
        assert "deps-stuck" not in out["actions"]


class TestSplitObservation:
    """§4/§7：分裂结构在观测中标注；协助者奖励信号由主执行者代写。"""

    def test_split_primary_and_assistant_records(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("split", route=B_ROUTE)},
            {"text": "协助者产出：后半部分"},
            {"text": "主执行者整合交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("可二分任务")
        assert out["items"] and out["items"][0]["outcome"] == "accepted"
        # §7 同层拓宽：split 不建 DERIVE item——全树只有 root item，主执行者
        # 与协助者都挂在被分裂的 root item 上
        assert len(cp.proj.work_items) == 1
        assert out["items"][0]["item"] == cp._root_item_id()
        events = cp.store.read_all()
        sink = ObservationSink(events)
        by_topo = {r.topology: r for r in sink.delegation_records()}
        assert "split-primary" in by_topo and "split-assistant" in by_topo
        chan = next(e.payload for e in events if e.kind == "peer_channel_opened")
        primary_rec = by_topo["split-primary"]
        assert primary_rec.node_id == chan["primary_node"]
        assistant_rec = by_topo["split-assistant"]
        assert assistant_rec.node_id == chan["assistant_node"]
        assert assistant_rec.lead_node_id == chan["primary_node"]   # 主代写（§7）
        assert assistant_rec.outcome == "assistant-accepted"
        assert assistant_rec.accepted_by == {"node": chan["primary_node"],
                                             "proxy": True}
        # §9.3：协助者同样走完两阶段（node_activated），超时时钟起算
        assert any(e.kind == "node_activated"
                   and e.payload["node_id"] == chan["assistant_node"] for e in events)
        # §7/§9.5：协助者回报物理上拼进主执行者包——主实际收到协助产出文本
        # （调用序：Lead 决策 → 协助者 → 主执行者 → 验收 → Lead 收束）
        primary_call = orch.provider.calls[2]
        assert "协助者产出：后半部分" in primary_call["messages"][-1]["content"]

    def test_split_assistant_rejected_signal(self, tmp_path):
        """主 work item 未 accepted（contradiction 上交）→ 协助者记
        assistant-rejected（§7 奖励信号随主结案，主未过 = 协助者未过）。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("split", route=B_ROUTE)},
            {"text": "协助者产出"},
            {"text": "主执行者交付"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "任务自相矛盾",
                                 "attribution": "contradiction"})},
            {"text": _decision("escalate")},
        ])
        out = orch.run_task("矛盾任务")
        assert out["items"][0]["outcome"] == "escalated"
        assert len(cp.proj.work_items) == 1        # split 不建 DERIVE item（§7）
        sink = ObservationSink(cp.store.read_all())
        by_topo = {r.topology: r for r in sink.delegation_records()}
        assistant_rec = by_topo["split-assistant"]
        assert assistant_rec.outcome == "assistant-rejected"
        assert assistant_rec.accepted_by is None


class TestReweightWait:
    """§9.3 reweight-wait 抑制窗口：capability 升级重试时点数差额不足
    （POINTS_EXCEEDED）→ 编排器标记节点等待（tick 豁免），不消耗重试预算、
    不误走 budget-exhausted 上交。"""

    def test_upgrade_points_shortfall_marks_wait(self, tmp_path):
        cat = ModelCatalog()
        cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
        cat.register(ModelFacts("p", "a-model", Level.A, aa_dimensional={"coding": 8.5},
                                context_window=512_000))   # weight 8（升级目标）
        cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5},
                                context_window=64_000))    # weight 1（首试）
        # root lead(1) + worker(1) = 2 满；升级 reweight 1→8 → 1+8=9 > 2 差额不足
        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                                 max_active_node_points=2),
                          store_path=tmp_path / "events.jsonl", catalog=cat)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route={"provider": "p", "model": "b-model"})},
            {"text": "坏交付"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "能力不足",
                                 "attribution": "capability"})},
            {"text": _decision("escalate")},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("能力超限任务")
        assert out["final"] == "escalated"
        item_id = next(i for i in cp.proj.work_items
                       if i != cp._root_item_id())
        item = cp.proj.work_items[item_id]
        assert item.attempt == 1                     # 等待期间不消耗重试预算
        events = cp.store.read_all()
        # 进入等待的标记事件在（结案 drain 投影侧收敛，不出退出事件）
        assert any(e.kind == "node_reweight_wait" and e.payload["waiting"]
                   for e in events)
        # 委派记录终局 = reweight-wait（非 budget-exhausted 上交）
        sink = ObservationSink(events)
        rec = next(r for r in sink.delegation_records() if r.item_id == item_id)
        assert rec.outcome == "reweight-wait"
        assert not [e for e in events if e.kind == "work_item_escalated"
                    and "budget-exhausted" in str(e.payload.get("reason"))]
        # Lead 决策 escalate 收口后节点 drain，等待标记随之清除
        assert not any(n.reweight_wait for n in cp.proj.nodes.values())


class TestCmCostAccounting:
    """§5.5/§7：CM 成本记触发方（ctx job）账下，economics cm_tokens 聚合。"""

    def test_cm_tokens_recorded_under_ctx_job(self, tmp_path):
        from dpswarm.providers.base import ProviderResult, Usage

        cp = make_cp(tmp_path)
        memory = MemoryService(sink=cp._record)
        big = memory.add_candidate("稳定知识：" + "关键事实条目内容。" * 3000,
                                   "root", ["src-1"])   # est > 8000 触发压缩
        memory.promote(big.memory_id)
        assembler = ContextAssembler(memory)
        cm = ContextManagerLLM(
            lambda route, messages: ProviderResult(
                text="## 目标\n零损压缩摘要", stop_reason=StopReason.COMPLETED,
                usage=Usage(input_tokens=500, output_tokens=100, cost_usd=0.02)),
            ModelRoute("p", "b-model", level=Level.B))
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "worker 交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ], assembler=assembler, memory=memory, context_manager=cm)
        out = orch.run_task("上下文重的任务")
        assert out["items"][0]["outcome"] == "accepted"

        sink = ObservationSink(cp.store.read_all())
        ledger = sink.token_ledger()
        ctx_nodes = [n for n in ledger if n.startswith("ctx-job:")]
        assert ctx_nodes, "CM 成本应记到 ctx-job:<item_id> 账户"
        assert ledger[ctx_nodes[0]]["input"] == 500
        # 角色规则：ctx-job:* → context-manager（§7），cm_tokens 真正聚合
        assert sink.node_roles()[ctx_nodes[0]] == "context-manager"
        econ = sink.economics_summary()
        assert econ["cm_tokens"] == 600          # 500 + 100，不再恒 0
        # 不混入 lead_tokens（此前误记 root lead 节点）
        assert econ["lead_tokens"] == 0
        lead_row = ledger[cp.root_lead_node]
        assert sum(lead_row[k] for k in ("input", "output", "cache_read", "cache_write")) == 0


class TestLeadReviewAccounting:
    """§4/§6：验收调用（_ask_lead_review）token 全账入事件流；cache 分账
    计入 lead_tokens——orchestrator 内账与 observation 聚合两口径一致。"""

    def test_review_call_tokens_and_cache_counted(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE),
             "usage": {"input_tokens": 100, "output_tokens": 10}},
            {"text": "worker 交付",
             "usage": {"input_tokens": 500, "output_tokens": 50}},
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "ok"}),
             "usage": {"input_tokens": 60, "output_tokens": 6,
                       "cache_read_tokens": 700, "cache_write_tokens": 30}},
            {"text": _decision("accept"),
             "usage": {"input_tokens": 40, "output_tokens": 4}},
        ])
        out = orch.run_task("验收记账任务")
        assert out["items"][0]["outcome"] == "accepted"
        lead = cp.root_lead_node
        tok = [e for e in cp.store.read_all()
               if e.kind == "token_usage_recorded" and e.payload["node_id"] == lead]
        # Lead 调用三笔全入账：决策 ×2 + 验收 ×1（验收此前漏记 token 事件）
        assert len(tok) == 3
        review_ev = next(e for e in tok if e.payload.get("cache_read") == 700)
        assert review_ev.payload["cache_write"] == 30
        # cache 计入 lead_tokens：110 + 796 + 44 = 950（此前丢 cache 分账）
        assert orch.lead_tokens == 950
        econ = ObservationSink(cp.store.read_all()).economics_summary()
        assert econ["lead_tokens"] == 950                # 两口径一致（§6）


class TestEconomicsSavings:
    """§6 盈亏线节省侧接线：run_task 末尾 record_economics 的 estimated_savings
    = worker 节点 token 合计（"若由 Lead 直做"的代理估算，同构前缀近似），
    不再恒 None。"""

    def test_estimated_savings_wired_from_worker_tokens(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "worker 交付",
             "usage": {"input_tokens": 500, "output_tokens": 50}},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("可委派任务")
        assert out["final"] == "accepted-by-lead"
        econ = ObservationSink(cp.store.read_all()).economics_summary()
        assert econ["worker_tokens"] == 550
        assert econ["est_saved"] is not None
        assert econ["est_saved"] == 550              # V1 代理口径 = worker token 合计


class TestSubtasksOverLimit:
    """§2 硬准入：subtasks 超限结构化拒绝，不静默截断。"""

    def test_four_subtasks_rejected_structurally(self, tmp_path):
        cp = make_cp(tmp_path)   # 默认 max_team_workers = 3
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE,
                               subtasks=["t1", "t2", "t3", "t4"])},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("超限任务")
        assert "admission-rejected:SUBTASKS_OVER_LIMIT(max=3)" in out["actions"]
        # 硬准入失败：一个 work item 都不建（只剩 root）
        created = [e for e in cp.store.read_all() if e.kind == "work_item_created"]
        assert len(created) == 1 and created[0].payload["kind"] == "root"
        assert out["final"] == "accepted-by-lead"      # 由 Lead 重选后收束
