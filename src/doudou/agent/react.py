"""ReAct 循环：推理 → 调用工具 → 观察 → 再推理，直到给出最终答复。

循环只有「推理」和「执行」两个动作，过程以 ``RunEvent`` 流式吐出，由 CLI / SSE /
飞书卡片各自渲染。工具调用按 index 手工累积（只依赖 ``tool_call_chunks`` 协议），
不假定 provider 的返回形态，因此对 OpenAI / Anthropic / 兼容网关都成立。
单步失败不会中断整个循环：工具异常会转成错误 ``ToolMessage`` 回填给模型，
让它有机会换一种做法，而不是把异常抛到用户面前。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from doudou.agent.context import ResultOffloader, is_summary_message
from doudou.agent.events import RunEvent, ToolOutcome
from doudou.agent.messages import (
    append_persona_reminder,
    has_persona_reminder,
    normalize_persona_reminder,
    should_attach_persona_reminder,
    strip_display_annotations_text,
)
from doudou.observability.logging import get_logger
from doudou.security.secrets import redact
from doudou.tools.decorators import annotations_of
from doudou.utils.text import content_to_text, stringify_tool_result, truncate

logger = get_logger(__name__)


#: 需要人工确认时，向调用方发起的请求。
@dataclass(frozen=True, slots=True)
class ToolApprovalRequest:
    tool_name: str
    args: dict[str, Any]
    step: int
    call_id: str
    thread_id: str | None = None


#: 确认回调：返回 ``True`` 表示允许执行。
ApprovalHook = Callable[[ToolApprovalRequest], Awaitable[bool]]


@dataclass(slots=True)
class RunResult:
    """一次运行的完整结果。"""

    text: str
    steps: int
    messages: list[BaseMessage]
    usage: dict[str, int] = field(default_factory=dict)
    events: list[RunEvent] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    llm_elapsed: float | None = None
    ttft: float | None = None
    tool_elapsed: float | None = None
    approval_elapsed: float | None = None


@dataclass(slots=True)
class _ModelTurn:
    """累计一轮模型的输出。"""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: ``tool_call_id`` -> 参数解析失败原因。
    arg_errors: dict[str, str] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    llm_elapsed: float | None = None
    ttft: float | None = None


class ReactAgent:
    """把「聊天模型 + 工具列表」跑成一个 ReAct 循环。"""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        fallback_models: Sequence[BaseChatModel] = (),
        tools: Sequence[BaseTool] = (),
        system_prompt: str = "",
        max_steps: int = 8,
        max_tool_calls_per_step: int = 8,
        interrupt_on: dict[str, bool] | None = None,
        approval_hook: ApprovalHook | None = None,
        tool_timeout: float | None = None,
        offloader: ResultOffloader | None = None,
        persona_reminder: str = "",
        persona_reminder_interval: int = 5,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须 >= 1")
        self.model = model
        self.fallback_models = list(fallback_models)
        self.tools = list(tools)
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_tool_calls_per_step = max(1, max_tool_calls_per_step)
        self.interrupt_on = dict(interrupt_on or {})
        self.approval_hook = approval_hook
        self.tool_timeout = tool_timeout
        #: 大结果卸载。放在这里而不是通道层：工具结果是唯一会「一条顶掉整个上下文」
        #: 的来源，在它产生的地方处理最直接。
        self.offloader = offloader
        self.persona_reminder = normalize_persona_reminder(persona_reminder)
        self.persona_reminder_interval = max(0, persona_reminder_interval)
        self._tools_by_name = {tool.name: tool for tool in self.tools}
        #: 没有工具时不绑定，避免部分 provider 在空工具列表上报错。
        self._runnables = [
            candidate.bind_tools(self.tools) if self.tools else candidate
            for candidate in [model, *self.fallback_models]
        ]

    # ---------------------------------------------------------------- 对外入口

    async def astream(
        self,
        messages: Sequence[BaseMessage],
        *,
        thread_id: str | None = None,
        messages_out: list[BaseMessage] | None = None,
    ) -> AsyncIterator[RunEvent]:
        """执行 ReAct 循环，流式产出事件。

        Args:
            messages: 完整的输入上下文。若首条不是 ``SystemMessage``，
                会自动在最前面插入 :attr:`system_prompt`。
            thread_id: 会话标识，仅用于事件标注。
            messages_out: 传入一个列表即可获得本轮新增的消息
                （``AIMessage`` / ``ToolMessage``），用于维护多轮历史。

        Yields:
            RunEvent: 依次为 ``run_start`` → (``step_start`` → ``text_delta``* →
            ``tool_call``* → ``tool_result``* → ``step_end``)+ → ``run_end``（或 ``error``）。
        """
        history, has_summary = _prepend_system_prompt(messages, self.system_prompt)
        history = _ensure_turn_persona_reminder(
            history,
            reminder=self.persona_reminder,
            interval=self.persona_reminder_interval,
            has_summary=has_summary,
        )

        usage: dict[str, int] = {}
        total_llm_elapsed = 0.0
        total_tool_elapsed = 0.0
        total_approval_elapsed = 0.0
        saw_approval = False
        first_ttft: float | None = None
        yield RunEvent.run_start(thread_id=thread_id)

        for step in range(1, self.max_steps + 1):
            yield RunEvent.step_start(step, thread_id=thread_id)

            turn = _ModelTurn()
            async for event in self._reason(history, step=step, thread_id=thread_id, turn=turn):
                yield event
            _merge_usage(usage, turn.usage)
            if turn.llm_elapsed is not None:
                total_llm_elapsed += turn.llm_elapsed
            if first_ttft is None and turn.ttft is not None:
                first_ttft = turn.ttft

            history.append(AIMessage(content=turn.text, tool_calls=list(turn.tool_calls)))
            self._mirror(history, messages_out, 1)

            if not turn.tool_calls:
                yield RunEvent.step_end(
                    step,
                    llm_elapsed=turn.llm_elapsed,
                    ttft=turn.ttft,
                    tool_elapsed=0.0,
                    thread_id=thread_id,
                )
                yield RunEvent.run_end(
                    turn.text,
                    step=step,
                    usage=usage,
                    llm_elapsed=total_llm_elapsed,
                    ttft=first_ttft,
                    tool_elapsed=total_tool_elapsed,
                    approval_elapsed=total_approval_elapsed if saw_approval else None,
                    thread_id=thread_id,
                )
                return

            calls = turn.tool_calls[: self.max_tool_calls_per_step]
            skipped = turn.tool_calls[self.max_tool_calls_per_step :]
            if skipped:
                logger.warning(
                    "agent.tool_calls_truncated",
                    requested=len(turn.tool_calls),
                    executed=len(calls),
                    step=step,
                )

            for call in calls:
                yield RunEvent.tool_call(
                    step=step,
                    name=call["name"],
                    call_id=call["id"],
                    args=call.get("args") or {},
                    thread_id=thread_id,
                )

            outcomes = await self._execute_calls(calls, step=step, turn=turn, thread_id=thread_id)

            for outcome in outcomes:
                if outcome.approval is not None:
                    # 先报确认结果，再报执行结果：顺序反过来会让人以为
                    # 「先执行了、后来才补的确认」。
                    yield RunEvent.approval(
                        step=step,
                        name=outcome.name,
                        call_id=outcome.call_id,
                        approved=outcome.approval,
                        reason=outcome.approval_reason,
                        approval_elapsed=outcome.approval_elapsed,
                        thread_id=thread_id,
                    )
                history.append(
                    ToolMessage(
                        content=outcome.content,
                        tool_call_id=outcome.call_id,
                        name=outcome.name,
                        status="success" if outcome.ok else "error",
                    )
                )
                yield RunEvent.tool_result(
                    step=step,
                    name=outcome.name,
                    call_id=outcome.call_id,
                    result=outcome.content,
                    ok=outcome.ok,
                    elapsed=outcome.elapsed,
                    thread_id=thread_id,
                )

            # 被截断的调用也必须有应答：上面的 AIMessage 带着全部 tool_calls，
            # 缺了对应的 ToolMessage，OpenAI 兼容接口下一轮会直接 400。
            # 这里只写进历史（让模型知道没执行、可以分批重发），不对外发事件。
            for call in skipped:
                history.append(
                    ToolMessage(
                        content=(
                            f"错误：单步最多执行 {self.max_tool_calls_per_step} 个工具调用，"
                            f"本次调用 {call['name']} 未执行，如仍需要请在下一步重新发起。"
                        ),
                        tool_call_id=call["id"],
                        name=call["name"],
                        status="error",
                    )
                )
            self._mirror(history, messages_out, len(outcomes) + len(skipped))

            tool_intervals = [
                (item.started_at, item.finished_at)
                for item in outcomes
                if item.started_at is not None and item.finished_at is not None
            ]
            step_tool_elapsed = _union_duration(tool_intervals)
            approval_intervals = [
                (item.approval_started_at, item.approval_finished_at)
                for item in outcomes
                if item.approval_started_at is not None and item.approval_finished_at is not None
            ]
            step_approval_elapsed = (
                _union_duration(approval_intervals) if approval_intervals else None
            )
            total_tool_elapsed += step_tool_elapsed
            if step_approval_elapsed is not None:
                saw_approval = True
                total_approval_elapsed += step_approval_elapsed

            yield RunEvent.step_end(
                step,
                llm_elapsed=turn.llm_elapsed,
                ttft=turn.ttft,
                tool_elapsed=step_tool_elapsed,
                approval_elapsed=step_approval_elapsed,
                thread_id=thread_id,
            )

        message = f"达到最大步数 {self.max_steps}，已停止执行。"
        logger.warning("agent.max_steps_reached", max_steps=self.max_steps, thread_id=thread_id)
        yield RunEvent.error(message, step=self.max_steps, thread_id=thread_id)

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        *,
        thread_id: str | None = None,
    ) -> RunResult:
        """执行一次循环并返回聚合结果（不关心中间事件时使用）。"""
        produced: list[BaseMessage] = []
        events: list[RunEvent] = []
        text = ""
        steps = 0
        usage: dict[str, int] = {}
        error: str | None = None
        llm_elapsed = 0.0
        saw_llm = False
        tool_elapsed = 0.0
        saw_tool = False
        approval_elapsed = 0.0
        saw_approval = False
        ttft: float | None = None

        async for event in self.astream(messages, thread_id=thread_id, messages_out=produced):
            events.append(event)
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
                text = event.text
                steps = event.step
                usage = event.usage
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
            elif event.type == "error":
                error = event.text
                steps = event.step

        history, _ = _prepend_system_prompt(messages, self.system_prompt)
        history.extend(produced)

        return RunResult(
            text=text,
            steps=steps,
            messages=history,
            usage=usage,
            events=events,
            ok=error is None,
            error=error,
            llm_elapsed=llm_elapsed if saw_llm else None,
            ttft=ttft,
            tool_elapsed=tool_elapsed if saw_tool else None,
            approval_elapsed=approval_elapsed if saw_approval else None,
        )

    # ---------------------------------------------------------------- 推理

    async def _reason(
        self,
        history: list[BaseMessage],
        *,
        step: int,
        thread_id: str | None,
        turn: _ModelTurn,
    ) -> AsyncIterator[RunEvent]:
        """按配置顺序尝试模型；已经输出文本的请求不能透明重放。"""
        reason_started = perf_counter()
        for index in range(len(self._runnables)):
            attempt = _ModelTurn()
            emitted = False
            try:
                async for event in self._reason_once(
                    history,
                    step=step,
                    thread_id=thread_id,
                    turn=attempt,
                    model_index=index,
                    started_at=reason_started,
                ):
                    emitted = True
                    yield event
            except Exception as exc:
                # 工具尚未执行，未对外输出的残片可丢弃；已输出则抛出原错误，
                # 避免用户收到主模型半句 + 备用模型完整答案的混合内容。
                if emitted or index == len(self._runnables) - 1:
                    raise
                logger.warning(
                    "agent.model_fallback",
                    step=step,
                    thread_id=thread_id,
                    failed_index=index,
                    next_index=index + 1,
                    error_type=type(exc).__name__,
                )
                continue
            turn.text = attempt.text
            turn.tool_calls = attempt.tool_calls
            turn.arg_errors = attempt.arg_errors
            turn.usage = attempt.usage
            turn.llm_elapsed = perf_counter() - reason_started
            turn.ttft = attempt.ttft
            logger.info(
                "agent.model_finished",
                step=step,
                thread_id=thread_id,
                elapsed=round(turn.llm_elapsed, 3),
                ttft=round(turn.ttft, 3) if turn.ttft is not None else None,
                tool_calls=len(turn.tool_calls),
                text_chars=len(turn.text),
            )
            return

    async def _reason_once(
        self,
        history: list[BaseMessage],
        *,
        step: int,
        thread_id: str | None,
        turn: _ModelTurn,
        model_index: int,
        started_at: float,
    ) -> AsyncIterator[RunEvent]:
        """跑一轮模型，把文本与工具调用累积进 ``turn``。"""
        pending: dict[int, dict[str, Any]] = {}
        saw_chunks = False
        direct_calls: list[dict[str, Any]] = []
        first_token_at: float | None = None

        # 模型调用起止在 INFO 留下轻量边界（不含正文，避免噪声与敏感信息），
        # 方便定位卡在模型还是工具；完整入参摘要保留在 DEBUG。
        logger.info(
            "agent.model_started",
            step=step,
            thread_id=thread_id,
            model_index=model_index,
            context_messages=len(history),
            tools=len(self._tools_by_name),
        )
        logger.debug(
            "agent.model_call",
            step=step,
            thread_id=thread_id,
            context_messages=len(history),
            tools=len(self._tools_by_name),
            last_user=_log_safe(_last_user_preview(history)),
        )

        async for chunk in self._runnables[model_index].astream(history):
            text = content_to_text(getattr(chunk, "content", None))
            if text:
                if first_token_at is None:
                    first_token_at = perf_counter()
                turn.text += text
                yield RunEvent.text_delta(text, step=step, thread_id=thread_id)

            raw_chunks = getattr(chunk, "tool_call_chunks", None) or []
            if raw_chunks and first_token_at is None:
                first_token_at = perf_counter()
            for raw in raw_chunks:
                saw_chunks = True
                index = raw.get("index")
                if not isinstance(index, int):
                    index = len(pending)
                slot = pending.setdefault(index, {"id": None, "name": None, "args": ""})
                if raw.get("id"):
                    slot["id"] = raw["id"]
                if raw.get("name"):
                    slot["name"] = raw["name"]
                fragment = raw.get("args")
                if fragment:
                    slot["args"] += fragment if isinstance(fragment, str) else json.dumps(fragment)

            if not saw_chunks:
                raw_direct = getattr(chunk, "tool_calls", None) or []
                if raw_direct and first_token_at is None:
                    first_token_at = perf_counter()
                for call in raw_direct:
                    direct_calls.append(dict(call))

            _merge_usage(turn.usage, getattr(chunk, "usage_metadata", None) or {})

        if first_token_at is not None:
            turn.ttft = max(0.0, first_token_at - started_at)
        turn.tool_calls, turn.arg_errors = _finalize_tool_calls(pending, direct_calls)
        if turn.tool_calls:
            logger.debug(
                "agent.tool_calls",
                step=step,
                thread_id=thread_id,
                tools=[call["name"] for call in turn.tool_calls],
            )

    # ---------------------------------------------------------------- 执行

    async def _execute_calls(
        self,
        calls: Sequence[dict[str, Any]],
        *,
        step: int,
        turn: _ModelTurn,
        thread_id: str | None = None,
    ) -> list[ToolOutcome]:
        """执行一步内的所有工具调用，返回与 ``calls`` 同序的结果。

        只读工具之间互不依赖，连续的只读调用并发执行以省时间；
        有副作用的工具（含未注解、被保守视为有副作用的）是「栅栏」：
        单独执行、前后都不与其他调用重叠，保证「先写后读」「连续两次编辑
        同一文件」这类依赖模型给出顺序的场景结果确定。
        """
        outcomes: list[ToolOutcome] = []
        batch: list[dict[str, Any]] = []

        async def flush() -> None:
            if batch:
                outcomes.extend(
                    await asyncio.gather(
                        *(
                            self._execute_tool(call, step=step, turn=turn, thread_id=thread_id)
                            for call in batch
                        )
                    )
                )
                batch.clear()

        for call in calls:
            if self._has_side_effects(str(call.get("name") or "")):
                await flush()
                outcomes.append(
                    await self._execute_tool(call, step=step, turn=turn, thread_id=thread_id)
                )
            else:
                batch.append(call)
        await flush()
        return outcomes

    def _has_side_effects(self, tool_name: str) -> bool:
        # 不存在的工具会在 _execute_tool 里直接返回错误，没有副作用，可并发。
        tool = self._tools_by_name.get(tool_name)
        return tool is not None and annotations_of(tool).side_effects

    def _needs_approval(self, tool_name: str) -> bool:
        if self.interrupt_on.get(tool_name):
            return True
        return bool(self.interrupt_on.get("*"))

    async def _execute_tool(
        self,
        call: dict[str, Any],
        *,
        step: int,
        turn: _ModelTurn,
        thread_id: str | None = None,
    ) -> ToolOutcome:
        name = str(call.get("name") or "")
        call_id = str(call.get("id") or "")
        args = call.get("args") or {}

        tool = self._tools_by_name.get(name)
        if tool is None:
            available = "、".join(sorted(self._tools_by_name)) or "（无）"
            return ToolOutcome(
                call_id=call_id,
                name=name,
                args=args,
                ok=False,
                content=f"错误：不存在名为 {name!r} 的工具。可用工具：{available}。",
            )

        if error := turn.arg_errors.get(call_id):
            return ToolOutcome(
                call_id=call_id,
                name=name,
                args=args,
                ok=False,
                content=f"错误：工具 {name} 的参数无法解析（{error}），请重新给出合法 JSON。",
            )

        # 需要人工确认的工具：先问人，再把结果带回 ToolOutcome ——
        # 审计要能答「这次操作有没有经过人」，这件事只有这里知道。
        approval: bool | None = None
        approval_reason = ""
        approval_elapsed: float | None = None
        approval_started: float | None = None
        approval_finished: float | None = None
        if self._needs_approval(name):
            request = ToolApprovalRequest(
                tool_name=name,
                args=args,
                step=step,
                call_id=call_id,
                thread_id=thread_id,
            )
            if self.approval_hook is None:
                return ToolOutcome(
                    call_id=call_id,
                    name=name,
                    args=args,
                    ok=False,
                    approval=False,
                    approval_reason="需要人工确认，但当前运行没有确认回调",
                    content=(
                        f"错误：工具 {name} 被配置为需要人工确认，"
                        "但当前运行未提供确认回调，已跳过执行。"
                    ),
                )
            approval_started = perf_counter()
            approved = await self.approval_hook(request)
            approval_finished = perf_counter()
            approval_elapsed = approval_finished - approval_started
            if not approved:
                logger.info("agent.tool_rejected", tool=name, step=step)
                return ToolOutcome(
                    call_id=call_id,
                    name=name,
                    args=args,
                    ok=False,
                    approval=False,
                    approval_reason="用户拒绝",
                    approval_elapsed=approval_elapsed,
                    approval_started_at=approval_started,
                    approval_finished_at=approval_finished,
                    content=f"用户拒绝执行工具 {name}，请改用其他方式或询问用户。",
                )
            approval, approval_reason = True, "用户同意"

        started = perf_counter()
        # 工具执行是 Agent 真正产生副作用的地方，出入参必须留痕。
        # 参数与结果都可能含敏感值（api_key / token），先 redact 再截断。
        # 超时优先用工具自己声明的（出网工具需要的余量不同），否则用全局默认。
        timeout = annotations_of(tool).timeout or self.tool_timeout
        logger.info(
            "agent.tool_started",
            tool=name,
            step=step,
            call_id=call_id,
            args=_log_safe(args),
            timeout=timeout,
        )
        try:
            invocation = tool.ainvoke(args)
            result = await asyncio.wait_for(invocation, timeout) if timeout else await invocation
        except TimeoutError:
            finished = perf_counter()
            elapsed = finished - started
            logger.warning(
                "agent.tool_timeout",
                tool=name,
                step=step,
                timeout=timeout,
                elapsed=round(elapsed, 4),
            )
            return ToolOutcome(
                call_id=call_id,
                name=name,
                args=args,
                ok=False,
                elapsed=elapsed,
                started_at=started,
                finished_at=finished,
                approval=approval,
                approval_reason=approval_reason,
                approval_elapsed=approval_elapsed,
                approval_started_at=approval_started,
                approval_finished_at=approval_finished,
                content=f"错误：工具 {name} 执行超时（>{timeout}s）。",
            )
        except Exception as exc:
            finished = perf_counter()
            elapsed = finished - started
            logger.warning(
                "agent.tool_failed",
                tool=name,
                step=step,
                error_type=type(exc).__name__,
                error=str(exc),
                elapsed=round(elapsed, 4),
                exc_info=True,
            )
            return ToolOutcome(
                call_id=call_id,
                name=name,
                args=args,
                ok=False,
                elapsed=elapsed,
                started_at=started,
                finished_at=finished,
                approval=approval,
                approval_reason=approval_reason,
                approval_elapsed=approval_elapsed,
                approval_started_at=approval_started,
                approval_finished_at=approval_finished,
                content=f"错误：工具 {name} 执行失败（{type(exc).__name__}: {exc}）。",
            )

        finished = perf_counter()
        elapsed = finished - started
        content = stringify_tool_result(result)
        # 超大结果落盘：一条 5 万字的返回值会把后续所有请求挤掉，
        # 而模型通常只需要结论 —— 留预览 + 路径，需要细节时再去读。
        if self.offloader is not None:
            content = self.offloader.maybe_offload(content, name=name, call_id=call_id)
        logger.info(
            "agent.tool_finished",
            tool=name,
            step=step,
            call_id=call_id,
            elapsed=round(elapsed, 4),
            result=_log_safe(content),
        )
        return ToolOutcome(
            call_id=call_id,
            name=name,
            args=args,
            ok=True,
            elapsed=elapsed,
            started_at=started,
            finished_at=finished,
            approval=approval,
            approval_reason=approval_reason,
            approval_elapsed=approval_elapsed,
            approval_started_at=approval_started,
            approval_finished_at=approval_finished,
            content=content,
        )

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _mirror(history: list[BaseMessage], sink: list[BaseMessage] | None, count: int) -> None:
        """把 ``history`` 末尾新增的 ``count`` 条消息同步到调用方的列表。"""
        if sink is not None and count > 0:
            sink.extend(history[-count:])


def _finalize_tool_calls(
    pending: dict[int, dict[str, Any]], direct_calls: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """把累积的 ``tool_call_chunks`` 收敛成完整的工具调用列表。"""
    calls: list[dict[str, Any]] = []
    arg_errors: dict[str, str] = {}

    if not pending:
        for index, call in enumerate(direct_calls):
            calls.append(
                {
                    "name": str(call.get("name") or ""),
                    "args": call.get("args") or {},
                    "id": str(call.get("id") or f"call_{index}"),
                    "type": "tool_call",
                }
            )
        return [call for call in calls if call["name"]], arg_errors

    for index in sorted(pending):
        slot = pending[index]
        name = slot.get("name")
        if not name:
            # 残片（name 未到齐），丢弃并留痕。
            logger.debug("agent.tool_call_fragment_dropped", index=index)
            continue
        call_id = str(slot.get("id") or f"call_{index}")
        raw_args = str(slot.get("args") or "")
        if raw_args:
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                arg_errors[call_id] = f"非法 JSON：{exc.msg}"
                parsed = {}
        else:
            parsed = {}
        if not isinstance(parsed, dict):
            arg_errors[call_id] = f"参数必须是 JSON 对象，收到 {type(parsed).__name__}"
            parsed = {}
        calls.append({"name": str(name), "args": parsed, "id": call_id, "type": "tool_call"})

    return calls, arg_errors


def _last_user_preview(history: list[BaseMessage], *, limit: int = 200) -> str:
    """取最后一条用户消息的文本，用于日志中的「本次输入是什么」。"""
    for message in reversed(history):
        if isinstance(message, HumanMessage):
            return truncate(strip_display_annotations_text(content_to_text(message.content)), limit)
    return ""


def _log_safe(value: Any, *, limit: int = 500) -> Any:
    """日志专用的值处理：先模糊化敏感字段，再截断过长的内容。

    - 字典/列表递归打码（`api_key`、`token`、`password` 等键）；
    - 字符串按 ``limit`` 截断，避免把整个工具返回值塞进日志。
    """
    redacted = redact(value)
    if isinstance(redacted, str):
        return truncate(redacted, limit)
    return redacted


def _merge_usage(target: dict[str, int], source: Any) -> None:
    """累加 ``usage_metadata``。"""
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if isinstance(value, int):
            target[key] = target.get(key, 0) + value


def _prepend_system_prompt(
    messages: Sequence[BaseMessage], system_prompt: str
) -> tuple[list[BaseMessage], bool]:
    """确保系统提示词在首位，同时保留摘要压缩产出的首条摘要内容。"""
    history = list(messages)
    has_summary = bool(history) and is_summary_message(history[0])
    if system_prompt:
        first = history[0] if history else None
        if not isinstance(first, SystemMessage):
            history.insert(0, SystemMessage(content=system_prompt))
        elif has_summary:
            # 历史摘要占用首条 SystemMessage 时，不能跳过系统提示词，
            # 否则会丢失人设、长期记忆与技能索引；合并成单条 SystemMessage 兼容各 provider。
            summary_text = content_to_text(first.content)
            history[0] = SystemMessage(content=f"{system_prompt}\n\n{summary_text}")
    return history, has_summary


def _ensure_turn_persona_reminder(
    history: list[BaseMessage],
    *,
    reminder: str,
    interval: int,
    has_summary: bool = False,
) -> list[BaseMessage]:
    """在直接调用 ReactAgent（未经过 AgentRunner）时，按相同间隔在循环开始前补齐人设锚点。

    只在本轮开始前判定一次，循环内所有工具调用步都共享完全一致的前缀；
    经过 AgentRunner 时，锚点已提前附着并持久化在最新的 HumanMessage 上，此处直接跳过。
    """
    if not reminder or interval <= 0:
        return history

    last_human_idx = -1
    for idx in range(len(history) - 1, -1, -1):
        if isinstance(history[idx], HumanMessage):
            last_human_idx = idx
            break
    if last_human_idx < 0:
        return history

    target = history[last_human_idx]
    assert isinstance(target, HumanMessage)
    if has_persona_reminder(target):
        return history

    if not should_attach_persona_reminder(
        history[:last_human_idx], interval=interval, has_summary=has_summary
    ):
        return history

    patched = list(history)
    patched[last_human_idx] = append_persona_reminder(target, reminder)
    return patched


def _union_duration(intervals: Sequence[tuple[float, float]]) -> float:
    """合并重叠的时间区间，算出并发/串行混合场景下的精确墙钟耗时（秒）。"""
    if not intervals:
        return 0.0
    ordered = sorted(intervals, key=lambda pair: pair[0])
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if end <= start:
            continue
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += max(0.0, cur_end - cur_start)
            cur_start, cur_end = start, end
    total += max(0.0, cur_end - cur_start)
    return total


def build_messages(
    system_prompt: str,
    history: Sequence[BaseMessage] = (),
    user_text: str = "",
) -> list[BaseMessage]:
    """组装一次运行的输入上下文。"""
    messages: list[BaseMessage] = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))
    messages.extend(history)
    if user_text:
        messages.append(HumanMessage(content=user_text))
    return messages


__all__ = [
    "ApprovalHook",
    "ReactAgent",
    "RunResult",
    "ToolApprovalRequest",
    "build_messages",
]
