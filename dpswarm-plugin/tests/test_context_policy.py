"""借鉴项⑤：上下文策略职责矩阵显式化。

锚定的不是表本身，而是各调用面的消息构造语义：
- 返工 = 干净上下文 + 打回证据（不继承实现者的旧对话轮次）；
- 验收判决携带 context_policy 审计字段（fresh+materials / fresh+registry）；
- 下游交接的解锁事件携带 selective-inherit 声明。
"""
from __future__ import annotations

import json
import threading

from dpswarm.orchestrator import CONTEXT_POLICIES, Orchestrator
from dpswarm.providers.base import Provider, ProviderResult, Usage
from dpswarm.types import StopReason
from test_open_items_o import B_ROUTE, TASK2, make_cp, make_orch


class TestPolicyTable:
    def test_all_call_surfaces_declared(self):
        for surface in ("worker-first", "worker-retry", "worker-handoff",
                        "lead-review", "lead-integration", "lead-decision"):
            assert surface in CONTEXT_POLICIES and CONTEXT_POLICIES[surface]


class _RetryFlowProvider(Provider):
    """derive → worker 交付 → 验收 reject → 返工交付 → 验收 accept → 集成 pass。"""

    name = "retry-flow"

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()
        self._review_round = 0

    def complete(self, route, messages, tools=None, max_tokens=4096):
        user = next((m["content"] for m in reversed(messages or [])
                     if m.get("role") == "user"), "")
        sysmsg = str(messages[0].get("content", "")) if messages else ""
        with self._lock:
            self.calls.append({"user": user, "sys": sysmsg,
                               "roles": [m.get("role") for m in messages or []]})
        if "你是集成验收 Lead" in sysmsg:
            text = json.dumps({"integrated": "pass", "gaps": []})
        elif "决策协议" in user:
            text = json.dumps({"action": "derive", "route": B_ROUTE})
        elif "你是验收 Lead" in sysmsg:
            with self._lock:
                self._review_round += 1
                n = self._review_round
            if n == 1:
                text = json.dumps({"verdict": "reject",
                                   "verdict_reason": "缺少边界处理",
                                   "attribution": "description"})
            else:
                text = json.dumps({"verdict": "accept", "verdict_reason": "ok"})
        else:
            text = "交付：已实现函数。"
        return ProviderResult(text=text, stop_reason=StopReason.COMPLETED,
                              usage=Usage(input_tokens=10, output_tokens=5))


class TestContextPolicySemantics:
    def test_retry_is_fresh_with_failure_evidence(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [])
        provider = _RetryFlowProvider()
        orch.provider = provider
        orch.run_task(TASK2)
        # 返工调用 = worker 面、且消息是 system+user 两条（干净上下文），
        # user 里携带打回理由与任务全文（证据，而非旧对话轮次）
        retries = [c for c in provider.calls
                   if "打回重试" in c["user"]]
        assert retries, "返工应发生（首轮 reject）"
        retry = retries[0]
        assert retry["roles"] == ["system", "user"], \
            "返工必须是干净上下文（system+user），不继承实现者旧轮次"
        assert "缺少边界处理" in retry["user"]
        assert "验收 Lead" not in retry["sys"]
        # 验收判决携带 context_policy 审计字段（按调用面区分）
        evs = [e for e in cp.store.read_all() if e.kind == "review_evidence"]
        by_surface = {}
        for e in evs:
            by_surface.setdefault(e.payload.get("surface"), []).append(
                e.payload.get("context_policy"))
        assert by_surface.get("lead-review") and \
            all(p == "fresh+materials" for p in by_surface["lead-review"])
        assert by_surface.get("lead-integration") == ["fresh+registry"]
