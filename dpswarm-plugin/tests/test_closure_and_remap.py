"""收束硬规则与同构归因改道回归（r4 实证驱动，§7 终局纪律 / §8 归因处置）：

- 债①：非 root item 全部终态 → 不再问 Lead，直接收束（accepted-by-lead
  或 settled-no-acceptance 上交，不虚报验收）；连续扩张 + 已有 accepted
  时决策 prompt 注入收束警示。
- 债③：同构目录 capability 归因 → 改道修述重试（attribution_remapped
  审计事件），不再 upgrade-exceeds-lead-level 直接上交；attempt 预算照耗。
"""
from __future__ import annotations

import json

from dpswarm.control import ControlPlane
from dpswarm.orchestrator import Orchestrator
from dpswarm.providers import MockProvider
from dpswarm.types import (
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
)

B_ROUTE = {"provider": "p", "model": "b-model"}


def catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


def make_cp(tmp_path, cat=None):
    return ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                               max_active_node_points=8),
                        store_path=tmp_path / "events.jsonl",
                        catalog=cat or catalog())


def _decision(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


class TestAutoclose:
    """债①：全树终态即收束，不再消耗 Lead 决策调用。"""

    def test_no_lead_call_after_all_accepted(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},  # 集成验收（债①）
            # 无第五条：集成过后直接收束，Lead 决策不再被调用——
            # 若硬规则失效，MockProvider 会重复末条，调用数暴露。
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("收束任务")
        assert out["final"] == "accepted-by-lead"
        assert out["result"] == "success"
        assert len(orch.provider.calls) == 4          # 决策+worker+验收+集成验收
        assert "settled-autoclose" in out["actions"]
        fin = next(e for e in cp.store.read_all() if e.kind == "run_finalized")
        assert fin.payload["result"] == "success" and fin.payload["integration"] == "pass"

    def test_all_terminal_without_acceptance_escalates(self, tmp_path):
        """全部终态但无一 accepted：不虚报验收，root 走 settled-no-acceptance
        上交（硬规则同样兜住，不再问 Lead）。"""
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "坏交付"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "任务矛盾",
                                 "attribution": "contradiction"})},   # → 直接上交
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("全灭任务")
        assert out["final"] == "escalated"
        assert out["result"] == "failed"            # 债①：无 accepted → failed
        assert "settled-autoclose" in out["actions"]
        assert len(orch.provider.calls) == 3
        root = cp.proj.work_items[cp._root_item_id()]
        assert root.acceptance.value == "escalated"

    def test_closure_warning_helper(self, tmp_path):
        """收束警示判定：连续 ≥2 轮扩张且有 accepted → 警示文本；否则 None。"""
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付"},
            {"text": json.dumps({"verdict": "accept"})},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        orch.run_task("警示判定任务")   # 跑完留一个 accepted item
        assert orch._closure_warning(["derive", "fission"]) is not None
        assert "收束警示" in orch._closure_warning(["derive", "fission"])
        assert orch._closure_warning(["derive", "accept"]) is None
        assert orch._closure_warning(["derive"]) is None


class TestIntegrationGate:
    """债① 集成验收门：fail → Lead 续决策 → 再次收束落 partial；prompt 内容。"""

    def test_integration_fail_then_partial_close(self, tmp_path):
        """集成 fail → Lead 续决策 → 再次收束：有验证过的 mandatory 子集 →
        partial（④ 语义）；逐需求 verdict 落 integration_review 审计。"""
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付甲"},
            {"text": json.dumps({"verdict": "accept"})},
            # 集成验收：req-0 pass、req-1 fail（逐需求 verdict）
            {"text": json.dumps({
                "integrated": "fail",
                "requirements": [{"id": "req-0", "status": "pass"},
                                 {"id": "req-1", "status": "fail",
                                  "note": "无任何交付支撑"}],
                "gaps": ["req-1 无交付支撑"]})},
            # Lead 续决策（gaps 注入 prompt）后直接收束
            {"text": _decision("accept")},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("集成门任务\n1. 做交付甲并自测\n2. 做交付乙并自测")
        assert out["final"] == "accepted-by-lead"
        assert out["result"] == "partial"          # req-0 验证过 → partial
        assert out["phase"] == "finalized" and out["stop_reason"] == "completed"
        assert "integration-failed" in out["actions"]
        ev = next(e for e in cp.store.read_all() if e.kind == "integration_review")
        assert ev.payload["verdict"] == "fail"
        assert ev.payload["requirements"] == {"req-0": "pass", "req-1": "fail"}
        fin = next(e for e in cp.store.read_all() if e.kind == "run_finalized")
        assert fin.payload["result"] == "partial"
        assert fin.payload["integration"] == "fail-closed-by-lead"

    def test_integration_all_fail_is_failed_not_partial(self, tmp_path):
        """④：集成逐需求全 fail（无验证子集）→ 收束落 failed，不自动 partial。"""
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付甲"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({
                "integrated": "fail",
                "requirements": [{"id": "req-0", "status": "fail"},
                                 {"id": "req-1", "status": "fail"}],
                "gaps": []})},
            {"text": _decision("accept")},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("集成门任务\n1. 做交付甲并自测\n2. 做交付乙并自测")
        assert out["result"] == "failed"
        assert out["final"] == "accepted-by-lead"    # final 路由值兼容不动

    def test_integration_prompt_contents(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付内容 UNIQ-德尔塔"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("覆盖任务\n1. 输出 UNIQ-德尔塔\n2. 格式合规")
        assert out["result"] == "success"
        prompt = orch.provider.calls[3]["messages"][-1]["content"]
        assert "需求清单" in prompt and "输出 UNIQ-德尔塔" in prompt  # 要求编号在
        assert "已验收交付" in prompt and "UNIQ-德尔塔" in prompt      # 交付在


class TestCoverageMapping:
    """债① 需求覆盖映射：covers 并集缺漏 → coverage-rejected 不建队，Lead 重选。"""

    TASK = "三步任务：\n1. 实现函数 alpha 并自测\n2. 实现函数 beta 并自测\n3. 写说明文档（含用法示例）"

    def test_missing_covers_rejected_then_fixed(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            # 首轮：subtasks 不带 covers → 并集为空 → 全覆盖缺口，不建队
            {"text": _decision("fission", route=B_ROUTE,
                               subtasks=["做 alpha 和 beta", "写文档"])},
            # 次轮：covers 覆盖全部要求 [0,1]+[2]
            {"text": _decision("fission", route=B_ROUTE, subtasks=[
                {"title": "做 alpha 和 beta", "covers": [0, 1]},
                {"title": "写文档", "covers": [2]}])},
            {"text": "交付1"},
            {"text": "交付2"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task(self.TASK)
        assert out["result"] == "success"
        gap = next(e for e in cp.store.read_all() if e.kind == "coverage_gap")
        assert gap.payload["missing"] == [0, 1, 2] and gap.payload["total"] == 3
        assert "coverage-rejected:missing=[0, 1, 2]" in out["actions"]
        created = [e for e in cp.store.read_all() if e.kind == "work_item_created"]
        # 首轮不建队：只有 root + 次轮两个 fission item
        assert len(created) == 3

    def test_no_requirements_no_gate(self, tmp_path):
        """任务无可提取要求行 → 覆盖门不生效（兼容无结构任务书）。"""
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("一句话任务，没有编号也没有 bullet")
        assert out["result"] == "success"
        assert not [e for e in cp.store.read_all() if e.kind == "coverage_gap"]

    def test_indented_data_lines_not_requirements(self):
        """r6 实证回归：缩进的编号数据行（内联材料清单）不算要求——
        否则 semantic 任务 12 条反馈会把覆盖门卡成死循环。"""
        from dpswarm.orchestrator import extract_requirements
        task = ("两阶段任务：\n- 子任务 A：归纳下列反馈\n- 子任务 B：写简报\n"
                "  1. 更新后闪退频繁\n  2. 夜间模式对比度太低\n  3. 客服响应慢")
        reqs = extract_requirements(task)
        assert len(reqs) == 2                      # 只有两个顶格 bullet
        assert any("子任务 A" in r for r in reqs)

    def test_repair_round_exempt_from_full_coverage(self, tmp_path):
        """r6 实证回归：已有 accepted 交付的修复轮，covers 不覆盖全部要求也
        放行（首轮全覆盖门槛只管初始分解）。

        流程注记：旧 B 三次打回耗尽上交后，autoclose 先于 Lead 修复决策触发
        （全部终态）→ 集成验收 fail（req-1 无支撑）→ Lead 续决策时 fission
        修复 B（covers=[1]，豁免全覆盖）→ 重验过 → success（③ 探索/失败
        分支不否决）。"""
        cp = make_cp(tmp_path)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("fission", route=B_ROUTE, subtasks=[
                {"title": "做 alpha", "covers": [0]},
                {"title": "做 beta", "covers": [1]}])},
            {"text": "alpha 交付"},
            {"text": "beta 坏交付"},
            {"text": json.dumps({"verdict": "accept"})},   # alpha 过
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "不达标",
                                 "attribution": "description"})},
            {"text": "beta 仍坏"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "仍不达标",
                                 "attribution": "description"})},
            {"text": "beta 再坏"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "还是不行",
                                 "attribution": "description"})},   # beta 三次败 → 上交
            # autoclose 先触发：集成验收 fail（req-1 无验证支撑）
            {"text": json.dumps({
                "integrated": "fail",
                "requirements": [{"id": "req-0", "status": "pass"},
                                 {"id": "req-1", "status": "fail"}],
                "gaps": ["req-1 无验证支撑"]})},
            # Lead 续决策：fission 修复轮（covers=[1] 不全覆盖，应豁免放行）
            {"text": _decision("fission", route=B_ROUTE, subtasks=[
                {"title": "重做 beta", "covers": [1]}])},
            {"text": "beta 好交付"},
            {"text": json.dumps({"verdict": "accept"})},
            # 重验：全部 mandatory 过
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ]), store_dir=None, lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("两步任务：\n1. 实现函数 alpha 并自测\n2. 实现函数 beta 并自测")
        # 修复轮未被 coverage-rejected（豁免生效）
        assert not [a for a in out["actions"]
                    if isinstance(a, str) and a.startswith("coverage-rejected")]
        assert out["result"] == "success"    # 全部 mandatory 终验过（③）


class TestHomogeneousCapabilityRemap:
    """债③：同构目录（无可升级目标）capability → 修述重试，不直接上交。"""

    def test_capability_remapped_to_description_retry(self, tmp_path):
        # 同构目录：只有 b-model（Level.B），Lead 也是 b-model/B——
        # recommend_upgrade(b, cap=B) 无目标，r2-r4 里这条路径直接上交。
        cat = ModelCatalog()
        cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
        cp = make_cp(tmp_path, cat=cat)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "坏交付"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "能力不足",
                                 "attribution": "capability"})},
            {"text": "修述后的好交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},  # 集成验收（债①）
        ]), store_dir=None, lead_route=ModelRoute("p", "b-model", level=Level.B))
        out = orch.run_task("同构归因任务")
        assert out["final"] == "accepted-by-lead"
        assert out["result"] == "success"
        assert out["items"][0]["outcome"] == "accepted"
        events = cp.store.read_all()
        remap = next(e for e in events if e.kind == "attribution_remapped")
        assert remap.payload["from"] == "capability"
        assert remap.payload["to"] == "description"
        assert remap.payload["reason"] == "homogeneous-catalog"
        rejected = next(e for e in events if e.kind == "work_item_rejected")
        assert rejected.payload["attribution"] == "description"   # 改道后落账
        assert not [e for e in events
                    if e.kind == "work_item_escalated"
                    and "upgrade-exceeds" in str(e.payload.get("reason"))]
        item_id = next(i for i in cp.proj.work_items if i != cp._root_item_id())
        assert cp.proj.work_items[item_id].attempt == 2   # 修述重试照耗预算

    def test_remap_still_escalates_when_attempts_exhausted(self, tmp_path):
        """改道不豁免预算语义（§8）：attempt 耗尽仍上交。"""
        cat = ModelCatalog()
        cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
        cp = make_cp(tmp_path, cat=cat)
        orch = Orchestrator(cp, MockProvider(script=[
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "坏1"},
            {"text": json.dumps({"verdict": "reject", "attribution": "capability"})},
            {"text": "坏2"},
            {"text": json.dumps({"verdict": "reject", "attribution": "capability"})},
            {"text": "坏3"},
            {"text": json.dumps({"verdict": "reject", "attribution": "capability"})},
        ]), store_dir=None, lead_route=ModelRoute("p", "b-model", level=Level.B))
        out = orch.run_task("改道仍耗尽任务")
        remaps = [e for e in cp.store.read_all() if e.kind == "attribution_remapped"]
        assert remaps                                        # 改道发生
        assert out["items"][0]["outcome"] == "escalated"     # 预算耗尽仍上交（§8）
        assert out["result"] == "failed"                     # 债①：无 accepted
