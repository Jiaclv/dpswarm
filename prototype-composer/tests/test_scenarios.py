#!/usr/bin/env python3
"""Scenario tests for DPswarm control-plane logic prototype."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dpswarm.control_plane import ControlPlane, ControlPlaneError
from dpswarm.models import (
    AcceptanceState,
    BlockState,
    DelegationKind,
    LifecycleState,
    ModelLevel,
    ModelRoute,
    RootExecutionSpec,
    RouteSource,
    WorkItemTerminal,
)


def route(level: ModelLevel = ModelLevel.B, provider: str = "ds", model: str = "chat") -> ModelRoute:
    return ModelRoute(provider=provider, model=model, level=level)


def activate_worker(cp: ControlPlane, node_id: str) -> None:
    cp.complete_provisioning(node_id, "hash-ok", "hash-ok")


class ScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = RootExecutionSpec(
            max_open_work_items=4,
            max_active_node_points=80,
            max_semantic_depth=2,
            max_team_workers=3,
            max_attempts_per_work_item=3,
        )
        self.cp = ControlPlane(self.spec)

    # §2 RootExecutionSpec / routing hard admission
    def test_s02_spec_revision_stops_new_admission_when_over_capacity(self) -> None:
        """§2.1: lower spec cap blocks new admission without killing running nodes."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        used_slots = self.cp.state.open_worker_slots
        self.cp.publish_spec_revision(max_open=used_slots, max_points=80)
        with self.assertRaises(ControlPlaneError):
            self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.C), lead_level=ModelLevel.S)

    def test_s02_human_route_not_silently_overridden(self) -> None:
        """§2: human route priority; code must not silently replace."""
        lead = self.cp.create_root_lead(route(ModelLevel.A))
        human = route(ModelLevel.B, model="human-pick")
        catalog = {"ds/human-pick": route(ModelLevel.C, model="human-pick")}
        with self.assertRaises(ControlPlaneError):
            self.cp.admit_delegation(
                lead,
                DelegationKind.DERIVE,
                human,
                source=RouteSource.HUMAN,
                lead_level=ModelLevel.A,
                catalog=catalog,
            )

    def test_s02_level_direction_blocked_without_override(self) -> None:
        """§2 / §7: summon higher level worker forbidden."""
        lead = self.cp.create_root_lead(route(ModelLevel.A))
        with self.assertRaises(ControlPlaneError):
            self.cp.admit_delegation(
                lead,
                DelegationKind.DERIVE,
                route(ModelLevel.S),
                lead_level=ModelLevel.A,
            )

    def test_s02_human_level_override_allows_higher_worker(self) -> None:
        """§2: human override may bypass level direction."""
        lead = self.cp.create_root_lead(route(ModelLevel.A))
        adm = self.cp.admit_delegation(
            lead,
            DelegationKind.DERIVE,
            route(ModelLevel.S),
            lead_level=ModelLevel.A,
            human_level_override=True,
        )
        self.assertIsNotNone(adm.work_item_id)

    # §4 acceptance pipeline
    def test_s04_acceptance_five_step_pipeline(self) -> None:
        """§4: submitted -> finalizing (lease held) -> evidence -> accepted releases lease/slot."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        slots_before = self.cp.state.open_worker_slots
        points_before = self.cp.state.active_node_points

        self.cp.worker_submit(adm.work_item_id)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, AcceptanceState.SUBMITTED)

        self.cp.lead_decide_accept(adm.work_item_id)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, AcceptanceState.FINALIZING)
        self.assertEqual(self.cp.state.open_worker_slots, slots_before)
        self.assertEqual(self.cp.state.active_node_points, points_before)

        with self.assertRaises(ControlPlaneError):
            self.cp.publish_accepted(adm.work_item_id)

        self.cp.commit_evidence_package(adm.work_item_id)
        self.cp.publish_accepted(adm.work_item_id)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, AcceptanceState.ACCEPTED)
        self.assertEqual(self.cp.state.open_worker_slots, slots_before - 1)
        self.assertLess(self.cp.state.active_node_points, points_before)

    def test_s04_accept_skips_finalizing_rejected(self) -> None:
        """§9.1 invariant: accepted must follow finalizing."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        self.cp.worker_submit(adm.work_item_id)
        self.cp.commit_evidence_package(adm.work_item_id)
        with self.assertRaises(ControlPlaneError):
            self.cp.publish_accepted(adm.work_item_id)

    # §5.7 memory promotion gate (control-plane slice)
    def test_s057_submitted_cannot_publish_accepted_without_finalizing(self) -> None:
        """§5.7: submitted is not promotable."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        self.cp.worker_submit(adm.work_item_id)
        self.cp.commit_evidence_package(adm.work_item_id)
        with self.assertRaises(ControlPlaneError):
            self.cp.publish_accepted(adm.work_item_id)

    # §5.8 rollover / CAS
    def test_s058_successor_cas_unique_and_reset_on_activate(self) -> None:
        """§5.8: CAS registers one successor; reset after activation."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        node = self.cp.state.nodes[adm.node_id]
        self.cp.register_successor(adm.node_id, node.context_epoch)
        with self.assertRaises(ControlPlaneError):
            self.cp.register_successor(adm.node_id, node.context_epoch)
        self.cp.rollover_provision_successor(adm.node_id, "cap-1")
        self.assertEqual(self.cp.state.nodes[adm.node_id].context_epoch, 1)
        self.assertFalse(self.cp.state.nodes[adm.node_id].successor_registered)

    def test_s058_capsule_prep_failure_enters_recovery_without_state_change(self) -> None:
        """§5.8: capsule prep failure -> blocked/recovery, no acceptance change."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        before = self.cp.state.work_items[adm.work_item_id].acceptance
        self.cp.capsule_prep_failed(adm.node_id)
        self.assertEqual(self.cp.state.nodes[adm.node_id].block, BlockState.RECOVERY)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, before)

    def test_s058_rollover_keeps_lease_no_new_slot(self) -> None:
        """§5.8 / §7: rollover keeps same lease and worker slot."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        slots = self.cp.state.open_worker_slots
        lease_id = adm.lease_id
        node = self.cp.state.nodes[adm.node_id]
        self.cp.register_successor(adm.node_id, node.context_epoch)
        self.cp.rollover_provision_successor(adm.node_id, "cap-2")
        self.assertEqual(self.cp.state.open_worker_slots, slots)
        self.assertEqual(self.cp.state.nodes[adm.node_id].lease_id, lease_id)

    # §7 topology guards
    def test_s07_semantic_depth_default_two_layers(self) -> None:
        """§7: derive adds depth; third layer forbidden at default depth=2."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        l2 = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.A), lead_level=ModelLevel.S)
        activate_worker(self.cp, l2.node_id)
        with self.assertRaises(ControlPlaneError):
            self.cp.admit_delegation(l2.node_id, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.A)

    def test_s07_split_same_depth_one_slot_two_point_leases(self) -> None:
        """§7 split: same semantic depth, one worker slot, primary+assistant leases."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        before_slots = self.cp.state.open_worker_slots
        adm = self.cp.admit_delegation(lead, DelegationKind.SPLIT, route(ModelLevel.A), lead_level=ModelLevel.S)
        self.assertEqual(self.cp.state.open_worker_slots, before_slots + 1)
        wi = self.cp.state.work_items[adm.work_item_id]
        self.assertTrue(wi.peer_channel_open)
        assistants = [n for n in self.cp.state.nodes.values() if n.work_item_id == adm.work_item_id and n.assistant_of]
        self.assertEqual(len(assistants), 1)

    def test_s07_fission_requires_s_level(self) -> None:
        """§7: fission permission only for S lead."""
        lead = self.cp.create_root_lead(route(ModelLevel.A))
        with self.assertRaises(ControlPlaneError):
            self.cp.admit_delegation(lead, DelegationKind.FISSION, route(ModelLevel.B), lead_level=ModelLevel.A)

    def test_s07_worker_slot_exhaustion(self) -> None:
        """§7: maxOpenWorkItems hard cap."""
        spec = RootExecutionSpec(max_open_work_items=1, max_active_node_points=200)
        cp = ControlPlane(spec)
        lead = cp.create_root_lead(route(ModelLevel.S))
        cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        with self.assertRaises(ControlPlaneError):
            cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.C), lead_level=ModelLevel.S)

    def test_s07_point_capacity_blocks_admission(self) -> None:
        """§7: node points double admission gate."""
        with self.assertRaises(ControlPlaneError):
            cp = ControlPlane(RootExecutionSpec(max_open_work_items=5, max_active_node_points=25))
            cp.create_root_lead(route(ModelLevel.S))
        cp2 = ControlPlane(RootExecutionSpec(max_open_work_items=5, max_active_node_points=62))
        lead2 = cp2.create_root_lead(route(ModelLevel.S))  # 30
        cp2.admit_delegation(lead2, DelegationKind.DERIVE, route(ModelLevel.A), lead_level=ModelLevel.S)  # +20
        with self.assertRaises(ControlPlaneError):
            cp2.admit_delegation(lead2, DelegationKind.DERIVE, route(ModelLevel.A), lead_level=ModelLevel.S)

    def test_s07_degrade_assistant_closes_peer_channel(self) -> None:
        """§5.5 / §7: degrade closes peer channel and assistant lease."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.SPLIT, route(ModelLevel.A), lead_level=ModelLevel.S)
        points_before = self.cp.state.active_node_points
        self.cp.degrade_assistant(adm.work_item_id)
        self.assertFalse(self.cp.state.work_items[adm.work_item_id].peer_channel_open)
        self.assertLess(self.cp.state.active_node_points, points_before)

    def test_s07_timeout_only_on_active(self) -> None:
        """§7 / §9.3: wall-clock timeout applies to active nodes only."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        with self.assertRaises(ControlPlaneError):
            self.cp.node_timeout(adm.node_id)
        activate_worker(self.cp, adm.node_id)
        self.cp.node_timeout(adm.node_id)
        self.assertEqual(self.cp.state.nodes[adm.node_id].block, BlockState.BLOCKED)

    def test_s07_assistant_timeout_wakeup_primary(self) -> None:
        """§7 / §9.4: assistant failure wakes primary without carrying state."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.SPLIT, route(ModelLevel.A), lead_level=ModelLevel.S)
        assistant = [n for n in self.cp.state.nodes.values() if n.assistant_of][0]
        self.cp.provisioning_failed(assistant.node_id)
        self.cp.wakeup(adm.node_id)
        self.assertIn(adm.node_id, self.cp.state.notifications)

    # §8 retry budget and escalation
    def test_s08_retry_budget_max_three_attempts(self) -> None:
        """§8: first + 2 retries = 3 attempts max."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        wi = adm.work_item_id
        self.cp.worker_submit(wi)
        self.cp.lead_reject(wi)
        self.cp.retry_after_reject(wi)
        self.cp.lead_reject(wi)
        self.cp.retry_after_reject(wi)
        self.cp.lead_reject(wi)
        with self.assertRaises(ControlPlaneError):
            self.cp.retry_after_reject(wi)

    def test_s08_provisioning_failed_no_retry_budget(self) -> None:
        """§8: provisioning failed does not consume retry budget."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        attempts_before = self.cp.state.work_items[adm.work_item_id].attempt_count
        self.cp.provisioning_failed(adm.node_id)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].attempt_count, attempts_before)

    def test_s08_escalation_releases_resources(self) -> None:
        """§8: escalated terminal releases slot and points."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        slots = self.cp.state.open_worker_slots
        self.cp.worker_submit(adm.work_item_id)
        self.cp.escalate(adm.work_item_id)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, AcceptanceState.ESCALATED)
        self.assertEqual(self.cp.state.open_worker_slots, slots - 1)

    def test_s08_model_change_reweight_needs_capacity(self) -> None:
        """§7 / §8: upgrade model requires point delta before retry launch."""
        spec = RootExecutionSpec(max_open_work_items=3, max_active_node_points=40)
        cp = ControlPlane(spec)
        lead = cp.create_root_lead(route(ModelLevel.A))  # 20 points
        adm = cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.A)  # +12
        activate_worker(cp, adm.node_id)
        cp.worker_submit(adm.work_item_id)
        cp.lead_reject(adm.work_item_id)
        with self.assertRaises(ControlPlaneError):
            cp.retry_after_reject(adm.work_item_id, new_route=route(ModelLevel.S))

    # §9 control plane consistency
    def test_s09_invariant_rejects_dag_cycle(self) -> None:
        """§9.1 / §9.2: DAG must stay acyclic."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        a = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        b = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        self.cp.add_dag_edge(a.node_id, b.node_id)
        with self.assertRaises(ControlPlaneError):
            self.cp.add_dag_edge(b.node_id, a.node_id)

    def test_s09_cas_revision_mismatch(self) -> None:
        """§9.2: stale graph revision rejected."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        a = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        b = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.C), lead_level=ModelLevel.S)
        rev = self.cp.state.graph_revision
        self.cp.add_dag_edge(a.node_id, b.node_id)
        with self.assertRaises(ControlPlaneError):
            self.cp.add_dag_edge(b.node_id, a.node_id, expected_revision=rev)

    def test_s09_transaction_rollback_on_invariant_failure(self) -> None:
        """§9.2: method-level atomicity rolls back illegal accept."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        self.cp.worker_submit(adm.work_item_id)
        self.cp.commit_evidence_package(adm.work_item_id)
        rev_before = self.cp.state.graph_revision
        with self.assertRaises(ControlPlaneError):
            self.cp.publish_accepted(adm.work_item_id)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, AcceptanceState.SUBMITTED)
        self.assertEqual(self.cp.state.graph_revision, rev_before)

    def test_s09_provisioning_two_phase_hash_gate(self) -> None:
        """§9.3: active only after package hash match."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        with self.assertRaises(ControlPlaneError):
            self.cp.complete_provisioning(adm.node_id, "bad", "expected")
        self.assertEqual(self.cp.state.nodes[adm.node_id].lifecycle, LifecycleState.FAILED)

    def test_s09_aborted_finalize_releases_lease_not_successor(self) -> None:
        """§9.3: finalizing -> aborted-finalize legal; releases lease."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        points_before = self.cp.state.active_node_points
        self.cp.worker_submit(adm.work_item_id)
        self.cp.lead_decide_accept(adm.work_item_id)
        self.cp.abort_finalize(adm.work_item_id)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, AcceptanceState.ABORTED_FINALIZE)
        self.assertLess(self.cp.state.active_node_points, points_before)

    def test_s09_notify_carries_no_state_payload(self) -> None:
        """§9.4: notifications are tokens only."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        self.cp.wakeup(lead)
        self.assertEqual(self.cp.state.notifications, [lead])

    # §9.6 sealing three-phase
    def test_s096_seal_order_drain_before_terminal(self) -> None:
        """§9.6: drain nodes/CAS before item terminal migration."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        self.cp.trigger_deadline_seal()
        self.assertFalse(self.cp.state.admission_open)
        with self.assertRaises(ControlPlaneError):
            self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.C), lead_level=ModelLevel.S)
        self.cp.seal_settlement(drain_nodes=[adm.node_id])
        self.cp.seal_finalize_items([adm.work_item_id])
        self.assertEqual(
            self.cp.state.work_items[adm.work_item_id].terminal_reason,
            WorkItemTerminal.DEADLINE_STOPPED,
        )

    def test_s096_seal_allows_finalizing_accept_if_evidence_ready(self) -> None:
        """§9.6 + §4: in-flight finalizing may complete during settlement."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        self.cp.worker_submit(adm.work_item_id)
        self.cp.lead_decide_accept(adm.work_item_id)
        self.cp.commit_evidence_package(adm.work_item_id)
        self.cp.trigger_deadline_seal()
        self.cp.seal_settlement(drain_nodes=[adm.node_id])
        self.cp.seal_finalize_items([adm.work_item_id])
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, AcceptanceState.ACCEPTED)

    # §7 termination / degrade
    def test_s07_terminate_releases_slot_not_accepted(self) -> None:
        """§7 degrade: terminated sub-node not accepted, releases slot."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        slots = self.cp.state.open_worker_slots
        self.cp.terminate_work_item(adm.work_item_id, WorkItemTerminal.MANUAL_STOPPED)
        self.assertEqual(self.cp.state.work_items[adm.work_item_id].acceptance, AcceptanceState.TERMINATED)
        self.assertEqual(self.cp.state.open_worker_slots, slots - 1)

    # §9.3 terminal priority voids provisioning
    def test_s093_terminal_priority_voids_successor_registration(self) -> None:
        """§9.3: deadline seal voids successor CAS."""
        lead = self.cp.create_root_lead(route(ModelLevel.S))
        adm = self.cp.admit_delegation(lead, DelegationKind.DERIVE, route(ModelLevel.B), lead_level=ModelLevel.S)
        activate_worker(self.cp, adm.node_id)
        node = self.cp.state.nodes[adm.node_id]
        self.cp.register_successor(adm.node_id, node.context_epoch)
        self.cp.trigger_deadline_seal()
        self.cp.seal_settlement(drain_nodes=[adm.node_id])
        self.assertTrue(self.cp.state.nodes[adm.node_id].successor_registration_consumed)


class ScenarioReport:
    @staticmethod
    def collect() -> list[tuple[str, str, str]]:
        cases = []
        for method in ScenarioTests.__dict__:
            if method.startswith("test_s"):
                doc = ScenarioTests.__dict__[method].__doc__ or ""
                section = method.split("_")[1]
                cases.append((method, section, doc.strip()))
        return cases


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(ScenarioTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\nSUMMARY: {total - failed}/{total} passed")
    sys.exit(0 if result.wasSuccessful() else 1)
