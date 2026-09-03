"""modelbench 矩阵跑批：臂定义 A1-A6（logpipe）/ B1-B3（widesearch，见计划 §2）。

用法：
  python modelbench/run_matrix.py --mock --phase A      # Mock 干跑（零 token）
  python modelbench/run_matrix.py --mock --phase B
  python modelbench/run_matrix.py --phase A --arm A1,A4
  python modelbench/run_matrix.py --phase B --parallel-by-provider

- --parallel-by-provider：同一 provider 内串行，GLM / DeepSeek / 混编臂
  分组用 threading 并行（每个 run 独立 run_id、独立目录，天然隔离）。
- run 级 token 护栏：LoggingProvider 启动时先记录该 run 日志已有总量，
  run 中累计超 RUN_TOKEN_BUDGET（默认 1_500_000，环境变量可覆盖）即在
  下次调用前中止，记 budget-abort。
- --mock：thin 臂用 AtomicMockProvider 顺序脚本（单条产出）；team 臂用
  ResponsiveMockProvider 按消息内容分发 Lead 决策/worker 产出/验收裁决
  （worker 线程池下顺序脚本会失序），验证全管线不烧真 token。
- 每 run 结束追加 results/runs.jsonl；跑批结束自动汇总 results/summary.md。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MB_DIR = Path(__file__).resolve().parent
if str(MB_DIR) not in sys.path:
    sys.path.insert(0, str(MB_DIR))   # 允许 python modelbench/run_matrix.py 直跑

import driver
from providers import AtomicMockProvider, BudgetExceeded, LOGS_DIR, RESULTS_DIR

# ---------------------------------------------------------------------------
# 臂定义（计划 §2）
# ---------------------------------------------------------------------------

ARMS: Dict[str, Dict[str, Any]] = {
    # Phase A：logpipe（无 web 依赖；评分 = 冻结 pytest 门 5 例）
    "A1": {"phase": "A", "kind": "thin-logpipe",
           "provider": "glm", "model": "glm-5.3"},
    "A2": {"phase": "A", "kind": "thin-logpipe",
           "provider": "deepseek", "model": "deepseek-v4-flash"},
    "A3": {"phase": "A", "kind": "thin-logpipe",
           "provider": "deepseek", "model": "deepseek-v4-pro"},
    "A4": {"phase": "A", "kind": "team-logpipe",      # GLM 同构（两层家族）
           # Coding Plan 约束：套餐内只有 glm-5.3 / glm-5.3-flash 不被静默
           # 切模型（MODELS.md §1.4），GLM 侧 worker 档只能是 5.3-flash。
           "lead": ("glm", "glm-5.3"), "worker": ("glm", "glm-5.3-flash"),
           "cm": ("glm", "glm-5.3-flash")},
    "A5": {"phase": "A", "kind": "team-logpipe",      # DeepSeek 全家桶（同构）
           "lead": ("deepseek", "deepseek-v4-pro"),
           "worker": ("deepseek", "deepseek-v4-flash"),
           "cm": ("deepseek", "deepseek-v4-flash")},
    "A6": {"phase": "A", "kind": "team-logpipe",      # 跨厂混编（异构）
           "lead": ("glm", "glm-5.3"), "worker": ("deepseek", "deepseek-v4-flash"),
           "cm": ("glm", "glm-5.3-flash")},
    # Phase B：widesearch（GLM web_search；B3 为 DeepSeek 闭卷参考臂）
    "B1": {"phase": "B", "kind": "thin-widesearch",
           "provider": "glm", "model": "glm-5.3"},
    "B2": {"phase": "B", "kind": "team-widesearch",   # GLM 同构（两层家族，同上约束）
           "lead": ("glm", "glm-5.3"), "worker": ("glm", "glm-5.3-flash")},
    "B3": {"phase": "B", "kind": "team-widesearch",   # 异构：worker 闭卷
           "lead": ("glm", "glm-5.3"), "worker": ("deepseek", "deepseek-v4-flash")},
}

# 计划 §2：ws_zh_079（既往团队臂唯一明显胜场）+ ws_zh_004（既往单臂胜场）
WS_TASKS = ["ws_zh_079", "ws_zh_004"]

RUN_TOKEN_BUDGET = int(os.environ.get("RUN_TOKEN_BUDGET", "1500000"))
RUNS_JSONL = RESULTS_DIR / "runs.jsonl"
SUMMARY_MD = RESULTS_DIR / "summary.md"

# ---------------------------------------------------------------------------
# Mock 载荷：一个能过 experiment-01 冻结验收门的最简 logpipe 实现
# （语义按 CONTRACT.md；仅作管线验证，不构成模型能力证据）
# ---------------------------------------------------------------------------

_MOCK_INGEST = '''# logpipe/ingest.py
"""WI-1：日志摄取与清洗校验（mock 最简实现，语义按 CONTRACT.md §2/§3）。"""
import json
from datetime import datetime

REQUIRED = ("event_id", "ts", "service", "endpoint", "status",
            "latency_ms", "user_id")
QUARANTINE_KEYS = ("invalid_json", "missing_field", "invalid_timestamp",
                   "invalid_status", "negative_latency", "duplicate_event_id")
_TS = "%Y-%m-%dT%H:%M:%SZ"


def _valid_ts(v):
    if not isinstance(v, str):
        return False
    try:
        parsed = datetime.strptime(v, _TS)
    except ValueError:
        return False
    # strptime 对未补零月份较宽松；格式化回绕保证与契约格式逐字一致
    return parsed.strftime(_TS) == v


def iter_records(path):
    clean = []
    quarantine = {k: 0 for k in QUARANTINE_KEYS}
    seen = set()
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                quarantine["invalid_json"] += 1
                continue
            if not isinstance(obj, dict):
                quarantine["invalid_json"] += 1
                continue
            if any(k not in obj for k in REQUIRED):
                quarantine["missing_field"] += 1
                continue
            if not _valid_ts(obj["ts"]):
                quarantine["invalid_timestamp"] += 1
                continue
            status = obj["status"]
            if type(status) is not int or not (100 <= status <= 599):
                quarantine["invalid_status"] += 1
                continue
            lat = obj["latency_ms"]
            if type(lat) not in (int, float) or lat < 0:
                quarantine["negative_latency"] += 1
                continue
            if obj["event_id"] in seen:
                quarantine["duplicate_event_id"] += 1
                continue
            seen.add(obj["event_id"])   # 只有干净记录占用去重空间（§2 注意）
            clean.append(obj)
    return clean, quarantine
'''

_MOCK_TRANSFORM = '''# logpipe/transform.py
"""WI-2：聚合统计（nearest-rank 百分位，CONTRACT.md §4）。"""
import math


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    rank = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[rank - 1]


def _group(records):
    n = len(records)
    if n == 0:
        return {"requests": 0, "error_rate": 0.0,
                "p50_ms": 0, "p95_ms": 0, "p99_ms": 0}
    errors = sum(1 for r in records if r["status"] >= 500)
    lats = sorted(r["latency_ms"] for r in records)
    return {"requests": n, "error_rate": round(errors / n, 4),
            "p50_ms": _percentile(lats, 50), "p95_ms": _percentile(lats, 95),
            "p99_ms": _percentile(lats, 99)}


def aggregate(records):
    if not records:
        return {"services": {}, "per_minute": {},
                "totals": {"requests": 0, "error_rate": 0.0,
                           "p50_ms": 0, "p95_ms": 0, "p99_ms": 0}}
    by_service = {}
    for r in records:
        by_service.setdefault(r["service"], []).append(r)
    per_minute = {}
    for r in records:
        key = r["ts"][:16]
        per_minute[key] = per_minute.get(key, 0) + 1
    return {"services": {name: _group(by_service[name])
                         for name in sorted(by_service)},
            "per_minute": per_minute, "totals": _group(records)}
'''

_MOCK_MAIN = '''# logpipe/__main__.py
"""WI-3：CLI 入口（只写胶水，清洗/聚合调 logpipe.ingest / logpipe.transform）。"""
import argparse
import json
import sys

from logpipe.ingest import iter_records
from logpipe.transform import aggregate


def main(argv=None):
    ap = argparse.ArgumentParser(prog="logpipe")
    ap.add_argument("input")
    ap.add_argument("--json", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args(argv)
    try:
        clean, quarantine = iter_records(args.input)
        stats = aggregate(clean)
    except (OSError, UnicodeError, ValueError) as e:
        print("logpipe: %s" % e, file=sys.stderr)
        return 1
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump({"quarantine": quarantine, "stats": stats}, fh,
                  ensure_ascii=False, indent=2)
    lines = ["# logpipe 报告", "",
             "| service | requests | error_rate | p50_ms | p95_ms | p99_ms |",
             "|---|---|---|---|---|---|"]
    for name, s in stats["services"].items():
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            name, s["requests"], s["error_rate"],
            s["p50_ms"], s["p95_ms"], s["p99_ms"]))
    t = stats["totals"]
    lines += ["", "totals: requests=%s, error_rate=%s, p50_ms=%s, p95_ms=%s, "
              "p99_ms=%s" % (t["requests"], t["error_rate"],
                             t["p50_ms"], t["p95_ms"], t["p99_ms"])]
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write("\\n".join(lines) + "\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_MOCK_INIT = '''# logpipe/__init__.py
"""logpipe 包。"""
'''


def _code_block(code: str) -> str:
    return "```python\n" + code.rstrip() + "\n```"


# widesearch mock：直接产出小表格（仅验证管线，分数无意义）
_MOCK_WS_TABLE_A = (
    "分片一完成：\n\n"
    "| 项目 | 数值 |\n|---|---|\n| 示例甲 | 100 |\n| 示例乙 | 200 |")
_MOCK_WS_TABLE_B = (
    "分片二完成：\n\n"
    "| 项目 | 数值 |\n|---|---|\n| 示例丙 | 300 |")


def _mock_script(cfg: Dict[str, Any], task_id: str) -> List[Dict[str, Any]]:
    """thin 臂 MockProvider 脚本（单条产出；thin 无并发，顺序回放安全）。
    team 臂走 ResponsiveMockProvider——顺序脚本在 worker 线程池下失序。"""
    kind = cfg["kind"]
    if kind == "thin-logpipe":
        text = "\n\n".join(_code_block(c) for c in
                           (_MOCK_INGEST, _MOCK_TRANSFORM, _MOCK_MAIN, _MOCK_INIT))
        return [{"text": text, "usage": {"input": 3000, "output": 2200}}]
    if kind == "thin-widesearch":
        return [{"text": _MOCK_WS_TABLE_A, "usage": {"input": 500, "output": 300}}]
    raise ValueError(f"team 臂 {kind!r} 不用顺序脚本，走 ResponsiveMockProvider")


class ResponsiveMockProvider(AtomicMockProvider):
    """team 臂 mock：按消息内容分发而非顺序脚本（线程池下调用序不可假设）。

    顺序脚本假设"决策→N worker→N 验收→accept"的调用序，但 orchestrator 在
    worker future 完成时即验收，线程池下调用交错（w1,r1,w2,r2…），Lead 决策点
    会拿到 worker 文本 → 解析失败 → fallback escalate 全树（A5 事件流实证）。
    此处按 prompt 标记分发：
    - 含验收裁决模板字面量（orchestrator._ask_lead_review）→ accept verdict；
    - 含 Lead 决策协议段（routing.LEAD_DECISION_PROMPT）→ 首次 fission（路由
      取臂配置 worker），后续 accept 收束；
    - 其余为 worker 调用：按子任务标题中的文件名/分片号分发对应产出，
      标记未命中时轮转兜底（管线验证完整性优先）。
    """

    _REVIEW_MARK = '输出 JSON：{"verdict"'      # orchestrator.py _ask_lead_review
    _DECISION_MARK = "决策协议"                    # routing.py LEAD_DECISION_PROMPT

    def __init__(self, cfg: Dict[str, Any]) -> None:
        super().__init__([])
        if cfg["kind"] == "team-logpipe":
            self._worker_texts = [
                "WI-1 完成：\n" + _code_block(_MOCK_INGEST),
                "WI-2 完成：\n" + _code_block(_MOCK_TRANSFORM),
                "WI-3 完成：\n" + _code_block(_MOCK_MAIN) + "\n"
                + _code_block(_MOCK_INIT),
            ]
            self._worker_marks = ["ingest.py", "transform.py", "__main__.py"]
            subtasks = ["WI-1：实现 logpipe/ingest.py（清洗校验）",
                        "WI-2：实现 logpipe/transform.py（聚合统计）",
                        "WI-3：实现 logpipe/__main__.py 与 logpipe/__init__.py"
                        "（CLI 胶水）"]
        else:  # team-widesearch
            self._worker_texts = [_MOCK_WS_TABLE_A, _MOCK_WS_TABLE_B]
            self._worker_marks = ["分片一", "分片二"]
            subtasks = ["分片一：前半范围检索并出表", "分片二：后半范围检索并出表"]
        worker_provider, worker_model = cfg["worker"]
        self._fission = json.dumps(
            {"action": "fission",
             "route": {"provider": worker_provider, "model": worker_model},
             "subtasks": subtasks}, ensure_ascii=False)
        self._accept = json.dumps(
            {"action": "accept", "verdict_reason": "全部分片验收通过"},
            ensure_ascii=False)
        self._verdict = json.dumps(
            {"verdict": "accept", "verdict_reason": "mock 验收通过"},
            ensure_ascii=False)
        self._decision_count = 0
        self._worker_count = 0

    def complete(self, route, messages, tools=None, max_tokens: int = 4096):
        import copy
        content = "\n".join(str(m.get("content", "")) for m in (messages or []))
        with self._mock_lock:
            self.calls.append({
                "route": route,
                "messages": [dict(m) for m in (messages or [])],
                "tools": None, "max_tokens": max_tokens, "script_index": -1})
            if self._REVIEW_MARK in content:
                step = {"text": self._verdict, "usage": {"input": 600, "output": 40}}
            elif self._DECISION_MARK in content:
                self._decision_count += 1
                if self._decision_count == 1:
                    step = {"text": self._fission,
                            "usage": {"input": 900, "output": 150}}
                else:
                    step = {"text": self._accept,
                            "usage": {"input": 1000, "output": 50}}
            else:
                text = None
                for mark, candidate in zip(self._worker_marks, self._worker_texts):
                    if mark in content:
                        text = candidate
                        break
                if text is None:
                    text = self._worker_texts[
                        self._worker_count % len(self._worker_texts)]
                self._worker_count += 1
                step = {"text": text, "usage": {"input": 2200, "output": 700}}
            return copy.deepcopy(self._materialize(step))


# ---------------------------------------------------------------------------
# 跑批
# ---------------------------------------------------------------------------


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def _provider_family(cfg: Dict[str, Any]) -> str:
    """臂的厂商分组：glm / deepseek / mixed（并行分组键）。"""
    names = set()
    if cfg.get("provider"):
        names.add(cfg["provider"])
    for key in ("lead", "worker", "cm"):
        v = cfg.get(key)
        if v:
            names.add(v[0])
    if names == {"glm"}:
        return "glm"
    if names == {"deepseek"}:
        return "deepseek"
    return "mixed"


def _token_split(run_id: str):
    """从该 run 的调用日志按 role 归集 token（lead/worker/cm 分账）。"""
    path = LOGS_DIR / f"{run_id}.jsonl"
    by_role: Dict[str, int] = {}
    total = 0
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                usage = entry.get("usage") or {}
                t = sum(int(usage.get(k) or 0)
                        for k in ("input", "output", "cache_read", "cache_write"))
                if t <= 0:
                    continue
                role = entry.get("role") or "unknown"
                by_role[role] = by_role.get(role, 0) + t
                total += t
    return by_role, total


def run_one(arm_id: str, task_id: str, mock: bool, budget: int,
            io_lock: threading.Lock) -> Dict[str, Any]:
    """执行单个 run 并记账到 results/runs.jsonl（异常不炸穿跑批，记 error）。"""
    cfg = ARMS[arm_id]
    run_id = f"{arm_id}-{task_id}-{time.strftime('%Y%m%d_%H%M%S')}"
    if mock:
        provider = (ResponsiveMockProvider(cfg)
                    if cfg["kind"].startswith("team")
                    else AtomicMockProvider(_mock_script(cfg, task_id)))
    else:
        provider = None
    rec: Dict[str, Any] = {
        "run_id": run_id, "arm": arm_id, "task": task_id, "mock": mock,
        "cfg": _jsonable(cfg), "score": 0.0, "tokens_total": 0,
        "tokens_by_role": {}, "wall_s": None, "retries": 0,
        "protocol_failures": 0, "error": None,
    }
    t0 = time.time()
    try:
        kind = cfg["kind"]
        if kind == "thin-logpipe":
            r = driver.run_thin_logpipe(cfg["model"], cfg["provider"], run_id,
                                        provider=provider, budget=budget)
        elif kind == "team-logpipe":
            r = driver.run_team_logpipe({**cfg, "arm": arm_id}, run_id,
                                        provider=provider, budget=budget)
        elif kind == "thin-widesearch":
            r = driver.run_thin_widesearch(cfg["model"], cfg["provider"], task_id,
                                           run_id, provider=provider, budget=budget)
        else:
            r = driver.run_team_widesearch(
                {**cfg, "arm": arm_id, "task_id": task_id}, run_id,
                provider=provider, budget=budget)
        rec["score"] = r.get("score", 0.0)
        rec["retries"] = r.get("retries", 0)
        rec["protocol_failures"] = r.get("protocol_failures", 0)
        actions = (r.get("outcome") or {}).get("actions")
        if actions:
            rec["actions"] = actions   # team 臂 Lead 决策序列（复盘硬准入/收束依据）
        for key in ("passed", "total", "rounds", "item_f1", "row_f1", "dpswarm_final"):
            if key in r:
                rec[key] = r[key]
        if r.get("protocol_failure_detail"):
            rec["protocol_failure_detail"] = r["protocol_failure_detail"]
        eval_info = r.get("eval") or {}
        if eval_info.get("eval_error"):
            rec["error"] = "eval-error: " + str(eval_info["eval_error"])[:200]
    except BudgetExceeded as e:
        rec["error"] = f"budget-abort({e})"
    except Exception as e:   # 失败也是数据：记账后继续跑批
        rec["error"] = f"{type(e).__name__}: {e}"[:400]
    rec["wall_s"] = round(time.time() - t0, 1)
    rec["tokens_by_role"], rec["tokens_total"] = _token_split(run_id)
    with io_lock:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(RUNS_JSONL, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with io_lock:
        print(f"[{arm_id}/{task_id}] run={run_id} score={rec['score']} "
              f"tokens={rec['tokens_total']} wall={rec['wall_s']}s "
              f"retries={rec['retries']} proto_fail={rec['protocol_failures']} "
              f"err={rec['error']}", flush=True)
    return rec


def write_summary() -> None:
    """汇总 results/runs.jsonl → results/summary.md（臂 × 分数/token 分账/墙钟/
    重试/协议失败 表）。"""
    if not RUNS_JSONL.exists():
        return
    rows = []
    n_superseded = 0
    with open(RUNS_JSONL, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                if r.get("superseded"):
                    n_superseded += 1   # 被复跑取代的轮次（如 r1 max_tokens=4096
                    continue            # 截断事故轮）保留在 runs.jsonl 作 RQ3 证据
                rows.append(r)
    lines = [
        "# modelbench 汇总",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}；run 数：{len(rows)}"
        f"（另 {n_superseded} 条 superseded 轮次见 runs.jsonl）",
        "",
        "| 臂 | 任务 | run_id | 分数 | tokens(总) | lead | worker | cm |"
        " 墙钟s | 重试 | 协议失败 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (x["arm"], x["task"], x["run_id"])):
        by = r.get("tokens_by_role") or {}
        note = []
        if r.get("mock"):
            note.append("mock")
        if r.get("dpswarm_final"):
            note.append(f"final={r['dpswarm_final']}")
        if r.get("error"):
            note.append(str(r["error"])[:80])
        lines.append(
            "| {arm} | {task} | {rid} | {score} | {tot} | {lead} | {worker} |"
            " {cm} | {wall} | {retries} | {pf} | {note} |".format(
                arm=r["arm"], task=r["task"], rid=r["run_id"],
                score=r.get("score"), tot=r.get("tokens_total", 0),
                lead=by.get("lead", 0), worker=by.get("worker", 0),
                cm=by.get("cm", 0), wall=r.get("wall_s"),
                retries=r.get("retries", 0), pf=r.get("protocol_failures", 0),
                note=";".join(note)))
    lines += [
        "",
        "## 口径说明",
        "- 分数：logpipe = 冻结 pytest 门 passed/total；widesearch = evaluate2 item_f1。",
        "- tokens 分账按调用日志 role 归集（team 臂 lead/worker 由模型名映射；"
        "CM 未接线（orchestrator 无 assembler/memory 时不触发），cm 列恒 0）。",
        "- 协议失败 = Lead 决策/验收 JSON 不可解析次数（orchestrator 已按机制"
        " fallback 到 escalate/reject，不掩饰；RQ3 数据）。",
        "- 重试：thin 臂 = 测试反馈轮次 - 1；team 臂 = work_item_retried 事件数。",
        "- mock 行仅验证管线（日志/评分/控制面链路），不构成模型能力证据。",
        "- 容量归一化：team 臂 spec max_active_node_points=96（子 Team 帽 48），"
        "使 1M-ctx 的 DeepSeek v4 系（点数权重 15）与 128K 的 GLM worker（权重 2）"
        "都能跑满 3 并发 worker——容量不是自变量。",
    ]
    SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"summary: {SUMMARY_MD}", flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="modelbench.run_matrix")
    ap.add_argument("--phase", choices=["A", "B"], required=True)
    ap.add_argument("--arm", default=None,
                    help="逗号分隔臂号（如 A1,A4）；缺省 = 该 phase 全部臂")
    ap.add_argument("--mock", action="store_true",
                    help="MockProvider 干跑：不烧真 token，验证全管线")
    ap.add_argument("--parallel-by-provider", action="store_true",
                    help="同一 provider 内串行，GLM/DeepSeek/混编分组并行(threading)")
    ap.add_argument("--budget", type=int, default=RUN_TOKEN_BUDGET,
                    help="run 级 token 护栏（默认 1500000，env RUN_TOKEN_BUDGET 可改）")
    args = ap.parse_args(argv)

    if args.arm:
        selected = [a.strip() for a in args.arm.split(",") if a.strip()]
        for a in selected:
            if a not in ARMS:
                ap.error(f"未知臂 {a!r}（可选：{','.join(sorted(ARMS))}）")
            if ARMS[a]["phase"] != args.phase:
                ap.error(f"臂 {a} 属 phase {ARMS[a]['phase']}，与 --phase {args.phase} 不符")
    else:
        selected = [a for a in ARMS if ARMS[a]["phase"] == args.phase]

    def tasks_of(arm: str) -> List[str]:
        return ["logpipe"] if ARMS[arm]["phase"] == "A" else WS_TASKS

    io_lock = threading.Lock()

    def run_arm(arm: str) -> None:
        for task_id in tasks_of(arm):
            run_one(arm, task_id, args.mock, args.budget, io_lock)

    print(f"phase={args.phase} arms={','.join(selected)} mock={args.mock} "
          f"budget={args.budget} parallel={args.parallel_by_provider}", flush=True)
    if args.parallel_by_provider:
        groups: Dict[str, List[str]] = {}
        for arm in selected:
            groups.setdefault(_provider_family(ARMS[arm]), []).append(arm)
        threads = []
        for family, arms in sorted(groups.items()):
            def _group_run(arms=arms):
                for arm in arms:
                    run_arm(arm)
            threads.append(threading.Thread(target=_group_run, name=family))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for arm in selected:
            run_arm(arm)
    write_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
