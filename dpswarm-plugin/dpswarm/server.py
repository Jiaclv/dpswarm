"""DPSwarm 控制面板服务器（标准库 http.server，零依赖）。

架构对位（机制文档 §9.1）：控制面事件存储独立、harness 只作执行底座——
本服务直接暴露 ControlPlane 的串行事务链，页面经 JSON API 控制参数：

  GET  /                      控制页面（static/index.html）
  GET  /api/status            投影快照 + Spec + 目录（§9.1 状态由回放派生）
  GET  /api/events?limit=N    事件流（唯一真源，审计视角）
  GET  /api/observation       观测汇总（§4 全账 / §6 经济性）
  POST /api/spec              发布 Spec 新 revision（§2.1 config 型人工指令：
                               降容不强杀在途、只停新准入）
  POST /api/directive         人工指令（§9.2 三类：immediate / config / terminal）
  POST /api/task              提交演示任务（MockProvider 脚本或 OpenAI 兼容路由）
  POST /api/seal              手动封存三段式（§9.6）
  POST /api/tick              手动时间护栏巡检（§7）
  POST /api/delegate          拓扑委派（§7/§8：kind+subtasks 新建 item+节点两阶段，
                               或 item_id+subtask 重跑既有 item——归因重试执行臂/超时重试）
  POST /api/submit            worker（dsh subagent）交付回控制面（§5.7 submitted）
  POST /api/review            Lead 验收：accept / reject / terminate（§4/§8；
                               reject 只打回挂起，预算由重跑消费）
  POST /api/peer              分裂对 peer 通道投递（§9.5：queued+delivered，
                               消息账本即 evidence）

用法：python -m dpswarm.server [--port 8790] [--workspace .dpswarm-panel]
页面：http://127.0.0.1:8790/  （dph Web UI 入口见 apps/web/public/dpswarm.html）
"""
from __future__ import annotations

import argparse
import dataclasses
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import observation
from .aa import AASnapshot, register_route_model
from .control import ControlPlane, ControlPlaneError
from .events import DelegationRecord
from .orchestrator import Orchestrator
from .providers import MockProvider, OpenAICompatProvider
from .types import (
    HumanDirective,
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
)

STATIC = Path(__file__).parent / "static"

# Spec 可控参数白名单（§2.1 稳定约束；发布新 revision 时仅接受这些字段，
# 其余继承当前值——Spec 不原地覆写）。
SPEC_FIELDS = {
    "max_open_work_items": int,
    "max_active_node_points": int,
    "subteam_point_ratio": float,
    "max_depth": int,
    "max_team_workers": int,
    "max_attempts": int,
    "deadline_seconds": (float, type(None)),
    "node_wallclock_timeout": float,
    "root_acceptance_mode": str,
}


def default_catalog() -> ModelCatalog:
    """演示目录（aa_source='demo'，注入文本带演示标记，与真实 AA 快照区分）。"""
    cat = ModelCatalog()
    cat.register(ModelFacts("mock", "s-grok", Level.S, aa_source="demo",
                            aa_dimensional={"coding": 9.0, "reasoning": 9.2, "overall": 9.1}))
    cat.register(ModelFacts("mock", "a-glm", Level.A, aa_source="demo",
                            aa_dimensional={"coding": 8.6, "reasoning": 8.4, "overall": 8.5}))
    cat.register(ModelFacts("mock", "b-kimi", Level.B, aa_source="demo",
                            aa_dimensional={"coding": 7.8, "reasoning": 7.6, "overall": 7.7}))
    cat.register(ModelFacts("mock", "c-fast", Level.C, aa_source="demo",
                            aa_dimensional={"coding": 6.5, "reasoning": 6.3, "overall": 6.4}))
    return cat


class PanelState:
    """单写者（ControlPlane 自带锁）；server 线程只读投影、写经控制面方法。"""

    def __init__(self, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self.aa = AASnapshot.load_default()
        self.token = self._load_or_create_token(workspace)
        # root Lead 级别（P1-5）：DPSWARM_ROOT_MODEL 命中 AA 快照 → 按外部分档
        # 解析（fission 权限跟实际模型走）；未配置默认 S（历史口径）。
        root_level = None
        root_model = os.environ.get("DPSWARM_ROOT_MODEL", "")
        if root_model and self.aa is not None:
            entry = self.aa.lookup(root_model)
            if entry is not None:
                root_level = self.aa.level_for(entry)
        self.cp = ControlPlane(store_path=workspace / "events.jsonl",
                               catalog=default_catalog(), root_level=root_level)

    @staticmethod
    def _load_or_create_token(workspace: Path) -> str:
        """写操作 bearer capability：进程启动时读/建 workspace/.dpswarm-token。

        autoStart（dsh 插件拉起）与手动启动共用同一 workspace 约定
        （README：cd dpswarm-plugin && python -m dpswarm.server），因此 Host
        半按约定路径读同一文件即可对上。写盘失败退化为进程级随机 token
        （重启换新——宁可断写不裸奔）。
        """
        f = workspace / ".dpswarm-token"
        try:
            if f.exists():
                t = f.read_text(encoding="utf-8").strip()
                if t:
                    return t
            t = secrets.token_urlsafe(32)
            f.write_text(t, encoding="utf-8")
            return t
        except OSError:
            return secrets.token_urlsafe(32)

    # -- API 实现 -----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        p = self.cp.proj
        return {
            "snapshot": self.cp.snapshot(),
            "spec": dataclasses.asdict(p.spec),
            "spec_revisions": sorted(p.spec_revisions.keys()),
            "aa_snapshot": self.aa.meta() if self.aa else None,
            "catalog": [
                {"provider": f.provider, "model": f.model, "level": f.level.value,
                 "aa": f.aa_dimensional, "src": f.aa_source, "ctx": f.context_window,
                 "in": f.input_price_per_mtok, "out": f.output_price_per_mtok}
                for f in p and self.cp.catalog.facts.values()
            ],
        }

    def events(self, limit: int = 50) -> Dict[str, Any]:
        evs = self.cp.store.read_all()
        return {"total": len(evs), "events": [
            {"seq": e.seq, "kind": e.kind, "ts": e.ts, "payload": e.payload}
            for e in evs[-limit:]]}

    def observation_report(self) -> Dict[str, Any]:
        return observation.summarize_events(self.cp.store.read_all())

    def publish_spec(self, body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """§2.1：人工调整边界发布新 revision。只接受白名单字段，其余继承；
        非法值返回结构化原因（不静默纠正）。"""
        current = dataclasses.asdict(self.cp.proj.spec)
        merged = dict(current)
        errors: Dict[str, str] = {}
        for k, v in (body or {}).items():
            if k not in SPEC_FIELDS:
                errors[k] = "unknown field"
                continue
            want = SPEC_FIELDS[k]
            ok = isinstance(v, want) if isinstance(want, tuple) else (
                isinstance(v, want) and not isinstance(v, bool))
            if not ok:
                errors[k] = f"type mismatch, want {want}"
                continue
            merged[k] = v
        if errors:
            return False, {"ok": False, "errors": errors}
        try:
            ev = self.cp.human_directive(HumanDirective(
                kind="config", payload={"spec": merged}))
            return True, {"ok": True, "revision": self.cp.proj.spec.revision,
                         "event_seq": ev.seq}
        except ControlPlaneError as e:
            return False, {"ok": False, "error": e.code, "message": str(e)}

    def directive(self, body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """§9.2 三类人工指令。config 型转 publish_spec；immediate 支持
        wakeup（通知不带状态 §9.4）；terminal 记录终态指令入链。"""
        kind = (body or {}).get("kind")
        if kind not in ("immediate", "config", "terminal"):
            return False, {"ok": False, "error": "kind must be immediate|config|terminal"}
        if kind == "config":
            return self.publish_spec((body or {}).get("spec") or {})
        try:
            payload = (body or {}).get("payload") or {}
            if kind == "immediate" and payload.get("op") == "wakeup":
                ev = self.cp.wakeup(payload["node_id"], payload.get("about", "manual"))
            else:
                ev = self.cp.human_directive(HumanDirective(kind=kind, payload=payload))
            return True, {"ok": True, "event_seq": ev.seq}
        except (ControlPlaneError, KeyError) as e:
            return False, {"ok": False, "error": getattr(e, "code", "BAD_REQUEST"),
                           "message": str(e)}

    def run_task(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """演示任务：mock_script（确定性）或 model=PROVIDER/MODEL（OpenAI 兼容）。"""
        task = (body or {}).get("task") or "示例任务"
        mock_script = (body or {}).get("mock_script")
        model = (body or {}).get("model")
        lead: Optional[ModelRoute] = None
        if mock_script:
            provider = MockProvider(script=list(mock_script))
        elif model:
            prov, name = model.split("/", 1)
            facts = register_route_model(self.cp.catalog, self.aa, prov, name,
                                         aa_hint={"coding": 8.0})
            provider = OpenAICompatProvider()
            lead = ModelRoute(prov, name, level=facts.level,
                              point_weight=max(1, facts.context_window // 64_000))
        else:
            # 默认演示脚本：single 直通（不依赖外部模型即可看到完整链）
            provider = MockProvider(script=[
                {"text": json.dumps({"action": "single"})},
                {"text": "DPswarm 默认单干，按需生长拓扑（演示交付）。"},
            ])
        orch = Orchestrator(self.cp, provider, store_dir=self.workspace / "packages",
                            lead_route=lead)
        out = orch.run_task(task)
        return {"ok": True, "result": out}

    def seal(self, body: Dict[str, Any]) -> Dict[str, Any]:
        team = (body or {}).get("team", "root")
        try:
            self.cp.begin_seal(team)
            self.cp.begin_settlement(team)
            # timed_out 由控制面事实推导（§9.6），请求体字段忽略（防伪造超时收尾）
            self.cp.finish_seal(team, timed_out=self._seal_timed_out(team))
            return {"ok": True, "phase": self.cp.proj.seal_phase.get(team).value
                    if self.cp.proj.seal_phase.get(team) else "open"}
        except ControlPlaneError as e:
            return {"ok": False, "error": e.code, "message": str(e)}

    def _seal_timed_out(self, team: str) -> bool:
        """timed_out 推导（§9.6）：树级 deadline 已过即超时收尾——与 tick 的
        deadline-seal 触发条件同口径（root_started 起算）。"""
        spec = self.cp.proj.spec
        return bool(spec.deadline_seconds is not None
                    and self.cp._root_started_at > 0
                    and time.time() - self.cp._root_started_at > spec.deadline_seconds)

    def tick(self) -> Dict[str, Any]:
        return {"ok": True, "actions": self.cp.tick()}

    # -- dsh 插件对接面（程序化委派：执行底座在 dsh subagent，控制面在此）------

    def _route_from_subtask(self, st: Dict[str, Any], *,
                            team: str = "root") -> Tuple[str, ModelRoute]:
        """Agent 路由入口：只接受模型选择，来源和权限级别由服务端决定。

        未知模型须命中可信 AA 快照；声明级别只能由服务端目录配置提供。
        在创建 item / 消费重试预算前完成字段、可用性和级别方向检查。
        人工操作仍走独立 directive 入口，不能用请求内 source 冒充。
        """
        from dpswarm.types import ModelRoute, RouteSource
        if not isinstance(st, dict):
            raise ControlPlaneError("BAD_SUBTASK", "subtask must be an object")
        allowed = {"provider", "model", "reasoning_effort", "title", "prompt", "deps", "source"}
        errors = {str(k): "unknown or server-owned field" for k in st if k not in allowed}
        for name in ("provider", "model"):
            if not isinstance(st.get(name), str) or not st[name].strip():
                errors[name] = "required non-empty string"
        for name in ("reasoning_effort", "title", "prompt"):
            if name in st and not isinstance(st[name], str):
                errors[name] = "must be a string"
        if "source" in st and st["source"] != "lead":
            errors["source"] = "agent requests always have source=lead"
        if "deps" in st and not isinstance(st["deps"], list):
            errors["deps"] = "must be an array of subtask indices"
        if errors:
            raise ControlPlaneError("BAD_SUBTASK", "invalid agent subtask", errors=errors)
        provider, model = st["provider"], st["model"]
        facts = self.cp.catalog.resolve(provider, model)
        if facts is None:
            entry = self.aa.lookup(model) if self.aa is not None else None
            if entry is None:
                raise ControlPlaneError(
                    "MODEL_UNAVAILABLE",
                    f"{provider}/{model} has no trusted catalog or AA entry; "
                    "configure model facts outside the agent request")
            # Build without registering: a rejected route must not mutate the catalog.
            facts = ModelFacts(provider, model, self.aa.level_for(entry),
                               aa_dimensional=self.aa.dimensions(entry),
                               aa_source=self.aa.source_tag())
        if not facts.available:
            raise ControlPlaneError("MODEL_UNAVAILABLE", f"{provider}/{model} is unavailable")
        lead_level = self.cp._team_lead_level(team)
        if lead_level is not None and facts.level.rank > lead_level.rank:
            raise ControlPlaneError(
                "LEVEL_DIRECTION",
                f"can only summon same or lower level: worker {facts.level.value} "
                f"> lead {lead_level.value}; agent requests cannot use human override")
        route = ModelRoute(
            provider=provider, model=model,
            reasoning_effort=st.get("reasoning_effort", "default"),
            level=facts.level,
            source=RouteSource.ROUTE_LEAD,
            point_weight=max(1, facts.context_window // 64_000))
        return "lead", route

    def _register_agent_routes(self, routes: list[ModelRoute]) -> None:
        """完整请求路由预检通过后，才将可信 AA 事实加入服务端目录。"""
        for route in routes:
            if self.cp.catalog.resolve(route.provider, route.model) is None:
                register_route_model(self.cp.catalog, self.aa, route.provider, route.model)

    def _fence_of(self, node_id: str) -> Dict[str, Any]:
        """P1-2 fence：节点当前 (context_epoch, session_id) 随 delegate 回传，
        dsh 端 submit 原样回带——旧 session（rollover 后）提交即拒。"""
        n = self.cp.proj.nodes.get(node_id)
        if n is None:
            return {}
        return {"context_epoch": n.context_epoch, "session_id": n.session_id}

    def delegate(self, body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """拓扑动作 + 硬准入（§7）：两模式——
        新建：kind + subtasks（可带 deps 0 基下标，§7 DAG；未就绪项不启动、
        列入 pending）；重跑：item_id + subtask（§8 归因重试执行臂 / §7 超时
        重试）。物理执行由调用方（dsh subagent）承担——机制文档 §9.1 执行底座。"""
        from dpswarm.types import DelegationKind
        if not isinstance(body, dict):
            return False, {"ok": False, "error": "BAD_REQUEST", "message": "body must be an object"}
        if body.get("item_id"):
            return self._delegate_rerun(body)
        kind_map = {"derive": DelegationKind.DERIVE, "fission": DelegationKind.FISSION,
                    "split": DelegationKind.SPLIT}
        kind = kind_map.get(body.get("kind"))
        subtasks = body.get("subtasks") or []
        if not isinstance(subtasks, list):
            return False, {"ok": False, "error": "BAD_SUBTASK", "message": "subtasks must be an array"}
        if kind is None or not subtasks:
            return False, {"ok": False, "error": "kind ∈ derive|fission|split 且 subtasks 非空"
                                                "（重跑既有 item 用 item_id + subtask）"}
        if kind == DelegationKind.SPLIT and len(subtasks) != 1:
            return False, {"ok": False, "error": "split 的 subtasks 恰 1 条（1 主 1 副由内部生成）"}
        max_workers = self.cp.proj.spec.max_team_workers
        if len(subtasks) > max_workers:
            # §2/§7 裂变规模：结构化拒绝，不静默截断
            return False, {"ok": False, "error": "SUBTASKS_OVER_LIMIT", "max": max_workers,
                           "message": f"subtasks {len(subtasks)} 条 > max_team_workers "
                                      f"{max_workers}（§7 裂变规模硬上限）"}
        try:
            routes = [self._route_from_subtask(st)[1] for st in subtasks]
        except ControlPlaneError as e:
            return False, {"ok": False, "error": e.code, "message": str(e), **e.context}
        # §7 DAG：deps 0 基下标预检（非法下标结构化拒绝，不静默丢弃边）
        deps_by_index = []
        for i, st in enumerate(subtasks):
            raw = st.get("deps", [])
            for d in raw:
                if (not isinstance(d, int) or isinstance(d, bool)
                        or not 0 <= d < len(subtasks) or d == i):
                    return False, {"ok": False, "error": "BAD_DEPS",
                                   "message": f"subtasks[{i}].deps 下标非法: {d!r}"
                                              f"（须为 0 基、指向其他 subtask）"}
            if len(set(raw)) != len(raw):
                return False, {"ok": False, "error": "BAD_DEPS",
                               "message": f"subtasks[{i}].deps contains duplicate indices"}
            deps_by_index.append([d for d in raw])
        # Validate the complete DAG before create_work_item/add_dependency can
        # consume slots or publish a partial graph.
        remaining = [len(deps) for deps in deps_by_index]
        followers: list[list[int]] = [[] for _ in subtasks]
        for i, deps in enumerate(deps_by_index):
            for d in deps:
                followers[d].append(i)
        ready = [i for i, count in enumerate(remaining) if count == 0]
        visited = 0
        while ready:
            visited += 1
            for follower in followers[ready.pop()]:
                remaining[follower] -= 1
                if remaining[follower] == 0:
                    ready.append(follower)
        if visited != len(subtasks):
            return False, {"ok": False, "error": "CYCLE",
                           "message": "subtask dependencies must form an acyclic graph"}
        results: list = []
        pending: list = []
        try:
            self._register_agent_routes(routes)
            team = "root"
            created: list = []  # (subtask 下标, WorkItem)
            for i, st in enumerate(subtasks):
                route = routes[i]
                if kind == DelegationKind.SPLIT:
                    # §7 分裂 = 同层拓宽：不新建 work item（不加层、不占新槽），
                    # 主执行者直接建在被分裂的 root item 上（acceptance=None 时
                    # begin_node 合法）。响应 kind 与事件口径一致——此前报
                    # "split" 却建 DERIVE item，两侧口径不一致。
                    item_id = self.cp._root_item_id()
                    primary = self.cp.begin_node(item_id, route)
                    self.cp.confirm_node(primary.node_id)
                    assistant, chan = self.cp.split(primary.node_id, route)
                    self.cp.confirm_node(assistant.node_id)
                    results.append({"item_id": item_id, "node_id": primary.node_id,
                                    "assistant_node_id": assistant.node_id,
                                    "channel_id": chan, "kind": "split",
                                    "level": route.level.value, "subtask_index": i,
                                    **self._fence_of(primary.node_id)})
                    break
                item = self.cp.create_work_item(kind, parent_item=self.cp._root_item_id(),
                                                team=team)
                if kind == DelegationKind.FISSION and item.team != "root":
                    team = item.team
                created.append((i, item))
            # 全部 item 建成后再连边（§7 DAG；add_dependency 各自带 CAS）
            for i, item in created:
                for d in deps_by_index[i]:
                    self.cp.add_dependency(created[d][1].item_id, item.item_id)
            # §4 解锁后继：只对 deps 全 accepted 的 item 启动（begin_node 另有
            # DEPS_NOT_READY 硬门禁兜底）；未就绪项不启动，回报 waiting_on。
            from dpswarm.types import AcceptanceState
            for i, item in created:
                if not self.cp.proj.item_ready(item.item_id):
                    waiting = [dep for dep in item.deps
                               if (self.cp.proj.work_items.get(dep) is None
                                   or self.cp.proj.work_items[dep].acceptance
                                   != AcceptanceState.ACCEPTED)]
                    pending.append({"item_id": item.item_id, "waiting_on": waiting})
                    continue
                route = routes[i]
                node = self.cp.begin_node(item.item_id, route)
                self.cp.confirm_node(node.node_id)
                results.append({"item_id": item.item_id, "node_id": node.node_id,
                                "kind": kind.value, "level": route.level.value,
                                "subtask_index": i, **self._fence_of(node.node_id)})
            return True, {"ok": True, "items": results, "pending": pending}
        except ControlPlaneError as e:
            return False, {"ok": False, "error": e.code, "message": str(e), "partial": results}

    def _delegate_rerun(self, body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """重跑既有 item（§8 归因重试的执行臂 + §7 超时重试）：
        - REJECTED → prepare_retry（预算在此消费；耗尽自动 escalate）→
          retire_item_nodes（旧节点退役、lease 归还）→ 新节点两阶段；
        - 执行中超时（节点 blocked_reason=wallclock-timeout 且无在途节点）→
          retry_timeout（同事务 attempt+1 并清超时节点）→ 新节点两阶段；
        - 冷 item（无在途节点，如启动失败）→ 直接重启（启动失败不耗预算 §8）。"""
        from dpswarm.types import AcceptanceState, BlockState, LifecycleState
        item_id = (body or {}).get("item_id")
        item = self.cp.proj.work_items.get(item_id)
        if item is None:
            return False, {"ok": False, "error": "ITEM_UNKNOWN",
                           "message": f"work item {item_id} 不存在"}
        if item.acceptance in (AcceptanceState.ACCEPTED, AcceptanceState.TERMINATED,
                               AcceptanceState.ESCALATED, AcceptanceState.ABORTED_FINALIZE):
            return False, {"ok": False, "error": "ITEM_TERMINAL",
                           "message": f"item {item_id} 已终态 {item.acceptance.value}，不可重跑"}
        try:
            _, route = self._route_from_subtask(body.get("subtask"), team=item.team)
            self._register_agent_routes([route])
            nodes = [n for n in self.cp.proj.nodes.values()
                     if n.item_id == item_id and not n.terminated]
            running = [n for n in nodes
                       if n.lifecycle in (LifecycleState.PROVISIONING, LifecycleState.ACTIVE)
                       and n.blocked == BlockState.NONE]
            if item.acceptance == AcceptanceState.REJECTED:
                try:
                    self.cp.prepare_retry(item_id, new_route=route)  # 预算在此消费（§8）
                except ControlPlaneError as e:
                    if e.code in ("RETRY_BUDGET", "ATTEMPT_EXHAUSTED"):
                        self.cp.escalate(item_id, "budget-exhausted")
                        return True, {"ok": True, "outcome": "escalated",
                                      "message": "重试预算耗尽，已上交（§8）"}
                    if e.code == "POINTS_EXCEEDED":
                        # §9.3 reweight-wait 抑制窗口：增重差额不足——标记在途
                        # 节点等待（tick 超时豁免、不耗预算），容量归还后重发
                        # 本请求即可（lease_reweight 成功时同事务清除标记）。
                        for n in nodes:
                            if not n.terminated:
                                self.cp.set_reweight_wait(
                                    n.node_id, True,
                                    reason=f"points-shortfall: {e}")
                        return False, {"ok": False, "error": "POINTS_EXCEEDED",
                                       "reweight_wait": True,
                                       "message": "点数差额不足，节点已进入 reweight-wait "
                                                  "抑制窗口（§9.3）；容量归还后重发重跑"}
                    raise
                self.cp.retire_item_nodes(item_id)  # 旧节点 drain + lease 释放
            elif item.acceptance in (AcceptanceState.SUBMITTED, AcceptanceState.FINALIZING):
                return False, {"ok": False, "error": "ITEM_UNDER_REVIEW",
                               "message": f"item {item_id} 已提交待验收"
                                          f"（{item.acceptance.value}），先 review"}
            elif running:
                return False, {"ok": False, "error": "ITEM_ALREADY_RUNNING",
                               "message": f"item {item_id} 已有在途节点（ACTIVE/PROVISIONING）"}
            else:
                timed_out = [n for n in nodes if n.blocked == BlockState.BLOCKED
                             and n.blocked_reason == "wallclock-timeout"]
                if timed_out:
                    try:
                        self.cp.retry_timeout(item_id)  # §7 超时重试：预算 + 清超时节点
                    except ControlPlaneError as e:
                        if e.code in ("RETRY_BUDGET", "ATTEMPT_EXHAUSTED"):
                            self.cp.escalate(item_id, "budget-exhausted")
                            return True, {"ok": True, "outcome": "escalated",
                                          "message": "重试预算耗尽，已上交（§8）"}
                        raise
                # 无超时 blocked 节点的冷 item：启动失败不耗预算（§8），直接重启
            node = self.cp.begin_node(item_id, route)
            self.cp.confirm_node(node.node_id)
            return True, {"ok": True, "items": [{"item_id": item_id,
                                                 "node_id": node.node_id,
                                                 "kind": "re-run",
                                                 "level": route.level.value,
                                                 **self._fence_of(node.node_id)}]}
        except ControlPlaneError as e:
            return False, {"ok": False, "error": e.code, "message": str(e), **e.context}

    def submit_output(self, body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """worker（dsh subagent）交付回控制面：记观测 + submitted（§5.7）。
        output 即时内容寻址落盘并绑定证据包（P1-3）；fence 必填（P1-2）：
        delegate 返回的 context_epoch/session_id 原样回带。"""
        item_id = (body or {}).get("item_id")
        node_id = (body or {}).get("node_id")
        output = (body or {}).get("output", "")
        epoch = (body or {}).get("context_epoch")
        session = (body or {}).get("session_id")
        if epoch is None or not session:
            return False, {"ok": False, "error": "FENCE_REQUIRED",
                           "message": "submit 必须携带 delegate 返回的 context_epoch"
                                      " 与 session_id（旧 session 禁写，P1-2）"}
        try:
            # fence 校验（submit）先于观测记账：伪造 fence 被拒后不得把
            # stop_reason/token 脏观测写进唯一真源（§4/§9.1）。
            self.cp.submit(item_id, node_id, output,
                           context_epoch=int(epoch), session_id=session)
            self.cp.record_stop_reason(node_id or "", (body or {}).get("stop_reason", "completed"))
            if (body or {}).get("token_usage"):
                u = body["token_usage"]
                self.cp.record_token_usage(
                    node_id or "", u.get("input_tokens", 0), u.get("output_tokens", 0),
                    u.get("cache_read_tokens", 0), u.get("cache_write_tokens", 0),
                    u.get("cost_usd", 0.0))
            return True, {"ok": True}
        except ControlPlaneError as e:
            return False, {"ok": False, "error": e.code, "message": str(e)}
        except (TypeError, ValueError) as e:
            return False, {"ok": False, "error": "FENCE_REQUIRED", "message": str(e)}

    def review(self, body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """Lead 验收（§4 验收制 / §8 异常路径）：
        - accept：证据落盘 + 原子发布（解锁后继、释放槽与点数）；
        - reject：只打回挂起（rejected-awaiting-rerun）——重试预算由重跑时的
          prepare_retry 消费（提前消费会把"可反悔的 reject"变成"已扣预算的
          retry"，且二次 review 将 ILLEGAL_TRANSITION 卡死 item）；contradiction
          归因维持 reject+escalate；
        - terminate：明确放弃（§7），drain 节点 + 归还槽与点数。"""
        from dpswarm.types import RejectAttribution
        item_id = (body or {}).get("item_id")
        verdict = (body or {}).get("verdict")
        output = (body or {}).get("output", "")
        reason = (body or {}).get("reason")  # terminate 走六值词汇默认，reject 走自由文本默认
        review_reason = reason or "lead-review"  # reject/escalate 自由文本理由默认
        attribution = (body or {}).get("attribution")
        route = (body or {}).get("route")
        if verdict not in ("accept", "reject", "terminate"):
            return False, {"ok": False, "error": "verdict ∈ accept|reject|terminate"}
        item = self.cp.proj.work_items.get(item_id)
        if item is None:
            return False, {"ok": False, "error": "ITEM_UNKNOWN",
                           "message": f"work item {item_id} 不存在"}
        try:
            if verdict == "accept":
                # P1-3 引用制：accept 只准引用 submit 落盘的证据包，正文不再
                # 由 review 提供（防替换）。缺包（如 submit 未带 output）= 拒绝。
                pkg = (body or {}).get("package_id") or item.submission_package_id
                if not pkg:
                    return False, {"ok": False, "error": "PACKAGE_MISSING",
                                   "message": "accept 需引用 submit 落盘的证据包"
                                              "（package_id）；review 不再接收正文（P1-3）"}
                if item.submission_package_id and pkg != item.submission_package_id:
                    return False, {"ok": False, "error": "EVIDENCE_MISMATCH",
                                   "message": f"package {pkg} != submit 绑定的 "
                                              f"{item.submission_package_id}（证据不可替换）"}
                self.cp.begin_finalize(item_id)
                self.cp.complete_accept(
                    item_id, package_id=pkg, evidence_ready=True,
                    accepted_by={"node": "dsh-lead", "via": "dpswarm-dsh-plugin"})
                return True, {"ok": True, "outcome": "accepted"}
            if verdict == "terminate":
                self.cp.terminate(item_id, reason or "manual-stopped")  # §4 六值词汇默认
                return True, {"ok": True, "outcome": "terminated"}
            att = RejectAttribution(attribution) if attribution \
                else RejectAttribution.DESCRIPTION
            rejected_by = {"node": "dsh-lead", "via": "dpswarm-dsh-plugin"}
            if att == RejectAttribution.CONTRADICTION:
                self.cp.reject(item_id, review_reason, att, rejected_by=rejected_by)
                self.cp.escalate(item_id, f"contradiction: {review_reason}")
                return True, {"ok": True, "outcome": "escalated"}
            self.cp.reject(item_id, review_reason, att, rejected_by=rejected_by)
            resp: Dict[str, Any] = {
                "ok": True, "outcome": "rejected-awaiting-rerun",
                "next": "用 dpswarm_delegate(item_id=..., subtask=...) 重跑"
                        "（重试预算在重跑时消费，capability 归因可在 subtask 里换模型）；"
                        "或 dpswarm_review(verdict=terminate) 放弃（释放槽与点数）",
            }
            if route:
                # capability 归因的换模提议仅回显、不持久化——重跑请求须自带 route
                resp["proposed_route"] = route
            return True, resp
        except ControlPlaneError as e:
            if e.code in ("RETRY_BUDGET", "ATTEMPT_EXHAUSTED"):
                self.cp.escalate(item_id, f"budget-exhausted: {review_reason}")
                return True, {"ok": True, "outcome": "escalated"}
            return False, {"ok": False, "error": e.code, "message": str(e)}

    def peer(self, body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """§9.5 分裂对通道投递：先 queued+flush、投递确认后 delivered——
        消息账本（proj.messages）即 evidence，验收时 Lead 可审计主从实际分工。"""
        channel_id = (body or {}).get("channel_id")
        from_node = (body or {}).get("from_node")
        text = (body or {}).get("body")
        if not channel_id or not from_node or text is None:
            return False, {"ok": False, "error": "channel_id / from_node / body 必填"}
        try:
            message_id = self.cp.peer_send(channel_id, from_node, text)
            self.cp.peer_deliver(message_id)
            return True, {"ok": True, "message_id": message_id}
        except ControlPlaneError as e:
            return False, {"ok": False, "error": e.code, "message": str(e)}


class _BodyTooLarge(Exception):
    pass


# 写操作请求体上限（submit 正文含交付文本，5MB 足够，防滥用打崩线程）
MAX_BODY_BYTES = 5_000_000


class Handler(BaseHTTPRequestHandler):
    state: PanelState = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:  # 安静模式
        pass

    # -- 安全门（P0 修复）：Origin 只认 loopback（任意端口），写操作必须带 token --
    def _origin_allowed(self) -> bool:
        """无 Origin（本机进程/同源导航）放行；有 Origin 时只认 127.0.0.1/
        localhost/[::1]——恶意网页的 Origin 是它自己的域名，直接拒。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            host = origin.split("//", 1)[1].split(":")[0].strip("[]").lower()
        except (IndexError, AttributeError):
            return False
        return host in ("127.0.0.1", "localhost", "::1")

    def _token_injectable(self) -> bool:
        """GET / token 注入口径（P2 收口）：无 Origin（本机导航/本机进程）注入；
        有 Origin 时仅与 Host 完全同源（含端口）才注入——跨端口 loopback 页面
        虽过 Origin 门且有 ACAO 回显，但拿到的 HTML 不含 token 本体。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            authority = origin.split("//", 1)[1].split("/", 1)[0].lower()
        except (IndexError, AttributeError):
            return False
        host = (self.headers.get("Host") or "").lower()
        return bool(authority) and authority == host

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization") or ""
        return hmac.compare_digest(got, f"Bearer {self.state.token}")

    def _forbid(self) -> None:
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"error": "origin not allowed"}')

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"error": "missing or invalid bearer token"}')

    # -- helpers ------------------------------------------------------------
    def _apply_cors(self) -> None:
        """只给 loopback Origin 回显 ACAO（替代原来的 *：跨站页面读不到响应）。"""
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._apply_cors()
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise _BodyTooLarge()
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 容错：部分 Windows 客户端按本地代码页发送非 ASCII。浏览器 fetch
            # 恒为 UTF-8；这里兜底解析失败返回空体，由路由层按缺参处理，
            # 不让单个坏请求打崩服务线程。
            try:
                return json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return {}

    # -- routing ------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # CORS 预检：loopback 专属
        if not self._origin_allowed():
            self._forbid()
            return
        self.send_response(204)
        self._apply_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        st = self.state
        if not self._origin_allowed():
            self._forbid()
            return
        if self.path in ("/", "/index.html"):
            page = STATIC / "index.html"
            if page.exists():
                # token 注入：面板同源页面持 token 调写接口（P0 修复配套）；
                # 跨端口 Origin 收窄为不含 token 的版本（_token_injectable）。
                html = page.read_text(encoding="utf-8").replace(
                    "{{DPSWARM_TOKEN}}", st.token if self._token_injectable() else "")
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json({"error": "index.html missing"}, 404)
        elif self.path.startswith("/api/status"):
            # 只读快照：loopback 页面（dph Web UI 状态灯）免 token 可读；
            # 不含 token、不含交付正文。
            self._json(st.status())
        elif self.path.startswith("/api/events"):
            if not self._authorized():
                self._unauthorized()
                return
            limit = 50
            if "limit=" in self.path:
                try:
                    limit = int(self.path.split("limit=")[1].split("&")[0])
                except ValueError:
                    pass
            self._json(st.events(limit))
        elif self.path.startswith("/api/observation"):
            if not self._authorized():
                self._unauthorized()
                return
            self._json(st.observation_report())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        st = self.state
        if not self._origin_allowed():
            self._forbid()
            return
        if not self._authorized():
            self._unauthorized()
            return
        try:
            body = self._body()
        except _BodyTooLarge:
            self._json({"ok": False, "error": "BODY_TOO_LARGE",
                        "message": f"request body exceeds {MAX_BODY_BYTES} bytes"}, 413)
            return
        try:
            if self.path == "/api/spec":
                ok, resp = st.publish_spec(body)
                self._json(resp, 200 if ok else 400)
            elif self.path == "/api/directive":
                ok, resp = st.directive(body)
                self._json(resp, 200 if ok else 400)
            elif self.path == "/api/task":
                self._json(st.run_task(body))
            elif self.path == "/api/seal":
                self._json(st.seal(body))
            elif self.path == "/api/tick":
                self._json(st.tick())
            elif self.path == "/api/delegate":
                ok, resp = st.delegate(body)
                self._json(resp, 200 if ok else 400)
            elif self.path == "/api/submit":
                ok, resp = st.submit_output(body)
                self._json(resp, 200 if ok else 400)
            elif self.path == "/api/review":
                ok, resp = st.review(body)
                self._json(resp, 200 if ok else 400)
            elif self.path == "/api/peer":
                ok, resp = st.peer(body)
                self._json(resp, 200 if ok else 400)
            else:
                self._json({"error": "not found"}, 404)
        # 0.13 结构化错误：输入类错误 400；漏网异常 500 且不外泄 traceback/路径
        except (ValueError, KeyError, TypeError) as e:
            self._json({"ok": False, "error": "BAD_REQUEST", "message": str(e)}, 400)
        except Exception as e:  # noqa: BLE001 - 兜底保证连接存活
            self._json({"ok": False, "error": "INTERNAL_ERROR", "message": type(e).__name__}, 500)


def serve(port: int = 8790, workspace: Optional[Path] = None) -> None:
    # 单实例守卫：HTTPServer 默认 SO_REUSEADDR，Windows 上允许多进程双绑同一
    # 端口、旧进程抢流量（实测踩坑）。connect 探测占用即拒绝启动。
    import socket
    try:
        probe = socket.create_connection(("127.0.0.1", port), timeout=1)
        probe.close()
        raise SystemExit(
            f"端口 {port} 已有服务在监听——DPSwarm 面板可能已在运行"
            f"（直接访问 http://127.0.0.1:{port}/ ，或先结束旧进程）")
    except (ConnectionRefusedError, OSError, TimeoutError):
        pass
    ws = workspace or Path(".dpswarm-panel")
    Handler.state = PanelState(ws)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"DPSwarm 控制面板: http://127.0.0.1:{port}/  (workspace={ws.resolve()})")
    httpd.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(prog="dpswarm-panel")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--workspace", default=".dpswarm-panel")
    args = ap.parse_args()
    serve(args.port, Path(args.workspace))


if __name__ == "__main__":
    main()
