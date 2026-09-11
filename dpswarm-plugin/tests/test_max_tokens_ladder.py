"""§4 截断阶梯与角色级 max_tokens 下限测试（lg_compare_20260910 r1 实证驱动）：

- 角色下限：worker 交付面 ≥ 32768，Lead 决策/验桌面 16384；模型上限从
  catalog 的 max_output 事实读取（None = 不 clamp），不硬编码进核心。
- 截断阶梯：stop_reason=max-tokens → max_tokens 翻倍重试，不消耗 attempt
  预算，升档记 max_tokens_escalated 审计事件（from/to）；到顶/耗尽则带
  截断结果走正常 submit/review 流程。
- 覆盖首投与打回重试两条 worker 调用面（_run_worker / _review 重试）。
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


def catalog(max_output=131_072) -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5},
                            max_output=max_output))
    return cat


def make_cp(tmp_path, max_output=131_072):
    return ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                               max_active_node_points=8),
                        store_path=tmp_path / "events.jsonl",
                        catalog=catalog(max_output))


def make_orch(cp, script):
    return Orchestrator(cp, MockProvider(script=script), store_dir=None,
                        lead_route=ModelRoute("p", "s-model", level=Level.S))


def _decision(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


class TestRoleTokenFloors:
    """角色级下限：Lead 16384 / worker 32768（thinking 模型 CoT 计入输出上限）。"""

    def test_lead_and_worker_floors(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},  # 集成验收（债①）
        ])
        out = orch.run_task("下限标定任务")
        assert out["items"][0]["outcome"] == "accepted"
        assert out["final"] == "accepted-by-lead"     # 收束硬规则（债①）
        calls = orch.provider.calls
        assert len(calls) == 4                        # 决策+worker+验收+集成验收
        assert calls[0]["max_tokens"] == 16384      # Lead 决策
        assert calls[1]["max_tokens"] == 32768      # worker 交付
        assert calls[2]["max_tokens"] == 16384      # Lead 验收
        assert calls[3]["max_tokens"] == 16384      # Lead 集成验收


class TestLeadSurfaceLadder:
    """Lead 协议面（决策/验收）对称阶梯：裁决 JSON 截断 → 翻倍重试 → 正常解析，
    不落保守 reject（surface=lead-review / lead-decision 可区分）。"""

    def test_review_truncation_retries_and_parses(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付"},
            # 验收首轮：裁决 JSON 被 16384 截断（长 verdict_reason 截在字符串里）
            {"text": '{"verdict": "accept", "verdict_reason": "很长的理由……',
             "stop": "max-tokens"},
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "ok"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("验收截断任务")
        assert out["items"][0]["outcome"] == "accepted"
        calls = orch.provider.calls
        assert calls[2]["max_tokens"] == 16384     # 验收首轮下限
        assert calls[3]["max_tokens"] == 32768     # 翻倍重试后解析成功
        esc = [e for e in cp.store.read_all() if e.kind == "max_tokens_escalated"]
        assert len(esc) == 1
        assert esc[0].payload["surface"] == "lead-review"
        assert esc[0].payload["from"] == 16384 and esc[0].payload["to"] == 32768
        # 无保守 reject：不打回、不消耗重试预算
        assert not [e for e in cp.store.read_all() if e.kind == "work_item_rejected"]
        item_id = next(i for i in cp.proj.work_items if i != cp._root_item_id())
        assert cp.proj.work_items[item_id].attempt == 1

    def test_decision_truncation_retries(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": '{"action": "derive", "route": {"provider": "p", "mod',
             "stop": "max-tokens"},
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("决策截断任务")
        assert out["final"] == "accepted-by-lead"
        esc = [e for e in cp.store.read_all() if e.kind == "max_tokens_escalated"]
        assert esc and esc[0].payload["surface"] == "lead-decision"


class TestSingleSurfaceLadder:
    """single 交付面（Lead 直做）同样走截断阶梯：半截交付不得直接封存为
    root 终局交付（与 worker 交付面同构，surface=single）。"""

    def test_single_truncation_retries_before_seal(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("single", route=B_ROUTE)},
            {"text": "半截交付（CoT 吃满）", "stop": "max-tokens"},
            {"text": "翻倍后的完整交付"},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},  # 集成验收（O2）
        ])
        out = orch.run_task("单干截断任务")
        assert out["final"] == "single"
        assert out["result"] == "success"
        assert out["result_text"] == "翻倍后的完整交付"   # 封存的是完整交付
        calls = orch.provider.calls
        assert len(calls) == 4                         # 决策 + 单干 ×2 + 集成验收
        assert calls[1]["max_tokens"] == 16384         # Lead 面下限
        assert calls[2]["max_tokens"] == 32768         # 翻倍重试
        assert calls[3]["max_tokens"] == 16384         # 集成验收（Lead 面）
        esc = [e for e in cp.store.read_all() if e.kind == "max_tokens_escalated"]
        assert len(esc) == 1
        assert esc[0].payload["surface"] == "single"
        assert esc[0].payload["from"] == 16384 and esc[0].payload["to"] == 32768


class TestTruncationLadder:
    """截断阶梯：翻倍重试、不耗 attempt、审计事件、模型上限 clamp。"""

    def test_escalate_then_accept_without_attempt(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "被 CoT 吃满的半截交付", "stop": "max-tokens"},
            {"text": "翻倍后的完整交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("截断重试任务")
        assert out["items"][0]["outcome"] == "accepted"
        calls = orch.provider.calls
        assert calls[1]["max_tokens"] == 32768
        assert calls[2]["max_tokens"] == 65536     # 翻倍重试
        esc = [e for e in cp.store.read_all() if e.kind == "max_tokens_escalated"]
        assert len(esc) == 1
        assert esc[0].payload["from"] == 32768 and esc[0].payload["to"] == 65536
        item_id = next(i for i in cp.proj.work_items if i != cp._root_item_id())
        assert cp.proj.work_items[item_id].attempt == 1   # 阶梯不耗 attempt 预算

    def test_ladder_clamped_by_model_max_output(self, tmp_path):
        """max_output=32768：首轮即顶格，无法升档 → 带截断结果走正常流程。"""
        cp = make_cp(tmp_path, max_output=32768)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "顶格仍截断的交付", "stop": "max-tokens"},
            {"text": json.dumps({"verdict": "accept"})},   # 验收宽容放行
            {"text": _decision("accept")},
        ])
        out = orch.run_task("顶格截断任务")
        assert out["items"][0]["outcome"] == "accepted"
        calls = orch.provider.calls
        worker_calls = [c for c in calls if c["max_tokens"] == 32768]
        assert len(worker_calls) == 1                      # 无翻倍重试
        assert not [e for e in cp.store.read_all()
                    if e.kind == "max_tokens_escalated"]

    def test_ladder_inside_review_retry_path(self, tmp_path):
        """§8 打回重试面同样走阶梯：reject → 重试截断 → 翻倍成功；attempt=2
        （真实打回耗一次，阶梯升档不再耗）。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "坏交付"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "不达标",
                                 "attribution": "description"})},
            {"text": "重试仍半截", "stop": "max-tokens"},
            {"text": "翻倍后修好的交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("打回后截断任务")
        assert out["items"][0]["outcome"] == "accepted"
        esc = [e for e in cp.store.read_all() if e.kind == "max_tokens_escalated"]
        assert len(esc) == 1
        assert esc[0].payload["from"] == 32768 and esc[0].payload["to"] == 65536
        item_id = next(i for i in cp.proj.work_items if i != cp._root_item_id())
        assert cp.proj.work_items[item_id].attempt == 2   # 一次真实打回 + 阶梯不耗
