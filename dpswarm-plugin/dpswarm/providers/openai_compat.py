"""OpenAI 兼容 /chat/completions 传输 Provider（纯标准库 urllib）。

机制定位（§2 / §4）：只做传输与计量——
- 目标地址与密钥环境变量驱动：``DPSWARM_BASE_URL`` / ``DPSWARM_API_KEY``
  （构造参数可覆盖 base_url / api_key / timeout）；
- body 最小集 ``{model, messages, max_tokens}``，仅当
  ``route.reasoning_effort != "default"`` 时附加 ``reasoning_effort``
  （精确路由的传输侧投影，§2）；
- 解析 stopReason：stop→completed、length→max-tokens；合法工具请求是已完成
  的传输回合，同时 response_kind=tools / continuation=True，不代表任务完成；
- token 分账（§4）：prompt_tokens/completion_tokens；若响应带
  ``prompt_tokens_details.cached_tokens``，则 cache_read = 该值且
  input_tokens = prompt_tokens - cached_tokens（cache 与 input 不相交）；
- 429 与配额分开（§4）：HTTPError 429 → 抛 :class:`RateLimitBackoff`
  （退避信号，可重试、不进负面样本）；401/402/403 → 抛
  :class:`QuotaExhausted`（明确终止/上交人工）；其余 HTTP/JSON/网络
  异常 → ``ProviderResult(stop_reason=ERROR, text="")``，不抛异常；
- 超时两段式：建连 120s、读 300s（经自定义 Connection 在建连后收紧
  socket 超时实现；构造参数 timeout 可整体覆盖）。
"""
from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple, Union

from ..types import ModelRoute, StopReason
from .base import Provider, ProviderResult, QuotaExhausted, RateLimitBackoff, Usage

DEFAULT_CONNECT_TIMEOUT = 120.0   # 建连超时（秒）
DEFAULT_READ_TIMEOUT = 300.0      # 建连后的读写超时（秒）

_FINISH_REASON_MAP = {
    "stop": StopReason.COMPLETED,
    "length": StopReason.MAX_TOKENS,
}


# ---------------------------------------------------------------------------
# 两段式超时：urlopen(timeout=connect) 管建连；建连完成后收紧为读超时。
# ---------------------------------------------------------------------------


class _SplitTimeoutHTTPConnection(http.client.HTTPConnection):
    read_timeout: Optional[float] = DEFAULT_READ_TIMEOUT

    def connect(self) -> None:
        super().connect()  # 建连阶段受 self.timeout（= urlopen 传入值）约束
        if self.sock is not None and self.read_timeout is not None:
            self.sock.settimeout(self.read_timeout)


class _SplitTimeoutHTTPSConnection(http.client.HTTPSConnection):
    read_timeout: Optional[float] = DEFAULT_READ_TIMEOUT

    def connect(self) -> None:
        super().connect()  # 含 TLS 握手，同样受建连超时约束
        if self.sock is not None and self.read_timeout is not None:
            self.sock.settimeout(self.read_timeout)


class _SplitTimeoutHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_SplitTimeoutHTTPConnection, req)


class _SplitTimeoutHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # 不透传 context：使用 HTTPSConnection 的默认校验上下文即可，
        # 避免 do_open 跨版本关键字差异。
        return self.do_open(_SplitTimeoutHTTPSConnection, req)


def _retry_after_seconds(err: urllib.error.HTTPError) -> Optional[float]:
    """尽力解析 Retry-After 头（秒）；缺失或为 HTTP 日期格式时返回 None。"""
    try:
        value = err.headers.get("Retry-After") if err.headers is not None else None
    except Exception:  # pragma: no cover - 防御性
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error_body_snippet(err: urllib.error.HTTPError, limit: int = 200) -> str:
    """安全读取 HTTPError 响应体片段，供诊断信息使用。"""
    try:
        data = err.read()
        return data.decode("utf-8", errors="replace")[:limit]
    except Exception:  # pragma: no cover - 防御性
        return ""


class OpenAICompatProvider(Provider):
    """OpenAI 兼容端点的纯传输实现（POST {base_url}/chat/completions）。"""

    name = "openai-compat"

    def __init__(self, base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 timeout: Optional[Union[float, Tuple[float, float]]] = None) -> None:
        self.base_url = (base_url or os.environ.get("DPSWARM_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.environ.get("DPSWARM_API_KEY")
        if timeout is None:
            self.connect_timeout = DEFAULT_CONNECT_TIMEOUT
            self.read_timeout = DEFAULT_READ_TIMEOUT
        elif isinstance(timeout, (tuple, list)):
            self.connect_timeout, self.read_timeout = float(timeout[0]), float(timeout[1])
        else:
            self.connect_timeout = self.read_timeout = float(timeout)
        self._opener = urllib.request.build_opener(
            _SplitTimeoutHTTPHandler, _SplitTimeoutHTTPSHandler)

    # -- 内部 -------------------------------------------------------------

    def _endpoint(self) -> str:
        if not self.base_url:
            raise ValueError(
                "base_url 未配置：设置 DPSWARM_BASE_URL 或构造参数 base_url")
        return f"{self.base_url}/chat/completions"

    def _build_body(self, route: ModelRoute, messages: List[Dict[str, Any]],
                    tools: Optional[List[Dict[str, Any]]], max_tokens: int) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if route.reasoning_effort and route.reasoning_effort != "default":
            body["reasoning_effort"] = route.reasoning_effort
        if tools:
            from ..team_runtime.protocol import normalize_tool_declarations
            body["tools"] = normalize_tool_declarations(tools)
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _parse_response(data: Dict[str, Any], tools=None) -> ProviderResult:
        from ..team_runtime.protocol import ProtocolError, parse_native_response

        def observed(value):
            return value if type(value) is int and value >= 0 else None

        if not isinstance(data, dict):
            return ProviderResult(text="", stop_reason=StopReason.ERROR,
                                  response_kind="error", raw={"error": "Response must be an object"})
        usage_data = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = usage_data.get("prompt_tokens_details") or {}
        output_details = usage_data.get("completion_tokens_details") or {}
        prompt = observed(usage_data.get("prompt_tokens"))
        completion = observed(usage_data.get("completion_tokens"))
        cached = observed(details.get("cached_tokens")) if isinstance(details, dict) else None
        total = observed(usage_data.get("total_tokens"))
        total_source = "reported" if total is not None else None
        if total is None and prompt is not None and completion is not None:
            total, total_source = prompt + completion, "derived_input_plus_output"
        cache_valid = cached is None or (prompt is not None and cached <= prompt)
        observation = {"input_tokens": prompt, "cached_input_tokens": cached,
                       "output_tokens": completion, "total_tokens": total,
                       "total_tokens_source": total_source,
                       "reasoning_tokens": observed(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None,
                       "cost_usd": None, "cache_partition_valid": cache_valid,
                       "complete": prompt is not None and completion is not None and cache_valid}
        # Preserve the integer Usage API. Unknown counters are placeholders only;
        # usage_observation is the authoritative measurement-completeness record.
        cache_read = cached if cached is not None and cache_valid else 0
        usage = Usage(input_tokens=max(0, (prompt or 0) - cache_read),
                      output_tokens=completion or 0, cache_read_tokens=cache_read)
        choices = data.get("choices") or []
        if not choices:
            return ProviderResult(text="", stop_reason=StopReason.ERROR,
                                  usage=usage, raw=dict(data), response_kind="error",
                                  usage_observation=observation)
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            return ProviderResult(text="", stop_reason=StopReason.ERROR, usage=usage,
                                  raw=copy.deepcopy(data), response_kind="error",
                                  usage_observation=observation)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            return ProviderResult(text="", stop_reason=StopReason.ERROR, usage=usage,
                                  raw=copy.deepcopy(data), response_kind="error",
                                  finish_reason=choice.get("finish_reason"),
                                  usage_observation=observation)
        text = message.get("content")
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        # Unknown finish reasons remain aborted; validated tool continuations
        # below explicitly distinguish a completed transport from finished work.
        # message.refusal（安全拒绝）显式映射 REFUSAL，不让第一层的 refusal 维度失真
        stop_reason = _FINISH_REASON_MAP.get(choice.get("finish_reason"),
                                             StopReason.ABORTED)
        if message.get("refusal"):
            stop_reason = StopReason.REFUSAL
        declarations = tools
        if declarations is None:
            # A standalone parser call has no request registry. Validate wire
            # shape only; complete() always supplies the actual declarations.
            declarations = []
            for call in message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []:
                if isinstance(call, dict) and isinstance(call.get("function"), dict):
                    name = call["function"].get("name")
                    if isinstance(name, str) and not any(d["name"] == name for d in declarations):
                        declarations.append({"name": name, "parameters": {"type": "object"}})
        try:
            action = parse_native_response(message, declarations)
        except ProtocolError as exc:
            return ProviderResult(text=text, stop_reason=StopReason.ERROR, usage=usage,
                                  raw=copy.deepcopy(data), assistant_message=copy.deepcopy(message),
                                  response_kind="error", finish_reason=choice.get("finish_reason"),
                                  protocol_error={"code": exc.code, "message": str(exc)},
                                  usage_observation=observation)
        continuation = action["kind"] == "tools" and not message.get("refusal") and choice.get("finish_reason") in ("tool_calls", "function_call", "stop")
        if continuation:
            # COMPLETED means the provider call completed, not the role/task.
            stop_reason = StopReason.COMPLETED
        return ProviderResult(text=text, stop_reason=stop_reason, usage=usage,
                              raw=copy.deepcopy(data), assistant_message=copy.deepcopy(message),
                              response_kind=action["kind"], finish_reason=choice.get("finish_reason"),
                              continuation=continuation, usage_observation=observation)

    # -- Provider 契约 ------------------------------------------------------

    def complete(self, route: ModelRoute, messages: List[Dict[str, Any]],
                 tools: Optional[List[Dict[str, Any]]] = None,
                 max_tokens: int = 4096) -> ProviderResult:
        body = self._build_body(route, messages, tools, max_tokens)
        headers = {"Content-Type": "application/json", "User-Agent": "dpswarm/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            # timeout 作用于建连阶段；读超时由 Connection.connect 后收紧
            with self._opener.open(request, timeout=self.connect_timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            status = err.code
            if status == 429:
                # §4：RATE_LIMIT 是背压信号——退避重试，不进负面样本
                raise RateLimitBackoff(
                    f"rate limited (HTTP 429): {_error_body_snippet(err)}",
                    retry_after=_retry_after_seconds(err), status=429,
                ) from err
            if status in (401, 402, 403):
                # §4：QUOTA/鉴权不是背压——明确终止/上交人工，进运维审计
                raise QuotaExhausted(
                    f"quota/auth exhausted (HTTP {status}): {_error_body_snippet(err)}",
                    status=status,
                ) from err
            return ProviderResult(text="", stop_reason=StopReason.ERROR,
                                  raw={"error": f"HTTP {status}",
                                       "detail": _error_body_snippet(err)})
        except (OSError, ValueError) as err:
            # URLError/ConnectionError/socket.timeout（含 TimeoutError）⊂ OSError；
            # JSONDecodeError ⊂ ValueError。传输失败不抛异常（除 429/配额）。
            return ProviderResult(text="", stop_reason=StopReason.ERROR,
                                  raw={"error": f"{type(err).__name__}: {err}"})
        return self._parse_response(payload, tools=tools or [])
