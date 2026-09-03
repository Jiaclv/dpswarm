"""模型分级与异常升级路径（§8 机制六）。

- 全局分级 S/A/B/C/D：先验来自目录（AA 分维），更新慢。
- V1 选型依据：按任务类型匹配 AA 分维评分，全局总分兜底；自家画像只攒不用。
- 异常路径：打回 → 归因四分支 → 对因处置；升级上限 = Lead 级别（验收制推论）。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .types import Level, ModelCatalog, ModelFacts, ModelRoute, RejectAttribution


def recommend_upgrade(
    catalog: ModelCatalog,
    current: ModelRoute,
    lead_level: Level,
    task_bucket: str,
) -> Optional[ModelFacts]:
    """能力弱项归因的换模型建议（§8）：B/C/D→A 轻决策；A→S 之前必须先深挖
    （防系统问题误当能力问题）。升级上限 = Lead 级别——A Lead 的 worker 最高
    升到 A，需 S 执行时走任务上交（§7 推论②）。"""
    target_level = Level.A if current.level.rank < Level.A.rank else Level.S
    if target_level.rank > lead_level.rank:
        return None  # 超过 Lead 级别：无升级路径，调用方应走 escalate
    candidates = [
        f for f in catalog.facts.values()
        if f.available and f.level == target_level
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.aa_score(task_bucket))


def escalation_path(attribution: RejectAttribution) -> str:
    """归因四分支 → 处置路由（§8 表）。Lead 做语义判断，本函数给出处置协议。"""
    return {
        RejectAttribution.CAPABILITY: "upgrade-model-and-retry",      # 升级模型（上限 = Lead 级别）
        RejectAttribution.CONTEXT: "fix-context-and-retry",          # 修 context 重试，不动模型（兼作 manager 裁偏率观测点）
        RejectAttribution.DESCRIPTION: "fix-description-and-retry",  # 修描述重试
        RejectAttribution.CONTRADICTION: "degenerate-or-escalate",   # 任务矛盾：退化或上报人工，不硬磕
    }[attribution]


def match_bucket(task_description: str) -> str:
    """V1 任务分桶（§10 留白，机制层用 AA 分维作桶代理）：关键词粗分。"""
    d = task_description.lower()
    if any(k in d for k in ("code", "coding", "代码", "编码", "implement",
                            "实现", "编写", "开发", "refactor", "重构",
                            "bug", "修复")):
        return "coding"
    if any(k in d for k in ("reason", "推理", "analyz", "分析", "prove", "论证")):
        return "reasoning"
    if any(k in d for k in ("search", "检索", "survey", "调研", "collect", "搜集")):
        return "search"
    return "overall"


def level_envelope(lead: Level, worker: Level) -> bool:
    """召唤级别方向（§7）：只能召唤同级或低级——验收制要求审查者 ≥ 执行者。"""
    return worker.rank <= lead.rank
