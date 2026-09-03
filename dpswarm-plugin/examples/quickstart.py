"""DPswarm quickstart：机制全链的最小编排示例（MockProvider，确定性）。

演示：§3 事实注入 → §7 裂变（2 worker 并行）→ §4 Lead 验收 →
§9.6 封存 → §4 观测汇总 → §9.1 离线 replay 对账。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from dpswarm.control import ControlPlane
from dpswarm.orchestrator import Orchestrator
from dpswarm.providers import MockProvider
from dpswarm import observation
from dpswarm.types import (
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
)


def main() -> None:
    catalog = ModelCatalog()
    catalog.register(ModelFacts("p", "s-model", Level.S,
                                aa_dimensional={"coding": 9.0, "reasoning": 9.1}))
    catalog.register(ModelFacts("p", "b-model", Level.B,
                                aa_dimensional={"coding": 7.5, "reasoning": 7.3}))

    with tempfile.TemporaryDirectory() as td:
        cp = ControlPlane(spec=RootExecutionSpec(max_open_work_items=4,
                                                 max_active_node_points=8),
                          store_path=Path(td) / "events.jsonl",
                          catalog=catalog)
        script = [
            {"text": json.dumps({"action": "fission",
                                 "route": {"provider": "p", "model": "b-model"},
                                 "subtasks": ["分片A：解析", "分片B：汇总"]})},
            {"text": "分片执行完成：解析 5150 行，产出 clean.jsonl"},
            {"text": json.dumps({"verdict": "accept", "verdict_reason": "计数守恒"})},
        ]
        orch = Orchestrator(cp, MockProvider(script=script),
                            store_dir=Path(td) / "packages",
                            lead_route=ModelRoute("p", "s-model", level=Level.S))
        result = orch.run_task("清洗日志并汇总统计")
        print("== 任务结果 ==")
        print(json.dumps(result, ensure_ascii=False, indent=1))

        # §9.6 三段式：run_task 的 accepted-by-lead 路径已自动走完；
        # 仅在相位仍开放时演示手动触发（如 Lead 未验收的路径）。
        if cp.snapshot()["seal_phase"].get("root") == "open":
            cp.begin_seal("root")
            cp.begin_settlement("root")
            cp.finish_seal("root")
        print("seal:", cp.snapshot()["seal_phase"])

        print("== 观测汇总（§4）==")
        print(json.dumps(observation.summarize_events(cp.store.read_all()),
                         ensure_ascii=False, indent=1))

        cp.close()  # 释放写者文件锁，Windows 下临时目录才能清理


if __name__ == "__main__":
    main()
