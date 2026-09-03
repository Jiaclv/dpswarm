"""DPswarm LLM 传输层（providers）。

纯传输 SDK（对齐 dsh-llm-pi-ai 的定位）：不做 agent 逻辑、不做路由决策
（§2 职责切分）；stopReason 五值照记、token 分账、429 与配额分开（§4）。
"""
from __future__ import annotations

from .base import (
    Provider,
    ProviderResult,
    QuotaExhausted,
    RateLimitBackoff,
    Usage,
)
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "Provider",
    "MockProvider",
    "OpenAICompatProvider",
    "ProviderResult",
    "Usage",
    "RateLimitBackoff",
    "QuotaExhausted",
]
