from __future__ import annotations

from typing import Dict, List, Set, Tuple

from .events import Event
from .models import (
    AcceptanceState,
    BlockState,
    LeaseRecord,
    LifecycleState,
    ModelRoute,
    NodeRecord,
    RootRuntimeState,
    RouteSource,
    TeamRecord,
    WorkItemRecord,
    WorkItemTerminal,
)
from .resources import point_weight_for


def apply_event(state: RootRuntimeState, event: Event) -> None:
    kind = event.kind
    p = event.payload

    if kind == "root_created":
        return

    if kind == "spec_revision_published":
        state.spec.max_open_work_items = p["max_open_work_items"]
        state.spec.max_active_node_points = p["max_active_node_points"]
        state.spec.revision = p["revision"]
        return

    if kind == "work_item_created":
        wi = WorkItemRecord(
            work_item_id=p["work_item_id"],
            team_id=p["team_id"],
            semantic_depth=p["semantic_depth"],
            primary_node_id=p["primary_node_id"],
            worker_slot_held=p.get("worker_slot_held", True),
        )
        state.work_items[wi.work_item_id] = wi
        if wi.worker_slot_held:
            state.open_worker_slots += 1
        return

    if kind == "node_provisioning":
        existing = state.nodes.get(p["node_id"])
        if existing and p.get("context_epoch", 0) > existing.context_epoch:
            existing.lifecycle = LifecycleState.PROVISIONING
            existing.context_epoch = p.get("context_epoch", existing.context_epoch)
            existing.predecessor_session_id = p.get("predecessor_session_id")
            existing.checkpoint_id = p.get("checkpoint_id")
            return
        node = NodeRecord(
            node_id=p["node_id"],
            work_item_id=p["work_item_id"],
            role=p["role"],
            route=ModelRoute(**p["route"]),
            route_source=RouteSource(p["route_source"]),
            lifecycle=LifecycleState.PROVISIONING,
            lease_id=p.get("lease_id"),
            semantic_depth=p["semantic_depth"],
            delegation_depth=p["delegation_depth"],
            context_epoch=p.get("context_epoch", 0),
            predecessor_session_id=p.get("predecessor_session_id"),
            checkpoint_id=p.get("checkpoint_id"),
            assistant_of=p.get("assistant_of"),
            primary_node_id=p.get("primary_node_id"),
            team_id=p.get("team_id"),
            parent_team_id=p.get("parent_team_id"),
        )
        state.nodes[node.node_id] = node
        return

    if kind == "lease_acquired":
        lease = LeaseRecord(
            lease_id=p["lease_id"],
            node_id=p["node_id"],
            work_item_id=p["work_item_id"],
            point_weight=p["point_weight"],
            route=ModelRoute(**p["route"]),
        )
        state.leases[lease.lease_id] = lease
        state.active_node_points += lease.point_weight
        node = state.nodes[p["node_id"]]
        node.lease_id = lease.lease_id
        return

    if kind == "node_active":
        node = state.nodes[p["node_id"]]
        node.lifecycle = LifecycleState.ACTIVE
        node.active_since = p.get("active_since")
        return

    if kind == "node_failed":
        node = state.nodes[p["node_id"]]
        node.lifecycle = LifecycleState.FAILED
        return

    if kind == "node_blocked":
        node = state.nodes[p["node_id"]]
        node.block = BlockState.BLOCKED
        return

    if kind == "node_recovery":
        node = state.nodes[p["node_id"]]
        node.block = BlockState.RECOVERY
        return

    if kind == "lease_reweighted":
        lease = state.leases[p["lease_id"]]
        old = lease.point_weight
        new = p["new_weight"]
        delta = new - old
        lease.point_weight = new
        lease.route = ModelRoute(**p["route"])
        state.active_node_points += delta
        return

    if kind == "lease_released":
        lease = state.leases[p["lease_id"]]
        lease.active = False
        state.active_node_points -= lease.point_weight
        return

    if kind == "work_item_submitted":
        wi = state.work_items[p["work_item_id"]]
        wi.acceptance = AcceptanceState.SUBMITTED
        primary = state.nodes[wi.primary_node_id]
        primary.acceptance = AcceptanceState.SUBMITTED
        return

    if kind == "work_item_finalizing":
        wi = state.work_items[p["work_item_id"]]
        wi.acceptance = AcceptanceState.FINALIZING
        primary = state.nodes[wi.primary_node_id]
        primary.acceptance = AcceptanceState.FINALIZING
        return

    if kind == "work_item_accepted":
        wi = state.work_items[p["work_item_id"]]
        wi.acceptance = AcceptanceState.ACCEPTED
        wi.terminal_reason = WorkItemTerminal.ACCEPTED
        primary = state.nodes[wi.primary_node_id]
        primary.acceptance = AcceptanceState.ACCEPTED
        if wi.worker_slot_held:
            state.open_worker_slots -= 1
            wi.worker_slot_held = False
        if primary.lease_id:
            lease = state.leases[primary.lease_id]
            lease.active = False
            state.active_node_points -= lease.point_weight
        for nid, node in state.nodes.items():
            if node.work_item_id == wi.work_item_id and node.assistant_of and node.lease_id:
                lease = state.leases[node.lease_id]
                if lease.active:
                    lease.active = False
                    state.active_node_points -= lease.point_weight
        return

    if kind == "work_item_rejected":
        wi = state.work_items[p["work_item_id"]]
        wi.acceptance = AcceptanceState.REJECTED
        primary = state.nodes[wi.primary_node_id]
        primary.acceptance = AcceptanceState.REJECTED
        return

    if kind == "work_item_terminated":
        wi = state.work_items[p["work_item_id"]]
        wi.acceptance = AcceptanceState.TERMINATED
        wi.terminal_reason = WorkItemTerminal(p["terminal_reason"])
        primary = state.nodes[wi.primary_node_id]
        primary.acceptance = AcceptanceState.TERMINATED
        if wi.worker_slot_held:
            state.open_worker_slots -= 1
            wi.worker_slot_held = False
        if primary.lease_id:
            lease = state.leases[primary.lease_id]
            if lease.active:
                lease.active = False
                state.active_node_points -= lease.point_weight
        state.terminated_ids.add(wi.work_item_id)
        return

    if kind == "work_item_escalated":
        wi = state.work_items[p["work_item_id"]]
        wi.acceptance = AcceptanceState.ESCALATED
        wi.terminal_reason = WorkItemTerminal.ESCALATED
        primary = state.nodes[wi.primary_node_id]
        primary.acceptance = AcceptanceState.TERMINATED
        if wi.worker_slot_held:
            state.open_worker_slots -= 1
            wi.worker_slot_held = False
        if primary.lease_id:
            lease = state.leases[primary.lease_id]
            if lease.active:
                lease.active = False
                state.active_node_points -= lease.point_weight
        state.terminated_ids.add(wi.work_item_id)
        return

    if kind == "work_item_aborted_finalize":
        wi = state.work_items[p["work_item_id"]]
        wi.acceptance = AcceptanceState.ABORTED_FINALIZE
        primary = state.nodes[wi.primary_node_id]
        primary.acceptance = AcceptanceState.ABORTED_FINALIZE
        if primary.lease_id:
            lease = state.leases[primary.lease_id]
            if lease.active:
                lease.active = False
                state.active_node_points -= lease.point_weight
        return

    if kind == "dag_edge_added":
        state.dag_edges.add((p["from_node"], p["to_node"]))
        return

    if kind == "successor_registered":
        node = state.nodes[p["node_id"]]
        node.successor_registered = True
        return

    if kind == "successor_registration_reset":
        node = state.nodes[p["node_id"]]
        node.successor_registered = False
        node.successor_registration_consumed = True
        return

    if kind == "peer_channel_opened":
        wi = state.work_items[p["work_item_id"]]
        wi.peer_channel_open = True
        return

    if kind == "peer_channel_closed":
        wi = state.work_items[p["work_item_id"]]
        wi.peer_channel_open = False
        return

    if kind == "team_created":
        state.teams[p["team_id"]] = TeamRecord(
            team_id=p["team_id"],
            parent_team_id=p.get("parent_team_id"),
            lead_node_id=p["lead_node_id"],
            local_point_cap=p["local_point_cap"],
            fission_allowed=p.get("fission_allowed", True),
        )
        return

    if kind == "attempt_started":
        wi = state.work_items[p["work_item_id"]]
        wi.attempt_count += 1
        return

    if kind == "attempt_rejected":
        wi = state.work_items[p["work_item_id"]]
        wi.retry_budget_used += 1
        return

    if kind == "seal_admission_cutoff":
        state.admission_open = False
        state.deadline_active = True
        state.sealing_phase = "cutoff"
        return

    if kind == "seal_settlement_start":
        state.sealing_phase = "settlement"
        return

    if kind == "seal_timeout_fallback":
        state.sealing_phase = "timeout_fallback"
        return

    if kind == "seal_complete":
        state.sealing_phase = "done"
        return

    if kind == "notify_wakeup":
        state.notifications.append(p["target_node_id"])
        return

    state.graph_revision += 0  # silence unused in partial handlers


def project(events: List[Event], spec) -> RootRuntimeState:
    state = RootRuntimeState(spec=spec)
    for i, ev in enumerate(events):
        ev.seq = i
        apply_event(state, ev)
        state.event_count = i + 1
        state.graph_revision = i + 1
    return state


def team_point_usage(state: RootRuntimeState) -> Dict[str, int]:
    usage: Dict[str, int] = {}
    for node in state.nodes.values():
        if not node.lease_id:
            continue
        lease = state.leases.get(node.lease_id)
        if not lease or not lease.active:
            continue
        team_id = node.team_id or "root"
        usage[team_id] = usage.get(team_id, 0) + lease.point_weight
    return usage


def has_dag_cycle(edges: Set[Tuple[str, str]]) -> bool:
    graph: Dict[str, List[str]] = {}
    for a, b in edges:
        graph.setdefault(a, []).append(b)
    visited: Set[str] = set()
    stack: Set[str] = set()

    def dfs(n: str) -> bool:
        if n in stack:
            return True
        if n in visited:
            return False
        visited.add(n)
        stack.add(n)
        for m in graph.get(n, []):
            if dfs(m):
                return True
        stack.remove(n)
        return False

    for n in graph:
        if dfs(n):
            return True
    return False
