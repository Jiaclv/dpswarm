"""LLM 传输层基座。

定位对齐 dsh-llm-pi-ai：纯传输 SDK——把一次补全请求送到模型、把结果与
token 全账带回来，不做任何 agent / 编排 / 路由逻辑。对应机制文档：

- §2 职责切分：具体模型的语义选择权在"人工 > Lead"，硬准入在控制面代码；
  Provider 只负责传输与计量，不决定路由、不解释结果、不重试策略。
- §4 观测与闭环：
  * stopReason 五值照记：completed / error / max-tokens / aborted / refusal；
  * token 分账：cacheRead / cacheWrite 与 input 不相交，单独计账；
  * 429 与配额分开——RATE_LIMIT 是背压信号（退避重试、不进负面样本、
    不污染画像）；QUOTA（余额/鉴权耗尽）不是背压，走明确终止/上交人工
    并进运维审计（仍不进能力画像）。二者用独立异常类型表达，由调用方
    （harness 重试集 / 控制面）分别处置。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..types import ModelRoute, StopReason


class RateLimitBackoff(Exception):
    """HTTP 429：背压信号（§4）。

    语义：服务端过载，可退避重试；样本不进 failure audit、不进能力画像。
    ``retry_after`` 尽力解析自 Retry-After 响应头（秒），不可得为 None。
    """

    def __init__(self, message: str = "rate limited (HTTP 429)",
                 retry_after: Optional[float] = None,
                 status: int = 429) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


class QuotaExhausted(Exception):
    """HTTP 401/402/403：配额/鉴权耗尽（§4）。

    语义：不是背压——不可重试糊弄过去，应明确终止该次执行或上交人工，
    并写入运维审计；同样不进能力画像（它不是模型能力失败）。
    """

    def __init__(self, message: str = "quota/auth exhausted",
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Usage:
    """token 与费用分账（§4）。cache_read/write 与 input 不相交，单独计账。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0    # 与 input 不相交，单独计账（§4）
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def total_tokens(self) -> int:
        """四个分账之和 = 该次请求真实流转的 token 总量（各分账互不相交）。"""
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)


@dataclass
class ProviderResult:
    """一次补全的传输结果：正文 + stopReason 五值 + token 全账 + 原始响应。"""

    text: str
    stop_reason: StopReason       # completed/error/max-tokens/aborted/refusal（§4）
    usage: Usage = field(default_factory=Usage)
    raw: dict = field(default_factory=dict)
    assistant_message: Optional[Dict[str, Any]] = None
    response_kind: str = "unknown"  # tools/no_action/error; never task acceptance
    finish_reason: Optional[str] = None
    continuation: bool = False     # valid tool requests require another turn
    protocol_error: Optional[Dict[str, str]] = None
    # Nullable observations distinguish missing measurements from Usage's legacy
    # integer placeholders. Input here includes cache; Usage.input excludes it.
    usage_observation: Dict[str, Any] = field(default_factory=dict)


class Provider(ABC):
    """LLM 传输抽象：一次非流式补全，并带回可供观测闭环使用的全账。

    约定：
    - 传输失败默认返回 ``ProviderResult(stop_reason=ERROR, text="")``，
      不抛异常（异常只用于需要调用方改变控制流的信号，见下）；
    - 429 → 抛 :class:`RateLimitBackoff`（退避信号，§4）；
    - 401/402/403 → 抛 :class:`QuotaExhausted`（明确终止/上交，§4）；
    - 不做重试、不做降级换模、不吞 stopReason——重试集属 harness，
      路由属控制面（§2 职责切分）。
    """

    name: str = "provider"

    @abstractmethod
    def complete(self, route: ModelRoute, messages: List[Dict[str, Any]],
                 tools: Optional[List[Dict[str, Any]]] = None,
                 max_tokens: int = 4096) -> ProviderResult:
        """按精确路由执行一次补全。

        :param route: 精确路由（provider/model/reasoning_effort/level/source）。
            Provider 只消费其中的传输相关字段（model/reasoning_effort），
            不做任何路由判断（§2）。
        :param messages: OpenAI 风格消息列表，原样透传。
        :param tools: 可选工具 schema 列表。
        :param max_tokens: 本次补全的输出上限。
        """
        raise NotImplementedError
