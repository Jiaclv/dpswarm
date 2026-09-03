"""DPswarm 机制文档的独立、可执行逻辑原型（仅 Python 标准库）。"""
from __future__ import annotations
import copy, hashlib, json, os, tempfile
from dataclasses import dataclass, field
from typing import Any

LEVEL = {"D": 0, "C": 1, "B": 2, "A": 3, "S": 4}
TERMINAL = {"accepted", "terminated", "escalated"}


class RuleViolation(Exception):
    pass


@dataclass
class RootSpec:
    max_open: int = 3
    max_points: int = 10
    max_depth: int = 2
    max_team_workers: int = 3
    deadline: float | None = None
    seal_timeout: float = 1.0


@dataclass
class Projection:
    root: str
    items: dict[str, dict] = field(default_factory=dict)
    nodes: dict[str, dict] = field(default_factory=dict)
    leases: dict[str, dict] = field(default_factory=dict)
    edges: set[tuple[str, str]] = field(default_factory=set)
    graph_revision: int = 0
    used_slots: int = 0
    used_points: int = 0
    sealing: bool = False
    sealed: bool = False
    next_ids: set[str] = field(default_factory=set)
    packages: dict[str, dict] = field(default_factory=dict)
    successors: dict[tuple[str, int], dict] = field(default_factory=dict)
    mailbox: dict[str, dict] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)


class ControlPlane:
    """单写者事件日志；所有 public mutation 都 append-and-flush。"""
    def __init__(self, root="root-1", spec=None, path=None, clock=None):
        self.spec = spec or RootSpec()
        self.path = path or os.path.join(tempfile.gettempdir(), "dpswarm-luna.jsonl")
        open(self.path, "w", encoding="utf-8").close()
        self.clock = clock or (lambda: 0.0)
        self.p = Projection(root)
        self.events: list[dict] = []

    def _apply(self, p, e):
        t, d = e["type"], e.get("data", {})
        if t == "item_created":
            p.items[d["id"]] = {"status": "open", "depth": d["depth"], "team": d.get("team")}
            p.next_ids.add(d["id"])
        elif t == "node_provisioning":
            p.nodes[d["id"]] = {"physical": "provisioning", "item": d["item"],
                                "owner": d["owner"], "depth": d["depth"],
                                "model": d["model"], "lease": d["lease"],
                                "epoch": d.get("epoch", 0), "session": None,
                                "kind": d.get("kind", "worker"), "started": None}
            if d["lease"] not in p.leases:
                p.leases[d["lease"]] = {"node": d["id"], "points": d["points"], "slot": d.get("slot", True)}
            p.items[d["item"]]["status"] = "running"
        elif t == "node_active":
            n = p.nodes[d["id"]]; n["physical"], n["session"], n["started"] = "active", d["session"], self.clock()
            n["manifest_hash"] = d["manifest_hash"]
        elif t == "node_failed":
            p.nodes[d["id"]]["physical"] = "failed"
        elif t == "node_drain":
            n = p.nodes[d["id"]]; n["physical"] = "terminated"
            still_using = any(x["physical"] in {"provisioning", "active"} and
                              x["lease"] == n["lease"] for k, x in p.nodes.items() if k != d["id"])
            if not still_using:
                l = p.leases.pop(n["lease"], None)
                if l: n["released_points"] = l["points"]
        elif t == "node_archive":
            p.nodes[d["id"]]["physical"] = "archived"
        elif t == "submitted":
            p.items[d["item"]].update(status="submitted", attempt=d["attempt"])
        elif t == "finalizing":
            p.items[d["item"]]["status"] = "finalizing"
        elif t == "accepted":
            p.items[d["item"]].update(status="accepted", accepted_by=d["accepted_by"])
            p.evidence.append(d["evidence"])
        elif t == "rejected":
            p.items[d["item"]].update(status="rejected", attempt=d["attempt"])
            p.audit.append(d)
        elif t == "retry_prepared":
            p.items[d["item"]]["attempt"] = d["attempt"]
        elif t == "blocked":
            p.items[d["item"]]["status"] = "blocked"
        elif t == "escalated":
            p.items[d["item"]]["status"] = "escalated"
            p.audit.append({"item": d["item"], "reason": "escalated"})
        elif t == "terminated":
            p.items[d["item"]]["status"] = "terminated"
        elif t == "edge_added":
            p.edges.add((d["before"], d["after"])); p.graph_revision += 1
        elif t == "package":
            p.packages[d["id"]] = d
        elif t == "successor_reserved":
            p.successors[(d["node"], d["epoch"])] = d
        elif t == "successor_reset":
            p.successors.pop((d["node"], d["epoch"]), None)
        elif t == "mail_queued":
            p.mailbox[d["id"]] = dict(d, state="queued")
        elif t == "mail_delivered":
            p.mailbox[d["id"]]["state"] = "delivered"
        elif t == "seal_begin":
            p.sealing = True
        elif t == "seal_end":
            p.sealed = True
        elif t == "spec_revision":
            pass

    def _invariants(self, p):
        if p.used_slots > self.spec.max_open: raise RuleViolation("worker 槽超限")
        if p.used_points > self.spec.max_points: raise RuleViolation("节点点数超限")
        for i, x in p.items.items():
            if x["status"] in TERMINAL:
                live = [n for n in p.nodes.values() if n["item"] == i and n["physical"] in {"provisioning", "active"}]
                if live: raise RuleViolation("终态仍有在途节点")
        for a, b in p.edges:
            if a not in p.items or b not in p.items: raise RuleViolation("依赖缺失")
        # DAG 全量环检测
        seen, stack = set(), set()
        def visit(x):
            if x in stack: raise RuleViolation("DAG 环")
            if x in seen: return
            stack.add(x)
            for a, b in p.edges:
                if a == x: visit(b)
            stack.remove(x); seen.add(x)
        for x in p.items: visit(x)
        for i in p.items:
            attempts = p.items[i].get("attempt", 0)
            if attempts > 2: raise RuleViolation("重试预算超限")

    def append(self, typ, **data):
        candidate = copy.deepcopy(self.p)
        self._apply(candidate, {"type": typ, "data": data})
        # 资源计数是投影的派生量，验证候选事件后的完整状态。
        candidate.used_slots = sum(1 for n in candidate.nodes.values()
                                   if n["physical"] in {"provisioning", "active"}
                                   and n["kind"] == "worker" and n["item"] in candidate.items)
        candidate.used_points = sum(x["points"] for x in candidate.leases.values())
        self._invariants(candidate)
        event = {"seq": len(self.events), "type": typ, "data": data}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n"); f.flush(); os.fsync(f.fileno())
        self.events.append(event); self.p = candidate
        return event

    def create_item(self, item, depth=1, team=None):
        if self.p.sealing or item in self.p.next_ids: raise RuleViolation("禁止创建或 ID 重用")
        if depth > self.spec.max_depth: raise RuleViolation("语义深度超限")
        return self.append("item_created", id=item, depth=depth, team=team)

    def add_dependency(self, before, after, expected_revision):
        if expected_revision != self.p.graph_revision: raise RuleViolation("CAS graph revision 过期")
        return self.append("edge_added", before=before, after=after)

    def start(self, node, item, owner, model, points, depth=1, kind="worker",
              manifest=b"manifest", epoch=0):
        if self.p.sealing: raise RuleViolation("准入已截止")
        if item not in self.p.items or self.p.items[item]["status"] in TERMINAL: raise RuleViolation("item 不可启动")
        if node in self.p.nodes or node in self.p.next_ids: raise RuleViolation("node ID 重用")
        if kind == "worker" and self.p.used_slots >= self.spec.max_open: raise RuleViolation("worker 槽不足")
        if self.p.used_points + points > self.spec.max_points: raise RuleViolation("点数不足")
        if depth > self.spec.max_depth: raise RuleViolation("深度超限")
        h = hashlib.sha256(manifest).hexdigest()
        self.append("node_provisioning", id=node, item=item, owner=owner, depth=depth,
                    model=model, points=points, lease="lease-"+node, epoch=epoch, kind=kind)
        return self.append("node_active", id=node, session="session-"+node, manifest_hash=h)

    def submit(self, item, attempt=0):
        if self.p.items[item]["status"] not in {"running", "blocked", "rejected"}: raise RuleViolation("非法提交")
        return self.append("submitted", item=item, attempt=attempt)

    def accept(self, item, accepted_by, evidence=b"evidence"):
        if self.p.items[item]["status"] != "submitted": raise RuleViolation("只能验收 submitted")
        self.append("finalizing", item=item)
        # 证据/package 在链外完成，原型用不可变 hash 作为提交证明。
        package = "pkg-" + item
        self.append("package", id=package, item=item, hash=hashlib.sha256(evidence).hexdigest())
        # 先 drain，再终态，保证每个中间事件也满足 invariant。
        for nid, n in list(self.p.nodes.items()):
            if n["item"] == item and n["physical"] in {"active", "provisioning"}:
                self.append("node_drain", id=nid)
        return self.append("accepted", item=item, accepted_by=accepted_by, evidence={"item": item, "package": package})

    def reject(self, item, reason, attempt):
        if self.p.items[item]["status"] != "submitted": raise RuleViolation("只能打回 submitted")
        return self.append("rejected", item=item, reason=reason, attempt=attempt)

    def retry(self, item, attempt, model=None):
        if attempt > 2: raise RuleViolation("最多两次重试")
        if self.p.items[item]["status"] not in {"rejected", "blocked"}: raise RuleViolation("只能重试 rejected/blocked")
        self.append("retry_prepared", item=item, attempt=attempt, model=model)
        return self.p.items[item]

    def escalate(self, item, reason):
        if self.p.items[item]["status"] not in {"rejected", "blocked"}: raise RuleViolation("非法上交")
        # 资源回收必须先于终态事件。
        for nid, n in list(self.p.nodes.items()):
            if n["item"] == item and n["physical"] in {"active", "provisioning"}:
                self.append("node_drain", id=nid)
        return self.append("escalated", item=item, reason=reason)

    def terminate(self, item, reason="manual"):
        if self.p.items[item]["status"] in TERMINAL: raise RuleViolation("终态不可恢复")
        for nid, n in list(self.p.nodes.items()):
            if n["item"] == item and n["physical"] in {"active", "provisioning"}:
                self.append("node_drain", id=nid)
        return self.append("terminated", item=item, reason=reason)

    def reserve_successor(self, node, epoch):
        key = (node, epoch)
        if key in self.p.successors: raise RuleViolation("successor 重复")
        return self.append("successor_reserved", node=node, epoch=epoch)

    def reset_successor(self, node, epoch):
        return self.append("successor_reset", node=node, epoch=epoch)

    def rollover(self, node, manifest=b"capsule"):
        n = self.p.nodes[node]
        if n["physical"] != "active": raise RuleViolation("只能从 active rollover")
        epoch = n["epoch"] + 1
        self.reserve_successor(node, epoch)
        h = hashlib.sha256(manifest).hexdigest()
        # successor 仍是同 node/item/lease/depth，且不增加 attempt/资源。
        self.append("node_archive", id=node)
        self.append("node_provisioning", id=node+"@"+str(epoch), item=n["item"], owner=n["owner"],
                    depth=n["depth"], model=n["model"], points=0, lease=n["lease"],
                    epoch=epoch, kind=n["kind"])
        self.append("node_active", id=node+"@"+str(epoch), session="session-"+node+"@"+str(epoch),
                    manifest_hash=h)
        self.reset_successor(node, epoch)

    def send(self, mid, sender, target, body):
        if mid in self.p.mailbox: raise RuleViolation("消息重复")
        return self.append("mail_queued", id=mid, sender=sender, target=target, body=body)

    def deliver(self, mid):
        if mid not in self.p.mailbox or self.p.mailbox[mid]["state"] != "queued": raise RuleViolation("消息不可投递")
        return self.append("mail_delivered", id=mid)

    def seal(self, item=None):
        self.append("seal_begin")
        # 原型把准入截止、已准入节点 drain、终态迁移作为同一控制事务序列。
        for i, x in list(self.p.items.items()):
            if x["status"] not in TERMINAL:
                self.terminate(i, "deadline" if item is None else "manual")
        return self.append("seal_end")

    def check_timeout(self, node):
        n = self.p.nodes[node]
        if n["physical"] == "active" and self.clock() - n["started"] > 1:
            self.append("blocked", item=n["item"], reason="timeout")

    @staticmethod
    def recover(events, spec=None):
        cp = ControlPlane(spec=spec)
        cp.events = []
        cp.p = Projection("recovered")
        for e in events:
            cp._apply(cp.p, e)
            cp.events.append(e)
        cp.p.used_slots = sum(1 for n in cp.p.nodes.values() if n["physical"] in {"provisioning", "active"} and n["kind"] == "worker")
        cp.p.used_points = sum(x["points"] for x in cp.p.leases.values())
        cp._invariants(cp.p)
        return cp
