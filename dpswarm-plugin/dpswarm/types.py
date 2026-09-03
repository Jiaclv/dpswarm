"""DPswarm 核心类型（契约层）。

对应《DPswarm-机制架构.md》：
- §2.1  RootExecutionSpec —— 整棵任务树的运行边界合同
- §5.6  状态词汇三层：验收流 / 物理生命周期 / 调度阻塞
- §5.7  验收状态机
- §7    拓扑护栏（深度按 agent 层级计：主 agent = 第 1 层）
- §8    模型分级 S/A/B/C/D 与路由
- §9.6  封存三段式相位

本文件是所有模块的公共契约：字段名被事件 payload、状态投影、不变量
和 Provider 适配层共同引用，修改需同步全链路。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 枚举：状态词汇三层（§5.6，勿混用）
# ---------------------------------------------------------------------------


class AcceptanceState(str, Enum):
    """① 验收流状态（内容裁决层）。None（初始态）不在枚举中。"""

    SUBMITTED = "submitted"          # 待审
    FINALIZING = "finalizing"        # Lead 已决定通过，依赖材料原子提交中
    ACCEPTED = "accepted"            # 原子发布完成
    REJECTED = "rejected"            # 打回（可归因重试）
    TERMINATED = "terminated"        # 明确终止（含退化收回，不算验收、不进画像）
    ESCALATED = "escalated"          # 任务上交（无裁决、不进画像）
    ABORTED_FINALIZE = "aborted-finalize"  # finalizing 被显式取消（证据保留、不解锁后继）


class LifecycleState(str, Enum):
    """② 物理生命周期状态（§9.3 启动协议）。"""

    PROVISIONING = "provisioning"    # 两阶段第一步：意图已落盘，session 未确认
    ACTIVE = "active"                # hash 确认后
    FAILED = "failed"                # 启动事务失败（不耗重试预算）


class BlockState(str, Enum):
    """③ 调度阻塞态。"""

    NONE = "none"
    BLOCKED = "blocked"              # 超时转 blocked / 预装失败
    RECOVERY = "recovery"            # 预装 capsule 失败回退（§5.8）


class NodeRole(str, Enum):
    ROOT_LEAD = "root-lead"          # 主 agent（默认单干态；委派发生即 Lead）
    WORKER = "worker"                # 普通执行节点（占 worker 槽的 work item 主执行者）
    ASSISTANT = "assistant"          # 分裂协助者（依附型：不独立提交验收）
    CONTEXT_MANAGER = "context-manager"  # 按需瞬时调用，不是节点、不占点（§7）


class DelegationKind(str, Enum):
    """拓扑三方向（§7 机制五）。ROOT 为主 agent 自身任务。"""

    ROOT = "root"
    DERIVE = "derive"      # 派生：parent + 单个较轻量子 agent，产生新 work item
    SPLIT = "split"        # 分裂：1 主 1 副同构协作，不产生新 work item（同层拓宽）
    FISSION = "fission"    # 裂变：扩展成多 agent team，产生新 work item + 子 Team


class StartType(str, Enum):
    """节点物理启动类型（§9.3：新建 / 硬切 successor / 唤起恢复同一协议）。"""

    NEW = "new"
    ROLLOVER = "rollover"  # context 硬切：同 node/item/lease，epoch+1，深度保持
    RESUME = "resume"      # continuable 封存唤起


class RouteSource(str, Enum):
    ROUTE_LEAD = "lead"    # 语义选择权：人工 > Lead（§2）
    ROUTE_HUMAN = "human"


class SealPhase(str, Enum):
    """Team 封存三段式（§9.6）。"""

    OPEN = "open"
    CUTOFF = "cutoff"            # 准入截止（同时封死 start_node）
    SETTLEMENT = "settlement"    # 有界结算（in-flight finalizing 可完成 accepted）
    COMPLETED = "completed"
    TIMED_OUT = "timed-out"


class RejectAttribution(str, Enum):
    """验收打回归因四分支（§8 异常路径）。"""

    CAPABILITY = "capability"  # 能力弱项 → 换模型升级（上限 = Lead 级别）
    CONTEXT = "context"        # context 裁错/缺料 → 修 context 重试
    DESCRIPTION = "description"  # 描述不清 → 修描述重试
    CONTRADICTION = "contradiction"  # 任务矛盾 → 退化或上报人工，不硬磕


class StopReason(str, Enum):
    """harness 侧两层终止原因的第一层：stopReason 五值照记（§4）。"""

    COMPLETED = "completed"
    ERROR = "error"
    MAX_TOKENS = "max-tokens"
    ABORTED = "aborted"
    REFUSAL = "refusal"


class WorkItemOutcome(str, Enum):
    """work item 终局原因（§4 第二层）。"""

    ACCEPTED = "accepted"
    REJECTED_EXHAUSTED = "rejected-exhausted"
    ESCALATED = "escalated"
    TIMEOUT = "timeout"
    DEADLINE_STOPPED = "deadline-stopped"
    MANUAL_STOPPED = "manual-stopped"


# ---------------------------------------------------------------------------
# 路由与分级（§2 / §3 / §8）
# ---------------------------------------------------------------------------


class Level(str, Enum):
    """模型分级 S/A/B/C/D（§8，自定五档口径）。"""

    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"

    @property
    def rank(self) -> int:
        return {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}[self.value]


@dataclass(frozen=True)
class ModelRoute:
    """精确路由。选择来源（lead|human）、提议与实际解析路由均持久化（§2）。"""

    provider: str
    model: str
    reasoning_effort: str = "default"
    level: Level = Level.B          # 由模型目录（catalog）解析，非调用方声称
    source: RouteSource = RouteSource.ROUTE_LEAD
    point_weight: int = 1           # ModelPointPolicy 占位权重（公式 §10 留白）

    def same_model(self, other: "ModelRoute") -> bool:
        return (self.provider, self.model) == (other.provider, other.model)


@dataclass
class ModelFacts:
    """注入给 Lead 的单个模型事实（§3 事实注入：目录 + 价目 + AA 分维）。"""

    provider: str
    model: str
    level: Level
    aa_dimensional: Dict[str, float] = field(default_factory=dict)  # coding/reasoning/...
    aa_source: str = "declared"  # 'aa@<date>' 外部快照 | 'declared' 声明值 | 'demo' 演示目录
    context_window: int = 128_000
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0
    available: bool = True

    def aa_score(self, task_type: str) -> float:
        """按任务类型取 AA 分维评分，全局总分（overall）兜底（§8 V1 选型依据）。"""
        if task_type in self.aa_dimensional:
            return self.aa_dimensional[task_type]
        return self.aa_dimensional.get("overall", 0.0)


@dataclass
class ModelCatalog:
    """模型目录与事实：代码采集与确定性计算（§2 职责切分）。"""

    facts: Dict[str, ModelFacts] = field(default_factory=dict)  # key = "provider/model"
    point_policy_version: str = "pp-v0"

    def register(self, facts: ModelFacts) -> None:
        self.facts[f"{facts.provider}/{facts.model}"] = facts

    def resolve(self, provider: str, model: str) -> Optional[ModelFacts]:
        return self.facts.get(f"{provider}/{model}")

    def resolve_level(self, provider: str, model: str) -> Optional[Level]:
        f = self.resolve(provider, model)
        return f.level if f else None

    def fact_sheet(self, root_points_total: int, root_points_used: int,
                   task_type: str = "coding") -> str:
        """生成注入文本（§3）：目录 + 价目 + 当前容量 + AA 分维。"""
        avail = root_points_total - root_points_used
        lines = [
            "# DPswarm 模型事实（代码生成，非 LLM 判断）",
            f"- root 点数：总容量 {root_points_total}，已占用 {root_points_used}，可用 {avail}",
            f"- point policy 版本：{self.point_policy_version}",
            "- 模型目录（AA 分维评分按任务类型匹配，仅作选择事实）：",
        ]
        for key, f in sorted(self.facts.items()):
            if not f.available:
                continue
            src = "" if f.aa_source.startswith("aa@") else f" [{f.aa_source}·声明值]"
            lines.append(
                f"  - {key} [级别 {f.level.value}] AA[{task_type}]="
                f"{f.aa_score(task_type):.1f} ctx={f.context_window} "
                f"in=${f.input_price_per_mtok}/Mtok out=${f.output_price_per_mtok}/Mtok{src}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# RootExecutionSpec（§2.1）：root 级稳定约束合同
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootExecutionSpec:
    """只能保存 root 级稳定约束；实时状态属 RootRuntimeState（§2.1）。

    不原地覆写：人工调整边界时发布新 revision（rev 单调递增）。
    新 revision 容量低于当前占用时：不强杀在途节点，停止新准入。
    """

    max_open_work_items: int = 4            # 只统计普通 worker 任务（§7）
    max_active_node_points: int = 8         # 加权占用上限，非消费预算
    subteam_point_ratio: float = 0.5        # 子 Team 本地上限 = 父有效上限的 50%
    max_depth: int = 2                      # agent 层级：主 agent = 第 1 层（默认两层）
    max_team_workers: int = 3               # 同时存在的直属子 agent（不含 Lead）
    max_attempts: int = 3                   # 首次 + ≤2 重试 = 最多 3 次 attempt（§8）
    # 树级 deadline，默认开启（None=关闭）。默认 4h，必须晚于单节点 2h wall-clock
    # 超时：两道时间闸各司其职（§2.1/§7）——单节点超时先兜住"某个节点无声卡死"，
    # 树级 deadline 才兜住"整棵树不收敛"；若 deadline ≤ 单节点超时，节点还没来得及
    # 转 blocked 走 Lead 归因重试，树就被整体封存（§9.6），第一道闸形同虚设。
    deadline_seconds: Optional[float] = 14400.0
    node_wallclock_timeout: float = 7200.0  # 单节点超时（转 blocked），量级参考 2h 先例
    # §9.6 封存三段式③超时兜底：SETTLEMENT 有界结算上限，超期 tick 明确
    # finish_seal(timed_out=True)——结算不得悬挂
    settlement_timeout_seconds: float = 600.0
    root_acceptance_mode: str = "lead"      # 模式细节 §10 留白，仅承载
    human_override_always: bool = True      # §2.1：人工 override 始终有效
    permission_config_ref: str = ""         # §2.1：权限配置引用（内容 §10 留白）
    point_policy_version: str = "pp-v0"     # §2.1：ModelPointPolicy 版本固化于 Spec
    cumulative_budget: Optional[dict] = None  # §2.1 optional cumulative-budget Hook
    revision: int = 1
    spec_id: str = field(default_factory=lambda: f"spec-{uuid.uuid4().hex[:8]}")


# ---------------------------------------------------------------------------
# 运行时实体（投影目标；事件日志才是唯一真源 §9.1）
# ---------------------------------------------------------------------------


@dataclass
class WorkItem:
    """一个验收单元。深度按 agent 层级计：root item depth=1。"""

    item_id: str
    kind: DelegationKind
    parent_item: Optional[str]           # 派生/裂变的父 work item；root 为 None
    team: str                            # 所属 Team id（root team = "root"）
    depth: int                           # agent 层级（root=1；分裂协助者与其主同 depth）
    acceptance: Optional[AcceptanceState] = None
    attempt: int = 0                     # 已用 attempt 数（首次执行 = 1）
    deps: List[str] = field(default_factory=list)        # DAG 依赖（上游 item_id）
    unlock_items: List[str] = field(default_factory=list)  # 反向索引
    holds_worker_slot: bool = False      # 创建即占（防 DAG 无限展开护栏）
    outcome: Optional[WorkItemOutcome] = None
    created_seq: int = 0                 # 事件序号（排序/审计）
    summary: str = ""                    # 退化回流：结论落盘、主 agent 只收摘要（§7）
    submission_package_id: Optional[str] = None  # submit 落盘证据包；review 只准引用（P1-3）
    submission_sha256: str = ""          # submit 正文内容寻址哈希


@dataclass
class Node:
    """LLM 常驻节点（worker / root lead / 协助者）。context manager 不是节点（§7）。"""

    node_id: str
    item_id: str                          # 协助者与其主共享同一 work item
    role: NodeRole
    lifecycle: LifecycleState = LifecycleState.PROVISIONING
    blocked: BlockState = BlockState.NONE
    blocked_reason: str = ""               # 阻塞原因（"wallclock-timeout" / "package-fail: ..." 等，§7/§5.8）
    route: Optional[ModelRoute] = None
    level: Level = Level.B                # 节点级别 = 模型级别（权限由自身级别决定）
    lease_id: Optional[str] = None
    team: str = "root"
    context_epoch: int = 0
    delegation_depth: int = 1             # 物理深度（rollover 保持；split 协助者 = 主+1 物理层）
    start_type: StartType = StartType.NEW
    assistant_of: Optional[str] = None    # 分裂主从：协助者记主执行者 node_id
    session_id: Optional[str] = None
    package_ref: Optional[str] = None     # 预装包（启动协议只提交不拼装 §5.8）
    package_hash: Optional[str] = None
    predecessor_session: Optional[str] = None  # rollover 专用
    activated_seq: Optional[int] = None   # 超时时钟从 active 起算（§9.3）
    activated_at: Optional[float] = None  # 最近一次 node_activated 的 ts（tick 直接读，
                                          # 替代每节点全表反扫事件日志的 O(节点×事件)）
    successor_reg: Optional[Tuple[str, int]] = None  # 已登记 successor 的 (node, epoch)
    terminated: bool = False
    # §9.3 超时抑制窗口：换模型重试/rollover 增重、点数差额不足（POINTS_EXCEEDED）
    # 时置 True——节点保持等待不启动新模型，tick 超时豁免；成功 reweight /
    # 结案（drain）/ failed 时清除。等待期间不消耗重试预算。
    reweight_wait: bool = False


@dataclass
class Lease:
    """节点点数 lease：从准入启动到 accepted/明确终止持续占用（§7）。"""

    lease_id: str
    node_id: str
    points: int
    active: bool = True


@dataclass
class Team:
    """Team：root team 或裂变产生的子 Team。裂变者即 Lead（§7）。"""

    team_id: str
    lead_node: Optional[str]              # root team 的 lead = root lead node
    parent_team: Optional[str]
    local_point_cap: Optional[int] = None  # 子 Team 本地上限（必须 < 父有效上限）
    sealed: bool = False


@dataclass
class PackageEntry:
    ref: str            # artifact 引用（路径 / store id）
    required: bool      # required：runtime 预取注入；optional：仅 pull 可达（§5.3）
    inline: bool        # 低于 inline 上限可进 persona/prompt；大包只传引用
    description: str = ""


@dataclass
class ContextPackage:
    """Assembler 产物：带来源、revision、hash 的语义包（§5.3 落盘）。"""

    package_id: str
    revision: int
    content: str
    entries: List[PackageEntry] = field(default_factory=list)
    source_pointers: List[str] = field(default_factory=list)


@dataclass
class HumanDirective:
    """人工指令三类（§9.2）：即时生效 / 配置变更 / 终态。"""

    kind: str        # "immediate" | "config" | "terminal"
    payload: Dict[str, Any] = field(default_factory=dict)
    issued_at: float = field(default_factory=time.time)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"
