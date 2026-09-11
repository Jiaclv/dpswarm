"""终局记录一致性：任何 finalized 路径都必须带路由标签。

来源：r8g 真实 run（classic-semantic）——6 个 item 全 accepted、集成验收判
证据不足后由轮次耗尽路径收口，记录里 result=failed 但 **final=null**，消费者
读到"无结论"。修法：_close_dimensions 对未设置 final 的路径兜底标签
（turn-exhausted / aborted），不覆盖已设置的路径语义。
"""
from __future__ import annotations

from test_open_items_o import TASK2, make_cp, make_orch


class TestFinalizeCoherence:
    def test_turn_exhausted_close_carries_route_label(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [])
        orch._init_task_registry(TASK2)
        outcome = {"task": TASK2, "items": [], "actions": []}
        orch._close_from_evidence(outcome, "attempts_exhausted")
        assert outcome["phase"] == "finalized"
        assert outcome["result"] == "failed"          # 证据不足，不虚报
        assert outcome["final"] == "turn-exhausted"   # 路由标签不为空

    def test_existing_route_label_not_overwritten(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [])
        orch._init_task_registry(TASK2)
        outcome = {"task": TASK2, "items": [], "actions": [],
                   "final": "quota-exhausted"}
        orch._close_from_evidence(outcome, "quota_exhausted")
        assert outcome["final"] == "quota-exhausted"  # 已设置的语义保留
