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
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .atomic import extract_atomic_facts
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
    SealPhase,
    StopReason,
    new_id,
)

PersonaHeader = "# DPswarm worker\n# 稳定内容在前（persona），任务特定在后（prompt）——§5.3 排列顺序。\n"

#: 借鉴项⑤：上下文策略职责矩阵（fresh/fork 显式化）。每个 LLM 调用面声明它的
#: 上下文来源策略，prompt 构造与审计按此表对账——防两类静默偏差：无条件继承
#: 全部历史（旧假设污染）、独立验收者被实现者的推理过程污染。
#: - fresh+X：干净上下文 + 指定材料（实现者的推理历史不进入）
#: - selective-inherit：只继承已验收的上游交付（三层交接），非全部历史
CONTEXT_POLICIES = {
    "worker-first": "fresh+package",          # 新 item：干净上下文 + 装配包
    "worker-retry": "fresh+failure-evidence",  # 返工：干净上下文 + 打回证据
    "worker-handoff": "selective-inherit",     # 下游：仅注入上游已验收交付
    "lead-review": "fresh+materials",          # 逐 item 验收：干净 + 保真材料
    "lead-integration": "fresh+registry",      # 集成验收：干净 + 原文 + 需求登记
    "lead-decision": "fresh+state",            # Lead 决策：干净 + 状态摘要
}


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
    #: 角色级 max_tokens 下限（§4 传输纪律）：thinking 模型 CoT 计入输出上限，
    #: 下限过低会被 CoT 吃满 → worker 空交付/裁决 JSON 截断（lg_compare_20260910
    #: r1 实证）。worker 交付面 32768，Lead 决策/验收面 16384。
    WORKER_MIN_TOKENS = 32768
    LEAD_MIN_TOKENS = 16384
    #: 截断阶梯的安全上限（无 max_output 目录事实时兜底防无限翻倍）。
    MAX_TOKENS_LADDER_STEPS = 4

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
        #: 并行面（worker fan-out / LG 并行验收）下的共享可变状态保护：
        #: lead_tokens 是读-改-写计数。控制面事件链自有 _lock，不在此列。
        #: prepare_package 的 compress_fn 曾需属性注入锁——已改为 assemble()
        #: 按次传参（per-call 作用域），该锁退役。
        self._lead_tokens_lock = threading.Lock()
        self._worker_routes: Dict[str, ModelRoute] = {}
        self._packages: Dict[str, Dict[str, str]] = {}   # item_id -> {content, ref, inline}
        self._submissions: Dict[str, str] = {}           # item_id -> worker 交付文本
        self._cm_costs: List[Dict[str, Any]] = []        # CM 记账（记触发方账下 §5.8/§7）
        self._integration_gaps: Optional[List[str]] = None   # 集成验收未过的缺口（债①）
        self._integration_item_count = 0                     # 挂 gaps 时的 item 数（防重验循环）
        self._integration_statuses: Dict[str, str] = {}      # 逐需求 verdict（partial 判定用）
        self._requirements: List[Dict[str, Any]] = []        # 需求登记表（债①-b ③）
        self._declared_covers: set = set()                   # mandatory 覆盖只增不减（铁律）
        self._contract_version = ""                          # 任务文本 hash（候选装配用）
        self._finalized: Optional[Dict[str, Any]] = None     # finalize 幂等缓存（④）
        self._mandatory_snapshot: Dict[str, bool] = {}       # O6 登记时快照
        self._weakening_recorded: set = set()                # O6 冲突事件去重
        self._contract_revision = 0                          # O3 合同修正 revision
        self._integration_contract_revision = 0              # 上次集成验收时的 revision
        self._amendments_used = 0                            # O3 修正预算（≤2/run）
        self._fetch_spent = 0                                # O4 回读预算（字符）
        self._finalized_contract = ""                        # 缓存所属任务的合同版本
        #: 借鉴项③：下游晋级时锚定的上游消费版本 {item_id: {upstream_id: version}}
        self._consumed_versions: Dict[str, Dict[str, str]] = {}

    def _all_settled(self) -> Optional[bool]:
        """收束硬规则（§7 终局纪律；r4 实证：活已干完 Lead 仍倾向再 fission）。

        返回 None = 未收束（继续问 Lead）；True = 非 root item 全部终态且至少
        一个 accepted → 直接走 accept 收尾（不再消耗 Lead 决策调用）；False =
        全部终态但无一 accepted → 不虚报验收，走上交收口。"""
        root = self.cp._root_item_id()
        items = [i for i in self.cp.proj.work_items.values() if i.item_id != root]
        if not items or not all(i.acceptance in TERMINAL_ACCEPTANCE for i in items):
            return None
        return any(i.acceptance == AcceptanceState.ACCEPTED for i in items)

    def _dominant_abandon_reason(self) -> Optional[str]:
        """有弃置终局的原因透传（§4 观测口径）：非 root item 全部因同一耗尽型
        原因（quota/rate-limit）上交时，run 终局沿用该原因而非泛化 escalated。"""
        root = self.cp._root_item_id()
        reasons = [e.payload.get("reason") for e in self.cp.store.read_all()
                   if e.kind == "work_item_escalated"
                   and e.payload.get("item_id") != root]
        if (reasons and len(set(reasons)) == 1
                and reasons[0] in ("quota-exhausted", "rate-limit-exhausted")):
            return reasons[0]
        return None

    #: O3 finding 处置预算：每次验收 ≤4 条、每 run ≤2 次合同修正
    #: （防"模型不断发明新需求永不结束"）。
    FINDINGS_PER_REVIEW_CAP = 4
    AMENDMENTS_PER_RUN_CAP = 2
    #: O4 Reviewer 回查预算（字符/run）与单次回查窗口。
    FETCH_BUDGET_CHARS = 65536
    FETCH_MAX_ROUNDS = 2
    FETCH_WINDOW = 4000

    def _process_findings(self, verdict: Dict[str, Any]) -> List[str]:
        """O3 清单外 finding 处置流（不只落账）。

        finding 实体：{text（原文依据）, contract_version, candidate_version,
        reviewer（验收来源）}。三分支：
        - id 命中或文本互含 → id-correction（校正匹配到既有登记条目）；
        - 原文确有但登记漏 → contract-amendment（覆盖缺口 + revision 递增 +
          该需求待重验；未受影响项证据按规则复用——status 不动）；修正预算
          耗尽 → 阻塞项（不静默吞）；
        - 原文无据 → suggestion（记建议，永不变 mandatory，不阻塞）。
        返回阻塞项清单：存在未处置必需发现时调用方不得维持 success。"""
        blocking: List[str] = []
        findings = [f for f in (verdict.get("findings") or []) if isinstance(f, dict)]
        for f in findings[: self.FINDINGS_PER_REVIEW_CAP]:
            text = str(f.get("text") or f.get("evidence") or "")[:400]
            fid = str(f.get("id") or "").strip()
            base = {"finding": text, "contract_version": self._contract_version,
                    "contract_revision": self._contract_revision,
                    "reviewer": f.get("reviewer", "integration-lead")}
            req = next((r for r in self._requirements
                        if r["requirement_id"] == fid), None)
            if req is None and fid:
                # ID 写错校正：文本与既有登记条目互含即对齐（有限：首个命中）
                req = next((r for r in self._requirements
                            if text and (text in r["text"] or r["text"] in text)),
                           None)
            if req is not None:
                self.cp._record("finding_disposition", {
                    **base, "branch": "id-correction",
                    "matched": req["requirement_id"]})
                continue
            if text and text in self._task:
                # 原文确有但登记漏 → 受控合同修正
                if self._amendments_used >= self.AMENDMENTS_PER_RUN_CAP:
                    self.cp._record("finding_disposition", {
                        **base, "branch": "amendment-budget-exhausted"})
                    blocking.append("合同修正预算耗尽，finding 未处置：%s" % text[:80])
                    continue
                self._amendments_used += 1
                self._contract_revision += 1
                new_req = {
                    "requirement_id": "req-amend-%d" % self._amendments_used,
                    "origin_ref": "reviewer-finding",
                    "text": text,
                    "contract_version": self._contract_version,
                    "mandatory": True,
                    "assigned_to": [],
                    "verification_rule": "lead-review",
                    "revision": self._contract_revision,
                }
                self._requirements.append(new_req)
                self._mandatory_snapshot[new_req["requirement_id"]] = True
                self.cp._record("contract_amended", {
                    "requirement_id": new_req["requirement_id"],
                    "revision": self._contract_revision, "text": text[:200]})
                self.cp._record("finding_disposition", {
                    **base, "branch": "contract-amendment",
                    "matched": new_req["requirement_id"]})
                blocking.append("合同修正待重验：%s" % text[:80])
                continue
            # Reviewer 自新增（原文无据）→ 建议，永不变 mandatory
            self.cp._record("finding_disposition", {
                **base, "branch": "suggestion", "mandatory": False})
        return blocking

    def _fetch_parse_ref(self, ref: str) -> Tuple[Optional[str], Optional[str]]:
        """ref 解析：pkg://<item>/<digest> / 裸 item_id / submission_package_id
        → (item_id, ref_digest)。禁止静默 latest：digest 缺失时返回 None digest
        由调用方按"无版本锚定"处理。"""
        m = re.match(r"^pkg://([\w\-]+)/([0-9a-f]{6,})$", ref or "")
        if m:
            return m.group(1), m.group(2)
        if ref and re.match(r"^[\w\-]+$", ref):
            if ref in self.cp.proj.work_items:
                return ref, None
            for iid, item in self.cp.proj.work_items.items():
                if item.submission_package_id == ref:
                    return iid, None
        return None, None

    def _review_fetch(self, ref: str, offset: int = 0,
                      limit: int = FETCH_WINDOW, *,
                      requester: str,
                      sink: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """O4 Reviewer 只读回查通道：按 ref 取候选交付**对应版本**的原始材料，
        范围读取 + 续读，返回版本/范围/完整性信息。

        纪律：旧 ref 不静默解析成 latest（digest 不符 → version-mismatch 不出
        内容）；越权/不存在/版本失配/预算不足 → error 分支（调用方记
        unknown）；按任务范围逐请求校验（requester 须为 root Lead，ref 须属
        本 run 投影）。sink：借鉴项①——每次成功取料追加一条记录，由调用方
        随 verdict 落 review_evidence（verdict↔实际看过的材料绑定）。"""
        if requester != self.cp.root_lead_node:
            return {"error": "forbidden", "ref": ref}
        item_id, ref_digest = self._fetch_parse_ref(ref)
        if item_id is None:
            return {"error": "not-found", "ref": ref}
        text = self._submissions.get(item_id)
        if text is None:
            pkg = self._packages.get(item_id)
            text = pkg.get("content") if pkg else None
        if text is None:
            return {"error": "not-found", "ref": ref}
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        if ref_digest and not digest.startswith(ref_digest):
            # 版本失配：不出内容（不静默给 latest），只报当前版本号
            return {"error": "version-mismatch", "ref": ref,
                    "current_version": digest}
        if self._fetch_spent + limit > self.FETCH_BUDGET_CHARS:
            return {"error": "fetch-budget-exhausted", "ref": ref}
        offset = max(0, int(offset))
        slice_ = text[offset: offset + limit]
        self._fetch_spent += len(slice_)
        end = offset + len(slice_)
        rec = {"ref": ref, "requester": requester, "version": digest,
               "range": [offset, end], "complete": end >= len(text)}
        self.cp._record("review_fetch", rec)
        if sink is not None:
            sink.append(rec)
        return {"content": slice_, "version": digest, "range": [offset, end],
                "complete": end >= len(text), "total": len(text)}

    @staticmethod
    def _extract_fetch(text: str) -> Optional[Dict[str, Any]]:
        """验收输出里的回查指令：一行 `FETCH: <ref>` 或 `FETCH: <ref> from=N`。"""
        for line in (text or "").splitlines():
            line = line.strip()
            if line.upper().startswith("FETCH:"):
                rest = line[6:].strip()
                m = re.search(r"\s+from=(\d+)", rest)
                offset = int(m.group(1)) if m else 0
                ref = rest[:m.start()].strip() if m else rest
                return {"ref": ref, "offset": offset} if ref else None
        return None

    def _close_stop_reason(self) -> str:
        """收口 stop_reason（②）：任一非 root item 因预算耗尽上交 →
        budget_exhausted（⑤ partial 可交付子集 + 停止原因并载）；否则
        completed。quota/rate-limit 耗尽由 autoclose 失败路径原因透传。"""
        root = self.cp._root_item_id()
        for e in self.cp.store.read_all():
            if (e.kind == "work_item_escalated"
                    and e.payload.get("item_id") != root
                    and str(e.payload.get("reason", "")).startswith("budget-exhausted")):
                return "budget_exhausted"
        return "completed"

    def _try_finalize(self, outcome: Dict[str, Any], task: str, *,
                      from_lead: bool) -> bool:
        """收束尝试（债①-b：finalize 三维度 + 逐需求验证门 + 候选装配）。

        返回 True = 本轮已收口；False = 验证未过，Lead 续决策（gaps 落审计并
        注入下轮决策 prompt）。

        语义锚点（评审契约）：
        - success = 全部 mandatory 需求在最终候选交付上验证通过（逐需求
          verdict）；探索性失败分支（被替代/废弃 item）不否决 success（③）；
        - partial = 经验证、可独立交付的有效子集 + 明确缺口清单；无验证子集
          不自动 partial（④）；
        - failed = 无契约认可交付；quota/rate-limit 耗尽原因透传；
        - unknown 阻止 success 但不等于 fail（①）；材料缺失处不得臆断（⑤）；
        - finalize 幂等：重复调用返回同一逻辑结果，不重复封存（⑧）。"""
        if (self._finalized is not None                 # ④ 幂等（合同键控：
                and self._finalized_contract ==         # 同实例只复放同一任务）
                hashlib.sha256(task.encode("utf-8")).hexdigest()[:12]):
            outcome.update(self._finalized)
            return True
        self._check_registry_integrity()   # O6 原地弱化集中校验
        root = self.cp._root_item_id()
        items = [i for i in self.cp.proj.work_items.values() if i.item_id != root]
        accepted_items = [i for i in items
                          if i.acceptance == AcceptanceState.ACCEPTED]
        if items and not accepted_items:
            if not from_lead:
                reason = self._dominant_abandon_reason()
                self.cp.escalate(root, reason or "settled-no-acceptance")
                outcome["final"] = reason or "escalated"
            else:
                outcome["final"] = "accepted-by-lead"
            # ② 停止原因透传（与 _finalize_run 回填同口径）：耗尽型终局不得
            # 泛化记 completed——quota/rate-limit 透传为 quota_exhausted /
            # rate_limit_exhausted；budget 耗尽由 _close_stop_reason 覆盖。
            stop = {"quota-exhausted": "quota_exhausted",
                    "rate-limit-exhausted": "rate_limit_exhausted"}.get(
                        outcome["final"], self._close_stop_reason())
            self._close_dimensions(outcome, "failed", stop, "skipped")
            return True
        if not items:
            # 单干/root 直做（O2）：root 验收标记是证据载体，但 success 仍需
            # 集成验收验证（拒绝空成功；合法直接交付过门 → success）
            root_item = self.cp.proj.work_items.get(root)
            outcome["final"] = outcome.get("final") or "accepted-by-lead"
            if root_item is None or root_item.acceptance != AcceptanceState.ACCEPTED:
                # 零有效交付：Lead 发 accept 也不得 success（O2 边界）
                self._close_dimensions(outcome, "failed", "completed", "skipped")
                return True
            verdict = self._ask_lead_integration(task, [root_item])
            if verdict.get("integrated") == "pass":
                candidate = self._assemble_candidate([root_item])
                self._close_dimensions(outcome, "success", "completed", "pass",
                                       candidate)
            else:
                self._close_dimensions(outcome, "failed", "completed",
                                       "fail-direct-delivery")
            return True
        if self._integration_gaps is not None:
            if (len(items) != self._integration_item_count
                    or self._contract_revision
                    != self._integration_contract_revision):
                pass   # 新增 item 或合同修正（O3）→ 重新走集成验收
            elif from_lead:
                passed = [r["requirement_id"] for r in self._requirements
                          if r["mandatory"] and
                          self._integration_statuses.get(r["requirement_id"]) == "pass"]
                # ④：有验证子集才 partial；全无 → failed（保留中间成果与缺口）
                result = "partial" if passed else "failed"
                candidate = self._assemble_candidate(accepted_items)
                outcome["final"] = "accepted-by-lead"
                self._close_dimensions(outcome, result, self._close_stop_reason(),
                                       "fail-closed-by-lead", candidate)
                return True
            else:
                # autoclose 但 gaps 在挂：不收口不重验，让 Lead 续决策
                return False
        verdict = self._ask_lead_integration(task, accepted_items)
        # 本次验收跑在 revision N 的合同上（findings 处置可能随即递增 N+1）——
        #  revision 锚记的是"验收发生时的合同"，不是处置后的
        self._integration_contract_revision = self._contract_revision
        statuses = self._req_statuses(verdict)
        mandatory = [r for r in self._requirements if r["mandatory"]]
        failed = [r["requirement_id"] for r in mandatory
                  if statuses.get(r["requirement_id"]) == "fail"]
        unknown = [r["requirement_id"] for r in mandatory
                   if statuses.get(r["requirement_id"]) == "unknown"]
        # O3：清单外 finding 处置——存在未处置必需发现（阻塞项）时不得维持
        # success；合同修正触发 revision 递增，下轮按新合同重验（旧验收不冒充
        # 新合同完整验收）
        blocking = self._process_findings(verdict)
        all_pass = (verdict.get("integrated") == "pass"
                    and not failed and not unknown and not blocking)
        if all_pass:
            # 审计对称：success 的逐需求验证证据同样落账（此前只记 fail——
            # "outcome 由 mandatory 验证决定"的通过侧证据必须在账可查）。
            self.cp._record("integration_review", {
                "verdict": "pass", "gaps": [],
                "requirements": statuses})
            candidate = self._assemble_candidate(accepted_items)
            if candidate["drift"]:
                # ⑦ 验 A 封 B：封存拒绝、版本漂移审计，降级 partial
                outcome["final"] = "accepted-by-lead"
                self._close_dimensions(outcome, "partial", self._close_stop_reason(),
                                       "version-drift", candidate)
                return True
            outcome["final"] = "accepted-by-lead"
            self._close_dimensions(outcome, "success", self._close_stop_reason(),
                                   "pass", candidate)
            return True
        gaps = [str(g)[:200] for g in (verdict.get("gaps") or [])]
        gaps += ["%s=%s" % (rid, statuses[rid]) for rid in failed + unknown]
        gaps += blocking
        self._integration_gaps = gaps[:8]
        self._integration_item_count = len(items)
        self._integration_statuses = statuses
        self.cp._record("integration_review", {
            "verdict": "fail", "gaps": self._integration_gaps,
            "requirements": statuses})
        outcome["actions"].append("integration-failed")
        return False

    def _req_statuses(self, verdict: Dict[str, Any]) -> Dict[str, str]:
        """逐需求 verdict 归一：verdict.requirements 列表在 → 未提及的登记要求
        记 unknown（登记完整性复核，①）；列表缺省 → 兼容旧口径（按 integrated
        总 verdict 赋全部）。"""
        per = {}
        for r in (verdict.get("requirements") or []):
            if isinstance(r, dict) and r.get("id"):
                per[str(r["id"])] = r
        out = {}
        default = "pass" if verdict.get("integrated") == "pass" else "fail"
        for r in self._requirements:
            entry = per.get(r["requirement_id"])
            st = str(entry.get("status")) if entry and entry.get("status") else None
            if st not in ("pass", "fail", "unknown"):
                st = None
            out[r["requirement_id"]] = st or ("unknown" if per else default)
        return out

    def _assemble_candidate(self, accepted_items: List[Any]) -> Dict[str, Any]:
        """④ 候选交付装配：finalize 前把验收过的交付钉成不可变候选
        {candidate_id, contract_version, selected_submission_package_ids,
        artifact_manifest, bundle_digest}。

        封存原子核对（⑦）：item 当前 submission_package ≠ 验收时 package →
        验 A 封 B 漂移，漂移项剔除候选并记 candidate_version_mismatch 审计。"""
        accepted_pkg = {}
        for e in self.cp.store.read_all():
            if e.kind == "work_item_accepted":
                accepted_pkg[e.payload.get("item_id")] = e.payload.get("package_id")
        selected, drift = [], []
        stale = set(self._dependency_drift(accepted_items))   # ③ 证据过期检测
        for i in accepted_items:
            if i.item_id in stale:
                drift.append(i.item_id)
                continue
            cur = i.submission_package_id
            ap = accepted_pkg.get(i.item_id)
            if ap and cur and cur != ap:
                drift.append(i.item_id)
                continue
            selected.append(str(cur))
        digest = hashlib.sha256(
            "\n".join(sorted(selected)).encode("utf-8")).hexdigest()[:16]
        candidate = {"candidate_id": new_id("cand"),
                     "contract_version": self._contract_version,
                     "selected_submission_package_ids": selected,
                     "artifact_manifest": list(selected),
                     "bundle_digest": digest, "drift": drift}
        self.cp._record("candidate_assembled", {
            "candidate_id": candidate["candidate_id"], "bundle_digest": digest,
            "selected": selected})
        if drift:
            self.cp._record("candidate_version_mismatch", {
                "candidate_id": candidate["candidate_id"], "drifted_items": drift})
        return candidate

    def _close_from_evidence(self, outcome: Dict[str, Any], stop: str) -> str:
        """O1 异常/回填路径的证据驱动判定（不调 LLM，复用持久化证据）。

        - 全部 mandatory 已有持久化 pass 记录 → success（候选装配照走）；
        - ≥1 mandatory pass → partial（可验证子集 + 缺口清单）；
        - 否则 failed（证据不足；保留 unknown 状态与真实 stop_reason——
          failed 语义 = 本次未完成可验证交付，不宣称中间产物皆错）。"""
        if self._finalized is not None:
            outcome.update(self._finalized)
            return outcome.get("outcome", "failed")
        root = self.cp._root_item_id()
        items = [i for i in self.cp.proj.work_items.values() if i.item_id != root]
        accepted_items = [i for i in items
                          if i.acceptance == AcceptanceState.ACCEPTED]
        mandatory = [r for r in self._requirements if r["mandatory"]]
        passed = [r["requirement_id"] for r in mandatory
                  if self._integration_statuses.get(r["requirement_id"]) == "pass"]
        if mandatory and len(passed) == len(mandatory):
            candidate = self._assemble_candidate(accepted_items)
            result = "partial" if candidate["drift"] else "success"
            self._close_dimensions(outcome, result, stop,
                                   "version-drift" if candidate["drift"]
                                   else "evidence-reused", candidate)
        elif passed:
            candidate = self._assemble_candidate(accepted_items)
            self._close_dimensions(outcome, "partial", stop,
                                   "evidence-partial", candidate)
        else:
            self._close_dimensions(outcome, "failed", stop,
                                   "insufficient-evidence")
        return outcome["outcome"]

    def _close_dimensions(self, outcome: Dict[str, Any], result: str,
                          stop: str, integration: str,
                          candidate: Optional[Dict[str, Any]] = None) -> str:
        """② 三维度分存：phase/outcome/stop_reason 同步进 outcome 与
        run_finalized 事件；final 路由值不动；result 为 outcome 的兼容别名。
        ④ 幂等：首次收口后缓存逻辑结果，重复 finalize 原样命中。"""
        root = self.cp._root_item_id()
        items = [i for i in self.cp.proj.work_items.values() if i.item_id != root]
        accepted = sum(1 for i in items if i.acceptance == AcceptanceState.ACCEPTED)
        outcome["phase"] = "finalized"
        outcome["outcome"] = result
        outcome["result"] = result          # r6 兼容别名
        outcome["stop_reason"] = stop
        # 终局路径必须带路由标签：轮次耗尽等路径从不设置 final（此前留 None，
        # 消费者读到"无结论"）——统一在此兜底，不覆盖已设置的路径语义。
        if outcome.get("final") is None:
            outcome["final"] = ("turn-exhausted" if stop == "attempts_exhausted"
                                else "aborted")
        if candidate is not None:
            outcome["candidate"] = candidate
        payload = {"result": result, "phase": "finalized", "stop_reason": stop,
                   "final": outcome.get("final"),
                   "contract_version": self._contract_version,
                   "accepted": accepted, "total": len(items),
                   "integration": integration}
        if candidate is not None:
            payload["candidate_id"] = candidate["candidate_id"]
            payload["bundle_digest"] = candidate["bundle_digest"]
        self.cp._record("run_finalized", payload)
        self._finalized = {"final": outcome.get("final"), "phase": "finalized",
                           "outcome": result, "result": result,
                           "stop_reason": stop}
        self._finalized_contract = self._contract_version
        if candidate is not None:
            self._finalized["candidate"] = candidate   # ⑧ 幂等含候选
        return result

    def _resume_finalized(self, outcome: Dict[str, Any], task: str) -> bool:
        """⑧ 跨进程幂等：store 已有本 run 的 run_finalized（崩溃/响应丢失后
        在同一事件日志上重跑 run_task）→ 回填已落账的三维度与候选标识直接
        收口，不重复集成验收 / 候选装配 / run_finalized 落账。进程内幂等仍由
        _finalized 缓存承载（_try_finalize 顶部）；此处把它水合到事件层。

        合同键控：仅当 run_finalized 的 contract_version 与本次任务文本一致
        才命中——同一 store 上的不同任务不劫持（新旧任务各走各的终局）。"""
        contract = hashlib.sha256(task.encode("utf-8")).hexdigest()[:12]
        if self._finalized is not None and self._finalized_contract == contract:
            outcome.update(self._finalized)
            return True
        fin = next((e for e in self.cp.store.read_all()
                    if e.kind == "run_finalized"), None)
        if fin is None:
            return False
        if fin.payload.get("contract_version") != contract:
            return False
        cached: Dict[str, Any] = {"phase": "finalized",
                                  "outcome": fin.payload.get("result"),
                                  "result": fin.payload.get("result"),
                                  "stop_reason": fin.payload.get("stop_reason")}
        if fin.payload.get("final"):
            cached["final"] = fin.payload["final"]
        if fin.payload.get("candidate_id"):
            cached["candidate"] = {
                "candidate_id": fin.payload["candidate_id"],
                "bundle_digest": fin.payload.get("bundle_digest")}
        self._finalized = cached
        self._finalized_contract = contract
        outcome.update(cached)
        outcome["actions"].append("already-finalized")
        return True

    #: JSON 协议修复轮数：模型输出散文/markdown 而非 JSON 时，做一次只重排版、
    #: 不重新推理的纠正性追问（仍失败才走保守路径）。截断类失败已由阶梯覆盖。
    JSON_REPAIR_ROUNDS = 1

    def _json_repair(self, messages: List[Dict[str, str]], bad_text: str,
                     *, node_id: str, surface: str,
                     keys=("action", "verdict")) -> Dict[str, Any]:
        """JSON 协议修复：把模型刚才的非 JSON 输出原样回贴，要求只改排版不改
        判断；token 记账口径与主调用一致（同 surface）。仍失败抛 ValueError。"""
        cur = messages + [
            {"role": "assistant", "content": bad_text},
            {"role": "user", "content":
                "你的上一次输出不是合法 JSON 对象。保持刚才的判断内容不变，"
                "只改排版：输出一个 JSON 对象，不要多余文本、不要 markdown 围栏。"},
        ]
        for _ in range(self.JSON_REPAIR_ROUNDS):
            result = self._complete_ladder(self.lead_route, cur,
                                           node_id=node_id, surface=surface)
            with self._lead_tokens_lock:
                self.lead_tokens += result.usage.total_tokens()
            self.cp.record_token_usage(
                self.cp.root_lead_node, result.usage.input_tokens,
                result.usage.output_tokens, result.usage.cache_read_tokens,
                result.usage.cache_write_tokens, result.usage.cost_usd)
            try:
                return parse_lead_decision(result.text, keys=keys)
            except ValueError:
                continue
        raise ValueError("json repair failed")

    def _ask_lead_integration(self, task: str,
                              accepted_items: List[Any]) -> Dict[str, Any]:
        """父任务级集成验收（债① §4 验收制的任务级一层）：逐需求核对——
        每条登记要求是否有已验收交付支撑、交付之间是否一致、清单与父任务
        原文是否相符（登记完整性复核，以原文为准）。

        ⑤ 保真：交付材料经 _review_material（全文优先，超窗口 L1 逐字段 +
        ref + unknown 明示）；token 记 Lead 账；调用面接截断阶梯
        （surface=lead-integration）。输出不可解析 → 保守 fail。"""
        parts = [
            "父任务级集成验收：对需求清单逐条 verdict（pass/fail/unknown），"
            "并核对交付之间一致性（引用/数值/约束矛盾即 fail）；材料不足判 "
            "unknown（unknown 阻止 success 但不等于 fail）；清单与父任务原文"
            "不符时以原文为准。\n",
            "## 父任务全文\n" + task,
            "## 需求登记清单（逐条 verdict；origin_ref=原文行号）\n"
            + ("\n".join("%s（%s，%s）%s" % (
                r["requirement_id"], r["origin_ref"],
                "mandatory" if r["mandatory"] else "optional",
                r["text"]) for r in self._requirements)
               or "（无登记条目：按父任务全文整体核对）"),
            "## 已验收交付（保真材料）",
        ]
        for i in accepted_items:
            parts.append("### [%s]\n%s"
                         % (i.item_id,
                            self._review_material(i.item_id,
                                                  self._submissions.get(i.item_id, ""))))
        parts.append("材料超出预览窗口时可输出一行 `FETCH: <ref>` 或 "
                     "`FETCH: <ref> from=<偏移>` 经只读通道回查原文（按版本核对）；"
                     "回查失败则相关需求记 unknown，不得凭预览判 pass。")
        parts.append('输出 JSON：{"integrated": "pass" | "fail", '
                     '"requirements": [{"id": "req-0", "status": '
                     '"pass"|"fail"|"unknown", "note": "..."}], '
                     '"gaps": ["缺口描述", ...]}')
        messages = [
            {"role": "system", "content": PersonaHeader + "你是集成验收 Lead。"},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        result = None
        fetch_log: List[Dict[str, Any]] = []   # 借鉴项①：本次验收实际取料清单
        # O4 回查循环（与逐 item 验收同口径：只读通道 + 版本核对 + 有界轮数）——
        # 集成验收是任务级判定的那道门，材料不可达时同样必须有取料通道，
        # 否则"决定性缺陷在预览之外"只对逐 item 验收成立、对最终判决不成立。
        for _round in range(self.FETCH_MAX_ROUNDS + 1):
            result = self._complete_ladder(self.lead_route, messages,
                                           node_id=self.cp.root_lead_node,
                                           surface="lead-integration")
            with self._lead_tokens_lock:
                self.lead_tokens += result.usage.total_tokens()
            self.cp.record_token_usage(
                self.cp.root_lead_node, result.usage.input_tokens, result.usage.output_tokens,
                result.usage.cache_read_tokens, result.usage.cache_write_tokens,
                result.usage.cost_usd)
            fetch = self._extract_fetch(result.text)
            if fetch is None:
                break
            fetched = self._review_fetch(fetch["ref"], fetch["offset"],
                                         requester=self.cp.root_lead_node,
                                         sink=fetch_log)
            if "content" in fetched:
                note = ("回查结果（版本 %s，范围 %s，complete=%s）：\n%s"
                        % (fetched["version"], fetched["range"],
                           fetched["complete"], fetched["content"]))
            else:
                note = ("回查失败（%s，ref=%s）：相关材料不可见——涉及该材料的"
                        "需求记 unknown，不得凭预览判 pass。"
                        % (fetched.get("error"), fetched.get("ref")))
            messages = messages + [
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": note},
            ]
        # 借鉴项①：verdict↔实际输入清单——判决绑定它实际看过的材料版本与范围、
        # 合同版本/修订、模型面；不复读原文，只记不可变引用。
        def _bind_evidence(verdict: Dict[str, Any]) -> Dict[str, Any]:
            self.cp._record("review_evidence", {
                "surface": "lead-integration",
                "verdict": verdict.get("integrated"),
                "context_policy": CONTEXT_POLICIES["lead-integration"],
                "contract_version": self._contract_version,
                "contract_revision": self._contract_revision,
                "accepted_item_ids": [i.item_id for i in accepted_items],
                "materials": fetch_log,
                "route": "%s/%s" % (self.lead_route.provider, self.lead_route.model),
            })
            return verdict
        try:
            return _bind_evidence(parse_lead_decision(result.text, keys=("integrated",)))
        except ValueError:
            # 协议鲁棒性：先做一次只重排版的修复追问（markdown/散文输出是
            # flash 级模型的常见模式，内容本身可能有效）；仍失败才保守 fail。
            try:
                return _bind_evidence(self._json_repair(
                    messages, result.text, node_id=self.cp.root_lead_node,
                    surface="lead-integration", keys=("integrated",)))
            except ValueError:
                return _bind_evidence({"integrated": "fail",
                                       "gaps": [f"unparseable integration review: {result.text[:80]}"]})

    def _closure_warning(self, actions: List[Optional[str]]) -> Optional[str]:
        """收束警示：已有 accepted 交付且 Lead 连续 ≥2 轮只做扩张动作
        （derive/fission）时，注入决策 prompt（不硬改其选择权，只给事实）。"""
        trailing = 0
        for a in reversed([x for x in actions if x]):
            if a in ("derive", "fission"):
                trailing += 1
            else:
                break
        if trailing < 2:
            return None
        accepted = sum(1 for i in self.cp.proj.work_items.values()
                       if i.acceptance == AcceptanceState.ACCEPTED)
        if not accepted:
            return None
        return ("\n\n收束警示：已有 %d 项交付验收通过，而你已连续 %d 轮只做扩张"
                "（derive/fission）。全部工作的交付均已验收时请决策 accept 收束；"
                "仅在确有未覆盖的需求时才继续扩张。" % (accepted, trailing))

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
            # §7 CM 成本记触发方账下：用捕获 item_id 的 lambda 包 compress_fn。
            # 借鉴项②：按次传参（per-call 作用域），不再注入/还原共享属性——
            # 并发下无串号窗口，压缩调用（含 LLM）不再被 _assemble_lock 串行化。
            pkg = self.assembler.assemble(
                brief, route, heterogeneous=heterogeneous,
                compress_fn=(lambda materials, b: self._cm_compress(materials, b, item_id))
                if self.context_manager is not None else None)
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

    def _init_task_registry(self, task: str) -> None:
        """run 开工：需求登记 + 契约版本（债①-b ③；两臂共用）。"""
        self._task = task   # O3 finding 处置的原文比对基线
        self._requirements = build_requirement_registry(task)
        self._contract_version = hashlib.sha256(task.encode("utf-8")).hexdigest()[:12]
        # O6：mandatory 快照——原地弱化（ID 不变、true→false/措辞软化）
        # 的判定基准；校验集中在 _check_registry_integrity
        self._mandatory_snapshot = {r["requirement_id"]: r["mandatory"]
                                    for r in self._requirements}
        if self._requirements:
            self.cp._record("requirements_registered", {
                "count": len(self._requirements),
                "contract_version": self._contract_version,
                "ids": [r["requirement_id"] for r in self._requirements]})

    def _check_registry_integrity(self) -> None:
        """O6 原地弱化集中校验：requirement ID 不变而 mandatory true→false 或
        contract_version 漂移 → 记 mandate_weakened（每个需求只记一次，
        不重复记冲突事件、无副作用）。"""
        for r in self._requirements:
            rid = r["requirement_id"]
            was = self._mandatory_snapshot.get(rid)
            weakened = (was is True and not r["mandatory"]) or (
                was is not None and r["contract_version"] != self._contract_version)
            if weakened and rid not in self._weakening_recorded:
                self._weakening_recorded.add(rid)
                self.cp._record("mandate_weakened", {
                    "requirement_id": rid, "was_mandatory": was,
                    "now_mandatory": r["mandatory"]})

    def run_task(self, task: str) -> Dict[str, Any]:
        """主入口：默认单 agent（§7），Lead 在理解任务后决策拓扑，全程可生长/收缩。"""
        bucket = match_bucket(task)
        spec = self.cp.proj.spec
        done: List[str] = []
        pending: List[Dict[str, Any]] = []
        outcome = {"task": task, "bucket": bucket, "items": [], "actions": []}
        if self._resume_finalized(outcome, task):
            # ⑧ 跨进程幂等重入：本 run 已收口——只补终局闭环（seal 幂等续走）
            self._finalize_run(outcome)
            return outcome
        self._init_task_registry(task)

        try:
            for _turn in range(self.max_turns):
                # 收束硬规则（§7 终局纪律）：非 root item 全部终态 → 不再问 Lead；
                # 集成验收 gaps 在挂且 item 数未变时不自动收口——让 Lead 续决策
                gaps_fresh = (self._integration_gaps is not None
                              and len([i for i in self.cp.proj.work_items.values()
                                       if i.item_id != self.cp._root_item_id()])
                              == self._integration_item_count)
                settled = self._all_settled()
                if settled is not None and not gaps_fresh:
                    # 收束硬规则 + finalize 三值 + 集成验收门（债①）
                    if self._try_finalize(outcome, task, from_lead=False):
                        outcome["actions"].append("settled-autoclose")
                        break
                    continue   # 集成验收未过：Lead 续决策（gaps 已注入下轮 prompt）
                prompt = build_lead_prompt(
                    self.catalog, spec.max_active_node_points,
                    self.cp.proj.active_points, task, done,
                    [p["title"] for p in pending], spec.max_team_workers, bucket)
                warning = self._closure_warning(outcome["actions"])
                if warning:
                    prompt += warning
                if self._integration_gaps:
                    prompt += ("\n\n上一轮集成验收未通过，缺口：\n"
                               + "\n".join("- " + g for g in self._integration_gaps)
                               + "\n可 fission 修复缺口项后收束；确认无法修复时可再次 "
                                 "accept 收束（将落 partial）。")
                decision = self._ask_lead(prompt)
                # 归一：验收裁决文本（verdict 键）在决策循环里语义等同 accept/reject
                action = decision.get("action") or (
                    "accept" if decision.get("verdict") == "accept" else None)
                outcome["actions"].append(action)

                if action == "single":
                    self._run_single(task, bucket, outcome)
                    # O2：直接交付同样过集成验收门（合法直接交付 → success；
                    # 零有效交付不得 success）
                    self._try_finalize(outcome, task, from_lead=True)
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
                    self._lead_degenerate_all(outcome)
                    break
                if action == "accept":
                    # Lead 收束同样过 finalize + 集成验收门（债①）
                    if self._try_finalize(outcome, task, from_lead=True):
                        break
                    continue   # 集成验收未过：Lead 续决策
                if action == "escalate":
                    self._lead_escalate_all(outcome)
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
        # P1-6 终局闭环与盈亏线接线（seal/tick/economics）收敛在 _finalize_run。
        self._finalize_run(outcome)
        return outcome

    def _lead_escalate_all(self, outcome: Dict[str, Any],
                           reason: str = "lead-decision") -> None:
        """任务上交（§8）：全部非终态 item 转 escalated——含未提交项
        （(None, ESCALATED) 合法，§5.7）；已结案项跳过。"""
        for item in list(self.cp.proj.work_items.values()):
            if item.acceptance not in TERMINAL_ACCEPTANCE:
                self.cp.escalate(item.item_id, reason)
        outcome["final"] = "escalated"

    def _lead_degenerate_all(self, outcome: Dict[str, Any]) -> None:
        """退化是精髓（§7）：可逆收缩——结论落盘、主 agent 只收摘要。
        遍历投影中全部非终态 item（outcome["items"] 只含已结案项，
        deps-stuck 悬挂项不在其中、会漏收）；协助者先走退化收回（§7）。"""
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

    def _finalize_run(self, outcome: Dict[str, Any]) -> None:
        """run_task 收尾：终局闭环 + tick + 盈亏线（§6）。

        P1-6 终局闭环：Lead 裁定整树验收通过 → 封存三段式收尾（root item
        accepted + 全树 drain + root lease 归还，见 finish_seal）。单干路径
        root 已 accepted，finish_seal 会跳过重复终局。幂等重入
        （_resume_finalized）时相位已终态 → 跳过封存，不留伪 seal_error。"""
        if (outcome.get("final") == "accepted-by-lead"
                and self.cp.proj.seal_phase.get("root", SealPhase.OPEN)
                not in (SealPhase.COMPLETED, SealPhase.TIMED_OUT)):
            try:
                self.cp.begin_seal("root")
                self.cp.begin_settlement("root")
                self.cp.finish_seal("root")
            except ControlPlaneError as e:
                outcome["seal_error"] = f"{e.code}: {e}"
        # O1 统一证据驱动判定：single/degenerate/escalate/quota/轮次耗尽等
        # 不过集成门的终局路径与主路径同一个 outcome 判定逻辑（逐需求验证
        # 状态）；异常只改 stop_reason，不降低交付标准；已有持久化证据复用，
        # 不为异常路径强制再调 LLM；证据不足 → failed + 真实 stop_reason。
        if "outcome" not in outcome:
            final = outcome.get("final")
            stop = {"quota-exhausted": "quota_exhausted",
                    "rate-limit-exhausted": "rate_limit_exhausted",
                    "degenerated": "cancelled"}.get(final)
            if stop is None:
                stop = "completed" if final else "attempts_exhausted"
            self._close_from_evidence(outcome, stop)
        self.cp.tick()
        # §6 盈亏线节省侧接线（V1 代理口径）：estimated_savings = worker 节点
        # token 合计，作"若由 Lead 直做"的消耗估算——同构前缀近似（Lead 直做
        # 需读入等量材料），不含 Lead/worker 的单价与能力差；观测侧只读事件流。
        # 幂等重入（_resume_finalized）不重复落账。
        if not any(e.kind == "delegation_economics_recorded"
                   and e.payload.get("item_id") == self.cp._root_item_id()
                   for e in self.cp.store.read_all()):
            worker_tokens = ObservationSink(
                self.cp.store.read_all()).economics_summary()["worker_tokens"]
            self.cp.record_economics(self.cp._root_item_id(), self.lead_tokens,
                                     worker_tokens)

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
        result = self._complete_ladder(self.lead_route, [
            {"role": "system", "content": PersonaHeader + "你是调度 Lead。"},
            {"role": "user", "content": prompt},
        ], node_id=self.cp.root_lead_node, surface="lead-decision")
        with self._lead_tokens_lock:
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
        """保持单干：root work item 直接由 root lead 完成（§7 默认态）。

        交付面与 worker 同构：截断阶梯同样生效（surface=single——半截交付
        不得直接封存为 root 终局交付）。"""
        root_item = self.cp._root_item_id()
        result = self._complete_ladder(self.lead_route, [
            {"role": "user", "content": f"直接完成任务：\n{task}"}],
            node_id=self.cp.root_lead_node, surface="single")
        with self._lead_tokens_lock:
            self.lead_tokens += result.usage.total_tokens()
        self.cp.record_token_usage(
            self.cp.root_lead_node, result.usage.input_tokens, result.usage.output_tokens,
            result.usage.cache_read_tokens, result.usage.cache_write_tokens,
            result.usage.cost_usd)
        self.cp.record_stop_reason(self.cp.root_lead_node, result.stop_reason.value)
        self._submissions[root_item] = result.text   # O2：root 直做交付入证据池
        self.cp.submit(root_item, self.cp.root_lead_node, result.text)
        pkg_id = self.cp.proj.work_items[root_item].submission_package_id
        ev = self.cp.accept_submission(
            root_item, package_id=pkg_id,
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
            entries = [{"title": task, "deps": [], "covers": []}]
        # §2 硬准入：subtasks 超限不静默截断——结构化拒绝，由 Lead 重选。
        if len(entries) > spec.max_team_workers:
            outcome["actions"].append(
                "admission-rejected:SUBTASKS_OVER_LIMIT(max=%d)" % spec.max_team_workers)
            return []
        # 债①需求覆盖映射 + ③ 三层之分配层：Lead 显式拆 subtasks 且任务有
        # 登记要求时，covers 并集必须覆盖全部要求；有遗漏 → 不建队，记
        # coverage_gap 审计 + coverage-rejected 动作，Lead 下轮重选。
        # 铁律：mandatory 覆盖集合只增不减——重声明时丢掉已覆盖的 mandatory
        # 项即违规拒绝。修复轮豁免：已有 accepted 交付时未覆盖要求视为已被
        # 在账交付满足（r6 实证误伤）。
        if subtasks and self._requirements:
            reqs = self._requirements
            has_accepted = any(i.acceptance == AcceptanceState.ACCEPTED
                               for i in self.cp.proj.work_items.values())
            if not has_accepted:
                covered = set()
                for e in entries:
                    covered.update(c for c in e["covers"] if 0 <= c < len(reqs))
                # 覆盖门的判定域 = 真实要求行；heuristic 标记项（O5 未处理范围）
                # 不参与 covers 分配（它们由集成验收 verdict 兜底，不参与派发）
                gated = [i for i, r in enumerate(reqs)
                         if r["origin_ref"] != "heuristic"]
                missing = [i for i in gated if i not in covered]
                mandatory_idx = {i for i in gated if reqs[i]["mandatory"]}
                weakened = sorted(i for i in (self._declared_covers & mandatory_idx)
                                  if i not in covered)
                if missing or weakened:
                    self.cp._record("coverage_gap", {"missing": missing,
                                                     "total": len(reqs),
                                                     "weakened": weakened})
                    outcome["actions"].append(
                        "coverage-rejected:missing=%s" % missing
                        + (";weakened=%s" % weakened if weakened else ""))
                    return []
                self._declared_covers |= covered
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
                             "title": entry["title"], "attempt": 1,
                             "origin": task})
        # 分配层回填：covers → registry.assigned_to（③ 三层之分配层落账）
        for idx, entry in enumerate(entries):
            iid = item_ids[idx] if idx < len(item_ids) else None
            if iid is None:
                continue
            for c in entry["covers"]:
                if 0 <= c < len(self._requirements):
                    self._requirements[c]["assigned_to"].append(iid)
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

    def _worker_task_text(self, p: Dict[str, Any]) -> str:
        """worker 装配包的任务文本 = 子任务标题 + 父任务全文（§5.3 投递契约）。

        r3 实证：Lead 自拟的 subtask 标题只是摘要，父任务内联材料（数据/
        契约全文）不下发 → worker 缺输入、验收不可核验。父任务全文随包下发
        （超 inline 上限时 §5.3 引用投递机制原样生效）。"""
        origin = p.get("origin") or ""
        title = p["title"]
        if origin and origin != title and origin not in title:
            return title + "\n\n## 原始任务全文（含全部材料，唯一行为依据）\n" + origin
        return title

    def _record_input_consumption(self, item_id: str) -> None:
        """借鉴项③（按输入版本解锁）：下游晋级时锚定它消费的上游版本。

        解锁口径从"上游 item 到达 accepted"收紧为"上游**当前版本**到达 accepted"：
        记录 {upstream_id: submission_package_id（缺省回退交付文本 digest）}。
        之后上游若被返工出新版本，候选装配时按此锚定检测证据过期。"""
        item = self.cp.proj.work_items.get(item_id)
        if item is None or not item.deps:
            return
        consumed: Dict[str, str] = {}
        for up_id in item.deps:
            up = self.cp.proj.work_items.get(up_id)
            if up is None:
                continue
            version = up.submission_package_id
            if not version:
                text = self._submissions.get(up_id, "")
                version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else ""
            consumed[up_id] = str(version)
        if consumed:
            self._consumed_versions[item_id] = consumed
            self.cp._record("input_versions_consumed", {
                "item_id": item_id, "consumed": consumed,
                "context_policy": CONTEXT_POLICIES["worker-handoff"]})

    def _dependency_drift(self, accepted_items: List[Any]) -> List[str]:
        """借鉴项③（版本失效传播）：消费锚定与上游当前版本不一致 → 该下游的
        验收证据过期（上游返工改了它依赖的东西），不得进入最终候选。"""
        stale: List[str] = []
        for i in accepted_items:
            consumed = self._consumed_versions.get(i.item_id) or {}
            for up_id, consumed_version in consumed.items():
                up = self.cp.proj.work_items.get(up_id)
                current = up.submission_package_id if up else None
                if current != consumed_version:
                    stale.append(i.item_id)
                    self.cp._record("dependency_version_drift", {
                        "item_id": i.item_id, "upstream": up_id,
                        "consumed_version": consumed_version,
                        "current_version": str(current)})
                    break
        return stale

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
            self._record_input_consumption(p["item"])   # ③ 解锁即锚定消费版本
            pkg_ref, pkg_hash = self.prepare_package(
                p["item"], route, self._worker_task_text(p))
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


    def _model_max_output(self, route) -> Optional[int]:
        """模型最大输出上限（catalog 事实，max_output=None = 不 clamp）。"""
        facts = self.catalog.resolve(route.provider, route.model)
        return getattr(facts, "max_output", None) if facts is not None else None

    def _token_budget_for(self, route, node_id: str,
                          base: Optional[int] = None) -> int:
        """角色级 max_tokens 下限 + 模型上限 clamp（§4 传输纪律）。"""
        floor = (self.LEAD_MIN_TOKENS if node_id == self.cp.root_lead_node
                 else self.WORKER_MIN_TOKENS)
        value = max(base or 0, floor)
        cap = self._model_max_output(route)
        return min(value, cap) if cap else value

    def _complete_with_backoff(self, route, messages, node_id, max_tokens=None):
        """§4 通用传输包装：全部 provider.complete 调用面统一走此门。

        - max_tokens 经角色级下限与模型 max_output clamp（_token_budget_for）；
          子类覆写本方法时应接受同名 kwarg 并透传（截断阶梯依赖此通道）。
        - RATE_LIMIT 是背压信号——按 1s/2s/4s 退避重试最多 3 次（retry_after
          优先、单次封顶 8s），仍失败则上抛；不进 failure audit / 画像。
        - QUOTA（余额/鉴权耗尽）不是背压——记 stop_reason aborted + 运维审计
          事件 + profile quota=True 后上抛，由上层做明确终止/上交（不重试）。"""
        budget = self._token_budget_for(route, node_id, max_tokens)
        for attempt in range(len(self.RATE_LIMIT_BACKOFF_SECONDS) + 1):
            try:
                return self.provider.complete(route, messages, max_tokens=budget)
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

    def _complete_ladder(self, route, messages, node_id, surface: str = "worker"):
        """截断阶梯重试（§4 全账口径不变），worker 交付面与 Lead 协议面共用。

        stop_reason=max-tokens → max_tokens 翻倍重试（不超过模型 max_output；
        无目录事实时以 MAX_TOKENS_LADDER_STEPS 兜底）。阶梯内重试不消耗
        attempt/pull 预算（截断是传输层问题，不是交付质量问题）；每次升档记
        max_tokens_escalated 审计事件（from/to + surface：worker /
        lead-decision / lead-review）。阶梯耗尽则带截断结果返回，走正常
        submit/review 流程。多轮用量合并进单次 ProviderResult 返回（阶梯
        每次调用都是真实消耗，一笔不漏）。
        """
        budget = self._token_budget_for(route, node_id, None)
        cap = self._model_max_output(route)
        total = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0}
        for step in range(self.MAX_TOKENS_LADDER_STEPS + 2):
            result = self._complete_with_backoff(route, messages, node_id,
                                                 max_tokens=budget)
            total["in"] += result.usage.input_tokens
            total["out"] += result.usage.output_tokens
            total["cr"] += result.usage.cache_read_tokens
            total["cw"] += result.usage.cache_write_tokens
            total["cost"] += result.usage.cost_usd
            if result.stop_reason != StopReason.MAX_TOKENS:
                break
            nxt = min(budget * 2, cap) if cap else budget * 2
            if nxt <= budget or step >= self.MAX_TOKENS_LADDER_STEPS:
                break   # 阶梯耗尽/到顶：截断结果原样上行
            self.cp._record("max_tokens_escalated", {
                "node_id": node_id, "from": budget, "to": nxt, "surface": surface})
            budget = nxt
        result.usage.input_tokens = total["in"]
        result.usage.output_tokens = total["out"]
        result.usage.cache_read_tokens = total["cr"]
        result.usage.cache_write_tokens = total["cw"]
        result.usage.cost_usd = total["cost"]
        return result

    def _complete_worker_ladder(self, route, messages, node_id):
        """worker 交付调用面（surface=worker；语义见 _complete_ladder）。"""
        return self._complete_ladder(route, messages, node_id, surface="worker")

    def _run_worker(self, p: Dict[str, Any], bucket: str) -> None:
        """worker 执行（§5.3/§5.4 投递契约 + §4 截断阶梯）：
        - inline 小包正文进 prompt；大包只传不可变引用（正文在 artifact store）
        - 截断阶梯：stop_reason=max-tokens 时 max_tokens 翻倍重试（不耗
          attempt/pull 预算，记 max_tokens_escalated 审计事件）
        - pull 兜底：worker 输出 `PULL: <query>` 行时先回查本 run 上游原文、
          再经 Memory Service 补料再来一轮（裁剪是初筛，任务全程可修正，§5.4）"""
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
            result = self._complete_worker_ladder(route, messages, node_id)
            total_in += result.usage.input_tokens
            total_out += result.usage.output_tokens
            total_cr += result.usage.cache_read_tokens
            total_cw += result.usage.cache_write_tokens
            cost += result.usage.cost_usd
            text, stop = result.text, result.stop_reason
            pull = self._extract_pull(text)
            if pull is None:
                break
            # §5.4 兜底：先查本 run 已验收/已提交的上游原文（L0 原文层回查，
            # 逐字内容以此为准），再经 Memory Service 补料；两者皆空才收尾。
            # 活 worker 的正常运行机制，与 §5.8 禁止的 successor 拉
            # predecessor 增量无关（这里是 lead 编排层显式供给）。
            extra = self._pull_respond(pull, node_id=node_id, item_id=item_id)
            if extra is None:
                break
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

    def _pull_respond(self, query: str, node_id: str = "",
                      item_id: str = "") -> Optional[str]:
        """PULL 兜底应答（§5.4）：上游原文优先，Memory Service 其次。

        query 命中本 run 内已有交付的 item 标识（item_id 或 pkg://<item> 引用）
        时，逐字返回该上游 submission 原文（_submissions 全量持有）；否则走
        Memory Service 检索。皆无命中返回 None（结束 pull 循环）。应答记
        pull_served 审计事件（source: upstream/memory/none，实跑可观测）。

        快照迭代：worker 并行面下 _submissions 可能被并发写入（他 worker 的
        submit），裸迭代有 dict 伸缩 RuntimeError 风险。"""
        for key, text in list(self._submissions.items()):
            if key in query or f"pkg://{key}" in query:
                self.cp._record("pull_served", {"node_id": node_id,
                                                "item_id": item_id,
                                                "query": query[:200],
                                                "source": "upstream"})
                return f"上游交付原文 [{key}]（逐字，未经压缩）：\n{text}"
        if self.memory is not None:
            hits = self.memory.retrieve(scope="root", query=query, limit=3)
            self.cp._record("pull_served", {"node_id": node_id, "item_id": item_id,
                                            "query": query[:200], "source": "memory"})
            return "\n".join(f"- {m.content}" for m in hits) or "（无命中材料）"
        self.cp._record("pull_served", {"node_id": node_id, "item_id": item_id,
                                        "query": query[:200], "source": "none"})
        return None

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
        # 交付面同构走截断阶梯（surface=worker）：半截回报不得拼进主执行者包。
        a_result = self._complete_worker_ladder(route, [
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

    def _review(self, p: Dict[str, Any], bucket: str, outcome: Dict[str, Any]) -> str:
        """Lead 验收（§4 奖励信号：Lead 验收制——通过=成功、打回=失败）。

        返回结案 outcome 字符串（accepted/escalated/reweight-wait 等），供
        并行验收编排（orchestrator_lg）回收每 item 终局；串行调用方可忽略。

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
            return "escalated"
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
            return "escalated"
        submission = self._submissions.get(item_id, "")
        upstream = self._upstream_evidence(item_id)   # deps 验收可见性（§4）
        verdict = self._ask_lead_review(p["title"], submission,
                                        upstream_evidence=upstream,
                                        item_id=item_id)
        record_outcome = None
        for _attempt in range(self.cp.proj.spec.max_attempts):
            if verdict.get("verdict") == "accept":
                package_id = self.cp.proj.work_items[item_id].submission_package_id
                self.cp.accept_submission(
                    item_id, package_id=package_id,
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
            # 换模型（capability）：升级上限 = Lead 级别；同构目录无升级目标时
            # 改道修述重试（r2-r4 实证：直接上交会把 capability 归因短路成
            # 不耗重试预算的终局——attribution_remapped 审计事件落账）。
            failed_route = self._worker_routes[item_id]  # 本次交付的实际路由（升级前先取）
            new_route = None
            if disposition == "upgrade-model-and-retry":
                current = failed_route
                upgrade = recommend_upgrade(
                    self.catalog, current, self.lead_route.level, bucket)
                if upgrade is None:
                    self.cp._record("attribution_remapped", {
                        "item_id": item_id, "node_id": p["node"],
                        "from": attribution.value,
                        "to": RejectAttribution.DESCRIPTION.value,
                        "reason": "homogeneous-catalog"})
                    attribution = RejectAttribution.DESCRIPTION
                    disposition = escalation_path(attribution)   # 修述重试（§8）
                else:
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
                              verdict.get("verdict_reason", ""),
                              self._worker_task_text(p)))
            if disposition == "fix-context-and-retry":
                extra = ""
                if self.memory is not None:
                    hits = self.memory.retrieve(scope="root", query=p["title"], limit=3)
                    extra = "\n".join("- " + m.content for m in hits)
                pkg_ref, _ = self.prepare_package(
                    item_id + "-r%d" % p.get("attempt", 1), route,
                    self._worker_task_text(p) + "\n" + retry_brief
                    + "\n补充材料：\n" + extra)
                retry_brief += "（context 已重建：%s）\n" % pkg_ref
            result = self._complete_worker_ladder(route, [
                {"role": "system", "content": PersonaHeader},
                {"role": "user", "content": retry_brief}], node_id=p["node"])
            self.cp.record_token_usage(
                p["node"], result.usage.input_tokens, result.usage.output_tokens,
                result.usage.cache_read_tokens, result.usage.cache_write_tokens,
                result.usage.cost_usd)
            self._submissions[item_id] = result.text
            self.cp.submit(item_id, p["node"], result.text)
            verdict = self._ask_lead_review(p["title"], result.text,
                                            upstream_evidence=upstream,
                                            item_id=item_id)
        if record_outcome is None:
            self.cp.escalate(item_id, "review-loop-exhausted")
            record_outcome = "escalated"
        self._record_delegation(p, record_outcome, bucket)
        outcome["items"].append({"item": item_id, "outcome": record_outcome,
                                 "submission": submission[:200]})
        return record_outcome


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

    def _upstream_evidence(self, item_id: str) -> str:
        """验收可见性（§4 验收制）：item 有 deps 时，把上游 accepted 交付逐字
        带给验收 prompt——r3 实证：classic 臂 Lead 看不到上游交付，逐字约束
        物理上不可核验导致盲放。逐字截断引用（每份 ≤4000 字符、总量 ≤8000，
        超出部分以 package ref 指代），不另造摘要层（§5.6 分层不变）。"""
        item = self.cp.proj.work_items.get(item_id)
        if item is None or not item.deps:
            return ""
        parts: List[str] = []
        total = 0
        for up_id in item.deps:
            up = self.cp.proj.work_items.get(up_id)
            text = self._submissions.get(up_id, "")
            if up is None or up.acceptance != AcceptanceState.ACCEPTED or not text:
                continue
            keep = min(len(text), 4000, 8000 - total)
            if keep <= 0:
                break
            note = "" if keep == len(text) else "…（截断，全文见 submission_package=%s）" % (
                up.submission_package_id)
            parts.append("### 上游已验收交付 [%s]（逐字，供核验依赖约束）\n%s%s"
                         % (up_id, text[:keep], note))
            total += keep
        return "\n\n".join(parts)

    #: 验收材料窗口（债①-b ⑤）：submission 全文 ≤ 此值原样进验收；超出给
    #: L1 逐字段 + 全文 ref + unknown 明示（取消 2000 硬截断作唯一材料）。
    REVIEW_MATERIAL_LIMIT = 12000

    def _review_material(self, item_id: str, submission: str) -> str:
        """验收材料保真：完整 submission 优先；超窗口给 L1 逐字段提取 +
        全文 ref + 「材料缺失处不得臆断，记 unknown」明示。"""
        if len(submission) <= self.REVIEW_MATERIAL_LIMIT:
            return submission
        facts = extract_atomic_facts(submission, cap=8000)
        ref = ""
        item = self.cp.proj.work_items.get(item_id)
        if item is not None and item.submission_package_id:
            ref = str(item.submission_package_id)
        return ("（交付正文超验收窗口（>%d 字符）：以下为 L1 逐字段提取，"
                "全文见 submission_package=%s；材料缺失处不得臆断，记 unknown）\n\n%s"
                % (self.REVIEW_MATERIAL_LIMIT, ref, facts))

    def _ask_lead_review(self, title: str, submission: str,
                       upstream_evidence: str = "", item_id: str = "") -> Dict[str, Any]:
        material = (self._review_material(item_id, submission) if item_id
                    else submission[: self.REVIEW_MATERIAL_LIMIT])
        prompt = (
            f"审查子任务交付。任务：{title}\n交付：\n{material}\n\n"
            + (upstream_evidence + "\n\n" if upstream_evidence else "")
            + "验收规则：交付内容必须是任务的实质产出；若交付是「缺少材料/"
            "无法完成」式的阻塞报告，或依赖约束相对上游交付不成立（如要求"
            "逐字引用但内容不一致），应 reject 并按 context/description 归因，"
            "不得 accept 阻塞报告；证据不足以判定时 attribution 记 uncertain。"
            "材料超出预览窗口时可输出一行 `FETCH: <ref>` 或 "
            "`FETCH: <ref> from=<偏移>` 经只读通道回查原文（按版本核对）；"
            "回查失败则相关验收项记 uncertain/unknown，不得凭预览放行。\n"
            '输出 JSON：{"verdict": "accept" | "reject", "verdict_reason": "...", '
            '"attribution": "capability|context|description|contradiction|uncertain"}')
        messages = [
            {"role": "system", "content": PersonaHeader + "你是验收 Lead。"},
            {"role": "user", "content": prompt},
        ]
        result = None
        fetch_log: List[Dict[str, Any]] = []   # 借鉴项①：本次验收实际取料清单
        # O4 回查循环：reviewer 输出 FETCH 行 → 只读通道取料 → 续审（有界 2 轮）
        for _round in range(self.FETCH_MAX_ROUNDS + 1):
            result = self._complete_ladder(self.lead_route, messages,
                                           node_id=self.cp.root_lead_node,
                                           surface="lead-review")
            with self._lead_tokens_lock:
                self.lead_tokens += result.usage.total_tokens()
            # §4 全账：验收/回查调用同样是 Lead 消耗，token 事件一笔不漏
            self.cp.record_token_usage(
                self.cp.root_lead_node, result.usage.input_tokens, result.usage.output_tokens,
                result.usage.cache_read_tokens, result.usage.cache_write_tokens,
                result.usage.cost_usd)
            fetch = self._extract_fetch(result.text)
            if fetch is None:
                break
            fetched = self._review_fetch(fetch["ref"], fetch["offset"],
                                         requester=self.cp.root_lead_node,
                                         sink=fetch_log)
            if "content" in fetched:
                note = ("回查结果（版本 %s，范围 %s，complete=%s）：\n%s"
                        % (fetched["version"], fetched["range"],
                           fetched["complete"], fetched["content"]))
            else:
                note = ("回查失败（%s，ref=%s）：相关材料不可见——涉及该材料的"
                        "验收项记 uncertain，不得凭预览放行。"
                        % (fetched.get("error"), fetched.get("ref")))
            messages = messages + [
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": note},
            ]
        # 借鉴项①：verdict↔实际输入清单——判决绑定实际看过的材料版本与范围。
        def _bind(d: Dict[str, Any]) -> Dict[str, Any]:
            if "verdict" not in d and "action" in d:
                d = {**d, "verdict": d["action"]}
            self.cp._record("review_evidence", {
                "surface": "lead-review", "verdict": d.get("verdict"),
                "item_id": item_id or None,
                "context_policy": CONTEXT_POLICIES["lead-review"],
                "contract_version": self._contract_version,
                "contract_revision": self._contract_revision,
                "materials": fetch_log,
                "route": "%s/%s" % (self.lead_route.provider, self.lead_route.model),
            })
            return d
        try:
            return _bind(parse_lead_decision(result.text))
        except ValueError:
            # 协议鲁棒性：先修复追问（markdown/散文是 flash 级常见模式）；
            # 仍失败 → 验收制保守默认（不安全放行）+ ⑥ 记 invalid_output。
            try:
                return _bind(self._json_repair(messages, result.text,
                                               node_id=self.cp.root_lead_node,
                                               surface="lead-review"))
            except ValueError:
                return _bind({"verdict": "reject", "verdict_reason": f"unparseable review: {result.text[:80]}",
                              "attribution": "invalid_output"})

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
    """打回归因归一（§8 四分支 + ⑥ 解析维度）：缺省/非法值记
    INVALID_OUTPUT（不再伪装成 description；capability 是最贵路径不作兜底），
    回退记审计事件（可观测、可归责，措辞同步解析维度）。"""
    raw = verdict.get("attribution")
    try:
        return RejectAttribution(raw)
    except ValueError:
        if cp is not None:
            cp._record("watchdog_suggested", {
                "node_id": node_id, "kind": "attribution-fallback",
                "note": f"attribution 缺省/非法（{raw!r}），记 invalid_output（⑥）"})
        return RejectAttribution.INVALID_OUTPUT


_REQ_LINE = re.compile(
    r"^(?:\d+[.、\)]|[-*•])\s*(\S.{6,200})$")   # 顶格行才收（缩进的编号多为数据清单）

_OPTIONAL_SIGNALS = ("可选", "非必须", "optional", "酌情")
_VERBATIM_RULE_SIGNALS = ("逐字", "引用", "一致", "schema", "签名")


def _extract_requirement_lines(task: str) -> List[Tuple[int, str]]:
    """顶格编号/bullet 行 → [(行号, 文本)]（缩进数据行不收；上限 12）。"""
    out = []
    for lineno, line in enumerate(task.splitlines(), start=1):
        if line != line.lstrip():
            continue
        m = _REQ_LINE.match(line)
        if m:
            out.append((lineno, m.group(1).strip()))
    return out   # O5：12 上限改批大小——全量提取，不再静默截断


def _detect_unhandled(task: str, covered_lines: set) -> List[str]:
    """O5 已知未处理范围探测：顶格规则命中不了的约束载体——嵌套/缩进列表、
    markdown 表格、含约束措辞的散文段。返回标记说明列表（每条进登记表为
    一个待验收项；存在未解决覆盖缺口不得判 success）。"""
    marks = []
    lines = task.splitlines()
    if any(l != l.lstrip() and _REQ_LINE.match(l.lstrip()) for l in lines):
        marks.append("nested-list（缩进列表项可能携带约束，未逐条登记）")
    if any(l.strip().startswith("|") for l in lines):
        marks.append("table（markdown 表格内容未逐条登记）")
    for lineno, line in enumerate(lines, start=1):
        s = line.strip()
        if lineno in covered_lines or not s or s.startswith("#"):
            continue
        if _REQ_LINE.match(line):
            continue
        if len(s) >= 20 and any(k in s for k in ("必须", "不得", "要求", "禁止", "验收")):
            marks.append("prose-constraint L%d（%s）" % (lineno, s[:60]))
    return marks


def extract_requirements(task: str) -> List[str]:
    """兼容接口：只回要求文本（下标 = covers 0 基序号）。"""
    return [text for _, text in _extract_requirement_lines(task)]


def build_requirement_registry(task: str) -> List[Dict[str, Any]]:
    """需求登记表（债①-b ③）：从"字符串列表"升级为登记记录。

    {requirement_id, origin_ref（原文行号）, contract_version（任务文本 hash）,
    mandatory（可选信号判定）, assigned_to（covers 分配回填）, verification_rule}
    mandatory 集合由代码从父任务原文推导——Lead 不得删除/弱化或改可选性
    （铁律在 _dispatch 的覆盖门执行）。"""
    version = hashlib.sha256(task.encode("utf-8")).hexdigest()[:12]
    registry = []
    lines = _extract_requirement_lines(task)
    for i, (lineno, text) in enumerate(lines):
        registry.append({
            "requirement_id": "req-%d" % i,
            "origin_ref": "L%d" % lineno,
            "text": text,
            "contract_version": version,
            "mandatory": not any(k in text for k in _OPTIONAL_SIGNALS),
            "assigned_to": [],
            "verification_rule": ("verbatim-match"
                                  if any(k in text for k in _VERBATIM_RULE_SIGNALS)
                                  else "lead-review"),
        })
    for note in _detect_unhandled(task, {n for n, _ in lines}):
        registry.append({
            "requirement_id": "req-extra-%d" % len(registry),
            "origin_ref": "heuristic",
            "text": note,
            "contract_version": version,
            "mandatory": True,   # O5：未解决覆盖缺口不得判 success
            "assigned_to": [],
            "verification_rule": "lead-review",
        })
    return registry


def _parse_subtask(entry: Any, fallback_title: str) -> Dict[str, Any]:
    """subtasks 条目归一（§7 裂变 = DAG 协调 + 债①覆盖映射）：字符串或
    {"title": ..., "deps": [0 基下标], "covers": [0 基要求序号]} →
    {"title": str, "deps": [int], "covers": [int]}。"""
    if isinstance(entry, dict):
        title = str(entry.get("title") or entry.get("task") or fallback_title)
        deps = [d for d in (entry.get("deps") or []) if isinstance(d, int)]
        covers = [c for c in (entry.get("covers") or []) if isinstance(c, int)]
        return {"title": title, "deps": deps, "covers": covers}
    return {"title": str(entry), "deps": [], "covers": []}
