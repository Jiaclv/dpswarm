"""DPswarm 控制面逻辑原型（独立于生产代码，以机制文档为唯一事实源）。"""

from .control import ControlPlane, AdmissionError, ControlError
from .types import (
    AcceptanceState,
    Attribution,
    BlockedState,
    ChoiceSource,
    HumanInstructionKind,
    Level,
    PhysicalState,
    RootExecutionSpec,
    Route,
    StartKind,
    StopReason,
    TerminalReason,
)

__all__ = [
    "ControlPlane",
    "AdmissionError",
    "ControlError",
    "AcceptanceState",
    "Attribution",
    "BlockedState",
    "ChoiceSource",
    "HumanInstructionKind",
    "Level",
    "PhysicalState",
    "RootExecutionSpec",
    "Route",
    "StartKind",
    "StopReason",
    "TerminalReason",
]
