"""确定性脚本回放 Provider（测试 / 冒烟 / 离线 replay 用）。

机制定位（§2 / §4）：与真实 Provider 完全同契约——同样的
``complete(route, messages, tools, max_tokens)`` 签名与
``ProviderResult`` 全账，保证控制面与观测闭环可以在无网络环境下
按同一代码路径被测试（可复现性属代码职责，§2）。

脚本项格式（每项二选一或组合）：
    {"text": str}                        → completed
    {"text": str, "usage": {...}}        → completed + 指定 token 全账
    {"error": "..."}                     → ProviderResult(stop_reason=ERROR, text="")
    {"stop": "max-tokens"}               → 指定 stopReason（可与 text 组合）
    {"raise": "rate-limit", "retry_after": 0} → complete 抛 RateLimitBackoff（§4 背压，
                                          retry_after 可省略；缺省 None 时由调用方
                                          退避序列决定等待时长）
    {"raise": "quota"}                   → complete 抛 QuotaExhausted（§4 明确终止/上交）
raise 项同样推进脚本索引；脚本耗尽后重复最后一条的行为对 raise 项同样生效
（最后一条是 raise 项时，后续每次调用都抛同一异常）。
usage 字典支持 input/input_tokens、output/output_tokens、
cache_read/cache_read_tokens、cache_write/cache_write_tokens、cost/cost_usd。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from ..types import ModelRoute, StopReason
from .base import Provider, ProviderResult, Usage


def _usage_from(data: Any) -> Usage:
    """从脚本项的 usage 字典构造 Usage；容忍长/短两种键名与 Usage 实例。"""
    if data is None:
        return Usage()
    if isinstance(data, Usage):
        return copy.deepcopy(data)
    d = dict(data)

    def pick(*keys: str, default: int = 0) -> int:
        for k in keys:
            if k in d and d[k] is not None:
                return int(d[k])
        return default

    cost = d.get("cost_usd", d.get("cost", 0.0))
    return Usage(
        input_tokens=pick("input_tokens", "input"),
        output_tokens=pick("output_tokens", "output"),
        cache_read_tokens=pick("cache_read_tokens", "cache_read"),
        cache_write_tokens=pick("cache_write_tokens", "cache_write"),
        cost_usd=float(cost) if cost is not None else 0.0,
    )


def _stop_from(value: Any) -> StopReason:
    """把脚本里的 stop 标注解析为 StopReason 五值（§4），未知值落 aborted。"""
    if isinstance(value, StopReason):
        return value
    text = str(value or "completed").strip().lower().replace("_", "-")
    try:
        return StopReason(text)
    except ValueError:
        return StopReason.ABORTED


class MockProvider(Provider):
    """确定性脚本回放：按调用序号逐条返回；耗尽后返回最后一条的拷贝。

    ``calls`` 记录每次 complete() 收到的入参（messages 已做浅拷贝），
    供测试断言 Lead 实际注入了什么 context（§4 采集路径的可测试替身）。
    """

    name = "mock"

    def __init__(self, script: Optional[List[Dict[str, Any]]] = None) -> None:
        self.script: List[Dict[str, Any]] = list(script or [])
        self.calls: List[Dict[str, Any]] = []
        self._index = 0

    def complete(self, route: ModelRoute, messages: List[Dict[str, Any]],
                 tools: Optional[List[Dict[str, Any]]] = None,
                 max_tokens: int = 4096) -> ProviderResult:
        self.calls.append({
            "route": route,
            "messages": [dict(m) for m in (messages or [])],
            "tools": copy.deepcopy(tools) if tools is not None else None,
            "max_tokens": max_tokens,
            "script_index": min(self._index, max(len(self.script) - 1, 0)),
        })
        if not self.script:
            # 空脚本 = 传输层硬失败（ERROR），与真实 Provider 的兜底语义一致。
            return ProviderResult(text="", stop_reason=StopReason.ERROR,
                                  raw={"mock_error": "empty script"})
        step = self.script[min(self._index, len(self.script) - 1)]
        self._index += 1
        if step.get("raise"):
            self._raise_from(step)
        # 拷贝返回，避免调用方修改结果污染后续回放。
        return copy.deepcopy(self._materialize(step))

    @staticmethod
    def _raise_from(step: Dict[str, Any]) -> None:
        """按脚本项抛传输层控制流异常（§4）：429 背压 / QUOTA 明确终止。"""
        from .base import QuotaExhausted, RateLimitBackoff
        kind = str(step["raise"]).strip().lower()
        if kind in ("rate-limit", "rate_limit", "429"):
            retry_after = step.get("retry_after")
            raise RateLimitBackoff(
                retry_after=float(retry_after) if retry_after is not None else None)
        if kind == "quota":
            raise QuotaExhausted(status=int(step.get("status", 402)))
        raise ValueError(f"unknown mock raise kind: {step['raise']!r}")

    def _materialize(self, step: Dict[str, Any]) -> ProviderResult:
        step = dict(step or {})
        if "error" in step:
            return ProviderResult(
                text="",
                stop_reason=StopReason.ERROR,
                usage=_usage_from(step.get("usage")),
                raw={"mock_error": step["error"]},
            )
        stop = _stop_from(step.get("stop", "completed"))
        return ProviderResult(
            text=str(step.get("text", "")),
            stop_reason=stop,
            usage=_usage_from(step.get("usage")),
            raw={"mock": True},
        )
