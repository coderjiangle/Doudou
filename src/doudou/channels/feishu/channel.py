"""Feishu channel implementation: long connection in, streaming card out.

## 为什么需要独立线程

lark 的 ``ws.Client`` 在**模块导入时**创建一个模块级 event loop，``start()`` 会
``loop.run_until_complete(_select())`` 一直阻塞——也就是说它自己霸占一个线程和一个
事件循环。所以：

* ``start()`` 必须在**专属线程**里调用，不能占用主事件循环（会把 FastAPI 卡死）；
* 反过来，我们在那个线程里收到事件后，必须用
  ``asyncio.run_coroutine_threadsafe`` 把活儿交回**主**事件循环——Agent 是异步的；
* 停止时向那个模块级 loop 投递 ``stop``（这是唯一能打断 ``_select()`` 的方式），
  并且线程设为 daemon：**即使优雅停止失败，进程也必须能退出**。

## 为什么用整卡 patch 而不是 cardkit 增量

见 ``cards.py`` 的说明：API 面小、行为可预测，配合节流已经足够像流式。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from doudou.agent.events import RunEvent
from doudou.channels.base import InboundMessage, OutboundMessage
from doudou.channels.feishu import cards
from doudou.channels.feishu.client import FeishuClient, FeishuError
from doudou.channels.feishu.events import CHANNEL_NAME, EventDedup, parse_message_event
from doudou.config.loader import Settings
from doudou.gateway.router import MessageRouter
from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics
from doudou.utils.text import truncate

logger = get_logger(__name__)

#: 等待长连接线程退出的上限。
WS_STOP_TIMEOUT = 5.0


class FeishuWebSocketRunner:
    """在独立 daemon 线程里跑 lark 的长连接客户端。"""

    def __init__(self, config: Any, *, on_event: Callable[[Any], None]) -> None:
        self.config = config
        self.on_event = on_event
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._error: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> BaseException | None:
        return self._error

    def start(self) -> None:
        if self.running:
            return
        self._thread = threading.Thread(target=self._run, name="feishu-ws", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """线程主体。

        ## 为什么必须在本线程里接管 lark 的模块级 loop

        ``lark_oapi.ws.client`` 在**模块导入时**就执行了
        ``loop = asyncio.get_event_loop()``。如果 lark 已经在主线程被导入过
        （构造 ``lark.Client`` 就会导入它），那个模块级 ``loop`` 就是**主事件循环**，
        于是 ``start()`` 里的 ``loop.run_until_complete`` 会抛
        ``This event loop is already running`` —— 长连接根本连不上，而异常被线程吞掉，
        表面看服务一切正常。

        所以这里显式：本线程新建 loop、``set_event_loop``，并把 lark 的模块级
        ``loop`` 指过去。在它当前的设计下这是唯一可靠的做法。
        """
        try:
            import lark_oapi as lark
            from lark_oapi.ws import client as ws_module

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            ws_module.loop = loop
            self._loop = loop

            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self.on_event)
                .build()
            )
            client = lark.ws.Client(
                app_id=self.config.app_id or "",
                app_secret=self.config.app_secret or "",
                event_handler=handler,
                domain=self.config.base_url,
                log_level=lark.LogLevel.WARNING,
            )
            logger.info("feishu.ws_connecting", domain=self.config.base_url)
            client.start()
            logger.info("feishu.ws_stopped")
        except BaseException as exc:
            self._error = exc
            logger.error("feishu.ws_crashed", error=str(exc), exc_info=True)

    def stop(self, *, timeout: float = WS_STOP_TIMEOUT) -> None:
        """停止长连接。"""
        thread, loop = self._thread, self._loop
        if thread is None:
            return
        if loop is not None:
            try:
                # 只有把 loop 停掉，start() 里的 run_until_complete(_select()) 才会返回。
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError as exc:  # 循环已关闭
                logger.debug("feishu.ws_loop_closed", error=str(exc))
        thread.join(timeout=timeout)
        if thread.is_alive():
            # daemon 线程不会阻止进程退出，这里只是如实说明「没干净地停」。
            logger.warning("feishu.ws_stop_timeout", timeout=timeout)
        self._thread = None
        self._loop = None
        # 刻意**不**调 loop.close()：lark 的长连接客户端可能在 loop 上留有
        # 未完成的回调/任务，关闭它只会在退出阶段制造 "Event loop is closed" 噪音。
        # 线程已结束、loop 会被 GC 回收，而通道的启停在一个进程里只发生一次。


class FeishuChannel:
    """飞书通道：长连接收消息，卡片流式回复。

    同时实现 ``Channel``（能启停、能发消息）与 ``EventSink``（能渲染一次运行的事件流）。
    """

    name = CHANNEL_NAME

    def __init__(
        self,
        settings: Settings,
        router: MessageRouter,
        *,
        client: FeishuClient | None = None,
        runner: FeishuWebSocketRunner | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.app.channels.feishu
        self.router = router
        self.client = client if client is not None else FeishuClient(self.config)
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._dedup = EventDedup(ttl_seconds=float(self.config.dedup_ttl_seconds))
        self._ws = runner
        self._tasks: set[asyncio.Task[Any]] = set()

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        if not self.config.app_id or not self.config.app_secret:
            raise FeishuError(
                "飞书通道已启用但缺少凭据：请设置 FEISHU_APP_ID 与 FEISHU_APP_SECRET。"
            )
        if self.config.mode != "websocket":
            logger.warning(
                "feishu.mode_unsupported_yet",
                mode=self.config.mode,
                hint="目前只实现了 websocket 长连接；callback 模式需要验签解密与公网回调地址",
            )

        # 记下主事件循环：事件从 WS 线程进来时要靠它把活儿交回来。
        self._main_loop = asyncio.get_running_loop()

        if not self.config.bot_open_id:
            # 取不到不是致命问题：@ 判定会退回按 bot_name / mentioned_type 匹配。
            open_id = await asyncio.to_thread(self._safe_fetch_bot_open_id)
            if open_id:
                self.config = self.config.model_copy(update={"bot_open_id": open_id})
                logger.info("feishu.bot_identity", open_id=open_id)

        if self._ws is None:
            self._ws = FeishuWebSocketRunner(self.config, on_event=self._on_event)
        self._ws.start()
        logger.info(
            "feishu.channel_started",
            mode=self.config.mode,
            bot_name=self.config.bot_name,
            group_require_at=self.config.group_require_at,
            isolate_by_thread=self.config.isolate_by_thread,
            allowed_chats=len(self.config.allowed_chats),
        )

    def _safe_fetch_bot_open_id(self) -> str | None:
        try:
            return self.client.fetch_bot_open_id()
        except Exception as exc:
            logger.warning("feishu.bot_identity_failed", error=str(exc))
            return None

    async def stop(self) -> None:
        if self._ws is not None:
            await asyncio.to_thread(self._ws.stop)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("feishu.channel_stopped")

    # ---------------------------------------------------------------- 入站（WS 线程）

    def _on_event(self, event: Any) -> None:
        """lark 的事件回调。**运行在 WS 线程**，因此只做解析与转发，绝不阻塞。"""
        # 必须先答 200 语义：lark 通过不抛异常表示「已处理」，抛了会触发重推。
        try:
            event_id = self._event_id(event)
            if not self._dedup.check_and_mark(event_id):
                get_metrics().incr("feishu.duplicate_event")
                logger.info("feishu.duplicate_event", event_id=event_id)
                return

            parsed = parse_message_event(event, self.config, event_id=event_id)
            if not parsed.ok:
                get_metrics().incr("feishu.skipped")
                logger.debug(
                    "feishu.event_skipped",
                    event_id=event_id,
                    reason=parsed.skip.code if parsed.skip else "unknown",
                    detail=parsed.skip.detail if parsed.skip else "",
                )
                return

            message = parsed.message
            assert message is not None
            loop = self._main_loop
            if loop is None or loop.is_closed():
                logger.error("feishu.main_loop_unavailable", event_id=event_id)
                return
            # 交回主事件循环执行；WS 线程立刻返回，不阻塞心跳。
            asyncio.run_coroutine_threadsafe(self._dispatch(message), loop)
        except Exception as exc:
            logger.error("feishu.event_handler_failed", error=str(exc), exc_info=True)

    @staticmethod
    def _event_id(event: Any) -> str | None:
        header = getattr(event, "header", None)
        value = getattr(header, "event_id", None)
        return str(value) if value else None

    async def _dispatch(self, message: InboundMessage) -> None:
        """在主事件循环里把消息交给 router。"""
        get_metrics().incr("feishu.received")
        logger.info(
            "feishu.inbound",
            conversation_id=message.conversation_id,
            user_id=message.user_id,
            message_id=message.message_id,
            text_chars=len(message.text),
        )
        try:
            await self.router.dispatch(message, self)
        except Exception as exc:
            logger.error(
                "feishu.dispatch_failed",
                conversation_id=message.conversation_id,
                error=str(exc),
                exc_info=True,
            )

    # ---------------------------------------------------------------- 出站

    async def send(self, message: OutboundMessage) -> None:
        """发一条独立文本消息（队列繁忙提示等）。"""
        await asyncio.to_thread(
            self.client.send_text,
            message.conversation_id,
            truncate(message.text, self.config.max_reply_chars),
        )

    async def deliver(self, message: InboundMessage, events: AsyncIterator[RunEvent]) -> None:
        """消费一次运行的事件流并渲染成卡片。

        流程：先发「正在处理」卡片拿到 message_id → 消费事件、按节流更新卡片 →
        收尾时用最终卡片覆盖。任何一步失败都退回发一条文本，保证用户一定收到结果。
        """
        card_message_id = await self._open_card(message)
        state = _CardState(question=message.text)

        async for event in events:
            state.consume(event)
            if self._should_flush(state):
                await self._flush(card_message_id, state, message)

        await self._close(card_message_id, state, message)

    async def _open_card(self, message: InboundMessage) -> str | None:
        if self.config.reply_mode != "card":
            return None
        try:
            return await asyncio.to_thread(
                self.client.send_card,
                message.conversation_id,
                cards.thinking_card(bot_name=self.config.bot_name, question=message.text),
            )
        except Exception as exc:
            logger.warning(
                "feishu.thinking_card_failed",
                conversation_id=message.conversation_id,
                error=str(exc),
            )
            return None

    def _should_flush(self, state: _CardState) -> bool:
        if state.finished:
            return True
        if not self.config.stream_reply:
            return False
        interval = max(0.0, self.config.card_update_interval_ms / 1000)
        now = time.monotonic()
        if now - state.last_flush < interval:
            return False
        state.last_flush = now
        return True

    async def _flush(
        self, card_message_id: str | None, state: _CardState, message: InboundMessage
    ) -> None:
        card = state.render(self.config.bot_name)
        if card_message_id is None:
            return
        ok = await asyncio.to_thread(self.client.patch_card, card_message_id, card)
        if not ok:
            state.patch_failures += 1

    async def _close(
        self, card_message_id: str | None, state: _CardState, message: InboundMessage
    ) -> None:
        card = state.render(self.config.bot_name)

        if card_message_id is not None:
            patched = await asyncio.to_thread(self.client.patch_card, card_message_id, card)
            if patched:
                return
            logger.warning("feishu.final_patch_failed", conversation_id=message.conversation_id)

        # 没有卡片（或卡片更新失败）时兜底发文本：用户必须能看到结果。
        fallback = state.answer or "（本次运行没有产生内容）"
        await self.send(OutboundMessage(conversation_id=message.conversation_id, text=fallback))


class _CardState:
    """一次运行的卡片状态机。

    把「事件流怎么变成卡片内容」收在一个类里，好处是节流、失败兜底、工具痕迹
    这些逻辑不必和通道的 IO 混在一起，也方便单独测试。
    """

    def __init__(self, *, question: str) -> None:
        self.question = question
        self.text = ""
        self.answer = ""
        self.tool_notes: list[str] = []
        self.finished = False
        self.failed = False
        self.error: str | None = None
        self.last_flush = time.monotonic()
        self.patch_failures = 0

    def consume(self, event: RunEvent) -> None:
        if event.type == "text_delta":
            self.text += event.text
        elif event.type == "tool_call":
            if event.tool_name:
                self.tool_notes.append(cards.tool_running_note(event.tool_name, event.tool_args))
        elif event.type == "tool_result":
            # 用「结果」替换掉同一工具的「正在调用」痕迹，避免卡片上出现两行。
            if event.tool_name:
                self._replace_tool_note(
                    event.tool_name,
                    cards.tool_note(event.tool_name, ok=event.tool_ok, result=event.result or ""),
                )
        elif event.type == "run_end":
            self.answer = event.text
            self.finished = True
        elif event.type == "error":
            self.error = event.text
            self.failed = True
            self.finished = True

    def _replace_tool_note(self, tool_name: str, note: str) -> None:
        prefixes = (f"… 正在调用 {tool_name}", f"✓ {tool_name}", f"✗ {tool_name}")
        for index in range(len(self.tool_notes) - 1, -1, -1):
            if self.tool_notes[index].startswith(prefixes):
                self.tool_notes[index] = note
                return
        self.tool_notes.append(note)

    def render(self, bot_name: str) -> dict[str, Any]:
        """渲染成卡片 JSON。未结束时展示「生成中」的部分文本。"""
        if self.finished:
            if self.failed:
                return cards.error_card(
                    bot_name=bot_name,
                    question=self.question,
                    message=self.error or "运行失败",
                )
            return cards.answer_card(
                bot_name=bot_name,
                question=self.question,
                answer=self.answer,
                tool_notes=self.tool_notes,
                footer=self._footer(),
            )
        partial = self.text.strip() or "正在思考…"
        return cards.answer_card(
            bot_name=bot_name,
            question=self.question,
            answer=partial,
            tool_notes=self.tool_notes,
        )

    def _footer(self) -> str | None:
        if self.patch_failures:
            return f"（有 {self.patch_failures} 次卡片更新失败，内容可能不是最新）"
        return None


__all__ = ["FeishuChannel", "FeishuWebSocketRunner"]
