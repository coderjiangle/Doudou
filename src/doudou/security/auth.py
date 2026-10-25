"""API authentication and channel signature verification."""

from __future__ import annotations

import secrets

from doudou.observability.logging import get_logger

logger = get_logger(__name__)

BEARER_PREFIX = "bearer"


class AuthenticationError(Exception):
    """鉴权失败。

    不依赖 FastAPI：``security`` 层只做判断，由 ``api.errors`` 决定映射成什么响应。
    """


def extract_bearer(header_value: str | None) -> str | None:
    """从 ``Authorization`` 头里取出 Bearer token。"""
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.strip().lower() != BEARER_PREFIX:
        return None
    token = token.strip()
    return token or None


def verify_token(provided: str | None, expected: str | None) -> None:
    """校验令牌。

    用常量时间比较，避免通过响应耗时推断令牌内容。

    Args:
        provided: 请求携带的令牌。
        expected: 配置的令牌；为空表示**不开启鉴权**（本地开发）。

    Raises:
        AuthenticationError: 令牌缺失或不匹配。
    """
    if not expected:
        return
    if not provided:
        raise AuthenticationError("缺少访问令牌")
    if not secrets.compare_digest(provided, expected):
        logger.warning("security.token_mismatch", provided_length=len(provided))
        raise AuthenticationError("访问令牌无效")


__all__ = ["BEARER_PREFIX", "AuthenticationError", "extract_bearer", "verify_token"]
