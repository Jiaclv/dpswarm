from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .models import AcceptanceState, DelegationKind, ModelRoute, RouteSource


@dataclass(frozen=True)
class Event:
    kind: str
    payload: Dict[str, Any]
    seq: int = -1


# Event kinds (subset executable in prototype)
ROOT_CREATED = "root_created"
SPEC_REVISION_PUBLISHED = "spec_revision_published"
NODE_PROVISIONING = "node_provisioning"
NODE_ACTIVE = "node_active"
NODE_FAILED = "node_failed"
NODE_BLOCKED = "node_blocked"
NODE_RECOVERY = "node_recovery"
WORK_ITEM_CREATED = "work_item_created"
WORK_ITEM_SUBMITTED = "work_item_submitted"
WORK_ITEM_FINALIZING = "work_item_finalizing"
WORK_ITEM_ACCEPTED = "work_item_accepted"
WORK_ITEM_REJECTED = "work_item_rejected"
WORK_ITEM_TERMINATED = "work_item_terminated"
WORK_ITEM_ESCALATED = "work_item_escalated"
WORK_ITEM_ABORTED_FINALIZE = "work_item_aborted_finalize"
LEASE_ACQUIRED = "lease_acquired"
LEASE_RELEASED = "lease_released"
LEASE_REWEIGHTED = "lease_reweighted"
DAG_EDGE_ADDED = "dag_edge_added"
SUCCESSOR_REGISTERED = "successor_registered"
SUCCESSOR_REGISTRATION_RESET = "successor_registration_reset"
PEER_CHANNEL_OPENED = "peer_channel_opened"
PEER_CHANNEL_CLOSED = "peer_channel_closed"
NOTIFY_WAKEUP = "notify_wakeup"
SEAL_ADMISSION_CUTOFF = "seal_admission_cutoff"
SEAL_SETTLEMENT_START = "seal_settlement_start"
SEAL_TIMEOUT_FALLBACK = "seal_timeout_fallback"
SEAL_COMPLETE = "seal_complete"
ATTEMPT_STARTED = "attempt_started"
ATTEMPT_REJECTED = "attempt_rejected"
HUMAN_OVERRIDE = "human_override"
