"""最小可用的 ReAct 循环：推理 → 调用工具 → 观察 → 再推理，直到给出最终答复。

设计要点
--------
* **不依赖框架的 agent executor**：循环本身只有 ``推理`` 与 ``执行`` 两个动作，
  事件以 :class:`~doudou.agent.events.RunEvent` 流式吐出，CLI / SSE / 飞书卡片
  各自决定怎么渲染。这一层刻意不引入 LangGraph / deepagents：
  编排逻辑（规划、委派、确认、上下文压缩）都由本仓库自己实现，
  见 ``docs/roadmap.md`` 修正记录 #3。
* **工具调用按 index 手工累积**：只依赖 ``tool_call_chunks`` 协议，
  不假定 provider 的返回形态，因此对 OpenAI / Anthropic / 兼容网关都成立。
* **单步失败不中断整个循环**：工具异常会转成错误 ``ToolMessage`` 回填给模型，
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

from doudou.agent.context import ResultOffloader
from doudou.agent.events import RunEvent, ToolOutcome
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


@dataclass(slots=True)
class _ModelTurn:
    """累计一轮模型的输出。"""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: ``tool_call_id`` -> 参数解析失败原因。
    arg_errors: dict[str, str] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)


class ReactAgent:
    """把「聊天模型 + 工具列表」跑成一个 ReAct 循环。"""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        tools: Sequence[BaseTool] = (),
        system_prompt: str = "",
        max_steps: int = 8,
        max_tool_calls_per_step: int = 8,
        interrupt_on: dict[str, bool] | None = None,
        approval_hook: ApprovalHook | None = None,
        tool_timeout: float | None = None,
        offloader: ResultOffloader | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须 >= 1")
        self.model = model
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
        self._tools_by_name = {tool.name: tool for tool in self.tools}
        #: 没有工具时不绑定，避免部分 provider 在空工具列表上报错。
        self._runnable = model.bind_tools(self.tools) if self.tools else model

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
            ``tool_call``* → ``tool_result``*)+ → ``run_end``（或 ``error``）。
        """
        history = list(messages)
        if self.system_prompt and not isinstance(history[0] if history else None, SystemMessage):
            history.insert(0, SystemMessage(content=self.system_prompt))

        usage: dict[str, int] = {}
        yield RunEvent.run_start(thread_id=thread_id)

        for step in range(1, self.max_steps + 1):
            yield RunEvent.step_start(step, thread_id=thread_id)

            turn = _ModelTurn()
            async for event in self._reason(history, step=step, thread_id=thread_id, turn=turn):
                yield event
            _merge_usage(usage, turn.usage)

            history.append(AIMessage(content=turn.text, tool_calls=list(turn.tool_calls)))
            self._mirror(history, messages_out, 1)

            if not turn.tool_calls:
                yield RunEvent.run_end(turn.text, step=step, usage=usage, thread_id=thread_id)
                return

            calls = turn.tool_calls[: self.max_tool_calls_per_step]
            if len(turn.tool_calls) > self.max_tool_calls_per_step:
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

            outcomes = await asyncio.gather(
                *(self._execute_tool(call, step=step, turn=turn) for call in calls)
            )

            for outcome in outcomes:
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
            self._mirror(history, messages_out, len(outcomes))

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

        async for event in self.astream(messages, thread_id=thread_id, messages_out=produced):
            events.append(event)
            if event.type == "run_end":
                text = event.text
                steps = event.step
                usage = event.usage
            elif event.type == "error":
                error = event.text
                steps = event.step

        history = list(messages)
        if self.system_prompt and not isinstance(history[0] if history else None, SystemMessage):
            history.insert(0, SystemMessage(content=self.system_prompt))
        history.extend(produced)

        return RunResult(
            text=text,
            steps=steps,
            messages=history,
            usage=usage,
            events=events,
            ok=error is None,
            error=error,
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
        """跑一轮模型，把文本与工具调用累积进 ``turn``。"""
        pending: dict[int, dict[str, Any]] = {}
        saw_chunks = False
        direct_calls: list[dict[str, Any]] = []

        # 模型调用是循环里最关键的一步：入参（上下文规模 + 最新用户输入）用 DEBUG 留痕，
        # 正常运行时不会打扰，排查「模型为什么不调工具」时把日志级别调到 DEBUG 即可。
        logger.debug(
            "agent.model_call",
            step=step,
            thread_id=thread_id,
            context_messages=len(history),
            tools=len(self._tools_by_name),
            last_user=_log_safe(_last_user_preview(history)),
        )

        async for chunk in self._runnable.astream(history):
            text = content_to_text(getattr(chunk, "content", None))
            if text:
                turn.text += text
                yield RunEvent.text_delta(text, step=step, thread_id=thread_id)

            for raw in getattr(chunk, "tool_call_chunks", None) or []:
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
                for call in getattr(chunk, "tool_calls", None) or []:
                    direct_calls.append(dict(call))

            _merge_usage(turn.usage, getattr(chunk, "usage_metadata", None) or {})

        turn.tool_calls, turn.arg_errors = _finalize_tool_calls(pending, direct_calls)
        if turn.tool_calls:
            logger.debug(
                "agent.tool_calls",
                step=step,
                thread_id=thread_id,
                tools=[call["name"] for call in turn.tool_calls],
            )

    # ---------------------------------------------------------------- 执行

    def _needs_approval(self, tool_name: str) -> bool:
        if self.interrupt_on.get(tool_name):
            return True
        return bool(self.interrupt_on.get("*"))

    async def _execute_tool(
        self, call: dict[str, Any], *, step: int, turn: _ModelTurn
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

        if self._needs_approval(name):
            request = ToolApprovalRequest(tool_name=name, args=args, step=step, call_id=call_id)
            if self.approval_hook is None:
                return ToolOutcome(
                    call_id=call_id,
                    name=name,
                    args=args,
                    ok=False,
                    content=(
                        f"错误：工具 {name} 被配置为需要人工确认，"
                        "但当前运行未提供确认回调，已跳过执行。"
                    ),
                )
            approved = await self.approval_hook(request)
            if not approved:
                logger.info("agent.tool_rejected", tool=name, step=step)
                return ToolOutcome(
                    call_id=call_id,
                    name=name,
                    args=args,
                    ok=False,
                    content=f"用户拒绝执行工具 {name}，请改用其他方式或询问用户。",
                )

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
            elapsed = perf_counter() - started
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
                content=f"错误：工具 {name} 执行超时（>{timeout}s）。",
            )
        except Exception as exc:
            elapsed = perf_counter() - started
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
                content=f"错误：工具 {name} 执行失败（{type(exc).__name__}: {exc}）。",
            )

        elapsed = perf_counter() - started
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
            return truncate(content_to_text(message.content), limit)
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
