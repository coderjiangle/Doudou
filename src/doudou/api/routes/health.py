"""Health, readiness and version endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from doudou.api.deps import StateDep
from doudou.api.lifespan import CHECK_FAILED, CHECK_OK, AppState
from doudou.api.schemas import HealthResponse
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health(state: StateDep) -> HealthResponse:
    """轻量健康检查：**不触发任何模型调用**。

    只报告进程内已知的状态（工具数量、历史存储后端等），
    因此可以被探针以秒级频率轮询。
    """
    settings = state.settings
    checks = {
        "history": settings.app.storage.history,
        "database": CHECK_OK if state.database is not None else "memory",
        "channels": ",".join(state.channel_names) if state.channel_names else "none",
        "mcp": ",".join(status.name for status in (state.mcp.statuses if state.mcp else []))
        or "none",
        # 观测能力是排查的前提，所以放在 health 里 —— 别让人以为没开追踪时
        # 「trace 里什么都没有」是出了问题。
        "observability": ",".join(state.observability.names) or "none",
    }
    return HealthResponse(
        status=CHECK_OK,
        version=state.version,
        env=settings.app.runtime.env,
        engine="react",
        model=state.bundle.metadata.get("model", settings.app.model.primary),
        uptime_seconds=state.uptime_seconds,
        tools=state.bundle.tool_names,
        channels=state.channel_names,
        mcp=_mcp_summary(state),
        checks=checks,
        metrics=state.metrics_snapshot(),
        observability=state.observability.describe(),
    )


def _mcp_summary(state: AppState) -> dict[str, Any]:
    """MCP 的紧凑状态：几个 server、几个工具、有没有掉线的。"""
    runtime = state.mcp
    if runtime is None or not runtime.enabled:
        return {"enabled": False, "servers": 0, "tools": 0, "failed": []}
    statuses = runtime.statuses
    return {
        "enabled": True,
        "servers": len(statuses),
        "tools": len(runtime.tools),
        "failed": [status.name for status in statuses if not status.ok],
    }


@router.get("/health/ready", summary="就绪检查")
async def ready(state: StateDep) -> dict[str, str]:
    """就绪检查：真正探一次历史存储。

    与 ``/health`` 的区别是它会访问数据库，因此能反映「数据库连不上」这种情况；
    代价是更重，适合做 readiness 探针而不是 liveness。
    """
    checks: dict[str, str] = {}
    try:
        await state.runner.list_threads(limit=1)
        checks["history"] = CHECK_OK
    except Exception as exc:
        logger.warning("health.history_unavailable", error=str(exc), exc_info=True)
        checks["history"] = CHECK_FAILED

    status = CHECK_OK if all(value == CHECK_OK for value in checks.values()) else CHECK_FAILED
    return {"status": status, **checks}


@router.get("/version", summary="版本号")
async def version(state: StateDep) -> dict[str, str]:
    return {"version": state.version, "env": state.settings.app.runtime.env}


__all__ = ["router"]
