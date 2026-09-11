"""DPswarm LangGraph 实验编排器：调度层迁入 StateGraph，控制面一字不动（§7 推论）。

与父类 Orchestrator 的关系：
- 复用全部控制面交互与记账路径（prepare_package / _cm_compress / _run_worker /
  _review / _ask_lead* / _record_delegation / _run_single / 收口矩阵），
  准入、账本、锁、归因语义不变——图只替代调度（fan-out、join、波次推进）。
- 修复两个结构问题：
  * P0（串行管道只解锁不传递）：deps 下游晋级时注入三层交接包——L2 CM
    摘要（导航）、L1 确定性逐字原子事实（引用依据）、L0 artifact 引用
    （PULL 原文回查）；无 CM 时 L2 降级截断，成本记 ctx-job:<下游 item_id>。
  * P1（并行执行、串行验收）：验收按 item Send fan-out 并行执行，各 item
    的打回-重试循环互不阻塞；验收 LLM 仍走 lead_route + root_lead_node
    （本版不设独立 reviewer 角色，只解决串行瓶颈）。
- 图 state 只放流程变量（action/decision/deferred/pending/executed/done/波次）；
  一切状态变更与记账只经 ControlPlane 方法落账（单写者串行链不变）。

本版边界（后续轮次）：
- split 不支持：Lead 选 split 记 unsupported-in-lg:split 后让其重选。
- checkpointer（断点恢复 ≈ 真 resume 探针）与 interrupt（澄清挂起 ≈
  ClarificationScheduler 替代）预留不接。
"""
from __future__ import annotations

import operator
import re
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Tuple

from .control import AdmissionError, ControlPlaneError
from .levels import match_bucket
from .orchestrator import Orchestrator
from .providers.base import QuotaExhausted, RateLimitBackoff
from .routing import build_lead_prompt
from .types import AcceptanceState

try:
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Send
    _LG_AVAILABLE = True
except ImportError:  # 缺依赖时 import 不炸；构造 OrchestratorLG 才报错
    END = START = StateGraph = Send = None  # type: ignore[assignment]
    _LG_AVAILABLE = False


class _LGState(Dict[str, Any]):
    """图 state（total=False 语义由 TypedDict 在运行期等价表达，见 _state_schema）。

    通道写入纪律（无 reducer 的通道每 superstep 只有一个写入方，不冲突）：
    - turn/action/decision/final：仅 lead 节点写；
    - wave/pending/deferred：仅 dispatch / promote 节点写（二者互斥于不同 step）；
    - executed/done：reducer 累加通道，worker / review 节点并行写。
    """


def _state_schema() -> Any:
    from typing_extensions import TypedDict

    class LGState(TypedDict, total=False):
        task: str
        bucket: str
        turn: int
        wave: int
        action: Optional[str]
        decision: Dict[str, Any]
        final: Optional[str]
        done: Annotated[List[str], operator.add]
        executed: Annotated[List[Dict[str, Any]], operator.add]
        deferred: List[Dict[str, Any]]
        pending: List[Dict[str, Any]]
        p: Dict[str, Any]          # Send 载荷的瞬态通道（worker/review 节点输入）

    return LGState


#: deps 交接 L2 摘要层的边界标注（v3 备忘 §C 演进：摘要不作逐字依据）
HANDOFF_TRUST_NOTE = "（摘要仅供导航，逐字内容以 L1/原文为准）"
#: 无 CM 时 L2 降级拼接的每份上游交付截断长度（与 DSH staged 相位摘要口径一致）
HANDOFF_FALLBACK_CHARS = 2000

# L1 提取与逐字判型已中立化到 atomic.py（验收材料保真 §4 侧复用同一份
# 确定性实现）；此处 re-export 保持既有引用/测试不破。
from .atomic import (  # noqa: E402
    ATOMIC_CAP as HANDOFF_ATOMIC_CAP,
    ATOMIC_CAP_VERBATIM as HANDOFF_ATOMIC_CAP_VERBATIM,
    extract_atomic_facts as _extract_atomic_facts,
    handoff_profile,
    playbook_priority as _playbook_priority,
)


def _load_playbook() -> str:
    """加载版本化 playbook 资产（dpswarm/data/handoff_playbook_v1.md）。
    缺失/损坏（读不出、为空）返回 ""——调用方降级规则判型，不炸。"""
    try:
        text = (Path(__file__).parent / "data"
                / "handoff_playbook_v1.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return text.strip()


class OrchestratorLG(Orchestrator):
    """LangGraph 版编排器（实验）。构造需 langgraph 可用（pip install dpswarm[lg]）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if not _LG_AVAILABLE:
            raise ImportError(
                "OrchestratorLG 需要 langgraph：pip install 'dpswarm[lg]' "
                "（核心包保持零依赖，lg 为可选 extra）")
        super().__init__(*args, **kwargs)
        self._outcome: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 主入口：StateGraph 主循环（§7 拓扑动作 = 图节点/边）
    # ------------------------------------------------------------------

    def run_task(self, task: str) -> Dict[str, Any]:
        """默认单 agent（§7），Lead 在理解任务后决策拓扑，全程可生长/收缩。"""
        bucket = match_bucket(task)
        self._outcome = {"task": task, "bucket": bucket, "items": [], "actions": []}
        if self._resume_finalized(self._outcome, task):
            # ⑧ 跨进程幂等重入：本 run 已收口——只补终局闭环（seal 幂等续走）
            self._finalize_run(self._outcome)
            return self._outcome
        self._init_task_registry(task)
        graph = self._build_graph()
        initial = {
            "task": task, "bucket": bucket, "turn": 0, "wave": 0,
            "action": None, "decision": {}, "final": None,
            "done": [], "executed": [], "deferred": [], "pending": [],
        }
        try:
            end = graph.invoke(initial)
            if end.get("final"):
                self._outcome["final"] = end["final"]
        except QuotaExhausted:
            # §4：QUOTA 不是背压——明确终止/上交人工；不炸穿 run_task（否则观测断账）。
            self._outcome["final"] = "quota-exhausted"
            self._abandon_open_items_for_quota()
        except RateLimitBackoff:
            # §4：429 退避有界耗尽后与 QUOTA 同构收口。
            self._outcome["final"] = "rate-limit-exhausted"
            self._abandon_open_items_for_quota("rate-limit-exhausted")
        self._finalize_run(self._outcome)
        return self._outcome

    def _build_graph(self):
        builder = StateGraph(_state_schema())
        builder.add_node("lead", self._lg_lead)
        builder.add_node("single", self._lg_single)
        builder.add_node("dispatch", self._lg_dispatch)
        builder.add_node("worker", self._lg_worker)
        builder.add_node("collect", self._lg_collect)
        builder.add_node("review", self._lg_review)
        builder.add_node("promote", self._lg_promote)
        builder.add_node("escalate", self._lg_escalate)
        builder.add_node("degenerate", self._lg_degenerate)
        builder.add_node("finalize", self._lg_finalize)

        builder.add_edge(START, "lead")
        builder.add_conditional_edges("lead", self._route_after_lead)
        builder.add_edge("single", "finalize")
        builder.add_conditional_edges("dispatch", self._fanout_workers)
        builder.add_edge("worker", "collect")
        builder.add_conditional_edges("collect", self._fanout_reviews)
        builder.add_edge("review", "promote")
        builder.add_conditional_edges("promote", self._route_after_promote)
        builder.add_edge("escalate", "finalize")
        builder.add_edge("degenerate", "finalize")
        builder.add_edge("finalize", END)
        return builder.compile()

    # ------------------------------------------------------------------
    # Lead 决策节点（机制一：决策前注入事实块）
    # ------------------------------------------------------------------

    def _lg_lead(self, state: Dict[str, Any]) -> Dict[str, Any]:
        turn = state.get("turn", 0)
        # 收束硬规则 + finalize 门（与父类同语义）；集成验收 gaps 在挂且
        # item 数未变时不自动收口——让 Lead 续决策（修复或主动收束落 partial）
        settled = self._all_settled()
        gaps_fresh = (self._integration_gaps is not None
                      and len([i for i in self.cp.proj.work_items.values()
                               if i.item_id != self.cp._root_item_id()])
                      == self._integration_item_count)
        if settled is not None and not gaps_fresh:
            if self._try_finalize(self._outcome, state["task"], from_lead=False):
                self._outcome["actions"].append("settled-autoclose")
                return {"action": None, "final": self._outcome.get("final"),
                        "turn": turn + 1}
            return {"action": None, "turn": turn + 1}
        if turn >= self.max_turns:
            return {"action": None}
        spec = self.cp.proj.spec
        prompt = build_lead_prompt(
            self.catalog, spec.max_active_node_points,
            self.cp.proj.active_points, state["task"], state.get("done", []),
            [d["title"] for d in state.get("deferred", [])],
            spec.max_team_workers, state["bucket"])
        warning = self._closure_warning(self._outcome["actions"])
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
        self._outcome["actions"].append(action)
        if action == "split":
            # 本版不支持：记录并让 Lead 重选（不静默走偏拓扑）
            self._outcome["actions"].append("unsupported-in-lg:split")
            action = None
        if action == "accept":
            # Lead 收束同样过 finalize + 集成验收门（债①）
            if self._try_finalize(self._outcome, state["task"], from_lead=True):
                return {"action": None, "final": self._outcome.get("final"),
                        "turn": turn + 1}
            return {"action": None, "turn": turn + 1}
        return {"action": action, "decision": decision, "turn": turn + 1}

    def _route_after_lead(self, state: Dict[str, Any]) -> str:
        if state.get("final"):
            return "finalize"   # 收束硬规则的直接终局（不再走动作路由）
        action = state.get("action")
        if action == "single":
            return "single"
        if action in ("derive", "fission"):
            return "dispatch"
        if action == "escalate":
            return "escalate"
        if action == "degenerate":
            return "degenerate"
        if action == "accept":
            return "finalize"
        # 未知/None/split-拒绝：轮次内让 Lead 重选；轮次耗尽由 lead 节点收口
        return "lead" if state.get("turn", 0) < self.max_turns else "finalize"

    # ------------------------------------------------------------------
    # 拓扑动作节点
    # ------------------------------------------------------------------

    def _lg_single(self, state: Dict[str, Any]) -> Dict[str, Any]:
        self._run_single(state["task"], state["bucket"], self._outcome)
        # O2：与父类同口径——直接交付也过集成验收门（合法直接交付 → success；
        # 零有效交付不得 success）
        self._try_finalize(self._outcome, state["task"], from_lead=True)
        return {"final": self._outcome.get("final")}

    def _lg_escalate(self, state: Dict[str, Any]) -> Dict[str, Any]:
        self._lead_escalate_all(self._outcome)
        return {"final": "escalated"}

    def _lg_degenerate(self, state: Dict[str, Any]) -> Dict[str, Any]:
        self._lead_degenerate_all(self._outcome)
        return {"final": "degenerated"}

    def _lg_finalize(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    # ------------------------------------------------------------------
    # 派发与波次推进（§7 裂变 = DAG 协调；fan-out 走 Send）
    # ------------------------------------------------------------------

    def _lg_dispatch(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """建 item + 接 deps 边 + 首波晋级（父类 _dispatch 原样复用，硬准入不变）。"""
        pending: List[Dict[str, Any]] = []
        deferred = self._dispatch(state["action"], state["decision"], state["task"],
                                  state["bucket"], pending, self._outcome)
        return {"deferred": deferred, "pending": pending,
                "wave": state.get("wave", 0) + 1}

    def _fanout_workers(self, state: Dict[str, Any]):
        """dispatch/promote 后的 worker fan-out：每 ready item 一个 Send（物理并行）。"""
        pending = state.get("pending") or []
        if not pending:
            return "promote"
        wave = state.get("wave", 0)
        return [Send("worker", {"p": p, "bucket": state["bucket"], "wave": wave})
                for p in pending]

    def _lg_worker(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """worker 执行（§5.3/§5.4 投递契约不变）。

        §4：QUOTA/429 逐 worker 打标（不中断其余 worker），处置在 review 节点
        按有弃置矩阵执行——与父类 _run_pending 的 future 打标语义一致。"""
        p = dict(state["p"])
        try:
            self._run_worker(p, state["bucket"])
        except QuotaExhausted:
            p["quota_exhausted"] = True
        except RateLimitBackoff:
            p["rate_limited"] = True
        p["_wave"] = state.get("wave", 0)
        return {"executed": [p]}

    def _lg_collect(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """worker fan-in 屏障（map-reduce join）：本波全部 worker 到齐后才放行。"""
        return {}

    def _fanout_reviews(self, state: Dict[str, Any]):
        """P1 修复：验收按 item Send fan-out 并行（各 item 的打回-重试互不阻塞）。

        验收 LLM 仍走 lead_route + root_lead_node——本版不设独立 reviewer
        角色，只把父类 `for p in pending: _review` 的串行循环改并行。"""
        wave = state.get("wave", 0)
        ready = [p for p in state.get("executed", []) if p.get("_wave") == wave]
        if not ready:
            return "promote"
        return [Send("review", {"p": p, "bucket": state["bucket"]}) for p in ready]

    def _lg_review(self, state: Dict[str, Any]) -> Dict[str, Any]:
        p = state["p"]
        record_outcome = self._review(p, state["bucket"], self._outcome)
        return {"done": ["%s: %s" % (p["item"], record_outcome)]}

    def _lg_promote(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """波次推进：deps 全部 accepted（item_ready，§4 解锁后继）的 deferred 项
        注入上游交接摘要（P0）→ 装配包 → 两阶段启动 → 下一波 worker。

        无进展且仍有 deferred = 依赖卡死（deps-stuck）：悬挂项留在投影中，
        交 Lead 后续决策处置（escalate/degenerate 收口矩阵不变），不死循环。"""
        deferred = [dict(d) for d in state.get("deferred", [])]
        pending: List[Dict[str, Any]] = []
        self._promote_ready(deferred, pending)
        update: Dict[str, Any] = {"deferred": deferred, "pending": pending}
        if pending:
            update["wave"] = state.get("wave", 0) + 1
        elif deferred:
            self._outcome["actions"].append("deps-stuck")
            update["deferred"] = []
        return update

    def _route_after_promote(self, state: Dict[str, Any]):
        return self._fanout_workers(state) if state.get("pending") else "lead"

    # ------------------------------------------------------------------
    # P0 修复：deps 交接摘要（CM 挂载点②）
    # ------------------------------------------------------------------

    def _promote_ready(self, deferred: List[Dict[str, Any]],
                       pending: List[Dict[str, Any]]) -> bool:
        """deferred 晋级（父类 _promote_deferred 同构 + 交接摘要注入）。

        与父类差异仅一处：装配 task_text = 子任务描述 + _handoff_section
        （上游 accepted 交付的 CM 摘要）；准入/两阶段/错误打标全部同口径。"""
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
                p["item"], route,
                self._worker_task_text(p)
                + self._handoff_section(p["item"], p["title"]))
            try:
                node = self.cp.begin_node(p["item"], route,
                                          package_ref=pkg_ref, package_hash=pkg_hash)
                self.cp.confirm_node(node.node_id)
            except (AdmissionError, ControlPlaneError) as e:
                self._outcome["actions"].append(f"admission-rejected:{e.code}")
                continue
            p["node"] = node.node_id
            pending.append(p)
        return progressed

    def _classify_handoff(self, task_text: str, item_id: str,
                          playbook: str) -> Tuple[str, str]:
        """依赖类型判型：有 CM 且 playbook 在 → CM 按 playbook 判（decider=llm，
        成本记 ctx-job:<item_id>）；否则关键词规则兜底（decider=rule）；
        CM 输出非法/异常 → 规则判型不炸（decider=rule-fallback）。"""
        if self.context_manager is not None and playbook:
            try:
                label, account = self.context_manager.classify_handoff(
                    task_text, playbook)
                if label not in ("verbatim", "semantic"):
                    raise ValueError("handoff classify label invalid: %r" % label)
                if account.get("input_tokens") or account.get("cost_usd"):
                    self.cp.record_token_usage(
                        "ctx-job:%s" % item_id,
                        input_tokens=account.get("input_tokens", 0),
                        output_tokens=account.get("output_tokens", 0),
                        cost_usd=account.get("cost_usd", 0.0))
                return label, "llm"
            except Exception:
                return handoff_profile(task_text), "rule-fallback"
        return handoff_profile(task_text), "rule"

    def _handoff_section(self, item_id: str, task_text: str = "") -> str:
        """deps 交接：上游 accepted 交付 → 下游包，三层结构（分层+符号化+可回查）。

        - 依赖类型分派（handoff_profile）：下游任务书命中逐字依赖信号
          （逐字/引用/一致/schema/签名等）→ verbatim：L1 上限放宽至
          HANDOFF_ATOMIC_CAP_VERBATIM 且 L1 排在摘要前；否则 semantic 保持
          L2 摘要在前。分派记 handoff_profile 审计事件（含 decider=
          rule/llm/rule-fallback；有 CM 且 playbook 在时由 CM 按 playbook
          判型，成本记 ctx-job:<下游 item_id>；实验归因用）。
        - L2 摘要层（导航用）：有 CM 走零损压缩（§5.2 按需唤起，成本记
          ctx-job:<下游 item_id>，§5.5/§7）；无 CM 降级截断拼接。摘要仅供
          导航，不作逐字依据（r1 实证：逐字引用型验收与 CM 摘要冲突）。
        - L1 原子事实层（逐字依据）：确定性提取 fenced 代码块/JSON 行/签名/
          表格行等，逐字拼接，按模式优先级控量——"逐字引用请以此层为准"。
        - L0 原文层（可回查）：列上游 item 标识与 submission package 引用，
          原文经 PULL 兜底逐字取回（_pull_respond，§5.4）。
        三层进同一份 _stash 落盘包（provenance 链不断）。"""
        item = self.cp.proj.work_items.get(item_id)
        if item is None:
            return ""
        upstreams = []
        for up_id in (item.deps or []):
            up = self.cp.proj.work_items.get(up_id)
            if up is not None and up.acceptance == AcceptanceState.ACCEPTED:
                text = self._submissions.get(up_id, "")
                if text:
                    upstreams.append((up_id, text))
        if not upstreams:
            return ""
        playbook = _load_playbook()
        profile, decider = self._classify_handoff(task_text, item_id, playbook)
        self.cp._record("handoff_profile", {"item_id": item_id, "profile": profile,
                                            "decider": decider})
        priority = _playbook_priority(playbook)
        # L2 摘要层（playbook §1 结构指引作为 CM 的 prompt 资产注入）
        if self.context_manager is not None:
            from .context.assembler import AssemblerBrief  # 延迟导入避免环
            brief = AssemblerBrief(
                task_intent=("相位交接摘要：为下游子任务压缩上游已验收交付，"
                             "保留决定、引用、版本号与未决问题，零新增事实。"
                             + ("\n\n交接 playbook（结构指引须遵守）：\n" + playbook
                                if playbook else "")),
                select=[], scope="team",
                token_budget=self.INLINE_TOKEN_LIMIT,
                inline_token_limit=self.INLINE_TOKEN_LIMIT)
            summary = self._cm_compress(
                [f"[{up_id}]\n{text}" for up_id, text in upstreams], brief, item_id)
        else:
            summary = "\n\n".join(
                f"[{up_id}]\n{text[:HANDOFF_FALLBACK_CHARS]}"
                for up_id, text in upstreams)
        l2 = "### L2 摘要层" + HANDOFF_TRUST_NOTE + "\n" + summary
        # L1 原子事实层（确定性提取；verbatim 上限放宽；优先级可被 playbook 覆盖）
        cap = (HANDOFF_ATOMIC_CAP_VERBATIM if profile == "verbatim"
               else HANDOFF_ATOMIC_CAP)
        l1_blocks = [_extract_atomic_facts(text, cap=cap, priority=priority)
                     for _, text in upstreams]
        l1 = None
        if any(l1_blocks):
            labeled = "\n\n".join(f"[{up_id}]\n{facts}"
                                  for (up_id, _), facts in zip(upstreams, l1_blocks)
                                  if facts)
            l1 = ("### L1 原子事实层（逐字提取；逐字引用请以此层为准）\n" + labeled)
        # L0 原文层（artifact 引用 + PULL 回查路径）
        refs = []
        for up_id, _ in upstreams:
            up = self.cp.proj.work_items[up_id]
            refs.append(
                f"- [{up_id}] submission_package={up.submission_package_id}；"
                f"逐字原文：输出一行 `PULL: {up_id}` 经 §5.4 兜底通道取回")
        l0 = ("### L0 原文层（artifact 引用，provenance 链不断）\n"
              + "\n".join(refs))
        layers = [l1, l2, l0] if profile == "verbatim" and l1 else [l2, l1, l0]
        header = ("\n\n## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）"
                  + "（handoff_profile=%s）" % profile)
        if profile == "verbatim":
            # r4 实证：verbatim 下游凭 L1 记忆复述而不取原文（pull_served 恒 0）
            # ——逐字保真最后一厘米靠显式契约（prompt 层，不改机制语义）。
            header += ("\n\n**逐字内容禁止凭记忆复述**：交付中需要逐字引用上游"
                       "内容时，必须先输出一行 `PULL: <上游 item 标识>` 取回原文"
                       "（§5.4 兜底通道，L0 层列有标识），再逐字直贴；L1 层仅供"
                       "定位预览。")
        return (header + "\n\n" + "\n\n".join(p for p in layers if p))
