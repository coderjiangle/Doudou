"""Agent assembly package."""

from __future__ import annotations

from doudou.agent.events import RunEvent, ToolOutcome
from doudou.agent.factory import AgentBundle, build_agent
from doudou.agent.react import ApprovalHook, ReactAgent, RunResult, ToolApprovalRequest

__all__ = [
    "AgentBundle",
    "ApprovalHook",
    "ReactAgent",
    "RunEvent",
    "RunResult",
    "ToolApprovalRequest",
    "ToolOutcome",
    "build_agent",
]
