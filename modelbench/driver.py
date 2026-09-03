"""modelbench 驱动：thin 臂最小循环 + team 臂 dpswarm 控制面真跑。

- logpipe：产出落盘 results/<run_id>/logpipe/（LOGPIPE_UNDER_TEST 目录），
  子进程跑 experiment-01 冻结 pytest 门评分（-p no:cacheprovider，不动冻结资产）。
- widesearch：产出 results/<run_id>/submission.md，子进程调 experiment-02
  冻结评分器 evaluate2.py（只读调用；它会往 experiment-02-widesearch/results/
  写 judge_queue2_mb_<tag>.json，属允许的新增产物）。
- team 臂：ControlPlane（store 落 results/<run_id>/events.jsonl）+ Orchestrator
  （provider = LoggingProvider 包真 provider）；事件流原样归档
  logs/<run_id>.events.jsonl；Lead 协议失败（orchestrator fallback 到
  escalate/reject）由 LeadProtocolProbe 计数——不掩饰，这是 RQ3 数据。
- thin 臂不进 dpswarm 控制面（"无控制面"对照）；429 退避重试集在 driver 侧
  （对齐 orchestrator §4 纪律：1s/2s/4s ≤3 次，retry_after 优先、封顶 8s）。

口径说明：API 模型无仓库文件访问（experiment-01 原 thin 臂在 Cursor 里可读本），
因此 thin/team 的 logpipe prompt 均内联 CONTRACT.md 全文——契约是"唯一行为依据"，
不内联任务不可完成；数据文件路径保留原文引用（只读）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import providers  # noqa: F401  # 路径引导：import 时把 dpswarm-plugin 加进 sys.path
from providers import (
    GLM_WEB_SEARCH_TOOLS,
    LOGS_DIR,
    RESULTS_DIR,
    LoggingProvider,
    RouterProvider,
    log_context,
    make_provider,
)

from dpswarm.control import ControlPlane
from dpswarm.orchestrator import Orchestrator
from dpswarm.providers.base import Provider, QuotaExhausted, RateLimitBackoff
from dpswarm.routing import parse_lead_decision
from dpswarm.types import (
    Level,
    ModelCatalog,
    ModelFacts,
    ModelRoute,
    RootExecutionSpec,
)

ROOT = Path(__file__).resolve().parent.parent
EXP01 = ROOT / "experiment-01-teambench"
EXP02 = ROOT / "experiment-02-widesearch"
ACCEPTANCE_TEST = EXP01 / "tests" / "test_acceptance.py"
EVALUATE2 = EXP02 / "evaluate2.py"

# 实验映射（声明值，非 dpswarm 内置；仅作臂定义与 Lead 事实注入用）。
# 模型名经 preflight 调研核实（2026-09-03,见 modelbench/MODELS.md）：
# glm-4.5-flash 已下线（glm-4.7-flash 替代）;deepseek-chat/reasoner 已于
# 2026-07-24 停用（v4-flash/v4-pro 替代）。
MODEL_LEVELS: Dict[str, Level] = {
    "glm/glm-5.3": Level.S,
    "glm/glm-5.3-flash": Level.B,   # Coding Plan 内 GLM worker 档(不被静默切模型)
    "glm/glm-4.7": Level.A,
    "glm/glm-4.5-air": Level.B,
    "glm/glm-4.7-flash": Level.C,
    "deepseek/deepseek-v4-pro": Level.S,
    "deepseek/deepseek-v4-flash": Level.B,
}

# 价目经 2026-09-03 官方页核实（modelbench/price.yaml);GLM=CNY、DeepSeek=USD
# (off-peak)/Mtok,币种不同,成本对比在 summary 按 FX 假设归一,此处仅记账。
_MODEL_META = {
    "glm/glm-5.3":                {"ctx": 1_000_000, "in": 8.0,  "out": 28.0},
    # 5.3-flash 按量等价 ¥0.4/¥1.4(限时五折);本实验 GLM 走 Coding Plan,
    # 实际按积分抵扣(系数 Input 2.3/Cached 0.56/Output 24,price.yaml),
    # 成本对比在 summary 里按"按量等价"归一并声明口径。
    "glm/glm-5.3-flash":          {"ctx": 1_000_000, "in": 0.4,  "out": 1.4},
    "glm/glm-4.7":                {"ctx": 200_000,   "in": 2.0,  "out": 8.0},
    "glm/glm-4.5-air":            {"ctx": 128_000,   "in": 0.8,  "out": 2.0},
    "glm/glm-4.7-flash":          {"ctx": 200_000,   "in": 0.0,  "out": 0.0},
    "deepseek/deepseek-v4-pro":   {"ctx": 1_000_000, "in": 0.66, "out": 1.98},
    "deepseek/deepseek-v4-flash": {"ctx": 1_000_000, "in": 0.22, "out": 0.66},
}
_AA_BY_LEVEL = {Level.S: 9.0, Level.A: 8.0, Level.B: 7.0, Level.C: 6.0, Level.D: 5.0}

# thin 臂 429 退避序列（对齐 Orchestrator.RATE_LIMIT_BACKOFF_SECONDS §4）
_THIN_BACKOFF = (1.0, 2.0, 4.0)
_THIN_BACKOFF_CAP = 8.0


def build_catalog(model_keys: Optional[List[str]] = None) -> ModelCatalog:
    """注册模型与实验级别映射。team 臂按臂配置限制目录（model_keys），
    使 Lead 的选择空间 = 臂定义（worker 档次是受控变量）。"""
    catalog = ModelCatalog()
    for key, level in MODEL_LEVELS.items():
        if model_keys is not None and key not in model_keys:
            continue
        provider, model = key.split("/")
        meta = _MODEL_META[key]
        aa = _AA_BY_LEVEL[level]
        catalog.register(ModelFacts(
            provider, model, level,
            aa_dimensional={"coding": aa, "reasoning": aa, "overall": aa},
            aa_source="declared",  # 声明值，待 preflight 用真实快照/价目替换
            context_window=meta["ctx"],
            input_price_per_mtok=meta["in"], output_price_per_mtok=meta["out"]))
    return catalog


def level_of(provider: str, model: str) -> Level:
    return MODEL_LEVELS.get(f"{provider}/{model}", Level.B)


def _arm_of(run_id: str) -> str:
    """run_id 约定 {arm}-{task}-{ts}，取臂号。"""
    return run_id.split("-", 1)[0]


def _ensure_logging(provider: Provider, run_id: str, *, arm: str, task: str,
                    budget: Optional[int],
                    role_by_model: Optional[Dict[str, str]] = None) -> LoggingProvider:
    if isinstance(provider, LoggingProvider):
        return provider
    return LoggingProvider(provider, LOGS_DIR / f"{run_id}.jsonl",
                           run_id=run_id, arm=arm, task=task,
                           role_by_model=role_by_model, budget=budget)


# ---------------------------------------------------------------------------
# logpipe：代码提取 → 落盘 → 冻结 pytest 门
# ---------------------------------------------------------------------------

_LOGPIPE_FILES = ("ingest.py", "transform.py", "__main__.py", "__init__.py")


def extract_python_blocks(text: str) -> List[str]:
    """提取回复中的 ```python 代码块（``` 后不带语言标注也收）。"""
    return [m.group(1) for m in
            re.finditer(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", text or "", re.S)]


def map_blocks_to_files(blocks: List[str]) -> Dict[str, str]:
    """代码块 → 文件名：文件名注释优先，内容嗅探其次，顺序（ingest/transform/
    __main__/__init__）兜底。"""
    files: Dict[str, str] = {}
    unnamed: List[str] = []
    for block in blocks:
        head = "\n".join(block.splitlines()[:3])
        m = re.search(r"(?:logpipe[\\/])?(__init__|__main__|ingest|transform)\.py", head)
        if m and (m.group(1) + ".py") not in files:
            files[m.group(1) + ".py"] = block
        else:
            unnamed.append(block)
    sniff = (("def iter_records", "ingest.py"), ("def aggregate", "transform.py"),
             ("argparse", "__main__.py"))
    for block in list(unnamed):
        hit = next((name for pat, name in sniff if pat in block and name not in files), None)
        if hit:
            files[hit] = block
            unnamed.remove(block)
    for block in unnamed:
        for name in _LOGPIPE_FILES:
            if name not in files:
                files[name] = block
                break
    return files


def _write_logpipe_files(pkg_dir: Path, files: Dict[str, str]) -> None:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for name, code in files.items():
        (pkg_dir / name).write_text(code.rstrip() + "\n", encoding="utf-8")
    init = pkg_dir / "__init__.py"
    if not init.exists():
        # 模型没单独给 __init__.py 时补默认空包文件（契约要求该文件存在）
        init.write_text('"""logpipe 包（modelbench 驱动补写的默认 __init__）。"""\n',
                        encoding="utf-8")


def _int_match(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def run_acceptance(under_test_dir: Path) -> Tuple[int, int, str]:
    """子进程跑 experiment-01 冻结验收门。返回 (passed, total, 输出尾部)。"""
    env = dict(os.environ, LOGPIPE_UNDER_TEST=str(under_test_dir))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(ACCEPTANCE_TEST), "-q",
             "-p", "no:cacheprovider"],
            capture_output=True, timeout=300, env=env)
        out = (proc.stdout.decode("utf-8", errors="replace")
               + proc.stderr.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        return 0, 0, "pytest timeout(300s)"
    passed = _int_match(r"(\d+) passed", out)
    failed = _int_match(r"(\d+) failed", out)
    errors = _int_match(r"(\d+) errors?\b", out)
    return passed, passed + failed + errors, out[-2000:]


def _thin_complete(provider: Provider, route: ModelRoute, messages, tools=None,
                   max_tokens: int = 32768):
    """thin 臂传输重试集：429 按 1/2/4s 退避 ≤3 次（retry_after 优先、封顶 8s）；
    QuotaExhausted 不是背压，原样上抛（§4）。

    max_tokens=32768 的依据：thinking 模型（glm-5.3 强制 / v4 默认开）CoT 计入
    输出上限，16384 下 A1/A2 三轮全截断、正文零代码块（Phase A r1 实证）；
    thin 一次交付 4 个文件，上限按"全量交付 + CoT"定。team worker 每次只交
    1-2 个文件，用 16384（_backoff_complete）。两侧模型上限均 ≥128K，安全。"""
    for attempt in range(len(_THIN_BACKOFF) + 1):
        try:
            return provider.complete(route, messages, tools=tools, max_tokens=max_tokens)
        except RateLimitBackoff as e:
            if attempt >= len(_THIN_BACKOFF):
                raise
            delay = _THIN_BACKOFF[attempt]
            if getattr(e, "retry_after", None) is not None:
                delay = float(e.retry_after)
            time.sleep(min(max(delay, 0.0), _THIN_BACKOFF_CAP))


def _thin_logpipe_prompt(out_dir: Path) -> str:
    template = (EXP01 / "THIN_TASK.md").read_text(encoding="utf-8")
    out = out_dir.resolve().as_posix()
    prompt = template.replace(
        "在 `experiment-01-teambench/thin/` 下创建 `logpipe` 包",
        f"在 `{out}/` 下创建 `logpipe` 包")
    prompt = prompt.replace(
        "只允许写 `thin/` 下的文件", f"只允许写 `{out}/` 下的文件")
    # API 模型无仓库文件访问：CONTRACT.md 全文内联（契约是唯一行为依据）
    contract = (EXP01 / "CONTRACT.md").read_text(encoding="utf-8")
    prompt += (
        "\n\n## 接口契约（experiment-01-teambench/CONTRACT.md 全文）\n\n" + contract
        + "\n\n## 交付格式（harness 约定）\n"
          "你不直接写文件——把四个文件的完整内容各放在一个 ```python 代码块里输出，"
          "每个代码块首行注释标文件名（如 `# logpipe/ingest.py`）；"
          "harness 负责落盘并运行冻结验收门，失败输出会反馈给你修正（最多 3 轮）。")
    return prompt


def run_thin_logpipe(model: str, provider_name: str, run_id: str,
                     provider: Optional[Provider] = None,
                     budget: Optional[int] = None) -> Dict[str, Any]:
    """thin 臂 logpipe：单模型直做，≤3 轮测试反馈循环，冻结 pytest 门评分。"""
    out_dir = RESULTS_DIR / run_id / "logpipe"   # LOGPIPE_UNDER_TEST 目录
    pkg_dir = out_dir / "logpipe"
    logged = _ensure_logging(provider or make_provider(provider_name), run_id,
                             arm=_arm_of(run_id), task="logpipe", budget=budget)
    route = ModelRoute(provider_name, model, level=level_of(provider_name, model))
    messages = [{"role": "user", "content": _thin_logpipe_prompt(out_dir)}]
    passed = total = rounds = 0
    for rnd in range(1, 4):   # 首轮 + ≤2 轮反馈修正
        rounds = rnd
        with log_context(role="worker", work_item="logpipe", attempt=rnd):
            result = _thin_complete(logged, route, messages)
        files = map_blocks_to_files(extract_python_blocks(result.text))
        if files:
            _write_logpipe_files(pkg_dir, files)
        passed, total, tail = run_acceptance(out_dir)
        if total > 0 and passed == total:
            break
        if rnd < 3:
            messages += [
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": (
                    f"冻结验收门未全过（{passed}/{total}）。pytest 输出尾部：\n{tail}\n"
                    "请修正后重新输出全部四个文件（```python 代码块，首行注释标文件名）。")}]
    score = (passed / total) if total else 0.0
    out = {"score": round(score, 4), "passed": passed, "total": total,
           "rounds": rounds, "retries": rounds - 1, "protocol_failures": 0}
    if passed != total:
        out["pytest_tail"] = tail   # 失败尾部落账（失败也是数据，便于归因）
    return out


# ---------------------------------------------------------------------------
# team 臂：dpswarm 控制面真跑
# ---------------------------------------------------------------------------


class LeadProtocolProbe(Provider):
    """RQ3 观测：统计 Lead 决策/验收 JSON 协议失败次数。

    只计数、不干预——orchestrator 的 escalate/reject fallback 照常发生。
    传输层失败（stop_reason=error / 429 / 配额）不算协议失败，不计。
    """

    name = "protocol-probe"

    def __init__(self, inner: Provider, lead_route: ModelRoute) -> None:
        self.inner = inner
        self.lead_route = lead_route
        self.failures: List[Dict[str, str]] = []

    def complete(self, route: ModelRoute, messages, tools=None, max_tokens: int = 4096):
        result = self.inner.complete(route, messages, tools=tools, max_tokens=max_tokens)
        if not route.same_model(self.lead_route):
            return result
        # 只探测 Lead 协议调用面（_ask_lead/_ask_lead_review 带 Lead system
        # 消息；_run_single 的直做调用输出自由文本，不属协议调用）
        sysmsg = str(messages[0].get("content", "")) if messages else ""
        is_decision = "你是调度 Lead" in sysmsg
        is_review = "你是验收 Lead" in sysmsg
        if not (is_decision or is_review):
            return result
        if result.stop_reason.value != "completed":
            return result
        try:
            parse_lead_decision(result.text)
        except ValueError:
            self.failures.append({
                "kind": "decision" if is_decision else "review",
                "snippet": result.text[:160]})
        return result


def _backoff_complete(orch: Orchestrator, route, messages, node_id,
                      tools=None, max_tokens: int = 16384):
    """modelbench 侧退避循环（与 Orchestrator._complete_with_backoff 同纪律：
    §4 1s/2s/4s 最多 3 次、retry_after 优先、封顶 8s；QUOTA 记 aborted +
    审计后上抛），仅 tools/max_tokens 可配。不改 dpswarm 源码。

    为什么 team 臂必须抬到 16384：thinking 模型（glm-5.3 强制开 / DeepSeek v4
    默认开）的 CoT 计入输出上限，dpswarm 默认 4096 会被 CoT 吃满 → worker
    空交付、Lead 裁决 JSON 截断（Phase A 首轮 A4/A5 实证）。thin 臂本就用
    16384，team 对齐同上限才可比。"""
    for attempt in range(len(orch.RATE_LIMIT_BACKOFF_SECONDS) + 1):
        try:
            return orch.provider.complete(route, messages, tools=tools,
                                          max_tokens=max_tokens)
        except RateLimitBackoff as e:
            if attempt >= len(orch.RATE_LIMIT_BACKOFF_SECONDS):
                raise
            delay = orch.RATE_LIMIT_BACKOFF_SECONDS[attempt]
            if getattr(e, "retry_after", None) is not None:
                delay = float(e.retry_after)
            time.sleep(min(max(delay, 0.0), orch.RATE_LIMIT_BACKOFF_CAP))
        except QuotaExhausted as e:
            orch.cp.record_stop_reason(node_id, "aborted")
            orch.cp._record("stop_reason_recorded", {
                "node_id": node_id, "stop_reason": "aborted",
                "quota_exhausted": True, "detail": str(e)})
            if orch.profile is not None:
                orch.profile.record_attempt(
                    "%s/%s" % (route.provider, route.model), "overall",
                    "quota-exhausted", None, None, quota=True)
            raise


class BigTokenOrchestrator(Orchestrator):
    """logpipe team 臂：全部调用面（Lead 决策/验收 + worker）max_tokens=16384，
    与 thin 臂对齐（理由见 _backoff_complete docstring 的 Phase A 实证）。"""

    def _complete_with_backoff(self, route, messages, node_id):
        return _backoff_complete(self, route, messages, node_id,
                                 tools=None, max_tokens=16384)


class WebSearchOrchestrator(Orchestrator):
    """widesearch team 臂：worker 的 complete 透传服务端 web_search 工具
    （modelbench 侧子类化，不动 dpswarm 源码）。

    worker 判定：node_id != root lead node——覆盖 _run_worker 与 _review 重试
    两类 worker 调用面；Lead 调用无工具，但同样抬到 16384（thinking 截断
    风险同 logpipe 臂，见 _backoff_complete docstring）。
    """

    def __init__(self, *args, worker_tools=None, worker_tool_providers=("glm",),
                 worker_max_tokens: int = 16384, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 配置口：preflight 若实测工具格式不同，改 worker_tools 即可
        self._worker_tools = GLM_WEB_SEARCH_TOOLS if worker_tools is None else worker_tools
        self._worker_tool_providers = set(worker_tool_providers)
        self._worker_max_tokens = worker_max_tokens

    def _complete_with_backoff(self, route, messages, node_id):
        if node_id == self.cp.root_lead_node:
            # Lead 无工具，但同抬 16384（thinking 截断风险与 logpipe 臂相同）
            return _backoff_complete(self, route, messages, node_id,
                                     tools=None, max_tokens=16384)
        # DeepSeek 无服务端 web_search → tools=None（闭卷，B3 参考臂口径）
        tools = self._worker_tools if route.provider in self._worker_tool_providers else None
        return _backoff_complete(self, route, messages, node_id,
                                 tools=tools, max_tokens=self._worker_max_tokens)


def _router_for(cfg: Dict[str, Any]) -> Provider:
    """team 臂真实 provider 装配：按臂配置涉及的几家厂商接线。"""
    names = {cfg["lead"][0], cfg["worker"][0]}
    if cfg.get("cm"):
        names.add(cfg["cm"][0])
    providers = {n: make_provider(n) for n in sorted(names)}
    if len(providers) == 1:
        return next(iter(providers.values()))
    return RouterProvider(providers)


def _collect_accepted_texts(events, artifacts_dir: Path) -> List[str]:
    """从控制面收集 accepted 的 worker 产出：work_item_accepted 的 item 的
    证据包正文（package_stored 的 artifact_ref → artifacts/<sha>.txt）。

    只收证据包（store_evidence_package，无 stage 字段）；submit 阶段的包
    （stage="submission"，含被打回的历史版本）不收。按 content_hash 去重。
    """
    accepted = {e.payload.get("item_id") for e in events
                if e.kind == "work_item_accepted"}
    texts: List[str] = []
    seen_hash = set()
    for e in events:
        if e.kind != "package_stored":
            continue
        p = e.payload
        if p.get("item_id") not in accepted or p.get("stage") == "submission":
            continue
        digest = p.get("content_hash")
        ref = p.get("artifact_ref")
        if not digest or digest in seen_hash or not ref:
            continue
        f = artifacts_dir / ref
        if f.exists():
            seen_hash.add(digest)
            texts.append(f.read_text(encoding="utf-8"))
    return texts


def _team_run(cfg: Dict[str, Any], run_id: str, task_text: str, *,
              provider: Optional[Provider], budget: Optional[int],
              widesearch: bool) -> Dict[str, Any]:
    """team 臂公共底座：控制面 + 编排器真跑，归档事件流，收集 accepted 产出。

    BudgetExceeded 等异常：事件流在 finally 语义下照常归档后再上抛。
    """
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    lead_p, lead_m = cfg["lead"]
    worker_key = "%s/%s" % cfg["worker"]
    model_keys = ["%s/%s" % (lead_p, lead_m), worker_key]
    if cfg.get("cm"):
        model_keys.append("%s/%s" % cfg["cm"])
    catalog = build_catalog(model_keys)
    # 容量是归一化条件（非自变量）：放寛到全臂拟合同一目标拓扑（3 并发 worker）。
    # 点数权重 = ctx//64k（routing.py 机制）：DeepSeek v4 系 1M ctx → 权重 15，
    # 3 worker = 45 点 + lead 1 ≤ 96；裂变子 Team 帽 floor(96×0.5)=48 ≥ 45。
    # （旧值 16 是按 128K worker（权重 2）定的，v4 系刷新后 TEAM_POINT_CAP 全拒，
    # A5/A6 mock 实证：3 item 创建后全部 admission-rejected → deps-stuck → 封存。）
    cp = ControlPlane(
        spec=RootExecutionSpec(max_open_work_items=8, max_active_node_points=96,
                               max_team_workers=4),
        store_path=run_dir / "events.jsonl",
        catalog=catalog)
    lead_route = ModelRoute(lead_p, lead_m, level=level_of(lead_p, lead_m))
    inner = provider or _router_for(cfg)
    # role 归集只标 lead，其余默认 worker：CM 未接线（无 assembler/memory 时
    # orchestrator 不触发 CM 调用），且 A5 的 CM 与 worker 同模型，把 cm 模型
    # 映射进 role_by_model 会把 worker 调用误记为 cm。
    role_by_model = {lead_m: "lead"}
    logged = _ensure_logging(inner, run_id, arm=cfg["arm"], task=cfg["task"],
                             budget=budget, role_by_model=role_by_model)
    probe = LeadProtocolProbe(logged, lead_route)
    orch_cls = WebSearchOrchestrator if widesearch else BigTokenOrchestrator
    orch = orch_cls(cp, probe, store_dir=run_dir / "packages", lead_route=lead_route)
    outcome: Dict[str, Any] = {}
    error: Optional[BaseException] = None
    try:
        outcome = orch.run_task(task_text)
    except Exception as e:   # 不吞：归档后原样上抛给 run_matrix 记账
        error = e
    events = cp.store.read_all()
    cp.close()   # 释放写者文件锁（Windows），再归档
    ev_path = run_dir / "events.jsonl"
    if ev_path.exists():
        shutil.copyfile(ev_path, LOGS_DIR / f"{run_id}.events.jsonl")
    if error is not None:
        raise error
    return {
        "outcome": outcome,
        "dpswarm_final": outcome.get("final"),
        "accepted_texts": _collect_accepted_texts(events, run_dir / "artifacts"),
        "retries": sum(1 for e in events if e.kind == "work_item_retried"),
        "protocol_failures": len(probe.failures),
        "protocol_failure_detail": probe.failures[:10],
    }


def _team_logpipe_task() -> str:
    contract = (EXP01 / "CONTRACT.md").read_text(encoding="utf-8")
    return (
        "构建 Python 命令行工具 logpipe：读入服务访问日志（JSONL，混有脏数据）→ "
        "清洗校验 → 聚合统计 → 输出机器可读 JSON 与人读报告。\n\n"
        "## 冻结接口契约（experiment-01-teambench/CONTRACT.md 全文，唯一行为依据）\n\n"
        + contract +
        "\n\n## 分工提示（建议 fission 三分工，对应契约 WI-1/2/3）\n"
        "- WI-1：logpipe/ingest.py — iter_records 清洗校验\n"
        "- WI-2：logpipe/transform.py — aggregate 聚合统计\n"
        "- WI-3：logpipe/__main__.py + logpipe/__init__.py — CLI 胶水"
        "（禁止复制清洗/聚合逻辑，必须调用 logpipe.ingest / logpipe.transform）\n"
        "每个 worker 用 ```python 代码块交付完整文件，首行注释标文件名"
        "（如 `# logpipe/ingest.py`）。\n"
        "数据文件：experiment-01-teambench/data/golden_logs.jsonl（约 5150 行，只读）。\n"
        "全部子任务验收通过后，Lead 决策 accept 收束。")


def run_team_logpipe(cfg: Dict[str, Any], run_id: str,
                     provider: Optional[Provider] = None,
                     budget: Optional[int] = None) -> Dict[str, Any]:
    """team 臂 logpipe：dpswarm 真跑 → 收 accepted 产出 → 落盘 → pytest 评分。"""
    cfg = dict(cfg, task="logpipe")
    r = _team_run(cfg, run_id, _team_logpipe_task(),
                  provider=provider, budget=budget, widesearch=False)
    out_dir = RESULTS_DIR / run_id / "logpipe"
    blocks: List[str] = []
    for text in r["accepted_texts"]:
        blocks.extend(extract_python_blocks(text))
    files = map_blocks_to_files(blocks)
    if files:
        _write_logpipe_files(out_dir / "logpipe", files)
    passed, total, tail = run_acceptance(out_dir)
    score = (passed / total) if total else 0.0
    r.update(score=round(score, 4), passed=passed, total=total)
    if passed != total:
        r["pytest_tail"] = tail
    return r


# ---------------------------------------------------------------------------
# widesearch：thin 直答 / team 编排 → submission.md → evaluate2 评分
# ---------------------------------------------------------------------------


def _ws_query(task_id: str) -> str:
    with open(EXP02 / "data" / "widesearch.jsonl", encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            if t["instance_id"] == task_id:
                return t["query"]
    raise ValueError(f"widesearch 题 {task_id!r} 不在 data/widesearch.jsonl")


def run_evaluate2(task_id: str, submission_path: Path, tag: str) -> Dict[str, Any]:
    """子进程调 experiment-02 冻结评分器（只读调用；新增产物
    judge_queue2_<tag>.json 落在 experiment-02-widesearch/results/，允许）。"""
    try:
        proc = subprocess.run(
            [sys.executable, str(EVALUATE2), task_id, str(submission_path), tag],
            capture_output=True, timeout=600, cwd=str(EXP02))
    except subprocess.TimeoutExpired:
        return {"item_f1": 0.0, "row_f1": 0.0, "eval_error": "evaluate2 timeout(600s)"}
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        return {"item_f1": 0.0, "row_f1": 0.0,
                "eval_error": (err or out)[-500:]}
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        return {"item_f1": 0.0, "row_f1": 0.0, "eval_error": out[-500:]}


def _is_separator(cells: List[str]) -> bool:
    nonempty = [c for c in cells if c != ""]
    return bool(nonempty) and all(re.fullmatch(r":?-+:?", c) for c in nonempty)


def _merge_markdown_tables(texts: List[str]) -> str:
    """把多个 worker 的 markdown 表格片段合成一张表：首个表头为准，
    后续片段的表头/分隔行丢弃，数据行按原文去重追加。"""
    header: Optional[str] = None
    rows: List[str] = []
    seen = set()
    for text in texts:
        for line in (text or "").splitlines():
            s = line.strip()
            if not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if _is_separator(cells):
                continue
            if header is None:
                header = s
                continue
            if s == header or s in seen:
                continue
            seen.add(s)
            rows.append(s)
    if header is None:
        return ""
    ncol = len(header.strip("|").split("|"))
    sep = "|" + "---|" * ncol
    return "\n".join([header, sep] + rows) + "\n"


def run_thin_widesearch(model: str, provider_name: str, task_id: str, run_id: str,
                        provider: Optional[Provider] = None,
                        budget: Optional[int] = None) -> Dict[str, Any]:
    """thin 臂 widesearch：单模型带 web_search 工具（GLM 服务端）直答。"""
    query = _ws_query(task_id)
    logged = _ensure_logging(provider or make_provider(provider_name), run_id,
                             arm=_arm_of(run_id), task=task_id, budget=budget)
    route = ModelRoute(provider_name, model, level=level_of(provider_name, model))
    tools = GLM_WEB_SEARCH_TOOLS if provider_name == "glm" else None
    with log_context(role="worker", work_item=task_id, attempt=1):
        result = _thin_complete(logged, route, [{"role": "user", "content": query}],
                                tools=tools, max_tokens=16384)
    sub_path = RESULTS_DIR / run_id / "submission.md"
    sub_path.parent.mkdir(parents=True, exist_ok=True)
    sub_path.write_text(result.text, encoding="utf-8")
    scores = run_evaluate2(task_id, sub_path, f"mb_{_arm_of(run_id)}_{task_id}")
    return {"score": scores.get("item_f1", 0.0), "item_f1": scores.get("item_f1", 0.0),
            "row_f1": scores.get("row_f1", 0.0), "eval": scores,
            "retries": 0, "protocol_failures": 0}


def run_team_widesearch(cfg: Dict[str, Any], run_id: str,
                        provider: Optional[Provider] = None,
                        budget: Optional[int] = None) -> Dict[str, Any]:
    """team 臂 widesearch：WebSearchOrchestrator（worker 透传 GLM web_search，
    worker max_tokens≥16384），accepted 片段合并成表后评分。"""
    task_id = cfg["task_id"]
    cfg = dict(cfg, task=task_id)
    query = _ws_query(task_id)
    task_text = (
        query +
        "\n\n## 分工提示\n"
        "可按子主题/时间片裂变并行检索（worker 已挂载服务端 web_search 工具）；"
        "每个 worker 交付 markdown 表格片段（含表头行，列结构与题目要求一致）；"
        "harness 会把各 accepted 片段合并成一张表送冻结评分器。")
    r = _team_run(cfg, run_id, task_text,
                  provider=provider, budget=budget, widesearch=True)
    merged = _merge_markdown_tables(r["accepted_texts"])
    sub_path = RESULTS_DIR / run_id / "submission.md"
    sub_path.parent.mkdir(parents=True, exist_ok=True)
    # 合并不出表格时原样拼接兜底（评分器只认 | 行，仍能抽出片段）
    sub_path.write_text(merged or "\n\n".join(r["accepted_texts"]), encoding="utf-8")
    scores = run_evaluate2(task_id, sub_path, f"mb_{cfg['arm']}_{task_id}")
    r.update(score=scores.get("item_f1", 0.0), item_f1=scores.get("item_f1", 0.0),
             row_f1=scores.get("row_f1", 0.0), eval=scores)
    return r
