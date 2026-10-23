"""List available tools and their metadata."""

from __future__ import annotations

from fastapi import APIRouter

from doudou.api.deps import StateDep
from doudou.api.schemas import ToolInfoOut, ToolListResponse
from doudou.observability.logging import get_logger
from doudou.tools.registry import tool_infos

logger = get_logger(__name__)

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=ToolListResponse, summary="工具清单")
async def list_tools(state: StateDep) -> ToolListResponse:
    """列出 Agent **实际装载**的工具（含 MCP 拉取到的那些）。

    刻意用 ``state.bundle.tools`` 而不是从配置重新推导：MCP 工具是运行时
    拉取的、不在 provider 里，只有这条路径能把它们如实列出来。
    """
    settings = state.settings
    infos = tool_infos(settings, tools=state.bundle.tools)
    return ToolListResponse(
        tools=[
            ToolInfoOut(
                name=info.name,
                description=info.description,
                group=info.group,
                source=info.source,
                args=info.args,
                requires_approval=info.requires_approval,
                side_effects=info.side_effects,
                timeout=info.timeout,
            )
            for info in infos
        ]
    )


__all__ = ["router"]
