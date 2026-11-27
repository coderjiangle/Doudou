"""飞书通道：长连接收事件，流式卡片回复。

lark 的 ``ws.Client`` 在模块导入时就创建一个模块级 event loop，``start()`` 会
``run_until_complete(_select())`` 一直阻塞，等于自己霸占一个线程和一个事件循环。
所以 ``start()`` 必须在专属线程里调用（否则会卡死 FastAPI）；在那个线程收到事件后，
用 ``asyncio.run_coroutine_threadsafe`` 把活儿交回主事件循环（Agent 是异步的）；
停止时向那个模块级 loop 投递 ``stop`` —— 这是唯一能打断 ``_select()`` 的方式。
线程设为 daemon：即使优雅停止失败，进程也要能退出。

卡片用整卡 patch 而不是 cardkit 增量更新，理由见 ``cards.py``。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from doudou.agent.approvals import ApprovalBroker, ApprovalRequest
from doudou.agent.events import RunEvent
from doudou.agent.messages import MAX_IMAGES, strip_display_annotations_text
from doudou.channels.base import InboundMessage, OutboundMessage, current_inbound
from doudou.channels.feishu import cards
from doudou.channels.feishu.client import FeishuClient, FeishuError, parse_action_value
from doudou.channels.feishu.events import CHANNEL_NAME, EventDedup, parse_message_event
from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics
from doudou.utils.asyncio_compat import install_connection_reset_filter
from doudou.utils.text import truncate

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查
    # 运行时不能 import：channels.feishu.channel ← gateway.router 是反向依赖，
    # 而 gateway.router 又要通过 channels.base 触发本包的 __init__，会成环。
    from doudou.gateway.router import MessageRouter

logger = get_logger(__name__)

#: 等待长连接线程退出的上限。
WS_STOP_TIMEOUT = 5.0


class FeishuWebSocketRunner:
    """在独立 daemon 线程里跑 lark 的长连接客户端。"""

    def __init__(
        self,
        config: Any,
        *,
        on_event: Callable[[Any], None],
        on_card_action: Callable[[Any], None] | None = None,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.on_card_action = on_card_action
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
            install_connection_reset_filter(loop)
            asyncio.set_event_loop(loop)
            ws_module.loop = loop
            self._loop = loop

            builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
                self.on_event
            )
            if self.on_card_action is not None:
                # 卡片按钮回调（人工确认）。与消息回调同处一个 dispatcher ——
                # 长连接只有一条，两个事件源共用它。
                builder = builder.register_p2_card_action_trigger(self.on_card_action)
            handler = builder.build()
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
        # **不**调 loop.close()：lark 的长连接客户端可能在 loop 上留有
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
        broker: ApprovalBroker | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.app.channels.feishu
        self.router = router
        self.client = client if client is not None else FeishuClient(self.config)
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._dedup = EventDedup(ttl_seconds=float(self.config.dedup_ttl_seconds))
        self._ws = runner
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inbound_locks: dict[str, asyncio.Lock] = {}
        #: 确认卡片 ID：点了按钮之后要把那张卡换成「已同意 / 已拒绝」，
        #: 否则按钮还挂在那里，容易被重复点击（虽然决策是幂等的）。
        self._approval_cards: dict[str, str] = {}
        self._broker = broker
        if broker is not None:
            if broker.on_request is not None:
                logger.warning(
                    "feishu.approval_handler_replaced",
                    hint="已有其它组件登记了确认通知回调，将改由飞书卡片接管",
                )
            # 接管的含义：有工具需要确认时，飞书**主动推一张带按钮的卡片**，
            # 用户的点击再回到 broker。没有这一步，飞书里所有副作用工具都只能被拒绝
            # （无人可问 = 拒绝）。
            broker.on_request = self._on_approval_request

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        if not self.config.app_id or not self.config.app_secret:
            raise FeishuError(
                "飞书通道已启用但缺少凭据：请设置 DOUDOU_CHANNELS_FEISHU_APP_ID "
                "与 DOUDOU_CHANNELS_FEISHU_APP_SECRET。"
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
            self._ws = FeishuWebSocketRunner(
                self.config, on_event=self._on_event, on_card_action=self._on_card_action
            )
        self._ws.start()
        logger.info(
            "feishu.channel_started",
            transport="websocket",
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
            # 同一会话先完成图片下载再入队，避免后来的文字越过尚在下载的图片。
            lock = self._inbound_locks.setdefault(message.session_key, asyncio.Lock())
            async with lock:
                keys = message.raw.get("image_keys") or []
                if keys:
                    if len(keys) > MAX_IMAGES or not message.message_id:
                        raise ValueError(
                            f"每条消息最多支持 {MAX_IMAGES} 张图片，且需要有效消息 ID。"
                        )
                    images = await asyncio.gather(
                        *(self.client.download_image(message.message_id, key) for key in keys)
                    )
                    message = replace(message, images=tuple(images))
                await self.router.dispatch(message, self)
        except Exception as exc:
            logger.error(
                "feishu.dispatch_failed",
                conversation_id=message.conversation_id,
                error=str(exc),
                exc_info=True,
            )
            if message.raw.get("image_keys"):
                try:
                    await self.send(
                        OutboundMessage(
                            conversation_id=message.conversation_id,
                            text="图片读取失败。请重发 PNG、JPEG、GIF 或 WebP 图片"
                            "（每张不超过 5 MiB，"
                            "每条最多 4 张），并确认应用已开通图片资源读取权限。",
                        )
                    )
                except Exception:
                    logger.warning("feishu.image_error_reply_failed", exc_info=True)

    # ---------------------------------------------------------------- 出站

    async def send(self, message: OutboundMessage) -> None:
        """发一条独立文本消息（队列繁忙提示等）。"""
        await asyncio.to_thread(
            self.client.send_text,
            message.conversation_id,
            truncate(strip_display_annotations_text(message.text), self.config.max_reply_chars),
        )

    # ---------------------------------------------------------------- 人工确认

    async def _on_approval_request(self, request: ApprovalRequest) -> None:
        """Broker 的「有人要确认」通知：往对应会话推一张带按钮的卡片。

        会话从 :func:`~doudou.channels.base.current_inbound` 取 —— 它由 router
        在跑这一轮之前绑定，因此这里的正是**发起这次调用的那条会话**。

        不是飞书发起的调用（HTTP API / CLI）就什么都不做：那些路径各有自己的
        确认方式（``/approvals`` 或终端提问），这里抢着发卡片会发错地方。
        """
        inbound = current_inbound()
        if inbound is None or inbound.channel != CHANNEL_NAME:
            logger.debug(
                "feishu.approval_not_for_us",
                approval_id=request.approval_id,
                channel=inbound.channel if inbound else None,
            )
            return

        card = cards.approval_card(
            bot_name=self.config.bot_name,
            approval_id=request.approval_id,
            tool_name=request.tool_name,
            args=request.args,
            question=strip_display_annotations_text(inbound.text),
            expires_in=self._broker.timeout if self._broker else None,
        )
        message_id = await asyncio.to_thread(self.client.send_card, inbound.conversation_id, card)
        if message_id:
            self._approval_cards[request.approval_id] = message_id
        logger.info(
            "feishu.approval_card_sent",
            approval_id=request.approval_id,
            tool=request.tool_name,
            conversation_id=inbound.conversation_id,
            ok=bool(message_id),
        )

    def _on_card_action(self, data: Any) -> None:
        """lark 的卡片按钮回调。**运行在 WS 线程**，因此只解析 + 转发。"""
        try:
            value = parse_action_value(self._raw_action_value(data))
            if value.action not in {"approve", "reject"} or not value.approval_id:
                return  # 不是我们的按钮（或点了别的卡）
            loop = self._main_loop
            if loop is None or loop.is_closed():
                logger.error("feishu.main_loop_unavailable", approval_id=value.approval_id)
                return
            asyncio.run_coroutine_threadsafe(
                self._handle_card_action(value.approval_id, value.action == "approve"), loop
            )
        except Exception as exc:
            logger.error("feishu.card_action_failed", error=str(exc), exc_info=True)

    @staticmethod
    def _raw_action_value(data: Any) -> Any:
        """从 lark 的卡片回调对象里掏出按钮的 ``value``。

        这里用 ``getattr`` 链而不是直接访问属性：lark 的事件对象在不同
        版本里层级会变（``data.event.action.value`` vs ``data.action.value``），
        而「读不到」应该表现为「忽略这次点击」，不是把回调打崩。

        掏出来的原始值交给 :func:`parse_action_value` —— 按钮的 ``value``
        既可能是 dict 也可能是 JSON 字符串，解析只该有一处实现。
        """
        event = getattr(data, "event", None) or data
        action = getattr(event, "action", None)
        return getattr(action, "value", None)

    async def _handle_card_action(self, approval_id: str, approved: bool) -> None:
        """在主事件循环里落决策，并把卡片换成结果态。"""
        if self._broker is None:
            logger.warning("feishu.approval_without_broker", approval_id=approval_id)
            return
        request = self._broker.get(approval_id)
        if request is None:
            # 幂等：可能是超时之后才点的，或重复点击。
            logger.info("feishu.approval_unknown", approval_id=approval_id)
            return
        outcome = await self._broker.decide(approval_id, approved=approved)

        message_id = self._approval_cards.pop(approval_id, None)
        if message_id:
            card = cards.approval_decided_card(
                bot_name=self.config.bot_name,
                tool_name=request.tool_name,
                approved=outcome.approved,
                reason=outcome.reason,
            )
            await asyncio.to_thread(self.client.patch_card, message_id, card)
        get_metrics().incr("feishu.approval_decided")
        logger.info(
            "feishu.approval_decided",
            approval_id=approval_id,
            tool=request.tool_name,
            approved=outcome.approved,
        )

    async def deliver(self, message: InboundMessage, events: AsyncIterator[RunEvent]) -> None:
        """消费一次运行的事件流并渲染成卡片。

        流程：先发「正在处理」卡片拿到 message_id → 消费事件、按节流更新卡片 →
        收尾时用最终卡片覆盖。任何一步失败都退回发一条文本，保证用户一定收到结果。
        """
        card_message_id = await self._open_card(message)
        state = _CardState()

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
                cards.thinking_card(bot_name=self.config.bot_name),
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
        fallback = strip_display_annotations_text(state.answer) or "（本次运行没有产生内容）"
        await self.send(OutboundMessage(conversation_id=message.conversation_id, text=fallback))


class _CardState:
    """一次运行的卡片状态机。

    把「事件流怎么变成卡片内容」收在一个类里，好处是节流、失败兜底、工具痕迹
    这些逻辑不必和通道的 IO 混在一起，也方便单独测试。
    """

    def __init__(self) -> None:
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
                    message=self.error or "运行失败",
                )
            return cards.answer_card(
                bot_name=bot_name,
                answer=strip_display_annotations_text(self.answer),
                tool_notes=self.tool_notes,
                footer=self._footer(),
            )
        partial = strip_display_annotations_text(self.text).strip() or "正在思考…"
        return cards.answer_card(
            bot_name=bot_name,
            answer=partial,
            tool_notes=self.tool_notes,
        )

    def _footer(self) -> str | None:
        if self.patch_failures:
            return f"（有 {self.patch_failures} 次卡片更新失败，内容可能不是最新）"
        return None


__all__ = ["FeishuChannel", "FeishuWebSocketRunner"]
