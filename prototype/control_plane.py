"""DPswarm 控制面逻辑原型（非实现）。

把《DPswarm-机制架构.md》的控制面机制建成可执行的逻辑模型：
事件溯源 + invariant 落盘前校验（§9.1）、单写者原子事务（§9.2）、
两阶段启动与崩溃对账（§9.3）、三层状态词汇（§5.6）、
槽/点资源生命周期（§7）、重试预算与上交（§8）、
终态优先（§9.3）、时间方向护栏三件套（§7）。

LLM 的语义决策（Lead 验收/归因、拓扑判断）由场景脚本以指令驱动。
条款对照以 §x.y 注释标出。
"""

from __future__ import annotations

import copy
import functools
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class InvariantError(Exception):
    """候选事件未通过 append 前校验，拒绝落盘（§9.1）。"""


class CapacityError(Exception):
    """硬准入失败：槽位或点数不足（§2 代码硬准入）。"""


class BudgetError(Exception):
    """重试预算耗尽，必须走上交（§8）。"""


def atomic(fn):
    """方法级原子事务：任何一步失败，方法的全部迁移与事件整体回滚（§9.2）。"""

    @functools.wraps(fn)
    def wrapper(self: "ControlPlane", *args, **kwargs):
        snap = self._snapshot()
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            self._restore(snap)
            raise

    return wrapper


# ---- 三层状态词汇（§5.6，勿混用）----

class Physical(str, Enum):  # 物理生命周期（§9.3 启动协议）
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    FAILED = "failed"
    DRAINED = "drained"


class Blocking(str, Enum):  # 调度阻塞态
    NONE = "none"
    BLOCKED = "blocked"
    RECOVERY = "recovery"


class Acceptance(str, Enum):  # 验收流状态（内容裁决）
    RUNNING = "running"
    SUBMITTED = "submitted"
    FINALIZING = "finalizing"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ESCALATED = "escalated"            # 上交：无裁决、不进画像（§8）
    TERMINATED = "terminated"          # 退化/人工停止：不进画像（§7）
    ABORTED_FINALIZE = "aborted-finalize"  # 终态优先下的结案取消（§9.3）
    DEADLINE_STOPPED = "deadline-stopped"


TERMINAL = {
    Acceptance.ACCEPTED,
    Acceptance.ESCALATED,
    Acceptance.TERMINATED,
    Acceptance.ABORTED_FINALIZE,
    Acceptance.DEADLINE_STOPPED,
}

ACCEPTANCE_EDGES = {
    Acceptance.RUNNING: {Acceptance.SUBMITTED, Acceptance.ESCALATED,
                         Acceptance.TERMINATED, Acceptance.DEADLINE_STOPPED},
    Acceptance.SUBMITTED: {Acceptance.FINALIZING, Acceptance.REJECTED,
                           Acceptance.ESCALATED, Acceptance.TERMINATED,
                           Acceptance.DEADLINE_STOPPED},
    Acceptance.FINALIZING: {Acceptance.ACCEPTED, Acceptance.ABORTED_FINALIZE},
    Acceptance.REJECTED: {Acceptance.RUNNING, Acceptance.ESCALATED,
                          Acceptance.TERMINATED, Acceptance.DEADLINE_STOPPED},
}

PHYSICAL_EDGES = {
    Physical.PROVISIONING: {Physical.ACTIVE, Physical.FAILED, Physical.DRAINED},
    Physical.ACTIVE: {Physical.DRAINED, Physical.PROVISIONING},  # 后者 = rollover 续接
    Physical.FAILED: {Physical.PROVISIONING},  # 允许同 lease 重走启动
}

MAX_DEPTH = 3  # harness 物理上限（tool-subagent 默认）


@dataclass
class Node:
    node_id: str
    item_id: str
    kind: str            # worker | lead | assistant（manager 不占点，不入此表）
    model: str
    points: int
    depth: int
    epoch: int = 0
    physical: Physical = Physical.PROVISIONING
    blocking: Blocking = Blocking.NONE
    lease_id: str = ""
    active_ticks: int = 0          # 节点级 wall-clock，跨窗口累计
    suppress_timeout: bool = False  # rollover / reweight-wait 抑制窗口（§9.3）
    successor_registered: bool = False  # CAS 唯一 successor（§5.8）


@dataclass
class WorkItem:
    item_id: str
    parent_id: Optional[str]
    deps: tuple[str, ...]
    acceptance: Acceptance = Acceptance.RUNNING
    retries_used: int = 0
    occupies_slot: bool = True     # 普通 worker 占槽；协助者不建 item（§7 分裂主从）
    nodes: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    accepted_by: Optional[str] = None
    rejected_by: list[str] = field(default_factory=list)


class ControlPlane:
    """单写者控制面：一切变更走事件 + invariant，事务可原子回滚（§9.1/§9.2）。"""

    def __init__(self, max_open_work_items: int = 3,
                 max_active_node_points: int = 10,
                 node_timeout: int = 5,
                 deadline: Optional[int] = None,
                 settle_limit: int = 4,
                 retry_budget: int = 2):
        self.max_open = max_open_work_items
        self.max_points = max_active_node_points
        self.node_timeout = node_timeout
        self.deadline = deadline
        self.settle_limit = settle_limit
        self.retry_budget = retry_budget

        self.events: list[tuple] = []       # 唯一真源（§9.1 独立存储）
        self.items: dict[str, WorkItem] = {}
        self.nodes: dict[str, Node] = {}
        self.edges: list[tuple[str, str]] = []
        self.profile: list[dict] = []       # 画像样本：V1 只攒不用（§8）
        self.ops_audit: list[dict] = []     # 运维审计（QUOTA 等，§4）
        self.wakeups: list[str] = []        # 通知不带状态，只记"有变化"（§9.4）
        self.clock = 0
        self.admission_closed = False       # 封存第一段：准入截止（§9.6）
        self.tree_terminal: Optional[str] = None  # None | "settling" | "deadline" | 人工原因
        self._settle_left = 0
        self._id_counter = 0
        self._used_ids: set[str] = set()    # id 永不复用（§9.2）

    # ---- 基础设施 ----

    def _snapshot(self):
        return copy.deepcopy((self.events, self.items, self.nodes, self.edges,
                              self.profile, self.ops_audit, self.wakeups,
                              self.clock, self.admission_closed,
                              self.tree_terminal, self._settle_left,
                              self._id_counter, self._used_ids))

    def _restore(self, snap):
        (self.events, self.items, self.nodes, self.edges, self.profile,
         self.ops_audit, self.wakeups, self.clock, self.admission_closed,
         self.tree_terminal, self._settle_left,
         self._id_counter, self._used_ids) = snap

    def _emit(self, etype: str, **payload):
        self.events.append((len(self.events) + 1, etype, payload))
        self._check_global_invariants()

    def _check_global_invariants(self):
        if self.used_slots() > self.max_open:
            raise InvariantError("worker 槽超 maxOpenWorkItems")
        if self.used_points() > self.max_points:
            raise InvariantError("节点点数超 maxActiveNodePoints")
        self._assert_acyclic()
        for it in self.items.values():
            if it.acceptance in TERMINAL:
                for nid in it.nodes:
                    n = self.nodes[nid]
                    if n.physical in (Physical.PROVISIONING, Physical.ACTIVE):
                        raise InvariantError(
                            f"终态 item {it.item_id} 仍有在途节点 {nid}（资源未闭合）")

    def _assert_acyclic(self):
        graph: dict[str, list[str]] = {}
        for a, b in self.edges:  # a 解锁 b
            graph.setdefault(a, []).append(b)
        visiting, done = set(), set()

        def dfs(u: str):
            if u in visiting:
                raise InvariantError("DAG 存在环")
            if u in done:
                return
            visiting.add(u)
            for v in graph.get(u, []):
                dfs(v)
            visiting.discard(u)
            done.add(u)

        for u in list(graph):
            dfs(u)

    def _move_acceptance(self, it: WorkItem, to: Acceptance):
        allowed = ACCEPTANCE_EDGES.get(it.acceptance, set())
        if to not in allowed:
            raise InvariantError(
                f"验收流非法转换 {it.acceptance.value} → {to.value}")
        it.acceptance = to

    def _move_physical(self, n: Node, to: Physical):
        allowed = PHYSICAL_EDGES.get(n.physical, set())
        if to not in allowed:
            raise InvariantError(
                f"物理态非法转换 {n.physical.value} → {to.value}")
        n.physical = to

    def _fresh_id(self, prefix: str) -> str:
        self._id_counter += 1
        new = f"{prefix}-{self._id_counter}"
        if new in self._used_ids:
            raise InvariantError("id 复用被拒绝")
        self._used_ids.add(new)
        return new

    # ---- 资源口径（§7）----

    def used_slots(self) -> int:
        return sum(1 for it in self.items.values()
                   if it.occupies_slot and it.acceptance not in TERMINAL)

    def used_points(self) -> int:
        return sum(n.points for n in self.nodes.values()
                   if n.physical in (Physical.PROVISIONING, Physical.ACTIVE))

    # ---- 图变更与准入 ----

    def init_root(self, lead_model: str = "S-model", lead_points: int = 2) -> str:
        root_id = self._fresh_id("item")
        self.items[root_id] = WorkItem(root_id, parent_id=None, deps=(),
                                       occupies_slot=False)
        self._emit("item/open", item=root_id, kind="root")
        self.start_node(root_id, lead_model, lead_points, kind="lead", depth=1)
        return root_id

    @atomic
    def new_item(self, parent_id: str, deps: tuple[str, ...] = ()) -> str:
        if self.admission_closed:
            raise CapacityError("封存第一段：准入已截止（§9.6）")
        if self.tree_terminal and self.tree_terminal != "settling":
            raise InvariantError("终态树上禁止新准入（终态优先）")
        if self.used_slots() + 1 > self.max_open:
            raise CapacityError(
                f"worker 槽不足：在用 {self.used_slots()} / 上限 {self.max_open}")
        item_id = self._fresh_id("item")
        for d in deps:
            if d not in self.items:
                raise InvariantError(f"依赖 {d} 不存在")
        self.items[item_id] = WorkItem(item_id, parent_id, deps)
        self.edges.extend((d, item_id) for d in deps)
        self._emit("item/open", item=item_id, parent=parent_id, deps=deps)
        return item_id

    @atomic
    def start_node(self, item_id: str, model: str, points: int,
                   kind: str = "worker", depth: int = 2,
                   lease_id: Optional[str] = None) -> str:
        """两阶段启动第一阶段：落 provisioning 意图（§9.3）。"""
        if self.admission_closed or self.tree_terminal:
            raise CapacityError("准入已截止，禁止新启动（§9.6 封存第一段的物理臂）")
        it = self.items[item_id]
        if it.acceptance is not Acceptance.RUNNING:
            raise InvariantError(f"item {item_id} 不在可启动态：{it.acceptance.value}")
        for d in it.deps:
            if self.items[d].acceptance is not Acceptance.ACCEPTED:
                raise InvariantError(f"后继 {item_id} 的前置 {d} 未 accepted（依赖门禁）")
        if depth > MAX_DEPTH:
            raise InvariantError("超 harness 物理深度上限")
        if self.used_points() + points > self.max_points:
            raise CapacityError(
                f"点数不足：在用 {self.used_points()} + 申请 {points} > 上限 {self.max_points}")
        node_id = self._fresh_id("node")
        n = Node(node_id, item_id, kind, model, points, depth,
                 lease_id=lease_id or f"lease-{node_id}")
        self.nodes[node_id] = n
        it.nodes.append(node_id)
        self._emit("node/provisioning", node=node_id, item=item_id,
                   model=model, points=points, kind=kind)
        return node_id

    @atomic
    def activate(self, node_id: str):
        """第二阶段：session 建好、预装包 hash 确认 → active（§9.3）。"""
        if self.tree_terminal:
            n = self.nodes[node_id]
            if n.physical is Physical.PROVISIONING:
                self._move_physical(n, Physical.DRAINED)
                self._emit("node/provision-aborted", node=node_id,
                           reason="tree-terminal")
            raise InvariantError(f"树已终态/封存，node {node_id} 激活被拒绝（终态优先）")
        n = self.nodes[node_id]
        self._move_physical(n, Physical.ACTIVE)
        self._emit("node/active", node=node_id, epoch=n.epoch)

    # ---- 崩溃对账（§9.3）----

    @atomic
    def reconcile(self, node_id: str, session_ok: bool, hash_ok: bool):
        n = self.nodes[node_id]
        if n.physical is not Physical.PROVISIONING:
            return
        if self.tree_terminal:
            self._move_physical(n, Physical.DRAINED)  # 终态优先压过恢复补完
            self._emit("node/provision-aborted", node=node_id,
                       reason="terminal-priority")
            return
        if session_ok and hash_ok:
            self._move_physical(n, Physical.ACTIVE)
            self._emit("node/active", node=node_id, epoch=n.epoch, via="reconcile")
        else:
            self._move_physical(n, Physical.FAILED)
            self._emit("node/failed", node=node_id)
            # 启动失败不耗重试预算、不持有点数（§8）

    # ---- 验收流（§4 结案五步骨架）----

    @atomic
    def submit(self, node_id: str):
        n = self.nodes[node_id]
        if n.physical is not Physical.ACTIVE:
            raise InvariantError("非 active 节点不能提交")
        it = self.items[n.item_id]
        self._move_acceptance(it, Acceptance.SUBMITTED)
        self._emit("item/submitted", item=it.item_id, node=node_id)

    @atomic
    def finalize_begin(self, item_id: str):
        it = self.items[item_id]
        self._move_acceptance(it, Acceptance.FINALIZING)
        self._emit("item/finalizing", item=item_id)

    @atomic
    def finalize_write_evidence(self, item_id: str, *artifacts: str):
        it = self.items[item_id]
        if it.acceptance is not Acceptance.FINALIZING:
            raise InvariantError("只有 finalizing 能落证据")
        it.evidence.extend(artifacts)
        self._emit("evidence/committed", item=item_id, artifacts=list(artifacts))

    @atomic
    def accept(self, item_id: str, by: str):
        """原子发布 accepted：解锁后继、释放 lease、写画像含 acceptedBy（§4）。"""
        it = self.items[item_id]
        self._move_acceptance(it, Acceptance.ACCEPTED)
        it.accepted_by = by
        for nid in it.nodes:
            n = self.nodes[nid]
            # 协助者必须由主执行者显式关闭（§7 依附生命周期）；仍 active
            # 则全局 invariant（终态 item 不得有在途节点）拒绝本次 accepted
            if n.physical is Physical.ACTIVE and n.kind != "assistant":
                self._move_physical(n, Physical.DRAINED)
        self._emit("item/accepted", item=item_id, by=by)
        self.profile.append({"item": item_id, "verdict": "accepted", "by": by})

    @atomic
    def reject(self, item_id: str, by: str, reason: str):
        it = self.items[item_id]
        self._move_acceptance(it, Acceptance.REJECTED)
        it.rejected_by.append(by)
        self._emit("item/rejected", item=item_id, by=by, reason=reason)
        self.profile.append({"item": item_id, "verdict": "rejected",
                             "by": by, "reason": reason})

    # ---- 重试预算与上交（§8 + B1 单事务交接）----

    @atomic
    def retry(self, item_id: str, new_model: Optional[str] = None,
              new_points: Optional[int] = None) -> str:
        """同 lease 重试：预算共用；换模型走原子 reweight（§7 点数 Hook）。"""
        it = self.items[item_id]
        if it.acceptance is not Acceptance.REJECTED:
            raise InvariantError("只有 rejected 能重试")
        if it.retries_used >= self.retry_budget:
            raise BudgetError(f"item {item_id} 重试预算耗尽，必须上交")
        it.retries_used += 1
        last = self.nodes[it.nodes[-1]]
        model = new_model or last.model
        points = new_points if new_points is not None else last.points
        if points > last.points \
                and self.used_points() + (points - last.points) > self.max_points:
            raise CapacityError("reweight 升档差额不足，节点保持等待")  # §7
        last.points = points  # 原子 reweight
        self._move_acceptance(it, Acceptance.RUNNING)
        nid = self.start_node(item_id, model, points=0, kind=last.kind,
                              depth=last.depth, lease_id=last.lease_id)
        self._emit("item/retry", item=item_id, attempt=it.retries_used + 1,
                   model=model, lease=last.lease_id)
        return nid

    def escalate(self, item_id: str, takeover: Optional[Callable] = None):
        """上交 = 单控制面事务：原 item → escalated 释放资源，父级接管同事务准入（B1）。"""

        @atomic
        def _tx(self_):
            it = self_.items[item_id]
            self_._move_acceptance(it, Acceptance.ESCALATED)
            for nid in it.nodes:
                n = self_.nodes[nid]
                if n.physical in (Physical.PROVISIONING, Physical.ACTIVE):
                    self_._move_physical(n, Physical.DRAINED)
            self_._emit("item/escalated", item=item_id)
            # escalated 无裁决、不写 profile（§8/B5）
            if takeover is not None:
                takeover(self_)

        _tx(self)

    # ---- 时间方向护栏（§7 三件套）----

    def tick(self, n: int = 1):
        for _ in range(n):
            self._tick_one()

    @atomic
    def _tick_one(self):
        self.clock += 1
        self._emit("clock/tick", t=self.clock)
        if self.deadline is not None and self.clock >= self.deadline \
                and self.tree_terminal is None:
            self._trigger_deadline_inner()
        if self.tree_terminal == "settling":
            self._settle_left -= 1
            if self._settle_left <= 0:
                self._deadline_force_close_inner()
        for node in list(self.nodes.values()):
            if node.physical is Physical.ACTIVE \
                    and node.blocking is Blocking.NONE \
                    and not node.suppress_timeout:      # 超时抑制窗口（§9.3）
                node.active_ticks += 1
                if node.active_ticks >= self.node_timeout:
                    node.blocking = Blocking.BLOCKED
                    self._emit("node/blocked", node=node.node_id,
                               reason="wall-clock-timeout")
                    if node.kind == "assistant":
                        main = self.nodes[it_main(self, node.item_id)]
                        self.wakeups.append(main.node_id)  # 只 wakeup 主执行者（B3）
                        self._emit("wakeup", target=main.node_id,
                                   cause="assistant-blocked")

    def trigger_deadline(self):
        self._trigger_deadline_inner()

    @atomic
    def _trigger_deadline_inner(self):
        """封存三段式（§9.6）：准入截止 → 有界结算 → 超时兜底。"""
        self.admission_closed = True
        self.tree_terminal = "settling"
        self._settle_left = self.settle_limit
        self._emit("tree/deadline-admission-closed")

    @atomic
    def _deadline_force_close_inner(self):
        # 顺序铁律：先回收节点，后改 item 终态——中间态不得出现
        # "终态 item 仍有在途节点"（invariant 在每个事件后全量校验）。
        for node in self.nodes.values():
            if node.physical in (Physical.PROVISIONING, Physical.ACTIVE):
                node.physical = Physical.DRAINED
                self._emit("node/drained", node=node.node_id, cause="deadline")
        for it in self.items.values():
            if it.acceptance in TERMINAL:
                continue
            if it.acceptance is Acceptance.FINALIZING:
                self._move_acceptance(it, Acceptance.ABORTED_FINALIZE)
                self._emit("item/aborted-finalize", item=it.item_id,
                           note="证据保留·不解锁后继·释放 lease")
            else:
                self._move_acceptance(it, Acceptance.DEADLINE_STOPPED)
                self._emit("item/deadline-stopped", item=it.item_id)
        self.tree_terminal = "deadline"
        self._emit("tree/sealed")

    @atomic
    def terminate_tree(self, reason: str = "manual-stop"):
        """人工停止：终态型指令，立即生效，FINALIZING 转 aborted-finalize（§9.3）。"""
        for node in self.nodes.values():  # 先回收节点（含 CAS successor 作废）
            if node.physical in (Physical.PROVISIONING, Physical.ACTIVE):
                node.physical = Physical.DRAINED
                node.successor_registered = False
                self._emit("node/drained", node=node.node_id, cause=reason)
        for it in self.items.values():    # 后改 item 终态
            if it.acceptance in TERMINAL:
                continue
            if it.acceptance is Acceptance.FINALIZING:
                self._move_acceptance(it, Acceptance.ABORTED_FINALIZE)
                self._emit("item/aborted-finalize", item=it.item_id)
            else:
                self._move_acceptance(it, Acceptance.TERMINATED)
                self._emit("item/terminated", item=it.item_id)
        self.tree_terminal = reason
        self._emit("tree/terminated", reason=reason)

    # ---- 硬切 rollover（§5.8 + §9.3）----

    @atomic
    def rollover_begin(self, node_id: str):
        """CAS 登记唯一 successor（§5.8）；rollover 期间为超时抑制窗口。"""
        n = self.nodes[node_id]
        if n.successor_registered:
            raise InvariantError("CAS：已存在 successor 登记，禁止双 successor")
        n.successor_registered = True
        n.suppress_timeout = True
        self._move_physical(n, Physical.PROVISIONING)  # successor 启动事务
        n.epoch += 1
        self._emit("node/rollover-begin", node=node_id, epoch=n.epoch,
                   lease=n.lease_id)

    @atomic
    def rollover_complete(self, node_id: str, capsule_ok: bool = True):
        n = self.nodes[node_id]
        if not capsule_ok:
            # 预装失败：不登记 successor、epoch 回退、转 blocked、lease 保持（§5.8）
            n.blocking = Blocking.BLOCKED
            self._move_physical(n, Physical.ACTIVE)
            n.epoch -= 1
            n.successor_registered = False
            n.suppress_timeout = False
            self._emit("node/rollover-aborted", node=node_id,
                       reason="capsule-build-failed")
            return
        depth_before = n.depth
        self._move_physical(n, Physical.ACTIVE)
        n.suppress_timeout = False
        n.successor_registered = False  # CAS 登记被本次成功 rollover 消费
        assert n.depth == depth_before, "rollover 必须保持深度（§9.3 深度保持）"
        self._emit("node/active", node=node_id, epoch=n.epoch, via="rollover",
                   lease=n.lease_id)

    # ---- 分裂主从（§7 + B3）----

    def split(self, item_id: str, model: str, points: int) -> str:
        """建依附型协助者：占点不占槽、不进 DAG、深度 = 主执行者 + 1（物理）。"""
        main = self.nodes[it_main(self, item_id)]
        return self.start_node(item_id, model, points, kind="assistant",
                               depth=main.depth + 1)

    @atomic
    def assistant_close(self, node_id: str, verdict: str, by_main: str):
        """协助者关闭：主执行者代写奖励信号进观测（B3）。"""
        n = self.nodes[node_id]
        if n.kind != "assistant":
            raise InvariantError("只有协助者走此通道")
        self._move_physical(n, Physical.DRAINED)
        self._emit("assistant/closed", node=node_id, verdict=verdict)
        self.profile.append({"item": n.item_id, "verdict": verdict,
                             "by": by_main, "role": "assistant"})

    # ---- LLM 失败归类（§4：RATE_LIMIT 背压 vs QUOTA 终止）----

    def llm_failure(self, node_id: str, code: str):
        n = self.nodes[node_id]
        if code == "RATE_LIMIT":
            self._emit("llm/rate-limited", node=node_id)  # 背压：退避，不进画像
        elif code == "QUOTA":
            self.ops_audit.append({"node": node_id, "code": "QUOTA"})
            self._emit("llm/quota-exhausted", node=node_id)
            self.escalate(n.item_id)  # 余额耗尽不是背压：上交人工（§4）
        else:
            raise InvariantError(f"未知失败码 {code}")


def it_main(cp: ControlPlane, item_id: str) -> str:
    """work item 的主执行者节点 id（分裂对的主侧）。"""
    for nid in cp.items[item_id].nodes:
        if cp.nodes[nid].kind in ("worker", "lead"):
            return nid
    raise InvariantError(f"item {item_id} 无主执行者")
