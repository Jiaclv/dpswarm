"""modelbench provider 配置与调用级日志（观测口径对齐 dpswarm §4）。

- ``make_provider("glm"|"deepseek")``：env 驱动（GLM_API_KEY / GLM_BASE_URL、
  DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL），返回 dpswarm 的 OpenAICompatProvider。
  dpswarm 是被测对象：此处只把 dpswarm-plugin 加进 sys.path 只读引用，不安装、不改源码。
- ``RouterProvider``：team 异构臂按 route.provider 分发到多家真实 provider。
- ``LoggingProvider``：包装任意 Provider，每次 complete() 追加一行 JSONL 到
  modelbench/logs/<run_id>.jsonl；429/QuotaExhausted 原样上抛但也落日志。
- ``log_context(...)``：thread-local 上下文。thin 臂由 driver 设置
  role/work_item/attempt；team 臂的 worker 跑在 dpswarm 线程池里，
  thread-local 不可达，role 由 LoggingProvider 的 role_by_model 静态映射兜底。
- ``BudgetExceeded``：run 级 token 护栏（run_matrix 的 RUN_TOKEN_BUDGET），
  超出即在下次调用前中止，driver/run_matrix 记 budget-abort。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# dpswarm-plugin 路径引导（被测对象，只读引用）
_DPSWARM_PLUGIN = Path(__file__).resolve().parent.parent / "dpswarm-plugin"
if str(_DPSWARM_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_DPSWARM_PLUGIN))

from dpswarm.providers import (  # noqa: E402
    MockProvider,
    OpenAICompatProvider,
    Provider,
    QuotaExhausted,
    RateLimitBackoff,
)
from dpswarm.types import ModelRoute  # noqa: E402

import keyconfig  # noqa: E402

MB_DIR = Path(__file__).resolve().parent
LOGS_DIR = MB_DIR / "logs"
RESULTS_DIR = MB_DIR / "results"

GLM_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"

# GLM 服务端 web_search 工具（服务端执行，无需客户端工具循环）。
# 若 preflight 实测格式不同，改这里或在 WebSearchOrchestrator 构造参数覆盖。
GLM_WEB_SEARCH_TOOLS: List[Dict[str, Any]] = [
    {"type": "web_search", "web_search": {"enable": True, "search_result": True}},
]


class BudgetExceeded(Exception):
    """run 级 token 护栏触发：中止该 run（非传输层信号，直接上抛给 driver）。"""


def make_provider(name: str) -> OpenAICompatProvider:
    """按厂商名构造 OpenAICompatProvider（env > keys.local.json 两级读取；
    key 缺失即报错，不静默）。GLM_BASE_URL 由 keyconfig 读：Coding Plan key
    指向编程通道（keys.local.json 里配置），按量 key 用默认 paas/v4。"""
    if name == "glm":
        key = keyconfig.get("GLM_API_KEY")
        base = keyconfig.get("GLM_BASE_URL") or GLM_DEFAULT_BASE_URL
    elif name == "deepseek":
        key = keyconfig.get("DEEPSEEK_API_KEY")
        base = keyconfig.get("DEEPSEEK_BASE_URL") or DEEPSEEK_DEFAULT_BASE_URL
    else:
        raise ValueError(f"unknown provider: {name!r}（支持 glm / deepseek）")
    if not key:
        raise RuntimeError(
            f"{name} 的 API key 未配置：请设置环境变量 "
            f"{'GLM_API_KEY' if name == 'glm' else 'DEEPSEEK_API_KEY'}"
            f" 或写 modelbench/keys.local.json（或用 --mock 干跑）")
    return OpenAICompatProvider(base_url=base, api_key=key)


class RouterProvider(Provider):
    """按 route.provider 分发到对应真实 provider（team 异构臂）。"""

    name = "router"

    def __init__(self, providers: Dict[str, Provider]) -> None:
        self.providers = dict(providers)

    def complete(self, route: ModelRoute, messages, tools=None, max_tokens: int = 4096):
        try:
            inner = self.providers[route.provider]
        except KeyError:
            raise ValueError(
                f"RouterProvider 未接线 provider {route.provider!r}"
                f"（已接线：{sorted(self.providers)}）") from None
        return inner.complete(route, messages, tools=tools, max_tokens=max_tokens)


class AtomicMockProvider(MockProvider):
    """线程安全的 MockProvider：dpswarm worker 线程池并发调用时脚本序号原子推进。

    不改 dpswarm 源码；mock 模式下 complete 瞬时返回，整体串行化无性能代价。
    """

    name = "mock"

    def __init__(self, script: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__(script)
        self._mock_lock = threading.Lock()

    def complete(self, route: ModelRoute, messages, tools=None, max_tokens: int = 4096):
        with self._mock_lock:
            return super().complete(route, messages, tools=tools, max_tokens=max_tokens)


_TLS = threading.local()


@contextmanager
def log_context(task: Optional[str] = None, arm: Optional[str] = None,
                role: Optional[str] = None, work_item: Optional[str] = None,
                attempt: Optional[int] = None):
    """设置当前线程的日志上下文（None 字段不覆盖，退出时还原）。

    注意：contextvars/thread-local 都不会传播到 dpswarm 的 worker 线程池，
    team 臂的 role 归集请用 LoggingProvider(role_by_model=...) 静态映射。
    """
    prev = getattr(_TLS, "ctx", None)
    ctx = dict(prev or {})
    for key, value in {"task": task, "arm": arm, "role": role,
                       "work_item": work_item, "attempt": attempt}.items():
        if value is not None:
            ctx[key] = value
    _TLS.ctx = ctx
    try:
        yield
    finally:
        _TLS.ctx = prev


class LoggingProvider(Provider):
    """调用级日志包装：每次 complete() 追加一行 JSONL。

    行格式：{ts, run_id, task, arm, role, provider, model, work_item, attempt,
    prompt_chars, max_tokens, tools, usage{input,output,cache_read,cache_write},
    latency_ms, stop_reason, error}。
    429/QuotaExhausted 落日志（error 字段）后原样上抛；budget 超限时在调用前
    抛 BudgetExceeded（同样落日志）。
    """

    name = "logging"

    def __init__(self, inner: Provider, log_path: Path, *, run_id: str,
                 arm: Optional[str] = None, task: Optional[str] = None,
                 role_by_model: Optional[Dict[str, str]] = None,
                 budget: Optional[int] = None) -> None:
        self.inner = inner
        self.log_path = Path(log_path)
        self.run_id = run_id
        self.arm = arm
        self.task = task
        self.role_by_model = dict(role_by_model or {})
        self.budget = budget
        self._lock = threading.Lock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # run 开始前先记录 logs 已有总量（续跑/重跑时护栏口径含历史消耗）
        self._spent = self._scan_existing()

    def _scan_existing(self) -> int:
        if not self.log_path.exists():
            return 0
        total = 0
        with open(self.log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    usage = json.loads(line).get("usage") or {}
                except ValueError:
                    continue
                total += sum(int(usage.get(k) or 0)
                             for k in ("input", "output", "cache_read", "cache_write"))
        return total

    def total_tokens(self) -> int:
        with self._lock:
            return self._spent

    def complete(self, route: ModelRoute, messages, tools=None, max_tokens: int = 4096):
        ctx = getattr(_TLS, "ctx", None) or {}
        entry = {
            "ts": time.time(),
            "run_id": self.run_id,
            "task": ctx.get("task", self.task),
            "arm": ctx.get("arm", self.arm),
            "role": ctx.get("role") or self.role_by_model.get(route.model, "worker"),
            "provider": route.provider,
            "model": route.model,
            "work_item": ctx.get("work_item"),
            "attempt": ctx.get("attempt"),
            "prompt_chars": sum(len(str(m.get("content", ""))) for m in (messages or [])),
            "max_tokens": max_tokens,
            "tools": [t.get("type") for t in tools] if tools else None,
            "usage": None,
            "latency_ms": None,
            "stop_reason": None,
            "error": None,
        }
        if self.budget is not None and self.total_tokens() >= self.budget:
            entry["error"] = f"BudgetExceeded(>={self.budget})"
            self._write(entry)
            raise BudgetExceeded(entry["error"])
        t0 = time.time()
        try:
            result = self.inner.complete(route, messages, tools=tools,
                                         max_tokens=max_tokens)
        except Exception as e:
            # 429/QuotaExhausted/其他异常：落日志后原样上抛（§4 信号不吞）
            entry["latency_ms"] = int((time.time() - t0) * 1000)
            entry["error"] = f"{type(e).__name__}: {e}"[:300]
            self._write(entry)
            raise
        entry["latency_ms"] = int((time.time() - t0) * 1000)
        usage = result.usage
        entry["usage"] = {"input": usage.input_tokens, "output": usage.output_tokens,
                          "cache_read": usage.cache_read_tokens,
                          "cache_write": usage.cache_write_tokens}
        entry["stop_reason"] = result.stop_reason.value
        if result.stop_reason.value == "error":
            entry["error"] = ("stop_reason=error "
                              + json.dumps(result.raw, ensure_ascii=False)[:250])
        with self._lock:
            self._spent += usage.total_tokens()
        self._write(entry)
        return result

    def _write(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
