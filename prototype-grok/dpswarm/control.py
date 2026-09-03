"""单写者控制面：方法级事务、硬准入、资源池、脚本化 Lead 决策入口。"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .invariants import check_projection, validate_candidate
from .store import AdmissionError, ControlError, Event, EventLog, Projection
from .types import (
    LEVEL_RANK,
    AcceptanceState,
    ArchivePhase,
    Attribution,
    BlockedState,
    ChildSpec,
    ChoiceSource,
    ContextPackage,
    HumanInstructionKind,
    Level,
    NodeRole,
    PackageEntry,
    PhysicalState,
    RootExecutionSpec,
    Route,
    SeedMode,
    StartKind,
    StopReason,
    TerminalReason,
    default_catalog,
    spec_replace,
)


class Tx:
    def __init__(self, seq: int, proj: Projection):
        self.seq = seq
        self.proj = proj
        self.events: list[Event] = []

    def emit(self, typ: str, **payload: Any) -> Event:
        self.seq += 1
        ev = Event(self.seq, typ, self.proj.clock_ms, payload)
        self.proj = validate_candidate(self.proj, ev)
        self.events.append(ev)
        return ev


class ControlPlane:
    """per-root 单写者。watchdog 只投建议，不直接改图。"""

    def __init__(
        self,
        spec: Optional[RootExecutionSpec] = None,
        catalog: Optional[dict] = None,
        clock_ms: int = 0,
    ) -> None:
        self.spec_template = spec or RootExecutionSpec()
        self.catalog = catalog or default_catalog()
        self.log = EventLog()
        self.proj = Projection()
        self.proj.clock_ms = clock_ms
        self._seq = 0
        self._n = 0
        self._writer = True
        self._flush_then_notify = True
        self.last_notifications_before_flush: list = []

    # ----- 基础设施 -----

    def snapshot(self) -> Projection:
        return self.proj

    def replay(self) -> Projection:
        return self.log.replay()

    def replay_matches_live(self) -> bool:
        rp = self.replay()
        return (
            rp.graph_revision == self.proj.graph_revision
            and set(rp.nodes) == set(self.proj.nodes)
            and set(rp.work_items) == set(self.proj.work_items)
            and rp.points_used() == self.proj.points_used()
            and rp.open_worker_slots() == self.proj.open_worker_slots()
            and rp.spec_revision == self.proj.spec_revision
        )

    def _new_id(self, prefix: str) -> str:
        self._n += 1
        ident = f"{prefix}-{self._n}"
        return ident

    def transact(self, build: Callable[[Tx], None]) -> list[Event]:
        if not self._writer:
            raise ControlError("NOT_WRITER", "only the single writer process may transact")
        tx = Tx(self._seq, self.proj.copy())
        try:
            build(tx)
        except Exception:
            # 方法级回滚：seq 与投影均不前进
            raise
        notified_holder: list[str] = []

        def before(evs: list[Event]) -> None:
            self.last_notifications_before_flush = list(tx.proj.notifications)

        def after(evs: list[Event]) -> None:
            notified_holder.append("flushed")

        self.log.before_flush = before
        self.log.after_flush = after
        self.log.append_and_flush(tx.events)
        if not notified_holder and tx.events:
            raise ControlError("FLUSH", "success reported before flush")
        self.proj = tx.proj
        self._seq = tx.seq
        check_projection(self.proj)
        return tx.events

    def resolve_model(self, route: Route):
        info = self.catalog.get(route.key())
        if info is None:
            raise AdmissionError("MODEL_NOT_FOUND", "model does not exist", route=str(route.key()))
        if not info.available:
            raise AdmissionError("MODEL_UNAVAILABLE", "model not available", route=str(route.key()))
        return info

    def _lead_identity(self, lead_node_id: str) -> dict:
        lead = self.proj.nodes[lead_node_id]
        info = self.resolve_model(lead.resolved_route or lead.route)
        return {"route": lead.resolved_route, "level": info.level.value, "node_id": lead_node_id}

    def _bump(self, tx: Tx) -> None:
        tx.emit("GraphRevisionBumped", graph_revision=tx.proj.graph_revision + 1, expected_graph_revision=tx.proj.graph_revision)

    def _assemble_and_prefetch(self, tx: Tx, node_id: str, package: ContextPackage) -> None:
        tx.emit("PackageAssembled", node_id=node_id, package=package)
        required = [e for e in package.entries if e.required]
        if required:
            tx.emit("RequiredPrefetched", node_id=node_id, package_id=package.package_id)

    def _default_package(self, node_id: str, seed: SeedMode = SeedMode.FRESH) -> ContextPackage:
        return ContextPackage(
            package_id=self._new_id("pkg"),
            revision="r1",
            content_hash=f"hash-{node_id}-{self._n}",
            seed_mode=seed,
            entries=[
                PackageEntry("required-body", True, f"hreq-{node_id}", "intent", inline=True),
                PackageEntry("optional-extra", False, f"hopt-{node_id}", "extra", inline=False),
            ],
        )

    # ----- 查询 / 注入事实 -----

    def injection_facts(self, task_type: str = "coding") -> dict:
        """§3 V1：AA 分维 + 目录价目 + 当前容量 + bench。不含生产画像。"""
        catalog = []
        for info in self.catalog.values():
            if not info.available:
                continue
            aa = info.aa_coding if task_type == "coding" else info.aa_reasoning
            catalog.append(
                {
                    "provider": info.provider,
                    "model": info.model,
                    "level": info.level.value,
                    "price": info.price,
                    "aa_dimension": aa,
                    "aa_coding": info.aa_coding,
                    "aa_reasoning": info.aa_reasoning,
                    "bench_decay_threshold": info.bench_decay_token_threshold,
                    "point_weight": info.point_weight,
                }
            )
        return {
            "aa_and_catalog": catalog,
            "capacity": self.proj.injection_capacity() if self.proj.spec else None,
            "bench": [c for c in catalog if c["bench_decay_threshold"] is not None],
            "portraits_injected": False,
            "policy_version": self.proj.spec.model_point_policy_version if self.proj.spec else None,
        }

    def should_invoke_context_manager(self, *, heterogeneous: bool, window_ratio: float, retrieval_conflict: bool) -> bool:
        """§5.1 是否唤起 manager 由代码判断，不交给 LLM。"""
        spec = self.proj.spec or self.spec_template
        return heterogeneous or window_ratio >= spec.soft_window_threshold or retrieval_conflict

    def prefix_cache_allowed(self, a: Route, b: Route) -> bool:
        """§5.1 异构只杀跨模型缓存；同模型仍可共享前缀。"""
        return a.provider == b.provider and a.model == b.model

    def durable_retrieval(self) -> list[str]:
        return [
            m.memory_id
            for m in self.proj.memories.values()
            if m.status.value == "durable"
        ]

    # ----- Root / Spec -----

    def create_root(self, lead_route: Route, *, choice_source: ChoiceSource = ChoiceSource.LEAD, proposed: Optional[Route] = None) -> str:
        info = self.resolve_model(lead_route)
        actual = lead_route if choice_source == ChoiceSource.HUMAN or proposed is None else lead_route
        if choice_source == ChoiceSource.HUMAN:
            actual = lead_route

        def build(tx: Tx) -> None:
            spec = self.spec_template
            root_id = "root-1"
            spec_id = "spec-1"
            team_id = "team-root"
            lead_id = "node-lead"
            lease_id = "lease-lead-pts"
            tx.emit(
                "RootCreated",
                root_id=root_id,
                spec_id=spec_id,
                spec_revision=1,
                spec=spec,
            )
            tx.emit(
                "TeamCreated",
                team_id=team_id,
                parent_team_id=None,
                lead_node_id=lead_id,
                local_point_cap=spec.max_active_node_points,
            )
            tx.emit(
                "LeaseAcquired",
                lease_id=lease_id,
                kind="points",
                amount=info.point_weight,
                owner_node_id=lead_id,
                policy_version=spec.model_point_policy_version,
            )
            tx.emit(
                "NodeProvisioning",
                node_id=lead_id,
                role="lead",
                is_root=True,
                work_item_id=None,
                team_id=team_id,
                lead_node_id=lead_id,
                layer=1,
                physical_depth=1,
                start_kind="new",
                route=actual,
                proposed_route=proposed or lead_route,
                resolved_route=actual,
                choice_source=choice_source.value,
                policy_version=spec.model_point_policy_version,
                point_weight=info.point_weight,
                lease_id=lease_id,
                context_epoch=0,
                fence_token=1,
                required_prefetched=True,
                package_ready=True,
                allow_empty_package=True,
                needs_package=False,
                model_level=info.level,
                lead_level=info.level,
            )
            tx.emit("NodeActivated", node_id=lead_id, session_id="sess-lead-0", fence_token=1, package_hash=None)
            self._bump(tx)

        self.transact(build)
        return "node-lead"

    def publish_spec_revision(self, **fields: Any) -> int:
        def build(tx: Tx) -> None:
            new_spec = spec_replace(tx.proj.spec, **fields)
            tx.emit("SpecPublished", spec_revision=tx.proj.spec_revision + 1, spec=new_spec)
            tx.emit(
                "HumanInstructionRecorded",
                kind=HumanInstructionKind.CONFIG.value,
                payload={"fields": fields},
            )

        self.transact(build)
        return self.proj.spec_revision

    # ----- 拓扑：派生 / 分裂 / 裂变 -----

    def derive(
        self,
        parent_node_id: str,
        route: Route,
        *,
        choice_source: ChoiceSource = ChoiceSource.LEAD,
        proposed: Optional[Route] = None,
        human_override_level: bool = False,
        deps: tuple[str, ...] = (),
        auto_activate: bool = True,
        work_item_id: Optional[str] = None,
        package: Optional[ContextPackage] = None,
        seed: SeedMode = SeedMode.FRESH,
        skip_prefetch: bool = False,
    ) -> dict:
        if choice_source == ChoiceSource.HUMAN:
            resolved = route
        else:
            resolved = route
        info = self.resolve_model(resolved)
        parent = self.proj.nodes[parent_node_id]
        lead_info = self.resolve_model(parent.resolved_route or parent.route)
        if (not human_override_level) and LEVEL_RANK[info.level] > LEVEL_RANK[lead_info.level]:
            raise AdmissionError("LEVEL_DIRECTION", "can only summon same or lower level")

        out: dict = {}

        def build(tx: Tx) -> None:
            wi_id = work_item_id or self._new_id("wi")
            node_id = self._new_id("node")
            slot_lease = self._new_id("lease-slot")
            pts_lease = self._new_id("lease-pts")
            layer = parent.layer + 1
            pkg = package or self._default_package(node_id, seed)
            if seed == SeedMode.FORK:
                pkg.seed_mode = SeedMode.FORK
                pkg.fork_seed_length = 10
                pkg.fork_parent_lineage = parent.session_id
            tx.emit(
                "LeaseAcquired",
                lease_id=slot_lease,
                kind="slot",
                amount=1,
                owner_work_item_id=wi_id,
                owner_node_id=node_id,
            )
            tx.emit(
                "WorkItemCreated",
                work_item_id=wi_id,
                parent_work_item_id=None,
                team_id=parent.team_id,
                layer=layer,
                kind="worker",
                primary_node_id=node_id,
                predecessors=list(deps),
                unlocked=all(tx.proj.work_items[d].status == AcceptanceState.ACCEPTED for d in deps) if deps else True,
                slot_lease_id=slot_lease,
                counts_as_team_worker=True,
            )
            for d in deps:
                tx.emit("DAGEdgeAdded", src=d, dst=wi_id)
            tx.emit(
                "LeaseAcquired",
                lease_id=pts_lease,
                kind="points",
                amount=info.point_weight,
                owner_node_id=node_id,
                owner_work_item_id=wi_id,
                policy_version=tx.proj.spec.model_point_policy_version,
            )
            tx.emit(
                "NodeProvisioning",
                node_id=node_id,
                role="worker",
                work_item_id=wi_id,
                team_id=parent.team_id,
                lead_node_id=parent_node_id,
                layer=layer,
                physical_depth=parent.physical_depth + 1,
                start_kind="new",
                route=resolved,
                proposed_route=proposed or route,
                resolved_route=resolved,
                choice_source=choice_source.value,
                human_override_level=human_override_level,
                policy_version=tx.proj.spec.model_point_policy_version,
                point_weight=info.point_weight,
                lease_id=pts_lease,
                context_epoch=0,
                fence_token=1,
                package_hash=pkg.content_hash,
                required_prefetched=not skip_prefetch,
                package_ready=not skip_prefetch,
                allow_empty_package=skip_prefetch,
                needs_package=not skip_prefetch,
                model_level=info.level,
                lead_level=lead_info.level,
                attempt=1,
            )
            if not skip_prefetch:
                self._assemble_and_prefetch(tx, node_id, pkg)
            if auto_activate:
                tx.emit(
                    "NodeActivated",
                    node_id=node_id,
                    session_id=self._new_id("sess"),
                    fence_token=1,
                    package_hash=pkg.content_hash,
                )
            self._bump(tx)
            out["work_item_id"] = wi_id
            out["node_id"] = node_id
            out["package_id"] = pkg.package_id
            out["lease_id"] = pts_lease
            out["slot_lease_id"] = slot_lease

        self.transact(build)
        return out

    def split(
        self,
        primary_node_id: str,
        assistant_route: Route,
        *,
        choice_source: ChoiceSource = ChoiceSource.LEAD,
        human_override_level: bool = False,
        auto_activate: bool = True,
    ) -> dict:
        primary = self.proj.nodes[primary_node_id]
        if primary.role == NodeRole.ASSISTANT:
            raise AdmissionError("SPLIT_SCALE", "assistant cannot split further")
        wi = self.proj.work_items[primary.work_item_id]
        if wi.assistant_node_id:
            raise AdmissionError("SPLIT_SCALE", "split is 1 primary + 1 assistant")
        info = self.resolve_model(assistant_route)
        prim_info = self.resolve_model(primary.resolved_route or primary.route)
        if (not human_override_level) and LEVEL_RANK[info.level] > LEVEL_RANK[prim_info.level]:
            raise AdmissionError("LEVEL_DIRECTION", "assistant must be same or lower than primary")
        out: dict = {}

        def build(tx: Tx) -> None:
            ast_id = self._new_id("node")
            pts = self._new_id("lease-pts")
            ch = self._new_id("chan")
            pkg = self._default_package(ast_id)
            tx.emit(
                "LeaseAcquired",
                lease_id=pts,
                kind="points",
                amount=info.point_weight,
                owner_node_id=ast_id,
                owner_work_item_id=primary.work_item_id,
                policy_version=tx.proj.spec.model_point_policy_version,
            )
            tx.emit(
                "NodeProvisioning",
                node_id=ast_id,
                role="assistant",
                work_item_id=primary.work_item_id,
                team_id=primary.team_id,
                lead_node_id=primary.lead_node_id,
                layer=primary.layer,
                physical_depth=primary.physical_depth + 1,
                start_kind="new",
                route=assistant_route,
                proposed_route=assistant_route,
                resolved_route=assistant_route,
                choice_source=choice_source.value,
                human_override_level=human_override_level,
                policy_version=tx.proj.spec.model_point_policy_version,
                point_weight=info.point_weight,
                lease_id=pts,
                context_epoch=0,
                fence_token=1,
                package_hash=pkg.content_hash,
                required_prefetched=True,
                package_ready=True,
                assistant_of=primary_node_id,
                model_level=info.level,
                lead_level=prim_info.level,
            )
            self._assemble_and_prefetch(tx, ast_id, pkg)
            if auto_activate:
                tx.emit(
                    "NodeActivated",
                    node_id=ast_id,
                    session_id=self._new_id("sess"),
                    fence_token=1,
                    package_hash=pkg.content_hash,
                )
            tx.emit(
                "PeerChannelOpened",
                channel_id=ch,
                work_item_id=primary.work_item_id,
                primary_node_id=primary_node_id,
                assistant_node_id=ast_id,
            )
            self._bump(tx)
            out.update(assistant_node_id=ast_id, channel_id=ch, lease_id=pts)

        self.transact(build)
        return out

    def fission(
        self,
        lead_node_id: str,
        children: list[ChildSpec],
        *,
        human_override_fission: bool = False,
        local_ratio: Optional[float] = None,
        auto_activate: bool = True,
    ) -> dict:
        lead = self.proj.nodes[lead_node_id]
        lead_info = self.resolve_model(lead.resolved_route or lead.route)
        if (not human_override_fission) and lead_info.level != Level.S:
            raise AdmissionError("FISSION_PERMISSION", "only S-level may fission")
        spec = self.proj.spec
        ratio = local_ratio if local_ratio is not None else spec.subteam_points_ratio
        out: dict = {"children": []}

        def build(tx: Tx) -> None:
            nested = lead.role == NodeRole.WORKER
            team_id = lead.team_id
            if nested:
                parent_team = tx.proj.teams[lead.team_id]
                cap = min(int(parent_team.local_point_cap * ratio), parent_team.local_point_cap - 1)
                if cap < 1:
                    raise AdmissionError("TEAM_LOCAL_POINTS", "cannot form nested team cap")
                new_team = self._new_id("team")
                tx.emit(
                    "TeamCreated",
                    team_id=new_team,
                    parent_team_id=lead.team_id,
                    lead_node_id=lead_node_id,
                    local_point_cap=cap,
                )
                tx.emit("RoleChanged", node_id=lead_node_id, role="lead", team_id=new_team)
                team_id = new_team
            created = []
            for i, child in enumerate(children):
                resolved = child.route
                info = self.resolve_model(resolved)
                if (not child.human_override_level) and LEVEL_RANK[info.level] > LEVEL_RANK[lead_info.level]:
                    raise AdmissionError("LEVEL_DIRECTION", "can only summon same or lower")
                wi_id = self._new_id("wi")
                node_id = self._new_id("node")
                slot_lease = self._new_id("lease-slot")
                pts_lease = self._new_id("lease-pts")
                layer = lead.layer + 1
                pred_wis = tuple(created[j]["work_item_id"] for j in child.depends_on)
                pkg = self._default_package(node_id)
                start_now = len(pred_wis) == 0
                tx.emit(
                    "LeaseAcquired",
                    lease_id=slot_lease,
                    kind="slot",
                    amount=1,
                    owner_work_item_id=wi_id,
                    owner_node_id=node_id,
                )
                tx.emit(
                    "WorkItemCreated",
                    work_item_id=wi_id,
                    parent_work_item_id=lead.work_item_id,
                    team_id=team_id,
                    layer=layer,
                    kind="worker",
                    primary_node_id=node_id,
                    predecessors=list(pred_wis),
                    unlocked=start_now,
                    slot_lease_id=slot_lease,
                    pending_route=resolved,
                    pending_weight=info.point_weight,
                    pending_choice_source=child.choice_source.value,
                    pending_human_override=child.human_override_level,
                )
                for pw in pred_wis:
                    tx.emit("DAGEdgeAdded", src=pw, dst=wi_id)
                rec = {"work_item_id": wi_id, "node_id": node_id, "lease_id": None, "index": i, "started": False}
                if start_now:
                    tx.emit(
                        "LeaseAcquired",
                        lease_id=pts_lease,
                        kind="points",
                        amount=info.point_weight,
                        owner_node_id=node_id,
                        owner_work_item_id=wi_id,
                        policy_version=tx.proj.spec.model_point_policy_version,
                    )
                    tx.emit(
                        "NodeProvisioning",
                        node_id=node_id,
                        role="worker",
                        work_item_id=wi_id,
                        team_id=team_id,
                        lead_node_id=lead_node_id,
                        layer=layer,
                        physical_depth=lead.physical_depth + 1,
                        start_kind="new",
                        route=resolved,
                        proposed_route=child.proposed_route or child.route,
                        resolved_route=resolved,
                        choice_source=child.choice_source.value,
                        human_override_level=child.human_override_level,
                        policy_version=tx.proj.spec.model_point_policy_version,
                        point_weight=info.point_weight,
                        lease_id=pts_lease,
                        context_epoch=0,
                        fence_token=1,
                        package_hash=pkg.content_hash,
                        required_prefetched=True,
                        package_ready=True,
                        model_level=info.level,
                        lead_level=lead_info.level,
                        needs_package=True,
                    )
                    self._assemble_and_prefetch(tx, node_id, pkg)
                    rec["lease_id"] = pts_lease
                    if auto_activate:
                        tx.emit(
                            "NodeActivated",
                            node_id=node_id,
                            session_id=self._new_id("sess"),
                            fence_token=1,
                            package_hash=pkg.content_hash,
                        )
                        rec["started"] = True
                created.append(rec)
            self._bump(tx)
            out["children"] = created
            out["team_id"] = team_id

        self.transact(build)
        return out

    def start_dependent(self, work_item_id: str) -> dict:
        wi = self.proj.work_items[work_item_id]
        if not wi.unlocked:
            raise AdmissionError("UNMET_DEPENDENCY", "predecessors not accepted")
        if wi.primary_node_id in self.proj.nodes:
            node = self.proj.nodes[wi.primary_node_id]
            if node.physical == PhysicalState.PROVISIONING:
                self.activate(node.node_id)
            return {"node_id": node.node_id, "work_item_id": work_item_id}
        out: dict = {}

        def build(tx: Tx) -> None:
            item = tx.proj.work_items[work_item_id]
            # 找到 team lead
            team = tx.proj.teams[item.team_id]
            lead_node = tx.proj.nodes[team.lead_node_id]
            lead_info = self.resolve_model(lead_node.resolved_route or lead_node.route)
            route = item.pending_route
            info = self.resolve_model(route)
            node_id = item.primary_node_id
            pts_lease = self._new_id("lease-pts")
            pkg = self._default_package(node_id)
            tx.emit(
                "LeaseAcquired",
                lease_id=pts_lease,
                kind="points",
                amount=info.point_weight,
                owner_node_id=node_id,
                owner_work_item_id=work_item_id,
                policy_version=tx.proj.spec.model_point_policy_version,
            )
            tx.emit(
                "NodeProvisioning",
                node_id=node_id,
                role="worker",
                work_item_id=work_item_id,
                team_id=item.team_id,
                lead_node_id=team.lead_node_id,
                layer=item.layer,
                physical_depth=lead_node.physical_depth + 1,
                start_kind="new",
                route=route,
                proposed_route=route,
                resolved_route=route,
                choice_source=item.pending_choice_source,
                human_override_level=item.pending_human_override,
                policy_version=tx.proj.spec.model_point_policy_version,
                point_weight=info.point_weight,
                lease_id=pts_lease,
                context_epoch=0,
                fence_token=1,
                package_hash=pkg.content_hash,
                required_prefetched=True,
                package_ready=True,
                model_level=info.level,
                lead_level=lead_info.level,
            )
            self._assemble_and_prefetch(tx, node_id, pkg)
            tx.emit(
                "NodeActivated",
                node_id=node_id,
                session_id=self._new_id("sess"),
                fence_token=1,
                package_hash=pkg.content_hash,
            )
            out["node_id"] = node_id
            out["lease_id"] = pts_lease

        self.transact(build)
        return out

    # ----- 启动协议 / 崩溃对账 -----

    def activate(
        self,
        node_id: str,
        *,
        session_id: Optional[str] = None,
        accepted_hash: Optional[str] = None,
        parent_match: bool = True,
        descriptor_match: bool = True,
        provider_match: bool = True,
        fence_token: Optional[int] = None,
        extra_child: bool = False,
    ) -> str:
        node = self.proj.nodes[node_id]
        if extra_child and node.physical == PhysicalState.ACTIVE:
            def drain_extra(tx: Tx) -> None:
                tx.emit("OpsAuditRecorded", kind="conflict_active", node_id=node_id)
                tx.emit("NodeDrained", node_id=f"extra-{node_id}")

            try:
                self.transact(drain_extra)
            except ControlError:
                pass
            raise ControlError("CONFLICT_ACTIVE", "typed conflict; drain extra child, no silent coexistence")
        matches = parent_match and descriptor_match and provider_match
        h = accepted_hash if accepted_hash is not None else node.package_hash
        if (not matches) or (node.package_hash and h != node.package_hash):

            def fail(tx: Tx) -> None:
                tx.emit("NodeFailed", node_id=node_id, reason="mismatch")

            self.transact(fail)
            return "failed"

        def build(tx: Tx) -> None:
            tx.emit(
                "NodeActivated",
                node_id=node_id,
                session_id=session_id or self._new_id("sess"),
                fence_token=fence_token if fence_token is not None else node.fence_token,
                package_hash=h,
            )
            if node.start_kind == StartKind.ROLLOVER and node.successor_registered:
                tx.emit("SuccessorCleared", node_id=node_id)

        self.transact(build)
        return "active"

    def fail_provisioning(self, node_id: str, reason: str = "mismatch") -> None:
        def build(tx: Tx) -> None:
            tx.emit("NodeFailed", node_id=node_id, reason=reason)

        self.transact(build)

    def recover_provisioning(self, node_id: str, truth: dict) -> str:
        node = self.proj.nodes[node_id]
        if node.physical == PhysicalState.ACTIVE and not truth.get("matches", True):
            raise ControlError("CONFLICT_ACTIVE", "recover failed while already active; drain extra")
        if node.physical != PhysicalState.PROVISIONING:
            return node.physical.value
        ok = all(
            [
                truth.get("parent_match", True),
                truth.get("descriptor_match", True),
                truth.get("provider_match", True),
                truth.get("hash", node.package_hash) == node.package_hash,
            ]
        )
        if node.start_kind == StartKind.ROLLOVER:
            ok = ok and truth.get("checkpoint_match", True) and truth.get("predecessor_match", True)
        if ok:
            return self.activate(node_id, session_id=truth.get("session_id"))
        self.fail_provisioning(node_id)
        return "failed"

    def recover_orphan_successor(self, node_id: str) -> None:
        """CAS 已登记但 provisioning 未落盘：补走启动协议，幂等。"""
        node = self.proj.nodes[node_id]
        if not node.successor_registered:
            raise ControlError("NO_CAS", "no successor registration")

        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            tx.emit(
                "NodeProvisioning",
                node_id=node_id,
                role=n.role.value,
                work_item_id=n.work_item_id,
                team_id=n.team_id,
                lead_node_id=n.lead_node_id,
                layer=n.layer,
                physical_depth=n.physical_depth,
                start_kind="rollover",
                route=n.resolved_route,
                resolved_route=n.resolved_route,
                proposed_route=n.proposed_route,
                choice_source=n.choice_source.value,
                policy_version=n.policy_version,
                point_weight=n.point_weight,
                lease_id=n.lease_id,
                context_epoch=n.context_epoch + 1,
                fence_token=n.fence_token + 1,
                package_hash=n.capsule_hash or n.package_hash,
                predecessor_session_id=n.session_id,
                checkpoint_id=n.checkpoint_id or "ckpt",
                required_prefetched=True,
                package_ready=True,
                allow_empty_package=True,
                needs_package=False,
            )

        self.transact(build)

    def retry_start(self, node_id: str, auto_activate: bool = True) -> None:
        """启动失败不耗重试预算。"""
        node = self.proj.nodes[node_id]

        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            tx.emit(
                "NodeProvisioning",
                node_id=node_id,
                role=n.role.value,
                work_item_id=n.work_item_id,
                team_id=n.team_id,
                lead_node_id=n.lead_node_id,
                layer=n.layer,
                physical_depth=n.physical_depth,
                start_kind="new",
                route=n.resolved_route,
                resolved_route=n.resolved_route,
                proposed_route=n.proposed_route,
                choice_source=n.choice_source.value,
                policy_version=n.policy_version,
                point_weight=n.point_weight,
                lease_id=n.lease_id,
                context_epoch=n.context_epoch,
                fence_token=n.fence_token + 1,
                package_hash=n.package_hash,
                required_prefetched=True,
                package_ready=True,
                allow_empty_package=True,
                needs_package=False,
                attempt=n.attempt,
            )
            if auto_activate:
                tx.emit(
                    "NodeActivated",
                    node_id=node_id,
                    session_id=self._new_id("sess"),
                    fence_token=n.fence_token + 1,
                    package_hash=n.package_hash,
                )

        self.transact(build)

    # ----- 验收五步 / 打回 / 重试 / 上交 -----

    def record_llm_call(self, node_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("LlmCallRecorded", node_id=node_id)

        self.transact(build)

    def submit(self, node_id: str, stop_reason: StopReason = StopReason.COMPLETED) -> None:
        node = self.proj.nodes[node_id]
        if node.role == NodeRole.ASSISTANT:
            raise ControlError("ASSISTANT_NO_SUBMIT", "assistant cannot independently submit")

        def build(tx: Tx) -> None:
            tx.emit("NodeSubmitted", node_id=node_id, stop_reason=stop_reason.value)

        self.transact(build)

    def lead_pass(self, node_id: str, lead_node_id: Optional[str] = None) -> None:
        node = self.proj.nodes[node_id]
        lid = lead_node_id or node.lead_node_id

        def build(tx: Tx) -> None:
            tx.emit("NodeFinalizing", node_id=node_id, accepted_by=self._lead_identity(lid))

        self.transact(build)

    def commit_evidence(self, node_id: str, evidence_hash: str = "evh", package_rev: str = "dep-1") -> None:
        def build(tx: Tx) -> None:
            tx.emit(
                "EvidenceCommitted",
                node_id=node_id,
                evidence_id=self._new_id("evidence"),
                evidence_hash=evidence_hash,
                kind="accepted_raw",
            )
            tx.emit("PackageCommitted", node_id=node_id, package_revision=package_rev)

        self.transact(build)

    def publish_accepted(self, node_id: str, *, assistant_signal: Optional[str] = None) -> None:
        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            tx.emit("NodeAccepted", node_id=node_id)
            tx.emit(
                "ObservationRecorded",
                who=n.lead_node_id,
                whom=node_id,
                route=str(n.resolved_route.key()) if n.resolved_route else None,
                work_item_id=n.work_item_id,
                split=bool(n.work_item_id and tx.proj.work_items.get(n.work_item_id) and tx.proj.work_items[n.work_item_id].assistant_node_id),
                acceptedBy=n.accepted_by,
                stopReason=n.stop_reason.value if n.stop_reason else None,
                terminal="accepted",
                choice_source=n.choice_source.value,
                policy_version=n.policy_version,
            )
            if n.portrait_eligible:
                tx.emit("PortraitUpdated", key=_portrait_key(n), outcome="success")
            wi = tx.proj.work_items.get(n.work_item_id or "")
            if wi and wi.assistant_node_id and assistant_signal:
                tx.emit(
                    "AssistantRewardRecorded",
                    assistant_node_id=wi.assistant_node_id,
                    primary_node_id=node_id,
                    signal=assistant_signal,
                )
                ast = tx.proj.nodes[wi.assistant_node_id]
                tx.emit(
                    "PortraitUpdated",
                    key=_portrait_key(ast),
                    outcome="success" if assistant_signal == "assistant-accepted" else "fail",
                )

        self.transact(build)

    def closeout_accept(self, node_id: str, lead_node_id: Optional[str] = None, assistant_signal: Optional[str] = None) -> None:
        """脚本化 Lead 通过：finalizing 与 accepted 仍是链上两事件（中间证据可另调）。"""
        self.lead_pass(node_id, lead_node_id)
        self.commit_evidence(node_id)
        self.publish_accepted(node_id, assistant_signal=assistant_signal)

    def lead_reject(self, node_id: str, attribution: Attribution, reason: str = "", lead_node_id: Optional[str] = None) -> None:
        node = self.proj.nodes[node_id]
        lid = lead_node_id or node.lead_node_id

        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            tx.emit(
                "NodeRejected",
                node_id=node_id,
                attribution=attribution.value,
                reason=reason,
                rejected_by=self._lead_identity(lid),
                negative_sample=True,
            )
            tx.emit("PortraitUpdated", key=_portrait_key(n), outcome="fail")

        self.transact(build)

    def retry(
        self,
        node_id: str,
        *,
        new_route: Optional[Route] = None,
        attribution: Optional[Attribution] = None,
    ) -> str:
        node = self.proj.nodes[node_id]
        spec = self.proj.spec
        if node.retries_used >= spec.max_retries:
            raise AdmissionError("RETRY_EXHAUSTED", "escalate instead of grinding")
        route = new_route or node.resolved_route
        info = self.resolve_model(route)
        if new_route and attribution == Attribution.CAPABILITY:
            lead = self.proj.nodes[node.lead_node_id]
            lead_info = self.resolve_model(lead.resolved_route or lead.route)
            if LEVEL_RANK[info.level] > LEVEL_RANK[lead_info.level]:
                raise AdmissionError("UPGRADE_EXCEEDS_LEAD", "upgrade cap = Lead level; need S via escalate")
        # 同模型：lease 不变；换模型：原子 reweight
        if new_route and info.point_weight != node.point_weight:
            used = self.proj.points_used()
            delta = info.point_weight - node.point_weight
            if delta > 0 and used + delta > spec.max_active_node_points:

                def wait(tx: Tx) -> None:
                    tx.emit("NodeBlocked", node_id=node_id, blocked="reweight_wait", timeout_suppressed=True)

                self.transact(wait)
                return "reweight_wait"

        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            if new_route and info.point_weight != n.point_weight:
                tx.emit(
                    "LeaseReweighted",
                    lease_id=n.lease_id,
                    node_id=node_id,
                    new_amount=info.point_weight,
                    policy_version=tx.proj.spec.model_point_policy_version,
                )
            tx.emit(
                "AttemptRecorded",
                node_id=node_id,
                attempt=n.attempt + 1,
                retries_used=n.retries_used + 1,
                route=route,
            )
            if n.blocked != BlockedState.NONE:
                tx.emit("NodeBlocked", node_id=node_id, blocked="none", timeout_suppressed=False)

        self.transact(build)
        return "retried"

    def resume_reweight(self, node_id: str, new_route: Route) -> str:
        return self.retry(node_id, new_route=new_route, attribution=Attribution.CAPABILITY)

    def escalate(self, work_item_id: str, *, parent_action: str = "self_do", new_children: Optional[list[ChildSpec]] = None) -> dict:
        """同一事务：drain → 终态 escalated 释放资源 → 父级接管准入。"""
        out: dict = {}
        wi = self.proj.work_items[work_item_id]

        def build(tx: Tx) -> None:
            item = tx.proj.work_items[work_item_id]
            # 先回收节点
            ast_id = item.assistant_node_id
            if ast_id and tx.proj.nodes[ast_id].physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE):
                tx.emit("NodeDrained", node_id=ast_id)
                tx.emit("NodeTerminated", node_id=ast_id, terminal_reason="terminated")
            primary = item.primary_node_id
            if item.peer_channel_id:
                ch = tx.proj.channels[item.peer_channel_id]
                if not ch.closed:
                    tx.emit("PeerChannelClosed", channel_id=ch.channel_id, discard_queued=True)
            pn = tx.proj.nodes[primary]
            if pn.successor_registered:
                tx.emit("SuccessorCleared", node_id=primary)
            if pn.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE):
                tx.emit("NodeDrained", node_id=primary)
            tx.emit("WorkItemEscalated", work_item_id=work_item_id)
            tx.emit(
                "ObservationRecorded",
                whom=primary,
                terminal="escalated",
                no_verdict=True,
                portrait=False,
            )
            tx.emit("PortraitUpdated", key=_portrait_key(pn), skip=True, reason="escalated")
            created = []
            if parent_action == "new_work_items" and new_children:
                parent_lead = tx.proj.nodes[item.primary_node_id].lead_node_id
                # 释放后同一事务内再准入
                for child in new_children:
                    rec = _emit_worker(self, tx, parent_lead, child.route, choice_source=child.choice_source)
                    created.append(rec)
            out["created"] = created

        self.transact(build)
        return out

    def abort_finalize(self, node_id: str) -> None:
        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            if n.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE):
                tx.emit("NodeDrained", node_id=node_id)
            tx.emit("NodeAbortedFinalize", node_id=node_id)

        self.transact(build)

    def terminate_node(self, node_id: str, reason: TerminalReason = TerminalReason.TERMINATED, *, human: bool = False) -> None:
        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            if human:
                tx.emit("HumanInstructionRecorded", kind=HumanInstructionKind.TERMINAL.value, payload={"node_id": node_id})
            wi = tx.proj.work_items.get(n.work_item_id or "")
            if wi and wi.assistant_node_id and node_id == wi.primary_node_id:
                ast = tx.proj.nodes[wi.assistant_node_id]
                if ast.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE):
                    tx.emit("NodeDrained", node_id=ast.node_id)
                    tx.emit("NodeTerminated", node_id=ast.node_id, terminal_reason="terminated")
                if wi.peer_channel_id and not tx.proj.channels[wi.peer_channel_id].closed:
                    tx.emit("PeerChannelClosed", channel_id=wi.peer_channel_id, discard_queued=True)
            if n.successor_registered:
                tx.emit("SuccessorCleared", node_id=node_id)
            if n.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE, PhysicalState.FAILED):
                if n.physical != PhysicalState.DRAINED:
                    tx.emit("NodeDrained", node_id=node_id)
            tx.emit("NodeTerminated", node_id=node_id, terminal_reason=reason.value)
            tx.emit("PortraitUpdated", key=_portrait_key(n), skip=True, reason="terminated")

        self.transact(build)

    def degenerate_child(self, work_item_id: str) -> None:
        """§7 退化：子节点 terminated，不算验收、不进画像；父继续。"""
        wi = self.proj.work_items[work_item_id]
        self.terminate_node(wi.primary_node_id, TerminalReason.TERMINATED)

    def close_assistant(self, assistant_node_id: str, *, evidence_first: bool = True) -> None:
        def build(tx: Tx) -> None:
            n = tx.proj.nodes[assistant_node_id]
            if evidence_first:
                tx.emit(
                    "EvidenceCommitted",
                    node_id=assistant_node_id,
                    evidence_id=self._new_id("evidence"),
                    evidence_hash="ast-transcript",
                    kind="assistant_transcript",
                )
            wi = tx.proj.work_items[n.work_item_id]
            if wi.peer_channel_id and not tx.proj.channels[wi.peer_channel_id].closed:
                tx.emit("PeerChannelClosed", channel_id=wi.peer_channel_id, discard_queued=True)
            if n.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE):
                tx.emit("NodeDrained", node_id=assistant_node_id)
            tx.emit("NodeTerminated", node_id=assistant_node_id, terminal_reason="terminated")

        self.transact(build)

    # ----- 超时 / deadline / 封存 -----

    def advance_clock(self, ms: int) -> None:
        def build(tx: Tx) -> None:
            tx.emit("ClockAdvanced", clock_ms=tx.proj.clock_ms + ms)

        self.transact(build)

    def on_node_timeout(self, node_id: str) -> str:
        node = self.proj.nodes[node_id]
        if node.physical == PhysicalState.PROVISIONING:
            def cancel(tx: Tx) -> None:
                tx.emit("NodeFailed", node_id=node_id, reason="provisioning_cancelled")

            self.transact(cancel)
            return "cancelled_provisioning"
        if node.blocked == BlockedState.REWEIGHT_WAIT or node.timeout_suppressed:
            raise ControlError("TIMEOUT_SUPPRESSED", "timeout only applies to active nodes outside suppression windows")
        if node.physical != PhysicalState.ACTIVE:
            raise ControlError("TIMEOUT_SUPPRESSED", "timeout clock starts at active")

        def build(tx: Tx) -> None:
            tx.emit("NodeBlocked", node_id=node_id, blocked="blocked", timeout_suppressed=False)
            if tx.proj.nodes[node_id].role == NodeRole.ASSISTANT:
                tx.emit(
                    "WakeupEmitted",
                    kind="changed",
                    target_id=tx.proj.nodes[node_id].assistant_of,
                )
            else:
                tx.emit("WakeupEmitted", kind="timeout", target_id=tx.proj.nodes[node_id].lead_node_id)

        self.transact(build)
        return "blocked"

    def fire_deadline(self) -> None:
        self.begin_archive("deadline")

    def begin_archive(self, reason: str = "deadline") -> None:
        def build(tx: Tx) -> None:
            if reason == "manual":
                tx.emit("HumanInstructionRecorded", kind=HumanInstructionKind.TERMINAL.value, payload={"archive": True})
            tx.emit("AdmissionCutoff", reason=reason)
            term = "deadline-stopped" if reason == "deadline" else "manual-stopped"
            tx.emit("ObservationRecorded", kind="archive_cutoff", terminal=term)

        self.transact(build)

    def settle_archive(self, *, timed_out: bool = False, abort_finalizing: bool = False) -> None:
        def build(tx: Tx) -> None:
            tx.emit("ArchivePhaseChanged", phase=ArchivePhase.SETTLING.value)
            # 先回收节点，再改 item 终态
            for wi in list(tx.proj.work_items.values()):
                if wi.status in (AcceptanceState.ACCEPTED, AcceptanceState.TERMINATED, AcceptanceState.ESCALATED, AcceptanceState.ABORTED_FINALIZE):
                    continue
                if wi.assistant_node_id:
                    ast = tx.proj.nodes[wi.assistant_node_id]
                    if ast.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE, PhysicalState.FAILED):
                        tx.emit("NodeDrained", node_id=ast.node_id)
                    if ast.acceptance not in (AcceptanceState.TERMINATED, AcceptanceState.ACCEPTED, AcceptanceState.ABORTED_FINALIZE, AcceptanceState.ESCALATED):
                        tx.emit("NodeTerminated", node_id=ast.node_id, terminal_reason="deadline-stopped")
                if wi.peer_channel_id and wi.peer_channel_id in tx.proj.channels and not tx.proj.channels[wi.peer_channel_id].closed:
                    tx.emit("PeerChannelClosed", channel_id=wi.peer_channel_id, discard_queued=True)
                pn = tx.proj.nodes[wi.primary_node_id]
                if pn.successor_registered:
                    tx.emit("SuccessorCleared", node_id=pn.node_id)
                if pn.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE, PhysicalState.FAILED):
                    tx.emit("NodeDrained", node_id=pn.node_id)
                if pn.acceptance == AcceptanceState.FINALIZING:
                    if abort_finalizing:
                        tx.emit("NodeAbortedFinalize", node_id=pn.node_id)
                    else:
                        # 允许完成原子发布则调用方应先 publish；此处默认 aborted
                        tx.emit("NodeAbortedFinalize", node_id=pn.node_id)
                elif pn.acceptance not in (AcceptanceState.ACCEPTED, AcceptanceState.TERMINATED, AcceptanceState.ESCALATED, AcceptanceState.ABORTED_FINALIZE):
                    tx.emit("NodeTerminated", node_id=pn.node_id, terminal_reason="deadline-stopped")
            # root lead
            lead = tx.proj.nodes.get(tx.proj.root_lead_id)
            if lead and lead.acceptance not in (AcceptanceState.ACCEPTED, AcceptanceState.TERMINATED, AcceptanceState.ABORTED_FINALIZE):
                if lead.physical in (PhysicalState.PROVISIONING, PhysicalState.ACTIVE):
                    tx.emit("NodeDrained", node_id=lead.node_id)
                if lead.acceptance == AcceptanceState.FINALIZING:
                    tx.emit("NodeAbortedFinalize", node_id=lead.node_id)
                else:
                    tx.emit("NodeTerminated", node_id=lead.node_id, terminal_reason="deadline-stopped")
            phase = ArchivePhase.TIMED_OUT.value if timed_out else ArchivePhase.COMPLETED.value
            tx.emit("ArchivePhaseChanged", phase=phase)

        self.transact(build)

    # ----- 硬切 -----

    def set_window_usage(self, node_id: str, ratio: float) -> None:
        def build(tx: Tx) -> None:
            tx.emit("WindowUsageSet", node_id=node_id, ratio=ratio)

        self.transact(build)

    def preload_capsule(self, node_id: str, *, success: bool, package_hash: str = "capsule-h") -> None:
        def build(tx: Tx) -> None:
            tx.emit("CapsulePreloaded", node_id=node_id, success=success, package_hash=package_hash)

        self.transact(build)

    def register_successor(self, node_id: str) -> None:
        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            tx.emit("SuccessorRegistered", node_id=node_id, context_epoch=n.context_epoch)

        self.transact(build)

    def start_rollover(self, node_id: str, *, new_route: Optional[Route] = None, auto_activate: bool = True) -> str:
        node = self.proj.nodes[node_id]
        route = new_route or node.resolved_route
        info = self.resolve_model(route)
        if new_route and info.point_weight != node.point_weight:
            used = self.proj.points_used()
            delta = info.point_weight - node.point_weight
            if delta > 0 and used + delta > self.proj.spec.max_active_node_points:

                def wait(tx: Tx) -> None:
                    tx.emit("NodeBlocked", node_id=node_id, blocked="reweight_wait", timeout_suppressed=True)

                self.transact(wait)
                return "reweight_wait"
        attempt_before = node.attempt
        out = {"attempt": attempt_before}

        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            if new_route and info.point_weight != n.point_weight:
                tx.emit(
                    "LeaseReweighted",
                    lease_id=n.lease_id,
                    node_id=node_id,
                    new_amount=info.point_weight,
                    policy_version=tx.proj.spec.model_point_policy_version,
                )
            if not n.capsule_ready:
                raise ControlError("NO_CAPSULE", "preload required before rollover start")
            if not n.successor_registered:
                tx.emit("SuccessorRegistered", node_id=node_id, context_epoch=n.context_epoch)
            old_lease = n.lease_id
            old_wi = n.work_item_id
            old_depth = n.physical_depth
            tx.emit(
                "NodeProvisioning",
                node_id=node_id,
                role=n.role.value,
                work_item_id=old_wi,
                team_id=n.team_id,
                lead_node_id=n.lead_node_id,
                layer=n.layer,
                physical_depth=old_depth,
                start_kind="rollover",
                route=route,
                resolved_route=route,
                proposed_route=n.proposed_route,
                choice_source=n.choice_source.value,
                policy_version=tx.proj.spec.model_point_policy_version,
                point_weight=info.point_weight if new_route else n.point_weight,
                lease_id=old_lease,
                context_epoch=n.context_epoch + 1,
                fence_token=n.fence_token + 1,
                package_hash=n.capsule_hash,
                predecessor_session_id=n.session_id,
                checkpoint_id="ckpt-1",
                required_prefetched=True,
                package_ready=True,
                allow_empty_package=True,
                needs_package=False,
            )
            if auto_activate:
                tx.emit(
                    "NodeActivated",
                    node_id=node_id,
                    session_id=self._new_id("sess"),
                    fence_token=n.fence_token + 1,
                    package_hash=n.capsule_hash,
                )
                tx.emit("SuccessorCleared", node_id=node_id)
            out["lease_id"] = old_lease
            out["attempt"] = n.attempt

        self.transact(build)
        return "rolled"

    def clear_successor_on_preload_fail(self, node_id: str) -> None:
        def build(tx: Tx) -> None:
            n = tx.proj.nodes[node_id]
            if n.successor_registered:
                tx.emit("SuccessorCleared", node_id=node_id)

        self.transact(build)

    # ----- peer / wakeup / context -----

    def peer_send(self, work_item_id: str, sender_node_id: str, msg_id: str, body: str = "") -> None:
        wi = self.proj.work_items[work_item_id]
        if not wi.peer_channel_id:
            raise ControlError("NOT_SPLIT_CHANNEL", "derive/fission remain star topology")

        def build(tx: Tx) -> None:
            tx.emit("PeerMessageQueued", channel_id=wi.peer_channel_id, msg_id=msg_id, sender=sender_node_id, body=body)

        self.transact(build)

    def peer_deliver(self, work_item_id: str, msg_id: str) -> None:
        wi = self.proj.work_items[work_item_id]

        def build(tx: Tx) -> None:
            tx.emit("PeerMessageDelivered", channel_id=wi.peer_channel_id, msg_id=msg_id)

        self.transact(build)

    def wakeup(self, target_id: str, kind: str = "changed") -> NotificationView:
        def build(tx: Tx) -> None:
            tx.emit("WakeupEmitted", kind=kind, target_id=target_id)

        evs = self.transact(build)
        last = self.proj.notifications[-1]
        return NotificationView(last.kind, last.target_id)

    def pull_optional(self, node_id: str, entry_id: str) -> str:
        node = self.proj.nodes[node_id]
        if node.physical != PhysicalState.ACTIVE:
            raise ControlError("NOT_ACTIVE", "only live worker may pull")
        return f"pulled:{entry_id}"

    def pull_from_predecessor(self, node_id: str) -> None:
        raise ControlError("PREDECESSOR_DELTA_FORBIDDEN", "no delta channel from predecessor logs")

    def invoke_context_manager(self, trigger_node_id: str) -> str:
        job_id = self._new_id("cmjob")

        def build(tx: Tx) -> None:
            tx.emit("ContextManagerAcquired", job_id=job_id, trigger_node_id=trigger_node_id)

        self.transact(build)
        return job_id

    def release_context_manager(self, job_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("ContextManagerReleased", job_id=job_id)

        self.transact(build)

    def assemble_without_prefetch(self, node_id: str) -> ContextPackage:
        pkg = self._default_package(node_id)

        def build(tx: Tx) -> None:
            tx.emit("PackageAssembled", node_id=node_id, package=pkg)

        self.transact(build)
        return pkg

    # ----- 记忆 / token / 限流 -----

    def extract_candidate(self, node_id: str, kind: str = "accepted", *, confirmed: bool = False) -> str:
        mid = self._new_id("mem")

        def build(tx: Tx) -> None:
            tx.emit("MemoryCandidateAdded", memory_id=mid, source_node_id=node_id, kind=kind, confirmed=confirmed)

        self.transact(build)
        return mid

    def promote_memory(self, memory_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("MemoryPromoted", memory_id=memory_id)

        self.transact(build)

    def extract_and_promote_fail_does_not_rollback(self, node_id: str) -> None:
        """抽取失败不回滚验收：只记审计。"""
        def build(tx: Tx) -> None:
            tx.emit("OpsAuditRecorded", kind="memory_extract_failed", node_id=node_id)

        self.transact(build)

    def supersede_memory(self, old_id: str, new_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("MemorySuperseded", old_id=old_id, new_id=new_id)

        self.transact(build)

    def invalidate_memory(self, memory_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("MemoryInvalidated", memory_id=memory_id)

        self.transact(build)

    def cleanup_window(self, node_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("WindowCleaned", node_id=node_id)

        self.transact(build)

    def record_tokens(self, node_id: str, *, input: int, output: int, cache_read: int, cache_write: int, cache_in_input: bool = False) -> None:
        def build(tx: Tx) -> None:
            tx.emit(
                "TokensRecorded",
                node_id=node_id,
                input=input,
                output=output,
                cache_read=cache_read,
                cache_write=cache_write,
                cache_in_input=cache_in_input,
            )

        self.transact(build)

    def record_rate_limit(self, node_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("ObservationRecorded", kind="RATE_LIMIT", node_id=node_id, backoff=True)
            tx.emit("PortraitUpdated", key=_portrait_key(self.proj.nodes[node_id]), skip=True, reason="RATE_LIMIT")

        self.transact(build)

    def record_quota(self, node_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("OpsAuditRecorded", kind="QUOTA", node_id=node_id)
            tx.emit("PortraitUpdated", key=_portrait_key(self.proj.nodes[node_id]), skip=True, reason="QUOTA")

        self.transact(build)
        self.terminate_node(node_id, TerminalReason.MANUAL_STOPPED)

    def human_immediate(self, payload: dict) -> None:
        def build(tx: Tx) -> None:
            tx.emit("HumanInstructionRecorded", kind=HumanInstructionKind.IMMEDIATE.value, payload=payload)

        self.transact(build)

    def watchdog_suggest(self, kind: str, **payload: Any) -> str:
        sid = self._new_id("sug")

        def build(tx: Tx) -> None:
            tx.emit("SuggestionQueued", suggestion_id=sid, kind=kind, payload=payload)

        self.transact(build)
        return sid

    def drain_writer_queue(self) -> None:
        pending = [s for s in self.proj.suggestions if not s.consumed]
        for s in pending:
            def consume(tx: Tx, sug=s) -> None:
                tx.emit("SuggestionConsumed", suggestion_id=sug.suggestion_id)

            self.transact(consume)
            if s.kind == "timeout":
                self.on_node_timeout(s.payload["node_id"])
            elif s.kind == "hard_cut":
                nid = s.payload["node_id"]
                if not self.proj.nodes[nid].capsule_ready:
                    self.preload_capsule(nid, success=True)
                if not self.proj.nodes[nid].successor_registered:
                    self.register_successor(nid)
                self.start_rollover(nid)
            elif s.kind == "deadline":
                self.fire_deadline()

    def accept_root_by_human(self) -> None:
        """§10 最终发布权留白；原型用 human 终态指令作为显式钩。"""
        lead_id = self.proj.root_lead_id

        def build(tx: Tx) -> None:
            tx.emit("HumanInstructionRecorded", kind=HumanInstructionKind.TERMINAL.value, payload={"accept_root": True})
            n = tx.proj.nodes[lead_id]
            if n.acceptance == AcceptanceState.NONE:
                tx.emit("NodeSubmitted", node_id=lead_id, stop_reason=StopReason.COMPLETED.value)
            if tx.proj.nodes[lead_id].acceptance == AcceptanceState.SUBMITTED:
                tx.emit("NodeFinalizing", node_id=lead_id, accepted_by={"source": "human"})
            if not tx.proj.nodes[lead_id].evidence_hash:
                tx.emit(
                    "EvidenceCommitted",
                    node_id=lead_id,
                    evidence_id=self._new_id("evidence"),
                    evidence_hash="root-ev",
                    kind="accepted_raw",
                )
                tx.emit("PackageCommitted", node_id=lead_id, package_revision="root-dep")
            tx.emit("NodeAccepted", node_id=lead_id)

        self.transact(build)

    def add_edge(self, src: str, dst: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("DAGEdgeAdded", src=src, dst=dst)
            self._bump(tx)

        self.transact(build)

    def spawn_reviewer(self, lead_node_id: str, route: Route, auto_activate: bool = True) -> dict:
        """独立 reviewer：占点不占普通 worker 槽（§7）。角色语义 §10 留白。"""
        info = self.resolve_model(route)
        lead = self.proj.nodes[lead_node_id]
        out: dict = {}

        def build(tx: Tx) -> None:
            nid = self._new_id("node")
            pts = self._new_id("lease-pts")
            pkg = self._default_package(nid)
            tx.emit(
                "LeaseAcquired",
                lease_id=pts,
                kind="points",
                amount=info.point_weight,
                owner_node_id=nid,
                policy_version=tx.proj.spec.model_point_policy_version,
            )
            tx.emit(
                "NodeProvisioning",
                node_id=nid,
                role="reviewer",
                work_item_id=None,
                team_id=lead.team_id,
                lead_node_id=lead_node_id,
                layer=lead.layer,
                physical_depth=lead.physical_depth + 1,
                start_kind="new",
                route=route,
                proposed_route=route,
                resolved_route=route,
                choice_source=ChoiceSource.LEAD.value,
                policy_version=tx.proj.spec.model_point_policy_version,
                point_weight=info.point_weight,
                lease_id=pts,
                context_epoch=0,
                fence_token=1,
                package_hash=pkg.content_hash,
                required_prefetched=True,
                package_ready=True,
                allow_empty_package=True,
                needs_package=False,
                model_level=info.level,
                lead_level=self.resolve_model(lead.resolved_route or lead.route).level,
            )
            self._assemble_and_prefetch(tx, nid, pkg)
            if auto_activate:
                tx.emit("NodeActivated", node_id=nid, session_id=self._new_id("sess"), fence_token=1, package_hash=pkg.content_hash)
            out["node_id"] = nid

        self.transact(build)
        return out

    def prefetch_required(self, node_id: str) -> None:
        def build(tx: Tx) -> None:
            tx.emit("RequiredPrefetched", node_id=node_id, package_id="")

        self.transact(build)

    def cas_stale(self, expected: int) -> None:
        def build(tx: Tx) -> None:
            tx.emit("GraphRevisionBumped", graph_revision=tx.proj.graph_revision + 1, expected_graph_revision=expected)

        self.transact(build)


class NotificationView:
    def __init__(self, kind: str, target_id: str):
        self.kind = kind
        self.target_id = target_id
        self.state = None  # 故意不带状态


def _portrait_key(node) -> str:
    r = node.resolved_route or node.route
    if r is None:
        return node.node_id
    return f"{r.provider}/{r.model}"


def _emit_worker(plane: ControlPlane, tx: Tx, parent_node_id: str, route: Route, choice_source: ChoiceSource = ChoiceSource.LEAD) -> dict:
    parent = tx.proj.nodes[parent_node_id]
    info = plane.resolve_model(route)
    lead_info = plane.resolve_model(parent.resolved_route or parent.route)
    wi_id = plane._new_id("wi")
    node_id = plane._new_id("node")
    slot_lease = plane._new_id("lease-slot")
    pts_lease = plane._new_id("lease-pts")
    pkg = plane._default_package(node_id)
    layer = parent.layer + 1
    tx.emit("LeaseAcquired", lease_id=slot_lease, kind="slot", amount=1, owner_work_item_id=wi_id, owner_node_id=node_id)
    tx.emit(
        "WorkItemCreated",
        work_item_id=wi_id,
        parent_work_item_id=None,
        team_id=parent.team_id,
        layer=layer,
        kind="worker",
        primary_node_id=node_id,
        unlocked=True,
        slot_lease_id=slot_lease,
    )
    tx.emit(
        "LeaseAcquired",
        lease_id=pts_lease,
        kind="points",
        amount=info.point_weight,
        owner_node_id=node_id,
        owner_work_item_id=wi_id,
        policy_version=tx.proj.spec.model_point_policy_version,
    )
    tx.emit(
        "NodeProvisioning",
        node_id=node_id,
        role="worker",
        work_item_id=wi_id,
        team_id=parent.team_id,
        lead_node_id=parent_node_id,
        layer=layer,
        physical_depth=parent.physical_depth + 1,
        start_kind="new",
        route=route,
        proposed_route=route,
        resolved_route=route,
        choice_source=choice_source.value,
        policy_version=tx.proj.spec.model_point_policy_version,
        point_weight=info.point_weight,
        lease_id=pts_lease,
        context_epoch=0,
        fence_token=1,
        package_hash=pkg.content_hash,
        required_prefetched=True,
        package_ready=True,
        model_level=info.level,
        lead_level=lead_info.level,
    )
    plane._assemble_and_prefetch(tx, node_id, pkg)
    tx.emit("NodeActivated", node_id=node_id, session_id=plane._new_id("sess"), fence_token=1, package_hash=pkg.content_hash)
    return {"work_item_id": wi_id, "node_id": node_id}
