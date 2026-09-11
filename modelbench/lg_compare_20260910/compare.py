"""lg_compare_20260910：经典 Orchestrator vs OrchestratorLG 真实 API 对照实验。

同构臂：所有角色（Lead 决策 / Lead 验收 / worker / CM）一律 glm-5.3-flash。
自变量只有调度层：classic = 父类串行验收循环；lg = StateGraph + Send fan-out
（worker 并行、验收按 item 并行、deps 交接摘要注入）。

观测口径：
- 调用级日志（LoggingProvider，含 ts/latency_ms）→ 并行证据（时间区间重叠）；
- 角色按消息内容分类（同构臂 role_by_model 失效：全模型同名会全标 lead）；
- 控制面事件账本原样保留（runs/<run>/events.jsonl），装配包落 packages/；
- 串行链任务的 P0 交接证据：下游 item 的装配包应含"上游交付摘要"段与
  不可信标注（classic 臂应两者皆无）。

运行：python compare.py（前台约 10-25 分钟；也可放后台任务）。真实 API
调用已获用户授权；每 run 预算护栏 200K tokens（BudgetExceeded 即中止该 run，
如实记录，不炸穿整轮实验）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
MB_DIR = HERE.parent
sys.path.insert(0, str(MB_DIR))

import providers  # noqa: E402,F401  # 引导 dpswarm-plugin 进 sys.path
from providers import LoggingProvider, log_context, make_provider  # noqa: E402
from providers import BudgetExceeded  # noqa: E402
from driver import (  # noqa: E402
    LeadProtocolProbe,
    _backoff_complete,
    build_catalog,
    level_of,
)

from dpswarm.context.manager import ContextManagerLLM  # noqa: E402
from dpswarm.control import ControlPlane  # noqa: E402
from dpswarm.orchestrator import Orchestrator  # noqa: E402
from dpswarm.orchestrator_lg import OrchestratorLG  # noqa: E402
from dpswarm.providers.base import Provider  # noqa: E402
from dpswarm.types import ModelRoute, RootExecutionSpec  # noqa: E402

MODEL_PROVIDER = os.environ.get("EXP_PROVIDER", "glm")
MODEL_NAME = os.environ.get("EXP_MODEL", "glm-5.3-flash")
MODEL_KEY = f"{MODEL_PROVIDER}/{MODEL_NAME}"
BUDGET_TOKENS = 200_000
MAX_TURNS = 6

RUNS_DIR = HERE / "runs"
LOGS_DIR = HERE / "logs"
RESULTS_DIR = HERE / "results"
SUMMARY_FILE = RESULTS_DIR / "summary.json"
PROGRESS_FILE = HERE / "r1-progress.log"
TAG = "r1"


def _apply_tag(tag: str) -> None:
    """实验轮次隔离：r1 用原始目录（runs/ logs/ results/summary.json），
    其余轮次独立目录（runs-<tag>/ logs-<tag>/ results/summary-<tag>.json）。"""
    global RUNS_DIR, LOGS_DIR, RESULTS_DIR, SUMMARY_FILE, PROGRESS_FILE, TAG
    TAG = tag
    if tag != "r1":
        RUNS_DIR = HERE / f"runs-{tag}"
        LOGS_DIR = HERE / f"logs-{tag}"
        SUMMARY_FILE = HERE / "results" / f"summary-{tag}.json"
        PROGRESS_FILE = HERE / f"{tag}-progress.log"


def _progress(msg: str) -> None:
    """进度可见：每 run 开始/结束追加一行时间戳 + 状态（<tag>-progress.log）。"""
    line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), TAG, msg)
    with open(PROGRESS_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)

TASKS: Dict[str, str] = {
    # 并行型：三个显然互相独立的小函数，强引导 fission 无 deps。
    "parallel": (
        "三个完全独立的小型交付，互不依赖、互不引用：\n"
        "1. 子任务「回文」：实现 Python 函数 is_palindrome(s: str) -> bool，"
        "忽略大小写与非字母数字字符，附 3 个 doctest 用例；\n"
        "2. 子任务「罗马数字」：实现 to_roman(n: int) -> str（1<=n<=3999），"
        "附 3 个 doctest 用例；\n"
        "3. 子任务「进制转换」：实现 base_convert(s: str, from_base: int, "
        "to_base: int) -> str（支持 2-36），附 3 个 doctest 用例。\n"
        "每个子任务交付一个 ```python 代码块（≤50 行，含函数与 doctest）。\n"
        "分工提示：三者零依赖，建议 fission 拆 3 个 subtasks 并行执行、不要标 "
        "deps；全部验收通过后 Lead 决策 accept 收束。"),
    # 串行链型（逐字依赖）：B 严格依赖 A 的产出，强引导 fission 带 deps。
    "serial": (
        "两阶段串行交付，第二阶段严格依赖第一阶段产出：\n"
        "- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——"
        "字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、"
        "tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与"
        "不超过 10 行的设计说明；\n"
        "- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库"
        "实现 validate_record(record: dict, schema: dict) -> list[str]"
        "（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 "
        "schema 必须逐字引用 A 的交付）。\n"
        "分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks"
        "（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，"
        "不得再继续裂变。"),
    # 串行链型（语义依赖，r3 新增）：A 出分析结论、B 基于结论写报告，
    # 验收不卡逐字——与 serial（逐字依赖）构成依赖类型对照。
    "semantic": (
        "两阶段串行交付，第二阶段依赖第一阶段的分析结论（语义依赖，不要求"
        "逐字引用）：\n"
        "- 子任务 A「反馈分析」：以下是某 App 最近一周的 12 条用户反馈：\n"
        "  1. 更新后闪退频繁，一天三次以上\n  2. 夜间模式对比度太低看不清\n"
        "  3. 客服工单三天没人回\n  4. 耗电明显变快，待机一晚上掉 20%\n"
        "  5. 闪退主要出现在打开聊天记录时\n  6. 夜间模式自动切换时间不对\n"
        "  7. 客服机器人答非所问，转人工难\n  8. 更新后发热严重\n"
        "  9. 希望增加字体大小调节\n  10. 闪退问题在最新补丁后好转\n"
        "  11. 电池健康度显示不准确\n  12. 客服响应速度比上月慢了\n"
        "  归纳出 3 条核心结论，每条附 1-2 条反馈编号作为证据；交付 markdown"
        "（结论用有序列表，不超过 300 字）；\n"
        "- 子任务 B「管理层简报」：基于 A 验收后的 3 条结论，写一份 ≤400 字的"
        "管理层简报（覆盖全部 3 条结论、语义一致即可，不要求逐字引用 A 的"
        "原文），语气面向非技术管理层，结尾给出一个优先级建议。\n"
        "分工提示：B 必须等 A 验收后开始，fission 拆且只拆 2 个 subtasks"
        "（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，"
        "不得再继续裂变。"),
    # r8 场景任务（最小任务集触发协议场景）
    # 预览外缺陷场景：长交付超验收窗口，决定性内容（校验和行）在末尾，
    # 验收须经 FETCH 回查（O4）。
    "longdoc": (
        "两阶段串行交付：\n"
        "- 子任务 A「手册撰写」：撰写《迷你记账字段参考手册》正文，不少于 "
        "13000 字符——4 个字段（id/amount/currency/tags）各写定义、类型、"
        "约束、示例、常见错误五个小节；文末必须有一行 `CHECKSUM: <8 位字母"
        "数字>` 作为完整性校验和；\n"
        "- 子任务 B「审校」：通读 A 的手册全文，确认 4 个字段 × 5 小节齐全且"
        "末尾校验和行存在，交付 ≤300 字审校报告。\n"
        "分工提示：fission 拆且只拆 2 个 subtasks（A 与 B），B 标 deps=[0]；"
        "全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。"),
    # 清单外原文要求场景：散文补充行规避登记表启发式（无约束关键词），
    # 集成验收应经 finding → 合同修正流程发现它（O3）。
    "hiddenreq": (
        "两步任务：\n"
        "1. 实现配置解析函数 parse_kv（key=value 文本 → dict）并附 2 个 "
        "doctest 用例\n"
        "2. 编写使用说明文档（≤200 字）\n"
        "附注：交付末尾请附上作者署名行。\n"
        "分工提示：fission 拆且只拆 2 个 subtasks（1 与 2），2 标 deps=[0]；"
        "全部验收通过后 Lead 决策 accept 收束。"),
    # 合法直接交付场景：Lead 应选 single，直接交付过集成门 → success（O2）。
    "direct": (
        "直接回答（无需拆分）：一个进程内两个线程共享同一把读写锁时，写锁"
        "被持有期间读锁能否被获取？一句话给出结论与理由。"),
}


class RoleTagger(Provider):
    """同构臂角色归集：按消息内容分类（decision/review/worker），经
    log_context 传给内层 LoggingProvider（role_by_model 在同构臂失效）。"""

    name = "role-tagger"

    def __init__(self, inner: Provider) -> None:
        self.inner = inner

    def complete(self, route, messages, tools=None, max_tokens: int = 4096):
        sysmsg = str(messages[0].get("content", "")) if messages else ""
        if "你是调度 Lead" in sysmsg:
            role = "lead-decision"
        elif "你是集成验收 Lead" in sysmsg:
            role = "lead-integration"
        elif "你是验收 Lead" in sysmsg:
            role = "lead-review"
        else:
            role = "worker"
        with log_context(role=role):
            return self.inner.complete(route, messages, tools=tools,
                                       max_tokens=max_tokens)


class BigTokenOrchestratorLG(OrchestratorLG):
    """r1 遗留：lg 臂的 BigToken 等价物（16384 固定上限）。

    r2 起弃用：角色级下限（worker 32768 / Lead 16384）+ 截断阶梯已进父类
    （§4），两臂直接用 Orchestrator / OrchestratorLG。保留此类仅为 r1
    数据可复现。"""

    def _complete_with_backoff(self, route, messages, node_id, max_tokens=None):
        return _backoff_complete(self, route, messages, node_id, tools=None,
                                 max_tokens=self._token_budget_for(
                                     route, node_id, max_tokens or 16384))


class BigTokenOrchestratorClassic(Orchestrator):
    """r1 遗留：classic 臂 BigToken 等价物（16384 固定上限）。r2 起弃用。"""

    def _complete_with_backoff(self, route, messages, node_id, max_tokens=None):
        return _backoff_complete(self, route, messages, node_id, tools=None,
                                 max_tokens=self._token_budget_for(
                                     route, node_id, max_tokens or 16384))


def preflight(inner: Provider) -> None:
    """一次极便宜的真实调用确认 key/模型可用；不可用即清晰报错退出。"""
    route = ModelRoute(MODEL_PROVIDER, MODEL_NAME, level=level_of(MODEL_PROVIDER, MODEL_NAME))
    try:
        result = inner.complete(route, [{"role": "user", "content": "回复 ok 两个字母即可。"}],
                                max_tokens=512)
    except Exception as e:
        raise SystemExit(
            f"preflight 失败：{type(e).__name__}: {e}\n"
            "检查 GLM_API_KEY / keys.local.json 与模型名，不跑空实验。") from e
    if result.stop_reason.value == "error":
        raise SystemExit(f"preflight 失败：stop_reason=error "
                         f"{json.dumps(result.raw, ensure_ascii=False)[:300]}")
    print(f"[preflight] ok: {MODEL_KEY} stop={result.stop_reason.value} "
          f"usage={result.usage.total_tokens()}", flush=True)


def _assemble(arm: str, task_key: str):
    """每臂独立 ControlPlane + 隔离 run 目录；catalog 只含 glm-5.3-flash。"""
    run_dir = RUNS_DIR / f"{arm}-{task_key}"
    run_dir.mkdir(parents=True, exist_ok=True)
    catalog = build_catalog([MODEL_KEY])
    cp = ControlPlane(
        spec=RootExecutionSpec(max_open_work_items=8, max_active_node_points=96,
                               max_team_workers=4),
        store_path=run_dir / "events.jsonl", catalog=catalog)
    lead_route = ModelRoute(MODEL_PROVIDER, MODEL_NAME,
                            level=level_of(MODEL_PROVIDER, MODEL_NAME))
    run_id = f"{arm}-{task_key}"
    logged = LoggingProvider(make_provider(MODEL_PROVIDER),
                             LOGS_DIR / f"{run_id}.jsonl",
                             run_id=run_id, arm=arm, task=task_key,
                             budget=BUDGET_TOKENS)
    probe = LeadProtocolProbe(RoleTagger(logged), lead_route)
    # CM 同构接线（§5.5 按需瞬时调用；lg 臂 deps 交接走它，classic 臂不触发）
    def cm_complete(route, messages):
        with log_context(role="cm"):
            return logged.complete(route, messages, max_tokens=16384)
    cm = ContextManagerLLM(
        cm_complete,
        ModelRoute(MODEL_PROVIDER, MODEL_NAME, level=lead_route.level))
    orch_cls = Orchestrator if arm == "classic" else OrchestratorLG
    orch = orch_cls(cp, probe, store_dir=run_dir / "packages",
                    lead_route=lead_route, context_manager=cm, max_turns=MAX_TURNS)
    return cp, orch, probe, run_dir


def run_one(arm: str, task_key: str) -> Dict[str, Any]:
    """单 run：跑任务、归档 outcome/事件账本/装配包，护栏异常如实记账。"""
    cp, orch, probe, run_dir = _assemble(arm, task_key)
    outcome: Dict[str, Any] = {}
    error: Optional[str] = None
    _progress(f"{arm}-{task_key} started")
    t0 = time.time()
    guard_abort: Optional[str] = None
    try:
        outcome = orch.run_task(TASKS[task_key])
    except BudgetExceeded:
        # 运行级 token 护栏中止（harness 侧，编排器看不到）：异常结束同样要有
        # 诚实的交付状态——O1"所有终局入口统一证据驱动判定"的边界补口：
        # 不得留 final/result 为空（那等于异常收尾无结论）；证据不足以判成功
        # 即 failed（不按 accepted 计数推 partial，保留真实 stop_reason）。
        guard_abort = "budget_exhausted"
        error = "BudgetExceeded"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    if outcome.get("result") is None and (error or guard_abort):
        outcome.setdefault("final", "aborted")
        outcome["result"] = "failed"
        outcome["dimensions"] = {
            "phase": "finalized", "outcome": "failed",
            "stop_reason": guard_abort or "aborted"}
        outcome["aborted"] = True
    wall = time.time() - t0
    cp.close()   # 释放写者文件锁（Windows）
    summary = {
        "run_id": f"{arm}-{task_key}", "arm": arm, "task": task_key,
        "wall_seconds": round(wall, 2),
        "final": outcome.get("final"), "result": outcome.get("result"),
        "stop_reason": outcome.get("stop_reason")
        or (outcome.get("dimensions") or {}).get("stop_reason"),
        "error": error,
        "actions": outcome.get("actions"),
        "items": outcome.get("items"),
        "lead_tokens": orch.lead_tokens,
        "protocol_failures": len(probe.failures),
        "protocol_failure_detail": probe.failures[:5],
    }
    (run_dir / "outcome.json").write_text(
        json.dumps({"summary": summary, "outcome": outcome}, ensure_ascii=False,
                   indent=2, default=str), encoding="utf-8")
    _progress(f"{arm}-{task_key} done final={summary['final']} error={error} "
              f"wall={wall:.0f}s")
    print(f"[run {arm}-{task_key}] final={summary['final']} error={error} "
          f"wall={wall:.0f}s lead_tokens={orch.lead_tokens} "
          f"protocol_failures={len(probe.failures)}", flush=True)
    return summary


# ---------------------------------------------------------------------------
# 分析：事件计数 / token 分账 / 终局 / 墙钟 / 并行与交接证据
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _read_events(path: Path) -> List[Dict[str, Any]]:
    """控制面 store 是事务信封格式：每行 {"txn": n, "events": [...]}，需展开。"""
    events: List[Dict[str, Any]] = []
    for row in _read_jsonl(path):
        if "events" in row:
            events.extend(row["events"])
        else:
            events.append(row)
    return events


def _overlap(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    a0, a1 = a["ts"], a["ts"] + (a.get("latency_ms") or 0) / 1000.0
    b0, b1 = b["ts"], b["ts"] + (b.get("latency_ms") or 0) / 1000.0
    return a0 < b1 and b0 < a1


def _any_overlap(rows: List[Dict[str, Any]]) -> bool:
    return any(_overlap(rows[i], rows[j])
               for i in range(len(rows)) for j in range(i + 1, len(rows)))


def analyze_run(run_id: str) -> Dict[str, Any]:
    calls = _read_jsonl(LOGS_DIR / f"{run_id}.jsonl")
    events = _read_events(RUNS_DIR / run_id / "events.jsonl")
    by_role: Dict[str, int] = {}
    for c in calls:
        usage = c.get("usage") or {}
        total = sum(int(usage.get(k) or 0)
                    for k in ("input", "output", "cache_read", "cache_write"))
        by_role[c.get("role") or "?"] = by_role.get(c.get("role") or "?", 0) + total
    wall = None
    if calls:
        wall = round(max(c["ts"] + (c.get("latency_ms") or 0) / 1000.0 for c in calls)
                     - min(c["ts"] for c in calls), 2)
    event_counts: Dict[str, int] = {}
    for e in events:
        event_counts[e.get("kind", "?")] = event_counts.get(e.get("kind", "?"), 0) + 1
    # 每 item 终局（取该 item 最后一个终局类事件）
    terminal: Dict[str, str] = {}
    for e in events:
        kind, payload = e.get("kind", ""), e.get("payload", {})
        item = payload.get("item_id")
        if not item:
            continue
        if kind == "work_item_accepted":
            terminal[item] = "accepted"
        elif kind == "work_item_escalated":
            terminal[item] = "escalated"
        elif kind == "work_item_terminated":
            terminal[item] = "terminated:%s" % payload.get("reason")
    reviews = [c for c in calls if c.get("role") == "lead-review"]
    workers = [c for c in calls if c.get("role") == "worker"]
    return {
        "run_id": run_id,
        "event_counts": event_counts,
        "tokens_by_role": by_role,
        "tokens_total": sum(by_role.values()),
        "api_wall_seconds": wall,
        "item_terminal": terminal,
        "review_calls": len(reviews),
        "review_overlap": _any_overlap(reviews) if len(reviews) > 1 else None,
        "worker_calls": len(workers),
        "worker_overlap": _any_overlap(workers) if len(workers) > 1 else None,
        # §4 截断阶梯观测：传输层截断次数 vs 阶梯升档次数（max_tokens_escalated
        # 在 event_counts 里；truncated_calls 含阶梯内重试与最终截断交付）
        "truncated_calls": sum(1 for c in calls
                               if c.get("stop_reason") == "max-tokens"),
        "ladder_escalations": event_counts.get("max_tokens_escalated", 0),
    }


def check_handoff(run_id: str) -> Dict[str, Any]:
    """P0 交接证据：串行链任务下游 item 的装配包三层结构与终局。

    - L2 标注：r1 措辞"不可信摘要，以实际文件为准" / r2 起"摘要仅供导航"；
    - L1：确定性逐字原子事实层（"逐字引用请以此层为准"）；
    - terminal：该下游 item 的账本终局（B 收束率口径）。"""
    events = _read_events(RUNS_DIR / run_id / "events.jsonl")
    downstream = [e["payload"]["after"] for e in events
                  if e.get("kind") == "work_item_dependency_added"]
    terminal: Dict[str, str] = {}
    for e in events:
        kind, payload = e.get("kind", ""), e.get("payload", {})
        item = payload.get("item_id")
        if kind == "work_item_accepted":
            terminal[item] = "accepted"
        elif kind == "work_item_escalated":
            terminal[item] = "escalated"
        elif kind == "work_item_terminated":
            terminal[item] = "terminated:%s" % payload.get("reason")
    pkg_dir = RUNS_DIR / run_id / "packages"
    findings = []
    for item_id in downstream:
        files = sorted(pkg_dir.glob(f"{item_id}.*.md")) if pkg_dir.exists() else []
        content = files[-1].read_text(encoding="utf-8") if files else ""
        findings.append({
            "item": item_id,
            "package_file": files[-1].name if files else None,
            "promoted": bool(files),   # 无包 = 从未晋级启动（deps-stuck 悬挂）
            "has_summary_section": "上游交付摘要" in content or "上游交付交接" in content,
            "has_trust_note": ("不可信摘要，以实际文件为准" in content
                               or "摘要仅供导航" in content),
            "has_atomic_layer": "逐字引用请以此层为准" in content,
            "terminal": terminal.get(item_id),
        })
    accepted = sum(1 for f in findings if f["terminal"] == "accepted")
    return {"run_id": run_id, "downstream": findings,
            "downstream_acceptance": f"{accepted}/{len(findings)}"}


def analyze(summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    report = {"runs": {}, "handoff": {}}
    for s in summaries:
        run_id = s["run_id"]
        report["runs"][run_id] = {**s, **analyze_run(run_id)}
    for task in TASKS:
        for arm in ("classic", "lg"):
            run_id = f"{arm}-{task}"
            if (RUNS_DIR / run_id).exists():
                report["handoff"][run_id] = check_handoff(run_id)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_FILE.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    return report


def _load_summary(run_id: str) -> Optional[Dict[str, Any]]:
    """已完成 run 的摘要（outcome.json 存在即视为完整自洽，幂等复用）。"""
    f = RUNS_DIR / run_id / "outcome.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))["summary"]
    except (ValueError, KeyError):
        return None


def _clean_run(run_id: str) -> None:
    """清掉不完整 run 的残留（run 目录 + 调用日志），保证重跑自洽。"""
    shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
    log = LOGS_DIR / f"{run_id}.jsonl"
    if log.exists():
        log.unlink()


def main() -> None:
    # 用法：python compare.py [--tag r3] [run_id ...]——缺省跑全矩阵；
    # run_id 形如 lg-serial / classic-semantic（<arm>-<task>），可传多个；
    # 已有 outcome.json 的 run 幂等复用，不完整残留清理后重跑。
    args = list(sys.argv[1:])
    if "--tag" in args:
        i = args.index("--tag")
        _apply_tag(args[i + 1])
        del args[i:i + 2]
    analyze_only = "--analyze-only" in args
    if analyze_only:
        args.remove("--analyze-only")
    RUNS_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    if args:
        plan = []
        for run_id in args:
            arm, task = run_id.split("-", 1)
            plan.append((task, arm))
    else:
        plan = [(task, arm) for task in TASKS for arm in ("classic", "lg")]
    if not analyze_only:
        for task, arm in plan:
            run_id = f"{arm}-{task}"
            existing = _load_summary(run_id)
            if existing is not None:
                print(f"[skip] {run_id} 已完成（final={existing['final']}），幂等复用",
                      flush=True)
                continue
            _clean_run(run_id)
            try:
                preflight(make_provider(MODEL_PROVIDER))
            except SystemExit as e:
                _progress(f"{run_id} preflight-failed: {e}")   # 如实记录，不伪造
                raise
            run_one(arm, task)
    # analyze 汇总全部四个 run（含本轮复用的）
    summaries = [s for s in (_load_summary(f"{arm}-{task}")
                             for task in TASKS for arm in ("classic", "lg"))
                 if s is not None]
    report = analyze(summaries)
    print("\n==== 对照摘要 ====")
    for run_id, r in report["runs"].items():
        print(json.dumps({k: r.get(k) for k in (
            "run_id", "final", "error", "wall_seconds", "tokens_total",
            "tokens_by_role", "review_calls", "review_overlap",
            "worker_calls", "worker_overlap", "protocol_failures",
            "truncated_calls", "ladder_escalations")},
            ensure_ascii=False), flush=True)
    print("==== 交接证据 ====")
    print(json.dumps(report["handoff"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
