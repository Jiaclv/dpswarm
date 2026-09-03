"""机制三：上下文分发子系统（Memory Service + Assembler + 可选 manager LLM）。

- §5.6/§5.7  memory.MemoryService      分层记忆、晋升/版本/失效
- §5.2-5.4   assembler.ContextAssembler 确定性装配、预算裁剪、落盘
- §5.1/§5.2/§5.8 manager.ContextManagerLLM 按需瞬时语义压缩与 capsule
"""
from .assembler import AssemblerBrief, ContextAssembler, est_tokens
from .manager import ContextManagerLLM
from .memory import MemoryEntry, MemoryService

__all__ = [
    "MemoryService",
    "MemoryEntry",
    "ContextAssembler",
    "AssemblerBrief",
    "ContextManagerLLM",
    "est_tokens",
]
