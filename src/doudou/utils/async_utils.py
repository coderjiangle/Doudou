"""Async helpers: task groups, retries, cancellation.

只放真正被用到的编排能力。重试策略集中在这里，避免每个出网工具各写一套
「试几次、退避多久」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tenacity import (
    AsyncRetrying,
    AttemptManager,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 出网调用的默认重试次数（含首次）。
DEFAULT_ATTEMPTS = 3


def _before_sleep(label: str) -> Callable[[Any], None]:
    def _log(retry_state: Any) -> None:
        logger.warning(
            "retry.backoff",
            label=label,
            attempt=getattr(retry_state, "attempt_number", None),
            error=str(getattr(retry_state, "outcome", None)),
        )

    return _log


async def retry_async[T](
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = 0.5,
    max_delay: float = 4.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    label: str = "call",
) -> T:
    """按指数退避重试一个异步调用。

    Args:
        call: 无参协程工厂（每次重试都会重新调用它）。
        attempts: 总尝试次数（含首次）。
        base_delay / max_delay: 退避区间（秒），指数增长并封顶。
        retry_on: 只在这些异常上重试；其它异常立即抛出。
        label: 日志里的调用标识。

    Returns:
        最后一次成功调用的返回值。

    Raises:
        最后一次失败的异常（``reraise=True``，不做包装以免丢失原始类型）。
    """
    retryer: AsyncRetrying = AsyncRetrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=wait_exponential(multiplier=base_delay, max=max_delay),
        retry=retry_if_exception_type(retry_on),
        before_sleep=_before_sleep(label),
        reraise=True,
    )

    attempt: AttemptManager
    async for attempt in retryer:
        with attempt:
            return await call()

    # 只有在 attempts <= 0 且没有 reraise 的情况下才会走到这里。
    raise RuntimeError(f"{label}: 重试未执行")  # pragma: no cover


__all__ = ["DEFAULT_ATTEMPTS", "retry_async"]
