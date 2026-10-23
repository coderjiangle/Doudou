"""Dependency providers for routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from doudou.api.errors import ApiError
from doudou.api.lifespan import AppState
from doudou.config.loader import Settings
from doudou.gateway.runner import AgentRunner
from doudou.security.auth import extract_bearer, verify_token


def get_state(request: Request) -> AppState:
    """取运行期状态；未初始化说明 lifespan 没跑起来。"""
    state: AppState | None = getattr(request.app.state, "doudou", None)
    if state is None:
        raise ApiError(503, "not_ready", "服务尚未完成初始化")
    return state


def get_settings_dep(state: Annotated[AppState, Depends(get_state)]) -> Settings:
    return state.settings


def get_runner(state: Annotated[AppState, Depends(get_state)]) -> AgentRunner:
    return state.runner


async def require_token(request: Request) -> None:
    """Bearer Token 鉴权。

    ``DOUDOU_API_TOKEN`` 为空时不鉴权（本地开发）；非空时除健康检查外一律要求令牌。
    """
    settings: Settings = get_state(request).settings
    verify_token(extract_bearer(request.headers.get("authorization")), settings.api_token)


StateDep = Annotated[AppState, Depends(get_state)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RunnerDep = Annotated[AgentRunner, Depends(get_runner)]
AuthDep = Annotated[None, Depends(require_token)]


__all__ = [
    "AuthDep",
    "RunnerDep",
    "SettingsDep",
    "StateDep",
    "get_runner",
    "get_settings_dep",
    "get_state",
    "require_token",
]
