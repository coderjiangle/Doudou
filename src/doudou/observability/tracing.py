"""Langfuse 追踪：把一次运行的结构化事件发成 trace。

Langfuse v3 的 SDK 是 OTEL 风格的：span 必须成对地 ``with`` 进入与退出。而观察者是
回调式的（``on_run_start`` / ``on_event`` / ``on_run_end`` 在不同时刻），跨回调持有
span 会带来两个问题：运行中途异常或超时时 span 永远悬着，并发会话交错时还需要额外
的上下文管理。所以这里把事件攒起来，在 ``on_run_end`` 里一次发出。运行只有几秒到
几十秒，追踪晚几秒出现完全可以接受；攒的同时有上限，长运行不会把内存撑爆。

已知限制：``RunEvent`` 里的 token 用量是整轮累计的（``run_end.usage``），没有按
step 拆分，因此 generation 的 ``usage_details`` 只挂在运行的根节点上。要按 step 拆
就得改 ReAct 循环的事件粒度，暂不做。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from doudou.agent.events import RunEvent
from doudou.observability.logging import get_logger
from doudou.observability.observers import BaseObserver, RunContext, RunSummary

logger = get_logger(__name__)

#: 一次运行最多保留多少条事件用于追踪（防止超长运行把内存吃掉）。
MAX_BUFFERED_EVENTS = 500
#: 工具参数/结果进入 trace 的长度上限。
MAX_PAYLOAD_CHARS = 1000


@dataclass(slots=True)
class _StepBuffer:
    """按 step 聚合的事件。"""

    step: int
    text: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.step, "text": _clip(self.text), "tools": list(self.tools)}


class LangfuseTracer:
    """Langfuse 客户端的薄封装。

    ``client`` 是 duck-typed 的（只要求 ``start_as_current_span`` /
    ``create_event`` / ``flush`` / ``shutdown``），因此测试可以注入假客户端，
    **不需要网络也不需要真 key**。
    """

    def __init__(self, client: Any, *, release: str | None = None) -> None:
        self.client = client
        self.release = release

    # ---------------------------------------------------------------- 对外

    def emit(self, context: RunContext, summary: RunSummary, events: Sequence[RunEvent]) -> None:
        """把一次运行发成 trace（同步；调用方负责放进线程池）。"""
        steps = _group_steps(events)
        failed = not summary.ok
        metadata = {
            "channel": context.channel,
            "conversation_id": context.conversation_id,
            "steps": summary.steps,
            "tool_calls": summary.tool_calls,
            "approvals": summary.approvals,
            "rejections": summary.rejections,
            "elapsed": round(summary.elapsed, 3),
        }

        with self.client.start_as_current_span(
            name=f"run:{context.channel}",
            input=_clip(context.text),
            metadata=metadata,
            level="ERROR" if failed else "DEFAULT",
            status_message=summary.error or None,
            release=self.release,
        ) as root:
            root.update(usage_details=_usage_details(summary))
            if hasattr(root, "update_trace"):
                # session_id 用 thread_id：这样一条会话的所有轮次在 Langfuse 里
                # 是一条可回溯的线，而不是一堆孤立的 trace。
                root.update_trace(
                    user_id=context.user_id,
                    session_id=context.thread_id,
                    tags=[context.channel],
                )

            for step in steps:
                with self.client.start_as_current_span(name=f"step {step.step}") as span:
                    with self.client.start_as_current_generation(
                        name="model",
                        output=_clip(step.text) or None,
                    ):
                        pass  # 用量挂在根节点，见模块 docstring
                    for tool in step.tools:
                        self._emit_tool(span, tool)

            if failed:
                self.client.create_event(
                    name="error",
                    level="ERROR",
                    metadata={"error": _clip(summary.error or "未知错误")},
                )

    def flush(self) -> None:
        self.client.flush()

    def shutdown(self) -> None:
        try:
            self.client.shutdown()
        except Exception as exc:
            logger.debug("tracing.shutdown_failed", error=str(exc))

    def auth_check(self) -> bool:
        checker = getattr(self.client, "auth_check", None)
        if checker is None:
            return False
        try:
            return bool(checker())
        except Exception as exc:
            logger.warning("tracing.auth_check_failed", error=str(exc))
            return False

    # ---------------------------------------------------------------- 内部

    def _emit_tool(self, parent: Any, tool: dict[str, Any]) -> None:
        """一个工具调用一个 span，被拒绝的标 WARNING。

        被拒绝的调用在 trace 上看应该是**显眼的**：它是「模型想做、人没让做」，
        排查「为什么没写文件」时这一条比任何日志都直接。
        """
        rejected = tool.get("approved") is False
        level = "WARNING" if rejected or not tool.get("ok", True) else "DEFAULT"
        with self.client.start_as_current_span(
            name=f"tool:{tool.get('name', '?')}",
            input=_clip(tool.get("args")) or None,
            output=_clip(tool.get("result")) or None,
            level=level,
            status_message=tool.get("error") or None,
            metadata={
                "step": tool.get("step"),
                "ok": tool.get("ok"),
                "approved": tool.get("approved"),
                "elapsed": tool.get("elapsed"),
            },
        ):
            pass


def _usage_details(summary: RunSummary) -> dict[str, int] | None:
    usage = summary.usage or {}
    details = {
        key: int(value)
        for key, value in (
            ("input", usage.get("input_tokens", 0)),
            ("output", usage.get("output_tokens", 0)),
        )
        if value
    }
    return details or None


def _group_steps(events: Sequence[RunEvent]) -> list[_StepBuffer]:
    """把事件按 step 聚成「这一步说了什么、调了哪些工具」。

    工具结果按 ``tool_call_id`` 合并回它的调用 —— 分开列会让人误以为
    结果是独立发生的事。
    """
    steps: dict[int, _StepBuffer] = {}
    order: list[int] = []
    pending: dict[str, dict[str, Any]] = {}

    for event in events:
        step = steps.get(event.step)
        if step is None:
            step = _StepBuffer(step=event.step)
            steps[event.step] = step
            order.append(event.step)

        if event.type == "text_delta":
            step.text += event.text
        elif event.type == "tool_call":
            call: dict[str, Any] = {
                "name": event.tool_name or "",
                "call_id": event.tool_call_id or "",
                "step": event.step,
                "args": event.tool_args or {},
                "ok": None,
                "approved": None,
                "result": "",
                "elapsed": None,
                "error": None,
            }
            step.tools.append(call)
            if call["call_id"]:
                pending[call["call_id"]] = call
        elif event.type == "approval":
            target = pending.get(event.tool_call_id or "")
            if target is not None:
                target["approved"] = event.tool_ok
        elif event.type == "tool_result":
            target = pending.get(event.tool_call_id or "")
            if target is None:
                # 没配对的 tool_call（事件流被截断）：照样记一条，别丢信息。
                target = {
                    "name": event.tool_name or "",
                    "call_id": event.tool_call_id or "",
                    "step": event.step,
                    "args": {},
                    "approved": None,
                }
                step.tools.append(target)
            target["ok"] = event.tool_ok
            target["result"] = event.result or ""
            target["elapsed"] = event.tool_elapsed
            if not event.tool_ok:
                target["error"] = _clip(event.result or "", 200)
        elif event.type == "error":
            step.text = step.text or event.text

    return [steps[key] for key in order]


def _clip(value: Any, limit: int = MAX_PAYLOAD_CHARS) -> Any:
    """截断进入 trace 的内容。

    trace 是给人看的排查工具，不是数据备份：一万字的文件内容贴进去，
    Langfuse 的界面只会变得难读。
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return value if not isinstance(value, str) else text
    return f"{text[:limit]}…（截断，共 {len(text)} 字）"


class TracingObserver(BaseObserver):
    """攒事件 → 运行结束时交给 :class:`LangfuseTracer` 整批发出去。"""

    name = "tracing"

    def __init__(self, tracer: LangfuseTracer) -> None:
        self.tracer = tracer
        self._events: dict[str, list[RunEvent]] = {}

    async def on_run_start(self, context: RunContext) -> None:
        self._events[context.thread_id] = []

    async def on_event(self, context: RunContext, event: RunEvent) -> None:
        buffer = self._events.get(context.thread_id)
        if buffer is None:
            buffer = self._events[context.thread_id] = []
        if len(buffer) < MAX_BUFFERED_EVENTS:
            buffer.append(event)
        elif len(buffer) == MAX_BUFFERED_EVENTS:
            logger.warning(
                "tracing.events_truncated", thread_id=context.thread_id, limit=MAX_BUFFERED_EVENTS
            )
            buffer.append(RunEvent(type="error", text="事件过多，追踪记录已截断"))

    async def on_run_end(self, context: RunContext, summary: RunSummary) -> None:
        events = self._events.pop(context.thread_id, [])

        def _emit() -> None:
            self.tracer.emit(context, summary, events)

        try:
            # SDK 调用是同步的（内部再异步批量上报），因此下放到线程 ——
            # 追踪慢不该让用户多等。
            await asyncio.to_thread(_emit)
        except Exception as exc:
            # 但这里多记一条带 thread 的日志更利于排查「哪个会话的 trace 丢了」
            logger.warning("tracing.emit_failed", thread_id=context.thread_id, error=str(exc))

    async def aclose(self) -> None:
        self._events.clear()
        await asyncio.to_thread(self.tracer.shutdown)


__all__ = [
    "MAX_BUFFERED_EVENTS",
    "MAX_PAYLOAD_CHARS",
    "LangfuseTracer",
    "TracingObserver",
]
