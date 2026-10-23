"""Route package aggregating all routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from doudou.api.deps import require_token
from doudou.api.routes import chat, health, mcp, skills, threads, tools


def build_router(base_path: str = "/api/v1") -> APIRouter:
    """把所有子路由挂到一个前缀下。

    健康检查**不加鉴权**（探针与容器编排需要免令牌访问），其余接口统一要求
    Bearer Token —— 令牌为空时 ``require_token`` 直接放行。
    """
    router = APIRouter(prefix=base_path)

    router.include_router(health.router)
    router.include_router(chat.router, dependencies=[Depends(require_token)])
    router.include_router(threads.router, dependencies=[Depends(require_token)])
    router.include_router(tools.router, dependencies=[Depends(require_token)])
    router.include_router(mcp.router, dependencies=[Depends(require_token)])
    router.include_router(skills.router, dependencies=[Depends(require_token)])

    return router


__all__ = ["build_router"]
