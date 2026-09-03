"""DPswarm 不变量层：append 前回放校验，非法转换拒绝落盘（§9.1）。

对应《DPswarm-机制架构.md》：
- §5.7  验收状态机转换表（LEGAL_ACCEPTANCE_TRANSITIONS）
- §9.3  节点物理生命周期转换表（LEGAL_LIFECYCLE_TRANSITIONS）与终态优先
- §7    拓扑护栏：深度、槽位、点数容量、级别方向、裂变权限与规模
- §8    attempt 口径与重试预算（首次 + ≤2 重试 = 最多 3 次 attempt）
- §9.2  CAS（expected_graph_revision / successor 唯一登记）、id 永不复用、全图重验
- §9.6  封存三段式线性相位与准入截止
- §2    路由对账：人工指定不得被静默替换
- §9.5  peer 通道：同 item 一主一协助、delivered 去重、closed 后拒新消息

check_event 流程：深拷贝投影 → pre 检查（事件语义合法性）→ apply_event →
post 全图重验（依赖存在 / 无重复边 / 无环 / 基础一致性；准入类事件另验
容量）→ 返回新投影；非法 raise InvariantViolation。原投影永不被污染。

机制文档歧义处的既定语义决策以"决策 N"标注（N 对应任务书 1-18 条）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set, Tuple

from .events import EVENT_KINDS, Event
from .state import ROOT_TEAM_ID, Projection, apply_event, route_from_dict, spec_from_dict
from .types import (
    AcceptanceState,
    BlockState,
    DelegationKind,
    Lease,
    LifecycleState,
    Level,
    Node,
    NodeRole,
    RejectAttribution,
    SealPhase,
    StartType,
    WorkItem,
    WorkItemOutcome,
)


class InvariantViolation(Exception):
    """不变量违规。code 为稳定错误码，供控制面结构化返回（§2 硬准入）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


#: 验收终态集合（决策 10）：任何终态不可再迁移。
TERMINAL_ACCEPTANCE: Set[AcceptanceState] = {
    AcceptanceState.ACCEPTED,
    AcceptanceState.TERMINATED,
    AcceptanceState.ESCALATED,
    AcceptanceState.ABORTED_FINALIZE,
}

#: 验收状态机合法转换（§5.7）。None = 初始态。
#: (REJECTED, None) 专用于 work_item_retried 的复位（决策 10：retried 是
#: 非状态机事件，apply 时把 REJECTED 置回 None 并 attempt+1）。
LEGAL_ACCEPTANCE_TRANSITIONS: Set[Tuple[Optional[AcceptanceState], AcceptanceState]] = {
    (None, AcceptanceState.SUBMITTED),
    (AcceptanceState.SUBMITTED, AcceptanceState.FINALIZING),
    (AcceptanceState.FINALIZING, AcceptanceState.ACCEPTED),      # §4 原子发布
    (AcceptanceState.SUBMITTED, AcceptanceState.REJECTED),
    (AcceptanceState.REJECTED, None),                            # work_item_retried
    (AcceptanceState.SUBMITTED, AcceptanceState.ESCALATED),      # §8 上交
    (AcceptanceState.REJECTED, AcceptanceState.ESCALATED),       # §8 重试耗尽后上交
    (None, AcceptanceState.ESCALATED),   # §7：超时在执行中、item 无裁决，blocked 后须能上交
    (None, AcceptanceState.TERMINATED),                          # §7 退化收回（未提交也可收）
    (AcceptanceState.SUBMITTED, AcceptanceState.TERMINATED),
    (AcceptanceState.REJECTED, AcceptanceState.TERMINATED),
    (AcceptanceState.FINALIZING, AcceptanceState.TERMINATED),    # §9.3 封存期显式终止
    (AcceptanceState.FINALIZING, AcceptanceState.ABORTED_FINALIZE),  # §9.3 显式取消
}

#: 节点物理生命周期合法转换（§9.3）。None = 节点尚未存在。
#: (ACTIVE, PROVISIONING) = rollover/resume 两阶段重启；同 PROVISIONING 重复
#: provisioning 允许（§9.3 崩溃恢复幂等补走）。
LEGAL_LIFECYCLE_TRANSITIONS: Set[Tuple[Optional[LifecycleState], LifecycleState]] = {
    (None, LifecycleState.PROVISIONING),
    (LifecycleState.PROVISIONING, LifecycleState.ACTIVE),
    (LifecycleState.PROVISIONING, LifecycleState.PROVISIONING),
    (LifecycleState.PROVISIONING, LifecycleState.FAILED),
    (LifecycleState.ACTIVE, LifecycleState.FAILED),
    (LifecycleState.ACTIVE, LifecycleState.PROVISIONING),
}

#: 触发"新准入"容量校验的事件（§2.1：容量回退不强杀在途，只停新准入）。
_ADMISSION_KINDS = {"work_item_created", "lease_acquired", "node_provisioning", "lease_reweight"}

_SEAL_ORDER: Dict[str, Tuple[SealPhase, SealPhase]] = {
    "seal_admission_cutoff": (SealPhase.OPEN, SealPhase.CUTOFF),
    "seal_settlement_started": (SealPhase.CUTOFF, SealPhase.SETTLEMENT),
    "seal_completed": (SealPhase.SETTLEMENT, SealPhase.COMPLETED),
    "seal_timed_out": (SealPhase.SETTLEMENT, SealPhase.TIMED_OUT),
}

_SUCCESSOR_RESET_CAUSES = {"activated-success", "package-fail-rollback", "terminal-invalidate"}
_HUMAN_DIRECTIVE_KINDS = {"immediate", "config", "terminal"}


def _v(code: str, message: str) -> InvariantViolation:
    return InvariantViolation(code, message)


def _req(payload: Dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] is None:
        raise _v("BAD_PAYLOAD", f"缺少必填字段 {key}")
    return payload[key]


def _get_item(p: Projection, item_id: str) -> WorkItem:
    item = p.work_items.get(item_id)
    if item is None:
        raise _v("ITEM_UNKNOWN", f"work item 不存在: {item_id}")
    return item


def _get_node(p: Projection, node_id: str) -> Node:
    node = p.nodes.get(node_id)
    if node is None:
        raise _v("NODE_UNKNOWN", f"node 不存在: {node_id}")
    return node


def _get_lease(p: Projection, lease_id: str) -> Lease:
    lease = p.leases.get(lease_id)
    if lease is None:
        raise _v("LEASE_UNKNOWN", f"lease 不存在: {lease_id}")
    return lease


def _acceptance_transition(item: WorkItem, new: Optional[AcceptanceState]) -> None:
    if (item.acceptance, new) not in LEGAL_ACCEPTANCE_TRANSITIONS:
        cur = item.acceptance.value if item.acceptance is not None else "None"
        nxt = new.value if new is not None else "None"
        raise _v("ILLEGAL_TRANSITION", f"{item.item_id}: {cur} -> {nxt} 非法（§5.7）")


def _lifecycle_transition(node: Node, new: LifecycleState) -> None:
    if (node.lifecycle, new) not in LEGAL_LIFECYCLE_TRANSITIONS:
        raise _v("ILLEGAL_TRANSITION",
                 f"{node.node_id}: {node.lifecycle.value} -> {new.value} 非法（§9.3）")


def _path_phase_in(p: Projection, team_id: str, phases: Set[SealPhase]) -> bool:
    """team 自身或任一祖先的封存相位 ∈ phases（§9.6：seal 作用于 team 及其子树）。"""
    current: Optional[str] = team_id
    visited = set()
    while current is not None and current not in visited:
        visited.add(current)
        if p.seal_phase.get(current, SealPhase.OPEN) in phases:
            return True
        team = p.teams.get(current)
        current = team.parent_team if team is not None else None
    return False


def _team_path_sealed(p: Projection, team_id: str) -> bool:
    """离开 OPEN（cutoff 及之后）即视为封存中（决策 11）。"""
    return _path_phase_in(p, team_id, {SealPhase.CUTOFF, SealPhase.SETTLEMENT,
                                       SealPhase.COMPLETED, SealPhase.TIMED_OUT})


# ---------------------------------------------------------------------------
# 全图校验（§9.2：每次变更后对全图重跑，图小时全量重验最不容易漏）
# ---------------------------------------------------------------------------


def verify_graph(proj: Projection) -> None:
    """DAG 无环 + 依赖存在 + 无重复边 + 边/依赖账一致。违规 raise。"""
    deps_of: Dict[str, Set[str]] = {}
    dependents: Dict[str, Set[str]] = {iid: set() for iid in proj.work_items}
    for iid, item in proj.work_items.items():
        seen: Set[str] = set()
        for dep in item.deps:
            if dep not in proj.work_items:
                raise _v("DEP_MISSING", f"{iid} 依赖的 work item 不存在: {dep}")
            if dep in seen:
                raise _v("DUPLICATE_EDGE", f"{iid} 对 {dep} 存在重复依赖")
            seen.add(dep)
        deps_of[iid] = seen
        for dep in seen:
            dependents[dep].add(iid)
    expected_edges = {(dep, iid) for iid, deps in deps_of.items() for dep in deps}
    if proj.edges != expected_edges:
        raise _v("GRAPH_INCONSISTENT", "edges 集合与各 item deps 账不一致")
    # 环检测（Kahn 拓扑排序）
    indegree = {iid: len(deps) for iid, deps in deps_of.items()}
    queue = [iid for iid, d in indegree.items() if d == 0]
    processed = 0
    while queue:
        current = queue.pop()
        processed += 1
        for nxt in dependents[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if processed != len(proj.work_items):
        stuck = sorted(iid for iid, d in indegree.items() if d > 0)
        raise _v("CYCLE", f"依赖图存在环，涉及: {stuck}")


def _verify_consistency(p: Projection) -> None:
    """投影基础一致性（对每个事件后的全图重验，§9.2）。"""
    for node in p.nodes.values():
        if node.item_id not in p.work_items:
            raise _v("ITEM_UNKNOWN", f"node {node.node_id} 挂靠的 work item 不存在: {node.item_id}")
        if node.team not in p.teams:
            raise _v("TEAM_UNKNOWN", f"node {node.node_id} 所属 team 不存在: {node.team}")
        if node.lease_id is not None and node.lease_id not in p.leases:
            raise _v("LEASE_UNKNOWN", f"node {node.node_id} 引用的 lease 不存在: {node.lease_id}")
    for item in p.work_items.values():
        if item.team not in p.teams:
            raise _v("TEAM_UNKNOWN", f"work item {item.item_id} 所属 team 不存在: {item.team}")
        if item.acceptance in TERMINAL_ACCEPTANCE and item.holds_worker_slot:
            raise _v("SLOT_LEAK", f"终态 item 仍占用 worker 槽: {item.item_id}")
    for team in p.teams.values():
        if team.lead_node is not None and team.lead_node not in p.nodes:
            raise _v("NODE_UNKNOWN", f"team {team.team_id} 的 lead 节点不存在: {team.lead_node}")
    for lease in p.leases.values():
        node = p.nodes.get(lease.node_id)
        if node is not None and node.lease_id != lease.lease_id:
            raise _v("LEASE_MISMATCH",
                     f"lease {lease.lease_id} 与 node {node.node_id} 的绑定不一致")
    for channel in p.peer_channels.values():
        for key in ("primary_node", "assistant_node"):
            nid = channel.get(key)
            if nid is not None and nid not in p.nodes:
                raise _v("NODE_UNKNOWN", f"peer 通道引用的节点不存在: {nid}")


def _verify_admission(p: Projection) -> None:
    """容量准入（§7）：仅对新准入类事件校验（§2.1 容量回退不强杀在途）。"""
    if p.open_worker_slots_used > p.spec.max_open_work_items:
        raise _v("SLOT_EXCEEDED",
                 f"开放 worker 槽 {p.open_worker_slots_used} > maxOpenWorkItems "
                 f"{p.spec.max_open_work_items}（§7）")
    if p.active_points > p.spec.max_active_node_points:
        raise _v("POINTS_EXCEEDED",
                 f"active 点数 {p.active_points} > maxActiveNodePoints "
                 f"{p.spec.max_active_node_points}（§7 决策 4）")
    for team_id, team in p.teams.items():
        if team_id == ROOT_TEAM_ID or team.local_point_cap is None:
            continue  # root 上限即全局点数上限，已校验（决策 4）
        used = p.team_subtree_points(team_id)
        if used > team.local_point_cap:
            raise _v("TEAM_POINT_CAP",
                     f"team {team_id} 子树点数 {used} > 本地上限 {team.local_point_cap}（§7）")
    for team_id in p.teams:
        workers = p.team_open_workers(team_id)
        if workers > p.spec.max_team_workers:
            raise _v("TEAM_WORKERS_EXCEEDED",
                     f"team {team_id} 直属未结案 worker {workers} > maxTeamWorkers "
                     f"{p.spec.max_team_workers}（§7 决策 6）")


def _post_item_closed(p: Projection, item_id: str, require_nodes_drained: bool) -> None:
    """结案后校验：同 item 无 active 节点、无 active lease（§4/决策 9、10）；
    peer 通道必须已关闭（§9.5：通道随 work item 进入任一终态关闭，不止绑 accepted）。"""
    if require_nodes_drained:
        live = [
            node.node_id for node in p.nodes.values()
            if node.item_id == item_id and not node.terminated
            and node.lifecycle in (LifecycleState.PROVISIONING, LifecycleState.ACTIVE)
        ]
        if live:
            raise _v("NODES_NOT_DRAINED",
                     f"item {item_id} 结案但仍有未 drain 节点: {live}（决策 9/10）")
    held = [
        lease.lease_id for lease in p.leases.values()
        if lease.active
        and (node := p.nodes.get(lease.node_id)) is not None
        and node.item_id == item_id
    ]
    if held:
        raise _v("LEASE_NOT_RELEASED",
                 f"item {item_id} 结案但 lease 未释放: {held}（决策 9/10）")
    open_channels = [
        ch.get("channel_id") for ch in p.peer_channels.values()
        if not ch.get("closed") and ch.get("item_id") == item_id
    ]
    if open_channels:
        raise _v("CHANNEL_NOT_CLOSED",
                 f"item {item_id} 已终态但 peer 通道未关闭: {open_channels}（§9.5）")


# ---------------------------------------------------------------------------
# pre 检查：按事件 kind 分发
# ---------------------------------------------------------------------------


def _pre_root_started(p: Projection, payload: Dict[str, Any]) -> None:
    if p.root_id is not None or p.work_items or p.nodes:
        raise _v("ROOT_ALREADY_STARTED", "root 已启动，root_started 不可重复（§2.1）")
    try:
        spec_from_dict(_req(payload, "spec"))
    except Exception as exc:
        raise _v("BAD_PAYLOAD", f"spec 解析失败: {exc}")


def _pre_spec_published(p: Projection, payload: Dict[str, Any]) -> None:
    try:
        spec = spec_from_dict(_req(payload, "spec"))
    except Exception as exc:
        raise _v("BAD_PAYLOAD", f"spec 解析失败: {exc}")
    if spec.revision <= p.spec.revision:
        raise _v("SPEC_REVISION",
                 f"spec revision {spec.revision} 未单调递增（当前 {p.spec.revision}，§2.1）")


def _pre_work_item_created(p: Projection, payload: Dict[str, Any]) -> None:
    item_id = _req(payload, "item_id")
    if item_id in p.work_items or item_id in p.terminated_item_ids:
        raise _v("ID_REUSE", f"work item id 永不复用: {item_id}（§9.2 决策 18）")
    try:
        kind = DelegationKind(_req(payload, "kind"))
    except ValueError:
        raise _v("BAD_PAYLOAD", f"kind 非法: {payload.get('kind')}")
    if kind == DelegationKind.SPLIT:
        raise _v("BAD_PAYLOAD", "split 不产生新 work item（§7 决策 3）")
    team_id = payload.get("team", ROOT_TEAM_ID)
    if team_id not in p.teams:
        raise _v("TEAM_UNKNOWN", f"team 不存在: {team_id}")
    depth = int(payload.get("depth", 1))
    deps = payload.get("deps", []) or []
    for dep in deps:
        if dep not in p.work_items:
            raise _v("DEP_MISSING", f"{item_id} 创建时依赖的 work item 不存在: {dep}")
    # 深度按 agent 层级（决策 3）：root=1；derive/fission = parent+1，≤ max_depth
    if kind == DelegationKind.ROOT:
        if depth != 1 or payload.get("parent_item") is not None:
            raise _v("DEPTH_MISMATCH", "root item 必须 depth=1 且无 parent（决策 3）")
        if team_id != ROOT_TEAM_ID:
            raise _v("BAD_PAYLOAD", "root item 必须属于 root team")
    else:
        parent_id = payload.get("parent_item")
        if parent_id is not None:
            if parent_id not in p.work_items:
                raise _v("PARENT_MISSING", f"parent work item 不存在: {parent_id}")
            base = p.work_items[parent_id].depth
        else:
            base = 1  # 直接挂在主 agent（第 1 层）之下
        if depth != base + 1:
            raise _v("DEPTH_MISMATCH",
                     f"{item_id} depth={depth}，期望 {base + 1}（= parent+1，决策 3）")
        if depth > p.spec.max_depth:
            raise _v("DEPTH_EXCEEDED",
                     f"{item_id} depth={depth} > maxDepth={p.spec.max_depth}（§7 决策 3）")
    # 裂变权限（§7 决策 6）：parent 的执行节点（该 team lead）必须 == S 级
    if kind == DelegationKind.FISSION:
        lead_id = p.teams[team_id].lead_node
        if lead_id is None or lead_id not in p.nodes:
            raise _v("FISSION_FORBIDDEN", f"team {team_id} 无已登记 lead，无法证明 S 级（§7）")
        if p.nodes[lead_id].level != Level.S:
            raise _v("FISSION_FORBIDDEN",
                     f"裂变权限仅 S 级，lead {lead_id} 为 {p.nodes[lead_id].level.value}（§7）")
        new_team_id = payload.get("new_team_id")
        if new_team_id is not None and new_team_id in p.teams:
            raise _v("TEAM_EXISTS", f"team 已存在: {new_team_id}")
    if _team_path_sealed(p, team_id):
        raise _v("SEALED_ADMISSION",
                 f"team {team_id} 已封存（cutoff 及之后），拒绝新 work item（§9.6 决策 11）")


def _pre_work_item_dependency_added(p: Projection, payload: Dict[str, Any]) -> None:
    before = _req(payload, "before")
    after = _req(payload, "after")
    if before not in p.work_items:
        raise _v("DEP_MISSING", f"依赖的 work item 不存在: {before}")
    if after not in p.work_items:
        raise _v("DEP_MISSING", f"被依赖的 work item 不存在: {after}")
    expected = payload.get("expected_graph_revision")
    if expected != p.graph_revision:
        raise _v("CAS_MISMATCH",
                 f"expected_graph_revision={expected} != 当前 {p.graph_revision}（§9.2 决策 8）")
    if before == after or (before, after) in p.edges:
        raise _v("DUPLICATE_EDGE", f"重复依赖边: {before} -> {after}（§9.2）")
    if _team_path_sealed(p, p.work_items[after].team):
        raise _v("SEALED_ADMISSION", "已封存，拒绝新增依赖边（§9.6 决策 11）")


def _pre_work_item_submitted(p: Projection, payload: Dict[str, Any]) -> None:
    item = _get_item(p, _req(payload, "item_id"))
    node = _get_node(p, _req(payload, "node_id"))
    attempt = int(payload.get("attempt", item.attempt))
    if attempt != item.attempt:
        raise _v("ATTEMPT_MISMATCH",
                 f"submitted attempt={attempt} != 当前 {item.attempt}（决策 1）")
    _acceptance_transition(item, AcceptanceState.SUBMITTED)
    if node.item_id != item.item_id:
        raise _v("NODE_ITEM_MISMATCH",
                 f"node {node.node_id} 挂靠 {node.item_id}，不是 {item.item_id} 的执行者")
    if node.terminated:
        raise _v("NODE_TERMINATED", f"节点已终止: {node.node_id}")
    if node.lifecycle != LifecycleState.ACTIVE:
        raise _v("NODE_NOT_ACTIVE", f"节点未 active: {node.node_id}（§9.3）")
    if node.assistant_of is not None:
        raise _v("ASSISTANT_SUBMIT",
                 f"协助者 {node.node_id} 依附主执行者，不独立提交验收（§7 决策 7）")
    # fence（P1-2）：旧 session 禁写——epoch/session 与节点当前值不符即拒。
    # 未携带时放行（同进程单写者自查场景；server 层对 dsh 路径强制携带）。
    f_epoch = payload.get("context_epoch")
    if f_epoch is not None and int(f_epoch) != node.context_epoch:
        raise _v("FENCE_VIOLATION",
                 f"提交 context_epoch={f_epoch} != 节点当前 {node.context_epoch}"
                 f"（旧 session 禁写：rollover 后该提交者已失效）")
    f_sess = payload.get("session_id")
    if f_sess and node.session_id and f_sess != node.session_id:
        raise _v("FENCE_VIOLATION",
                 f"提交 session 与节点当前 session 不符（旧 session 禁写）")
    # 决策 15：SETTLEMENT 期仍允许已准入操作完成；封存完结后不再接受
    if _path_phase_in(p, item.team, {SealPhase.COMPLETED, SealPhase.TIMED_OUT}):
        raise _v("SEALED_ADMISSION", "封存已完结，拒绝 submitted（§9.6）")


def _pre_work_item_finalizing(p: Projection, payload: Dict[str, Any]) -> None:
    item = _get_item(p, _req(payload, "item_id"))
    _acceptance_transition(item, AcceptanceState.FINALIZING)  # 只能从 SUBMITTED 来（决策 9）
    if _path_phase_in(p, item.team, {SealPhase.COMPLETED, SealPhase.TIMED_OUT}):
        raise _v("SEALED_ADMISSION", "封存已完结，拒绝 finalizing（§9.6）")


def _pre_work_item_accepted(p: Projection, payload: Dict[str, Any]) -> None:
    item = _get_item(p, _req(payload, "item_id"))
    _acceptance_transition(item, AcceptanceState.ACCEPTED)  # 只能从 FINALIZING 来（决策 9）
    if payload.get("evidence_ready") is not True:
        raise _v("EVIDENCE_NOT_READY", "accepted 要求 evidence_ready == True（§4 决策 9）")
    # §4 第 2/3 步竞态保证："accepted 对外可见时依赖材料已可读"——
    # package 必须先经 package_stored 落盘（evidence_ready 不允许自证）。
    if payload.get("package_id") not in p.packages:
        raise _v("PACKAGE_NOT_STORED",
                 f"accepted 引用的 package {payload.get('package_id')} 未落盘（§4）")
    if _path_phase_in(p, item.team, {SealPhase.COMPLETED, SealPhase.TIMED_OUT}):
        raise _v("SEALED_ADMISSION", "封存已完结，拒绝 accepted（§9.6）")


def _pre_work_item_rejected(p: Projection, payload: Dict[str, Any]) -> None:
    item = _get_item(p, _req(payload, "item_id"))
    _acceptance_transition(item, AcceptanceState.REJECTED)
    attribution = payload.get("attribution")
    if attribution is not None:
        try:
            RejectAttribution(attribution)
        except ValueError:
            raise _v("BAD_PAYLOAD", f"attribution 非法: {attribution}（§8）")
    if _path_phase_in(p, item.team, {SealPhase.COMPLETED, SealPhase.TIMED_OUT}):
        raise _v("SEALED_ADMISSION", "封存已完结，拒绝 rejected（§9.6）")


def _pre_work_item_retried(p: Projection, payload: Dict[str, Any]) -> None:
    item = _get_item(p, _req(payload, "item_id"))
    _acceptance_transition(item, None)  # 仅 REJECTED 可复位（决策 10）
    new_attempt = int(_req(payload, "attempt"))
    if new_attempt != item.attempt + 1:
        raise _v("ATTEMPT_MISMATCH",
                 f"retried attempt={new_attempt} != 当前+1（{item.attempt + 1}，决策 1）")
    if new_attempt > p.spec.max_attempts:
        raise _v("ATTEMPT_EXHAUSTED",
                 f"attempt {new_attempt} 超出重试预算 max_attempts={p.spec.max_attempts}（§8）")
    if _path_phase_in(p, item.team, {SealPhase.COMPLETED, SealPhase.TIMED_OUT}):
        raise _v("SEALED_ADMISSION", "封存已完结，拒绝 retried（§9.6）")


def _pre_work_item_timeout_retried(p: Projection, payload: Dict[str, Any]) -> None:
    """§7 时间护栏①：超时重试（计预算）的 pre 检查。

    与打回重试（work_item_retried，仅 REJECTED 可复位）不同：超时发生在
    执行中——item 尚无裁决，acceptance 必须仍为 None；attempt 推进走同一
    §8 预算口径（首次 + ≤2 重试）。item 名下必须存在超时转 blocked 的节点，
    防止该事件被用来凭空刷 attempt。
    """
    item = _get_item(p, _req(payload, "item_id"))
    if item.acceptance is not None:
        raise _v("ITEM_NOT_RUNNING",
                 f"{item.item_id} acceptance={item.acceptance.value}，超时重试只适用于"
                 f"执行中（acceptance is None）的 item（§7 时间护栏①）")
    new_attempt = int(_req(payload, "attempt"))
    if new_attempt != item.attempt + 1:
        raise _v("ATTEMPT_MISMATCH",
                 f"timeout retried attempt={new_attempt} != 当前+1（{item.attempt + 1}，§8）")
    if new_attempt > p.spec.max_attempts:
        raise _v("ATTEMPT_EXHAUSTED",
                 f"attempt {new_attempt} 超出重试预算 max_attempts={p.spec.max_attempts}（§8）")
    if _path_phase_in(p, item.team, {SealPhase.COMPLETED, SealPhase.TIMED_OUT}):
        raise _v("SEALED_ADMISSION", "封存已完结，拒绝 timeout retried（§9.6）")
    timeout_blocked = [
        n.node_id for n in p.nodes.values()
        if n.item_id == item.item_id and not n.terminated
        and n.blocked == BlockState.BLOCKED
        and n.blocked_reason == "wallclock-timeout"
    ]
    if not timeout_blocked:
        raise _v("NO_TIMEOUT_BLOCKED",
                 f"item {item.item_id} 名下无 wallclock-timeout blocked 节点"
                 f"（§7：先由 tick 将超时节点转 blocked）")


def _pre_work_item_escalated(p: Projection, payload: Dict[str, Any]) -> None:
    item = _get_item(p, _req(payload, "item_id"))
    _acceptance_transition(item, AcceptanceState.ESCALATED)


def _pre_work_item_terminated(p: Projection, payload: Dict[str, Any]) -> None:
    item = _get_item(p, _req(payload, "item_id"))
    _acceptance_transition(item, AcceptanceState.TERMINATED)
    # §4 第二层六值词汇：reason 必须是合法 WorkItemOutcome——落盘前拒非法值，
    # 防再次静默断账（磁盘上既有旧日志的容错在 state.apply_event，回放不受此限）
    try:
        WorkItemOutcome(payload.get("reason"))
    except ValueError:
        raise _v("BAD_PAYLOAD",
                 f"terminate reason 非法: {payload.get('reason')!r}（§4 六值词汇）")
    # 决策 16：summary 仅记录（退化回流摘要），无额外校验


def _pre_work_item_aborted_finalize(p: Projection, payload: Dict[str, Any]) -> None:
    item = _get_item(p, _req(payload, "item_id"))
    _acceptance_transition(item, AcceptanceState.ABORTED_FINALIZE)  # 只能从 FINALIZING 来（决策 9）


def _pre_node_provisioning(p: Projection, payload: Dict[str, Any]) -> None:
    node_id = _req(payload, "node_id")
    item = _get_item(p, _req(payload, "item_id"))
    try:
        role = NodeRole(_req(payload, "role"))
    except ValueError:
        raise _v("BAD_PAYLOAD", f"role 非法: {payload.get('role')}")
    # 决策 4：context manager 不是节点（§7），不允许 provisioning
    if role == NodeRole.CONTEXT_MANAGER:
        raise _v("CONTEXT_MANAGER_NOT_NODE", "context manager 按需瞬时调用，不是节点（§7 决策 4）")
    try:
        start_type = StartType(payload.get("start_type", "new"))
    except ValueError:
        raise _v("BAD_PAYLOAD", f"start_type 非法: {payload.get('start_type')}")
    # id 永不复用（§9.2 决策 18）：墓碑检查最先做——终止节点无法再取得有效
    # lease，若后置会被 LEASE_INACTIVE 掩盖真实原因
    tombstone = p.nodes.get(node_id)
    if tombstone is not None and tombstone.terminated:
        raise _v("ID_REUSE", f"node id 永不复用: {node_id}（§9.2 决策 18）")
    if node_id in p.terminated_node_ids:
        raise _v("ID_REUSE", f"node id 永不复用: {node_id}（§9.2 决策 18）")
    # 决策 4：lease 先于 provisioning 取得，且必须绑定本节点
    lease = _get_lease(p, _req(payload, "lease_id"))
    if lease.node_id != node_id:
        raise _v("LEASE_MISMATCH", f"lease {lease.lease_id} 未绑定节点 {node_id}")
    if not lease.active:
        raise _v("LEASE_INACTIVE", f"lease {lease.lease_id} 已释放（§7）")
    try:
        route = route_from_dict(payload.get("route")) if payload.get("route") is not None else None
    except Exception as exc:
        raise _v("BAD_PAYLOAD", f"route 解析失败: {exc}")
    try:
        level = Level(payload.get("level") or (route.level.value if route else "B"))
    except ValueError:
        raise _v("BAD_PAYLOAD", f"level 非法: {payload.get('level')}")

    assistant_of = payload.get("assistant_of")
    if role == NodeRole.ASSISTANT and assistant_of is None:
        raise _v("BAD_PAYLOAD", "ASSISTANT 必须携带 assistant_of（§7 分裂主从）")
    if assistant_of is not None:
        primary = _get_node(p, assistant_of)
        if primary.item_id != item.item_id:
            raise _v("NODE_ITEM_MISMATCH", "协助者必须与其主同 work item（§7 决策 7）")
        if primary.assistant_of is not None:
            raise _v("ASSISTANT_RELATION", "协助者的主不能也是协助者（§7）")

    existing = p.nodes.get(node_id)
    if existing is not None:
        # rollover / resume 场景（决策 3、18）
        if existing.terminated or node_id in p.terminated_node_ids:
            raise _v("ID_REUSE", f"node id 永不复用: {node_id}（§9.2 决策 18）")
        if start_type == StartType.NEW:
            raise _v("ID_REUSE", f"节点已存在，NEW 启动不得复用 id: {node_id}（决策 18）")
        if existing.item_id != item.item_id:
            raise _v("NODE_ITEM_MISMATCH", "rollover 必须同 node/item/lease（§5.8）")
        if int(payload.get("delegation_depth", existing.delegation_depth)) != existing.delegation_depth:
            raise _v("DEPTH_MISMATCH",
                     f"rollover/resume 必须保持 delegation_depth={existing.delegation_depth}（决策 3）")
        team_id = payload.get("team") or existing.team
        if payload.get("team") is not None and payload["team"] != existing.team:
            raise _v("TEAM_MISMATCH", "rollover 不得变更 team（§5.8）")
        _lifecycle_transition(existing, LifecycleState.PROVISIONING)
    else:
        if node_id in p.terminated_node_ids:
            raise _v("ID_REUSE", f"node id 永不复用: {node_id}（§9.2 决策 18）")
        if start_type != StartType.NEW:
            raise _v("BAD_PAYLOAD", "rollover/resume 要求节点已存在（§5.8/§9.3）")
        # §4 解锁后继：deps 全部 accepted 才准启动新建节点（硬门禁，代码层强制）。
        # 仅限新建分支——rollover/resume 分支不查：依赖在首启时已满足，而
        # accepted 是终态不可逆，续接窗口不应被已经满足过的依赖卡住。
        if item.deps and not p.item_ready(item.item_id):
            blocked_deps = [d for d in item.deps
                            if p.work_items.get(d) is None
                            or p.work_items[d].acceptance != AcceptanceState.ACCEPTED]
            raise _v("DEPS_NOT_READY",
                     f"work item {item.item_id} 的依赖未全部 accepted: {blocked_deps}"
                     f"（§4：accepted 才解锁后继）")
        team_id = payload.get("team") or item.team
        if team_id != item.team:
            raise _v("TEAM_MISMATCH", "节点 team 必须与 work item team 一致（§7）")
        dd = payload.get("delegation_depth")
        if dd is None:
            raise _v("BAD_PAYLOAD", "NEW 启动必须携带 delegation_depth（决策 3）")
        dd = int(dd)
        if role == NodeRole.ROOT_LEAD:
            if dd != 1:
                raise _v("DEPTH_MISMATCH", "root lead node delegation_depth 必须为 1（决策 3）")
        else:
            # NEW 启动 = 父节点 delegation_depth+1；协助者的父 = 其主（split 同层语义
            # 下的物理 child，§7），普通 worker 的父 = team lead。父未登记时无法核对，跳过。
            parent_id = assistant_of or p.teams[team_id].lead_node
            parent = p.nodes.get(parent_id) if parent_id else None
            if parent is not None and dd != parent.delegation_depth + 1:
                raise _v("DEPTH_MISMATCH",
                         f"delegation_depth={dd}，期望 {parent.delegation_depth + 1}（决策 3）")
    if team_id not in p.teams:
        raise _v("TEAM_UNKNOWN", f"team 不存在: {team_id}")
    # 级别方向（§7 决策 5）：worker/assistant ≤ team lead；human_override 跳过；
    # ROOT_LEAD 无此校验。
    if role in (NodeRole.WORKER, NodeRole.ASSISTANT) and not payload.get("human_override", False):
        lead_id = p.teams[team_id].lead_node
        if lead_id is not None and lead_id in p.nodes:
            if level.rank > p.nodes[lead_id].level.rank:
                raise _v("LEVEL_DIRECTION",
                         f"{level.value} 级 worker/assistant 超过 lead "
                         f"{p.nodes[lead_id].level.value} 级（§7 决策 5）")
    if item.acceptance in TERMINAL_ACCEPTANCE:
        raise _v("TERMINAL_PRIORITY", f"item {item.item_id} 已终态，不得再启动节点（§9.3 决策 15）")
    if _team_path_sealed(p, team_id):
        raise _v("SEALED_ADMISSION",
                 f"team {team_id} 已封存，拒绝节点启动（§9.6 决策 11）")


def _pre_node_activated(p: Projection, payload: Dict[str, Any]) -> None:
    node = _get_node(p, _req(payload, "node_id"))
    if node.terminated:
        raise _v("TERMINAL_PRIORITY", f"节点已终止: {node.node_id}（§9.3 决策 15）")
    # blocked 节点拒绝激活（修复链 1.2，须在 rollback_rollover 现场复原之后落地——
    # 复原前 confirm 是 rollback 后唯一事实通路）：超时/预装失败的 blocked 节点
    # 须先走归因处置（重试/上交/复原），不得直接激活
    if node.blocked != BlockState.NONE:
        raise _v("NODE_BLOCKED",
                 f"节点 {node.node_id} 处于 blocked（{node.blocked_reason}），拒绝激活（§5.8/§7）")
    _lifecycle_transition(node, LifecycleState.ACTIVE)
    item = p.work_items.get(node.item_id)
    # 决策 15：终态优先——item TERMINAL 或封存后，PROVISIONING 节点不得再 active
    if item is not None and item.acceptance in TERMINAL_ACCEPTANCE:
        raise _v("TERMINAL_PRIORITY",
                 f"item {item.item_id} 已终态，不得再激活节点（§9.3 决策 15）")
    if _team_path_sealed(p, node.team):
        raise _v("SEALED_ADMISSION",
                 f"team {node.team} 已封存，cutoff 后不得再激活节点（§9.6 决策 15）")


def _pre_node_failed(p: Projection, payload: Dict[str, Any]) -> None:
    # 决策 15：node_failed 恒合法（封存/终态后仍可失败收尾），仅要求节点存在且未终止
    node = _get_node(p, _req(payload, "node_id"))
    if node.terminated:
        raise _v("NODE_TERMINATED", f"节点已终止，不能再次 failed: {node.node_id}")
    _lifecycle_transition(node, LifecycleState.FAILED)


def _pre_node_blocked(p: Projection, payload: Dict[str, Any]) -> None:
    node = _get_node(p, _req(payload, "node_id"))
    if node.terminated:
        raise _v("NODE_TERMINATED", f"节点已终止: {node.node_id}")


def _pre_node_unblocked(p: Projection, payload: Dict[str, Any]) -> None:
    node = _get_node(p, _req(payload, "node_id"))
    if node.terminated:
        raise _v("NODE_TERMINATED", f"节点已终止: {node.node_id}")


def _pre_node_drained(p: Projection, payload: Dict[str, Any]) -> None:
    node = _get_node(p, _req(payload, "node_id"))
    if node.terminated:
        raise _v("NODE_TERMINATED", f"节点已终止，不能重复 drain: {node.node_id}")


def _pre_node_role_changed(p: Projection, payload: Dict[str, Any]) -> None:
    node = _get_node(p, _req(payload, "node_id"))
    if node.terminated:
        raise _v("NODE_TERMINATED", f"节点已终止: {node.node_id}")
    try:
        new_role = NodeRole(_req(payload, "new_role"))
    except ValueError:
        raise _v("BAD_PAYLOAD", f"new_role 非法: {payload.get('new_role')}")
    if new_role == NodeRole.CONTEXT_MANAGER:
        raise _v("CONTEXT_MANAGER_NOT_NODE", "context manager 不是节点（§7 决策 4）")
    team_id = payload.get("team")
    if team_id is not None and team_id not in p.teams:
        raise _v("TEAM_UNKNOWN", f"team 不存在: {team_id}")


def _pre_node_wakeup(p: Projection, payload: Dict[str, Any]) -> None:
    # 决策 13：about 只是提示词（§9.4 通知不带状态），仅要求节点存在
    _get_node(p, _req(payload, "node_id"))


def _pre_node_reweight_wait(p: Projection, payload: Dict[str, Any]) -> None:
    """§9.3 抑制窗口标记：节点存在且未终止（终止侧由 drain/failed 投影收敛，
    不需要退出事件）；waiting 缺省 True，重复进/出幂等。"""
    node = _get_node(p, _req(payload, "node_id"))
    if node.terminated:
        raise _v("NODE_TERMINATED", f"节点已终止: {node.node_id}")


def _pre_lease_acquired(p: Projection, payload: Dict[str, Any]) -> None:
    lease_id = _req(payload, "lease_id")
    if lease_id in p.leases:
        raise _v("LEASE_EXISTS", f"lease 已存在: {lease_id}（id 永不复用）")
    points = payload.get("points")
    if not isinstance(points, int) or isinstance(points, bool) or points <= 0:
        raise _v("BAD_PAYLOAD", f"points 必须为正整数: {points}")
    node = p.nodes.get(_req(payload, "node_id"))
    if node is not None:
        if node.terminated:
            raise _v("NODE_TERMINATED", f"节点已终止，不得取得 lease: {node.node_id}")
        if _team_path_sealed(p, node.team):
            raise _v("SEALED_ADMISSION", "已封存，拒绝新 lease（§9.6 决策 11）")
    # 节点尚未 provisioning（决策 4：lease 先行）时 team 未知，封存检查由
    # 后续 node_provisioning 兜底


def _pre_lease_released(p: Projection, payload: Dict[str, Any]) -> None:
    lease = _get_lease(p, _req(payload, "lease_id"))
    if not lease.active:
        raise _v("LEASE_INACTIVE", f"lease 已释放: {lease.lease_id}")


def _pre_lease_reweight(p: Projection, payload: Dict[str, Any]) -> None:
    lease = _get_lease(p, _req(payload, "lease_id"))
    if not lease.active:
        raise _v("LEASE_INACTIVE", f"lease 已释放: {lease.lease_id}")
    if int(payload.get("old_points", -1)) != lease.points:
        raise _v("REWEIGHT_MISMATCH",
                 f"old_points={payload.get('old_points')} != 当前 {lease.points}（决策 14）")
    new_points = payload.get("new_points")
    if not isinstance(new_points, int) or isinstance(new_points, bool) or new_points <= 0:
        raise _v("BAD_PAYLOAD", f"new_points 必须为正整数: {new_points}")


def _pre_successor_registered(p: Projection, payload: Dict[str, Any]) -> None:
    node = _get_node(p, _req(payload, "node_id"))
    if node.terminated:
        raise _v("NODE_TERMINATED", f"节点已终止: {node.node_id}")
    epoch = int(_req(payload, "context_epoch"))
    if epoch != node.context_epoch:
        raise _v("EPOCH_MISMATCH",
                 f"epoch={epoch} != 节点当前 {node.context_epoch}（§5.8 CAS）")
    if (node.node_id, epoch) in p.successor_regs:
        raise _v("DOUBLE_SUCCESSOR",
                 f"(node={node.node_id}, epoch={epoch}) 已登记 successor（§5.8 决策 8）")
    # §5.8 登记原子提交契约：capsule 引用与 hash 必须随 CAS 登记原子携带
    _req(payload, "capsule_ref")
    _req(payload, "capsule_hash")
    if _team_path_sealed(p, node.team):
        raise _v("SEALED_ADMISSION", "已封存，拒绝 successor 登记（§9.6 决策 11）")


def _pre_successor_reset(p: Projection, payload: Dict[str, Any]) -> None:
    _get_node(p, _req(payload, "node_id"))
    cause = payload.get("cause")
    if cause not in _SUCCESSOR_RESET_CAUSES:
        raise _v("BAD_PAYLOAD",
                 f"successor_reset cause 必须属于 {_SUCCESSOR_RESET_CAUSES}（决策 8）")
    # 对未登记项幂等放行（崩溃恢复补复位）


def _pre_seal(p: Projection, payload: Dict[str, Any], kind: str) -> None:
    team_id = payload.get("team") or payload.get("team_id") or ROOT_TEAM_ID
    if team_id not in p.teams:
        raise _v("TEAM_UNKNOWN", f"team 不存在: {team_id}")
    want, new = _SEAL_ORDER[kind]
    current = p.seal_phase.get(team_id, SealPhase.OPEN)
    if current != want:
        raise _v("SEAL_ORDER",
                 f"team {team_id} 相位 {current.value}，{kind} 要求 {want.value}"
                 f"（CUTOFF→SETTLEMENT→COMPLETED/TIMED_OUT 线性，§9.6 决策 11）")


def _pre_peer_channel_opened(p: Projection, payload: Dict[str, Any]) -> None:
    channel_id = _req(payload, "channel_id")
    if channel_id in p.peer_channels:
        raise _v("CHANNEL_EXISTS", f"通道已存在: {channel_id}")
    primary = _get_node(p, _req(payload, "primary_node"))
    assistant = _get_node(p, _req(payload, "assistant_node"))
    item = _get_item(p, _req(payload, "item_id"))
    if primary.terminated or assistant.terminated:
        raise _v("NODE_TERMINATED", "分裂对节点须存活（§9.5）")
    if primary.item_id != item.item_id or assistant.item_id != item.item_id:
        raise _v("CHANNEL_ITEM_MISMATCH", "通道两节点必须同 work item（§9.5 决策 17）")
    if primary.assistant_of is not None or assistant.assistant_of != primary.node_id:
        raise _v("ASSISTANT_RELATION",
                 f"必须一主（{primary.node_id}）一协助（{assistant.node_id}，assistant_of 指向主）（决策 17）")
    # 分裂规模 1 主 1 副（§7）：主执行者同 item 下不得已有未关闭通道。
    for ch in p.peer_channels.values():
        if (not ch.get("closed") and ch.get("item_id") == item.item_id
                and ch.get("channel_id") != channel_id):
            raise _v("SPLIT_PAIR_ONLY",
                     f"分裂规模 1 主 1 副（§7）：item {item.item_id} 已有未关闭通道 "
                     f"{ch.get('channel_id')}")


def _pre_message_queued(p: Projection, payload: Dict[str, Any]) -> None:
    message_id = _req(payload, "message_id")
    if message_id in p.messages:
        raise _v("MESSAGE_EXISTS", f"消息 id 已存在: {message_id}")
    channel = p.peer_channels.get(_req(payload, "channel_id"))
    if channel is None:
        raise _v("CHANNEL_UNKNOWN", f"通道不存在: {payload.get('channel_id')}")
    if channel.get("closed"):
        raise _v("CHANNEL_CLOSED", "通道已关闭，不再接受新消息（§9.5 决策 17）")
    # 相位检查（§9.6）：通道所属 team 路径封存完结后拒新消息；CUTOFF/SETTLEMENT
    # 期仍允许——有界结算"等待已准入操作完成"
    item = p.work_items.get(channel.get("item_id"))
    if item is not None and _path_phase_in(p, item.team,
                                           {SealPhase.COMPLETED, SealPhase.TIMED_OUT}):
        raise _v("SEALED_ADMISSION", "封存已完结，拒绝新消息（§9.6）")


def _pre_message_delivered(p: Projection, payload: Dict[str, Any]) -> None:
    message = p.messages.get(_req(payload, "message_id"))
    if message is None:
        raise _v("MESSAGE_UNKNOWN", f"消息不存在: {payload.get('message_id')}")
    if message.get("delivered"):
        raise _v("DUPLICATE_DELIVERY", f"消息重复投递: {payload.get('message_id')}（§9.5 决策 17）")


def _pre_peer_channel_closed(p: Projection, payload: Dict[str, Any]) -> None:
    channel = p.peer_channels.get(_req(payload, "channel_id"))
    if channel is None:
        raise _v("CHANNEL_UNKNOWN", f"通道不存在: {payload.get('channel_id')}")
    if channel.get("closed"):
        raise _v("CHANNEL_ALREADY_CLOSED", f"通道已关闭: {payload.get('channel_id')}")


def _pre_route_resolved(p: Projection, payload: Dict[str, Any]) -> None:
    _get_node(p, _req(payload, "node_id"))
    source = payload.get("source")
    if source not in ("lead", "human"):
        raise _v("BAD_PAYLOAD", f"source 非法: {source}（§2）")
    if source == "human":
        # 决策 12：人工优先，不得静默替换（§2）——比较实际选择三要素
        proposed = payload.get("proposed") or {}
        resolved = payload.get("resolved") or {}
        pick = lambda d: (d.get("provider"), d.get("model"), d.get("reasoning_effort"))
        if pick(proposed) != pick(resolved):
            raise _v("SILENT_OVERRIDE",
                     "人工指定路由被静默替换: proposed != resolved（§2 决策 12）")


def _pre_human_directive(p: Projection, payload: Dict[str, Any]) -> None:
    if payload.get("kind") not in _HUMAN_DIRECTIVE_KINDS:
        raise _v("BAD_PAYLOAD",
                 f"human_directive kind 必须属于 {_HUMAN_DIRECTIVE_KINDS}（§9.2）")


_PRE_CHECKS: Dict[str, Callable[[Projection, Dict[str, Any]], None]] = {
    "root_started": _pre_root_started,
    "spec_published": _pre_spec_published,
    "work_item_created": _pre_work_item_created,
    "work_item_dependency_added": _pre_work_item_dependency_added,
    "work_item_submitted": _pre_work_item_submitted,
    "work_item_finalizing": _pre_work_item_finalizing,
    "work_item_accepted": _pre_work_item_accepted,
    "work_item_rejected": _pre_work_item_rejected,
    "work_item_retried": _pre_work_item_retried,
    "work_item_timeout_retried": _pre_work_item_timeout_retried,
    "work_item_escalated": _pre_work_item_escalated,
    "work_item_terminated": _pre_work_item_terminated,
    "work_item_aborted_finalize": _pre_work_item_aborted_finalize,
    "node_provisioning": _pre_node_provisioning,
    "node_activated": _pre_node_activated,
    "node_failed": _pre_node_failed,
    "node_blocked": _pre_node_blocked,
    "node_unblocked": _pre_node_unblocked,
    "node_drained": _pre_node_drained,
    "node_role_changed": _pre_node_role_changed,
    "node_wakeup": _pre_node_wakeup,
    "node_reweight_wait": _pre_node_reweight_wait,
    "lease_acquired": _pre_lease_acquired,
    "lease_released": _pre_lease_released,
    "lease_reweight": _pre_lease_reweight,
    "successor_registered": _pre_successor_registered,
    "successor_reset": _pre_successor_reset,
    "seal_admission_cutoff": lambda p, pl: _pre_seal(p, pl, "seal_admission_cutoff"),
    "seal_settlement_started": lambda p, pl: _pre_seal(p, pl, "seal_settlement_started"),
    "seal_completed": lambda p, pl: _pre_seal(p, pl, "seal_completed"),
    "seal_timed_out": lambda p, pl: _pre_seal(p, pl, "seal_timed_out"),
    "peer_channel_opened": _pre_peer_channel_opened,
    "message_queued": _pre_message_queued,
    "message_delivered": _pre_message_delivered,
    "peer_channel_closed": _pre_peer_channel_closed,
    "route_resolved": _pre_route_resolved,
    "human_directive": _pre_human_directive,
    # package_stored / observation_* / token_usage_* / stop_reason_* / memory_* /
    # watchdog_suggested / delegation_economics_recorded：纯记录，无 pre 检查
}


def _post_check(p: Projection, event: Event) -> None:
    """post 全图重验（§9.2：图变更后全量重跑，不只验增量）。"""
    verify_graph(p)
    _verify_consistency(p)
    if event.kind in _ADMISSION_KINDS:
        # §2.1：新 revision 容量低于占用时不强杀在途，只停新准入——
        # 因此容量上限只对准入类事件校验
        _verify_admission(p)
    kind = event.kind
    if kind in ("work_item_accepted", "work_item_escalated", "work_item_terminated"):
        # 决策 9/10：结案后同 item 无 active 节点、无 active lease、槽已释放
        _post_item_closed(p, event.payload["item_id"], require_nodes_drained=True)
    elif kind == "work_item_aborted_finalize":
        # 决策 9：aborted 释放 lease（证据保留、不解锁后继）
        _post_item_closed(p, event.payload["item_id"], require_nodes_drained=False)


def check_event(proj: Projection, event: Event) -> Projection:
    """在 proj 的深拷贝上：pre 检查 → apply_event → post 全图重验 → 返回新投影。

    非法事件 raise InvariantViolation，原投影不受影响（§9.1 append 前校验）。
    """
    if event.kind not in EVENT_KINDS:
        raise _v("UNKNOWN_EVENT", f"未知事件类型: {event.kind}")
    candidate = proj.copy()
    pre = _PRE_CHECKS.get(event.kind)
    if pre is not None:
        pre(candidate, event.payload if event.payload is not None else {})
    apply_event(candidate, event)
    _post_check(candidate, event)
    return candidate
