"""事实注入与路由装配（§2 / §3 机制一）。

机制一的发动机：Lead 在做委派决策并直接选择 provider/model/effort 时，
手上有代码生成的事实——价位、AA 分维、当前容量。注入位置：决策消息
（对话确认的工具 schema / persona / 发现工具输出三处的等价物——本插件
中为决策 prompt 的注入段，内容生产归代码，语义选择归 Lead LLM）。

V1 注入来源（§3 与 §8 对齐）：AA 分维 + 目录价目 + 当前容量。
自家生产画像只落库、不注入（profile.ProfileStore 只写不读入决策）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .types import ModelCatalog, ModelRoute


def fact_block(catalog: ModelCatalog, points_total: int, points_used: int,
               task_bucket: str = "coding") -> str:
    """注入给 Lead 的事实块（§3）。"""
    return catalog.fact_sheet(points_total, points_used, task_bucket)


LEAD_DECISION_PROMPT = """你是 DPswarm 的 Lead（调度者）。当前任务树的运行边界与模型事实由代码注入，
你在有约束的空间里做语义选择（§2）。

{facts}

## 任务
{task}

## 已完成节点（供参考）
{done}

## 待办子任务（若选择 fission/derive，请列出）
{todo}

## 决策协议（只输出一个 JSON 对象，不要多余文本）
{{
  "action": "single" | "derive" | "fission" | "split" | "accept" | "reject" | "escalate" | "degenerate",
  "route": {{"provider": str, "model": str, "reasoning_effort": str}},   // action 为 single/derive/fission/split 时必填
  "subtasks": ["...", {{"title": "...", "deps": [0]}}],  // fission 时列出（≤ {max_workers} 个）；
                                                         // 条目可为字符串或对象，deps = 它依赖的前序
                                                         // subtask 下标（0 基，DAG 依赖边，§7——有依赖
                                                         // 才标，独立子任务省略 deps）
  "verdict_reason": "..."            // accept/reject 时给出理由；reject 另给 attribution
  "attribution": "capability" | "context" | "description" | "contradiction"  // 仅 reject
}}

决策参考（机制四，经济性边界）：只有当子任务的上下文需求能被一段简短描述
覆盖时，委派才划算；读到刚好够判断，不要读完。
"""


def build_lead_prompt(catalog: ModelCatalog, points_total: int, points_used: int,
                      task: str, done: List[str], todo: List[str],
                      max_workers: int, task_bucket: str = "coding") -> str:
    return LEAD_DECISION_PROMPT.format(
        facts=fact_block(catalog, points_total, points_used, task_bucket),
        task=task,
        done="\n".join(f"- {d}" for d in done) or "- （无）",
        todo="\n".join(f"- {t}" for t in todo) or "- （无）",
        max_workers=max_workers,
    )


def parse_lead_decision(text: str) -> Dict[str, Any]:
    """解析 Lead 决策 JSON；容错：提取首个平衡的花括号块。失败抛 ValueError。"""
    text = text.strip()
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in lead decision: {text[:120]!r}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                decision = json.loads(text[start:i + 1])
                # 主键：拓扑决策用 action，验收裁决用 verdict（§4 验收制），二选一。
                if "action" not in decision and "verdict" not in decision:
                    raise ValueError("decision missing 'action'/'verdict'")
                return decision
    raise ValueError("unbalanced JSON in lead decision")


def route_from_decision(decision: Dict[str, Any], catalog: ModelCatalog) -> Optional[ModelRoute]:
    """从决策 JSON 取精确路由；模型必须在目录中（硬准入前置检查）。"""
    r = decision.get("route")
    if not r:
        return None
    facts = catalog.resolve(r.get("provider", ""), r.get("model", ""))
    if facts is None:
        return None
    return ModelRoute(
        provider=facts.provider, model=facts.model,
        reasoning_effort=r.get("reasoning_effort", "default"),
        level=facts.level,
        point_weight=max(1, facts.context_window // 64_000),
    )
