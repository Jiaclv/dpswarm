"""Open 项 O3/O4/O6 + finalize 边界补验（评审决定版）：

- O3 清单外 finding 三分支处置：id-correction / contract-amendment（revision
  递增 + 阻塞待重验）/ suggestion（永不变 mandatory）；修正预算耗尽 → 阻塞
  不静默；合同修正后旧验收不得冒充新合同完整验收（revision 变化触发重验）。
- O4 Reviewer 回查通道：预览外缺陷经 FETCH 发现并拒；版本失配/越权/预算
  不足 → 不静默给 latest、相关验收项记 unknown 指引；版本不符不出内容。
- O6 原地弱化集中校验：mandatory true→false 记 mandate_weakened，每需求
  只记一次、无副作用。
- 边界①：崩溃在 finalize 中途的恢复（跨进程 _resume_finalized 从事件层
  水合，不重复集成验收/装配/落账）。
- 边界②：两执行者并发 finalize 同一 run——唯一生效逻辑终局（结果一致、
  digest 一致；LLM 计费恰好一次不在承诺范围，如实记录事件数）。
"""
from __future__ import annotations

import json
import threading

from dpswarm.control import ControlPlane
from dpswarm.orchestrator import Orchestrator
from dpswarm.providers import MockProvider
from dpswarm.providers.base import Provider, ProviderResult, Usage
from dpswarm.types import (
    DelegationKind,
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
    StopReason,
)

B_ROUTE = {"provider": "p", "model": "b-model"}
TASK2 = "两步任务：\n1. 实现交付甲并自测\n2. 实现交付乙并自测"
#: O3 修正/预算测试用：散文补充行（无约束关键词、不超长度阈值——不被
#: _detect_unhandled 标记），finding 文本是其中片段
TASK_PROSE = TASK2 + "\n\n附注：文末附一句口诀方便记忆；交付物末尾标注作者；附一页回滚指引。"


def catalog() -> ModelCatalog:
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "s-model", Level.S, aa_dimensional={"coding": 9.0}))
    cat.register(ModelFacts("p", "b-model", Level.B, aa_dimensional={"coding": 7.5}))
    return cat


def make_cp(tmp_path, name="events"):
    return ControlPlane(spec=RootExecutionSpec(max_open_work_items=6,
                                               max_active_node_points=8),
                        store_path=tmp_path / f"{name}.jsonl", catalog=catalog())


def make_orch(cp, script):
    return Orchestrator(cp, MockProvider(script=script), store_dir=None,
                        lead_route=ModelRoute("p", "s-model", level=Level.S))


def _decision(action: str, **kw) -> str:
    return json.dumps({"action": action, **kw})


def _accepted_item(cp, text="交付文本"):
    root = cp._root_item_id()
    item = cp.create_work_item(DelegationKind.DERIVE, parent_item=root,
                               deps=[], team="root")
    route = ModelRoute("p", "b-model", level=Level.B)
    node = cp.begin_node(item.item_id, route,
                         package_ref="pkg://t/abc", package_hash="abc")
    cp.confirm_node(node.node_id)
    cp.submit(item.item_id, node.node_id, text)
    pkg_id = cp.proj.work_items[item.item_id].submission_package_id
    cp.accept_submission(item.item_id, package_id=pkg_id,
                         accepted_by={"node": "test-lead"})
    return item, pkg_id


class TestO3FindingDispositions:
    """O3 清单外 finding 的完整处置流。"""

    def _finalize_with_verdict(self, tmp_path, verdict, task=TASK2):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [])   # 脚本空：不走 run_task，直调 _try_finalize
        orch._init_task_registry(task)
        self_item, _ = _accepted_item(cp)
        outcome = {"task": task, "items": [], "actions": []}
        # 桩掉集成验收 LLM：返回脚本 verdict
        orch._ask_lead_integration = lambda task_, items: verdict
        closed = orch._try_finalize(outcome, task, from_lead=True)
        return cp, orch, outcome, closed

    def test_id_correction_matches_existing_requirement(self, tmp_path):
        req0_text = "实现交付甲并自测"
        cp, orch, outcome, closed = self._finalize_with_verdict(tmp_path, {
            "integrated": "pass", "gaps": [],
            "findings": [{"id": "req-99", "text": req0_text}]})  # ID 写错
        assert closed and outcome["result"] == "success"   # 校正匹配，不阻塞
        ev = next(e for e in cp.store.read_all()
                  if e.kind == "finding_disposition")
        assert ev.payload["branch"] == "id-correction"
        assert ev.payload["matched"] == "req-0"

    def test_contract_amendment_blocks_then_reverify(self, tmp_path):
        """原文确有但登记漏 → 受控合同修正（revision 递增）+ 阻塞；revision
        变化后下一轮重验（旧验收不冒充新合同完整验收）。"""
        hidden = "文末附一句口诀方便记忆"   # 在 TASK_PROSE 原文、不在登记表
        cp, orch, outcome, closed = self._finalize_with_verdict(tmp_path, {
            "integrated": "pass", "gaps": [],
            "findings": [{"text": hidden}]}, task=TASK_PROSE)
        assert not closed                     # 未处置必需发现 → 不维持 success
        assert orch._contract_revision == 1
        kinds = [e.kind for e in cp.store.read_all()]
        assert "contract_amended" in kinds
        assert "finding_disposition" in kinds
        assert "run_finalized" not in kinds   # 未封 success
        # 下一轮：revision 变化触发重验（即使 item 数未变）；新需求被验收后放行
        orch._ask_lead_integration = lambda task, items: {
            "integrated": "pass", "gaps": [],
            "requirements": [{"id": r["requirement_id"], "status": "pass"}
                             for r in orch._requirements]}
        outcome2 = {"task": TASK_PROSE, "items": [], "actions": []}
        assert orch._try_finalize(outcome2, TASK_PROSE, from_lead=True)
        assert outcome2["result"] == "success"

    def test_reviewer_invented_is_suggestion_only(self, tmp_path):
        cp, orch, outcome, closed = self._finalize_with_verdict(tmp_path, {
            "integrated": "pass", "gaps": [],
            "findings": [{"text": "建议增加暗黑模式支持（原文无此要求）"}]})
        assert closed and outcome["result"] == "success"   # 建议不阻塞
        ev = next(e for e in cp.store.read_all()
                  if e.kind == "finding_disposition")
        assert ev.payload["branch"] == "suggestion"
        assert ev.payload["mandatory"] is False            # 永不变 mandatory

    def test_amendment_budget_exhausted_blocks(self, tmp_path):
        """修正预算（≤2/run）耗尽后，新 finding 记 amendment-budget-exhausted
        且阻塞（不静默吞，防"模型不断发明新需求"）。"""
        cp, orch, outcome, closed = self._finalize_with_verdict(tmp_path, {
            "integrated": "pass", "gaps": [],
            "findings": [{"text": "文末附一句口诀方便记忆"},
                         {"text": "交付物末尾标注作者"},
                         {"text": "附一页回滚指引"}]}, task=TASK_PROSE)
        assert not closed
        assert orch._amendments_used == 2                  # 预算 cap 生效
        branches = [e.payload["branch"] for e in cp.store.read_all()
                    if e.kind == "finding_disposition"]
        assert branches.count("contract-amendment") == 2
        assert "amendment-budget-exhausted" in branches
        assert any("预算耗尽" in g for g in orch._integration_gaps)


class _FetchDrivenReviewer(Provider):
    """内容路由 provider：集成/决策按脚本，验收面驱动 FETCH 回查（O4 体内）。"""

    name = "fetch-reviewer"

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = []
        self._lock = threading.Lock()

    def complete(self, route, messages, tools=None, max_tokens=4096):
        user = next((m["content"] for m in reversed(messages or [])
                     if m.get("role") == "user"), "")
        sysmsg = str(messages[0].get("content", "")) if messages else ""
        with self._lock:
            self.calls.append({"user": user, "sys": sysmsg})
        if "你是集成验收 Lead" in sysmsg:
            text = json.dumps({"integrated": "pass", "gaps": []})
        elif "决策协议" in user:
            with self._lock:
                d = self._decisions.pop(0) if self._decisions else {"action": "accept"}
            text = json.dumps(d)
        elif "你是验收 Lead" in sysmsg:
            if "FETCH 结果" not in user and "回查结果" not in user:
                # 预览外取料：从材料提示里抽 submission_package ref 发起 FETCH
                import re as _re
                m = _re.search(r"submission_package=(\S+?)[；\n）]", user)
                ref = m.group(1) if m else "dep-nonexist"
                text = "FETCH: %s from=12000" % ref
            elif "尾部缺陷" in user:
                text = json.dumps({"verdict": "reject",
                                   "verdict_reason": "回查发现尾部缺陷",
                                   "attribution": "description"})
            else:
                text = json.dumps({"verdict": "accept", "verdict_reason": "ok"})
        else:
            text = self._worker_text
        return ProviderResult(text=text, stop_reason=StopReason.COMPLETED,
                              usage=Usage(input_tokens=10, output_tokens=5))


class _IntegrationFetchDriven(Provider):
    """集成验收面驱动 FETCH：首轮取料、次轮依据回查内容判 fail。"""

    name = "integration-fetch"

    def __init__(self):
        self.calls = 0

    def complete(self, route, messages, tools=None, max_tokens=4096):
        import re as _re
        user = next((m["content"] for m in reversed(messages or [])
                     if m.get("role") == "user"), "")
        sysmsg = str(messages[0].get("content", "")) if messages else ""
        self.calls += 1
        if "你是集成验收 Lead" in sysmsg:
            if "回查结果" not in user:
                m = _re.search(r"submission_package=(\S+?)[；\n）]", user)
                text = "FETCH: %s from=12000" % (m.group(1) if m else "pkg-nonexist")
            elif "尾部缺陷" in user:
                text = json.dumps({"integrated": "fail", "requirements": [],
                                   "gaps": ["回查发现尾部缺陷：函数体崩缺"]})
            else:
                text = json.dumps({"integrated": "pass", "requirements": [],
                                   "gaps": []})
        else:
            text = "{}"
        return ProviderResult(text=text, stop_reason=StopReason.COMPLETED,
                              usage=Usage(input_tokens=10, output_tokens=5))


class TestO4FetchChannel:
    """O4：决定性缺陷在预览之外 → Reviewer 经真实通道发现并拒。"""

    def test_fetch_beyond_preview_finds_flaw_and_rejects(self, tmp_path):
        cp = make_cp(tmp_path)
        marker = "尾部缺陷"
        # 交付 > 12000 字符（预览窗口只到 12000）；缺陷落在回查窗口
        # [12000,16000) 内（"填充。"×4300 ≈ 12900 字符处）
        big = "前置正常。" + "填充。" * 4300 + marker + "：函数体在此崩缺"
        assert len(big) > 12000
        provider = _FetchDrivenReviewer([
            {"action": "derive", "route": B_ROUTE},
        ])
        provider._worker_text = big
        orch = Orchestrator(cp, provider, store_dir=None,
                            lead_route=ModelRoute("p", "s-model", level=Level.S))
        out = orch.run_task("长交付回查任务")
        # 验收首轮走 FETCH 回查 → 第二轮看到尾部缺陷 → reject（描述问题）
        fetches = [e for e in cp.store.read_all() if e.kind == "review_fetch"]
        assert fetches and fetches[0].payload["range"][0] == 12000
        rejected = [e for e in cp.store.read_all() if e.kind == "work_item_rejected"]
        assert rejected and "尾部缺陷" in rejected[0].payload["reason"]

    def test_fetch_guards(self, tmp_path):
        """版本失配不出内容（不静默给 latest）；越权/不存在/预算不足 → error。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [])
        item, pkg_id = _accepted_item(cp, text="版本A的内容")
        orch._submissions[item.item_id] = "版本A的内容"
        lead = cp.root_lead_node
        # 正常读取
        ok = orch._review_fetch(str(pkg_id), requester=lead)
        assert ok.get("content") == "版本A的内容" and ok["complete"]
        # 越权：非 root lead
        assert orch._review_fetch(str(pkg_id), requester="node-other")["error"] == "forbidden"
        # 不存在
        assert orch._review_fetch("pkg://wi-nonexist/abcdef12",
                                  requester=lead)["error"] == "not-found"
        # 版本失配：旧 digest ref 不静默解析成 latest
        mismatch = orch._review_fetch("pkg://%s/000000000000" % item.item_id,
                                      requester=lead)
        assert mismatch["error"] == "version-mismatch"
        assert "content" not in mismatch
        # 预算耗尽
        orch._fetch_spent = orch.FETCH_BUDGET_CHARS
        assert orch._review_fetch(str(pkg_id),
                                  requester=lead)["error"] == "fetch-budget-exhausted"

    def test_integration_review_fetches_beyond_preview(self, tmp_path):
        """O4 接线范围：集成验收（任务级判定那道门）同样能经只读通道取料，
        而不是只有逐 item 验收可取——决定性缺陷在集成材料预览之外时，
        判决必须建立在回查到的原文上。"""
        cp = make_cp(tmp_path)
        big = "前置正常。" + "填充。" * 4300 + "尾部缺陷：函数体崩缺"
        assert len(big) > Orchestrator.REVIEW_MATERIAL_LIMIT
        orch = make_orch(cp, [])
        orch._init_task_registry(TASK2)
        item, pkg_id = _accepted_item(cp, text=big)
        orch._submissions[item.item_id] = big
        provider = _IntegrationFetchDriven()
        orch.provider = provider
        verdict = orch._ask_lead_integration(
            TASK2, [cp.proj.work_items[item.item_id]])
        fetches = [e for e in cp.store.read_all() if e.kind == "review_fetch"]
        assert fetches and fetches[0].payload["range"][0] == 12000, \
            "集成验收必须发起回查，而不是只拿预览下结论"
        assert verdict.get("integrated") == "fail"
        assert any("尾部缺陷" in g for g in verdict.get("gaps") or [])

    def test_verdict_binds_actually_fetched_materials(self, tmp_path):
        """借鉴项①：verdict↔实际输入清单——判决必须落 review_evidence，
        绑定实际看过的材料版本/范围、合同版本、路由；不是"材料存了"就算看过。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [{"action": "derive", "route": B_ROUTE}])
        provider = _FetchDrivenReviewer([{"action": "derive", "route": B_ROUTE}])
        orch.provider = provider
        provider._worker_text = "前置正常。" + "填充。" * 4300 + "尾部缺陷：函数体崩缺"
        orch.run_task("长交付回查任务")
        evs = [e for e in cp.store.read_all() if e.kind == "review_evidence"]
        assert evs, "验收 verdict 必须落 review_evidence"
        rej = [e for e in evs if e.payload.get("verdict") == "reject"]
        assert rej, "回查发现缺陷后的 reject 判决必须有证据记录"
        p = rej[0].payload
        assert p["surface"] == "lead-review" and p["materials"], \
            "判决必须绑定它实际取过的料"
        m = p["materials"][0]
        assert m["range"][0] == 12000 and m["version"] and p["route"]


class TestO6WeakeningGuard:
    """O6：requirement ID 不变而 mandatory true→false → 集中校验记录一次。"""

    def test_weakening_recorded_once_centrally(self, tmp_path):
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [])
        orch._init_task_registry(TASK2)
        orch._requirements[0]["mandatory"] = False       # 原地弱化
        orch._check_registry_integrity()
        orch._check_registry_integrity()                  # 二次校验不重复记
        evs = [e for e in cp.store.read_all() if e.kind == "mandate_weakened"]
        assert len(evs) == 1
        assert evs[0].payload["requirement_id"] == "req-0"
        assert evs[0].payload["was_mandatory"] is True


class TestO5UnhandedMarkers:
    """O5：启发式边界不静默——表格/散文约束进登记表为待验收项，
    集成验收未覆盖（unknown）→ 阻止 success。"""

    def test_markers_excluded_from_dispatch_gate(self, tmp_path):
        """r8 体内回归：heuristic 标记项不参与派发覆盖门（它们由集成验收
        verdict 兜底）——否则含表格/嵌套列表的任务被 coverage-rejected 卡死。"""
        task = TASK2 + "\n\n| 交付物 | 格式 |\n| --- | --- |\n| 报告 | markdown |"
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("fission", route=B_ROUTE, subtasks=[
                {"title": "交付甲", "covers": [0]},
                {"title": "交付乙", "covers": [1]}])},
            {"text": "甲交付"}, {"text": "乙交付"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": [],
                                 "requirements": [{"id": r, "status": "pass"}
                                                  for r in ("req-0", "req-1",
                                                            "req-extra-2")]})},
        ])
        out = orch.run_task(task)
        assert not [a for a in out["actions"]
                    if isinstance(a, str) and a.startswith("coverage-rejected")]

    def test_unhandled_marker_blocks_success_until_verified(self, tmp_path):
        task = TASK2 + "\n\n| 交付物 | 格式 |\n| --- | --- |\n| 报告 | markdown |"
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [])
        orch._init_task_registry(task)
        extras = [r for r in orch._requirements
                  if r["requirement_id"].startswith("req-extra-")]
        assert extras and any("table" in r["text"] for r in extras)
        assert all(r["mandatory"] for r in extras)      # 未解决缺口不得判 success
        _accepted_item(cp)
        # 集成验收只提了 req-0/req-1：标记项未提及 → unknown → 不 success
        orch._ask_lead_integration = lambda t, items: {
            "integrated": "pass", "gaps": [],
            "requirements": [{"id": "req-0", "status": "pass"},
                             {"id": "req-1", "status": "pass"}]}
        outcome = {"task": task, "items": [], "actions": []}
        assert not orch._try_finalize(outcome, task, from_lead=True)
        assert orch._integration_statuses[extras[0]["requirement_id"]] == "unknown"
        # 补上标记项 verdict 后放行（模拟 Lead 补派了覆盖交付 → item 数变化
        # 触发重验）
        _accepted_item(cp, text="表格要求的报告交付")
        orch._ask_lead_integration = lambda t, items: {
            "integrated": "pass", "gaps": [],
            "requirements": [{"id": r["requirement_id"], "status": "pass"}
                             for r in orch._requirements]}
        outcome2 = {"task": task, "items": [], "actions": []}
        assert orch._try_finalize(outcome2, task, from_lead=True)
        assert outcome2["result"] == "success"


class TestO1O2TerminalSemantics:
    """O1 异常路径证据驱动 / O2 空成功拒绝与合法直接交付。"""

    def test_quota_path_failed_with_true_stop_reason(self, tmp_path):
        """O1：quota 耗尽于集成验收调用面 → failed + stop=quota_exhausted
        （accepted 但未验证 = 证据不足；不宣称中间产物皆错，真实停止原因）。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付甲"},
            {"text": json.dumps({"verdict": "accept"})},
            {"raise": "quota"},                    # 集成验收调用面配额耗尽
        ])
        out = orch.run_task(TASK2)
        assert out["final"] == "quota-exhausted"
        assert out["result"] == "failed"
        assert out["stop_reason"] == "quota_exhausted"

    def test_direct_delivery_with_verification_succeeds(self, tmp_path):
        """O2 边界 A：零子任务、Lead 直接交付且集成验收通过 → success。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("single", route=B_ROUTE)},
            {"text": "直接交付的完整答案"},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ])
        out = orch.run_task("单干任务")
        assert out["final"] == "single"
        assert out["result"] == "success"
        assert out["phase"] == "finalized" and out["stop_reason"] == "completed"

    def test_accept_with_zero_delivery_not_success(self, tmp_path):
        """O2 边界 B：零有效交付、Lead 发 accept → 不得 success（不看有无
        accepted 子 item，看有无契约认可交付与验证证据）。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("accept")},   # 空喊收束：无任何交付
        ])
        out = orch.run_task("空收束任务")
        assert out["result"] == "failed"
        assert out["result"] != "success"


class TestFinalizeBoundaries:
    """边界①崩溃恢复 / 边界②并发 finalize。"""

    def test_crash_before_finalize_recovers_from_events(self, tmp_path):
        """崩溃在 finalize 中途（验收完未写终局）：新进程在同一事件日志上
        恢复——_resume_finalized 水合终局（已 finalize）或不重复落账。"""
        cp = make_cp(tmp_path)
        orch = make_orch(cp, [
            {"text": _decision("derive", route=B_ROUTE)},
            {"text": "交付甲"},
            {"text": json.dumps({"verdict": "accept"})},
            {"text": json.dumps({"integrated": "pass", "gaps": []})},
        ])
        out1 = orch.run_task(TASK2)
        assert out1["result"] == "success"
        cp.close()   # 释放写者锁（Windows 单写者），再开同 store 的新实例
        # 模拟响应丢失/崩溃后重跑：同 store 新 ControlPlane + 新 Orchestrator
        cp2 = ControlPlane(spec=RootExecutionSpec(max_open_work_items=6,
                                                  max_active_node_points=8),
                           store_path=tmp_path / "events.jsonl",
                           catalog=catalog())
        orch2 = Orchestrator(cp2, MockProvider(script=[]), store_dir=None,
                             lead_route=ModelRoute("p", "s-model", level=Level.S))
        outcome2 = {"task": TASK2, "items": [], "actions": []}
        assert orch2._resume_finalized(outcome2, TASK2)
        assert outcome2["result"] == "success"
        assert "already-finalized" in outcome2["actions"]
        assert len(orch2.provider.calls) == 0            # 不重复调 LLM
        finals = [e for e in cp2.store.read_all() if e.kind == "run_finalized"]
        assert len(finals) == 1                          # 不重复落账/封存

    def test_concurrent_finalize_unique_logical_terminal(self, tmp_path):
        """两执行者并发 finalize 同一 run：逻辑终局唯一（结果/digest 一致）。

        承诺等级区分：逻辑终局唯一 = 保证；LLM 计费恰好一次 = 不承诺（两个
        进程内缓存各自独立的实例可能各调一次集成验收，如实记录事件数）。"""
        cp = make_cp(tmp_path)
        _accepted_item(cp)
        orchs = []
        for _ in range(2):
            o = make_orch(cp, [])
            o._init_task_registry(TASK2)
            o._ask_lead_integration = lambda task, items: {
                "integrated": "pass", "gaps": []}      # 桩掉 LLM（并发确定性）
            orchs.append(o)
        outcomes = [{"task": TASK2, "items": [], "actions": []} for _ in orchs]
        threads = [threading.Thread(
            target=o._try_finalize, args=(outcomes[i], TASK2),
            kwargs={"from_lead": True}) for i, o in enumerate(orchs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()
        results = [o.get("result") for o in outcomes]
        assert results == ["success", "success"]       # 唯一生效逻辑终局
        digests = [o["candidate"]["bundle_digest"] for o in outcomes]
        assert digests[0] == digests[1]                # 同一候选版本
