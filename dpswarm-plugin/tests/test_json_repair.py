"""JSON 协议修复（_json_repair）测试。

r6/r7/r8 三轮实验反复出现的失败模式：flash 级模型在验收/集成验收面输出
markdown 分析或散文而非 JSON → 保守 fail。修复：解析失败先做一次只重排版、
不重新推理的纠正性追问；仍失败才走保守路径（验收=reject+invalid_output、
集成=fail）。语义锚点：修复不改变判断内容，只改排版；不安全放行纪律不变。
"""
from __future__ import annotations

import json

from test_open_items_o import TASK2, _accepted_item, make_cp, make_orch

PROSE = "## 分析\n这是一段没有 JSON 的散文评审意见……交付基本满足要求。"


class TestJsonRepair:
    def test_review_repaired_after_prose(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": PROSE},                          # 验收首轮：散文
            {"text": json.dumps({"verdict": "reject",
                                 "verdict_reason": "缺少测试",
                                 "attribution": "description"})},
        ])
        decision = orch._ask_lead_review("某任务", "交付文本", item_id="x")
        assert decision["verdict"] == "reject"
        assert decision["attribution"] == "description"   # 修复成功，未记 invalid_output
        assert len(orch.provider.calls) == 2              # 一次正文 + 一次修复

    def test_review_repair_exhausted_stays_conservative(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [{"text": PROSE}])       # 两轮都散文（耗尽回放最后一条）
        decision = orch._ask_lead_review("某任务", "交付文本", item_id="x")
        assert decision["verdict"] == "reject"
        assert decision["attribution"] == "invalid_output"

    def test_integration_repaired_after_prose(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": PROSE},
            {"text": json.dumps({"integrated": "pass", "requirements": [],
                                 "gaps": []})},
        ])
        orch._init_task_registry(TASK2)
        item, _ = _accepted_item(cp)
        orch._submissions[item.item_id] = "交付文本"
        verdict = orch._ask_lead_integration(
            TASK2, [cp.proj.work_items[item.item_id]])
        assert verdict["integrated"] == "pass"

    def test_integration_repair_exhausted_conservative_fail(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [{"text": PROSE}])
        orch._init_task_registry(TASK2)
        item, _ = _accepted_item(cp)
        orch._submissions[item.item_id] = "交付文本"
        verdict = orch._ask_lead_integration(
            TASK2, [cp.proj.work_items[item.item_id]])
        assert verdict["integrated"] == "fail"
        assert any("unparseable" in g for g in verdict["gaps"])
