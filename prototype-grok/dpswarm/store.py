"""事件日志、投影与回放。事件是唯一真源；内存视图只是投影。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .types import (
    AcceptanceState,
    ArchivePhase,
    BlockedState,
    ChoiceSource,
    ContextPackage,
    Level,
    MemoryStatus,
    NodeRole,
    PhysicalState,
    RootExecutionSpec,
    Route,
    SeedMode,
    StartKind,
    StopReason,
    TerminalReason,
)


class ControlError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.message = message
        self.details = details
        super().__init__(f"{code}: {message}")


class AdmissionError(ControlError):
    """硬准入拒绝：返回结构化原因，禁止静默改选。"""


@dataclass
class Event:
    seq: int
    type: str
    ts: int
    payload: dict
    flushed: bool = False

    def as_json(self) -> str:
        return json.dumps(
            {"seq": self.seq, "type": self.type, "ts": self.ts, "payload": self.payload},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )


class EventLog:
    """独立存储（§9.1），不依附任何 agent session。append-and-flush 后才成功。"""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.flushed_seq: int = 0
        self._jsonl: list[str] = []
        self.before_flush: Optional[Callable[[list[Event]], None]] = None
        self.after_flush: Optional[Callable[[list[Event]], None]] = None

    def append_and_flush(self, events: list[Event]) -> None:
        if not events:
            return
        if self.before_flush:
            self.before_flush(events)
        self.events.extend(events)
        for ev in events:
            ev.flushed = True
            self._jsonl.append(ev.as_json())
        self.flushed_seq = events[-1].seq
        if self.after_flush:
            self.after_flush(events)

    def replay(self) -> "Projection":
        proj = Projection()
        for ev in self.events:
            apply_event(proj, ev)
        return proj


@dataclass
class NodeState:
    node_id: str
    role: NodeRole
    work_item_id: Optional[str]
    team_id: str
    lead_node_id: str
    layer: int
    physical_depth: int
    acceptance: AcceptanceState = AcceptanceState.NONE
    physical: PhysicalState = PhysicalState.PROVISIONING
    blocked: BlockedState = BlockedState.NONE
    route: Optional[Route] = None
    proposed_route: Optional[Route] = None
    resolved_route: Optional[Route] = None
    choice_source: ChoiceSource = ChoiceSource.LEAD
    human_override_level: bool = False
    policy_version: str = ""
    point_weight: int = 0
    lease_id: Optional[str] = None
    start_kind: StartKind = StartKind.NEW
    context_epoch: int = 0
    session_id: Optional[str] = None
    predecessor_session_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    package_hash: Optional[str] = None
    fence_token: int = 0
    assistant_of: Optional[str] = None
    attempt: int = 1
    retries_used: int = 0
    window_usage: float = 0.0
    active_at_ms: Optional[int] = None
    timeout_suppressed: bool = False
    successor_registered: bool = False
    successor_epoch: Optional[int] = None
    capsule_hash: Optional[str] = None
    capsule_ready: bool = False
    package_ready: bool = False
    required_prefetched: bool = False
    evidence_hash: Optional[str] = None
    package_revision: Optional[str] = None
    stop_reason: Optional[StopReason] = None
    accepted_by: Optional[dict] = None
    rejected_by: Optional[dict] = None
    last_attribution: Optional[str] = None
    llm_calls: int = 0
    window_cleaned: bool = False
    evidence_retained: bool = False
    portrait_eligible: bool = True
    session_lineage: list[str] = field(default_factory=list)


@dataclass
class WorkItemState:
    work_item_id: str
    parent_work_item_id: Optional[str]
    team_id: str
    layer: int
    kind: str
    primary_node_id: str
    assistant_node_id: Optional[str] = None
    status: AcceptanceState = AcceptanceState.NONE
    terminal_reason: Optional[TerminalReason] = None
    attempt: int = 1
    retries_used: int = 0
    predecessors: list[str] = field(default_factory=list)
    successors: list[str] = field(default_factory=list)
    peer_channel_id: Optional[str] = None
    slot_lease_id: Optional[str] = None
    unlocked: bool = True
    split_primary: bool = False
    pending_route: Optional[Route] = None
    pending_weight: int = 0
    pending_choice_source: str = "lead"
    pending_human_override: bool = False


@dataclass
class TeamState:
    team_id: str
    parent_team_id: Optional[str]
    lead_node_id: str
    local_point_cap: int
    spec_id: str = ""
    spec_revision: int = 1


@dataclass
class LeaseState:
    lease_id: str
    kind: str  # slot | points
    amount: int
    owner_node_id: Optional[str]
    owner_work_item_id: Optional[str]
    released: bool = False
    policy_version: str = ""


@dataclass
class PeerChannel:
    channel_id: str
    work_item_id: str
    primary_node_id: str
    assistant_node_id: str
    closed: bool = False
    queued: list[dict] = field(default_factory=list)
    delivered: list[dict] = field(default_factory=list)
    discarded: list[dict] = field(default_factory=list)


@dataclass
class MemoryItem:
    memory_id: str
    status: MemoryStatus
    source_node_id: str
    kind: str  # accepted | failure_finding
    scope: str = "root"
    supersedes: Optional[str] = None
    confirmed: bool = False


@dataclass
class EvidenceItem:
    evidence_id: str
    node_id: str
    kind: str
    in_default_retrieval: bool
    retained: bool = True
    hash: str = ""


@dataclass
class PortraitBucket:
    success: int = 0
    fail: int = 0
    skipped_reasons: list[str] = field(default_factory=list)


@dataclass
class TokenBook:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class Suggestion:
    suggestion_id: str
    kind: str
    payload: dict
    consumed: bool = False


@dataclass
class Notification:
    """§9.4 只报告有变化或超时，不携带状态内容。"""

    kind: str
    target_id: str
    ts: int


@dataclass
class Projection:
    root_id: Optional[str] = None
    spec_id: str = "root-spec"
    spec_revision: int = 0
    spec: Optional[RootExecutionSpec] = None
    graph_revision: int = 0
    clock_ms: int = 0
    deadline_at: Optional[int] = None
    created_at: int = 0
    archive_phase: ArchivePhase = ArchivePhase.NONE
    admission_cutoff: bool = False
    cutoff_reason: Optional[str] = None
    teams: dict[str, TeamState] = field(default_factory=dict)
    work_items: dict[str, WorkItemState] = field(default_factory=dict)
    nodes: dict[str, NodeState] = field(default_factory=dict)
    leases: dict[str, LeaseState] = field(default_factory=dict)
    used_ids: set[str] = field(default_factory=set)
    edges: list[tuple[str, str]] = field(default_factory=list)
    packages: dict[str, ContextPackage] = field(default_factory=dict)
    channels: dict[str, PeerChannel] = field(default_factory=dict)
    memories: dict[str, MemoryItem] = field(default_factory=dict)
    evidence: list[EvidenceItem] = field(default_factory=list)
    portraits: dict[str, PortraitBucket] = field(default_factory=dict)
    tokens: dict[str, TokenBook] = field(default_factory=dict)
    ops_audit: list[dict] = field(default_factory=list)
    failure_audit: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)
    human_instructions: list[dict] = field(default_factory=list)
    cm_inflight: int = 0
    cm_jobs: dict[str, str] = field(default_factory=dict)
    cumulative_spent: dict[str, int] = field(default_factory=lambda: {"amount": 0, "tokens": 0, "calls": 0})
    waiters_notified_at_flush: list[int] = field(default_factory=list)
    last_error: Optional[dict] = None
    root_lead_id: Optional[str] = None
    seq: int = 0

    def copy(self) -> "Projection":
        return copy.deepcopy(self)

    def spec_payload(self) -> dict:
        assert self.spec is not None
        return self.spec.without_runtime()

    def open_worker_slots(self) -> int:
        n = 0
        for wi in self.work_items.values():
            if wi.kind != "worker":
                continue
            if wi.status in (
                AcceptanceState.ACCEPTED,
                AcceptanceState.TERMINATED,
                AcceptanceState.ESCALATED,
                AcceptanceState.ABORTED_FINALIZE,
            ):
                continue
            n += 1
        return n

    def points_used(self, team_id: Optional[str] = None) -> int:
        total = 0
        for node in self.nodes.values():
            if node.acceptance in (
                AcceptanceState.ACCEPTED,
                AcceptanceState.TERMINATED,
                AcceptanceState.ESCALATED,
                AcceptanceState.ABORTED_FINALIZE,
            ):
                continue
            if node.lease_id is None:
                continue
            lease = self.leases.get(node.lease_id)
            if lease is None or lease.released:
                continue
            if team_id is not None and not self._node_in_team_subtree(node, team_id):
                continue
            total += node.point_weight
        return total

    def _node_in_team_subtree(self, node: NodeState, team_id: str) -> bool:
        tid = node.team_id
        seen = set()
        while tid:
            if tid == team_id:
                return True
            if tid in seen:
                break
            seen.add(tid)
            team = self.teams.get(tid)
            if team is None:
                break
            tid = team.parent_team_id or ""
        return False

    def live_direct_workers(self, team_id: str) -> int:
        count = 0
        team = self.teams[team_id]
        for wi in self.work_items.values():
            if wi.team_id != team_id or wi.kind != "worker":
                continue
            if wi.primary_node_id == team.lead_node_id:
                continue
            if wi.status in (
                AcceptanceState.ACCEPTED,
                AcceptanceState.TERMINATED,
                AcceptanceState.ESCALATED,
                AcceptanceState.ABORTED_FINALIZE,
            ):
                continue
            node = self.nodes.get(wi.primary_node_id)
            if node and node.role == NodeRole.LEAD and node.node_id == team.lead_node_id:
                continue
            count += 1
        return count

    def in_flight_nodes_for_item(self, work_item_id: str) -> list[str]:
        out = []
        for node in self.nodes.values():
            if node.work_item_id != work_item_id:
                continue
            if node.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE):
                out.append(node.node_id)
        return out

    def injection_capacity(self) -> dict:
        spec = self.spec
        assert spec is not None
        used = self.points_used()
        return {
            "total": spec.max_active_node_points,
            "used": used,
            "available": spec.max_active_node_points - used,
            "openWorkItems": self.open_worker_slots(),
            "maxOpenWorkItems": spec.max_open_work_items,
        }


def apply_event(proj: Projection, ev: Event) -> None:
    t = ev.type
    p = ev.payload
    proj.seq = ev.seq
    proj.clock_ms = max(proj.clock_ms, ev.ts)
    handler = _HANDLERS.get(t)
    if handler is None:
        raise ControlError("UNKNOWN_EVENT", t)
    handler(proj, p, ev)


def _set_id(proj: Projection, ident: str) -> None:
    proj.used_ids.add(ident)


def _h_root_created(proj: Projection, p: dict, ev: Event) -> None:
    proj.root_id = p["root_id"]
    proj.spec_id = p["spec_id"]
    proj.spec_revision = p["spec_revision"]
    proj.spec = p["spec"]
    proj.created_at = ev.ts
    if proj.spec and proj.spec.deadline_enabled:
        proj.deadline_at = ev.ts + proj.spec.deadline_ms
    _set_id(proj, p["root_id"])
    _set_id(proj, p["spec_id"])


def _release_node_resources(proj: Projection, node: NodeState, *, item_slot: bool) -> None:
    if node.lease_id and node.lease_id in proj.leases:
        proj.leases[node.lease_id].released = True
    if item_slot:
        wi = proj.work_items.get(node.work_item_id or "")
        if wi and wi.slot_lease_id and wi.slot_lease_id in proj.leases:
            proj.leases[wi.slot_lease_id].released = True


def _h_spec_published(proj: Projection, p: dict, ev: Event) -> None:
    proj.spec_revision = p["spec_revision"]
    proj.spec = p["spec"]
    if proj.spec and proj.spec.deadline_enabled:
        proj.deadline_at = proj.created_at + proj.spec.deadline_ms
    for team in proj.teams.values():
        team.spec_id = proj.spec_id
        team.spec_revision = proj.spec_revision


def _h_graph_bumped(proj: Projection, p: dict, ev: Event) -> None:
    proj.graph_revision = p["graph_revision"]


def _h_team_created(proj: Projection, p: dict, ev: Event) -> None:
    _set_id(proj, p["team_id"])
    proj.teams[p["team_id"]] = TeamState(
        team_id=p["team_id"],
        parent_team_id=p.get("parent_team_id"),
        lead_node_id=p["lead_node_id"],
        local_point_cap=p["local_point_cap"],
        spec_id=proj.spec_id,
        spec_revision=proj.spec_revision,
    )


def _h_work_item_created(proj: Projection, p: dict, ev: Event) -> None:
    _set_id(proj, p["work_item_id"])
    proj.work_items[p["work_item_id"]] = WorkItemState(
        work_item_id=p["work_item_id"],
        parent_work_item_id=p.get("parent_work_item_id"),
        team_id=p["team_id"],
        layer=p["layer"],
        kind=p["kind"],
        primary_node_id=p["primary_node_id"],
        predecessors=list(p.get("predecessors") or []),
        unlocked=p.get("unlocked", True),
        split_primary=p.get("split_primary", False),
        slot_lease_id=p.get("slot_lease_id"),
        pending_route=p.get("pending_route"),
        pending_weight=p.get("pending_weight", 0),
        pending_choice_source=p.get("pending_choice_source", "lead"),
        pending_human_override=p.get("pending_human_override", False),
    )


def _h_dag_edge(proj: Projection, p: dict, ev: Event) -> None:
    edge = (p["src"], p["dst"])
    if edge not in proj.edges:
        proj.edges.append(edge)
    src = proj.work_items[p["src"]]
    dst = proj.work_items[p["dst"]]
    if p["dst"] not in src.successors:
        src.successors.append(p["dst"])
    if p["src"] not in dst.predecessors:
        dst.predecessors.append(p["src"])
    dst.unlocked = all(
        proj.work_items[pred].status == AcceptanceState.ACCEPTED for pred in dst.predecessors
    )


def _h_lease_acquired(proj: Projection, p: dict, ev: Event) -> None:
    _set_id(proj, p["lease_id"])
    proj.leases[p["lease_id"]] = LeaseState(
        lease_id=p["lease_id"],
        kind=p["kind"],
        amount=p["amount"],
        owner_node_id=p.get("owner_node_id"),
        owner_work_item_id=p.get("owner_work_item_id"),
        policy_version=p.get("policy_version", ""),
    )


def _h_lease_released(proj: Projection, p: dict, ev: Event) -> None:
    lease = proj.leases[p["lease_id"]]
    lease.released = True


def _h_lease_reweighted(proj: Projection, p: dict, ev: Event) -> None:
    lease = proj.leases[p["lease_id"]]
    lease.amount = p["new_amount"]
    node = proj.nodes[p["node_id"]]
    node.point_weight = p["new_amount"]
    node.policy_version = p.get("policy_version", node.policy_version)


def _h_node_provisioning(proj: Projection, p: dict, ev: Event) -> None:
    nid = p["node_id"]
    _set_id(proj, nid)
    if nid in proj.nodes:
        node = proj.nodes[nid]
        node.physical = PhysicalState.PROVISIONING
        node.start_kind = StartKind(p["start_kind"])
        node.package_hash = p.get("package_hash")
        node.fence_token = p["fence_token"]
        node.context_epoch = p["context_epoch"]
        node.predecessor_session_id = p.get("predecessor_session_id")
        node.checkpoint_id = p.get("checkpoint_id")
        node.timeout_suppressed = p["start_kind"] in ("rollover", "wakeup") or node.blocked == BlockedState.REWEIGHT_WAIT
        if p.get("route"):
            node.route = p["route"]
            node.resolved_route = p["route"]
        if p.get("lease_id"):
            node.lease_id = p["lease_id"]
    else:
        node = NodeState(
            node_id=nid,
            role=NodeRole(p["role"]),
            work_item_id=p.get("work_item_id"),
            team_id=p["team_id"],
            lead_node_id=p["lead_node_id"],
            layer=p["layer"],
            physical_depth=p["physical_depth"],
            route=p.get("route"),
            proposed_route=p.get("proposed_route"),
            resolved_route=p.get("resolved_route") or p.get("route"),
            choice_source=ChoiceSource(p.get("choice_source", "lead")),
            human_override_level=p.get("human_override_level", False),
            policy_version=p.get("policy_version", ""),
            point_weight=p.get("point_weight", 0),
            lease_id=p.get("lease_id"),
            start_kind=StartKind(p["start_kind"]),
            context_epoch=p.get("context_epoch", 0),
            package_hash=p.get("package_hash"),
            fence_token=p.get("fence_token", 0),
            assistant_of=p.get("assistant_of"),
            attempt=p.get("attempt", 1),
            timeout_suppressed=p["start_kind"] in ("rollover", "wakeup"),
            predecessor_session_id=p.get("predecessor_session_id"),
            checkpoint_id=p.get("checkpoint_id"),
            required_prefetched=p.get("required_prefetched", False),
            package_ready=p.get("package_ready", False),
        )
        proj.nodes[nid] = node
    if p.get("role") == "lead" and p.get("is_root"):
        proj.root_lead_id = nid


def _h_node_activated(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.physical = PhysicalState.ACTIVE
    node.blocked = BlockedState.NONE
    node.timeout_suppressed = False
    node.session_id = p["session_id"]
    node.active_at_ms = ev.ts
    node.session_lineage.append(p["session_id"])
    if node.successor_registered and node.start_kind == StartKind.ROLLOVER:
        pass


def _h_node_failed(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.physical = PhysicalState.FAILED
    node.blocked = BlockedState.RECOVERY


def _h_node_drained(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.physical = PhysicalState.DRAINED
    node.successor_registered = False
    node.successor_epoch = None


def _h_node_blocked(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.blocked = BlockedState(p["blocked"])
    if p.get("timeout_suppressed") is not None:
        node.timeout_suppressed = p["timeout_suppressed"]


def _h_node_submitted(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.acceptance = AcceptanceState.SUBMITTED
    node.stop_reason = StopReason(p["stop_reason"]) if p.get("stop_reason") else StopReason.COMPLETED
    wi = proj.work_items.get(node.work_item_id or "")
    if wi and node.role != NodeRole.ASSISTANT:
        wi.status = AcceptanceState.SUBMITTED


def _h_node_rejected(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.acceptance = AcceptanceState.REJECTED
    node.rejected_by = p.get("rejected_by")
    node.last_attribution = p.get("attribution")
    wi = proj.work_items.get(node.work_item_id or "")
    if wi:
        wi.status = AcceptanceState.REJECTED
    proj.evidence.append(
        EvidenceItem(
            evidence_id=p.get("evidence_id", f"ev-rej-{node.node_id}-{ev.seq}"),
            node_id=node.node_id,
            kind="rejected",
            in_default_retrieval=False,
            hash=p.get("hash", ""),
        )
    )
    proj.failure_audit.append(
        {
            "node_id": node.node_id,
            "attribution": p.get("attribution"),
            "reason": p.get("reason"),
            "negative_sample": p.get("negative_sample", True),
        }
    )


def _h_node_finalizing(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.acceptance = AcceptanceState.FINALIZING
    node.accepted_by = p.get("accepted_by")
    wi = proj.work_items.get(node.work_item_id or "")
    if wi:
        wi.status = AcceptanceState.FINALIZING


def _h_evidence_committed(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.evidence_hash = p["evidence_hash"]
    node.evidence_retained = True
    proj.evidence.append(
        EvidenceItem(
            evidence_id=p["evidence_id"],
            node_id=node.node_id,
            kind=p.get("kind", "accepted_raw"),
            in_default_retrieval=False,
            hash=p["evidence_hash"],
        )
    )


def _h_package_committed(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.package_revision = p["package_revision"]


def _h_node_accepted(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.acceptance = AcceptanceState.ACCEPTED
    wi_id = node.work_item_id
    if wi_id:
        wi = proj.work_items[wi_id]
        if node.role != NodeRole.ASSISTANT:
            wi.status = AcceptanceState.ACCEPTED
            wi.terminal_reason = TerminalReason.ACCEPTED
            for succ_id in wi.successors:
                succ = proj.work_items[succ_id]
                succ.unlocked = all(
                    proj.work_items[pred].status == AcceptanceState.ACCEPTED for pred in succ.predecessors
                )
            _release_node_resources(proj, node, item_slot=True)
        else:
            _release_node_resources(proj, node, item_slot=False)
    else:
        _release_node_resources(proj, node, item_slot=False)


def _h_node_terminated(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.acceptance = AcceptanceState.TERMINATED
    node.portrait_eligible = False
    wi_id = node.work_item_id
    if wi_id and node.role != NodeRole.ASSISTANT:
        wi = proj.work_items[wi_id]
        wi.status = AcceptanceState.TERMINATED
        wi.terminal_reason = TerminalReason(p.get("terminal_reason", "terminated"))
        _release_node_resources(proj, node, item_slot=True)
    else:
        _release_node_resources(proj, node, item_slot=False)


def _h_aborted_finalize(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.acceptance = AcceptanceState.ABORTED_FINALIZE
    node.evidence_retained = True
    wi_id = node.work_item_id
    if wi_id:
        wi = proj.work_items[wi_id]
        wi.status = AcceptanceState.ABORTED_FINALIZE
        wi.terminal_reason = TerminalReason.MANUAL_STOPPED
        _release_node_resources(proj, node, item_slot=True)
    else:
        _release_node_resources(proj, node, item_slot=False)


def _h_item_escalated(proj: Projection, p: dict, ev: Event) -> None:
    wi = proj.work_items[p["work_item_id"]]
    wi.status = AcceptanceState.ESCALATED
    wi.terminal_reason = TerminalReason.ESCALATED
    node = proj.nodes[wi.primary_node_id]
    node.acceptance = AcceptanceState.ESCALATED
    node.portrait_eligible = False
    _release_node_resources(proj, node, item_slot=True)


def _h_attempt_recorded(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.attempt = p["attempt"]
    node.retries_used = p["retries_used"]
    node.acceptance = AcceptanceState.NONE
    if p.get("route"):
        node.route = p["route"]
        node.resolved_route = p["route"]
    wi = proj.work_items.get(node.work_item_id or "")
    if wi:
        wi.attempt = p["attempt"]
        wi.retries_used = p["retries_used"]
        wi.status = AcceptanceState.NONE


def _h_successor_registered(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.successor_registered = True
    node.successor_epoch = p["context_epoch"]


def _h_successor_cleared(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.successor_registered = False
    node.successor_epoch = None


def _h_capsule_preloaded(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.capsule_hash = p.get("package_hash")
    node.capsule_ready = bool(p.get("success"))
    if not p.get("success"):
        node.blocked = BlockedState.RECOVERY
        node.capsule_ready = False


def _h_package_assembled(proj: Projection, p: dict, ev: Event) -> None:
    pkg = p["package"]
    proj.packages[pkg.package_id] = pkg
    node = proj.nodes.get(p.get("node_id", ""))
    if node:
        node.package_hash = pkg.content_hash


def _h_required_prefetched(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes.get(p["node_id"])
    if node:
        node.required_prefetched = True
        node.package_ready = True
    pkg = proj.packages.get(p.get("package_id", ""))
    if pkg:
        for e in pkg.entries:
            if e.required:
                e.prefetched = True


def _h_peer_opened(proj: Projection, p: dict, ev: Event) -> None:
    _set_id(proj, p["channel_id"])
    proj.channels[p["channel_id"]] = PeerChannel(
        channel_id=p["channel_id"],
        work_item_id=p["work_item_id"],
        primary_node_id=p["primary_node_id"],
        assistant_node_id=p["assistant_node_id"],
    )
    wi = proj.work_items[p["work_item_id"]]
    wi.peer_channel_id = p["channel_id"]
    wi.assistant_node_id = p["assistant_node_id"]
    wi.split_primary = True


def _h_peer_queued(proj: Projection, p: dict, ev: Event) -> None:
    ch = proj.channels[p["channel_id"]]
    ch.queued.append({"msg_id": p["msg_id"], "sender": p["sender"], "body": p.get("body")})


def _h_peer_delivered(proj: Projection, p: dict, ev: Event) -> None:
    ch = proj.channels[p["channel_id"]]
    mid = p["msg_id"]
    for m in list(ch.queued):
        if m["msg_id"] == mid:
            ch.queued.remove(m)
            if not any(d["msg_id"] == mid and d["sender"] == m["sender"] for d in ch.delivered):
                ch.delivered.append(m)
            break


def _h_peer_closed(proj: Projection, p: dict, ev: Event) -> None:
    ch = proj.channels[p["channel_id"]]
    ch.closed = True
    if p.get("discard_queued"):
        ch.discarded.extend(ch.queued)
        ch.queued = []


def _h_cutoff(proj: Projection, p: dict, ev: Event) -> None:
    proj.admission_cutoff = True
    proj.cutoff_reason = p.get("reason")
    proj.archive_phase = ArchivePhase.CUTOFF


def _h_archive_phase(proj: Projection, p: dict, ev: Event) -> None:
    proj.archive_phase = ArchivePhase(p["phase"])


def _h_wakeup(proj: Projection, p: dict, ev: Event) -> None:
    proj.notifications.append(Notification(kind=p["kind"], target_id=p["target_id"], ts=ev.ts))


def _h_observation(proj: Projection, p: dict, ev: Event) -> None:
    proj.observations.append(p)


def _h_portrait(proj: Projection, p: dict, ev: Event) -> None:
    key = p["key"]
    bucket = proj.portraits.setdefault(key, PortraitBucket())
    if p.get("skip"):
        bucket.skipped_reasons.append(p.get("reason", "skip"))
        return
    if p["outcome"] == "success":
        bucket.success += 1
    else:
        bucket.fail += 1


def _h_memory_candidate(proj: Projection, p: dict, ev: Event) -> None:
    _set_id(proj, p["memory_id"])
    proj.memories[p["memory_id"]] = MemoryItem(
        memory_id=p["memory_id"],
        status=MemoryStatus.CANDIDATE,
        source_node_id=p["source_node_id"],
        kind=p["kind"],
        confirmed=p.get("confirmed", False),
        supersedes=p.get("supersedes"),
    )


def _h_memory_promoted(proj: Projection, p: dict, ev: Event) -> None:
    mem = proj.memories[p["memory_id"]]
    mem.status = MemoryStatus.DURABLE


def _h_memory_superseded(proj: Projection, p: dict, ev: Event) -> None:
    old = proj.memories[p["old_id"]]
    old.status = MemoryStatus.SUPERSEDED
    if p.get("new_id"):
        proj.memories[p["new_id"]].supersedes = p["old_id"]


def _h_memory_invalidated(proj: Projection, p: dict, ev: Event) -> None:
    mem = proj.memories[p["memory_id"]]
    mem.status = MemoryStatus.INVALIDATED


def _h_tokens(proj: Projection, p: dict, ev: Event) -> None:
    book = proj.tokens.setdefault(p["node_id"], TokenBook())
    book.input += p.get("input", 0)
    book.output += p.get("output", 0)
    book.cache_read += p.get("cache_read", 0)
    book.cache_write += p.get("cache_write", 0)
    if proj.spec and proj.spec.optional_cumulative_budget:
        proj.cumulative_spent["tokens"] += p.get("input", 0) + p.get("output", 0)
        proj.cumulative_spent["calls"] += 1


def _h_ops_audit(proj: Projection, p: dict, ev: Event) -> None:
    proj.ops_audit.append(p)


def _h_human(proj: Projection, p: dict, ev: Event) -> None:
    proj.human_instructions.append(p)


def _h_suggestion_queued(proj: Projection, p: dict, ev: Event) -> None:
    proj.suggestions.append(Suggestion(p["suggestion_id"], p["kind"], p["payload"]))


def _h_suggestion_consumed(proj: Projection, p: dict, ev: Event) -> None:
    for s in proj.suggestions:
        if s.suggestion_id == p["suggestion_id"]:
            s.consumed = True


def _h_cm_acq(proj: Projection, p: dict, ev: Event) -> None:
    proj.cm_inflight += 1
    proj.cm_jobs[p["job_id"]] = p["trigger_node_id"]


def _h_cm_rel(proj: Projection, p: dict, ev: Event) -> None:
    proj.cm_inflight = max(0, proj.cm_inflight - 1)
    proj.cm_jobs.pop(p["job_id"], None)


def _h_llm_call(proj: Projection, p: dict, ev: Event) -> None:
    proj.nodes[p["node_id"]].llm_calls += 1


def _h_window_cleaned(proj: Projection, p: dict, ev: Event) -> None:
    proj.nodes[p["node_id"]].window_cleaned = True
    # 不删除 evidence
    for e in proj.evidence:
        if e.node_id == p["node_id"]:
            e.retained = True


def _h_window_usage(proj: Projection, p: dict, ev: Event) -> None:
    proj.nodes[p["node_id"]].window_usage = p["ratio"]


def _h_role_changed(proj: Projection, p: dict, ev: Event) -> None:
    node = proj.nodes[p["node_id"]]
    node.role = NodeRole(p["role"])
    if p.get("team_id"):
        node.team_id = p["team_id"]


def _h_clock(proj: Projection, p: dict, ev: Event) -> None:
    proj.clock_ms = p["clock_ms"]


def _h_assistant_reward(proj: Projection, p: dict, ev: Event) -> None:
    proj.observations.append(
        {
            "kind": "assistant_reward",
            "assistant_node_id": p["assistant_node_id"],
            "signal": p["signal"],
            "primary_node_id": p["primary_node_id"],
        }
    )


_HANDLERS = {
    "RootCreated": _h_root_created,
    "SpecPublished": _h_spec_published,
    "GraphRevisionBumped": _h_graph_bumped,
    "TeamCreated": _h_team_created,
    "WorkItemCreated": _h_work_item_created,
    "DAGEdgeAdded": _h_dag_edge,
    "LeaseAcquired": _h_lease_acquired,
    "LeaseReleased": _h_lease_released,
    "LeaseReweighted": _h_lease_reweighted,
    "NodeProvisioning": _h_node_provisioning,
    "NodeActivated": _h_node_activated,
    "NodeFailed": _h_node_failed,
    "NodeDrained": _h_node_drained,
    "NodeBlocked": _h_node_blocked,
    "NodeSubmitted": _h_node_submitted,
    "NodeRejected": _h_node_rejected,
    "NodeFinalizing": _h_node_finalizing,
    "EvidenceCommitted": _h_evidence_committed,
    "PackageCommitted": _h_package_committed,
    "NodeAccepted": _h_node_accepted,
    "NodeTerminated": _h_node_terminated,
    "NodeAbortedFinalize": _h_aborted_finalize,
    "WorkItemEscalated": _h_item_escalated,
    "AttemptRecorded": _h_attempt_recorded,
    "SuccessorRegistered": _h_successor_registered,
    "SuccessorCleared": _h_successor_cleared,
    "CapsulePreloaded": _h_capsule_preloaded,
    "PackageAssembled": _h_package_assembled,
    "RequiredPrefetched": _h_required_prefetched,
    "PeerChannelOpened": _h_peer_opened,
    "PeerMessageQueued": _h_peer_queued,
    "PeerMessageDelivered": _h_peer_delivered,
    "PeerChannelClosed": _h_peer_closed,
    "AdmissionCutoff": _h_cutoff,
    "ArchivePhaseChanged": _h_archive_phase,
    "WakeupEmitted": _h_wakeup,
    "ObservationRecorded": _h_observation,
    "PortraitUpdated": _h_portrait,
    "MemoryCandidateAdded": _h_memory_candidate,
    "MemoryPromoted": _h_memory_promoted,
    "MemorySuperseded": _h_memory_superseded,
    "MemoryInvalidated": _h_memory_invalidated,
    "TokensRecorded": _h_tokens,
    "OpsAuditRecorded": _h_ops_audit,
    "HumanInstructionRecorded": _h_human,
    "SuggestionQueued": _h_suggestion_queued,
    "SuggestionConsumed": _h_suggestion_consumed,
    "ContextManagerAcquired": _h_cm_acq,
    "ContextManagerReleased": _h_cm_rel,
    "LlmCallRecorded": _h_llm_call,
    "WindowCleaned": _h_window_cleaned,
    "WindowUsageSet": _h_window_usage,
    "RoleChanged": _h_role_changed,
    "ClockAdvanced": _h_clock,
    "AssistantRewardRecorded": _h_assistant_reward,
}
