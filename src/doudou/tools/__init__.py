"""Tooling package: registry, annotations and builtin tool providers."""

from __future__ import annotations

from doudou.tools.decorators import ToolAnnotations, annotate, annotations_of
from doudou.tools.registry import (
    ALL_GROUPS,
    GroupStatus,
    ToolInfo,
    build_tools,
    describe_groups,
    effective_interrupt_on,
    find_tool,
    requires_approval,
    tool_infos,
)

__all__ = [
    "ALL_GROUPS",
    "GroupStatus",
    "ToolAnnotations",
    "ToolInfo",
    "annotate",
    "annotations_of",
    "build_tools",
    "describe_groups",
    "effective_interrupt_on",
    "find_tool",
    "requires_approval",
    "tool_infos",
]
