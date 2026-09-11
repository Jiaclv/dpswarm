"""材料下发与验收可见性回归（r3 意外 1/2/3 的修复锚定，§4/§5.3）：

- worker 装配包 = 子任务标题 + 父任务全文（内联材料随包下发）；
- 有 deps 的 item 验收 prompt 携带上游 accepted 交付（逐字截断引用，
  classic 臂"逐字约束不可核验"的盲区消除）；
- 验收规则明示：阻塞报告（"缺材料无法完成"）不得 accept，应 reject 并
  归因 context。
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


def make_cp(tmp_path):
    return ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                               max_active_node_points=8),
                        store_path=tmp_path / "events.jsonl", catalog=catalog())


def make_orch(cp, script):
    return Orchestrator(cp, MockProvider(script=script), store_dir=None,
                        lead_route=ModelRoute("p", "s-model", level=Level.S))


def _decision(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


class TestOriginMaterialDelivery:
    """父任务全文随 worker 装配包下发（r3 意外 1：标题即全部 → 缺输入）。"""

    def test_worker_package_carries_origin_task(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE, subtasks=["归纳子任务A"])},
            {"text": "A交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("分析任务。内联材料：INLINE-数据-##%% 十二条反馈全文在此。")
        assert out["items"][0]["outcome"] == "accepted"
        worker_prompt = orch.provider.calls[1]["messages"][-1]["content"]
        assert "归纳子任务A" in worker_prompt                 # 子任务标题
        assert "INLINE-数据-##%%" in worker_prompt            # 父任务内联材料
        assert "原始任务全文" in worker_prompt

    def test_retry_brief_keeps_origin_task(self, tmp_path):
        """打回重试的"任务全文"同样含父任务材料（§5.8 只增不减）。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "坏交付"},
            {"text": json.dumps({"verdict": "reject", "verdict_reason": "不达标",
                                 "attribution": "description"})},
            {"text": "修好的交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("含 RETRY-MARKER 材料的任务")
        assert out["items"][0]["outcome"] == "accepted"
        retry_prompt = orch.provider.calls[3]["messages"][-1]["content"]
        assert "打回重试" in retry_prompt and "RETRY-MARKER" in retry_prompt


class TestReviewUpstreamVisibility:
    """有 deps 时验收 prompt 携带上游 accepted 交付（r3 意外 2 盲区修复）。"""

    def test_review_prompt_carries_upstream_delivery(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE, subtasks=[
                "任务A", {"title": "任务B", "deps": [0]}])},
            {"text": "A的SCHEMA原文@@## 逐字内容"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": "B交付（引用A）"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("依赖链任务")
        assert all(i["outcome"] == "accepted" for i in out["items"])
        review_a = orch.provider.calls[2]["messages"][-1]["content"]
        review_b = orch.provider.calls[4]["messages"][-1]["content"]
        assert "上游已验收交付" not in review_a                # A 无 deps 不带
        assert "上游已验收交付" in review_b                    # B 有 deps 必带
        assert "A的SCHEMA原文@@##" in review_b                # 逐字可核验
        # 验收规则明示（r3 意外 3）：阻塞报告不得 accept
        assert "不得 accept 阻塞报告" in review_b


class TestBlockingReportRejected:
    """阻塞报告走 reject（归因 context）→ 修 context 重试 → accept（§8 对因处置）。"""

    def test_blocking_report_rejected_then_retried(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "缺少材料，无法交付（阻塞报告）"},
            {"text": json.dumps({"verdict": "reject",
                                 "verdict_reason": "阻塞报告非实质产出",
                                 "attribution": "context"})},
            {"text": "拿到材料后的真实交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": _decision("accept")},
        ])
        out = orch.run_task("可能缺料的任务")
        assert out["items"][0]["outcome"] == "accepted"
        rejected = next(e for e in cp.store.read_all()
                        if e.kind == "work_item_rejected")
        assert rejected.payload["attribution"] == "context"
