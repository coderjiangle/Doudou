"""Map channel conversations to agent threads and dispatch runs.

路由层只认识两件事：

* :class:`~doudou.channels.base.InboundMessage` —— 通道归一化后的入站消息；
* :class:`~doudou.channels.base.EventSink` —— 能把事件流渲染出来的东西。

因此它对「是终端还是飞书」、「怎么渲染」一无所知。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from doudou.channels.base import (
    EventSink,
    InboundMessage,
    OutboundMessage,
    bind_inbound,
    unbind_inbound,
)
from doudou.gateway.bus import MessageBus
from doudou.gateway.runner import AgentRunner
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics

logger = get_logger(__name__)

#: 队列满时回复用户的话术。
BUSY_REPLY = "我还在处理上一条消息，稍等一下再说～"

#: 入站钩子：拿到每条归一化消息（用于记录「用户 → 会话」映射等旁路）。
InboundHook = Callable[[InboundMessage], Awaitable[None]]


class MessageRouter:
    """把入站消息交给 Agent 跑，并把事件流交给通道渲染。"""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        bus: MessageBus | None = None,
        on_inbound: InboundHook | None = None,
    ) -> None:
        self.runner = runner
        self.bus = bus
        #: 旁路钩子：失败只告警，不影响运行（与观察者同一条原则）。
        self.on_inbound = on_inbound

    async def dispatch(self, message: InboundMessage, sink: EventSink) -> bool:
        """处理一条入站消息。

        Args:
            message: 归一化后的入站消息。
            sink: 事件流的渲染方（通常是通道自己）。

        Returns:
            ``False`` 表示会话队列已满、本次被拒绝（已回话告知用户）。
        """
        request = RunRequest.from_inbound(message)

        if self.bus is None:
            await self._run(message, sink, request)
            return True

        # 同一会话串行：AgentRunner 内部只保证「同一 thread 不并发」，
        # 但排队本身放在总线里做，这样积压可见、可限流。
        accepted = await self.bus.submit(
            message.session_key,
            lambda: self._run(message, sink, request),
            # 注意：这里用闭包捕获 request，所以每个任务拿到的是自己的那一条。
        )
        if not accepted:
            await self._reply_busy(message, sink)
        return accepted

    async def _run(self, message: InboundMessage, sink: EventSink, request: RunRequest) -> None:
        """真正执行一轮运行，并让 sink 消费事件流。"""
        logger.info(
            "router.run_start",
            channel=message.channel,
            conversation_id=message.conversation_id,
            user_id=message.user_id,
            text_chars=len(message.text),
        )
        await self._notify_inbound(message)
        stream = self.runner.astream(request)
        # 标记「现在服务的是这条消息」：确认回调要靠它知道把卡片发到哪个会话。
        token = bind_inbound(message)
        try:
            await sink.deliver(message, stream)
        finally:
            unbind_inbound(token)
            # 无论渲染是否成功，都要把生成器关掉，避免留下未消费的协程。
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                await aclose()

    async def run_now(self, message: InboundMessage, sink: EventSink) -> None:
        """立即执行一轮（不排队）并等待完成。

        定时任务用：它已经由调度器的节奏限流，且调用方需要等待结果来记录状态。
        同一 thread 的并发仍由 ``AgentRunner`` 的每线程锁兜住。
        """
        request = RunRequest.from_inbound(message)
        await self._run(message, sink, request)

    async def _notify_inbound(self, message: InboundMessage) -> None:
        """触发入站钩子；钩子是旁路，失败只告警。"""
        if self.on_inbound is None:
            return
        try:
            await self.on_inbound(message)
        except Exception as exc:
            logger.warning(
                "router.inbound_hook_failed",
                channel=message.channel,
                conversation_id=message.conversation_id,
                error=str(exc),
            )

    async def _reply_busy(self, message: InboundMessage, sink: EventSink) -> None:
        get_metrics().incr("router.busy_rejected")
        try:
            await sink.send(
                OutboundMessage(conversation_id=message.conversation_id, text=BUSY_REPLY)
            )
        except Exception as exc:
            logger.warning(
                "router.busy_reply_failed", conversation_id=message.conversation_id, error=str(exc)
            )


__all__ = ["BUSY_REPLY", "InboundHook", "MessageRouter"]
