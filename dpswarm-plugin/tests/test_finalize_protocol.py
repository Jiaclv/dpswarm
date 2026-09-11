"""债①-b 评审八条反例（任务级交付协议）逐条锚定：

① 一条 mandatory 未被集成验收提及（unknown）→ 阻止 success（不等于 fail）；
② 局部全过但集成接口不兼容（fail）→ 不得 success；
③ 探索分支失败（item escalated）但全部 mandatory 验证过 → success
   （不要求全树 accepted）；
④ 有 accepted 中间产物但无验证子集 → 不自动 partial（落 failed）；
⑤ 预算耗尽但有验证通过的可交付子集 → partial + stop_reason=budget_exhausted；
⑥ 关键缺陷在 2000 字符之后 → 验收材料必须覆盖（⑤ 保真）；
⑦ 验收后产物版本变化（验 A 封 B）→ 旧验收不批准新交付（漂移降级 partial）；
⑧ finalize 幂等：重复 finalize 返回同一逻辑结果，不重复封存。
"""
from __future__ import annotations

import json

from dpswarm.control import ControlPlane
from dpswarm.orchestrator import Orchestrator
from dpswarm.providers import MockProvider
from dpswarm.types import (
    DelegationKind,
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
)

B_ROUTE = {"provider": "p", "model": "b-model"}
TASK2 = "两步任务：\n1. 实现交付甲并自测\n2. 实现交付乙并自测"


def catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


def make_cp(tmp_path):
    return ControlPlane(spec=RootExecutionSpec(max_open_work_items=6,
                                               max_active_node_points=8),
                        store_path=tmp_path / "events.jsonl", catalog=catalog())


def make_orch(cp, script):
    return Orchestrator(cp, MockProvider(script=script), store_dir=None,
                        lead_route=ModelRoute("p", "s-model", level=Level.S))


def _decision(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


def _two_subtask_decision():
    return _decision("fission", route=B_ROUTE, subtasks=[
        {"title": "交付甲", "covers": [0]}, {"title": "交付乙", "covers": [1]}])


class TestAdversarial12:
    """① unknown 阻止 success；② 接口不兼容 fail 不得 success。"""

    def test_unmentioned_mandatory_blocks_success(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _two_subtask_decision()},
            {"text": "甲交付"}, {"text": "乙交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "accept"})},
            # 集成验收只提了 req-0（req-1 未提及 → unknown → 阻止 success）
            {"text": json.dumps({"integrated": "pass",
                                 "requirements": [{"id": "req-0", "status": "pass"}],
                                 "gaps": []})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task(TASK2)
        assert out["result"] == "partial"           # unknown 阻止 success
        assert out["result"] != "success"
        ev = next(e for e in cp.store.read_all() if e.kind == "integration_review")
        assert ev.payload["requirements"]["req-1"] == "unknown"

    def test_interface_incompatible_not_success(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _two_subtask_decision()},
            {"text": "甲交付"}, {"text": "乙交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({
                "integrated": "fail",
                "requirements": [{"id": "req-0", "status": "pass"},
                                 {"id": "req-1", "status": "fail",
                                  "note": "接口签名与甲交付不兼容"}],
                "gaps": ["乙与甲接口不兼容"]})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task(TASK2)
        assert out["result"] == "partial"           # 接口不兼容 → 不 success
        assert out["stop_reason"] == "completed"


class TestAdversarial345:
    """③ 探索分支失败不否决 success；④ 无验证子集不自动 partial；
    ⑤ 预算耗尽 + 验证子集 → partial + budget_exhausted。"""

    def test_exploratory_branch_failure_still_success(self, tmp_path):
        cp = make_cp(tmp_path)
        # deps 链串行化（MockProvider 顺序脚本在并行 worker 下不确定）
        orch = make_orch(cp, [
            {"text": _decision("fission", route=B_ROUTE, subtasks=[
                {"title": "交付甲", "covers": [0]},
                {"title": "交付乙", "covers": [1], "deps": [0]},
                {"title": "探索分支丙", "covers": [0], "deps": [1]}])},
            {"text": "甲交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": "乙交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": "丙交付（探路的）"},
            # 丙：探索分支被否（任务矛盾 → 直接上交，不进重试）
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "探路分支废弃",
                                 "attribution": "contradiction"})},
            # 集成验收：全部 mandatory 验证过（探索性失败不否决 success）
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ])
        out = orch.run_task(TASK2)
        assert out["result"] == "success"
        terminals = {i.item_id: i.acceptance.value
                     for i in cp.proj.work_items.values()}
        assert "escalated" in terminals.values()      # 丙确已上交（非全树 accepted）
        fin = next(e for e in cp.store.read_all() if e.kind == "run_finalized")
        assert fin.payload["result"] == "success"
        assert fin.payload["accepted"] == 2 and fin.payload["total"] == 3

    def test_budget_exhausted_with_verified_subset_is_partial(self, tmp_path):
        """⑤：B 三次打回耗尽重试预算（budget-exhausted 上交），A 验证过 →
        partial + stop_reason=budget_exhausted。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _two_subtask_decision()},
            {"text": "甲交付"}, {"text": "乙坏交付-1"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "x",
                                 "attribution": "description"})},
            {"text": "乙坏交付-2"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "x",
                                 "attribution": "description"})},
            {"text": "乙坏交付-3"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "x",
                                 "attribution": "description"})},   # 预算耗尽上交
            {"text": json.dumps({
                "integrated": "fail",
                "requirements": [{"id": "req-0", "status": "pass"},
                                 {"id": "req-1", "status": "fail"}],
                "gaps": ["req-1 预算耗尽"]})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task(TASK2)
        assert out["result"] == "partial"
        assert out["stop_reason"] == "budget_exhausted"
        fin = next(e for e in cp.store.read_all() if e.kind == "run_finalized")
        assert fin.payload["stop_reason"] == "budget_exhausted"


class TestAdversarial678:
    """⑥ 缺陷在 2000 之后也要可见；⑦ 验 A 封 B 拒绝；⑧ finalize 幂等。"""

    def _accepted_item(self, cp, text="交付文本"):
        root = cp._root_item_id()
        item = cp.create_work_item(DelegationKind.DERIVE, parent_item=root,
                                   deps=[], team="root")
        route = ModelRoute("p", "b-model", level=Level.B)
        node = cp.begin_node(item.item_id, route,
                             package_ref="pkg://t/abc", package_hash="abc")
        cp.confirm_node(node.node_id)
        cp.submit(item.item_id, node.node_id, text)
        pkg_id = cp.proj.work_items[item.item_id].submission_package_id
        cp.accept_submission(item.item_id, package_id=pkg_id,
                             accepted_by={"node": "test-lead"})
        return item, pkg_id

    def test_version_drift_after_acceptance_refused(self, tmp_path):
        """⑦：验收后产物版本变化（submission_package 被换）→ 封存核对拒绝
        按 success 封口，降级 partial + candidate_version_mismatch 审计。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [{"text": json.dumps({"integrated": "pass",
                                                   "gaps": []})}])
        orch._init_task_registry(TASK2)
        item, pkg_id = self._accepted_item(cp)
        # 验 A 封 B：验收版本 ≠ 当前交付版本
        cp.proj.work_items[item.item_id].submission_package_id = "pkg-tampered"
        outcome = {"task": TASK2, "items": [], "actions": []}
        assert orch._try_finalize(outcome, TASK2, from_lead=True)
        assert outcome["result"] == "partial"
        assert outcome["candidate"]["drift"] == [item.item_id]
        assert pkg_id not in outcome["candidate"]["selected_submission_package_ids"]
        assert any(e.kind == "candidate_version_mismatch"
                   for e in cp.store.read_all())
        assert not [e for e in cp.store.read_all()
                    if e.kind == "run_finalized" and e.payload["result"] == "success"]

    def test_finalize_idempotent_same_logical_result(self, tmp_path):
        """⑧：finalize 成功后重复调用 → 同一逻辑结果，不重复封存/不改 outcome。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [{"text": json.dumps({"integrated": "pass",
                                                   "gaps": []})}])
        orch._init_task_registry(TASK2)
        self._accepted_item(cp)
        o1 = {"task": TASK2, "items": [], "actions": []}
        assert orch._try_finalize(o1, TASK2, from_lead=True)
        assert o1["result"] == "success"
        digest1 = o1["candidate"]["bundle_digest"]
        # 模拟"响应丢失重复请求"：全新 outcome 再调一次
        o2 = {"task": TASK2, "items": [], "actions": []}
        assert orch._try_finalize(o2, TASK2, from_lead=True)
        assert o2["result"] == "success"
        finals = [e for e in cp.store.read_all() if e.kind == "run_finalized"]
        assert len(finals) == 1                        # 不重复封存/落账
        assert o2["candidate"]["bundle_digest"] == digest1   # 幂等缓存含候选

    def test_finalize_idempotent_across_process_restart(self, tmp_path):
        """⑧ 跨进程：同一事件日志上重建控制面重跑 run_task → 回填已落账的
        逻辑结果直接收口——不重复集成验收/候选装配/run_finalized/经济性落账。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _two_subtask_decision()},
            {"text": "甲交付"}, {"text": "乙交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ])
        out1 = orch.run_task(TASK2)
        assert out1["result"] == "success"
        cp.store.close()   # 释放写者锁，模拟进程退出
        # 进程重启：同 store_path 重建（控制面恢复是既有能力），全新 Orchestrator
        cp2 = ControlPlane(spec=RootExecutionSpec(max_open_work_items=6,
                                                  max_active_node_points=8),
                           store_path=tmp_path / "events.jsonl", catalog=catalog())
        orch2 = Orchestrator(cp2, MockProvider(script=[]), store_dir=None,
                             lead_route=ModelRoute("p", "s-model", level=Level.S))
        out2 = orch2.run_task(TASK2)
        assert out2["result"] == "success"             # 同一逻辑结果
        assert out2["phase"] == "finalized"
        assert "already-finalized" in out2["actions"]
        assert len(orch2.provider.calls) == 0          # 不重复消耗任何 LLM 调用
        events = cp2.store.read_all()
        assert len([e for e in events if e.kind == "run_finalized"]) == 1
        assert len([e for e in events if e.kind == "candidate_assembled"]) == 1
        econ = [e for e in events if e.kind == "delegation_economics_recorded"]
        assert len(econ) == 1                          # 经济性不重复落账
        cand = out2["candidate"]                       # 候选标识从账本水合
        assert cand["bundle_digest"] == out1["candidate"]["bundle_digest"]
        cp2.store.close()

    def test_resume_guard_keyed_by_contract_version(self, tmp_path):
        """⑧ 守卫的合同键控：同一 store 上的**不同任务**不被旧终局劫持——
        正常跑自己的决策循环并落自己的 run_finalized。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ])
        out1 = orch.run_task(TASK2)
        assert out1["result"] == "success"
        cp.store.close()
        cp2 = ControlPlane(spec=RootExecutionSpec(max_open_work_items=6,
                                                  max_active_node_points=8),
                           store_path=tmp_path / "events.jsonl", catalog=catalog())
        # 不同任务文本 → contract_version 不匹配 → 不命中幂等回填
        orch2 = Orchestrator(cp2, MockProvider(script=[
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out2 = orch2.run_task("另一个任务：\n1. 实现交付丙并自测\n2. 实现交付丁并自测")
        assert "already-finalized" not in out2["actions"]
        assert len(orch2.provider.calls) >= 1          # 真的跑了（集成验收）
        finals = [e for e in cp2.store.read_all() if e.kind == "run_finalized"]
        assert len(finals) == 2                        # 各任务各落各的终局
        versions = {e.payload.get("contract_version") for e in finals}
        assert len(versions) == 2                      # 两个 contract 各自在账
        cp2.store.close()


class TestStopReasonPassthrough:
    """② 停止原因透传：耗尽型终局不得泛化记 completed（与 _finalize_run
    回填同口径）。"""

    def test_quota_autoclose_stop_reason_quota_exhausted(self, tmp_path):
        """worker QUOTA 有弃置 → 全终态无 accepted → autoclose：final 与
        stop_reason 都透传 quota（此前 stop_reason 硬编码 completed）。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"raise": "quota"},                        # worker 调用 QUOTA
        ])
        out = orch.run_task("quota 任务")
        assert out["final"] == "quota-exhausted"
        assert out["result"] == "failed"
        assert out["stop_reason"] == "quota_exhausted"
        fin = next(e for e in cp.store.read_all() if e.kind == "run_finalized")
        assert fin.payload["stop_reason"] == "quota_exhausted"
        assert fin.payload["final"] == "quota-exhausted"

    def test_budget_autoclose_stop_reason_budget_exhausted(self, tmp_path):
        """重试预算耗尽上交（零 accepted）→ autoclose：stop_reason 并载
        budget_exhausted（⑤ 停止原因不得被 settled-no-acceptance 吞掉）。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "坏1"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "x",
                                 "attribution": "description"})},
            {"text": "坏2"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "x",
                                 "attribution": "description"})},
            {"text": "坏3"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "x",
                                 "attribution": "description"})},   # 预算耗尽上交
        ])
        out = orch.run_task("预算耗尽任务")
        assert out["result"] == "failed"
        assert out["stop_reason"] == "budget_exhausted"
        fin = next(e for e in cp.store.read_all() if e.kind == "run_finalized")
        assert fin.payload["stop_reason"] == "budget_exhausted"


class TestIntegrationReviewAuditSymmetry:
    """审计对称：success 的逐需求验证证据与 fail 同样落 integration_review。"""

    def test_success_path_records_per_requirement_evidence(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _two_subtask_decision()},
            {"text": "甲交付"}, {"text": "乙交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({
                "integrated": "pass",
                "requirements": [{"id": "req-0", "status": "pass"},
                                 {"id": "req-1", "status": "pass"}],
                "gaps": []})},
        ])
        out = orch.run_task(TASK2)
        assert out["result"] == "success"
        ev = next(e for e in cp.store.read_all() if e.kind == "integration_review")
        assert ev.payload["verdict"] == "pass"
        assert ev.payload["requirements"] == {"req-0": "pass", "req-1": "pass"}

    def test_long_submission_flaw_beyond_2000_visible(self, tmp_path):
        """⑥：关键缺陷在 2000 字符之后 → 验收材料必须覆盖（取消 2000 硬截断）。"""
        cp = make_cp(tmp_path)
        marker = "DEFECT-尾部关键缺陷-"
        long_text = "前置正常内容。\n" + "填充。" * 2000 + marker + "此处崩"
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": long_text},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "尾部缺陷",
                                 "attribution": "description"})},
            {"text": "修好的交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ])
        out = orch.run_task("长交付任务")
        assert out["items"][0]["outcome"] == "accepted"
        review_prompt = orch.provider.calls[2]["messages"][-1]["content"]
        assert marker in review_prompt                 # 2000 之后的缺陷可见
        assert len(long_text) > 2000                   # 确实在旧截断点之后


class TestReviewMaterialWindow:
    """⑥ 补充：超 12000 窗口 → L1 逐字段 + ref + unknown 明示。"""

    def test_over_window_material_uses_l1_and_unknown_note(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [])
        big = ("说明文。\n" + " filler" * 3000 + "\n```python\ndef f():\n"
               + "    x = 1  # " + "y" * 1500 + "\n```\n尾部。")
        material = orch._review_material("wi-nonexist", big)
        assert "不得臆断" in material and "unknown" in material
        assert "def f():" in material                  # L1 逐字段提取在
        assert len(material) < len(big)                # 确实走了窗口裁剪
