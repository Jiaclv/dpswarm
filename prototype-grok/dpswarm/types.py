"""机制文档中的枚举、合同与目录。字段只收录文档已确立的语义，不发明 schema。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Optional


class Level(Enum):
    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"


LEVEL_RANK = {
    Level.S: 5,
    Level.A: 4,
    Level.B: 3,
    Level.C: 2,
    Level.D: 1,
}


class ChoiceSource(Enum):
    LEAD = "lead"
    HUMAN = "human"


class StartKind(Enum):
    NEW = "new"
    ROLLOVER = "rollover"
    WAKEUP = "wakeup"


class AcceptanceState(Enum):
    """§5.6 验收流状态。"""

    NONE = "none"
    SUBMITTED = "submitted"
    FINALIZING = "finalizing"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TERMINATED = "terminated"
    ESCALATED = "escalated"
    ABORTED_FINALIZE = "aborted-finalize"


class PhysicalState(Enum):
    """§5.6 物理生命周期。drained 是 §9.6 回收后的实现态，文档三值之外用于表达 session 已回收。"""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    FAILED = "failed"
    DRAINED = "drained"


class BlockedState(Enum):
    """§5.6 调度阻塞态。NONE 表示无阻塞。REWEIGHT_WAIT 是点数差额等待（超时抑制窗口）。"""

    NONE = "none"
    BLOCKED = "blocked"
    RECOVERY = "recovery"
    REWEIGHT_WAIT = "reweight_wait"


class TerminalReason(Enum):
    """§4 work item 终局原因。"""

    ACCEPTED = "accepted"
    REJECTED_EXHAUSTED = "rejected-exhausted"
    ESCALATED = "escalated"
    TIMEOUT = "timeout"
    DEADLINE_STOPPED = "deadline-stopped"
    MANUAL_STOPPED = "manual-stopped"
    TERMINATED = "terminated"


class StopReason(Enum):
    """§4 harness 侧五值。"""

    COMPLETED = "completed"
    ERROR = "error"
    MAX_TOKENS = "max-tokens"
    ABORTED = "aborted"
    REFUSAL = "refusal"


class Attribution(Enum):
    """§8 打回归因。"""

    CAPABILITY = "capability"
    CONTEXT = "context"
    DESCRIPTION = "description"
    CONTRADICTION = "contradiction"


class HumanInstructionKind(Enum):
    """§9.2 人工指令三类。"""

    IMMEDIATE = "immediate"
    CONFIG = "config"
    TERMINAL = "terminal"


class ArchivePhase(Enum):
    """§9.6 封存三段式。"""

    NONE = "none"
    CUTOFF = "cutoff"
    SETTLING = "settling"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"


class SeedMode(Enum):
    """§5.3 seed 二元。"""

    FRESH = "fresh"
    FORK = "fork"


class NodeRole(Enum):
    LEAD = "lead"
    WORKER = "worker"
    ASSISTANT = "assistant"
    REVIEWER = "reviewer"


class TopologyKind(Enum):
    SOLO = "solo"
    DERIVE = "derive"
    SPLIT = "split"
    FISSION = "fission"


class MemoryStatus(Enum):
    CANDIDATE = "candidate"
    DURABLE = "durable"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class ErrorClass(Enum):
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA = "QUOTA"


TERMINAL_ACCEPTANCE = frozenset(
    {
        AcceptanceState.ACCEPTED,
        AcceptanceState.TERMINATED,
        AcceptanceState.ESCALATED,
        AcceptanceState.ABORTED_FINALIZE,
    }
)

RESOURCE_RELEASE_ACCEPTANCE = TERMINAL_ACCEPTANCE


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    reasoning_effort: str = "default"

    def key(self) -> tuple[str, str]:
        return (self.provider, self.model)


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    level: Level
    point_weight: int
    available: bool = True
    aa_coding: float = 50.0
    aa_reasoning: float = 50.0
    price: float = 1.0
    bench_decay_token_threshold: Optional[int] = None


@dataclass(frozen=True)
class RootExecutionSpec:
    """§2.1 仅 root 级稳定约束。运行时占用不得写入。"""

    max_open_work_items: int = 8
    max_active_node_points: int = 100
    subteam_points_ratio: float = 0.5
    root_acceptance_mode: str = "human"  # §10 具体模式留白；原型用 human 作为显式发布钩
    human_override_always_valid: bool = True
    permission_config_ref: str = "default"
    model_point_policy_version: str = "mpp-v1"
    deadline_enabled: bool = True
    deadline_ms: int = 24 * 3600 * 1000
    node_wall_clock_timeout_ms: int = 2 * 3600 * 1000
    max_team_workers: int = 3
    max_semantic_depth: int = 2
    physical_max_depth: int = 3  # 须 ≥ 语义深度 + 1，容纳同层 fork
    max_retries: int = 2
    archive_settlement_timeout_ms: int = 60_000
    context_manager_semaphore: int = 2
    inline_token_limit: int = 2000
    soft_window_threshold: float = 0.70
    hard_window_threshold: float = 0.85
    optional_cumulative_budget: Optional[dict] = None

    def without_runtime(self) -> dict:
        """断言用：Spec 可序列化字段不含占用/DAG/lease。"""
        return {
            "maxOpenWorkItems": self.max_open_work_items,
            "maxActiveNodePoints": self.max_active_node_points,
            "subTeamPointsRatio": self.subteam_points_ratio,
            "rootAcceptanceMode": self.root_acceptance_mode,
            "humanOverrideAlwaysValid": self.human_override_always_valid,
            "permissionConfigRef": self.permission_config_ref,
            "modelPointPolicyVersion": self.model_point_policy_version,
            "deadlineEnabled": self.deadline_enabled,
            "deadlineMs": self.deadline_ms,
            "nodeWallClockTimeoutMs": self.node_wall_clock_timeout_ms,
        }


def default_catalog() -> dict[tuple[str, str], ModelInfo]:
    return {
        ("acme", "s-lead"): ModelInfo("acme", "s-lead", Level.S, 10, aa_coding=90, aa_reasoning=88, price=10.0, bench_decay_token_threshold=128000),
        ("acme", "s-worker"): ModelInfo("acme", "s-worker", Level.S, 10, aa_coding=88, aa_reasoning=80, price=9.0, bench_decay_token_threshold=100000),
        ("acme", "a-coder"): ModelInfo("acme", "a-coder", Level.A, 5, aa_coding=80, aa_reasoning=70, price=4.0, bench_decay_token_threshold=64000),
        ("acme", "a-reason"): ModelInfo("acme", "a-reason", Level.A, 5, aa_coding=60, aa_reasoning=85, price=4.5),
        ("acme", "b-worker"): ModelInfo("acme", "b-worker", Level.B, 3, aa_coding=55, aa_reasoning=50, price=1.5),
        ("acme", "c-worker"): ModelInfo("acme", "c-worker", Level.C, 2, aa_coding=40, aa_reasoning=40, price=0.5),
        ("acme", "d-worker"): ModelInfo("acme", "d-worker", Level.D, 1, aa_coding=20, aa_reasoning=20, price=0.1),
        ("acme", "offline"): ModelInfo("acme", "offline", Level.B, 3, available=False),
        ("other", "s-alt"): ModelInfo("other", "s-alt", Level.S, 12, aa_coding=86, aa_reasoning=86, price=11.0),
        ("other", "a-alt"): ModelInfo("other", "a-alt", Level.A, 6, aa_coding=75, aa_reasoning=75, price=5.0),
    }


@dataclass
class PackageEntry:
    entry_id: str
    required: bool
    hash: str
    body: str = ""
    inline: bool = True
    prefetched: bool = False


@dataclass
class ContextPackage:
    package_id: str
    revision: str
    content_hash: str
    seed_mode: SeedMode = SeedMode.FRESH
    fork_seed_length: Optional[int] = None
    fork_parent_lineage: Optional[str] = None
    entries: list[PackageEntry] = field(default_factory=list)


@dataclass
class ChildSpec:
    route: Route
    depends_on: tuple[int, ...] = ()
    choice_source: ChoiceSource = ChoiceSource.LEAD
    proposed_route: Optional[Route] = None
    human_override_level: bool = False


def spec_replace(spec: RootExecutionSpec, **kwargs: Any) -> RootExecutionSpec:
    return replace(spec, **kwargs)
