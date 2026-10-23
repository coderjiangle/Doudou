"""List available tools and their metadata."""

from __future__ import annotations

from fastapi import APIRouter

from doudou.api.deps import SettingsDep
from doudou.api.schemas import ToolInfoOut, ToolListResponse
from doudou.observability.logging import get_logger
from doudou.tools.registry import tool_infos

logger = get_logger(__name__)

router = APIRouter(tags=["tools"])


@router.get("/tools", response_model=ToolListResponse, summary="工具清单")
async def list_tools(settings: SettingsDep) -> ToolListResponse:
    """列出当前会交给模型的工具。

    以配置为准重新构造清单（而不是复用运行中的实例），这样
    ``/tools`` 反映的始终是「按当前配置应该装载什么」。
    """
    infos = tool_infos(settings)
    return ToolListResponse(
        tools=[
            ToolInfoOut(
                name=info.name,
                description=info.description,
                group=info.group,
                source=info.source,
                args=info.args,
                requires_approval=info.requires_approval,
            )
            for info in infos
        ]
    )


__all__ = ["router"]
