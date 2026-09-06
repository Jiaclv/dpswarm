"""观测聚合（§4 观测与闭环 + §6 委派经济性）。

从事件流（唯一真源，§9.1）提取全账：委派记录、token 分账、失败清单、
委派经济性。本模块**只读事件、不写事件**——聚合是观测闭环的离线一端
（§4：V1 闭环只跑到"更新画像"为止，画像只攒不用，§8）。

事件 payload 契约（与 events.py 词汇表对齐）：
- ``observation_recorded``          payload = DelegationRecord.to_payload()
- ``token_usage_recorded``          payload = {node_id, input, output,
                                   cache_read, cache_write, cost}
- ``delegation_economics_recorded`` payload = {item_id, lead_tokens,
                                   estimated_savings}（§6：Lead 为完成委派
                                   消耗的 token vs 委派节省的 token）
"""
from __future__ import annotations

import dataclasses
from collections import Counter
from typing import Any, Dict, List, Optional

from .events import DelegationRecord, Event

# failures() 关注的终局（§4 第二层 work item 终局原因 + §8 打回路径；
# assistant-rejected = §7 分裂协助者未过信号，随主结案由主代写）
_FAILURE_OUTCOMES = {"rejected", "rejected-exhausted", "escalated", "timeout",
                     "assistant-rejected"}

# 节点角色 → 经济性分桶（§6/§7：root lead 与各 Team Lead 都是"调度者"，
# context manager 不是节点、不占点，其成本单列为 cm_tokens）
_LEAD_ROLES = {"root-lead", "lead"}
_CM_ROLES = {"context-manager"}

_LEDGER_KEYS = ("input", "output", "cache_read", "cache_write", "cost")


def _new_ledger_row() -> Dict[str, float]:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def _record_from_payload(payload: Dict[str, Any]) -> DelegationRecord:
    """按 DelegationRecord 字段重建记录；缺字段回退到 dataclass 默认值。"""
    kwargs: Dict[str, Any] = {}
    for f in dataclasses.fields(DelegationRecord):
        if f.name in payload:
            kwargs[f.name] = payload[f.name]
        elif f.default is not dataclasses.MISSING:
            kwargs[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            kwargs[f.name] = f.default_factory()  # type: ignore[misc]
        else:
            kwargs[f.name] = None
    return DelegationRecord(**kwargs)


class ObservationSink:
    """从事件流提取全账（§4 + §6）。只读事件，不写事件。"""

    def __init__(self, events: List[Event]) -> None:
        self.events: List[Event] = list(events)

    # -- 基础 ------------------------------------------------------------

    def _iter(self, kind: str):
        for e in self.events:
            if e.kind == kind:
                yield e

    # -- 委派全账（§4）----------------------------------------------------

    def delegation_records(self) -> List[DelegationRecord]:
        """全部委派记录：谁委派给谁、路由、拓扑、验收者、终局、token 账。"""
        return [_record_from_payload(e.payload)
                for e in self._iter("observation_recorded")
                if "execution_failure" not in e.payload]

    # -- token 分账（§4）---------------------------------------------------

    def token_accounting(self) -> tuple:
        """完整值、已知小计和逐字段未知事件数分别保留，null 不能变成免费。"""
        known, unknown = {}, {}
        for event in self._iter("token_usage_recorded"):
            node_id = event.payload.get("node_id") or "<unknown>"
            row = known.setdefault(node_id, _new_ledger_row())
            counts = unknown.setdefault(node_id, {key: 0 for key in _LEDGER_KEYS})
            for key in _LEDGER_KEYS:
                value = event.payload.get(key)
                if value is None:
                    counts[key] += 1
                else:
                    row[key] += float(value)
        for record in self.delegation_records():
            if record.node_id in known:
                continue
            values = dict(zip(_LEDGER_KEYS, (record.token_input, record.token_output,
                record.token_cache_read, record.token_cache_write, record.cost_usd)))
            known[record.node_id] = {key: float(value) if value is not None else 0.0
                                     for key, value in values.items()}
            unknown[record.node_id] = {key: int(value is None) for key, value in values.items()}
        totals = {node: {key: None if unknown[node][key] else value
                         for key, value in row.items()} for node, row in known.items()}
        return totals, known, unknown

    def token_ledger(self) -> Dict[str, Dict[str, Optional[float]]]:
        """每节点账本：任一组成事件未知，该字段总额为 None；已知小计另列。"""
        return self.token_accounting()[0]

    # -- 失败清单（§4 failure audit 数据源）--------------------------------

    def failures(self) -> List[dict]:
        """rejected / escalated / timeout 样本 + 归因（§4/§8）。

        来源两路：
        1. work_item_rejected / work_item_escalated 控制面终局事件；
        2. observation_recorded 中 outcome ∈ {rejected-exhausted, escalated,
           timeout} 的 attempt 记录（含 stopReason 与归因）。
        注意（§4）：rate-limit 背压样本不在此列（它不是失败，不进负面样本）。
        """
        out: List[dict] = []
        for e in self.events:
            if e.kind == "work_item_rejected":
                out.append(self._failure_entry(
                    e, outcome=str(e.payload.get("outcome") or "rejected")))
            elif e.kind == "work_item_escalated":
                out.append(self._failure_entry(e, outcome="escalated"))
            elif e.kind == "observation_recorded":
                outcome = "execution-failed" if "execution_failure" in e.payload else e.payload.get("outcome")
                if outcome in _FAILURE_OUTCOMES or outcome == "execution-failed":
                    out.append(self._failure_entry(e, outcome=str(outcome)))
        return out

    @staticmethod
    def _failure_entry(e: Event, outcome: str) -> dict:
        payload = dict(e.payload)
        return {
            "seq": e.seq,
            "kind": e.kind,
            "item_id": payload.get("item_id"),
            "node_id": payload.get("node_id"),
            "outcome": outcome,
            "stop_reason": payload.get("stop_reason"),
            "attribution": payload.get("attribution"),  # 免费失败归因（§4/§8）
            "payload": payload,
        }

    # -- 节点角色推断（经济性分桶用）----------------------------------------

    def node_roles(self) -> Dict[str, str]:
        """节点 → 角色推断：node_* 事件的 role（含 role_changed）优先，
        委派记录的 lead_node_id / topology 兜底（root lead 无委派时只能
        依赖 node 事件；两者都缺则按 worker 计）。

        另有一条 ID 规则（§5.5/§7）：node_id 以 ``ctx-job:`` 开头 = context
        manager 的 context job 记账账户（CM 不是节点、不占点，成本记触发方
        账下）→ 角色 context-manager，使 economics_summary 的 cm_tokens
        真正聚合 CM 成本（此前恒 0、混进 lead_tokens）。"""
        roles: Dict[str, str] = {}
        for e in self.events:
            if e.kind in ("node_provisioning", "node_activated"):
                node_id, role = e.payload.get("node_id"), e.payload.get("role")
                if node_id and role:
                    roles[node_id] = str(role)
            elif e.kind == "node_role_changed":
                node_id = e.payload.get("node_id")
                role = e.payload.get("role") or e.payload.get("to") \
                    or e.payload.get("new_role")
                if node_id and role:
                    roles[node_id] = str(role)
            elif e.kind == "token_usage_recorded":
                node_id = e.payload.get("node_id")
                if isinstance(node_id, str) and node_id.startswith("ctx-job:"):
                    roles[node_id] = "context-manager"   # context job 记账账户（§7）
        for record in self.delegation_records():
            if record.topology == "context-manager":
                roles[record.node_id] = "context-manager"   # 拓扑标注 CM（§7）
            elif record.lead_node_id:
                roles.setdefault(record.lead_node_id, "lead")
        return roles

    # -- 委派经济性（§6）----------------------------------------------------

    def economics_summary(self) -> dict:
        """委派经济性汇总（§6：Lead 消耗 vs 委派节省，量盈亏线）。

        输出：
        - ``lead_tokens``：root lead + 各 Team Lead 节点 token 合计；
        - ``worker_tokens``：其余执行节点 token 合计；
        - ``cm_tokens``：拓扑标注 context-manager 的消耗（CM 不是节点、
          不占点，§7，但其成本单独记账）；
        - ``est_saved``：delegation_economics_recorded 事件
          ``estimated_savings`` 的累计（est = max(0, Σ 各 worker 若由
          Lead 直做的估算 - worker 实耗)）；估算数据不可得时为 None；
        - ``events``：全部 delegation_economics_recorded payload（按序）。
        """
        ledger = self.token_ledger()
        roles = self.node_roles()
        buckets = {"lead_tokens": 0, "worker_tokens": 0, "cm_tokens": 0}
        for node_id, row in ledger.items():
            values = [row[key] for key in ("input", "output", "cache_read", "cache_write")]
            role = roles.get(node_id, "")
            bucket = "cm_tokens" if role in _CM_ROLES else "lead_tokens" if role in _LEAD_ROLES else "worker_tokens"
            if any(value is None for value in values):
                buckets[bucket] = None
            elif buckets[bucket] is not None:
                buckets[bucket] += int(sum(values))
        events: List[dict] = [dict(e.payload)
                              for e in self._iter("delegation_economics_recorded")]
        savings = [float(e["estimated_savings"]) for e in events
                   if e.get("estimated_savings") is not None]
        return {
            **buckets,
            "est_saved": sum(savings) if savings else None,
            "events": events,
        }


def summarize_events(events: List[Event]) -> dict:
    """一站式观测报告（§4/§6）：委派数、终局分布、token 总账、经济性。

    stopReason 五值照记（§4 第一层硬失败信号）：委派记录缺失时回退聚合
    stop_reason_recorded 事件，聚合不依赖上游填表自觉。"""
    sink = ObservationSink(events)
    records = sink.delegation_records()
    outcome_dist = Counter(r.outcome for r in records if r.outcome)
    stop_dist = Counter(r.stop_reason for r in records if r.stop_reason)
    if not stop_dist:
        stop_dist = Counter(
            e.payload.get("stop_reason") for e in events
            if e.kind == "stop_reason_recorded" and e.payload.get("stop_reason"))
    ledger, known, unknown = sink.token_accounting()
    known_totals = {key: sum(row[key] for row in known.values()) for key in _LEDGER_KEYS}
    unknown_counts = {key: sum(row[key] for row in unknown.values()) for key in _LEDGER_KEYS}
    totals = {key: None if unknown_counts[key] else known_totals[key] for key in _LEDGER_KEYS}
    return {
        "events": len(events),
        "delegations": len(records),
        "outcome_distribution": dict(outcome_dist),
        "stop_reason_distribution": dict(stop_dist),
        "token_totals": dict(totals),
        "token_by_node": ledger,
        "token_known_subtotals": known_totals,
        "token_unknown_counts": unknown_counts,
        "token_known_by_node": known,
        "token_unknown_by_node": unknown,
        "failures": len(sink.failures()),
        "economics": sink.economics_summary(),
    }
