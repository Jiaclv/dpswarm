"""OrchestratorLG（LangGraph 实验编排器，§7 调度层迁移）行为测试：

- 与父类同口径：fission fan-out 建 item/验收/账本事件一致（准入不变）；
- P0 修复：deps 链 A→B，下游 B 的包注入上游 accepted 交付摘要（无 CM 降级
  截断 + "不可信摘要"标注；有 CM 走零损压缩且成本记 ctx-job:<下游 item>）；
- P1 修复：验收按 item Send fan-out 并行——用线程 barrier 判定两次验收真并行
  （串行实现下 barrier 必然超时破裂，测试随之失败）；
- 打回-重试循环在并行验收内闭合：reject(description) → 修述重试 → accept。

mock 策略：MockProvider 是顺序脚本回放，并行调用下结果不确定；这里用
RoutingProvider 按消息内容路由（Lead 决策走脚本队列，worker/review 按
prompt 内容响应），任意交织下结果确定。
"""
from __future__ import annotations

import json
import re
import threading

import pytest

pytest.importorskip("langgraph")   # lg 为可选 extra；缺依赖时整个模块跳过

from dpswarm.control import ControlPlane
from dpswarm.orchestrator_lg import (
    HANDOFF_ATOMIC_CAP,
    HANDOFF_TRUST_NOTE,
    OrchestratorLG,
    _extract_atomic_facts,
)
from dpswarm.providers.base import Provider, ProviderResult, Usage
from dpswarm.types import (
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
    StopReason,
)

B_ROUTE = {"provider": "p", "model": "b-model"}


def catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    cat.register(ModelFacts("p", "a-model", Level.A, aa_dimensional={"coding": 8.5}))
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


def make_cp(tmp_path, **spec_kw):
    return ControlPlane(spec=RootExecutionSpec(
        max_open_work_items=spec_kw.pop("max_open_work_items", 4),
        max_active_node_points=spec_kw.pop("max_active_node_points", 8), **spec_kw),
        store_path=tmp_path / "events.jsonl", catalog=catalog())


class RoutingProvider(Provider):
    """内容路由的确定性 Provider：并行调用下结果与顺序无关。

    - Lead 决策（prompt 含"决策协议"）：按脚本队列逐条弹出（Lead 节点单线程，
      队列安全）；
    - 验收（prompt 含"审查子任务交付"）：review_fn(prompt) → verdict dict；
    - 其余 = worker 执行：worker_fn(prompt) → 交付文本。
    """

    name = "routing-mock"

    def __init__(self, decisions, worker_fn=None, review_fn=None):
        self._decisions = list(decisions)
        self._worker_fn = worker_fn or (lambda content: "交付：" + content[:50])
        self._review_fn = review_fn or (
            lambda content: {"verdict": "accept", "verdict_reason": "ok"})
        self.calls = []
        self._lock = threading.Lock()

    def complete(self, route, messages, tools=None, max_tokens=4096):
        user = next((m["content"] for m in reversed(messages or [])
                     if m.get("role") == "user"), "")
        with self._lock:
            self.calls.append({"user": user, "tid": threading.get_ident()})
            self.calls[-1]["worker_like"] = (
                "决策协议" not in user and "审查子任务交付" not in user
                and "父任务级集成验收" not in user)
        sysmsg = str(messages[0].get("content", "")) if messages else ""
        if "你是集成验收 Lead" in sysmsg:
            text = json.dumps({"integrated": "pass", "gaps": []}, ensure_ascii=False)
        elif "决策协议" in user:
            with self._lock:
                decision = (self._decisions.pop(0) if self._decisions
                            else {"action": "accept"})
            text = json.dumps(decision, ensure_ascii=False)
        elif "审查子任务交付" in user:
            text = json.dumps(self._review_fn(user), ensure_ascii=False)
        else:
            text = self._worker_fn(user)
        return ProviderResult(text=text, stop_reason=StopReason.COMPLETED,
                              usage=Usage(input_tokens=10, output_tokens=5))

    def worker_prompts(self):
        return [c["user"] for c in self.calls if c["worker_like"]]


def make_orch(cp, provider, **kw):
    kw.setdefault("lead_route", ModelRoute("p", "s-model", level=Level.S))
    return OrchestratorLG(cp, provider, store_dir=None, **kw)


class TestParallelFanOutAndReview:
    """§7 fission fan-out（Send 物理并行）+ P1 并行验收。"""

    def test_two_workers_and_two_reviews_run_in_parallel(self, tmp_path):
        cp = make_cp(tmp_path)
        worker_gate = threading.Barrier(2)
        review_gate = threading.Barrier(2)
        sync = {"worker_serial": False, "review_serial": False,
                "worker_tids": set(), "review_tids": set()}
        lock = threading.Lock()

        def worker_fn(content):
            with lock:
                sync["worker_tids"].add(threading.get_ident())
            try:
                worker_gate.wait(timeout=5)
            except threading.BrokenBarrierError:
                with lock:
                    sync["worker_serial"] = True
            return "交付:" + ("甲" if "任务甲" in content else "乙")

        def review_fn(content):
            with lock:
                sync["review_tids"].add(threading.get_ident())
            try:
                review_gate.wait(timeout=5)
            except threading.BrokenBarrierError:
                with lock:
                    sync["review_serial"] = True
            return {"verdict": "accept", "verdict_reason": "ok"}

        orch = make_orch(cp, RoutingProvider(
            decisions=[{"action": "fission", "route": B_ROUTE,
                        "subtasks": ["任务甲", "任务乙"]},
                       {"action": "accept"}],
            worker_fn=worker_fn, review_fn=review_fn))
        out = orch.run_task("两件并行的事")

        assert out["final"] == "accepted-by-lead"
        workers = [w for w in cp.proj.work_items.values() if w.kind.value == "fission"]
        assert len(workers) == 2
        assert all(w.acceptance.value == "accepted" for w in workers)
        # 账本：两个 fission item 各一次提交 + 一次验收（与父类同口径；
        # 封存三段式会给 root item 补 submit/accept，按 item 过滤）
        ids = {w.item_id for w in workers}
        events = cp.store.read_all()
        submitted = [e for e in events if e.kind == "work_item_submitted"
                     and e.payload["item_id"] in ids]
        accepted = [e for e in events if e.kind == "work_item_accepted"
                    and e.payload["item_id"] in ids]
        assert len(submitted) == 2 and len(accepted) == 2
        # P1 证据：worker fan-out 与验收都在并行（串行实现下 barrier 超时破裂）
        assert not sync["worker_serial"] and not sync["review_serial"]
        assert len(sync["worker_tids"]) == 2
        assert len(sync["review_tids"]) == 2


class TestDepsHandoff:
    """P0 修复：deps 链下游拿到上游 accepted 交付的三层交接包。"""

    def _decisions(self):
        return [{"action": "fission", "route": B_ROUTE,
                 "subtasks": ["任务A", {"title": "任务B", "deps": [0]}]},
                {"action": "accept"}]

    def test_downstream_package_carries_three_layers(self, tmp_path):
        cp = make_cp(tmp_path)
        provider = RoutingProvider(
            decisions=self._decisions(),
            worker_fn=lambda c: "A交付内容XYZ" if "任务A" in c else "B交付")
        orch = make_orch(cp, provider)
        out = orch.run_task("有依赖的两步任务")

        assert out["final"] == "accepted-by-lead"
        assert all(i["outcome"] == "accepted" for i in out["items"])
        # B 的 worker prompt：三层交接（无 CM 时 L2 降级截断拼接）
        b_prompt = next(p for p in provider.worker_prompts() if "任务B" in p)
        assert "上游交付交接" in b_prompt
        assert "L2 摘要层" in b_prompt and HANDOFF_TRUST_NOTE in b_prompt
        assert "A交付内容XYZ" in b_prompt                  # L2 降级含上游文本
        assert "L0 原文层" in b_prompt and "PULL: wi-" in b_prompt
        # DAG 边与调度序不变：依赖事件在，B 的 provisioning 晚于 A 的 accepted
        events = cp.store.read_all()
        deps = [e for e in events if e.kind == "work_item_dependency_added"]
        assert len(deps) == 1
        items = [w for w in cp.proj.work_items.values() if w.kind.value == "fission"]
        a = next(w for w in items if not w.deps)
        b = next(w for w in items if w.deps)
        acc_a = next(e for e in events if e.kind == "work_item_accepted"
                     and e.payload["item_id"] == a.item_id)
        prov_b = next(e for e in events if e.kind == "node_provisioning"
                      and e.payload["item_id"] == b.item_id)
        assert prov_b.seq > acc_a.seq

    def test_l1_atomic_facts_verbatim(self, tmp_path):
        """L1 层：上游交付里的 fenced 代码块逐字进下游包（确定性提取，无 LLM）。"""
        schema_block = '```json\n{"type": "object", "required": ["id"]}\n```'
        cp = make_cp(tmp_path)

        def worker_fn(content):
            if "任务A" in content:
                return "设计说明略。\n" + schema_block + "\ndef validate_record(record, schema):"
            return "B交付"

        provider = RoutingProvider(decisions=self._decisions(), worker_fn=worker_fn)
        orch = make_orch(cp, provider)
        out = orch.run_task("有依赖的两步任务")
        assert out["final"] == "accepted-by-lead"
        b_prompt = next(p for p in provider.worker_prompts() if "任务B" in p)
        assert "L1 原子事实层" in b_prompt
        assert "逐字引用请以此层为准" in b_prompt
        assert schema_block in b_prompt                          # 逐字保真
        assert "def validate_record(record, schema):" in b_prompt  # 签名行

    def test_pull_fetches_upstream_verbatim(self, tmp_path):
        """L0 回查：worker 输出 PULL: <上游 item_id> → 下一轮拿到逐字原文（§5.4）。"""
        cp = make_cp(tmp_path)
        seen = {"pull_followup": None}

        def worker_fn(content):
            if "任务A" in content:
                return "A的逐字交付内容ABC"
            if "任务B" in content and "PULL 结果" not in content:
                m = re.search(r"PULL: (wi-\w+)", content)
                return "PULL: pkg://" + m.group(1)     # 按 pkg:// 引用回查（债②）
            seen["pull_followup"] = content   # PULL 补料后的第二轮 B
            return "B基于原文完成"

        provider = RoutingProvider(decisions=self._decisions(), worker_fn=worker_fn)
        orch = make_orch(cp, provider)
        out = orch.run_task("有依赖的两步任务")
        assert out["final"] == "accepted-by-lead"
        followup = seen["pull_followup"]
        assert followup is not None
        assert "上游交付原文" in followup and "A的逐字交付内容ABC" in followup
        # pull_served 审计：source=upstream，实跑可观测（§5.4）
        served = [e for e in cp.store.read_all() if e.kind == "pull_served"]
        assert served and served[0].payload["source"] == "upstream"

    def test_handoff_via_cm_records_ctx_job(self, tmp_path):
        """有 CM：L2 走零损压缩，成本记触发方账下 ctx-job:<下游 item>（§5.5/§7）。"""

        class FakeCM:
            def compress(self, materials, brief):
                assert any("A交付内容XYZ" in m for m in materials)
                assert "相位交接摘要" in brief.task_intent
                return "压缩后的A摘要", {"input_tokens": 100, "output_tokens": 20,
                                       "cost_usd": 0.01}

        cp = make_cp(tmp_path)
        provider = RoutingProvider(
            decisions=self._decisions(),
            worker_fn=lambda c: "A交付内容XYZ" if "任务A" in c else "B交付")
        orch = make_orch(cp, provider, context_manager=FakeCM())
        out = orch.run_task("有依赖的两步任务")

        assert out["final"] == "accepted-by-lead"
        b_prompt = next(p for p in provider.worker_prompts() if "任务B" in p)
        assert "压缩后的A摘要" in b_prompt
        items = [w for w in cp.proj.work_items.values() if w.kind.value == "fission"]
        b = next(w for w in items if w.deps)
        ctx = [e for e in cp.store.read_all()
               if e.kind == "token_usage_recorded"
               and e.payload["node_id"] == "ctx-job:%s" % b.item_id]
        assert ctx and ctx[0].payload["input"] == 100


class TestOriginAndReviewVisibilityLG:
    """LG 路径锚定（继承父类语义）：父任务材料进 worker 包 + 验收可见上游。"""

    def test_origin_and_upstream_visible(self, tmp_path):
        cp = make_cp(tmp_path)
        review_prompts = []
        provider = RoutingProvider(
            decisions=[{"action": "fission", "route": B_ROUTE,
                        "subtasks": ["任务A", {"title": "任务B", "deps": [0]}]},
                       {"action": "accept"}],
            worker_fn=lambda c: "A的逐字交付@@LG" if "任务A" in c else "B交付",
            review_fn=lambda c: (review_prompts.append(c)
                                 or {"verdict": "accept", "verdict_reason": "ok"}))
        orch = make_orch(cp, provider)
        out = orch.run_task("两步任务。内联材料：LG-ORIGIN-MARKER 父任务数据。")
        assert out["final"] == "accepted-by-lead"
        b_prompt = next(p for p in provider.worker_prompts() if "任务B" in p)
        assert "LG-ORIGIN-MARKER" in b_prompt          # 父任务材料随包下发
        b_review = review_prompts[-1]                   # 第二个验收 = B
        assert "上游已验收交付" in b_review and "A的逐字交付@@LG" in b_review
        assert "不得 accept 阻塞报告" in b_review


class TestHandoffClassifierPaths:
    """判型双路径（playbook skill 化）：CM 按 playbook 判（decider=llm，账记
    ctx-job）；playbook 缺失/CM 输出非法 → 规则兜底不炸。"""

    class FakeCMAnswering:
        def __init__(self, label, expect_playbook_in_compress=True):
            self.label = label
            self.expect_playbook = expect_playbook_in_compress

        def classify_handoff(self, task_text, playbook):
            assert "判型准则" in playbook            # playbook 资产确被加载
            return self.label, {"input_tokens": 30, "output_tokens": 1,
                                "cost_usd": 0.001}

        def compress(self, materials, brief):
            if self.expect_playbook:
                assert "交接 playbook" in brief.task_intent   # §1 指引进压缩 prompt
            return "CM摘要", {"input_tokens": 50, "output_tokens": 10,
                             "cost_usd": 0.002}

    class FakeCMInvalid:
        def classify_handoff(self, task_text, playbook):
            return "both", {"input_tokens": 30, "output_tokens": 1,
                            "cost_usd": 0.0}

        def compress(self, materials, brief):
            return "CM摘要", {}

    def _run(self, tmp_path, cm, monkeypatch=None, playbook_text=None):
        if monkeypatch is not None and playbook_text is not None:
            import dpswarm.orchestrator_lg as lgmod
            monkeypatch.setattr(lgmod, "_load_playbook", lambda: playbook_text)
        cp = make_cp(tmp_path)
        provider = RoutingProvider(
            decisions=[{"action": "fission", "route": B_ROUTE,
                        "subtasks": ["任务A", {"title": "任务B：逐字引用 A 的 schema", "deps": [0]}]},
                       {"action": "accept"}],
            worker_fn=lambda c: "A交付```json\n{}\n```" if "任务A" in c else "B交付")
        orch = make_orch(cp, provider, context_manager=cm)
        out = orch.run_task("判型路径任务")
        assert out["final"] == "accepted-by-lead"
        return cp

    def test_llm_decider_overrides_rule(self, tmp_path):
        # 任务书含"逐字引用"（规则判 verbatim），CM 判 semantic → 以 CM 为准
        cp = self._run(tmp_path, self.FakeCMAnswering("semantic"))
        ev = next(e for e in cp.store.read_all() if e.kind == "handoff_profile")
        assert ev.payload["profile"] == "semantic"
        assert ev.payload["decider"] == "llm"
        items = [w for w in cp.proj.work_items.values() if w.kind.value == "fission"]
        b = next(w for w in items if w.deps)
        ctx = [e for e in cp.store.read_all() if e.kind == "token_usage_recorded"
               and e.payload["node_id"] == "ctx-job:%s" % b.item_id]
        assert any(e.payload["input"] == 30 for e in ctx)   # 判型账记触发方

    def test_invalid_llm_output_falls_back_to_rule(self, tmp_path):
        cp = self._run(tmp_path, self.FakeCMInvalid())
        ev = next(e for e in cp.store.read_all() if e.kind == "handoff_profile")
        assert ev.payload["profile"] == "verbatim"          # 规则关键词兜底
        assert ev.payload["decider"] == "rule-fallback"

    def test_broken_playbook_falls_back_to_rule(self, tmp_path, monkeypatch):
        cp = self._run(tmp_path, self.FakeCMAnswering("semantic",
                                                      expect_playbook_in_compress=False),
                       monkeypatch=monkeypatch, playbook_text="")
        ev = next(e for e in cp.store.read_all() if e.kind == "handoff_profile")
        assert ev.payload["decider"] == "rule"              # playbook 缺失 → 规则
        assert ev.payload["profile"] == "verbatim"


class TestPlaybookPriorityOverride:
    """playbook 的 l1_priority 配置覆盖默认丢弃序（提取仍是确定性代码）。"""

    def test_priority_override_reorders_drop(self):
        from dpswarm.orchestrator_lg import _playbook_priority
        playbook = "# x\nl1_priority: signature > fenced > json > misc\n"
        priority = _playbook_priority(playbook)
        assert priority == {"signature": 0, "fenced": 1, "json": 2, "misc": 3}
        fenced = "```python\n" + "z" * 3000 + "\n```"
        sig = "def important(a, b):"
        out = _extract_atomic_facts(sig + "\n" + fenced, cap=len(sig) + 10,
                                    priority=priority)
        assert sig in out                # signature 提到最高优先：保留
        assert "z" * 100 not in out      # fenced 降级后超额度被丢

    def test_no_override_keeps_default(self):
        from dpswarm.orchestrator_lg import _playbook_priority
        assert _playbook_priority("# 无配置行") is None
        assert _playbook_priority("l1_priority: bogus > unknown") is None


class TestAtomicFactsExtraction:
    """L1 提取质量（r2 真实样本审计驱动）：优先级丢弃、未闭合围栏、表格行。"""

    def test_oversize_fenced_block_truncated_not_dropped(self):
        """r2 实证回归：单个 4930 字符 fenced 块超 4000 上限时不得整块丢弃。"""
        big = "```json\n" + "{\n  \"k\": \"" + "x" * 5000 + "\"\n}\n```"
        out = _extract_atomic_facts("说明。\n" + big)
        assert out                                   # 非空（截断保留）
        assert "超上限截断" in out
        assert len(out) <= HANDOFF_ATOMIC_CAP + 40   # 截断 + 标注

    def test_priority_drop_order(self):
        """超上限按优先级丢弃：fenced(0) > JSON 行(1) > 签名(2) > 其他(3)，
        低优先先丢（不是简单按原文序截断）。"""
        fenced = "```python\ncode = '" + "y" * 3000 + "'\n```"
        json_line = '"standalone": "json line"'
        sig = "def signature_line(a, b):"
        text = ("VERSION_TOKEN v1.2.3 在前\n" + fenced + "\n"
                + json_line + "\n" + sig)
        # 额度恰好装下 P0+P1+P2：P3 版本标识被优先级丢弃（尽管它在原文最前）
        out = _extract_atomic_facts(text, cap=len(fenced) + len(json_line) + len(sig))
        assert "y" * 100 in out                      # fenced 块保留
        assert json_line in out                      # P1 保留
        assert sig in out                            # P2 保留
        assert "v1.2.3" not in out                   # P3 先被丢弃

    def test_unclosed_fence_captured(self):
        """截断交付的未闭合围栏：从开口到 EOF 收进 L1（带标注）。"""
        text = "前文。\n```python\ndef f():\n    return 1\n（此处交付被截断，无闭合围栏）"
        out = _extract_atomic_facts(text)
        assert "def f():" in out and "未闭合" in out

    def test_new_patterns_table_kv_path(self):
        """markdown 表格行 / 配置键值 / 文件路径行进网。"""
        text = ("分析如下：\n| 指标 | 值 |\n| --- | --- |\n| 错误率 | 3.2% |\n"
                "DATABASE_URL=postgres://x/y\n"
                "交付文件：\nsrc/logpipe/ingest.py\n")
        out = _extract_atomic_facts(text)
        assert "| 错误率 | 3.2% |" in out
        assert "DATABASE_URL=postgres://x/y" in out
        assert "src/logpipe/ingest.py" in out


class TestHandoffProfile:
    """依赖类型分派：verbatim 命中 → L1 前置 + 上限放宽 + handoff_profile 事件。"""

    def _run_pair(self, tmp_path, b_title):
        cp = make_cp(tmp_path)
        provider = RoutingProvider(
            decisions=[{"action": "fission", "route": B_ROUTE,
                        "subtasks": ["任务A", {"title": b_title, "deps": [0]}]},
                       {"action": "accept"}],
            worker_fn=lambda c: "A交付```json\n{\"k\": 1}\n```" if "任务A" in c else "B交付")
        orch = make_orch(cp, provider)
        out = orch.run_task("分派任务")
        return cp, provider, out

    def test_verbatim_profile_promotes_l1(self, tmp_path):
        cp, provider, out = self._run_pair(tmp_path, "任务B：逐字引用 A 的 schema 实现校验器")
        assert out["final"] == "accepted-by-lead"
        b_prompt = next(p for p in provider.worker_prompts() if "任务B" in p)
        assert "handoff_profile=verbatim" in b_prompt
        assert b_prompt.index("L1 原子事实层") < b_prompt.index("L2 摘要层")  # L1 前置
        # 债②：verbatim 直贴指令（禁止凭记忆复述，必须 PULL 取原文）
        assert "禁止凭记忆复述" in b_prompt and "PULL: <上游 item 标识>" in b_prompt
        ev = next(e for e in cp.store.read_all() if e.kind == "handoff_profile")
        assert ev.payload["profile"] == "verbatim"
        assert ev.payload["decider"] == "rule"          # 无 CM → 规则兜底

    def test_semantic_profile_keeps_summary_first(self, tmp_path):
        cp, provider, out = self._run_pair(tmp_path, "任务B：根据 A 的结论撰写报告")
        assert out["final"] == "accepted-by-lead"
        b_prompt = next(p for p in provider.worker_prompts() if "任务B" in p)
        assert "handoff_profile=semantic" in b_prompt
        assert b_prompt.index("L2 摘要层") < b_prompt.index("L1 原子事实层")  # 摘要在前
        ev = next(e for e in cp.store.read_all() if e.kind == "handoff_profile")
        assert ev.payload["profile"] == "semantic"
        assert ev.payload["decider"] == "rule"


class TestAutocloseLG:
    """债① LG 臂锚定：全树 accepted 后不再产生 Lead 决策调用。"""

    def test_no_extra_lead_decision_after_accepted(self, tmp_path):
        cp = make_cp(tmp_path)
        provider = RoutingProvider(
            decisions=[{"action": "fission", "route": B_ROUTE,
                        "subtasks": ["任务甲", "任务乙"]}],   # 不给 accept 决策
            worker_fn=lambda c: "交付")
        orch = make_orch(cp, provider)
        out = orch.run_task("收束任务")
        assert out["final"] == "accepted-by-lead"
        assert "settled-autoclose" in out["actions"]
        decision_calls = [c for c in provider.calls if "决策协议" in c["user"]]
        assert len(decision_calls) == 1              # 只有首次拓扑决策


class TestRejectRetryAccept:
    """§8 打回-重试循环在并行验收节点内闭合：reject → 修述重试 → accept。"""

    def test_reject_then_retry_then_accept(self, tmp_path):
        cp = make_cp(tmp_path)

        def worker_fn(content):
            return "修好的交付" if "打回重试" in content else "坏交付"

        def review_fn(content):
            if "坏交付" in content:
                return {"verdict": "reject", "verdict_reason": "不达标",
                        "attribution": "description"}
            return {"verdict": "accept", "verdict_reason": "ok"}

        orch = make_orch(cp, RoutingProvider(
            decisions=[{"action": "derive", "route": B_ROUTE},
                       {"action": "accept"}],
            worker_fn=worker_fn, review_fn=review_fn))
        out = orch.run_task("需要返工的任务")

        assert out["items"][0]["outcome"] == "accepted"
        rejected = next(e for e in cp.store.read_all()
                        if e.kind == "work_item_rejected")
        assert rejected.payload["attribution"] == "description"
        # 重试喂料：第二次 worker 调用带打回理由与任务全文（§5.8 只增不减）
        retries = [p for p in RoutingProvider.worker_prompts(orch.provider)
                   if "打回重试" in p]
        assert retries and "不达标" in retries[0]


class TestUnsupportedSplit:
    """split 本版不支持：记录后由 Lead 重选，不静默走偏拓扑。"""

    def test_split_marked_and_redecided(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, RoutingProvider(
            decisions=[{"action": "split", "route": B_ROUTE},
                       {"action": "single"}],
            worker_fn=lambda content: "单干交付"))
        out = orch.run_task("被误选 split 的任务")
        assert "unsupported-in-lg:split" in out["actions"]
        assert out["final"] == "single"
