from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .events import Event
from .invariants import (
    InvariantViolation,
    check_fission_permission,
    check_level_direction,
    check_retry_budget,
    check_semantic_depth,
    route_must_not_be_silent_override,
    validate_event,
)
from .models import (
    AcceptanceState,
    DelegationKind,
    LifecycleState,
    ModelLevel,
    ModelRoute,
    RootExecutionSpec,
    RootRuntimeState,
    RouteSource,
    WorkItemTerminal,
)
from .projection import apply_event, project, team_point_usage
from .resources import ResourcePool, point_weight_for


class ControlPlaneError(Exception):
    pass


@dataclass
class AdmissionResult:
    node_id: str
    work_item_id: str
    lease_id: str


class ControlPlane:
    """Executable control-plane prototype driven by append-only events."""

    def __init__(self, spec: Optional[RootExecutionSpec] = None) -> None:
        self.spec = spec or RootExecutionSpec()
        self.events: List[Event] = []
        self.state = RootRuntimeState(spec=self.spec)
        self._id_counter = 0
        self._evidence_ready: Dict[str, bool] = {}
        self._scripts: Dict[str, Callable[..., Any]] = {}
        self._register_default_scripts()
        self._transact([Event("root_created", {"spec_id": self.spec.root_spec_id})])

    @property
    def projection(self) -> RootRuntimeState:
        return project(self.events, self.spec)

    def _next_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}-{self._id_counter}"

    def _register_default_scripts(self) -> None:
        self._scripts["lead_accept"] = lambda _: True
        self._scripts["lead_reject"] = lambda _: "context"
        self._scripts["attribution"] = lambda _: "capability"

    def set_script(self, name: str, fn: Callable[..., Any]) -> None:
        self._scripts[name] = fn

    def _transact(self, batch: List[Event], expected_graph_revision: Optional[int] = None) -> None:
        if expected_graph_revision is not None and expected_graph_revision != self.state.graph_revision:
            raise ControlPlaneError("CAS revision mismatch")
        snapshot = copy.deepcopy(self.state)
        try:
            for ev in batch:
                validate_event(snapshot, ev)
                apply_event(snapshot, ev)
            self.events.extend(batch)
            for ev in batch:
                apply_event(self.state, ev)
                self.state.graph_revision += 1
                self.state.event_count += 1
            self._flush()
        except InvariantViolation as exc:
            self.state = snapshot
            raise ControlPlaneError(str(exc)) from exc

    def _flush(self) -> None:
        self.state.flushed = True

    def _team_caps(self) -> Tuple[Dict[str, int], Dict[str, int]]:
        caps = {tid: t.local_point_cap for tid, t in self.state.teams.items()}
        caps["root"] = self.spec.max_active_node_points
        return caps, team_point_usage(self.state)

    def _resolve_route(
        self,
        proposed: ModelRoute,
        source: RouteSource,
        catalog: Optional[Dict[str, ModelRoute]] = None,
    ) -> ModelRoute:
        catalog = catalog or {}
        key = f"{proposed.provider}/{proposed.model}"
        if catalog and key not in catalog:
            raise ControlPlaneError("model not in catalog")
        resolved = catalog.get(key, proposed)
        try:
            route_must_not_be_silent_override(proposed, resolved, source)
        except InvariantViolation as exc:
            raise ControlPlaneError(str(exc)) from exc
        return resolved

    def publish_spec_revision(self, max_open: int, max_points: int) -> None:
        self._transact([
            Event(
                "spec_revision_published",
                {
                    "revision": self.spec.revision + 1,
                    "max_open_work_items": max_open,
                    "max_active_node_points": max_points,
                },
            )
        ])
        self.spec.revision += 1
        self.spec.max_open_work_items = max_open
        self.spec.max_active_node_points = max_points

    def create_root_lead(self, route: ModelRoute, source: RouteSource = RouteSource.LEAD) -> str:
        node_id = self._next_id("lead")
        lease_id = self._next_id("lease")
        weight = point_weight_for(route)
        wi_id = self._next_id("wi-root")
        team_id = "root-team"
        if weight > self.spec.max_active_node_points:
            raise ControlPlaneError("insufficient points for root lead")
        self._transact([
            Event(
                "team_created",
                {
                    "team_id": team_id,
                    "lead_node_id": node_id,
                    "local_point_cap": self.spec.max_active_node_points,
                    "parent_team_id": None,
                },
            ),
            Event(
                "node_provisioning",
                {
                    "node_id": node_id,
                    "work_item_id": wi_id,
                    "role": "lead",
                    "route": route.__dict__,
                    "route_source": source.value,
                    "semantic_depth": 1,
                    "delegation_depth": 0,
                    "team_id": team_id,
                },
            ),
            Event(
                "lease_acquired",
                {
                    "lease_id": lease_id,
                    "node_id": node_id,
                    "work_item_id": wi_id,
                    "point_weight": weight,
                    "route": route.__dict__,
                },
            ),
            Event("node_active", {"node_id": node_id, "active_since": time.time()}),
        ])
        return node_id

    def admit_delegation(
        self,
        parent_lead_id: str,
        kind: DelegationKind,
        proposed_route: ModelRoute,
        source: RouteSource = RouteSource.LEAD,
        lead_level: ModelLevel = ModelLevel.A,
        human_level_override: bool = False,
        catalog: Optional[Dict[str, ModelRoute]] = None,
        team_worker_count: int = 1,
    ) -> AdmissionResult:
        if not self.state.admission_open:
            raise ControlPlaneError("admission closed")
        parent = self.state.nodes[parent_lead_id]
        try:
            check_fission_permission(lead_level, kind)
            check_level_direction(lead_level, proposed_route.level, human_level_override)
            check_semantic_depth(parent.semantic_depth, kind, self.spec.max_semantic_depth)
        except InvariantViolation as exc:
            raise ControlPlaneError(str(exc)) from exc
        route = self._resolve_route(proposed_route, source, catalog)

        pool = ResourcePool(self.spec)
        pool.open_worker_slots = self.state.open_worker_slots
        pool.active_node_points = self.state.active_node_points

        team_id = parent.team_id or "root-team"
        if kind == DelegationKind.SPLIT:
            return self._admit_split(parent, route, source, pool, team_id)
        if kind == DelegationKind.FISSION:
            if team_worker_count > self.spec.max_team_workers:
                raise ControlPlaneError("maxTeamWorkers exceeded")
        return self._admit_worker(parent, kind, route, source, pool, team_id)

    def _admit_worker(
        self,
        parent,
        kind: DelegationKind,
        route: ModelRoute,
        source: RouteSource,
        pool: ResourcePool,
        team_id: str,
    ) -> AdmissionResult:
        if not pool.can_acquire_worker_slot():
            raise ControlPlaneError("worker slot unavailable")
        weight = point_weight_for(route)
        caps, usage = self._team_caps()
        if not pool.can_acquire_points(weight, caps, usage):
            raise ControlPlaneError("node points unavailable")

        wi_id = self._next_id("wi")
        node_id = self._next_id("node")
        lease_id = self._next_id("lease")
        semantic_depth = parent.semantic_depth + 1
        delegation_depth = parent.delegation_depth + 1

        batch = [
            Event(
                "work_item_created",
                {
                    "work_item_id": wi_id,
                    "team_id": team_id,
                    "semantic_depth": semantic_depth,
                    "primary_node_id": node_id,
                    "worker_slot_held": True,
                },
            ),
            Event("attempt_started", {"work_item_id": wi_id}),
            Event(
                "node_provisioning",
                {
                    "node_id": node_id,
                    "work_item_id": wi_id,
                    "role": "worker",
                    "route": route.__dict__,
                    "route_source": source.value,
                    "semantic_depth": semantic_depth,
                    "delegation_depth": delegation_depth,
                    "team_id": team_id,
                    "parent_team_id": team_id,
                },
            ),
            Event(
                "lease_acquired",
                {
                    "lease_id": lease_id,
                    "node_id": node_id,
                    "work_item_id": wi_id,
                    "point_weight": weight,
                    "route": route.__dict__,
                },
            ),
        ]
        self._transact(batch)
        return AdmissionResult(node_id, wi_id, lease_id)

    def _admit_split(
        self,
        parent,
        route: ModelRoute,
        source: RouteSource,
        pool: ResourcePool,
        team_id: str,
    ) -> AdmissionResult:
        if not pool.can_acquire_worker_slot():
            raise ControlPlaneError("worker slot unavailable")
        primary_id = self._next_id("primary")
        assistant_id = self._next_id("assistant")
        wi_id = self._next_id("wi")
        primary_lease = self._next_id("lease")
        assistant_lease = self._next_id("lease")
        weight = point_weight_for(route)
        caps, usage = self._team_caps()
        if not pool.can_acquire_points(weight * 2, caps, usage):
            raise ControlPlaneError("node points unavailable for split pair")

        batch = [
            Event(
                "work_item_created",
                {
                    "work_item_id": wi_id,
                    "team_id": team_id,
                    "semantic_depth": parent.semantic_depth,
                    "primary_node_id": primary_id,
                    "worker_slot_held": True,
                },
            ),
            Event("attempt_started", {"work_item_id": wi_id}),
            Event("peer_channel_opened", {"work_item_id": wi_id}),
            Event(
                "node_provisioning",
                {
                    "node_id": primary_id,
                    "work_item_id": wi_id,
                    "role": "worker",
                    "route": route.__dict__,
                    "route_source": source.value,
                    "semantic_depth": parent.semantic_depth,
                    "delegation_depth": parent.delegation_depth + 1,
                    "team_id": team_id,
                    "primary_node_id": primary_id,
                },
            ),
            Event(
                "lease_acquired",
                {
                    "lease_id": primary_lease,
                    "node_id": primary_id,
                    "work_item_id": wi_id,
                    "point_weight": weight,
                    "route": route.__dict__,
                },
            ),
            Event(
                "node_provisioning",
                {
                    "node_id": assistant_id,
                    "work_item_id": wi_id,
                    "role": "assistant",
                    "route": route.__dict__,
                    "route_source": source.value,
                    "semantic_depth": parent.semantic_depth,
                    "delegation_depth": parent.delegation_depth + 1,
                    "team_id": team_id,
                    "assistant_of": primary_id,
                },
            ),
            Event(
                "lease_acquired",
                {
                    "lease_id": assistant_lease,
                    "node_id": assistant_id,
                    "work_item_id": wi_id,
                    "point_weight": weight,
                    "route": route.__dict__,
                },
            ),
        ]
        self._transact(batch)
        return AdmissionResult(primary_id, wi_id, primary_lease)

    def complete_provisioning(self, node_id: str, package_hash: str, expected_hash: str) -> None:
        if package_hash != expected_hash:
            self._transact([Event("node_failed", {"node_id": node_id})])
            raise ControlPlaneError("package hash mismatch")
        self._transact([Event("node_active", {"node_id": node_id, "active_since": time.time()})])

    def provisioning_failed(self, node_id: str) -> None:
        self._transact([Event("node_failed", {"node_id": node_id})])

    def worker_submit(self, work_item_id: str) -> None:
        wi = self.state.work_items[work_item_id]
        if wi.acceptance == AcceptanceState.SUBMITTED:
            raise ControlPlaneError("already submitted")
        self._transact([Event("work_item_submitted", {"work_item_id": work_item_id})])

    def lead_decide_accept(self, work_item_id: str) -> None:
        if not self._scripts["lead_accept"](work_item_id):
            raise ControlPlaneError("lead rejected in script")
        self._transact([Event("work_item_finalizing", {"work_item_id": work_item_id})])

    def commit_evidence_package(self, work_item_id: str) -> None:
        self._evidence_ready[work_item_id] = True

    def publish_accepted(self, work_item_id: str) -> None:
        if not self._evidence_ready.get(work_item_id):
            raise ControlPlaneError("evidence not ready")
        wi = self.state.work_items[work_item_id]
        batch = [Event("work_item_accepted", {"work_item_id": work_item_id, "evidence_ready": True})]
        if wi.peer_channel_open:
            batch.append(Event("peer_channel_closed", {"work_item_id": work_item_id}))
        self._transact(batch)

    def lead_reject(self, work_item_id: str) -> str:
        reason = self._scripts["lead_reject"](work_item_id)
        self._transact([
            Event("work_item_rejected", {"work_item_id": work_item_id}),
            Event("attempt_rejected", {"work_item_id": work_item_id}),
        ])
        return reason

    def retry_after_reject(self, work_item_id: str, new_route: Optional[ModelRoute] = None) -> None:
        wi = self.state.work_items[work_item_id]
        try:
            check_retry_budget(self.state, work_item_id)
        except InvariantViolation as exc:
            raise ControlPlaneError(str(exc)) from exc
        primary = self.state.nodes[wi.primary_node_id]
        if new_route and new_route != primary.route:
            self.reweight_lease(primary.node_id, new_route)
        self._transact([
            Event("attempt_started", {"work_item_id": work_item_id}),
            Event("work_item_submitted", {"work_item_id": work_item_id}),
        ])

    def reweight_lease(self, node_id: str, new_route: ModelRoute) -> None:
        node = self.state.nodes[node_id]
        lease = self.state.leases[node.lease_id]
        new_weight = point_weight_for(new_route)
        delta = new_weight - lease.point_weight
        if delta > 0:
            pool = ResourcePool(self.spec)
            pool.active_node_points = self.state.active_node_points
            caps, usage = self._team_caps()
            if not pool.can_acquire_points(delta, caps, usage):
                raise ControlPlaneError("reweight-wait: insufficient points")
        self._transact([
            Event(
                "lease_reweighted",
                {
                    "lease_id": lease.lease_id,
                    "new_weight": new_weight,
                    "route": new_route.__dict__,
                },
            )
        ])

    def escalate(self, work_item_id: str) -> None:
        wi = self.state.work_items[work_item_id]
        batch = [Event("work_item_escalated", {"work_item_id": work_item_id})]
        if wi.peer_channel_open:
            batch.append(Event("peer_channel_closed", {"work_item_id": work_item_id}))
        self._transact(batch)

    def degrade_assistant(self, work_item_id: str) -> None:
        wi = self.state.work_items[work_item_id]
        assistants = [n for n in self.state.nodes.values() if n.work_item_id == work_item_id and n.assistant_of]
        batch = [Event("peer_channel_closed", {"work_item_id": work_item_id})]
        for assistant in assistants:
            if assistant.lease_id:
                batch.append(Event("lease_released", {"lease_id": assistant.lease_id}))
            batch.append(Event("node_failed", {"node_id": assistant.node_id}))
        self._transact(batch)

    def terminate_work_item(self, work_item_id: str, reason: WorkItemTerminal) -> None:
        wi = self.state.work_items[work_item_id]
        batch = [Event("work_item_terminated", {"work_item_id": work_item_id, "terminal_reason": reason.value})]
        if wi.peer_channel_open:
            batch.append(Event("peer_channel_closed", {"work_item_id": work_item_id}))
        self._transact(batch)

    def abort_finalize(self, work_item_id: str) -> None:
        self._transact([Event("work_item_aborted_finalize", {"work_item_id": work_item_id})])

    def node_timeout(self, node_id: str) -> None:
        node = self.state.nodes[node_id]
        if node.lifecycle != LifecycleState.ACTIVE:
            raise ControlPlaneError("timeout only applies to active nodes")
        self._transact([Event("node_blocked", {"node_id": node_id})])

    def wakeup(self, target_node_id: str) -> None:
        self._transact([Event("notify_wakeup", {"target_node_id": target_node_id})])

    def register_successor(self, node_id: str, context_epoch: int) -> None:
        node = self.state.nodes[node_id]
        if node.context_epoch != context_epoch:
            raise ControlPlaneError("context_epoch mismatch for CAS")
        self._transact([Event("successor_registered", {"node_id": node_id})])

    def reset_successor_registration(self, node_id: str, reason: str) -> None:
        self._transact([
            Event("successor_registration_reset", {"node_id": node_id, "reason": reason}),
        ])

    def rollover_provision_successor(self, node_id: str, checkpoint_id: str) -> str:
        node = self.state.nodes[node_id]
        if not node.successor_registered:
            raise ControlPlaneError("successor not registered")
        new_epoch = node.context_epoch + 1
        successor_session = self._next_id("session")
        self._transact([
            Event(
                "node_provisioning",
                {
                    "node_id": node_id,
                    "work_item_id": node.work_item_id,
                    "role": node.role,
                    "route": node.route.__dict__,
                    "route_source": node.route_source.value,
                    "semantic_depth": node.semantic_depth,
                    "delegation_depth": node.delegation_depth,
                    "context_epoch": new_epoch,
                    "predecessor_session_id": successor_session,
                    "checkpoint_id": checkpoint_id,
                    "team_id": node.team_id,
                },
            ),
            Event("node_active", {"node_id": node_id, "active_since": time.time()}),
            Event("successor_registration_reset", {"node_id": node_id, "reason": "activated"}),
        ])
        return successor_session

    def capsule_prep_failed(self, node_id: str) -> None:
        self._transact([Event("node_recovery", {"node_id": node_id})])

    def trigger_deadline_seal(self) -> None:
        self._transact([Event("seal_admission_cutoff", {})])

    def seal_settlement(self, drain_nodes: List[str]) -> None:
        batch = [Event("seal_settlement_start", {"drained_nodes": drain_nodes})]
        for node_id in drain_nodes:
            node = self.state.nodes.get(node_id)
            if node and node.lifecycle == LifecycleState.PROVISIONING:
                batch.append(Event("node_failed", {"node_id": node_id}))
            if node and node.successor_registered:
                batch.append(Event("successor_registration_reset", {"node_id": node_id, "reason": "seal"}))
        self._transact(batch)

    def seal_finalize_items(self, work_item_ids: List[str]) -> None:
        batch: List[Event] = []
        for work_item_id in work_item_ids:
            wi = self.state.work_items[work_item_id]
            if wi.acceptance == AcceptanceState.FINALIZING and self._evidence_ready.get(work_item_id):
                batch.append(
                    Event(
                        "work_item_accepted",
                        {
                            "work_item_id": work_item_id,
                            "evidence_ready": True,
                            "during_seal": True,
                            "nodes_drained": True,
                        },
                    )
                )
            elif wi.acceptance not in {
                AcceptanceState.ACCEPTED,
                AcceptanceState.TERMINATED,
                AcceptanceState.ESCALATED,
            }:
                batch.append(
                    Event(
                        "work_item_terminated",
                        {
                            "work_item_id": work_item_id,
                            "terminal_reason": WorkItemTerminal.DEADLINE_STOPPED.value,
                            "during_seal": True,
                            "nodes_drained": True,
                        },
                    )
                )
        batch.append(Event("seal_complete", {}))
        self._transact(batch)

    def add_dag_edge(self, from_node: str, to_node: str, expected_revision: Optional[int] = None) -> None:
        self._transact(
            [Event("dag_edge_added", {"from_node": from_node, "to_node": to_node})],
            expected_graph_revision=expected_revision,
        )

    def simulate_accept_pipeline(self, work_item_id: str) -> None:
        self.worker_submit(work_item_id)
        self.lead_decide_accept(work_item_id)
        self.commit_evidence_package(work_item_id)
        self.publish_accepted(work_item_id)
