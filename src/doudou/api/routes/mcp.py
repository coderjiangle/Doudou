"""Inspect MCP servers and refresh their tools."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from doudou.api.deps import StateDep
from doudou.api.errors import ApiError
from doudou.api.schemas import McpServerOut, McpStatusResponse
from doudou.mcp.config import McpConfigError
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["mcp"])


def _require_mcp(state: StateDep) -> Any:
    runtime = state.mcp
    if runtime is None:
        raise ApiError(503, "mcp_unavailable", "MCP 运行时未初始化")
    return runtime


@router.get("/mcp", response_model=McpStatusResponse, summary="MCP server 与工具")
async def list_mcp(state: StateDep) -> McpStatusResponse:
    """列出每个 MCP server 的状态、暴露的工具与被过滤掉的工具。

    ``ok: false`` 时 ``error`` 里是失败原因 —— 这正是 ``fail_fast: false``
    降级后运维需要的可观测性：服务还在跑，但要能看出哪个 server 掉了。
    """
    runtime = _require_mcp(state)
    return McpStatusResponse(
        enabled=runtime.enabled,
        started=runtime.started,
        servers=[McpServerOut.model_validate(entry.as_dict()) for entry in runtime.describe()],
    )


@router.post("/mcp/refresh", response_model=McpStatusResponse, summary="重新拉取 MCP 工具")
async def refresh_mcp(state: StateDep) -> McpStatusResponse:
    """重新拉取工具列表，无需重启服务。

    注意：**Agent 不会因此更新**。工具列表在 Agent 构造时就固定了，
    刷新只影响检视结果与后续新建的 Agent。要真正生效需重启服务 ——
    这点必须如实说明，否则用户会以为刷新完就能用新工具。
    """
    runtime = _require_mcp(state)
    try:
        await runtime.refresh(reason="api")
    except McpConfigError as exc:
        raise ApiError(502, "mcp_refresh_failed", str(exc)) from exc
    return McpStatusResponse(
        enabled=runtime.enabled,
        started=runtime.started,
        servers=[McpServerOut.model_validate(entry.as_dict()) for entry in runtime.describe()],
        note="工具已重新拉取；Agent 的工具列表在启动时固定，需重启服务才会生效。",
    )


__all__ = ["router"]
