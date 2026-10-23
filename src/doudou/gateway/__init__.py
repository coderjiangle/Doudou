"""Gateway package: routing between channels and the agent."""

from __future__ import annotations

from doudou.gateway.history import (
    DEFAULT_MAX_MESSAGES,
    HistoryStore,
    InMemoryHistoryStore,
    ThreadInfo,
    is_tool_message,
    trim_history,
)
from doudou.gateway.runner import AgentRunner, truncate_dangling_tool_calls
from doudou.gateway.schema import RunRequest
from doudou.gateway.session import SessionRecord, SessionStore

__all__ = [
    "DEFAULT_MAX_MESSAGES",
    "AgentRunner",
    "HistoryStore",
    "InMemoryHistoryStore",
    "RunRequest",
    "SessionRecord",
    "SessionStore",
    "ThreadInfo",
    "is_tool_message",
    "trim_history",
    "truncate_dangling_tool_calls",
]
