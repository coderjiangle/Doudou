"""Execute agent runs and stream events back to the caller.

历史读写委托给 :class:`doudou.gateway.history.HistoryStore`：
内存与数据库两种实现对这里完全透明，``gateway`` 不认识 SQLAlchemy。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine, Sequence
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage

from doudou.agent.context import ContextManager, ContextStats, is_summary_message
from doudou.agent.events import RunEvent
from doudou.agent.factory import AgentBundle
from doudou.agent.messages import (
    append_persona_reminder,
    append_time_context,
    build_time_context,
    human_message,
    should_attach_persona_reminder,
    strip_display_annotations_message,
)
from doudou.agent.react import RunResult
from doudou.config.loader import Settings
from doudou.gateway.history import HistoryStore, InMemoryHistoryStore, ThreadInfo, is_tool_message
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger
from doudou.observability.observers import (
    RunContext,
    RunObserver,
    RunSummary,
    notify_end,
    notify_event,
    notify_start,
)

if TYPE_CHECKING:
    from doudou.tools.registry import ToolInfo

logger = get_logger(__name__)


class RunnerLike(Protocol):
    """CLI / 语音等交互通道依赖的运行器协议（本地 ``AgentRunner`` 与远程客户端共用）。"""

    settings: Settings

    @property
    def model_label(self) -> str: ...

    @property
    def tool_count(self) -> int: ...

    async def resolve_thread(self, request: RunRequest) -> str: ...

    async def start_new_thread(self, conversation_id: str, *, channel: str = "cli") -> str: ...

    async def list_threads(self, *, limit: int = 50) -> list[ThreadInfo]: ...

    async def history(self, thread_id: str) -> list[BaseMessage]: ...

    async def list_tool_infos(self) -> Sequence[ToolInfo]: ...

    def astream(
        self, request: RunRequest, *, thread_id: str | None = None
    ) -> AsyncIterator[RunEvent]: ...

    async def ainvoke(self, request: RunRequest, *, thread_id: str | None = None) -> RunResult: ...


class AgentRunner:
    """把 :class:`AgentBundle` 跑起来，并维护每个会话的多轮历史。

    并发模型：**同一 thread 串行**（否则历史互相踩踏），不同 thread 完全并发。
    锁是进程内的，多副本部署需要换成分布式锁。

    ``observers`` 是审计 / 成本 / 追踪的挂载点：它们**只读**事件流，
    而且失败了也不会影响运行（见 ``observability.observers``）。
    """

    def __init__(
        self,
        bundle: AgentBundle,
        *,
        store: HistoryStore | None = None,
        observers: Sequence[RunObserver] = (),
    ) -> None:
        self.bundle = bundle
        self.settings: Settings = bundle.settings
        self.store: HistoryStore = store if store is not None else InMemoryHistoryStore()
        self.context: ContextManager | None = bundle.context
        self.observers: list[RunObserver] = list(observers)
        self._locks: dict[str, asyncio.Lock] = {}
        self._stats: dict[str, ContextStats] = {}

    @property
    def model_label(self) -> str:
        return str(self.bundle.metadata.get("model") or self.settings.app.model.primary)

    @property
    def tool_count(self) -> int:
        return len(self.bundle.tools)

    async def list_tool_infos(self) -> Sequence[ToolInfo]:
        from doudou.tools.registry import tool_infos

        return tool_infos(self.settings, tools=self.bundle.tools)

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
        """取某个 thread 的历史消息（不含系统提示词，并剥离展示用的人设与时间锚点）。"""
        messages = await self.store.load(thread_id)
        return [strip_display_annotations_message(message) for message in messages]

    async def get_thread(self, thread_id: str) -> ThreadInfo | None:
        return await self.store.get_thread(thread_id)

    async def list_threads(self, *, limit: int = 50) -> list[ThreadInfo]:
        return await self.store.list_threads(limit=limit)

    async def clear_thread(self, thread_id: str) -> bool:
        self.context.forget(thread_id) if self.context else None
        self._stats.pop(thread_id, None)
        return await self.store.clear(thread_id)

    # ---------------------------------------------------------------- 上下文检视

    def context_stats(self, thread_id: str) -> ContextStats | None:
        """上一次运行时的上下文占用。"""
        return self._stats.get(thread_id)

    async def context_report(self, thread_id: str) -> dict[str, Any]:
        """给 ``GET /threads/{id}/context`` 的报告。

        它回答的是排查时最想问的问题：「这条会话现在占多少上下文、
        有没有被压缩过、原始历史还在不在」。
        """
        from doudou.agent.context import context_report

        history = await self.store.load(thread_id)
        summary = self.context.summary_for(thread_id) if self.context else None
        return context_report(
            thread_id=thread_id,
            messages=history,
            stats=self._stats.get(thread_id),
            summary=summary,
            budget=self.context.budget if self.context else 0,
        )

    # ---------------------------------------------------------------- 运行

    async def astream(
        self, request: RunRequest, *, thread_id: str | None = None
    ) -> AsyncIterator[RunEvent]:
        """执行一次运行并流式产出事件。"""
        resolved_thread = thread_id or await self.resolve_thread(request)
        context = RunContext(
            thread_id=resolved_thread,
            channel=request.channel,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            text=request.text,
        )
        started = perf_counter()

        # 同一会话串行，避免历史互相踩踏；不同会话完全并发。
        async with self._lock(resolved_thread):
            await notify_start(self.observers, context)
            stored = await self.store.load(resolved_thread)
            thread_info = await self.store.get_thread(resolved_thread) if stored else None
            if self.context is not None:
                history, stats = await self.context.prepare(resolved_thread, stored)
                self._stats[resolved_thread] = stats
            else:
                history, stats = stored, ContextStats()
            produced: list[BaseMessage] = []
            user_message = human_message(request.text, request.images)
            # 跨天或长间隔续聊时固化当前时间上下文（并落库），避免模型沿用旧对话日期，
            # 同时保持历史前缀不变、不破坏 Prefix Cache；对外展示时再剥离该标记。
            time_context = build_time_context(
                last_active_at=thread_info.updated_at if thread_info else None,
                timezone_name=self.settings.app.runtime.timezone,
                gap_hours=self.settings.app.agent.time_context_gap_hours,
            )
            if time_context:
                user_message = append_time_context(user_message, time_context)
            # 按间隔把人设锚点固化在用户消息上并一并落库：后续轮次直接复用同一历史前缀，
            # 既能周期性提醒人设，又不会破坏 Prefix Cache；对外展示时再剥离该标记。
            has_summary = stats.compressed or (bool(history) and is_summary_message(history[0]))
            reminder = self.bundle.agent.persona_reminder
            interval = self.bundle.agent.persona_reminder_interval
            if reminder and should_attach_persona_reminder(
                history, interval=interval, has_summary=has_summary
            ):
                user_message = append_persona_reminder(user_message, reminder)
            started_ok = False
            steps = 0
            tool_calls = 0
            approvals = 0
            rejections = 0
            usage: dict[str, int] = {}
            error: str | None = None
            llm_elapsed = 0.0
            saw_llm = False
            tool_elapsed = 0.0
            saw_tool = False
            approval_elapsed = 0.0
            saw_approval = False
            ttft: float | None = None
            cancelled = False
            try:
                async for event in self.bundle.agent.astream(
                    [*history, user_message],
                    thread_id=resolved_thread,
                    messages_out=produced,
                ):
                    await notify_event(self.observers, context, event)
                    if event.type == "step_end":
                        if event.llm_elapsed is not None:
                            saw_llm = True
                            llm_elapsed += event.llm_elapsed
                        if event.tool_elapsed is not None:
                            saw_tool = True
                            tool_elapsed += event.tool_elapsed
                        if event.approval_elapsed is not None:
                            saw_approval = True
                            approval_elapsed += event.approval_elapsed
                        if ttft is None and event.ttft is not None:
                            ttft = event.ttft
                    elif event.type == "run_end":
                        started_ok = True
                        steps = event.step
                        usage = dict(event.usage or {})
                        if event.llm_elapsed is not None:
                            saw_llm = True
                            llm_elapsed = event.llm_elapsed
                        if event.tool_elapsed is not None:
                            saw_tool = True
                            tool_elapsed = event.tool_elapsed
                        if event.approval_elapsed is not None:
                            saw_approval = True
                            approval_elapsed = event.approval_elapsed
                        if event.ttft is not None:
                            ttft = event.ttft
                    elif event.type == "tool_call":
                        tool_calls += 1
                    elif event.type == "approval":
                        if event.tool_ok:
                            approvals += 1
                        else:
                            rejections += 1
                    elif event.type == "error":
                        error = event.text
                    yield event
            except asyncio.CancelledError:
                cancelled = True
                if error is None:
                    error = "运行已中断"
                raise
            finally:
                await _shield_await(
                    self._finalize_run(
                        resolved_thread=resolved_thread,
                        request=request,
                        context=context,
                        user_message=user_message,
                        produced=produced,
                        stats=stats,
                        started=started,
                        started_ok=started_ok,
                        cancelled=cancelled,
                        steps=steps,
                        tool_calls=tool_calls,
                        approvals=approvals,
                        rejections=rejections,
                        usage=usage,
                        error=error,
                        run_llm=llm_elapsed if saw_llm else None,
                        ttft=ttft,
                        run_tool=tool_elapsed if saw_tool else None,
                        run_approval=approval_elapsed if saw_approval else None,
                    )
                )

    async def _finalize_run(
        self,
        *,
        resolved_thread: str,
        request: RunRequest,
        context: RunContext,
        user_message: BaseMessage,
        produced: list[BaseMessage],
        stats: ContextStats,
        started: float,
        started_ok: bool,
        cancelled: bool,
        steps: int,
        tool_calls: int,
        approvals: int,
        rejections: int,
        usage: dict[str, int],
        error: str | None,
        run_llm: float | None,
        ttft: float | None,
        run_tool: float | None,
        run_approval: float | None,
    ) -> None:
        """在屏蔽二次取消的前提下完成历史落库、日志与观察者收尾。"""
        # 残段（有 tool_call 没有对应结果）不能落库，否则下一轮会被 provider 拒绝；
        # 用户提问本身无论如何都要留下。
        committed = truncate_dangling_tool_calls(produced)
        await self.store.append(
            resolved_thread,
            [user_message, *committed],
            channel=request.channel,
            conversation_id=request.conversation_id,
        )
        stored_info = await self.store.get_thread(resolved_thread)
        elapsed = perf_counter() - started
        ok = started_ok and not cancelled and error is None
        logger.info(
            "runner.run_finished",
            thread_id=resolved_thread,
            channel=request.channel,
            user_id=request.user_id,
            ok=ok,
            cancelled=cancelled,
            steps=steps,
            tool_calls=tool_calls,
            approvals=approvals,
            rejections=rejections,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            elapsed=round(elapsed, 3),
            llm_elapsed=round(run_llm, 3) if run_llm is not None else None,
            ttft=round(ttft, 3) if ttft is not None else None,
            tool_elapsed=round(run_tool, 3) if run_tool is not None else None,
            approval_elapsed=round(run_approval, 3) if run_approval is not None else None,
            history_messages=stored_info.message_count if stored_info else 0,
            new_messages=len(committed) + 1,
            # 上下文占用：排查「它为什么忘了」全靠这几个数
            ctx_messages=stats.messages,
            ctx_tokens=stats.tokens,
            ctx_budget=stats.budget,
            ctx_compressed=stats.compressed,
            ctx_summarized=stats.summarized,
            error=error,
        )
        if not started_ok:
            logger.warning(
                "runner.run_incomplete",
                thread_id=resolved_thread,
                committed=len(committed),
                cancelled=cancelled,
            )
        await notify_end(
            self.observers,
            context,
            RunSummary(
                ok=ok,
                steps=steps,
                tool_calls=tool_calls,
                approvals=approvals,
                rejections=rejections,
                usage=usage,
                elapsed=elapsed,
                llm_elapsed=run_llm,
                ttft=ttft,
                tool_elapsed=run_tool,
                approval_elapsed=run_approval,
                error=error,
            ),
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
            messages=await self.history(resolved_thread),
            usage=usage,
            events=events,
            ok=error is None,
            error=error,
        )


async def _shield_await(coro: Coroutine[Any, Any, None]) -> None:
    """运行收尾协程并尽量抵御二次取消，避免数据库事务被中途撕裂。"""
    task = asyncio.create_task(coro)
    cancelled_again = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled_again = True
    if cancelled_again:
        raise asyncio.CancelledError


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
    "RunnerLike",
    "truncate_dangling_tool_calls",
]
