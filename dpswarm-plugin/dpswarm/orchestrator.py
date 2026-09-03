"""DPswarm 编排器：把控制面、LLM 传输层、上下文子系统、观测组装成可运行任务流。

对应机制全景（文档附录）：
- 机制一（§3）：Lead 决策前注入事实块（routing.fact_block）。
- 机制二（§4）：每次委派记全账（DelegationRecord + token 分账）。
- 机制三（§5）：节点准入前经 Assembler 装配 context package（有包才启动 §5.8 预装契约）。
- 机制四（§6）：Lead 消耗 token 记账（盈亏线数据）。
- 机制五（§7）：拓扑动作（single/derive/fission/split）由 Lead 语义决策、
  控制面硬准入执行；fission 的多个 worker 并行执行（线程池；控制面有锁，
  物理执行并行、状态变更仍走单写者串行链）。
- 机制六（§8）：打回 → 归因 → 对因处置；预算耗尽走上交。

Lead 是 LLM：所有语义决策走 provider；所有状态变更走 ControlPlane。
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .control import AdmissionError, ControlPlane, ControlPlaneError
from .events import DelegationRecord
from .invariants import TERMINAL_ACCEPTANCE
from .levels import escalation_path, match_bucket, recommend_upgrade
from .observation import ObservationSink
from .providers.base import QuotaExhausted, RateLimitBackoff
from .routing import build_lead_prompt, parse_lead_decision, route_from_decision
from .types import (
    AcceptanceState,
    DelegationKind,
    Level,
    ModelCatalog,
    ModelRoute,
    NodeRole,
    RejectAttribution,
    new_id,
)

PersonaHeader = "# DPswarm worker\n# 稳定内容在前（persona），任务特定在后（prompt）——§5.3 排列顺序。\n"


class Orchestrator:
    #: §5.3 inline 上限（与 AssemblerBrief.inline_token_limit 同口径）：低于此值
    #: 的小包进 prompt；大包只传不可变引用（正文由 runtime 落盘持有）。
    INLINE_TOKEN_LIMIT = 2000
    #: §5.4 pull 兜底轮数上限：裁剪是初筛，worker 缺料可补拉（任务全程可修正）。
    MAX_PULL_ROUNDS = 2
    #: §4 RATE_LIMIT 退避序列：1s/2s/4s 最多 3 次重试；retry_after 优先、
    #: 单次封顶 8s；仍失败则上抛（背压有界，不无限等）。
    RATE_LIMIT_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
    #: §4 retry_after 单次退避封顶（秒）。
    RATE_LIMIT_BACKOFF_CAP = 8.0

    def __init__(
        self,
        control: ControlPlane,
        provider,                      # providers.base.Provider
        store_dir: Optional[Path] = None,
        assembler: Any = None,         # context.ContextAssembler（可选：无则直传任务文本）
        memory: Any = None,            # context.MemoryService（可选）
        context_manager: Any = None,   # context.ContextManagerLLM（可选：按需压缩）
        profile: Any = None,           # profile.ProfileStore（可选：V1 只攒不用）
        lead_route: Optional[ModelRoute] = None,
        max_turns: int = 8,
        max_workers: int = 4,
    ) -> None:
        self.cp = control
        self.provider = provider
        self.catalog = control.catalog
        self.assembler = assembler
        self.memory = memory
        self.context_manager = context_manager
        self.profile = profile
        self.lead_route = lead_route or ModelRoute(
            provider="dpswarm", model="root-lead", level=Level.S)
        self.store_dir = store_dir
        self.max_turns = max_turns
        self.max_workers = max_workers
        self.lead_tokens = 0
        self._worker_routes: Dict[str, ModelRoute] = {}
        self._packages: Dict[str, Dict[str, str]] = {}   # item_id -> {content, ref, inline}
        self._submissions: Dict[str, str] = {}           # item_id -> worker 交付文本
        self._cm_costs: List[Dict[str, Any]] = []        # CM 记账（记触发方账下 §5.8/§7）

    # ------------------------------------------------------------------
    # 机制三：准入前装配（预装契约：先成包，启动只提交 §5.8）
    # ------------------------------------------------------------------

    def prepare_package(self, item_id: str, route: ModelRoute, task_text: str,
                        select: Optional[List[str]] = None) -> Tuple[str, str]:
        if self.assembler is None:
            content, inline = task_text, True
        else:
            from .context.assembler import AssemblerBrief  # 延迟导入避免环
            brief = AssemblerBrief(
                task_intent=task_text, select=select or [], scope="team",
                token_budget=8000, inline_token_limit=self.INLINE_TOKEN_LIMIT,
            )
            # §5.1：异构只杀跨模型缓存——worker 与 Lead 同模型（同构）时前缀
            # 共享成立，可直连统一前缀；异构才走检索裁剪 + 按需压缩。
            heterogeneous = not route.same_model(self.lead_route)
            # §7 CM 成本记触发方账下：用捕获 item_id 的 lambda 包 compress_fn，
            # 使 _cm_compress 把成本记到 ctx-job:<item_id>（record_token_usage
            # 是纯记录事件，不要求节点存在）。Assembler 的 compress_fn 注入点
            # 在构造器（assemble() 不收该参），此处按次注入、用后还原；无
            # manager 时保持 None（不触发压缩，与原语义一致）。
            prev_fn = getattr(self.assembler, "compress_fn", None)
            self.assembler.compress_fn = (
                lambda materials, brief: self._cm_compress(materials, brief, item_id)
            ) if self.context_manager is not None else None
            try:
                pkg = self.assembler.assemble(brief, route, heterogeneous=heterogeneous)
            finally:
                self.assembler.compress_fn = prev_fn
            content = pkg.content if pkg.content else task_text
            inline = len(content) // 3 <= self.INLINE_TOKEN_LIMIT
        ref, digest = self._stash(item_id, content)
        self._packages[item_id] = {"content": content, "ref": ref, "inline": inline}
        return ref, digest

    def _cm_compress(self, materials: List[str], brief: Any, item_id: str) -> str:
        """CM 适配器：Assembler 的 compress_fn → manager 压缩 + 成本记触发方账下。

        manager 不成为启动协议的执行臂（§5.8）；成本按 §5.5/§7 记 context job
        账（ctx-job:<item_id>，非节点、不占点），不混入 root lead 的调度账。"""
        if self.context_manager is None:
            return materials[-1] if materials else ""
        text, account = self.context_manager.compress(materials, brief)
        self._cm_costs.append({"item": item_id, **account})
        if account.get("input_tokens") or account.get("cost_usd"):
            self.cp.record_token_usage(
                "ctx-job:%s" % item_id,
                input_tokens=account.get("input_tokens", 0),
                output_tokens=account.get("output_tokens", 0),
                cost_usd=account.get("cost_usd", 0.0))
        return text

    def _stash(self, item_id: str, content: str) -> Tuple[str, str]:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        ref = f"pkg://{item_id}/{digest[:12]}"
        if self.store_dir is not None:
            self.store_dir.mkdir(parents=True, exist_ok=True)
            (self.store_dir / f"{item_id}.{digest[:12]}.md").write_text(
                content, encoding="utf-8")
        return ref, digest

    # ------------------------------------------------------------------
    # 机制一 + 五：Lead 决策循环
    # ------------------------------------------------------------------

    def run_task(self, task: str) -> Dict[str, Any]:
        """主入口：默认单 agent（§7），Lead 在理解任务后决策拓扑，全程可生长/收缩。"""
        bucket = match_bucket(task)
        spec = self.cp.proj.spec
        done: List[str] = []
        pending: List[Dict[str, Any]] = []
        outcome = {"task": task, "bucket": bucket, "items": [], "actions": []}

        try:
            for _turn in range(self.max_turns):
                prompt = build_lead_prompt(
                    self.catalog, spec.max_active_node_points,
                    self.cp.proj.active_points, task, done,
                    [p["title"] for p in pending], spec.max_team_workers, bucket)
                decision = self._ask_lead(prompt)
                # 归一：验收裁决文本（verdict 键）在决策循环里语义等同 accept/reject
                action = decision.get("action") or (
                    "accept" if decision.get("verdict") == "accept" else None)
                outcome["actions"].append(action)

                if action == "single":
                    self._run_single(task, bucket, outcome)
                    break
                if action in ("derive", "fission"):
                    deferred = self._dispatch(action, decision, task, bucket, pending, outcome)
                    # §7 裂变 = DAG 协调：每轮 _run_pending（内含验收）完成后，
                    # 把上游 accepted 而新变为 ready 的 deferred 项继续 begin+run，
                    # 直到全部完成或无进展（无进展 = 依赖卡死，记 deps-stuck
                    # 退出交 Lead 后续决策处置，不死循环）。
                    while True:
                        if pending:
                            self._run_pending(pending, bucket, outcome)
                            pending = []
                        if not deferred:
                            break
                        progressed = self._promote_deferred(deferred, pending, outcome)
                        if not progressed and not pending:
                            outcome["actions"].append("deps-stuck")
                            break
                    # §7「拓扑决策全程可做」：派发执行不终局——Lead 看结果再决策
                    # （继续生长 / accept 收束 / degenerate 收回 / escalate 上交）。
                    done.extend("%s: %s" % (i["item"], i["outcome"]) for i in outcome["items"])
                    continue
                if action == "split":
                    self._run_split(decision, task, bucket, outcome)
                    done.extend("%s: %s" % (i["item"], i["outcome"]) for i in outcome["items"])
                    continue
                if action == "degenerate":
                    # 退化是精髓（§7）：可逆收缩——结论落盘、主 agent 只收摘要。
                    # 遍历投影中全部非终态 item（outcome["items"] 只含已结案项，
                    # deps-stuck 悬挂项不在其中、会漏收）；协助者先走退化收回（§7）。
                    for item in list(self.cp.proj.work_items.values()):
                        if item.acceptance in TERMINAL_ACCEPTANCE:
                            continue
                        for n in list(self.cp.proj.nodes.values()):
                            if (n.item_id == item.item_id and not n.terminated
                                    and n.role == NodeRole.ASSISTANT):
                                self.cp.degenerate_assistant(n.node_id)
                        self.cp.terminate(
                            item.item_id, reason="manual-stopped",
                            summary=self._submissions.get(item.item_id, "")[:200])
                    outcome["final"] = "degenerated"
                    break
                if action == "accept":
                    outcome["final"] = "accepted-by-lead"
                    break
                if action == "escalate":
                    # 任务上交（§8）：全部非终态 item 转 escalated——含未提交项
                    # （(None, ESCALATED) 合法，§5.7）；已结案项跳过。
                    for item in list(self.cp.proj.work_items.values()):
                        if item.acceptance not in TERMINAL_ACCEPTANCE:
                            self.cp.escalate(item.item_id, "lead-decision")
                    outcome["final"] = "escalated"
                    break
                # 未知/None 动作：记录后继续
        except QuotaExhausted:
            # §4：QUOTA（余额/鉴权耗尽）不是背压——明确终止/上交人工。不炸穿
            # run_task（否则观测断账）：对全部非终态 item 有弃置后正常返回。
            outcome["final"] = "quota-exhausted"
            self._abandon_open_items_for_quota()
        except RateLimitBackoff:
            # §4：429 是背压——退避有界耗尽后与 QUOTA 同构收口（不炸穿
            # run_task）；画像侧由 record_attempt 文本兜底进 ops 运维表（§4）。
            outcome["final"] = "rate-limit-exhausted"
            self._abandon_open_items_for_quota("rate-limit-exhausted")
        # P1-6 终局闭环：Lead 裁定整树验收通过 → 封存三段式收尾（root item
        # accepted + 全树 drain + root lease 归还，见 finish_seal）。单干路径
        # root 已 accepted，finish_seal 会跳过重复终局。
        if outcome.get("final") == "accepted-by-lead":
            try:
                self.cp.begin_seal("root")
                self.cp.begin_settlement("root")
                self.cp.finish_seal("root")
            except ControlPlaneError as e:
                outcome["seal_error"] = f"{e.code}: {e}"
        self.cp.tick()
        # §6 盈亏线节省侧接线（V1 代理口径）：estimated_savings = worker 节点
        # token 合计，作"若由 Lead 直做"的消耗估算——同构前缀近似（Lead 直做
        # 需读入等量材料），不含 Lead/worker 的单价与能力差；观测侧只读事件流。
        worker_tokens = ObservationSink(
            self.cp.store.read_all()).economics_summary()["worker_tokens"]
        self.cp.record_economics(self.cp._root_item_id(), self.lead_tokens,
                                 worker_tokens)
        return outcome

    def _abandon_open_items_for_quota(self, reason: str = "quota-exhausted") -> None:
        """§4/§8 耗尽型有弃置（QUOTA / 429 退避耗尽）：对所有非终态 item 做合法
        转换收口。

        - None / SUBMITTED / REJECTED → escalate（合法转换，无裁决、不进画像）；
        - FINALIZING → terminate(reason="manual-stopped")——(FINALIZING, ESCALATED)
          不在 §5.7 合法域，只能明确终止（reason 走 §4 六值词汇）。"""
        for item in list(self.cp.proj.work_items.values()):
            try:
                if item.acceptance in (None, AcceptanceState.SUBMITTED,
                                       AcceptanceState.REJECTED):
                    self.cp.escalate(item.item_id, reason)
                elif item.acceptance == AcceptanceState.FINALIZING:
                    self.cp.terminate(item.item_id, reason="manual-stopped")
            except ControlPlaneError:
                # 收口尽力而为：个别转换失败不让其余 item 悬挂（槽位泄漏更贵）
                continue

    def _ask_lead(self, prompt: str) -> Dict[str, Any]:
        result = self._complete_with_backoff(self.lead_route, [
            {"role": "system", "content": PersonaHeader + "你是调度 Lead。"},
            {"role": "user", "content": prompt},
        ], node_id=self.cp.root_lead_node)
        self.lead_tokens += result.usage.total_tokens()  # cache 分账同计（§4/§6 两口径一致）
        self.cp.record_token_usage(
            self.cp.root_lead_node, result.usage.input_tokens, result.usage.output_tokens,
            result.usage.cache_read_tokens, result.usage.cache_write_tokens,
            result.usage.cost_usd)
        try:
            return parse_lead_decision(result.text)
        except ValueError:
            # 决策不可解析：保守处理为上交（不安全的 accept 结案被禁止）。
            return {"action": "escalate",
                    "verdict_reason": f"unparseable lead output: {result.text[:80]}"}

    # ------------------------------------------------------------------
    # 拓扑动作执行
    # ------------------------------------------------------------------

    def _run_single(self, task: str, bucket: str, outcome: Dict[str, Any]) -> None:
        """保持单干：root work item 直接由 root lead 完成（§7 默认态）。"""
        root_item = self.cp._root_item_id()
        result = self._complete_with_backoff(self.lead_route, [
            {"role": "user", "content": f"直接完成任务：\n{task}"}],
            node_id=self.cp.root_lead_node)
        self.lead_tokens += result.usage.total_tokens()
        self.cp.record_token_usage(
            self.cp.root_lead_node, result.usage.input_tokens, result.usage.output_tokens,
            result.usage.cache_read_tokens, result.usage.cache_write_tokens,
            result.usage.cost_usd)
        self.cp.record_stop_reason(self.cp.root_lead_node, result.stop_reason.value)
        self.cp.submit(root_item, self.cp.root_lead_node, result.text)
        self.cp.begin_finalize(root_item)
        pkg_id = f"single-{new_id('pkg')}"
        self.cp.store_evidence_package(root_item, pkg_id, result.text)
        ev = self.cp.complete_accept(
            root_item, package_id=pkg_id, evidence_ready=True,
            accepted_by={"node": self.cp.root_lead_node,
                         "route": f"{self.lead_route.provider}/{self.lead_route.model}",
                         "level": self.lead_route.level.value})
        outcome["final"] = "single"
        outcome["result_text"] = result.text
        outcome["items"].append({"item": root_item, "outcome": "accepted", "events": 1})

    def _dispatch(self, action: str, decision: Dict[str, Any], task: str,
                  bucket: str, pending: List[Dict[str, Any]],
                  outcome: Dict[str, Any]) -> List[Dict[str, Any]]:
        """派生 / 裂变：创建全部 work item（硬准入）→ 接 DAG 依赖边（§7）→
        两阶段启动。返回 deferred（依赖未就绪、等待上游 accepted 解锁的项）。

        §7 裂变 = DAG 协调：subtasks 条目支持可选 deps（0 基下标，指向本次
        subtasks 里它依赖的前序项）；执行层据此调 add_dependency 建边
        （控制面早有 deps/add_dependency/CAS，此处是执行层首次接线）。"""
        route = route_from_decision(decision, self.catalog)
        if route is None:
            outcome["actions"].append("admission-rejected:route-unavailable")
            return []
        root_item = self.cp._root_item_id()
        spec = self.cp.proj.spec
        kind = DelegationKind.DERIVE if action == "derive" else DelegationKind.FISSION
        subtasks = decision.get("subtasks") or []
        if subtasks:
            entries = [_parse_subtask(s, task) for s in subtasks]
        else:
            # 派生 = 单个较轻量子 agent（§7）：未细分时以任务本身为子任务；
            # 裂变至少拆一片（不拆说明不该选裂变）。
            entries = [{"title": task, "deps": []}]
        # §2 硬准入：subtasks 超限不静默截断——结构化拒绝，由 Lead 重选。
        if len(entries) > spec.max_team_workers:
            outcome["actions"].append(
                "admission-rejected:SUBTASKS_OVER_LIMIT(max=%d)" % spec.max_team_workers)
            return []
        # 先创建全部 work item（保持裂变建队逻辑：首 item 建队，其余挂同队）
        deferred: List[Dict[str, Any]] = []
        item_ids: List[Optional[str]] = []
        team = "root"
        team_created = False  # 一次裂变 = 一个子 Team（§7）
        for entry in entries:
            try:
                if kind == DelegationKind.FISSION and not team_created:
                    item = self.cp.create_work_item(kind, parent_item=root_item,
                                                    deps=[], team=team)
                    team = item.team if item.team != "root" else team
                    # root lead 兼任子 Team Lead：只登记指挥关系，不移动归属
                    # （root 控制节点的点数不挤占子 Team 的 50% cap）。
                    self.cp.become_team_lead(self.cp.root_lead_node, team, move_node=False)
                    team_created = True
                else:
                    item = self.cp.create_work_item(kind, parent_item=root_item,
                                                    deps=[], team=team)
            except (AdmissionError, ControlPlaneError) as e:
                outcome["actions"].append(f"admission-rejected:{e.code}")
                item_ids.append(None)
                continue
            item_ids.append(item.item_id)
            self._worker_routes[item.item_id] = route
            deferred.append({"item": item.item_id, "node": None,
                             "title": entry["title"], "attempt": 1})
        # 按边接依赖（§7；expected_graph_revision 的 CAS 在 control 内部处理）
        for idx, entry in enumerate(entries):
            after = item_ids[idx] if idx < len(item_ids) else None
            if after is None:
                continue
            for dep_idx in entry["deps"]:
                if not isinstance(dep_idx, int) or not (0 <= dep_idx < len(item_ids)):
                    continue
                before = item_ids[dep_idx]
                if before is None or before == after:
                    continue
                try:
                    self.cp.add_dependency(before, after)
                except ControlPlaneError as e:
                    outcome["actions"].append(f"admission-rejected:{e.code}")
        # 再调度执行：只对 deps 已满足（item_ready）的 item 启动；未就绪留
        # deferred。该顺序保证不会触发 begin_node 的 DEPS_NOT_READY 依赖门禁。
        self._promote_deferred(deferred, pending, outcome)
        return deferred

    def _promote_deferred(self, deferred: List[Dict[str, Any]],
                          pending: List[Dict[str, Any]],
                          outcome: Dict[str, Any]) -> bool:
        """deferred 晋级：deps 全部 accepted（item_ready，§4 解锁后继）的项
        装配包 → 两阶段启动 → 进 pending。返回本轮是否有进展。"""
        progressed = False
        for p in list(deferred):
            item = self.cp.proj.work_items.get(p["item"])
            if item is None or item.acceptance is not None:
                deferred.remove(p)   # 已被外部收口（终态/不存在），不再等
                continue
            if not self.cp.proj.item_ready(p["item"]):
                continue
            deferred.remove(p)
            progressed = True
            route = self._worker_routes.get(p["item"])
            if route is None:
                continue
            pkg_ref, pkg_hash = self.prepare_package(p["item"], route, p["title"])
            try:
                node = self.cp.begin_node(p["item"], route,
                                          package_ref=pkg_ref, package_hash=pkg_hash)
                self.cp.confirm_node(node.node_id)
            except (AdmissionError, ControlPlaneError) as e:
                outcome["actions"].append(f"admission-rejected:{e.code}")
                continue
            p["node"] = node.node_id
            pending.append(p)
        return progressed

    def _run_pending(self, pending: List[Dict[str, Any]], bucket: str,
                     outcome: Dict[str, Any]) -> None:
        """worker 并行执行（物理执行并行；状态变更走单写者串行链）→ Lead 逐个验收。

        §4：QUOTA 不是背压——逐 future 捕获，失败项只打标（p["quota_exhausted"]），
        不中断其余 worker；有弃置在 _review 开头按合法转换执行。"""
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_worker, p, bucket): p for p in pending}
            for fut in futures:
                p = futures[fut]
                try:
                    fut.result()
                except QuotaExhausted:
                    p["quota_exhausted"] = True
                except RateLimitBackoff:
                    # §4：429 背压退避耗尽——打标收口（同 QUOTA 矩阵），不中断其余 worker
                    p["rate_limited"] = True
        for p in pending:
            self._review(p, bucket, outcome)


    def _complete_with_backoff(self, route, messages, node_id):
        """§4 通用传输包装：全部 provider.complete 调用面统一走此门。

        - RATE_LIMIT 是背压信号——按 1s/2s/4s 退避重试最多 3 次（retry_after
          优先、单次封顶 8s），仍失败则上抛；不进 failure audit / 画像。
        - QUOTA（余额/鉴权耗尽）不是背压——记 stop_reason aborted + 运维审计
          事件 + profile quota=True 后上抛，由上层做明确终止/上交（不重试）。"""
        for attempt in range(len(self.RATE_LIMIT_BACKOFF_SECONDS) + 1):
            try:
                return self.provider.complete(route, messages)
            except RateLimitBackoff as e:
                if attempt >= len(self.RATE_LIMIT_BACKOFF_SECONDS):
                    raise   # 背压有界：3 次退避后仍 429，上抛由调用方处置
                delay = self.RATE_LIMIT_BACKOFF_SECONDS[attempt]
                if getattr(e, "retry_after", None) is not None:
                    delay = float(e.retry_after)
                time.sleep(min(max(delay, 0.0), self.RATE_LIMIT_BACKOFF_CAP))
            except QuotaExhausted as e:
                self.cp.record_stop_reason(node_id, "aborted")
                self.cp._record("stop_reason_recorded", {
                    "node_id": node_id, "stop_reason": "aborted",
                    "quota_exhausted": True, "detail": str(e)})
                if self.profile is not None:
                    self.profile.record_attempt(
                        "%s/%s" % (route.provider, route.model), "overall",
                        "quota-exhausted", None, None, quota=True)
                raise

    def _run_worker(self, p: Dict[str, Any], bucket: str) -> None:
        """worker 执行（§5.3/§5.4 投递契约）：
        - inline 小包正文进 prompt；大包只传不可变引用（正文在 artifact store）
        - pull 兜底：worker 输出 `PULL: <query>` 行时经 Memory Service 补料再来
          一轮（裁剪是初筛，任务全程可修正，§5.4）"""
        item_id, node_id = p["item"], p["node"]
        route = self._worker_routes[item_id]
        pkg = self._packages.get(item_id, {"content": p["title"], "ref": "", "inline": True})
        if pkg.get("inline", True):
            user_msg = pkg["content"]
        else:
            user_msg = (f"任务材料包 {pkg['ref']}（超 inline 上限，正文已落盘，"
                        f"此处为引用投递 §5.3）。\n任务：{p['title']}\n"
                        "需要补充材料时输出一行 `PULL: <关键词>`。")
        messages = [
            {"role": "system", "content": PersonaHeader},
            {"role": "user", "content": user_msg},
        ]
        total_in = total_out = total_cr = total_cw = 0
        cost = 0.0
        text, stop = "", None
        for _round in range(self.MAX_PULL_ROUNDS + 1):
            result = self._complete_with_backoff(route, messages, node_id)
            total_in += result.usage.input_tokens
            total_out += result.usage.output_tokens
            total_cr += result.usage.cache_read_tokens
            total_cw += result.usage.cache_write_tokens
            cost += result.usage.cost_usd
            text, stop = result.text, result.stop_reason
            pull = self._extract_pull(text)
            if pull is None or self.memory is None:
                break
            # §5.4 兜底：缺口经 Memory Service 补料（活 worker 的正常运行机制，
            # 与 §5.8 禁止的 successor 拉 predecessor 增量无关）。
            hits = self.memory.retrieve(scope="root", query=pull, limit=3)
            extra = "\n".join(f"- {m.content}" for m in hits) or "（无命中材料）"
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"PULL 结果（query={pull}）：\n{extra}"},
            ]
        self.cp.record_token_usage(node_id, total_in, total_out, total_cr, total_cw, cost)
        self.cp.record_stop_reason(node_id, stop.value if stop else "completed")
        self._submissions[item_id] = text
        try:
            self.cp.submit(item_id, node_id, text)
        except ControlPlaneError as e:
            self._submissions[item_id] = f"<submit-failed {e.code}>"

    @staticmethod
    def _extract_pull(text: str) -> Optional[str]:
        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("PULL:"):
                return line[5:].strip() or None
        return None


    def _run_split(self, decision: Dict[str, Any], task: str, bucket: str,
                   outcome: Dict[str, Any]) -> None:
        """分裂（§7）：同层拓宽——**不新建 work item**（主从共享既有 item，
        不加层、不占新槽；此前建 DERIVE item 是简化偏差）。主执行者 +
        1 同构协助者直接挂在被分裂的 root item 上，peer 通道随分裂建立
        （§9.5）。协助者输出经通道回报主执行者（消息进 evidence），主统一
        提交验收；协助者不独立提交。"""
        route = route_from_decision(decision, self.catalog)
        if route is None:
            outcome["actions"].append("admission-rejected:route-unavailable")
            return
        # 被分裂的既有 item = root item（Lead 对整任务判可分）。root item
        # acceptance=None 时 begin_node 合法，主执行者节点直接建在其上。
        item_id = self.cp._root_item_id()
        try:
            pkg_ref, pkg_hash = self.prepare_package(item_id, route, task)
            primary = self.cp.begin_node(item_id, route,
                                         package_ref=pkg_ref, package_hash=pkg_hash)
            self.cp.confirm_node(primary.node_id)
            assistant, chan = self.cp.split(primary.node_id, route,
                                            package_ref=pkg_ref, package_hash=pkg_hash)
            self.cp.confirm_node(assistant.node_id)  # 协助者同样走完两阶段（§9.3），
                                                     # 否则永停 PROVISIONING、超时时钟不起算
        except (AdmissionError, ControlPlaneError) as e:
            outcome["actions"].append("admission-rejected:%s" % e.code)
            return
        self._worker_routes[item_id] = route
        # 协助者先跑：产出经 peer 通道 queued→delivered（消息账本即 evidence，§9.5）
        a_result = self._complete_with_backoff(route, [
            {"role": "system", "content": PersonaHeader},
            {"role": "user", "content": "协助分工（写范围后半）：%s" % task}],
            node_id=assistant.node_id)
        mid = self.cp.peer_send(chan, assistant.node_id, a_result.text)
        self.cp.peer_deliver(mid)
        self.cp.record_token_usage(
            assistant.node_id, a_result.usage.input_tokens, a_result.usage.output_tokens,
            a_result.usage.cache_read_tokens, a_result.usage.cache_write_tokens,
            a_result.usage.cost_usd)
        # §7/§9.5：协助者回报物理上拼进主执行者包——产出进通道账本后不能丢弃，
        # 主执行者必须拿得到。inline 判定在 prepare_package 已完成，此处只拼
        # content 直投，不重建第二条投递路径。
        self._packages[item_id]["content"] += "\n\n协助者回报：\n" + a_result.text
        pending = [{"item": item_id, "node": primary.node_id,
                    "title": task + "（含协助者回报）", "attempt": 1,
                    # §4「分裂结构必须在观测中标注」（§7）：主执行者标 split-primary
                    "topology": "split-primary"}]
        self._run_pending(pending, bucket, outcome)
        # §7 协助者奖励信号由主执行者代写：assistant-accepted / assistant-rejected
        # 随主 work item 结案一并进观测（协助者不独立提交，parent Lead 只验收
        # 主执行者，不变）；accepted_by 标 proxy=True 指明代写身份。
        final_item = self.cp.proj.work_items.get(item_id)
        accepted = (final_item is not None
                    and final_item.acceptance == AcceptanceState.ACCEPTED)
        assistant_rec = DelegationRecord(
            record_id=new_id("rec"), item_id=item_id,
            node_id=assistant.node_id, lead_node_id=primary.node_id,
            route={"provider": route.provider, "model": route.model,
                   "level": route.level.value, "source": route.source.value},
            topology="split-assistant",
            team=final_item.team if final_item else "root",
            attempt=final_item.attempt if final_item else 1,
            outcome="assistant-accepted" if accepted else "assistant-rejected",
            accepted_by={"node": primary.node_id, "proxy": True} if accepted else None,
            rejected_by=None if accepted else {"node": primary.node_id, "proxy": True},
        )
        self.cp.record_observation(assistant_rec)

    def _review(self, p: Dict[str, Any], bucket: str, outcome: Dict[str, Any]) -> None:
        """Lead 验收（§4 奖励信号：Lead 验收制——通过=成功、打回=失败）。

        打回按 §8 归因四分支对因处置（处置路由 escalation_path 接线）：
        - capability   → 升级模型重试（recommend_upgrade，升级上限 = Lead 级别；
                         A→S 前先深挖——最贵升级前做归因确认，防系统问题误当
                         能力问题，升到 S 也救不了，§8）
        - context      → 修 context 重试：重建更丰富的 package（§5.8"每轮重试
                         喂更丰富的 capsule"），不动模型（兼作 manager 裁偏率观测点）
        - description  → 修描述重试：完整任务书 + 打回理由（修述）
        - contradiction→ 任务矛盾：退化/上报，不硬磕（escalate）
        """
        item_id = p["item"]
        if p.get("quota_exhausted"):
            # §4 QUOTA 有弃置（明确终止/上交人工，无裁决、不进画像）：非终态
            # 统一 escalate（含未提交项）；FINALIZING 不在 escalate 合法域（§5.7），
            # 明确终止（reason 走 §4 六值词汇）。
            item = self.cp.proj.work_items.get(item_id)
            acc = item.acceptance if item else None
            if acc != AcceptanceState.FINALIZING:
                self.cp.escalate(item_id, "quota-exhausted")
            else:
                self.cp.terminate(item_id, reason="manual-stopped")
            self._record_delegation(p, "escalated", bucket)
            outcome["items"].append({"item": item_id, "outcome": "escalated",
                                     "submission": ""})
            return
        if p.get("rate_limited"):
            # §4：429 退避耗尽的有弃置——转换矩阵与 QUOTA 同构（无裁决、不进
            # 画像；profile 文本兜底自动进 ops 运维表）。
            item = self.cp.proj.work_items.get(item_id)
            acc = item.acceptance if item else None
            if acc != AcceptanceState.FINALIZING:
                self.cp.escalate(item_id, "rate-limit-exhausted")
            else:
                self.cp.terminate(item_id, reason="manual-stopped")
            self._record_delegation(p, "escalated", bucket)
            outcome["items"].append({"item": item_id, "outcome": "escalated",
                                     "submission": ""})
            return
        submission = self._submissions.get(item_id, "")
        verdict = self._ask_lead_review(p["title"], submission)
        record_outcome = None
        for _attempt in range(self.cp.proj.spec.max_attempts):
            if verdict.get("verdict") == "accept":
                self.cp.begin_finalize(item_id)
                package_id = f"dep-{new_id('pkg')}"
                self.cp.store_evidence_package(item_id, package_id, submission)
                self.cp.complete_accept(
                    item_id, package_id=package_id, evidence_ready=True,
                    accepted_by={"node": self.cp.root_lead_node,
                                 "route": f"{self.lead_route.provider}/{self.lead_route.model}",
                                 "level": self.lead_route.level.value})
                self._extract_memory(item_id, package_id, submission)
                record_outcome = "accepted"
                break
            attribution = _attribution(verdict, self.cp, p["node"])
            disposition = escalation_path(attribution)   # §8 处置路由
            if disposition == "degenerate-or-escalate":
                # 任务矛盾/不可行：不硬磕（§8）
                self.cp.escalate(item_id, f"contradiction: {verdict.get('verdict_reason','')}")
                record_outcome = "escalated"
                break
            # 换模型（capability）：升级上限 = Lead 级别；超限走任务上交（§7 推论②）
            failed_route = self._worker_routes[item_id]  # 本次交付的实际路由（升级前先取）
            new_route = None
            if disposition == "upgrade-model-and-retry":
                current = failed_route
                upgrade = recommend_upgrade(
                    self.catalog, current, self.lead_route.level, bucket)
                if upgrade is None:
                    self.cp.escalate(item_id, "upgrade-exceeds-lead-level (§7 推论②)")
                    record_outcome = "escalated"
                    break
                if upgrade.level == Level.S and current.level != Level.S:
                    # A→S 前必须先深挖失败原因（§8）——深挖门事件可审计
                    self.cp._record("watchdog_suggested", {
                        "node_id": p["node"], "kind": "deep-dive-before-s-upgrade",
                        "note": verdict.get("verdict_reason", "")[:200]})
                new_route = ModelRoute(upgrade.provider, upgrade.model,
                                       level=upgrade.level,
                                       point_weight=max(1, upgrade.context_window // 64_000))
                self._worker_routes[item_id] = new_route
            try:
                self.cp.reject(item_id, verdict.get("verdict_reason", "lead-rejected"),
                               attribution,
                               rejected_by={"node": self.cp.root_lead_node,
                                            "route": f"{self.lead_route.provider}/{self.lead_route.model}",
                                            "level": self.lead_route.level.value})
                # §4/§8：打回 = 免费失败归因数据——每次打回立即喂画像 failures
                # 表（此前 failures 恒空、负样本断流）；末次归因透传委派记录。
                p["attribution"] = attribution.value
                if self.profile is not None:
                    self.profile.record_attempt(
                        f"{failed_route.provider}/{failed_route.model}", bucket,
                        "rejected", attribution=attribution.value,
                        reason=verdict.get("verdict_reason", ""))
                self.cp.prepare_retry(item_id, new_route=new_route
                                      if disposition == "upgrade-model-and-retry" else None)
                if disposition == "upgrade-model-and-retry" and new_route is not None:
                    # 换模型重试经 RESUME 复投（§9.3 唤起协议）：两阶段第二步——
                    # session 确认后节点回 active，才能执行与提交。
                    self.cp.confirm_node(p["node"])
            except ControlPlaneError as e:
                if e.code == "POINTS_EXCEEDED":
                    # §9.3 reweight-wait 抑制窗口：升级增重点数差额不足——节点
                    # 保持等待且不得启动新模型（事务原子未推进 attempt，不耗
                    # 重试预算）；标记后 tick 超时豁免，容量归还后由重试清除。
                    self.cp.set_reweight_wait(
                        p["node"], True, reason=f"points-shortfall: {e}")
                    record_outcome = "reweight-wait"
                    break
                # 预算耗尽 → 上交（无裁决、不进画像）
                self.cp.escalate(item_id, f"budget-exhausted: {e.code}")
                record_outcome = "escalated"
                break
            # 重试喂料（§5.8：每轮重试喂更丰富的 capsule——只增不减）
            route = self._worker_routes[item_id]
            retry_brief = ("打回重试（归因 %s，处置 %s）。\n打回理由：%s\n任务全文：%s\n"
                           % (attribution.value, disposition,
                              verdict.get("verdict_reason", ""), p["title"]))
            if disposition == "fix-context-and-retry":
                extra = ""
                if self.memory is not None:
                    hits = self.memory.retrieve(scope="root", query=p["title"], limit=3)
                    extra = "\n".join("- " + m.content for m in hits)
                pkg_ref, _ = self.prepare_package(
                    item_id + "-r%d" % p.get("attempt", 1), route,
                    p["title"] + "\n" + retry_brief + "\n补充材料：\n" + extra)
                retry_brief += "（context 已重建：%s）\n" % pkg_ref
            result = self._complete_with_backoff(route, [
                {"role": "system", "content": PersonaHeader},
                {"role": "user", "content": retry_brief}], node_id=p["node"])
            self.cp.record_token_usage(
                p["node"], result.usage.input_tokens, result.usage.output_tokens,
                result.usage.cache_read_tokens, result.usage.cache_write_tokens,
                result.usage.cost_usd)
            self._submissions[item_id] = result.text
            self.cp.submit(item_id, p["node"], result.text)
            verdict = self._ask_lead_review(p["title"], result.text)
        if record_outcome is None:
            self.cp.escalate(item_id, "review-loop-exhausted")
            record_outcome = "escalated"
        self._record_delegation(p, record_outcome, bucket)
        outcome["items"].append({"item": item_id, "outcome": record_outcome,
                                 "submission": submission[:200]})


    def _extract_memory(self, item_id: str, package_id: str, submission: str) -> None:
        """§4 结案五步第 4 步：从已验收证据提取 candidate memory，经显式
        promotion check 晋升 durable；未过检查走 reject_candidate（原文留
        evidence，不进默认检索 §5.7）。抽取失败不回滚验收（后继正确性依赖
        evidence/package，不等待记忆生成 §4）。

        hash 口径：artifact_hash = 未截断 submission 全文的 sha256 前 16 位
        （provenance 指向 evidence 原文，§5.6）；content 超长截断为前 4000
        字符并置 content_truncated=True——截断副本与全文 hash 口径不同属
        预期，靠标志区分。memory_* 事件经 MemoryService 的 sink 直通控制面
        （单套账本，不再手工重录）。
        """
        if self.memory is None:
            return
        entry = self.memory.add_candidate(
            content=submission[:4000], scope="root",
            source_ids=[item_id, package_id],
            artifact_hash=hashlib.sha256(submission.encode("utf-8")).hexdigest()[:16],
            accepted_by=self.cp.root_lead_node,
            content_truncated=len(submission) > 4000)
        if self.promotion_check(entry):
            self.memory.promote(entry.memory_id)
        else:
            self.memory.reject_candidate(entry.memory_id)

    @staticmethod
    def promotion_check(entry) -> bool:
        """§5.7 promotion check（V1 判据）：candidate 必须带验收 Lead 身份
        （durable memory 只收已验收内容，§4/§5.6）。"""
        return bool(entry.accepted_by)

    def _ask_lead_review(self, title: str, submission: str) -> Dict[str, Any]:
        prompt = (
            f"审查子任务交付。任务：{title}\n交付：\n{submission[:2000]}\n\n"
            '输出 JSON：{"verdict": "accept" | "reject", "verdict_reason": "...", '
            '"attribution": "capability|context|description|contradiction"}')
        result = self._complete_with_backoff(self.lead_route, [
            {"role": "system", "content": PersonaHeader + "你是验收 Lead。"},
            {"role": "user", "content": prompt},
        ], node_id=self.cp.root_lead_node)
        self.lead_tokens += result.usage.total_tokens()
        # §4 全账：验收调用同样是 Lead 消耗，token 事件一笔不漏（对照 _ask_lead）
        self.cp.record_token_usage(
            self.cp.root_lead_node, result.usage.input_tokens, result.usage.output_tokens,
            result.usage.cache_read_tokens, result.usage.cache_write_tokens,
            result.usage.cost_usd)
        try:
            decision = parse_lead_decision(result.text)
        except ValueError:
            # 验收制保守默认：Lead 输出不可解析 = 不通过（不安全放行），
            # 归因按描述问题处理走修描述重试。
            return {"verdict": "reject", "verdict_reason": f"unparseable review: {result.text[:80]}",
                    "attribution": "description"}
        if "verdict" not in decision and "action" in decision:
            decision["verdict"] = decision["action"]
        return decision

    def _record_delegation(self, p: Dict[str, Any], outcome: str, bucket: str) -> None:
        route = self._worker_routes.get(p["item"])
        item = self.cp.proj.work_items.get(p["item"])
        # §4/§7：拓扑标注优先取执行层显式标注（split-primary 等分裂结构必须
        # 在观测中标注），缺省才按 item kind 推导 derive/fission worker。
        topology = p.get("topology") or {"derive": "derive-worker",
                                         "fission": "fission-worker"}.get(
            item.kind.value if item else "", "derive-worker")
        stop = next((e.payload.get("stop_reason") for e in reversed(self.cp.store.read_all())
                     if e.kind == "stop_reason_recorded"
                     and e.payload.get("node_id") == p["node"]), None)
        rec = DelegationRecord(
            record_id=new_id("rec"), item_id=p["item"], node_id=p["node"],
            lead_node_id=self.cp.root_lead_node,
            route={"provider": route.provider, "model": route.model,
                   "level": route.level.value, "source": route.source.value} if route else {},
            topology=topology, team=item.team if item else "root",
            attempt=item.attempt if item else 1,
            stop_reason=stop, outcome=outcome,
            attribution=p.get("attribution"),   # 末次打回归因透传（§4/§8 免费归因数据）
            accepted_by={"node": self.cp.root_lead_node,
                         "route": f"{self.lead_route.provider}/{self.lead_route.model}",
                         "level": self.lead_route.level.value}
            if outcome == "accepted" else None,
            rejected_by={"node": self.cp.root_lead_node,
                         "route": f"{self.lead_route.provider}/{self.lead_route.model}",
                         "level": self.lead_route.level.value}
            if outcome in ("rejected", "rejected-exhausted") else None,
        )
        self.cp.record_observation(rec)
        # §4 闭环 V1：只攒不用——每次委派喂画像（无裁决类终局在 ProfileStore 内排除）
        if self.profile is not None and route is not None:
            self.profile.record_attempt(
                f"{route.provider}/{route.model}", bucket, outcome,
                attribution=None, stop_reason=stop)


def _attribution(verdict: Dict[str, Any], cp: Optional[ControlPlane] = None,
                 node_id: Optional[str] = None) -> RejectAttribution:
    """打回归因归一（§8 四分支）：缺省/非法值回退 DESCRIPTION——与验收保守
    默认（_ask_lead_review 不可解析 → description）对齐；capability 是最贵
    路径，不作兜底。回退记审计事件（可观测、可归责）。"""
    raw = verdict.get("attribution")
    try:
        return RejectAttribution(raw)
    except ValueError:
        if cp is not None:
            cp._record("watchdog_suggested", {
                "node_id": node_id, "kind": "attribution-fallback",
                "note": f"attribution 缺省/非法（{raw!r}），回退 description（§8）"})
        return RejectAttribution.DESCRIPTION


def _parse_subtask(entry: Any, fallback_title: str) -> Dict[str, Any]:
    """subtasks 条目归一（§7 裂变 = DAG 协调）：字符串或
    {"title": ..., "deps": [0 基下标]} → {"title": str, "deps": [int]}。"""
    if isinstance(entry, dict):
        title = str(entry.get("title") or entry.get("task") or fallback_title)
        deps = [d for d in (entry.get("deps") or []) if isinstance(d, int)]
        return {"title": title, "deps": deps}
    return {"title": str(entry), "deps": []}
