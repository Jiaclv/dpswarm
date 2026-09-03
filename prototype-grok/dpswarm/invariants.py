"""append 前全量 invariant。非法转换拒绝落盘。"""

from __future__ import annotations

from typing import Optional

from .store import AdmissionError, ControlError, Event, Projection, apply_event
from .types import (
    AcceptanceState,
    BlockedState,
    LEVEL_RANK,
    Level,
    NodeRole,
    PhysicalState,
    RESOURCE_RELEASE_ACCEPTANCE,
    TERMINAL_ACCEPTANCE,
)


ACCEPTANCE_TRANSITIONS = {
    AcceptanceState.NONE: {
        AcceptanceState.SUBMITTED,
        AcceptanceState.TERMINATED,
        AcceptanceState.ESCALATED,
    },
    AcceptanceState.SUBMITTED: {
        AcceptanceState.FINALIZING,
        AcceptanceState.REJECTED,
        AcceptanceState.TERMINATED,
        AcceptanceState.ESCALATED,
    },
    AcceptanceState.REJECTED: {
        AcceptanceState.NONE,
        AcceptanceState.ESCALATED,
        AcceptanceState.TERMINATED,
    },
    AcceptanceState.FINALIZING: {
        AcceptanceState.ACCEPTED,
        AcceptanceState.ABORTED_FINALIZE,
        AcceptanceState.TERMINATED,
    },
    AcceptanceState.ACCEPTED: set(),
    AcceptanceState.TERMINATED: set(),
    AcceptanceState.ESCALATED: set(),
    AcceptanceState.ABORTED_FINALIZE: set(),
}

PHYSICAL_TRANSITIONS = {
    PhysicalState.PROVISIONING: {
        PhysicalState.ACTIVE,
        PhysicalState.FAILED,
        PhysicalState.DRAINED,
        PhysicalState.PROVISIONING,  # 幂等补走
    },
    PhysicalState.ACTIVE: {
        PhysicalState.DRAINED,
        PhysicalState.PROVISIONING,  # rollover 新窗口
        PhysicalState.FAILED,
        PhysicalState.ACTIVE,
    },
    PhysicalState.FAILED: {
        PhysicalState.PROVISIONING,  # 启动失败可重试，不耗预算
        PhysicalState.DRAINED,
        PhysicalState.FAILED,
    },
    PhysicalState.DRAINED: {
        PhysicalState.DRAINED,
    },
}


def has_cycle(edges: list[tuple[str, str]]) -> bool:
    graph: dict[str, list[str]] = {}
    for s, d in edges:
        graph.setdefault(s, []).append(d)
        graph.setdefault(d, [])
    visiting = set()
    visited = set()

    def dfs(n: str) -> bool:
        if n in visiting:
            return True
        if n in visited:
            return False
        visiting.add(n)
        for m in graph.get(n, []):
            if dfs(m):
                return True
        visiting.remove(n)
        visited.add(n)
        return False

    return any(dfs(n) for n in graph)


def check_event(proj: Projection, ev: Event) -> None:
    """在已提交前缀的投影上校验候选事件；失败则拒绝。"""
    t = ev.type
    p = ev.payload

    if t == "WorkItemCreated":
        _chk_id_fresh(proj, p["work_item_id"])
        _chk_admission_cutoff(proj, "start_node/work_item")
        spec = proj.spec
        assert spec is not None
        if p.get("kind") == "worker":
            if proj.open_worker_slots() >= spec.max_open_work_items:
                raise AdmissionError("SLOT_CAPACITY", "open worker slots exhausted", used=proj.open_worker_slots())
            team_id = p.get("team_id")
            if team_id and team_id in proj.teams and p.get("counts_as_team_worker", True):
                if proj.live_direct_workers(team_id) >= spec.max_team_workers:
                    raise AdmissionError("TEAM_SIZE", "maxTeamWorkers simultaneous cap")
        if p.get("layer", 1) > spec.max_semantic_depth:
            raise AdmissionError("DEPTH_SEMANTIC", "semantic depth exceeded", layer=p.get("layer"))
        if p.get("predecessors"):
            for pred in p["predecessors"]:
                if pred not in proj.work_items and pred != p["work_item_id"]:
                    # edge added later; create may list them if already exist
                    if pred not in proj.work_items:
                        raise ControlError("DEP_MISSING", "predecessor not found", pred=pred)

    if t == "DAGEdgeAdded":
        if p["src"] not in proj.work_items or p["dst"] not in proj.work_items:
            raise ControlError("DEP_MISSING", "edge endpoint missing")
        trial_edges = list(proj.edges) + [(p["src"], p["dst"])]
        if has_cycle(trial_edges):
            raise ControlError("CYCLE", "DAG cycle detected")
        if (p["src"], p["dst"]) in proj.edges:
            raise ControlError("DEP_DUPLICATE", "duplicate edge")
        # 协助者不得出现在 DAG
        for nid, node in proj.nodes.items():
            if node.role == NodeRole.ASSISTANT:
                if node.work_item_id in (p["src"], p["dst"]) and node.assistant_of:
                    # work item is the primary's; assistants don't have their own WI in DAG.
                    pass

    if t == "NodeProvisioning":
        _chk_id_maybe_reuse_same_node(proj, p)
        spec = proj.spec
        assert spec is not None
        start_kind = p["start_kind"]
        if proj.admission_cutoff and start_kind == "new":
            raise AdmissionError("ADMISSION_CUTOFF", "admission cutoff blocks start_node")
        if p.get("physical_depth", 1) > spec.physical_max_depth:
            raise AdmissionError("DEPTH_PHYSICAL", "physical depth exceeded")
        if start_kind == "new":
            _chk_points_admission(proj, p)
            _chk_level(proj, p)
            _chk_deps_unlocked(proj, p)
            if not p.get("required_prefetched") and p.get("role") != "lead":
                # root lead 允许空包开工；普通节点必须预装
                if p.get("needs_package", True) and not p.get("allow_empty_package"):
                    raise AdmissionError("REQUIRED_NOT_PREFETCHED", "required package entries not prefetched")
        if start_kind == "rollover":
            existing = proj.nodes.get(p["node_id"])
            if existing is None:
                raise ControlError("NO_PREDECESSOR", "rollover without node")
            if existing.successor_registered and existing.physical == PhysicalState.PROVISIONING:
                # 幂等补走
                pass
            elif existing.successor_registered is False and p.get("require_cas", True):
                # 允许 CAS 已登记后补 provisioning；若既无 CAS 又无例外则拒绝双 successor 语义由 CAS 事件管
                pass
            if p.get("physical_depth") != existing.physical_depth:
                raise ControlError("DEPTH_KEEP", "rollover must copy predecessor physical depth")
            if p.get("lease_id") != existing.lease_id:
                raise ControlError("LEASE_KEEP", "rollover must keep original lease")
            if p.get("work_item_id") != existing.work_item_id:
                raise ControlError("WI_KEEP", "rollover must keep work_item_id")

    if t == "SuccessorRegistered":
        node = proj.nodes[p["node_id"]]
        if node.successor_registered:
            raise ControlError("SUCCESSOR_EXISTS", "(node_id, context_epoch) unique successor violated")
        if p["context_epoch"] != node.context_epoch:
            raise ControlError("EPOCH_MISMATCH", "CAS epoch mismatch")

    if t == "NodeActivated":
        node = proj.nodes[p["node_id"]]
        _chk_physical(node.physical, PhysicalState.ACTIVE)
        if p.get("fence_token") is not None and p["fence_token"] != node.fence_token:
            raise ControlError("FENCE", "stale session cannot overwrite; fence mismatch")
        if p.get("package_hash") and node.package_hash and p["package_hash"] != node.package_hash:
            raise AdmissionError("PACKAGE_HASH_MISMATCH", "initial content hash mismatch")
        if (
            node.start_kind.value == "new"
            and node.role != NodeRole.LEAD
            and not node.package_ready
            and not node.required_prefetched
        ):
            raise AdmissionError("REQUIRED_NOT_PREFETCHED", "cannot become active without required prefetch")

    if t == "NodeSubmitted":
        node = proj.nodes[p["node_id"]]
        _chk_acceptance(node.acceptance, AcceptanceState.SUBMITTED)
        if node.role == NodeRole.ASSISTANT:
            raise ControlError("ASSISTANT_NO_SUBMIT", "assistant cannot independently submit")
        if node.physical != PhysicalState.ACTIVE:
            raise ControlError("ILLEGAL_TRANSITION", "submit requires active physical")

    if t == "NodeFinalizing":
        node = proj.nodes[p["node_id"]]
        _chk_acceptance(node.acceptance, AcceptanceState.FINALIZING)

    if t == "NodeAccepted":
        node = proj.nodes[p["node_id"]]
        _chk_acceptance(node.acceptance, AcceptanceState.ACCEPTED)
        if node.acceptance != AcceptanceState.FINALIZING:
            raise ControlError("ILLEGAL_TRANSITION", "accepted must come from finalizing")
        if not node.evidence_hash or not node.package_revision:
            raise ControlError("FINALIZING_INCOMPLETE", "dependency package not committed")
        wi = proj.work_items.get(node.work_item_id or "")
        if wi and wi.assistant_node_id:
            ast = proj.nodes[wi.assistant_node_id]
            if ast.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE) or ast.acceptance not in TERMINAL_ACCEPTANCE:
                if ast.acceptance not in TERMINAL_ACCEPTANCE and ast.physical != PhysicalState.DRAINED:
                    raise ControlError("ASSISTANT_STILL_OPEN", "assistant must be closed before primary accepted/terminated")

    if t == "NodeTerminated":
        node = proj.nodes[p["node_id"]]
        if node.acceptance in TERMINAL_ACCEPTANCE:
            raise ControlError("ILLEGAL_TRANSITION", "already terminal")
        _chk_acceptance(node.acceptance, AcceptanceState.TERMINATED)
        wi = proj.work_items.get(node.work_item_id or "")
        if wi and wi.assistant_node_id and node.role != NodeRole.ASSISTANT:
            ast = proj.nodes[wi.assistant_node_id]
            if ast.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE) and ast.acceptance not in TERMINAL_ACCEPTANCE:
                raise ControlError("ASSISTANT_STILL_OPEN", "assistant must be closed before primary terminated")

    if t == "NodeAbortedFinalize":
        node = proj.nodes[p["node_id"]]
        if node.acceptance != AcceptanceState.FINALIZING:
            raise ControlError("ILLEGAL_TRANSITION", "aborted-finalize only from finalizing")

    if t == "WorkItemEscalated":
        wi = proj.work_items[p["work_item_id"]]
        inflight = [
            n
            for n in proj.in_flight_nodes_for_item(wi.work_item_id)
            if proj.nodes[n].physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE)
        ]
        if inflight:
            raise ControlError("IN_FLIGHT_ON_TERMINAL_ITEM", "recycle nodes before item terminal", inflight=inflight)

    if t == "NodeRejected":
        node = proj.nodes[p["node_id"]]
        _chk_acceptance(node.acceptance, AcceptanceState.REJECTED)

    if t == "AttemptRecorded":
        spec = proj.spec
        assert spec is not None
        if p["attempt"] > 1 + spec.max_retries:
            raise AdmissionError("RETRY_EXHAUSTED", "first + 2 retries = max 3 attempts")
        node = proj.nodes[p["node_id"]]
        retry_ok = (
            node.acceptance == AcceptanceState.REJECTED
            or node.blocked != BlockedState.NONE
            or node.physical == PhysicalState.FAILED
        )
        if not retry_ok:
            raise ControlError("ILLEGAL_TRANSITION", "retry only from rejected, blocked/recovery, or start-failed")

    if t == "LeaseReleased":
        lease = proj.leases[p["lease_id"]]
        if lease.released:
            raise ControlError("LEASE_DOUBLE_RELEASE", "lease already released")

    if t == "PeerChannelOpened":
        wi = proj.work_items[p["work_item_id"]]
        if wi.assistant_node_id:
            raise AdmissionError("SPLIT_SCALE", "split is 1 primary + 1 assistant")

    if t == "PeerMessageQueued":
        ch = proj.channels[p["channel_id"]]
        if ch.closed:
            raise ControlError("CHANNEL_CLOSED", "channel no longer accepts messages")

    if t == "MemoryPromoted":
        mem = proj.memories[p["memory_id"]]
        if mem.status.value != "candidate":
            raise ControlError("MEMORY_NOT_PROMOTED", "only candidate can promote")
        src = proj.nodes[mem.source_node_id]
        if mem.kind == "accepted" and src.acceptance != AcceptanceState.ACCEPTED:
            raise ControlError("MEMORY_NOT_PROMOTED", "submitted cannot promote; need accepted")
        if mem.kind == "failure_finding" and not mem.confirmed:
            raise ControlError("MEMORY_NOT_PROMOTED", "failure finding needs Lead confirmation")

    if t == "MemoryCandidateAdded":
        src = proj.nodes[p["source_node_id"]]
        if src.acceptance == AcceptanceState.SUBMITTED:
            raise ControlError("MEMORY_RACE", "extraction must not run at submitted")
        if p["kind"] == "accepted" and src.acceptance != AcceptanceState.ACCEPTED:
            raise ControlError("MEMORY_RACE", "accepted-memory only after stable close")
        if p["kind"] == "failure_finding" and src.acceptance == AcceptanceState.SUBMITTED:
            raise ControlError("MEMORY_RACE", "failure finding not at submitted")

    if t == "TokensRecorded":
        # cacheRead / cacheWrite 与 input 不相交：以独立字段记账，禁止把 cache 计入 input
        if p.get("cache_in_input"):
            raise ControlError("TOKEN_OVERLAP", "cache tokens must be disjoint from input")

    if t == "ContextManagerAcquired":
        spec = proj.spec
        assert spec is not None
        if proj.cm_inflight >= spec.context_manager_semaphore:
            raise AdmissionError("SEMAPHORE_EXHAUSTED", "context manager concurrency limit")

    if t == "GraphRevisionBumped":
        expected = p.get("expected_graph_revision")
        if expected is not None and expected != proj.graph_revision:
            raise ControlError("GRAPH_REVISION_STALE", "stale graph replica")

    if t in ("NodeTerminated", "WorkItemEscalated", "NodeAbortedFinalize"):
        # 终态 item 不得仍有在途节点：在这些事件应用前检查对应 item
        pass

    if t == "NodeDrained":
        pass

    # 全图重验在 apply 后由 check_projection 做


def check_projection(proj: Projection) -> None:
    spec = proj.spec
    if spec is None:
        return
    if has_cycle(proj.edges):
        raise ControlError("CYCLE", "DAG cycle in projection")
    for s, d in proj.edges:
        if s not in proj.work_items or d not in proj.work_items:
            raise ControlError("DEP_MISSING", "edge endpoint missing in projection")
        src_node = proj.nodes.get(proj.work_items[s].primary_node_id)
        # 协助者 work item 不独立存在，边只连 WI
        ast = proj.work_items[s].assistant_node_id
        if ast:
            ast_node = proj.nodes[ast]
            if ast_node.work_item_id == d or ast_node.node_id in (s, d):
                raise ControlError("ASSISTANT_IN_DAG", "assistant must not appear in DAG edges")

    # §9.6 批量终态：terminated/escalated/aborted-finalize 不得仍有在途节点。
    # accepted 允许节点仍 active（结案第五步才清理窗口）。
    for wi in proj.work_items.values():
        if wi.status in (
            AcceptanceState.TERMINATED,
            AcceptanceState.ESCALATED,
            AcceptanceState.ABORTED_FINALIZE,
        ):
            for nid in proj.in_flight_nodes_for_item(wi.work_item_id):
                raise ControlError(
                    "IN_FLIGHT_ON_TERMINAL_ITEM",
                    "terminal work item still has in-flight node",
                    work_item=wi.work_item_id,
                    node=nid,
                )

    # 所有层级引用同一 spec
    for team in proj.teams.values():
        if team.spec_id != proj.spec_id:
            raise ControlError("SPEC_FORK", "subteam must not copy independent spec")

    # 槽位：仅新准入时强制 <= max；spec 降容后占用可暂时超过
    # 点数同理——check_projection 不因降容超标而失败（那是准入门）
    # 但新 lease 未释放时不应 silently 超过除非降容
    # 由准入路径保证

    # lease 与占用一致
    for node in proj.nodes.values():
        if node.lease_id:
            lease = proj.leases[node.lease_id]
            released = lease.released
            should_hold = node.acceptance not in RESOURCE_RELEASE_ACCEPTANCE
            if should_hold and released:
                raise ControlError("LEASE_ATOMICITY", "open node has released points lease", node=node.node_id)
            if (not should_hold) and (not released) and node.acceptance in RESOURCE_RELEASE_ACCEPTANCE:
                raise ControlError("LEASE_ATOMICITY", "terminal node still holds points lease", node=node.node_id)

    for wi in proj.work_items.values():
        if wi.kind != "worker" or not wi.slot_lease_id:
            continue
        lease = proj.leases[wi.slot_lease_id]
        open_item = wi.status not in RESOURCE_RELEASE_ACCEPTANCE
        if open_item and lease.released:
            raise ControlError("LEASE_ATOMICITY", "open work item released slot", wi=wi.work_item_id)
        if (not open_item) and (not lease.released):
            raise ControlError("LEASE_ATOMICITY", "terminal work item still holds slot", wi=wi.work_item_id)


def _chk_id_fresh(proj: Projection, ident: str) -> None:
    if ident in proj.used_ids:
        raise ControlError("ID_REUSED", "ids never reused within root", id=ident)


def _chk_id_maybe_reuse_same_node(proj: Projection, p: dict) -> None:
    nid = p["node_id"]
    if nid in proj.used_ids and nid not in proj.nodes:
        raise ControlError("ID_REUSED", "id reused after terminate", id=nid)


def _chk_admission_cutoff(proj: Projection, what: str) -> None:
    if proj.admission_cutoff:
        raise AdmissionError("ADMISSION_CUTOFF", f"admission cutoff blocks {what}")


def _chk_acceptance(current: AcceptanceState, target: AcceptanceState) -> None:
    allowed = ACCEPTANCE_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ControlError(
            "ILLEGAL_TRANSITION",
            f"acceptance {current.value} -> {target.value} illegal",
        )


def _chk_physical(current: PhysicalState, target: PhysicalState) -> None:
    allowed = PHYSICAL_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ControlError(
            "ILLEGAL_TRANSITION",
            f"physical {current.value} -> {target.value} illegal",
        )


def _chk_points_admission(proj: Projection, p: dict) -> None:
    spec = proj.spec
    assert spec is not None
    weight = p.get("point_weight", 0)
    # spec 降容：当前占用可能已超 cap，一律停止新准入（占用+新 > cap 或占用已超）
    used = proj.points_used()
    if used + weight > spec.max_active_node_points:
        raise AdmissionError(
            "POINT_CAPACITY",
            "root node points exhausted or spec lowered below occupancy",
            used=used,
            add=weight,
            cap=spec.max_active_node_points,
        )
    team_id = p.get("team_id")
    if team_id and team_id in proj.teams:
        # 计入自身 + 全部祖先
        tid: Optional[str] = team_id
        while tid:
            team = proj.teams[tid]
            tused = proj.points_used(tid)
            if tused + weight > team.local_point_cap:
                raise AdmissionError(
                    "TEAM_LOCAL_POINTS",
                    "subteam local point cap exceeded",
                    team=tid,
                    used=tused,
                    cap=team.local_point_cap,
                )
            tid = team.parent_team_id


def _chk_level(proj: Projection, p: dict) -> None:
    if p.get("human_override_level"):
        return
    lead_id = p.get("lead_node_id")
    if not lead_id or lead_id not in proj.nodes:
        return
    lead = proj.nodes[lead_id]
    if not lead.resolved_route:
        return
    # 级别从 payload 带入
    worker_level: Optional[Level] = p.get("model_level")
    lead_level: Optional[Level] = p.get("lead_level")
    if worker_level is None or lead_level is None:
        return
    if LEVEL_RANK[worker_level] > LEVEL_RANK[lead_level]:
        raise AdmissionError("LEVEL_DIRECTION", "can only summon same or lower level")


def _chk_deps_unlocked(proj: Projection, p: dict) -> None:
    wi_id = p.get("work_item_id")
    if not wi_id or wi_id not in proj.work_items:
        return
    wi = proj.work_items[wi_id]
    if not wi.unlocked:
        raise AdmissionError("UNMET_DEPENDENCY", "predecessors not accepted")


def validate_candidate(proj: Projection, ev: Event) -> Projection:
    check_event(proj, ev)
    nxt = proj.copy()
    apply_event(nxt, ev)
    check_projection(nxt)
    return nxt
