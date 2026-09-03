from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple


class AcceptanceState(str, Enum):
    SUBMITTED = "submitted"
    FINALIZING = "finalizing"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TERMINATED = "terminated"
    ESCALATED = "escalated"
    ABORTED_FINALIZE = "aborted-finalize"


class LifecycleState(str, Enum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    FAILED = "failed"


class BlockState(str, Enum):
    NONE = "none"
    BLOCKED = "blocked"
    RECOVERY = "recovery"


class ModelLevel(str, Enum):
    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"


LEVEL_ORDER = {ModelLevel.S: 5, ModelLevel.A: 4, ModelLevel.B: 3, ModelLevel.C: 2, ModelLevel.D: 1}


class DelegationKind(str, Enum):
    DERIVE = "derive"
    SPLIT = "split"
    FISSION = "fission"


class RouteSource(str, Enum):
    LEAD = "lead"
    HUMAN = "human"


class WorkItemTerminal(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_EXHAUSTED = "rejected-exhausted"
    ESCALATED = "escalated"
    TIMEOUT = "timeout"
    DEADLINE_STOPPED = "deadline-stopped"
    MANUAL_STOPPED = "manual-stopped"


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    reasoning_effort: str = "default"
    level: ModelLevel = ModelLevel.B


@dataclass
class RootExecutionSpec:
    root_spec_id: str = "root-spec-1"
    revision: int = 1
    max_open_work_items: int = 8
    max_active_node_points: int = 100
    sub_team_point_ratio: float = 0.5
    max_semantic_depth: int = 2
    max_team_workers: int = 3
    max_attempts_per_work_item: int = 3  # first + 2 retries
    node_wall_clock_timeout_s: int = 7200
    deadline_enabled: bool = True
    model_point_policy_version: str = "v1"


@dataclass
class LeaseRecord:
    lease_id: str
    node_id: str
    work_item_id: str
    point_weight: int
    route: ModelRoute
    active: bool = True


@dataclass
class NodeRecord:
    node_id: str
    work_item_id: str
    role: str  # lead | worker | assistant | reviewer
    route: ModelRoute
    route_source: RouteSource
    acceptance: Optional[AcceptanceState] = None
    lifecycle: Optional[LifecycleState] = None
    block: BlockState = BlockState.NONE
    lease_id: Optional[str] = None
    semantic_depth: int = 1
    delegation_depth: int = 0
    context_epoch: int = 0
    predecessor_session_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    successor_registered: bool = False
    successor_registration_consumed: bool = False
    assistant_of: Optional[str] = None  # node_id of primary
    primary_node_id: Optional[str] = None  # for split primary
    team_id: Optional[str] = None
    parent_team_id: Optional[str] = None
    active_since: Optional[float] = None


@dataclass
class WorkItemRecord:
    work_item_id: str
    team_id: str
    semantic_depth: int
    primary_node_id: str
    acceptance: Optional[AcceptanceState] = None
    terminal_reason: Optional[WorkItemTerminal] = None
    attempt_count: int = 0
    retry_budget_used: int = 0
    worker_slot_held: bool = False
    peer_channel_open: bool = False
    sealed: bool = False


@dataclass
class TeamRecord:
    team_id: str
    parent_team_id: Optional[str]
    lead_node_id: str
    local_point_cap: int
    fission_allowed: bool = True


@dataclass
class RootRuntimeState:
    spec: RootExecutionSpec
    graph_revision: int = 0
    event_count: int = 0
    flushed: bool = True
    admission_open: bool = True
    sealing_phase: Optional[str] = None  # cutoff | settlement | timeout_fallback | done
    deadline_active: bool = False
    open_worker_slots: int = 0
    active_node_points: int = 0
    nodes: Dict[str, NodeRecord] = field(default_factory=dict)
    work_items: Dict[str, WorkItemRecord] = field(default_factory=dict)
    teams: Dict[str, TeamRecord] = field(default_factory=dict)
    leases: Dict[str, LeaseRecord] = field(default_factory=dict)
    dag_edges: Set[Tuple[str, str]] = field(default_factory=set)
    terminated_ids: Set[str] = field(default_factory=set)
    notifications: List[str] = field(default_factory=list)
