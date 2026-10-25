"""运行观察者：审计、成本、追踪挂在同一份事件流上。

三者要的东西完全一样：一次运行里谁在什么时候调了哪个工具、带了什么参数、结果如何、
用了多少 token，差别只在写到哪里（库 / 计数器 / Langfuse）。让它们各自去改
:class:`~doudou.gateway.runner.AgentRunner`，那个类很快就会变成横切关注点的垃圾桶；
所以这里定义一层很薄的协议：runner 按顺序喂事件，观察者自己决定怎么处理。

三条约束：观察者绝不能弄坏运行（每次回调都包 try/except，失败只记
``observability.observer_failed``）；只读事件，不修改、不拦截（要拦请用权限规则或
确认回调）；零配置时零开销（没配时拿到空列表，runner 的循环直接不跑）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from doudou.agent.events import RunEvent
from doudou.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RunContext:
    """一次运行的「谁 / 在哪 / 问了什么」。

    这些信息不在 :class:`RunEvent` 里（事件只描述 Agent 内部发生了什么），
    但审计的「谁」、追踪的 trace 名称、成本的归属都需要它，所以由 runner 补上。
    """

    thread_id: str
    channel: str = "unknown"
    conversation_id: str = ""
    user_id: str | None = None
    #: 用户这一轮的输入（原文）。审计只存摘要，见 ``observability.audit``。
    text: str = ""

    def as_log_fields(self) -> dict[str, Any]:
        """给日志用的精简字段集（不含正文）。"""
        return {
            "thread_id": self.thread_id,
            "channel": self.channel,
            "user_id": self.user_id,
        }


@dataclass(slots=True)
class RunSummary:
    """一次运行的收尾统计。"""

    ok: bool = True
    steps: int = 0
    tool_calls: int = 0
    approvals: int = 0
    rejections: int = 0
    #: 累计 token 用量（与 ``run_end`` 事件里的一致）。
    usage: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0
    error: str | None = None


@runtime_checkable
class RunObserver(Protocol):
    """只读地观察一次运行。"""

    #: 观察者名字，用于日志与 ``/health`` 汇总。
    name: str

    async def on_run_start(self, context: RunContext) -> None:
        """运行开始。"""
        ...

    async def on_event(self, context: RunContext, event: RunEvent) -> None:
        """收到一条事件（``run_start`` / ``run_end`` 也会经过这里）。"""
        ...

    async def on_run_end(self, context: RunContext, summary: RunSummary) -> None:
        """运行结束（无论成功或失败）。"""
        ...


async def notify_start(observers: Sequence[RunObserver], context: RunContext) -> None:
    await _safe_call(observers, "on_run_start", context)


async def notify_event(
    observers: Sequence[RunObserver], context: RunContext, event: RunEvent
) -> None:
    for observer in observers:
        await _safe_call_one(observer, "on_event", context, event)


async def notify_end(
    observers: Sequence[RunObserver], context: RunContext, summary: RunSummary
) -> None:
    await _safe_call(observers, "on_run_end", context, summary)


async def _safe_call(observers: Sequence[RunObserver], method: str, *args: Any) -> None:
    for observer in observers:
        await _safe_call_one(observer, method, *args)


async def _safe_call_one(observer: RunObserver, method: str, *args: Any) -> None:
    """调用观察者并吞掉异常。

    刻意在这里吞：观察者是**旁路**，它失败的唯一正确后果是「少一条记录」，
    而不是「用户的消息没有得到回复」。
    """
    try:
        await getattr(observer, method)(*args)
    except Exception as exc:
        logger.warning(
            "observability.observer_failed",
            observer=getattr(observer, "name", type(observer).__name__),
            method=method,
            error=str(exc),
            exc_info=True,
        )


class BaseObserver:
    """观察者基类：把不关心的方法变成空实现，子类只覆写需要的。"""

    name = "observer"

    async def on_run_start(self, context: RunContext) -> None:
        return None

    async def on_event(self, context: RunContext, event: RunEvent) -> None:
        return None

    async def on_run_end(self, context: RunContext, summary: RunSummary) -> None:
        return None

    async def aclose(self) -> None:
        """释放资源（HTTP 客户端、后台线程）。默认什么都不做。"""
        return None


__all__ = [
    "BaseObserver",
    "RunContext",
    "RunObserver",
    "RunSummary",
    "notify_end",
    "notify_event",
    "notify_start",
]
