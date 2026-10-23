"""Unified error model and exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from doudou.observability.logging import get_logger
from doudou.security.auth import AuthenticationError

logger = get_logger(__name__)


class ApiError(Exception):
    """API 层错误的统一表示。

    业务模块抛出领域异常（如 :class:`AuthenticationError`），
    由本模块翻译成 HTTP 响应 —— 领域层不必知道 HTTP 状态码。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_body(
    code: str, message: str, *, details: Any | None = None, request_id: str | None = None
) -> dict[str, Any]:
    """统一错误响应体。"""
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    if request_id:
        payload["request_id"] = request_id
    return payload


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def install_error_handlers(app: FastAPI) -> None:
    """把所有异常收敛成同一种响应结构。"""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        logger.warning(
            "api.error",
            code=exc.code,
            status=exc.status_code,
            path=request.url.path,
            detail=exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                exc.code, exc.message, details=exc.details, request_id=_request_id(request)
            ),
        )

    @app.exception_handler(AuthenticationError)
    async def _auth_error(request: Request, exc: AuthenticationError) -> JSONResponse:
        logger.warning("api.unauthorized", path=request.url.path, reason=str(exc))
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=error_body("unauthorized", str(exc), request_id=_request_id(request)),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 把 pydantic 的错误位置压成可读的一行，避免把整个 schema 回给调用方。
        details = [
            {
                "field": ".".join(str(part) for part in item.get("loc", ())),
                "issue": item.get("msg", ""),
            }
            for item in exc.errors()
        ]
        logger.info(
            "api.validation_failed", path=request.url.path, fields=[d["field"] for d in details]
        )
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_request",
                "请求参数不合法",
                details=details,
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：日志里带堆栈，响应里不带内部细节。
        logger.error(
            "api.unhandled_error",
            path=request.url.path,
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", "服务内部错误", request_id=_request_id(request)),
        )


__all__ = ["ApiError", "error_body", "install_error_handlers"]
