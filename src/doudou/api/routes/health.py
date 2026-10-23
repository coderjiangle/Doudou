"""Health, readiness and version endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from doudou.api.deps import StateDep
from doudou.api.lifespan import CHECK_FAILED, CHECK_OK
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
    }
    return HealthResponse(
        status=CHECK_OK,
        version=state.version,
        env=settings.app.runtime.env,
        engine="react",
        model=state.bundle.metadata.get("model", settings.app.model.primary),
        uptime_seconds=state.uptime_seconds,
        tools=state.bundle.tool_names,
        checks=checks,
        metrics=state.metrics_snapshot(),
    )


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
