from __future__ import annotations

from typing import List, Optional, Set, Tuple

from .events import Event
from .models import (
    AcceptanceState,
    BlockState,
    DelegationKind,
    LEVEL_ORDER,
    LifecycleState,
    ModelLevel,
    ModelRoute,
    RootRuntimeState,
    RouteSource,
)
from .projection import apply_event, has_dag_cycle, team_point_usage


class InvariantViolation(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


LEGAL_ACCEPTANCE_TRANSITIONS: Set[Tuple[Optional[AcceptanceState], AcceptanceState]] = {
    (None, AcceptanceState.SUBMITTED),
    (AcceptanceState.SUBMITTED, AcceptanceState.FINALIZING),
    (AcceptanceState.FINALIZING, AcceptanceState.ACCEPTED),
    (AcceptanceState.SUBMITTED, AcceptanceState.REJECTED),
    (AcceptanceState.REJECTED, AcceptanceState.SUBMITTED),
    (None, AcceptanceState.TERMINATED),
    (AcceptanceState.SUBMITTED, AcceptanceState.TERMINATED),
    (AcceptanceState.SUBMITTED, AcceptanceState.ESCALATED),
    (AcceptanceState.FINALIZING, AcceptanceState.ABORTED_FINALIZE),
}


def validate_event(state: RootRuntimeState, event: Event) -> None:
    """Validate candidate event against current projection; does not mutate."""
    shadow = _clone_state(state)
    _pre_apply_checks(shadow, event)
    apply_event(shadow, event)
    _post_apply_checks(shadow, event)


def _clone_state(state: RootRuntimeState) -> RootRuntimeState:
    import copy

    return copy.deepcopy(state)


def _pre_apply_checks(state: RootRuntimeState, event: Event) -> None:
    kind = event.kind
    p = event.payload

    if not state.admission_open and kind in {
        "work_item_created",
        "node_provisioning",
        "lease_acquired",
        "dag_edge_added",
    }:
        raise InvariantViolation("ADMISSION_CLOSED", "admission cutoff forbids new intake")

    if kind == "node_provisioning" and state.deadline_active:
        raise InvariantViolation("DEADLINE_ACTIVE", "deadline forbids new node start")

    if kind == "work_item_accepted":
        wi = state.work_items.get(p["work_item_id"])
        if not wi or wi.acceptance != AcceptanceState.FINALIZING:
            raise InvariantViolation("ACCEPT_FROM_NON_FINALIZING", "accepted must follow finalizing")

    if kind == "work_item_finalizing":
        wi = state.work_items.get(p["work_item_id"])
        if not wi or wi.acceptance != AcceptanceState.SUBMITTED:
            raise InvariantViolation("FINALIZE_FROM_NON_SUBMITTED", "finalizing requires submitted")

    if kind == "work_item_aborted_finalize":
        wi = state.work_items.get(p["work_item_id"])
        if not wi or wi.acceptance != AcceptanceState.FINALIZING:
            raise InvariantViolation("ABORT_NON_FINALIZING", "aborted-finalize requires finalizing")

    if kind == "successor_registered":
        node = state.nodes.get(p["node_id"])
        if node and node.successor_registered and not node.successor_registration_consumed:
            raise InvariantViolation("DOUBLE_SUCCESSOR", "successor already registered")

    if kind == "work_item_created":
        wid = p["work_item_id"]
        if wid in state.terminated_ids:
            raise InvariantViolation("ID_REUSE", "work_item_id must not be reused")

    if kind == "node_provisioning":
        nid = p["node_id"]
        if nid in {n.node_id for n in state.nodes.values() if n.acceptance == AcceptanceState.TERMINATED}:
            raise InvariantViolation("ID_REUSE", "node_id must not be reused after termination")


def _post_apply_checks(state: RootRuntimeState, event: Event) -> None:
    if state.open_worker_slots > state.spec.max_open_work_items:
        raise InvariantViolation("WORKER_SLOT_OVERFLOW", "worker slots exceed spec")

    if state.active_node_points > state.spec.max_active_node_points:
        raise InvariantViolation("POINT_OVERFLOW", "node points exceed root cap")

    if has_dag_cycle(state.dag_edges):
        raise InvariantViolation("DAG_CYCLE", "DAG must remain acyclic")

    for wi in state.work_items.values():
        if wi.acceptance in {AcceptanceState.ACCEPTED, AcceptanceState.TERMINATED, AcceptanceState.ESCALATED}:
            if wi.worker_slot_held:
                raise InvariantViolation("TERMINAL_HOLDS_SLOT", "terminal work item must not hold worker slot")

    for wi in state.work_items.values():
        if wi.acceptance in {AcceptanceState.ACCEPTED, AcceptanceState.TERMINATED, AcceptanceState.ESCALATED, AcceptanceState.ABORTED_FINALIZE}:
            for nid, node in state.nodes.items():
                if node.work_item_id != wi.work_item_id:
                    continue
                if node.lifecycle == LifecycleState.PROVISIONING and wi.acceptance != AcceptanceState.ABORTED_FINALIZE:
                    if state.sealing_phase != "settlement":
                        raise InvariantViolation(
                            "TERMINAL_WITH_PROVISIONING",
                            "terminal item cannot coexist with in-flight provisioning outside settlement",
                        )

    usage = team_point_usage(state)
    for team_id, team in state.teams.items():
        if usage.get(team_id, 0) > team.local_point_cap:
            raise InvariantViolation("TEAM_POINT_CAP", f"team {team_id} exceeds local cap")

    if event.kind == "work_item_accepted" and not p_get_evidence_ready(event):
        raise InvariantViolation("EVIDENCE_NOT_READY", "accepted requires evidence package ready")

    for node in state.nodes.values():
        if node.assistant_of and node.acceptance == AcceptanceState.SUBMITTED:
            raise InvariantViolation("ASSISTANT_CANNOT_SUBMIT", "assistant cannot independently submit")


def p_get_evidence_ready(event: Event) -> bool:
    return event.payload.get("evidence_ready", False)


def check_level_direction(lead_level: ModelLevel, worker_level: ModelLevel, human_override: bool) -> None:
    if human_override:
        return
    if LEVEL_ORDER[worker_level] > LEVEL_ORDER[lead_level]:
        raise InvariantViolation("LEVEL_DIRECTION", "can only summon same or lower level")


def check_fission_permission(lead_level: ModelLevel, kind: DelegationKind) -> None:
    if kind == DelegationKind.FISSION and lead_level != ModelLevel.S:
        raise InvariantViolation("FISSION_PERMISSION", "fission requires S level lead")


def check_semantic_depth(current_depth: int, kind: DelegationKind, spec_max: int) -> None:
    if kind == DelegationKind.SPLIT:
        return
    new_depth = current_depth + 1
    if new_depth > spec_max:
        raise InvariantViolation("SEMANTIC_DEPTH", "semantic depth exceeded")


def check_retry_budget(state: RootRuntimeState, work_item_id: str) -> None:
    wi = state.work_items[work_item_id]
    if wi.attempt_count >= state.spec.max_attempts_per_work_item:
        raise InvariantViolation("RETRY_BUDGET", "retry budget exhausted")


def check_finalizing_invariants(state: RootRuntimeState, work_item_id: str) -> None:
    wi = state.work_items[work_item_id]
    if wi.acceptance != AcceptanceState.FINALIZING:
        return
    primary = state.nodes[wi.primary_node_id]
    if primary.lease_id:
        lease = state.leases[primary.lease_id]
        if not lease.active:
            raise InvariantViolation("FINALIZING_LEASE_RELEASED", "finalizing must keep lease")


def validate_seal_order(events: List[Event]) -> None:
    """§9.6: drain nodes before terminal item migration during sealing."""
    phase = None
    for ev in events:
        if ev.kind == "seal_admission_cutoff":
            phase = "cutoff"
        if phase and ev.kind in {"work_item_accepted", "work_item_terminated", "work_item_escalated"}:
            if ev.payload.get("during_seal") and not ev.payload.get("nodes_drained"):
                raise InvariantViolation("SEAL_ORDER", "terminal item migration before node drain")


def route_must_not_be_silent_override(
    proposed: ModelRoute,
    resolved: ModelRoute,
    source: RouteSource,
) -> None:
    if proposed != resolved and source == RouteSource.HUMAN:
        raise InvariantViolation("SILENT_OVERRIDE", "cannot silently replace human route")
