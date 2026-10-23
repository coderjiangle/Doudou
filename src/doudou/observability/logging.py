"""Logging configuration and structured log helpers."""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

_configured = False


#: 这些 logger 前缀在 INFO 级别非常吵，统一压到 WARNING 以上。
#:
#: 用**前缀**匹配而非精确名字，是因为第三方可能出现变体名：
#: 实测 openai SDK 走的 httpx 变体 logger 叫 ``httpx2``，按名字列举必然漏掉。
_NOISY_PREFIXES: tuple[str, ...] = (
    "httpx",  # 覆盖 httpx / httpx2 等变体
    "httpcore",
    "urllib3",
    "openai",
    "anthropic",
    "asyncio",
)


class _NoisyLoggerFilter(logging.Filter):
    """丢弃噪音 logger 中低于 ``min_level`` 的记录。

    挂在 **handler** 上而不是用 ``logger.setLevel()``：第三方 logger 可能在
    :func:`configure_logging` 之后才被创建，逐个 setLevel 会漏掉新实例。
    """

    def __init__(self, prefixes: tuple[str, ...], min_level: int = logging.WARNING) -> None:
        super().__init__(name="doudou.noisy")
        self._prefixes = prefixes
        self._min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= self._min_level:
            return True
        return not record.name.startswith(self._prefixes)


def _install_noisy_filter(min_level: int) -> None:
    """把噪音过滤器挂到根 logger 的所有 handler 上（幂等）。"""
    for handler in logging.getLogger().handlers:
        if any(isinstance(item, _NoisyLoggerFilter) for item in handler.filters):
            continue
        handler.addFilter(_NoisyLoggerFilter(_NOISY_PREFIXES, max(min_level, logging.WARNING)))


def configure_logging(
    *,
    level: str = "INFO",
    json_logs: bool = False,
    stream: Any | None = None,
) -> None:
    """初始化 structlog 与标准库 logging。

    日志统一走 ``stderr``，避免污染 CLI 与 SSE 的 ``stdout`` 输出。

    Args:
        level: 日志级别名（``DEBUG`` / ``INFO`` / ...），大小写不敏感。
        json_logs: ``True`` 输出 JSON（生产环境），``False`` 输出带颜色的可读格式。
        stream: 覆盖输出流，测试时可传入 ``io.StringIO``。
    """
    global _configured

    level_no = _LEVELS.get(level.upper(), logging.INFO)
    out = stream if stream is not None else sys.stderr

    logging.basicConfig(
        format="%(message)s",
        stream=out,
        level=level_no,
        force=True,
    )
    _install_noisy_filter(level_no)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            # 走 stdlib logger factory，这样 add_logger_name 能取到模块名，
            # 第三方库（httpx / langchain）的日志也会汇到同一个出口。
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        logger_factory=structlog.stdlib.LoggerFactory(),
        # 关闭首用缓存：CLI 会先做一次最小初始化再按配置重配日志，
        # 缓存会让早于配置创建的 logger 一直用旧设置。
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """取一个绑定了模块名的 logger。"""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))


def is_configured() -> bool:
    """是否已调用过 :func:`configure_logging`（测试用）。"""
    return _configured


def reset_logging() -> None:
    """还原 structlog 与标准库 logging 配置（测试用）。"""
    global _configured
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _configured = False


__all__ = ["configure_logging", "get_logger", "is_configured", "reset_logging"]
