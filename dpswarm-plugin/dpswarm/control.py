"""DPswarm 控制面：per-root 单写者串行事务链（§9.1–§9.2）。

核心纪律（机制文档逐条对应）：
- 方法级原子事务（§9.2，实验 01 待确认 #6 的教训）：每个公开方法 = 一组事件，
  先在克隆投影上逐事件校验，全部通过才逐事件落盘；任何一步失败则整组不落盘，
  内存与磁盘状态一致——不存在"事件落了、状态没跟上"的半截态。
- append-and-flush 后才返回成功（§9.1）。
- 校验先于落盘：全部合法性检查由 invariants.check_event 在落盘前执行（§9.1）。
- 硬准入失败返回结构化原因（AdmissionError），不静默替换、不降级（§2）。
- 链内只放快速事务：finalizing 与 accepted 是两个独立事件，证据落盘在链外
  异步完成后才走 complete_accept（§9.2 / §4 结案五步）。
- watchdog / tick 不直接执行变更以外的语义决策：超时转 blocked 与 deadline
  触发封存是 §7/§9.6 确定行为；其余建议以 watchdog_suggested 事件入链（§9.2）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import invariants, state
from .events import DelegationRecord, Event, EventStore
from .invariants import InvariantViolation
from .types import (
    AcceptanceState,
    BlockState,
    DelegationKind,
    HumanDirective,
    Level,
    LifecycleState,
    ModelCatalog,
    ModelRoute,
    Node,
    NodeRole,
    RejectAttribution,
    RootExecutionSpec,
    RouteSource,
    SealPhase,
    StartType,
    WorkItem,
    new_id,
)


class ControlPlaneError(Exception):
    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class AdmissionError(ControlPlaneError):
    """硬准入结构化拒绝（§2）：代码返回原因，由人工或 Lead 重选或等待。"""


# §2.1 合法域：范围取自机制语义（深度=agent 层级、裂变护栏、重试预算口径）
# 与默认值的量级关系；两道时间闸的先后（deadline > 单节点 wall-clock）是
# 文档明文约束，发布时强制。
_SPEC_RANGES = {
    "max_open_work_items": (1, 99),
    "max_active_node_points": (1, 99),
    "max_depth": (1, 4),
    "max_team_workers": (1, 8),
    "max_attempts": (1, 5),
}


def _validate_spec(spec: RootExecutionSpec) -> None:
    for name, (lo, hi) in _SPEC_RANGES.items():
        v = getattr(spec, name)
        if not (lo <= v <= hi):
            raise ControlPlaneError("SPEC_INVALID", f"{name}={v} 不在 [{lo},{hi}]")
    if not (0 < spec.subteam_point_ratio < 1):
        raise ControlPlaneError("SPEC_INVALID",
                                f"subteam_point_ratio={spec.subteam_point_ratio} 不在 (0,1)"
                                "（§7：子 Team 本地上限必须小于父级）")
    if spec.deadline_seconds is not None and spec.deadline_seconds < 60:
        raise ControlPlaneError("SPEC_INVALID", "deadline_seconds 需 None 或 ≥60 秒")
    if not (0 < spec.node_wallclock_timeout <= 86400):
        raise ControlPlaneError("SPEC_INVALID",
                                f"node_wallclock_timeout={spec.node_wallclock_timeout} 不在 (0,86400]")
    if not (0 < spec.settlement_timeout_seconds <= 86400):
        raise ControlPlaneError("SPEC_INVALID",
                                f"settlement_timeout_seconds={spec.settlement_timeout_seconds}"
                                " 不在 (0,86400]（§9.6 结算超时兜底）")
    if spec.deadline_seconds is not None \
            and spec.deadline_seconds <= spec.node_wallclock_timeout:
        raise ControlPlaneError(
            "SPEC_INVALID",
            f"deadline({spec.deadline_seconds}s) 必须晚于单节点 wall-clock"
            f"({spec.node_wallclock_timeout}s)：两道时间闸的先后是 §2.1/§7 明文约束")
    if not spec.root_acceptance_mode:
        raise ControlPlaneError("SPEC_INVALID", "root_acceptance_mode 不能为空")


class ControlPlane:
    """一棵 root task 树一条事务链（§9.2）。所有变更经 _transact 串行执行。"""

    def __init__(
        self,
        spec: Optional[RootExecutionSpec] = None,
        store_path: Optional[Path] = None,
        catalog: Optional[ModelCatalog] = None,
        root_level: Optional[Level] = None,
    ) -> None:
        self.store = EventStore(store_path)
        self.catalog = catalog or ModelCatalog()
        self.proj = state.replay(self.store.read_all())
        self._lock = threading.Lock()  # 单写者进程内的防御性互斥
        self._root_started_at: float = 0.0
        if self.store.last_seq < 0:
            self._bootstrap(spec or RootExecutionSpec(), root_level)
        elif not any(it.kind == DelegationKind.ROOT for it in self.proj.work_items.values()):
            # 有事件但缺 root item = bootstrap 半截态（历史版本逐条 append 的崩溃
            # 窗口）：显式类型化报错，不带无修复路径的账本启动（§9.1 fail-closed；
            # 修复前重启后一切操作 NO_ROOT_ITEM）
            raise ControlPlaneError(
                "BOOTSTRAP_INCOMPLETE",
                "事件日志已有事件但缺 root work item（bootstrap 半截态）——"
                "账本不可自洽恢复，请从备份重建或新开 root")
        for ev in self.store.read_all():
            if ev.kind == "root_started":
                self._root_started_at = ev.ts
                break

    def close(self) -> None:
        """释放事件日志写者锁（同进程复盘重建 ControlPlane 前调用）。"""
        with self._lock:
            self.store.close()

    # ------------------------------------------------------------------
    # 事务原语：先全量校验（克隆投影），后落盘（append+apply）。
    # ------------------------------------------------------------------

    def _transact(self, *pairs: Tuple[str, Dict[str, Any]]) -> List[Event]:
        if not pairs:
            return []
        with self._lock:
            try:
                cand = self.proj.copy()
                base_seq = self.store.last_seq
                staged: List[Event] = []
                for i, (kind, payload) in enumerate(pairs):
                    ev = Event(seq=base_seq + 1 + i, kind=kind, payload=payload)
                    cand = invariants.check_event(cand, ev)
                    staged.append(ev)
            except InvariantViolation as iv:
                # 控制面拒绝统一以 ControlPlaneError 面向调用方（结构化 code 保留）；
                # 准入类调用方再细分包装为 AdmissionError。
                raise ControlPlaneError(iv.code, str(iv)) from iv
            # 整事务一行 envelope 落盘（P1-1）：磁盘成功后才推进内存投影
            events = self.store.append_txn(staged)
            for ev in events:
                state.apply_event(self.proj, ev)
            return events

    # 事件内容是纯记录类（无不变量约束）时的直通通道。
    def _record(self, kind: str, payload: Dict[str, Any]) -> Event:
        with self._lock:
            ev = self.store.append(kind, payload)
            state.apply_event(self.proj, ev)
            return ev

    # ------------------------------------------------------------------
    # root / spec（§2.1）
    # ------------------------------------------------------------------

    def _bootstrap(self, spec: RootExecutionSpec,
                   root_level: Optional[Level] = None) -> None:
        # 校验先于落盘（§9.1）：非法 spec 不进事件链——此前 bootstrap 绕过全部
        # invariant，是全系统唯一不校验写路径（修复 3.1）
        _validate_spec(spec)
        # root Lead 级别（P1-5）：默认 S（历史口径）；调用方传入经 AA 目录解析
        # 的实际级别——fission 权限跟着实际模型走，不再永远 S。
        root_route = _ROOT_ROUTE if root_level is None \
            else ModelRoute("dpswarm", "root-lead", level=root_level)
        root_id = new_id("root")
        lead_node = new_id("node")
        root_item = new_id("wi")
        lease = new_id("lease")
        # 7 事件 = 一个事务：在空投影上逐一过 check_event 暂存，单次 append_txn
        # 一行落盘（§9.1；此前 7 次独立 append = 7 次 fsync 且零校验）。
        # _root_started_at 由 __init__ 紧随其后的 read_all 循环从事件 ts 设置。
        self._transact(
            ("root_started", {
                "root_id": root_id, "spec": asdict(spec), "lead_node_id": lead_node,
            }),
            # root Lead 最先准入（§7）：控制节点占点，不占 worker 槽。
            ("work_item_created", {
                "item_id": root_item, "kind": DelegationKind.ROOT.value,
                "parent_item": None, "team": "root", "depth": 1, "deps": [],
            }),
            ("lease_acquired", {
                "lease_id": lease, "node_id": lead_node, "points": 1,
            }),
            ("node_provisioning", {
                "node_id": lead_node, "item_id": root_item, "role": NodeRole.ROOT_LEAD.value,
                "route": _route_dict(root_route), "level": root_route.level.value,
                "start_type": StartType.NEW.value, "lease_id": lease,
                "delegation_depth": 1, "team": "root",
            }),
            ("route_resolved", {
                "node_id": lead_node, "proposed": _route_dict(root_route),
                "resolved": _route_dict(root_route), "source": RouteSource.ROUTE_HUMAN.value,
                "policy_version": self.catalog.point_policy_version,
            }),
            ("node_activated", {
                "node_id": lead_node, "session_id": new_id("sess"), "manifest_hash": "",
            }),
            # 登记 root team 的 lead（决策 6：lead_node 由 node_role_changed 设置）
            ("node_role_changed", {
                "node_id": lead_node, "new_role": NodeRole.ROOT_LEAD.value, "team": "root",
            }),
        )

    def _root_item_id(self) -> str:
        for it in self.proj.work_items.values():
            if it.kind == DelegationKind.ROOT:
                return it.item_id
        raise ControlPlaneError("NO_ROOT_ITEM", "root work item missing")

    @property
    def root_lead_node(self) -> str:
        for n in self.proj.nodes.values():
            if n.role == NodeRole.ROOT_LEAD:
                return n.node_id
        raise ControlPlaneError("NO_ROOT_LEAD", "root lead node missing")

    def publish_spec(self, new_spec: RootExecutionSpec) -> Event:
        """人工调整边界：发布新 revision（§2.1）。容量低于当前占用时不强杀在途
        节点、只停止新准入——该语义由 invariants 的容量检查在下次准入时自然执行。
        越界值在此 fail-fast 拒绝（不进事件链）。"""
        _validate_spec(new_spec)
        revised = RootExecutionSpec(**{**asdict(new_spec),
                                       "spec_id": self.proj.spec.spec_id,
                                       "revision": self.proj.spec.revision + 1})
        return self._transact(("spec_published", {"spec": asdict(revised)}))[0]

    # ------------------------------------------------------------------
    # 拓扑：work item 与 DAG（§7 机制五）
    # ------------------------------------------------------------------

    def create_work_item(
        self,
        kind: DelegationKind,
        parent_item: Optional[str] = None,
        deps: Optional[List[str]] = None,
        team: str = "root",
    ) -> WorkItem:
        """派生 / 裂变创建新 work item（分裂不建 item，走 split()）。

        硬准入（§7 护栏，代码层强制）：
        - 深度：新 item depth = parent+1 ≤ spec.max_depth（默认两层 → 子 item 即底层）
        - 裂变权限：仅 S 级 Lead（该 team 的 lead 节点级别 == S）
        - 裂变规模：team_open_workers < spec.max_team_workers
        - worker 槽：open_worker_slots_used < spec.max_open_work_items
        - seal 期禁止（invariants）
        """
        deps = list(deps or [])
        parent = self.proj.work_items.get(parent_item) if parent_item else None
        if kind == DelegationKind.ROOT:
            raise ControlPlaneError("BAD_KIND", "ROOT item is created at bootstrap only")
        if parent is None:
            raise AdmissionError("NO_PARENT", "derive/fission requires parent work item")
        depth = parent.depth + 1
        team_lead_level = self._team_lead_level(team)
        payload: Dict[str, Any] = {
            "item_id": new_id("wi"), "kind": kind.value, "parent_item": parent_item,
            "team": team, "depth": depth, "deps": deps,
        }
        if kind == DelegationKind.FISSION:
            if team_lead_level is not None and team_lead_level != Level.S:
                raise AdmissionError(
                    "FISSION_PERMISSION",
                    f"fission requires S-level lead, team lead is {team_lead_level.value} (§7)",
                    team=team,
                )
            # 一次裂变 = 一个子 Team（§7）：裂变者即 Lead——同一 Lead 的既有
            # 未封存子 Team 优先挂入（一 Lead 一 team），否则建新队；本地上限 =
            # 父有效上限 × 50%。已指定具体 team 的后续 worker 直接挂入。
            if team == "root":
                lead_id = self.proj.teams["root"].lead_node
                existing = next(
                    (t.team_id for t in reversed(list(self.proj.teams.values()))
                     if t.parent_team == "root" and not t.sealed
                     and (t.lead_node is None or t.lead_node == lead_id)), None)
                if existing is not None:
                    payload["team"] = existing
                else:
                    payload["new_team_id"] = new_id("team")
        pairs: List[Tuple[str, Dict[str, Any]]] = [("work_item_created", payload)]
        if "new_team_id" in payload:
            # 裂变者即 Lead（§7）：建队与 lead 登记必须在同一事务原子完成——
            # 分两步且吞异常会留下 lead_node=None 的 team（fission 永卡
            # FISSION_FORBIDDEN、级别校验整体跳过）。root lead 兼任时不移动
            # 归属（不挤子 team cap）；worker 裂变转 Lead 时移动。
            lead_node = self.proj.teams[team].lead_node if team in self.proj.teams else None
            if lead_node:
                pairs.append(("node_role_changed", {
                    "node_id": lead_node,
                    "new_role": self.proj.nodes[lead_node].role.value,
                    "team": payload["new_team_id"],
                    "move_node": lead_node != self.root_lead_node,
                }))
        try:
            self._transact(*pairs)
        except ControlPlaneError as iv:
            raise AdmissionError(iv.code, str(iv), kind=kind.value) from iv
        return self.proj.work_items[payload["item_id"]]

    def add_dependency(self, before: str, after: str) -> Event:
        """DAG 边 + CAS（§9.2）：expected_graph_revision 过期则拒。"""
        return self._transact(("work_item_dependency_added", {
            "before": before, "after": after,
            "expected_graph_revision": self.proj.graph_revision,
        }))[0]

    def _team_lead_level(self, team_id: str) -> Optional[Level]:
        team = self.proj.teams.get(team_id)
        if team is None or team.lead_node is None:
            return None
        node = self.proj.nodes.get(team.lead_node)
        return node.level if node else None

    # ------------------------------------------------------------------
    # 节点准入与两阶段启动（§9.3）
    # ------------------------------------------------------------------

    def begin_node(
        self,
        item_id: str,
        route: ModelRoute,
        package_ref: Optional[str] = None,
        package_hash: Optional[str] = None,
        role: NodeRole = NodeRole.WORKER,
        assistant_of: Optional[str] = None,
        human_override: bool = False,
    ) -> Node:
        """两阶段第一步：provisioning 意图落盘（§9.3）。

        硬准入全检查（§2）：模型存在且可用（catalog）、级别方向（≤ lead，human
        override 例外）、深度、worker 槽（item 已在创建时占槽，此处只对非依附角色
        校验容量现状）、节点点数（lease 先取、不足即拒）。失败抛 AdmissionError
        ——代码返回结构化原因，不静默替换（§2）。
        """
        item = self.proj.work_items.get(item_id)
        if item is None:
            raise AdmissionError("NO_ITEM", f"work item {item_id} not found")
        if item.acceptance not in (None, AcceptanceState.REJECTED):
            raise AdmissionError("ITEM_NOT_RUNNABLE",
                                 f"item acceptance = {item.acceptance}, cannot start node")
        # §4 解锁后继：依赖准入硬门禁——deps 非空且未全部 accepted 时拒绝启动
        # （item_ready 不再只是查询；root item deps 为空不受影响）。rollover/
        # resume 不经此处（复用既有节点，依赖首启时已满足且 accepted 不可逆）。
        if item.deps and not self.proj.item_ready(item_id):
            raise AdmissionError(
                "DEPS_NOT_READY",
                f"work item {item_id} deps 未全部 accepted（§4：accepted 才解锁后继）",
                deps=list(item.deps),
            )
        facts = self.catalog.resolve(route.provider, route.model)
        if facts is None or not facts.available:
            raise AdmissionError("MODEL_UNAVAILABLE",
                                 f"{route.provider}/{route.model} not in catalog or unavailable")
        route = ModelRoute(**{**route.__dict__, "level": facts.level})
        if role in (NodeRole.WORKER, NodeRole.ASSISTANT):
            lead_level = self._team_lead_level(item.team)
            if lead_level is not None and route.level.rank > lead_level.rank and not human_override:
                raise AdmissionError(
                    "LEVEL_DIRECTION",
                    f"can only summon same or lower level: worker {route.level.value} "
                    f"> lead {lead_level.value} (§7); human override excepted",
                )
        node_id = new_id("node")
        lease_id = new_id("lease")
        # 物理深度：协助者挂其主执行者下一层（split 同层语义的物理 child，§7）；
        # 普通 worker 挂其 team 的 lead 节点下一层（root lead = 1）。
        if assistant_of and assistant_of in self.proj.nodes:
            parent_phys = self.proj.nodes[assistant_of].delegation_depth
        else:
            team = self.proj.teams.get(item.team)
            if team is not None and team.lead_node and team.lead_node in self.proj.nodes:
                parent_phys = self.proj.nodes[team.lead_node].delegation_depth
            else:
                parent_phys = max((n.delegation_depth for n in self.proj.nodes.values()), default=0)
        pairs: List[Tuple[str, Dict[str, Any]]] = [
            ("lease_acquired", {"lease_id": lease_id, "node_id": node_id,
                                "points": route.point_weight}),
            ("node_provisioning", {
                "node_id": node_id, "item_id": item_id, "role": role.value,
                "route": _route_dict(route), "level": route.level.value,
                "start_type": StartType.NEW.value, "lease_id": lease_id,
                "delegation_depth": parent_phys + 1, "team": item.team,
                "package_ref": package_ref, "package_hash": package_hash,
                "assistant_of": assistant_of, "human_override": human_override,
            }),
            ("route_resolved", {
                "node_id": node_id, "proposed": _route_dict(route),
                "resolved": _route_dict(route),
                "source": route.source.value,
                "policy_version": self.catalog.point_policy_version,
            }),
        ]
        try:
            self._transact(*pairs)
        except ControlPlaneError as iv:
            raise AdmissionError(iv.code, str(iv), node_id=node_id) from iv
        return self.proj.nodes[node_id]

    def confirm_node(self, node_id: str, session_id: Optional[str] = None,
                     manifest_hash: Optional[str] = None) -> Node:
        """两阶段第二步：child session 已建、预装包 hash 比对确认后才 active（§9.3）。"""
        node = self.proj.nodes.get(node_id)
        if node is None or node.lifecycle != LifecycleState.PROVISIONING:
            raise ControlPlaneError("NOT_PROVISIONING", f"node {node_id} not in provisioning")
        self._transact(("node_activated", {
            "node_id": node_id,
            "session_id": session_id or new_id("sess"),
            "manifest_hash": manifest_hash or node.package_hash or "",
        }))
        return self.proj.nodes[node_id]

    def fail_node(self, node_id: str, reason: str) -> None:
        """provisioning failed = 启动事务失败，不耗重试预算，配对释放 lease（§8/§9.3）。

        只适用于 PROVISIONING 节点——ACTIVE 节点的运行期失联/崩溃走 mark_crashed：
        生命周期表允许 ACTIVE→FAILED，但崩溃后去向未定前 lease 必须保持占用，
        不能因某次 API 失联就假定节点已关闭（§7 lease 保守持有）。
        """
        node = self.proj.nodes.get(node_id)
        if node is None:
            raise ControlPlaneError("NO_NODE", f"node {node_id} not found")
        if node.lifecycle != LifecycleState.PROVISIONING:
            raise ControlPlaneError(
                "NOT_PROVISIONING",
                f"fail_node 仅适用于 PROVISIONING 节点（当前 {node.lifecycle.value}）；"
                f"ACTIVE 节点崩溃走 mark_crashed（§7：去向未定前 lease 继续占用）")
        self._transact(
            ("node_failed", {"node_id": node_id, "reason": reason}),
            ("lease_released", {"lease_id": node.lease_id}),
        )

    def mark_crashed(self, node_id: str, reason: str) -> None:
        """ACTIVE 节点运行期崩溃（§7）：node_failed（ACTIVE→FAILED）但**不释放
        lease——节点去向未定，lease 保持占用直到 retry/terminate/escalate/结案
        处置（不能因某次 API 失联就假定节点已关闭）。若崩溃的是分裂协助者，
        同事务 wakeup 其主执行者（§9.4"协助者崩溃时控制面 wakeup 主执行者"
        的首个实际用例）：通知只报"有变化"，主重读投影看到 failed，自行决定
        重拉（走点数准入）或收拢单干。"""
        node = self.proj.nodes.get(node_id)
        if node is None:
            raise ControlPlaneError("NO_NODE", f"node {node_id} not found")
        if node.lifecycle != LifecycleState.ACTIVE:
            raise ControlPlaneError(
                "NOT_ACTIVE",
                f"mark_crashed 仅适用于 ACTIVE 节点（当前 {node.lifecycle.value}）；"
                f"PROVISIONING 启动失败走 fail_node（§8：不耗预算 + 配对释放）")
        pairs: List[Tuple[str, Dict[str, Any]]] = [
            ("node_failed", {"node_id": node_id, "reason": reason, "crashed": True}),
        ]
        if node.role == NodeRole.ASSISTANT and node.assistant_of:
            pairs.append(("node_wakeup", {"node_id": node.assistant_of,
                                          "about": "assistant-crashed"}))
        self._transact(*pairs)

    def reconcile_provisioning(self, node_id: str, session_id: Optional[str] = None,
                               manifest_hash: Optional[str] = None,
                               ok: bool = True, reason: str = "") -> None:
        """崩溃恢复对账（§9.3），带观测事实：ok 分支以恢复者观测到的 session_id /
        manifest_hash confirm——合成随机 id 会把真实 session 永久 fence 在外
        （修复 3.2）；对账判 failed 时若已 active，报类型化冲突不静默并存。
        V1 无真实 harness 层，观测值由调用方（崩溃恢复入口）提供；缺省时退回
        confirm_node 的占位生成（同进程单写者自查口径）。"""
        node = self.proj.nodes.get(node_id)
        if node is None:
            raise ControlPlaneError("NO_NODE", f"node {node_id} not found")
        if node.lifecycle == LifecycleState.ACTIVE:
            raise ControlPlaneError("RECONCILE_CONFLICT",
                                    f"node {node_id} already active; drain the duplicated child")
        if node.lifecycle == LifecycleState.FAILED:
            return
        if ok:
            self.confirm_node(node_id, session_id=session_id, manifest_hash=manifest_hash)
        else:
            self.fail_node(node_id, reason or "reconcile mismatch")

    # ------------------------------------------------------------------
    # 分裂：协助者 + peer 通道（§7 / §9.5）
    # ------------------------------------------------------------------

    def split(self, primary_node_id: str, route: ModelRoute,
              package_ref: Optional[str] = None,
              package_hash: Optional[str] = None) -> Tuple[Node, str]:
        """分裂 = 1 主 1 副同构协作（§7）：协助者与其主共享同一 work item，
        不新建 item、不占新 worker 槽，占自己的节点点数；随分裂动作代码建立
        peer 通道（不是 LLM 的可选项，§9.5）。"""
        primary = self.proj.nodes.get(primary_node_id)
        if primary is None:
            raise AdmissionError("NO_NODE", f"node {primary_node_id} not found")
        assistant = self.begin_node(
            primary.item_id, route, package_ref, package_hash,
            role=NodeRole.ASSISTANT, assistant_of=primary_node_id,
        )
        channel_id = new_id("chan")
        self._transact(("peer_channel_opened", {
            "channel_id": channel_id, "primary_node": primary_node_id,
            "assistant_node": assistant.node_id, "item_id": primary.item_id,
        }))
        return assistant, channel_id

    def peer_send(self, channel_id: str, from_node: str, body: str) -> str:
        message_id = new_id("msg")
        self._transact(("message_queued", {
            "channel_id": channel_id, "message_id": message_id,
            "from_node": from_node, "body": body,
        }))
        return message_id

    def peer_deliver(self, message_id: str) -> None:
        self._transact(("message_delivered", {"message_id": message_id}))

    def peer_close(self, channel_id: str, reason: str) -> None:
        """关闭 = 停止接受新消息；在途消息按封存三段式有界结算（§9.5）。"""
        self._transact(("peer_channel_closed", {"channel_id": channel_id, "reason": reason}))



    def _successor_invalidate_pairs(self, item_id: str) -> List[Tuple[str, Dict[str, Any]]]:
        """§9.3 终态优先：item 终态作废未完成的 §5.8 CAS successor 登记
        （三路复位的 terminal-invalidate 路，此前有词汇无发射点）。"""
        return [
            ("successor_reset", {"node_id": n.node_id,
                                 "context_epoch": n.successor_reg[1],
                                 "cause": "terminal-invalidate"})
            for n in self.proj.nodes.values()
            if n.item_id == item_id and n.successor_reg is not None
        ]

    def _channel_close_pairs(self, item_id: str) -> List[Tuple[str, Dict[str, Any]]]:
        """§9.5：通道随 work item 进入任一终态关闭。事件须排在终态事件之前——
        post 校验要求终态落盘时通道已 closed（invariants.CHANNEL_NOT_CLOSED）。
        payload 附 dropped = queued 未投递消息 id：关通道与记丢弃原子完成
        （§9.5/§9.6 有界结算"投递完毕或记录丢弃，不静默消失"）。"""
        pairs: List[Tuple[str, Dict[str, Any]]] = []
        for ch in self.proj.peer_channels.values():
            if ch.get("closed") or ch.get("item_id") != item_id:
                continue
            dropped = [mid for mid, m in self.proj.messages.items()
                       if m.get("channel_id") == ch["channel_id"] and not m.get("delivered")]
            pairs.append(("peer_channel_closed", {
                "channel_id": ch["channel_id"], "reason": "item-terminal",
                "dropped": dropped}))
        return pairs

    def _drain_pairs(self, item_id: str) -> List[Tuple[str, Dict[str, Any]]]:
        """结案资源清理（§4/§7，complete_accept / escalate / terminate /
        abort_finalize / retire_item_nodes 共用）：
        - node_drained 只对**非终态 PROVISIONING/ACTIVE** 节点——crashed 节点
          （node_failed 已置 terminated）不能再 drain（id 永不复用，重复 drain 违规）；
        - lease_released 对 item 名下**所有仍 active 的 lease**，无论节点 terminated
          与否——crashed 节点滞留的 active lease（§7 去向未定前保守持有）必须在
          结案时一并归还，否则 post 校验 LEASE_NOT_RELEASED 爆炸。
        顺序：先 drain 后 release（与既有结案序列一致）。"""
        pairs: List[Tuple[str, Dict[str, Any]]] = []
        for n in self.proj.nodes.values():
            if n.item_id != item_id:
                continue
            if (not n.terminated
                    and n.lifecycle in (LifecycleState.PROVISIONING, LifecycleState.ACTIVE)):
                pairs.append(("node_drained", {"node_id": n.node_id}))
        for lease in self.proj.leases.values():
            if not lease.active:
                continue
            node = self.proj.nodes.get(lease.node_id)
            if node is not None and node.item_id == item_id:
                pairs.append(("lease_released", {"lease_id": lease.lease_id}))
        return pairs

    def retire_item_nodes(self, item_id: str) -> None:
        """再执行前的旧节点退役（公共 API）：drain 非终态节点 + 释放 item 名下
        全部 active lease（含 crashed 滞留）。语义：旧节点去向已定、id 不复用，
        为下一次 begin_node 清出占点——例如超时重试换新节点、崩溃后重拉之前。"""
        self._transact(*self._drain_pairs(item_id))

    def degenerate_assistant(self, node_id: str, reason: str = "degenerate") -> None:
        """退化收回协助者（§7）：transcript 先落 evidence（调用方负责），
        再关通道与节点；不算验收、不进画像。"""
        node = self.proj.nodes.get(node_id)
        if node is None or node.role != NodeRole.ASSISTANT:
            raise ControlPlaneError("NOT_ASSISTANT", f"node {node_id} is not an assistant")
        chan = next((c["channel_id"] for c in self.proj.peer_channels.values()
                     if c.get("assistant_node") == node_id and not c.get("closed")), None)
        pairs: List[Tuple[str, Dict[str, Any]]] = []
        if chan:
            pairs.append(("peer_channel_closed", {"channel_id": chan, "reason": reason}))
        pairs += [("node_drained", {"node_id": node_id}),
                  ("lease_released", {"lease_id": node.lease_id})]
        self._transact(*pairs)

    # ------------------------------------------------------------------
    # 验收流（§4 结案五步 / §5.7 状态机 / §8 重试与上交）
    # ------------------------------------------------------------------

    def write_artifact(self, content: str) -> Optional[str]:
        """内容寻址证据落盘（§4 链外步骤，P1-3）：artifacts/<sha256>.txt，
        幂等。内存态（无 store_path）返回 None 不落盘。全文可恢复的前提：
        package_stored 只记 hash/预览/ref，正文以文件为准。"""
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if self.store.path is None:
            return None
        d = Path(self.store.path).parent / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sha}.txt").write_text(content, encoding="utf-8")
        return sha

    def submit(self, item_id: str, node_id: str, output: str = "",
               *, context_epoch: Optional[int] = None,
               session_id: Optional[str] = None) -> Event:
        """worker 提交（§5.7）。output 即时内容寻址落盘并绑定证据包（P1-3：
        后续 review 只准引用该 package，不再另行提供正文）。fence（P1-2）：
        context_epoch/session_id 与节点当前值不符即拒；None = 同进程单写者
        自查豁免（server 层对 dsh 路径强制携带）。协助者不得独立提交。"""
        item = self.proj.work_items[item_id]
        node = self.proj.nodes[node_id]
        if context_epoch is None:
            context_epoch = node.context_epoch
        if session_id is None:
            session_id = node.session_id
        pairs: List[Tuple[str, Dict[str, Any]]] = []
        pkg_id = None
        sha = ""
        if output:
            sha = hashlib.sha256(output.encode("utf-8")).hexdigest()
            pkg_id = f"dep-{item_id[:10]}-{sha[:8]}"
            self.write_artifact(output)  # 链外先落盘，链上只记 hash/ref
            pairs.append(("package_stored", {
                "package_id": pkg_id, "item_id": item_id,
                "content_hash": sha, "content_preview": output[:200],
                "source_refs": [], "size": len(output),
                "artifact_ref": f"{sha}.txt", "stage": "submission",
            }))
        pairs.append(("work_item_submitted", {
            "item_id": item_id, "attempt": item.attempt, "node_id": node_id,
            "context_epoch": context_epoch, "session_id": session_id,
            "output_sha256": sha, "package_id": pkg_id,
        }))
        return self._transact(*pairs)[0]

    def begin_finalize(self, item_id: str) -> Event:
        """Lead 决定通过 → finalizing；证据/package 提交在链外异步完成（§9.2）。"""
        return self._transact(("work_item_finalizing", {"item_id": item_id}))[0]

    def store_evidence_package(self, item_id: str, package_id: str,
                               content: str, source_refs: Optional[List[str]] = None) -> Event:
        """§4 结案第 2 步（链外，finalizing 之后、accepted 之前）：原始过程、产物
        及其 hash 写入 evidence / artifact store，提交供后继读取的 dependency
        package。accepted 前必须调用——invariant 校验 package 已落盘，杜绝
        evidence_ready 自证。正文经 write_artifact 内容寻址落盘（P1-3：事件
        只记 hash/预览/ref，全文以 artifact 文件为准）。"""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.write_artifact(content)
        return self._record("package_stored", {
            "package_id": package_id, "item_id": item_id,
            "content_hash": digest, "content_preview": content[:200],
            "source_refs": source_refs or [], "size": len(content),
            "artifact_ref": f"{digest}.txt",
        })

    def complete_accept(self, item_id: str, package_id: str, *,
                        evidence_ready: bool = False,
                        accepted_by: Optional[Dict[str, Any]] = None) -> Event:
        """accepted 原子发布（§4 五步压缩为链上事务）：同事务 drain 同 item 全部
        节点 → 释放 lease → accepted。evidence_ready 须由调用方在证据与 package
        真实落盘（store_evidence_package）后显式置 True——invariant 会校验
        package 在投影中存在，自证会被拒。落盘后：后继解锁由投影派生
        （deps 全 accepted）、槽位与点数全额归还。"""
        if not evidence_ready:
            raise ControlPlaneError("EVIDENCE_NOT_READY",
                                    "complete_accept 需要 evidence_ready=True（先 store_evidence_package）")
        pairs: List[Tuple[str, Dict[str, Any]]] = self._channel_close_pairs(item_id)
        pairs += self._drain_pairs(item_id)
        pairs.append(("work_item_accepted", {
            "item_id": item_id, "evidence_ready": True, "package_id": package_id,
            "accepted_by": accepted_by or {},
        }))
        return self._transact(*pairs)[-1]

    def reject(self, item_id: str, reason: str,
               attribution: RejectAttribution,
               rejected_by: Optional[Dict[str, Any]] = None) -> Event:
        """打回 = 免费失败归因数据（§4）；归因四分支由 Lead 语义判断（§8）。"""
        return self._transact(("work_item_rejected", {
            "item_id": item_id, "reason": reason,
            "attribution": attribution.value, "rejected_by": rejected_by or {},
        }))[0]

    def set_reweight_wait(self, node_id: str, waiting: bool = True,
                          reason: str = "") -> Event:
        """reweight-wait 抑制窗口标记（§9.3）：换模型重试/rollover 增重、点数
        差额不足（POINTS_EXCEEDED）时由调用方显式标记进入——节点保持等待且
        不得启动新模型，期间 tick 单节点超时豁免、不消耗重试预算；成功
        reweight（prepare_retry/begin_rollover 同事务携带退出事件）、结案
        drain、node_failed 时清除。"""
        node = self.proj.nodes.get(node_id)
        if node is None:
            raise ControlPlaneError("NO_NODE", f"node {node_id} not found")
        return self._transact(("node_reweight_wait", {
            "node_id": node_id, "waiting": waiting, "reason": reason}))[0]

    def prepare_retry(self, item_id: str, new_route: Optional[ModelRoute] = None) -> WorkItem:
        """归因重试（§8）：换模型 / 修 context / 修描述共用同一预算（≤ max_attempts）。
        换模型 = 同 lease 原子 reweight：增重须先取得差额，不足则等待（结构化拒绝），
        不得先启动再补账（§7）。"""
        item = self.proj.work_items[item_id]
        if item.attempt >= self.proj.spec.max_attempts:
            raise ControlPlaneError(
                "RETRY_BUDGET",
                f"attempt {item.attempt} >= max {self.proj.spec.max_attempts}; escalate (§8)")
        pairs: List[Tuple[str, Dict[str, Any]]] = [
            ("work_item_retried", {"item_id": item_id, "attempt": item.attempt + 1}),
        ]
        node = next((n for n in self.proj.nodes.values()
                     if n.item_id == item_id and not n.terminated), None)
        if node is not None and new_route is not None and not node.route.same_model(new_route):
            facts = self.catalog.resolve(new_route.provider, new_route.model)
            if facts is None or not facts.available:
                raise AdmissionError("MODEL_UNAVAILABLE", f"{new_route.provider}/{new_route.model}")
            new_route = ModelRoute(**{**new_route.__dict__, "level": facts.level})
            lease = self.proj.leases[node.lease_id]
            pairs.append(("lease_reweight", {
                "lease_id": lease.lease_id, "old_points": lease.points,
                "new_points": new_route.point_weight,
                "delta": new_route.point_weight - lease.points,
            }))
            # 换模型重试 = 同位置执行者自身替换：路由变更须持久化对账（§2），
            # 经 §9.3 唤起恢复（RESUME）复投协议更新节点路由，防止 node.route
            # 与 lease 权重脱钩。
            pairs.append(("node_provisioning", {
                "node_id": node.node_id, "item_id": item_id, "role": node.role.value,
                "route": _route_dict(new_route), "level": new_route.level.value,
                "start_type": StartType.RESUME.value, "lease_id": node.lease_id,
                "delegation_depth": node.delegation_depth, "team": node.team,
                "context_epoch": node.context_epoch,
            }))
            pairs.append(("route_resolved", {
                "node_id": node.node_id, "proposed": _route_dict(new_route),
                "resolved": _route_dict(new_route), "source": new_route.source.value,
                "policy_version": self.catalog.point_policy_version,
            }))
            if node.reweight_wait:
                # §9.3：成功 reweight 与退出抑制窗口同一事务——等待标记不留存
                pairs.append(("node_reweight_wait", {
                    "node_id": node.node_id, "waiting": False,
                    "reason": "reweight-ok"}))
        try:
            self._transact(*pairs)
        except ControlPlaneError as iv:
            raise ControlPlaneError(iv.code, str(iv), item_id=item_id) from iv
        return self.proj.work_items[item_id]

    def retry_timeout(self, item_id: str) -> WorkItem:
        """超时重试（§7 时间护栏①"重试（计预算）"）：tick 已把超时节点转
        blocked（确定性动作），此处是 Lead 归因后的处置通路——一个事务内：
        ① work_item_timeout_retried 计预算推进 attempt（acceptance 不变，
        超时发生在执行中）；② 对该 item 全部 wallclock-timeout blocked 节点
        node_drained + lease_released——释放滞留 lease，否则旧节点 lease 永久
        占点、新节点再取 lease 会造成同 item 双 lease 双占点。
        预算耗尽抛 ATTEMPT_EXHAUSTED，由调用方走 escalate/terminate 处置。"""
        item = self.proj.work_items.get(item_id)
        if item is None:
            raise ControlPlaneError("NO_ITEM", f"work item {item_id} not found")
        new_attempt = item.attempt + 1
        if new_attempt > self.proj.spec.max_attempts:
            raise ControlPlaneError(
                "ATTEMPT_EXHAUSTED",
                f"attempt {new_attempt} 超出重试预算 max_attempts="
                f"{self.proj.spec.max_attempts}（§8：预算耗尽走上交/终止，不硬磕）",
                item_id=item_id)
        pairs: List[Tuple[str, Dict[str, Any]]] = [
            ("work_item_timeout_retried", {"item_id": item_id, "attempt": new_attempt}),
        ]
        for n in self.proj.nodes.values():
            if (n.item_id == item_id and not n.terminated
                    and n.blocked == BlockState.BLOCKED
                    and n.blocked_reason == "wallclock-timeout"):
                pairs.append(("node_drained", {"node_id": n.node_id}))
                if n.lease_id:
                    pairs.append(("lease_released", {"lease_id": n.lease_id}))
        try:
            self._transact(*pairs)
        except ControlPlaneError as iv:
            raise ControlPlaneError(iv.code, str(iv), item_id=item_id) from iv
        return self.proj.work_items[item_id]

    def escalate(self, item_id: str, reason: str) -> Event:
        """任务上交（§8）：控制面事务——原 item 转 escalated 终态、释放槽与点数；
        无裁决、不进画像。父级接管在同一串行链上紧随执行（单写者天然无并发窗口）。"""
        pairs: List[Tuple[str, Dict[str, Any]]] = self._channel_close_pairs(item_id)
        pairs += self._drain_pairs(item_id)
        pairs.append(("work_item_escalated", {"item_id": item_id, "reason": reason}))
        pairs.extend(self._successor_invalidate_pairs(item_id))
        return self._transact(*pairs)[-1]

    def terminate(self, item_id: str, reason: str = "manual-stopped", summary: str = "") -> Event:
        """明确终止 / 退化收回（§7）：summary 非空 = 结论落盘、主 agent 只收摘要。
        reason 走 §4 第二层六值词汇（WorkItemOutcome），invariant 落盘前校验。"""
        pairs: List[Tuple[str, Dict[str, Any]]] = self._channel_close_pairs(item_id)
        pairs += self._drain_pairs(item_id)
        pairs.append(("work_item_terminated", {
            "item_id": item_id, "reason": reason, "summary": summary,
        }))
        pairs.extend(self._successor_invalidate_pairs(item_id))
        return self._transact(*pairs)[-1]

    def abort_finalize(self, item_id: str) -> Event:
        """finalizing 显式取消（§9.3）：已落盘证据保留、不解锁后继、释放 lease。"""
        pairs: List[Tuple[str, Dict[str, Any]]] = self._channel_close_pairs(item_id)
        pairs += self._drain_pairs(item_id)
        pairs.append(("work_item_aborted_finalize", {"item_id": item_id}))
        return self._transact(*pairs)[0]

    # ------------------------------------------------------------------
    # 硬切窗口 rollover（§5.8）
    # ------------------------------------------------------------------

    def begin_rollover(self, node_id: str, capsule_ref: str, capsule_hash: str,
                       new_route: Optional[ModelRoute] = None) -> Node:
        """软阈值预装 capsule（Assembler 链外完成）→ 硬阈值 CAS 登记 successor
        → 两阶段第一步。CAS 保证 (node_id, context_epoch) 最多承认一个 successor。"""
        node = self.proj.nodes.get(node_id)
        if node is None:
            raise ControlPlaneError("NO_NODE", f"node {node_id} not found")
        if node.successor_reg is not None:
            raise ControlPlaneError("DOUBLE_SUCCESSOR",
                                    f"successor already registered for {node_id} @{node.context_epoch}")
        route = node.route
        pairs: List[Tuple[str, Dict[str, Any]]] = [
            ("successor_registered", {
                "node_id": node_id, "context_epoch": node.context_epoch,
                "capsule_ref": capsule_ref, "capsule_hash": capsule_hash,
                "control_state_revision": self.proj.graph_revision,
            }),
        ]
        if new_route is not None and not route.same_model(new_route):
            # 换模型 rollover：原 lease 原子 reweight，先取差额（§5.8 第 4 步）。
            facts = self.catalog.resolve(new_route.provider, new_route.model)
            if facts is None or not facts.available:
                raise AdmissionError("MODEL_UNAVAILABLE", f"{new_route.provider}/{new_route.model}")
            new_route = ModelRoute(**{**new_route.__dict__, "level": facts.level})
            lease = self.proj.leases[node.lease_id]
            pairs.append(("lease_reweight", {
                "lease_id": lease.lease_id, "old_points": lease.points,
                "new_points": new_route.point_weight,
                "delta": new_route.point_weight - lease.points,
            }))
            if node.reweight_wait:
                # §9.3：成功 reweight 与退出抑制窗口同一事务
                pairs.append(("node_reweight_wait", {
                    "node_id": node_id, "waiting": False,
                    "reason": "reweight-ok"}))
        else:
            new_route = route
        pairs.append(("node_provisioning", {
            "node_id": node_id, "item_id": node.item_id, "role": node.role.value,
            "route": _route_dict(new_route), "level": new_route.level.value,
            "start_type": StartType.ROLLOVER.value, "lease_id": node.lease_id,
            "delegation_depth": node.delegation_depth,  # 深度保持（§9.3）
            "team": node.team, "context_epoch": node.context_epoch + 1,
            "predecessor_session": node.session_id,
            "package_ref": capsule_ref, "package_hash": capsule_hash,
        }))
        try:
            self._transact(*pairs)
        except ControlPlaneError as iv:
            raise ControlPlaneError(iv.code, str(iv), node_id=node_id) from iv
        return self.proj.nodes[node_id]

    def confirm_rollover(self, node_id: str, session_id: Optional[str] = None) -> Node:
        """两阶段第二步 + 登记复位（cause=activated-success，三路复位之一 §5.8）。"""
        node = self.proj.nodes[node_id]
        pairs: List[Tuple[str, Dict[str, Any]]] = [
            ("node_activated", {"node_id": node_id,
                                "session_id": session_id or new_id("sess"),
                                "manifest_hash": node.package_hash or ""}),
            ("successor_reset", {"node_id": node_id,
                                 "context_epoch": node.context_epoch - 1,  # 登记时的 epoch
                                 "cause": "activated-success"}),
        ]
        self._transact(*pairs)
        return self.proj.nodes[node_id]

    def rollback_rollover(self, node_id: str, reason: str) -> None:
        """预装失败回退（§5.8）= 现场复原：旧 session 从未被切换（预装在建 session
        之前失败），因此事务内把 context_epoch 回退登记值、session_id 恢复
        predecessor、lifecycle 恢复 ACTIVE，旧 fence 立即恢复可用——否则一次预装
        失败 = 旧 session 被 epoch fence 永久锁死 + 节点卡 PROVISIONING+BLOCKED
        无任何出口。node_blocked→node_unblocked 对记录失败归因（全库首个
        node_unblocked 发射点：复原完成即解除）；登记复位（cause=package-fail-rollback）。
        事件顺序约束：node_activated 必须排在 node_blocked 之前——invariant 拒绝
        激活 blocked 节点（1.2）。"""
        node = self.proj.nodes[node_id]
        self._transact(
            # epoch 回退登记值：唯一会写 context_epoch 的事件是 node_provisioning
            # （RESUME 语义 = 唤起 predecessor 窗口续接，§9.3 同一协议）
            ("node_provisioning", {
                "node_id": node_id, "item_id": node.item_id, "role": node.role.value,
                "route": _route_dict(node.route), "level": node.level.value,
                "start_type": StartType.RESUME.value, "lease_id": node.lease_id,
                "delegation_depth": node.delegation_depth,  # 深度保持（§9.3）
                "team": node.team, "context_epoch": node.context_epoch - 1,
            }),
            ("node_activated", {"node_id": node_id,
                                "session_id": node.predecessor_session,
                                "manifest_hash": node.package_hash or ""}),
            ("node_blocked", {"node_id": node_id, "reason": f"package-fail: {reason}"}),
            ("node_unblocked", {"node_id": node_id}),
            ("successor_reset", {"node_id": node_id,
                                 "context_epoch": node.context_epoch - 1,  # 登记时的 epoch
                                 "cause": "package-fail-rollback"}),
        )

    # ------------------------------------------------------------------
    # 通知（§9.4）与角色变更（§7 裂变者即 Lead）
    # ------------------------------------------------------------------

    def wakeup(self, node_id: str, about: str) -> Event:
        """唤醒只报告"有变化或超时"，不携带状态内容；被唤醒方重读投影（§9.4）。"""
        return self._transact(("node_wakeup", {"node_id": node_id, "about": about}))[0]

    def become_team_lead(self, node_id: str, team_id: str,
                         new_route: Optional[ModelRoute] = None,
                         new_role: Optional[NodeRole] = None,
                         move_node: bool = True) -> None:
        """裂变者即 Lead（§7）：拉起 team 的节点登记为该 team 的 Lead。

        move_node=False 用于 root lead 兼任子 Team Lead：只登记指挥关系、不移动
        节点归属（root 控制节点的点数不挤占子 Team 的 50% cap）；worker 裂变
        转子 Lead 时默认移动（§7 嵌套 Team 继承：沿用原节点、不重复计点）。
        new_role=None 保持原 role；按新 model 对原 lease 原子 reweight。"""
        node = self.proj.nodes.get(node_id)
        if node is None:
            raise ControlPlaneError("NO_NODE", f"node {node_id} not found")
        role = new_role or node.role
        pairs: List[Tuple[str, Dict[str, Any]]] = [
            ("node_role_changed", {"node_id": node_id, "new_role": role.value,
                                   "team": team_id, "move_node": move_node}),
        ]
        if new_route is not None and not node.route.same_model(new_route):
            facts = self.catalog.resolve(new_route.provider, new_route.model)
            if facts is None or not facts.available:
                raise AdmissionError("MODEL_UNAVAILABLE", f"{new_route.provider}/{new_route.model}")
            new_route = ModelRoute(**{**new_route.__dict__, "level": facts.level})
            lease = self.proj.leases[node.lease_id]
            pairs.append(("lease_reweight", {
                "lease_id": lease.lease_id, "old_points": lease.points,
                "new_points": new_route.point_weight,
                "delta": new_route.point_weight - lease.points,
            }))
        self._transact(*pairs)

    # ------------------------------------------------------------------
    # 封存三段式（§9.6）与时间护栏（§7）
    # ------------------------------------------------------------------

    def begin_seal(self, team_id: str = "root") -> Event:
        """准入截止：停止一切新准入；同时封死 start_node（item 创建与节点启动是
        同一扇门的两侧）。in-flight finalizing 允许在结算期完成 accepted。"""
        return self._transact(("seal_admission_cutoff", {"team_id": team_id}))[0]

    def begin_settlement(self, team_id: str = "root") -> Event:
        return self._transact(("seal_settlement_started", {"team_id": team_id}))[0]

    def finish_seal(self, team_id: str = "root", timed_out: bool = False) -> Event:
        """结算收尾（§9.6 + P1-6 终局闭环）：在途 peer 消息有界结算（投递完毕
        或记录丢弃）→ root item 终局（正常 = 提交结算快照后 accepted；超时 =
        terminated 上交记录）→ 子树滞留回收 → 相位事件收尾。

        回收循环对每个非终态 item 按序：关通道（dropped 记丢弃）→ drain +
        归还滞留 lease → work_item_terminated("deadline-stopped") → 作废未完成
        successor 登记（§9.3 终态优先）——修 CHANNEL_NOT_CLOSED 卡死 + 终态悬挂
        + 槽位账面永不释放。事件顺序：关通道 → drain → terminated（post 校验
        要求终态落盘时通道已 closed、节点已 drain）。
        回收与 drops 只作用于 team_id 子树（team_subtree_ids）：子 team 封存
        不拔整棵树。终局事件必须先于相位 COMPLETE（invariant：封存完结后拒绝
        accepted/finalizing）。"""
        kind = "seal_timed_out" if timed_out else "seal_completed"
        subtree = self.proj.team_subtree_ids(team_id)

        def _in_subtree(item_id: Optional[str]) -> bool:
            it = self.proj.work_items.get(item_id) if item_id else None
            return it is not None and it.team in subtree

        drops = [mid for mid, m in self.proj.messages.items()
                 if not m.get("delivered")
                 and _in_subtree((self.proj.peer_channels.get(m.get("channel_id")) or {})
                                 .get("item_id"))]
        pairs: List[Tuple[str, Dict[str, Any]]] = []
        handled: set = set()
        # root 终局（仅 root 封存；root item 已终态则跳过——单干路径已 accept）
        if team_id == "root":
            root_item = self.proj.work_items.get(self._root_item_id())
            handled.add(root_item.item_id if root_item else "")
            if root_item is not None and root_item.acceptance is None:
                lead = self.proj.nodes.get(self.proj.teams["root"].lead_node or "") \
                    if self.proj.teams.get("root") else None
                if lead is not None and lead.lifecycle == LifecycleState.ACTIVE:
                    # 通道关闭最前：root item 上 split 合法可达，终局事件的 post
                    # 校验要求通道已 closed（CHANNEL_NOT_CLOSED 卡死修复）
                    pairs += self._channel_close_pairs(root_item.item_id)
                    if timed_out:
                        # 先 drain 后 terminate（结案校验要求：决策 9/10）
                        pairs += self._drain_pairs(root_item.item_id)
                        pairs.append(("work_item_terminated", {
                            "item_id": root_item.item_id, "reason": "deadline-stopped",
                            "summary": "树级 deadline 到期，超时明确失败（§9.6）"}))
                    else:
                        # 结算快照 = root 证据：观测/投影摘要内容寻址落盘
                        summary = json.dumps(self.snapshot(), ensure_ascii=False,
                                             default=str)
                        sha = hashlib.sha256(summary.encode("utf-8")).hexdigest()
                        pkg = f"dep-root-{sha[:8]}"
                        self.write_artifact(summary)
                        pairs.append(("work_item_submitted", {
                            "item_id": root_item.item_id, "attempt": root_item.attempt,
                            "node_id": lead.node_id,
                            "context_epoch": lead.context_epoch,
                            "session_id": lead.session_id,
                            "output_sha256": sha, "package_id": pkg,
                        }))
                        pairs.append(("work_item_finalizing",
                                      {"item_id": root_item.item_id}))
                        pairs.append(("package_stored", {
                            "package_id": pkg, "item_id": root_item.item_id,
                            "content_hash": sha, "content_preview": summary[:200],
                            "source_refs": [], "size": len(summary),
                            "artifact_ref": f"{sha}.txt", "stage": "settlement",
                        }))
                        pairs += self._drain_pairs(root_item.item_id)
                        pairs.append(("work_item_accepted", {
                            "item_id": root_item.item_id, "evidence_ready": True,
                            "package_id": pkg,
                            "accepted_by": {"via": "seal-settlement"},
                        }))
        # 子树滞留回收：非终态 item 关通道 → drain + lease 归还 → terminated
        # → successor 作废（P1-6 + 2.1 终局闭环）。
        # root 上面已处理——staged 事件尚未应用到 self.proj，须显式跳过防重复 drain。
        for it in self.proj.work_items.values():
            if it.item_id in handled or it.team not in subtree:
                continue
            if it.acceptance in (AcceptanceState.ACCEPTED, AcceptanceState.TERMINATED,
                                 AcceptanceState.ESCALATED, AcceptanceState.ABORTED_FINALIZE):
                continue
            pairs += self._channel_close_pairs(it.item_id)
            pairs += self._drain_pairs(it.item_id)
            pairs.append(("work_item_terminated", {
                "item_id": it.item_id, "reason": "deadline-stopped",
                "summary": "封存有界结算期满仍未结案，明确终止不悬挂（§9.6）"}))
            pairs += self._successor_invalidate_pairs(it.item_id)
        pairs.append((kind, {"team_id": team_id, "dropped_messages": drops}))
        return self._transact(*pairs)[0]

    def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """时间护栏巡检（§7 三件套之②③）。

        - 单节点 wall-clock 超时：active 起算（§9.3），超时转 blocked——确定性动作。
          协助者超时不走任务上交（work item 级语义），只 wakeup 主执行者（§7/§9.4）。
        - 树级 deadline：触发即开始封存三段式第一段（§9.6）——确定性动作。
        - 其余观察结论以 watchdog_suggested 事件入链（§9.2：建议，不直接执行）。
        返回建议列表（含已执行动作的回执）。
        """
        now = now if now is not None else time.time()
        actions: List[Dict[str, Any]] = []
        spec = self.proj.spec
        for n in list(self.proj.nodes.values()):
            # 墓碑节点先跳过：node_drained 不改 lifecycle，结案节点 lifecycle 仍
            # ACTIVE，不跳过会对墓碑发 node_blocked（NODE_TERMINATED 穿透）。
            # reweight-wait 节点同样豁免（§9.3：等待点数差额期间为超时抑制
            # 窗口——节点保持等待不启动新模型，超时不得打死）。
            if (n.terminated or n.lifecycle != LifecycleState.ACTIVE
                    or n.blocked != BlockState.NONE or n.reweight_wait):
                continue
            started = n.activated_at  # 投影缓存（node_activated apply 记录），不再反扫日志
            if started is not None and now - started > spec.node_wallclock_timeout:
                self._transact(("node_blocked", {"node_id": n.node_id, "reason": "wallclock-timeout"}))
                actions.append({"action": "node-blocked", "node_id": n.node_id})
                if n.role == NodeRole.ASSISTANT and n.assistant_of:
                    # 依附型节点超时只 wakeup 主执行者（§7）。
                    self._transact(("node_wakeup", {"node_id": n.assistant_of,
                                                    "about": "assistant-timeout"}))
                    actions.append({"action": "wakeup-primary", "node_id": n.assistant_of})
                else:
                    self._transact(("watchdog_suggested", {
                        "node_id": n.node_id, "kind": "attribution-needed",
                        "note": "timeout: Lead 归因后重试（计预算）或上交（§7）"}))
                    actions.append({"action": "watchdog-suggested", "node_id": n.node_id,
                                    "kind": "attribution-needed"})
        if (spec.deadline_seconds is not None and self._root_started_at > 0
                and now - self._root_started_at > spec.deadline_seconds
                and self.proj.seal_phase.get("root", SealPhase.OPEN) == SealPhase.OPEN):
            self.begin_seal("root")
            self.begin_settlement("root")
            actions.append({"action": "deadline-seal", "team_id": "root"})
        # §9.6 三段式③超时兜底：结算不得超过配置上限——SETTLEMENT 超期明确失败
        # （timed_out），不允许树永悬 SETTLEMENT（修复前 tick 只 start 不 finish）。
        # 起算 ts 取 seal_settlement_started 事件 ts 的投影缓存（与 node_activated
        # → activated_at 同一纪律）。
        settle_ts = self.proj.seal_settlement_ts.get("root")
        if (settle_ts is not None
                and self.proj.seal_phase.get("root", SealPhase.OPEN) == SealPhase.SETTLEMENT
                and now - settle_ts > spec.settlement_timeout_seconds):
            self.finish_seal("root", timed_out=True)
            actions.append({"action": "settlement-timeout-seal", "team_id": "root"})
        return actions

    # ------------------------------------------------------------------
    # 人工指令（§9.2）与观测记录（§4 / §6）
    # ------------------------------------------------------------------

    def human_directive(self, directive: HumanDirective) -> Event:
        """三类指令均作为控制面事件入同一事务链，可审计（§9.2）。"""
        if directive.kind == "config":
            spec = RootExecutionSpec(**directive.payload["spec"])
            return self.publish_spec(spec)
        return self._transact(("human_directive", {
            "kind": directive.kind, "payload": directive.payload,
        }))[0]

    def record_observation(self, record: DelegationRecord) -> Event:
        return self._record("observation_recorded", record.to_payload())

    def record_token_usage(self, node_id: str, input_tokens: int = 0, output_tokens: int = 0,
                           cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                           cost_usd: float = 0.0) -> Event:
        """token 账：缓存读写与 input 不相交，单独计账（§4）。
        node_id 必须是已登记节点—— ctx-job:<item_id> 记账账户除外（§7 CM 成本
        记触发方账下，非节点不占点）；拒绝向唯一真源写无主脏观测。"""
        if node_id not in self.proj.nodes and not str(node_id).startswith("ctx-job:"):
            raise ControlPlaneError("NO_NODE", f"node {node_id} not found")
        return self._record("token_usage_recorded", {
            "node_id": node_id, "input": input_tokens, "output": output_tokens,
            "cache_read": cache_read_tokens, "cache_write": cache_write_tokens,
            "cost": cost_usd,
        })

    def record_stop_reason(self, node_id: str, stop_reason: str) -> Event:
        if node_id not in self.proj.nodes:
            raise ControlPlaneError("NO_NODE", f"node {node_id} not found")
        return self._record("stop_reason_recorded", {
            "node_id": node_id, "stop_reason": stop_reason})

    def record_economics(self, item_id: str, lead_tokens: int,
                         estimated_savings: Optional[float]) -> Event:
        """委派经济性（§6）：量"Lead 为完成委派消耗"对"委派节省"，找盈亏线。"""
        return self._record("delegation_economics_recorded", {
            "item_id": item_id, "lead_tokens": lead_tokens,
            "estimated_savings": estimated_savings})

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        p = self.proj
        return {
            "spec_revision": p.spec.revision,
            "work_items": {i: {"kind": w.kind.value, "depth": w.depth,
                               "acceptance": w.acceptance.value if w.acceptance else None,
                               "attempt": w.attempt, "team": w.team}
                           for i, w in p.work_items.items()},
            "nodes": {n.node_id: {"item": n.item_id, "role": n.role.value,
                                  "lifecycle": n.lifecycle.value, "blocked": n.blocked.value,
                                  "epoch": n.context_epoch, "terminated": n.terminated}
                      for n in p.nodes.values()},
            "open_worker_slots_used": p.open_worker_slots_used,
            "active_points": p.active_points,
            "seal_phase": {t: ph.value for t, ph in p.seal_phase.items()},
            "graph_revision": p.graph_revision,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _route_dict(route: ModelRoute) -> Dict[str, Any]:
    return {
        "provider": route.provider, "model": route.model,
        "reasoning_effort": route.reasoning_effort, "level": route.level.value,
        "source": route.source.value, "point_weight": route.point_weight,
    }


# root lead 的占位路由：主 agent 的真实模型由上层在事实注入后声明（§3）。
_ROOT_ROUTE = ModelRoute(provider="dpswarm", model="root-lead", level=Level.S)
