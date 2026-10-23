"""Execute agent runs and stream events back to the caller.

历史读写委托给 :class:`doudou.gateway.history.HistoryStore`：
内存与数据库两种实现对这里完全透明，``gateway`` 不认识 SQLAlchemy。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from doudou.agent.events import RunEvent
from doudou.agent.factory import AgentBundle
from doudou.agent.react import RunResult
from doudou.config.loader import Settings
from doudou.gateway.history import HistoryStore, InMemoryHistoryStore, ThreadInfo, is_tool_message
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger

logger = get_logger(__name__)


class AgentRunner:
    """把 :class:`AgentBundle` 跑起来，并维护每个会话的多轮历史。

    并发模型：**同一 thread 串行**（否则历史互相踩踏），不同 thread 完全并发。
    锁是进程内的，多副本部署需要换成分布式锁（见 docs/roadmap.md M10）。
    """

    def __init__(
        self,
        bundle: AgentBundle,
        *,
        store: HistoryStore | None = None,
    ) -> None:
        self.bundle = bundle
        self.settings: Settings = bundle.settings
        self.store: HistoryStore = store if store is not None else InMemoryHistoryStore()
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- 会话

    def _lock(self, thread_id: str) -> asyncio.Lock:
        return self._locks.setdefault(thread_id, asyncio.Lock())

    async def resolve_thread(self, request: RunRequest) -> str:
        """确定本次运行使用的 thread_id。"""
        if request.thread_id:
            await self.store.ensure_thread(
                request.thread_id,
                channel=request.channel,
                conversation_id=request.conversation_id,
            )
            return request.thread_id
        return await self.store.resolve_thread(
            request.conversation_id,
            channel=request.channel,
            new=request.new_thread,
        )

    async def start_new_thread(self, conversation_id: str, *, channel: str = "cli") -> str:
        """开一个新会话并绑定（``/new``）。"""
        thread_id = await self.store.resolve_thread(conversation_id, channel=channel, new=True)
        logger.info(
            "runner.thread_created",
            thread_id=thread_id,
            channel=channel,
            conversation_id=conversation_id,
        )
        return thread_id

    async def history(self, thread_id: str) -> list[BaseMessage]:
        """取某个 thread 的历史消息（不含系统提示词）。"""
        return await self.store.load(thread_id)

    async def get_thread(self, thread_id: str) -> ThreadInfo | None:
        return await self.store.get_thread(thread_id)

    async def list_threads(self, *, limit: int = 50) -> list[ThreadInfo]:
        return await self.store.list_threads(limit=limit)

    async def clear_thread(self, thread_id: str) -> bool:
        return await self.store.clear(thread_id)

    # ---------------------------------------------------------------- 运行

    async def astream(
        self, request: RunRequest, *, thread_id: str | None = None
    ) -> AsyncIterator[RunEvent]:
        """执行一次运行并流式产出事件。"""
        resolved_thread = thread_id or await self.resolve_thread(request)

        # 同一会话串行，避免历史互相踩踏；不同会话完全并发。
        async with self._lock(resolved_thread):
            history = await self.store.load(resolved_thread)
            produced: list[BaseMessage] = []
            started_ok = False
            steps = 0
            tool_calls = 0
            error: str | None = None
            try:
                async for event in self.bundle.agent.astream(
                    [*history, HumanMessage(content=request.text)],
                    thread_id=resolved_thread,
                    messages_out=produced,
                ):
                    if event.type == "run_end":
                        started_ok = True
                        steps = event.step
                    elif event.type == "tool_call":
                        tool_calls += 1
                    elif event.type == "error":
                        error = event.text
                    yield event
            finally:
                # 残段（有 tool_call 没有对应结果）不能落库，否则下一轮会被 provider 拒绝；
                # 用户提问本身无论如何都要留下。
                committed = truncate_dangling_tool_calls(produced)
                await self.store.append(
                    resolved_thread,
                    [HumanMessage(content=request.text), *committed],
                    channel=request.channel,
                    conversation_id=request.conversation_id,
                )
                stored = await self.store.get_thread(resolved_thread)
                logger.info(
                    "runner.run_finished",
                    thread_id=resolved_thread,
                    channel=request.channel,
                    user_id=request.user_id,
                    ok=started_ok and error is None,
                    steps=steps,
                    tool_calls=tool_calls,
                    history_messages=stored.message_count if stored else 0,
                    new_messages=len(committed) + 1,
                    error=error,
                )
                if not started_ok:
                    logger.warning(
                        "runner.run_incomplete",
                        thread_id=resolved_thread,
                        committed=len(committed),
                    )

    async def ainvoke(self, request: RunRequest, *, thread_id: str | None = None) -> RunResult:
        """执行一次运行并返回聚合结果。"""
        resolved_thread = thread_id or await self.resolve_thread(request)
        events: list[RunEvent] = []
        text = ""
        steps = 0
        usage: dict[str, int] = {}
        error: str | None = None

        async for event in self.astream(request, thread_id=resolved_thread):
            events.append(event)
            if event.type == "run_end":
                text = event.text
                steps = event.step
                usage = event.usage
            elif event.type == "error":
                error = event.text
                steps = event.step

        return RunResult(
            text=text,
            steps=steps,
            messages=await self.store.load(resolved_thread),
            usage=usage,
            events=events,
            ok=error is None,
            error=error,
        )


def truncate_dangling_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """丢弃结尾处「有工具调用但没有对应结果」的残段。

    运行被中断（超时、异常）时会留下这种消息；如果留在历史里，
    下一轮请求会因为 ``tool_call_id`` 找不到对应结果而被 provider 拒绝。
    """
    kept: list[BaseMessage] = []
    index = 0
    total = len(messages)

    while index < total:
        message = messages[index]

        if isinstance(message, AIMessage) and message.tool_calls:
            expected = {str(call["id"]) for call in message.tool_calls}
            cursor = index + 1
            satisfied: set[str] = set()
            while cursor < total:
                candidate = messages[cursor]
                if not is_tool_message(candidate):
                    break
                satisfied.add(str(getattr(candidate, "tool_call_id", "")))
                cursor += 1
            if not expected.issubset(satisfied):
                logger.debug("history.dangling_tool_calls", dropped=len(messages) - index)
                break
            kept.extend(messages[index:cursor])
            index = cursor
            continue

        if is_tool_message(message):
            # 孤立的工具结果（其 AIMessage 已被裁掉），一并丢弃。
            logger.debug("history.orphan_tool_message", message=message.type)
            index += 1
            continue

        kept.append(message)
        index += 1

    return kept


__all__ = [
    "AgentRunner",
    "truncate_dangling_tool_calls",
]
