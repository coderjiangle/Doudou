"""Attach and propagate a request id."""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """给每个请求分配 request id，并回写到响应头。

    id 同时写进 ``request.state`` 与 structlog 的 ``contextvars``，因此同一次请求里
    所有日志都会自动带上它，排查时可以直接过滤。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                response = await call_next(request)
            except Exception:
                # 异常会被 app 的 exception handler 处理，但 handler 在中间件之外，
                # 这里必须把 request_id 直接带进日志上下文。
                raise
        response.headers[HEADER] = request_id
        return response


__all__ = ["HEADER", "RequestIdMiddleware"]
