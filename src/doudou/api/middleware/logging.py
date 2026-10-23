"""Structured access logging."""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics

logger = get_logger(__name__)

#: 这些路径不写访问日志：健康检查会被探针高频调用。
QUIET_PATHS = ("/health", "/health/ready")


class AccessLogMiddleware(BaseHTTPMiddleware):
    """每个请求一条结构化日志 + 计时指标。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - started
            # 异常本身由 errors 里的处理器记录堆栈，这里只补上「耗时」这一维度。
            logger.info(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                elapsed=round(elapsed, 4),
            )
            get_metrics().incr("http.request_failed")
            get_metrics().observe("http.request", elapsed)
            raise

        elapsed = time.perf_counter() - started
        metrics = get_metrics()
        metrics.observe("http.request", elapsed)
        metrics.incr(f"http.status.{response.status_code}")

        if not any(request.url.path.endswith(item) for item in QUIET_PATHS):
            logger.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                elapsed=round(elapsed, 4),
                client=request.client.host if request.client else None,
            )
        return response


__all__ = ["QUIET_PATHS", "AccessLogMiddleware"]
