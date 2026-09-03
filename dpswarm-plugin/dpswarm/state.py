"""DPswarm 状态投影层：事件日志是唯一真源，全部状态由回放派生（§9.1）。

对应《DPswarm-机制架构.md》：
- §2.1  RootExecutionSpec 及其 revision 词典（spec_revisions）
- §5.6  状态词汇三层（验收流 / 物理生命周期 / 调度阻塞）落在 WorkItem/Node
- §5.7  验收状态机：本层只落状态，不做合法性判断
- §5.8  successor CAS 登记：(node_id, context_epoch) 唯一
- §7    worker 槽（创建即占、终态释放）、节点点数 lease、子 Team 本地上限
- §9.3  节点两阶段启动（provisioning → active）、rollover 深度保持
- §9.4  通知不带状态：node_wakeup 对投影零作用
- §9.5  分裂对 peer 通道与消息账（queued / delivered / closed）
- §9.6  Team 封存三段式相位（seal_phase）

分层纪律：state.py 只做"投影"——apply_event 是无判断的增量叠加；一切
"合法性"（状态机转换、DAG 环、容量准入、CAS、封存顺序）由 invariants.py
负责：check_event 在投影副本上 pre 检查 → apply_event → post 全图重验。

机制文档歧义处的既定语义决策以"决策 N"标注（N 对应任务书 1-18 条）。
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Set, Tuple

from .events import Event
from .types import (
    AcceptanceState,
    BlockState,
    DelegationKind,
    HumanDirective,
    Lease,
    Level,
    LifecycleState,
    ModelRoute,
    Node,
    NodeRole,
    RootExecutionSpec,
    RouteSource,
    SealPhase,
    StartType,
    Team,
    WorkItem,
    WorkItemOutcome,
)

ROOT_TEAM_ID = "root"
#: 占用 worker 槽的 work item 种类（§7：root item 不占普通槽；split 不产生 item）。
WORKER_ITEM_KINDS: Tuple[DelegationKind, ...] = (DelegationKind.DERIVE, DelegationKind.FISSION)

_SPEC_FIELDS = (
    "max_open_work_items", "max_active_node_points", "subteam_point_ratio",
    "max_depth", "max_team_workers", "max_attempts", "deadline_seconds",
    "node_wallclock_timeout", "settlement_timeout_seconds", "root_acceptance_mode",
    "human_override_always", "permission_config_ref", "point_policy_version",
    "cumulative_budget", "revision", "spec_id",
)


def spec_from_dict(d: Any) -> RootExecutionSpec:
    """root_started / spec_published payload 中的 spec dict → RootExecutionSpec（§2.1）。"""
    if isinstance(d, RootExecutionSpec):
        return d
    if not isinstance(d, dict):
        raise ValueError("spec payload 必须是 dict")
    kwargs = {k: d[k] for k in _SPEC_FIELDS if k in d}
    return RootExecutionSpec(**kwargs)


def route_from_dict(d: Any) -> Optional[ModelRoute]:
    """route dict → ModelRoute。payload 缺省字段按 types.py 默认值补齐（§2）。"""
    if d is None:
        return None
    if isinstance(d, ModelRoute):
        return d
    if not isinstance(d, dict):
        raise ValueError("route payload 必须是 dict")
    return ModelRoute(
        provider=str(d["provider"]),
        model=str(d["model"]),
        reasoning_effort=str(d.get("reasoning_effort", "default")),
        level=Level(d.get("level", "B")),
        source=RouteSource(d.get("source", "lead")),
        point_weight=int(d.get("point_weight", 1)),
    )


class Projection:
    """事件流的内存投影（§9.1：唯一真源是事件日志，本类只是派生视图）。

    必备字段与语义：
    - spec / spec_revisions      当前生效与历史 RootExecutionSpec（§2.1，rev 单调）
    - work_items / nodes / leases  验收单元 / LLM 节点 / 点数 lease（§5.6/§7）
    - teams                      含 "root" team；子 Team 由 fission 建（决策 6）
    - edges                      (before, after) 依赖边集合，DAG（§7/§9.2）
    - graph_revision             图变更 CAS 版本（决策 8）
    - seal_phase                 team_id → 封存相位，缺省 OPEN（§9.6）
    - terminated_item_ids / terminated_node_ids   id 永不复用墓碑（§9.2 决策 18）
    - packages / peer_channels / messages / human_directives   记录类账本

    扩展字段（超出任务书最小集，供控制面/不变量层使用）：
    - root_id                    root_started 落下的 root 标识
    - successor_regs             已登记 successor 的 (node_id, context_epoch) 集合（§5.8）
    - route_resolutions          route_resolved 对账流水（§2）

    派生量 open_worker_slots_used / active_points 以 property 随时从实体状态
    重算（决策 2：槽占/放由 item 终态推导，不单独记账）。
    """

    def __init__(self) -> None:
        self.root_id: Optional[str] = None
        self.spec: RootExecutionSpec = RootExecutionSpec()
        self.spec_revisions: Dict[int, RootExecutionSpec] = {}
        self.work_items: Dict[str, WorkItem] = {}
        self.nodes: Dict[str, Node] = {}
        self.leases: Dict[str, Lease] = {}
        self.teams: Dict[str, Team] = {
            ROOT_TEAM_ID: Team(team_id=ROOT_TEAM_ID, lead_node=None,
                               parent_team=None, local_point_cap=None)
        }
        self.edges: Set[Tuple[str, str]] = set()
        self.graph_revision: int = 0
        self.seal_phase: Dict[str, SealPhase] = {ROOT_TEAM_ID: SealPhase.OPEN}
        # seal_settlement_started 的事件 ts（§9.6 结算超时兜底：tick 直接读投影，
        # 与 node_activated → activated_at 同一缓存纪律）
        self.seal_settlement_ts: Dict[str, float] = {}
        self.terminated_item_ids: Set[str] = set()
        self.terminated_node_ids: Set[str] = set()
        self.packages: Dict[str, Dict[str, Any]] = {}
        self.peer_channels: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.human_directives: List[HumanDirective] = []
        self.successor_regs: Set[Tuple[str, int]] = set()
        self.route_resolutions: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 派生量（§7：占槽 / 点数全部由实体状态推导）
    # ------------------------------------------------------------------

    @property
    def open_worker_slots_used(self) -> int:
        """占槽且未到 accepted/终态的普通 worker item 数（决策 2：创建即占）。"""
        return sum(1 for it in self.work_items.values() if it.holds_worker_slot)

    @property
    def active_points(self) -> int:
        """active lease 点数合计（§7：从准入启动到结案持续占用）。"""
        return sum(l.points for l in self.leases.values() if l.active)

    def team_subtree_ids(self, team_id: str) -> Set[str]:
        """team_id 及其全部子孙 team 集合（§7：子树本地上限口径）。"""
        result = {team_id}
        frontier = [team_id]
        while frontier:
            current = frontier.pop()
            for tid, team in self.teams.items():
                if team.parent_team == current and tid not in result:
                    result.add(tid)
                    frontier.append(tid)
        return result

    def team_effective_cap(self, team_id: str) -> int:
        """team 有效点数上限：root = spec.max_active_node_points（决策 4）。"""
        if team_id == ROOT_TEAM_ID:
            return self.spec.max_active_node_points
        team = self.teams.get(team_id)
        if team is None or team.local_point_cap is None:
            return self.spec.max_active_node_points
        return team.local_point_cap

    def team_subtree_points(self, team_id: str) -> int:
        """该 team 及全部子孙 team 内 active 节点点数（§7：子树占用）。

        尚未 provisioning 的预占 lease（决策 4：lease_acquired 先于
        node_provisioning）team 未知，计入 root 视角。
        """
        subtree = self.team_subtree_ids(team_id)
        total = 0
        for lease in self.leases.values():
            if not lease.active:
                continue
            node = self.nodes.get(lease.node_id)
            if node is not None:
                if node.team in subtree:
                    total += lease.points
            elif ROOT_TEAM_ID in subtree:
                total += lease.points
        return total

    def team_open_workers(self, team_id: str) -> int:
        """该 team 未结案直属 worker item（kind in {derive, fission}）数（§7 决策 6）。"""
        return sum(
            1 for it in self.work_items.values()
            if it.team == team_id
            and it.kind in WORKER_ITEM_KINDS
            and it.holds_worker_slot
        )

    def item_ready(self, item_id: str) -> bool:
        """deps 全部 accepted（§4：accepted 原子发布后才解锁后继）。"""
        item = self.work_items.get(item_id)
        if item is None:
            return False
        for dep in item.deps:
            upstream = self.work_items.get(dep)
            if upstream is None or upstream.acceptance != AcceptanceState.ACCEPTED:
                return False
        return True

    def team_sealed(self, team_id: str) -> bool:
        """team 自身或任一祖先已离开 OPEN 相位（§9.6：seal 作用于对应 team 及其子树）。"""
        current: Optional[str] = team_id
        visited = set()
        while current is not None and current not in visited:
            visited.add(current)
            if self.seal_phase.get(current, SealPhase.OPEN) != SealPhase.OPEN:
                return True
            team = self.teams.get(current)
            current = team.parent_team if team is not None else None
        return False

    def copy(self) -> "Projection":
        """深拷贝（invariants.check_event 在副本上试算，不污染原投影）。"""
        return copy.deepcopy(self)


# ---------------------------------------------------------------------------
# 回放与增量应用
# ---------------------------------------------------------------------------


def replay(events: List[Event]) -> Projection:
    """从零回放事件序列得到投影（§9.1：不做合法性判断，日志已过 append 前校验）。"""
    proj = Projection()
    for event in events:
        apply_event(proj, event)
    return proj


def _seal_team_of(payload: Dict[str, Any]) -> str:
    return payload.get("team") or payload.get("team_id") or ROOT_TEAM_ID


def apply_event(proj: Projection, event: Event) -> None:
    """原地增量应用一个事件；纯投影，不做任何合法性判断。

    未知/纯记录类事件（observation_*、token_usage_*、memory_*、watchdog_*、
    delegation_economics_*、stop_reason_* 等）做最小记录或忽略。
    """
    kind = event.kind
    payload = event.payload if event.payload is not None else {}
    seq = event.seq

    # -- root / spec（§2.1）------------------------------------------------
    if kind == "root_started":
        spec = spec_from_dict(payload["spec"])
        proj.root_id = payload.get("root_id")
        proj.spec = spec
        proj.spec_revisions[spec.revision] = spec
    elif kind == "spec_published":
        spec = spec_from_dict(payload["spec"])
        proj.spec = spec
        proj.spec_revisions[spec.revision] = spec

    # -- work item 验收流（§5.7）------------------------------------------
    elif kind == "work_item_created":
        item_kind = DelegationKind(payload["kind"])
        item = WorkItem(
            item_id=payload["item_id"],
            kind=item_kind,
            parent_item=payload.get("parent_item"),
            team=payload.get("team", ROOT_TEAM_ID),
            depth=int(payload.get("depth", 1)),
            deps=list(payload.get("deps", [])),
            # 决策 2：创建即占槽（含依赖未满足的后继）；root item 不占 worker 槽
            holds_worker_slot=item_kind != DelegationKind.ROOT,
            created_seq=seq,
        )
        item.attempt = 1  # 决策 1：attempt 从 1 开始（首次执行 = 1），创建即预置
        proj.work_items[item.item_id] = item
        for dep in item.deps:
            # 创建时携带的依赖直接建边，不走 CAS、不抬 graph_revision（决策 8 只对
            # work_item_dependency_added 计版本）
            proj.edges.add((dep, item.item_id))
            if dep in proj.work_items:
                proj.work_items[dep].unlock_items.append(item.item_id)
        new_team_id = payload.get("new_team_id")
        if item_kind == DelegationKind.FISSION and new_team_id:
            # 决策 6：fission 同事务建子 Team，local_point_cap = floor(父 cap × ratio)；
            # lead_node 由后续 node_role_changed 设置。首个 fission item 移入新 team
            # （§7 裂变规模按该 team 的直属 worker 计数）。
            parent_cap = proj.team_effective_cap(item.team)
            proj.teams[new_team_id] = Team(
                team_id=new_team_id,
                lead_node=None,
                parent_team=item.team,
                local_point_cap=math.floor(parent_cap * proj.spec.subteam_point_ratio),
            )
            # 首个 fission item 移入新 team（§7 裂变规模按该 team 直属 worker 计）
            item.team = new_team_id
            proj.seal_phase.setdefault(new_team_id, SealPhase.OPEN)
    elif kind == "work_item_dependency_added":
        before, after = payload["before"], payload["after"]
        proj.edges.add((before, after))
        if after in proj.work_items:
            proj.work_items[after].deps.append(before)
        if before in proj.work_items:
            proj.work_items[before].unlock_items.append(after)
        proj.graph_revision += 1  # 决策 8：apply 后 graph_revision+1
    elif kind == "work_item_submitted":
        item = proj.work_items[payload["item_id"]]
        item.acceptance = AcceptanceState.SUBMITTED
        item.attempt = int(payload.get("attempt", item.attempt))
        if payload.get("package_id"):
            item.submission_package_id = payload["package_id"]
        if payload.get("output_sha256"):
            item.submission_sha256 = payload["output_sha256"]
    elif kind == "work_item_finalizing":
        proj.work_items[payload["item_id"]].acceptance = AcceptanceState.FINALIZING
    elif kind == "work_item_accepted":
        item = proj.work_items[payload["item_id"]]
        item.acceptance = AcceptanceState.ACCEPTED
        item.outcome = WorkItemOutcome.ACCEPTED
        item.holds_worker_slot = False  # 决策 2：accepted 释放槽
        proj.terminated_item_ids.add(item.item_id)
    elif kind == "work_item_rejected":
        proj.work_items[payload["item_id"]].acceptance = AcceptanceState.REJECTED
    elif kind == "work_item_retried":
        # 决策 10：retried 是非状态机事件——REJECTED 置回 None 并 attempt+1
        item = proj.work_items[payload["item_id"]]
        item.acceptance = None
        item.attempt = int(payload["attempt"])
    elif kind == "work_item_timeout_retried":
        # §7 时间护栏①：超时重试计预算——只推进 attempt，acceptance 不变
        # （超时发生在执行中，item 尚无裁决；与打回重试的 REJECTED→None 复位不同）
        item = proj.work_items[payload["item_id"]]
        item.attempt = int(payload["attempt"])
    elif kind == "work_item_escalated":
        item = proj.work_items[payload["item_id"]]
        item.acceptance = AcceptanceState.ESCALATED
        item.outcome = WorkItemOutcome.ESCALATED
        item.holds_worker_slot = False
        proj.terminated_item_ids.add(item.item_id)
    elif kind == "work_item_terminated":
        item = proj.work_items[payload["item_id"]]
        item.acceptance = AcceptanceState.TERMINATED
        item.holds_worker_slot = False
        # 决策 16：summary 非空时 = 退化回流摘要（§7 结论落盘、主 agent 只收摘要）
        item.summary = payload.get("summary", "") or ""
        try:
            item.outcome = WorkItemOutcome(payload.get("reason"))
        except ValueError:
            item.outcome = None
        proj.terminated_item_ids.add(item.item_id)
    elif kind == "work_item_aborted_finalize":
        item = proj.work_items[payload["item_id"]]
        item.acceptance = AcceptanceState.ABORTED_FINALIZE
        item.holds_worker_slot = False
        proj.terminated_item_ids.add(item.item_id)

    # -- 节点物理生命周期（§9.3）与通知（§9.4）-----------------------------
    elif kind == "node_provisioning":
        nid = payload["node_id"]
        route = route_from_dict(payload.get("route"))
        level = Level(payload.get("level") or (route.level.value if route else "B"))
        existing = proj.nodes.get(nid)
        if existing is not None:
            # rollover / resume：同 node/item/lease、深度保持（§5.8 决策 3）
            existing.lifecycle = LifecycleState.PROVISIONING
            existing.start_type = StartType(payload.get("start_type", "new"))
            if route is not None:
                existing.route = route
            existing.level = level
            existing.lease_id = payload.get("lease_id", existing.lease_id)
            existing.team = payload.get("team", existing.team)
            existing.context_epoch = int(payload.get("context_epoch", existing.context_epoch))
            existing.session_id = None
            existing.activated_seq = None
            existing.activated_at = None
            existing.package_ref = payload.get("package_ref", existing.package_ref)
            existing.package_hash = payload.get("package_hash", existing.package_hash)
            existing.predecessor_session = payload.get("predecessor_session", existing.predecessor_session)
            if "role" in payload:
                existing.role = NodeRole(payload["role"])
        else:
            proj.nodes[nid] = Node(
                node_id=nid,
                item_id=payload["item_id"],
                role=NodeRole(payload.get("role", "worker")),
                lifecycle=LifecycleState.PROVISIONING,
                route=route,
                level=level,
                lease_id=payload.get("lease_id"),
                team=payload.get("team", ROOT_TEAM_ID),
                context_epoch=int(payload.get("context_epoch", 0)),
                delegation_depth=int(payload.get("delegation_depth", 1)),
                start_type=StartType(payload.get("start_type", "new")),
                assistant_of=payload.get("assistant_of"),
                package_ref=payload.get("package_ref"),
                package_hash=payload.get("package_hash"),
                predecessor_session=payload.get("predecessor_session"),
            )
    elif kind == "node_activated":
        node = proj.nodes[payload["node_id"]]
        node.lifecycle = LifecycleState.ACTIVE
        node.session_id = payload.get("session_id")
        node.activated_seq = seq  # 超时时钟从 active 起算（§9.3）
        node.activated_at = event.ts  # 投影缓存激活时间：tick 直接读，不再反扫日志
    elif kind == "node_failed":
        node = proj.nodes[payload["node_id"]]
        node.lifecycle = LifecycleState.FAILED
        node.terminated = True
        node.reweight_wait = False  # 终态收敛抑制窗口标记（§9.3）
        proj.terminated_node_ids.add(node.node_id)  # id 永不复用（决策 18）
    elif kind == "node_blocked":
        node = proj.nodes[payload["node_id"]]
        node.blocked = BlockState.BLOCKED
        # §7 时间护栏②/§5.8：记录阻塞原因（"wallclock-timeout" 供超时重试核对、
        # "package-fail: ..." 供回退归因），投影只记账不做判断
        node.blocked_reason = str(payload.get("reason", ""))
    elif kind == "node_unblocked":
        node = proj.nodes[payload["node_id"]]
        node.blocked = BlockState.NONE
        node.blocked_reason = ""
    elif kind == "node_drained":
        node = proj.nodes[payload["node_id"]]
        node.terminated = True
        node.reweight_wait = False  # 结案收敛抑制窗口标记（§9.3）
        proj.terminated_node_ids.add(node.node_id)
    elif kind == "node_reweight_wait":
        # §9.3 reweight-wait 抑制窗口：投影只记账布尔（缺省 waiting=True，
        # 旧日志无此事件 = 默认 False，回放容错）
        node = proj.nodes[payload["node_id"]]
        node.reweight_wait = bool(payload.get("waiting", True))
    elif kind == "node_role_changed":
        node = proj.nodes[payload["node_id"]]
        node.role = NodeRole(payload["new_role"])
        team_id = payload.get("team")
        if team_id:
            # 决策 6：node_role_changed 设置 team lead（裂变者即 Lead，§7）。
            # move_node=False 用于 root lead 兼任子 Team Lead：只登记指挥关系，
            # 不移动节点归属——root 控制节点的点数不挤占子 Team 的 50% cap
            # （worker 裂变转 Lead 的场景默认移动，§7 嵌套 Team 继承）。
            if payload.get("move_node", True):
                node.team = team_id
            if team_id in proj.teams:
                proj.teams[team_id].lead_node = node.node_id
    elif kind == "node_wakeup":
        # §9.4 通知不带状态：about 只是提示词，投影无状态变化（决策 13）
        pass

    # -- 资源：点数 lease（§7）---------------------------------------------
    elif kind == "lease_acquired":
        proj.leases[payload["lease_id"]] = Lease(
            lease_id=payload["lease_id"],
            node_id=payload["node_id"],
            points=int(payload["points"]),
            active=True,
        )
    elif kind == "lease_released":
        proj.leases[payload["lease_id"]].active = False
    elif kind == "lease_reweight":
        # 决策 14：换模型重试原 lease 原子 reweight
        proj.leases[payload["lease_id"]].points = int(payload["new_points"])

    # -- 硬切窗口 CAS（§5.8）-----------------------------------------------
    elif kind == "successor_registered":
        key = (payload["node_id"], int(payload["context_epoch"]))
        proj.successor_regs.add(key)
        node = proj.nodes.get(payload["node_id"])
        if node is not None:
            node.successor_reg = key
    elif kind == "successor_reset":
        # 决策 8：三路复位 cause ∈ {activated-success, package-fail-rollback,
        # terminal-invalidate}；对未登记项幂等（恢复路径补复位）
        key = (payload["node_id"], int(payload["context_epoch"]))
        proj.successor_regs.discard(key)
        node = proj.nodes.get(payload["node_id"])
        if node is not None and node.successor_reg == key:
            node.successor_reg = None

    # -- 封存三段式（§9.6）--------------------------------------------------
    elif kind == "seal_admission_cutoff":
        proj.seal_phase[_seal_team_of(payload)] = SealPhase.CUTOFF
    elif kind == "seal_settlement_started":
        proj.seal_phase[_seal_team_of(payload)] = SealPhase.SETTLEMENT
        proj.seal_settlement_ts[_seal_team_of(payload)] = event.ts
    elif kind == "seal_completed":
        team_id = _seal_team_of(payload)
        proj.seal_phase[team_id] = SealPhase.COMPLETED
        if team_id in proj.teams:
            proj.teams[team_id].sealed = True
    elif kind == "seal_timed_out":
        team_id = _seal_team_of(payload)
        proj.seal_phase[team_id] = SealPhase.TIMED_OUT
        if team_id in proj.teams:
            proj.teams[team_id].sealed = True

    # -- 分裂对 peer 通道（§9.5）-------------------------------------------
    elif kind == "peer_channel_opened":
        proj.peer_channels[payload["channel_id"]] = {
            "channel_id": payload["channel_id"],
            "primary_node": payload.get("primary_node"),
            "assistant_node": payload.get("assistant_node"),
            "item_id": payload.get("item_id"),
            "closed": False,
            "opened_seq": seq,
        }
    elif kind == "message_queued":
        proj.messages[payload["message_id"]] = {
            "message_id": payload["message_id"],
            "channel_id": payload.get("channel_id"),
            "from_node": payload.get("from_node"),
            # to_node 由通道两端推导（非 from_node 一侧）；正文字段以 body 为准
            # （control.peer_send 契约），content 作兼容读。
            "body": payload.get("body") if payload.get("body") is not None else payload.get("content"),
            "delivered": False,
            "queued_seq": seq,
        }
    elif kind == "message_delivered":
        proj.messages[payload["message_id"]]["delivered"] = True
    elif kind == "peer_channel_closed":
        channel = proj.peer_channels.get(payload["channel_id"])
        if channel is not None:
            channel["closed"] = True

    # -- 路由对账（§2）------------------------------------------------------
    elif kind == "route_resolved":
        proj.route_resolutions.append(dict(payload))

    # -- 记录类事件：最小记录或忽略 ----------------------------------------
    elif kind == "package_stored":
        package_id = payload.get("package_id")
        if package_id:
            proj.packages[package_id] = dict(payload)
    elif kind == "human_directive":
        proj.human_directives.append(HumanDirective(
            kind=payload.get("kind", "immediate"),
            payload={k: v for k, v in payload.items() if k != "kind"},
        ))
    # observation_recorded / token_usage_recorded / stop_reason_recorded /
    # memory_* / watchdog_suggested / delegation_economics_recorded：
    # 观测与记忆账本不进控制面投影（§4/§5.6），忽略
    else:
        pass
