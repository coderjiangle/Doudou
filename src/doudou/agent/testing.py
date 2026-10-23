"""Deterministic offline models: for unit tests, CI and `--model scripted:demo`.

这个模块不参与生产链路，存在的意义是：**在没有 API Key 的环境里也能端到端验证
ReAct 循环**（推理 → 调用工具 → 观察 → 再推理）。CI 与本地自检都依赖它。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any, cast

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from pydantic import Field

#: 一个 step 要么是现成的 ``AIMessage``，要么是「根据当前上下文现算」的函数。
Step = AIMessage | Any  # Any: 允许传入 Callable[[list[BaseMessage]], AIMessage]


class ScriptedChatModel(BaseChatModel):
    """按预设脚本作答的确定性模型。

    脚本的选取方式是**无状态**的：模型统计上下文里已经出现过几条 ``AIMessage``，
    据此决定这次该返回第几个 step。因此同一个实例可以被多个会话安全复用，
    也不会因为重试而产生漂移。

    Args:
        steps: 依次返回的消息。元素可以是 ``AIMessage``，也可以是
            ``Callable[[list[BaseMessage]], AIMessage]``（用于根据工具返回结果动态生成）。
        fallback_text: 脚本用尽后返回的兜底文本。
    """

    steps: list[Any] = Field(default_factory=list)
    fallback_text: str = "（脚本已用尽）"
    #: 记录 ``bind_tools`` 收到的工具，方便断言绑定行为。
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    # ---------------------------------------------------------------- tools

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ScriptedChatModel:
        """记录被绑定的工具名。

        脚本模型不会真的按 schema 生成调用，但绑定信息在断言与调试时有用。
        """
        names: list[str] = []
        for item in tools:
            if isinstance(item, BaseTool):
                names.append(item.name)
            elif isinstance(item, dict):
                function = item.get("function")
                if isinstance(function, dict) and function.get("name"):
                    names.append(str(function["name"]))
            elif isinstance(item, type):
                names.append(getattr(item, "__name__", str(item)))
        return self.model_copy(update={"bound_tool_names": names})

    # ---------------------------------------------------------------- 脚本推进

    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        turns = sum(1 for message in messages if isinstance(message, AIMessage))
        if turns < len(self.steps):
            step = self.steps[turns]
            if callable(step):
                return cast("AIMessage", step(messages))
            if isinstance(step, AIMessage):
                return step
            raise TypeError(f"scripted step 必须是 AIMessage 或可调用对象，收到 {type(step)!r}")
        return AIMessage(content=self.fallback_text)

    # ---------------------------------------------------------------- 生成

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self._next_message(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self._next_message(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _to_chunk(self, message: AIMessage) -> AIMessageChunk:
        """把完整的 ``AIMessage`` 转成单个 ``AIMessageChunk``。

        工具调用以 ``tool_call_chunks`` 形式携带，正好覆盖真实模型流式返回的形态，
        让上层循环的累积逻辑得到同等验证。
        """
        chunks = [
            {
                "name": call["name"],
                "args": json.dumps(call.get("args") or {}, ensure_ascii=False),
                "id": call.get("id") or f"call_{index}",
                "index": index,
                "type": "tool_call_chunk",
            }
            for index, call in enumerate(message.tool_calls)
        ]
        return AIMessageChunk(
            content=message.content,
            tool_call_chunks=chunks,
            response_metadata=message.response_metadata,
            id=message.id,
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        yield ChatGenerationChunk(message=self._to_chunk(self._next_message(messages)))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        yield ChatGenerationChunk(message=self._to_chunk(self._next_message(messages)))


def tool_call(
    name: str, args: dict[str, Any] | None = None, *, call_id: str = "call_1"
) -> AIMessage:
    """构造一条「要求调用工具」的模型消息。"""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}],
    )


def summarize_last_tool_result(prefix: str = "工具返回：") -> Any:
    """生成一个「把最近一次工具结果复述出来」的 step。

    用于离线脚本的第二轮，验证 ToolMessage 确实被塞回了上下文。
    """

    def _build(messages: list[BaseMessage]) -> AIMessage:
        for message in reversed(messages):
            if isinstance(message, ToolMessage):
                return AIMessage(content=f"{prefix}{message.content}")
        return AIMessage(content=f"{prefix}（没有找到工具结果）")

    return _build


def demo_model() -> ScriptedChatModel:
    """``scripted:demo``：离线演示模型。

    第一轮根据用户最后一条消息决定「调用 ``now`` 工具」还是「直接作答」，
    第二轮把工具返回结果复述出来。整个过程不访问网络。
    """
    time_keywords = ("几点", "时间", "日期", "今天", "现在", "time", "date", "clock")

    def _first(messages: list[BaseMessage]) -> AIMessage:
        text = _last_human_text(messages)
        if any(keyword in text.lower() for keyword in time_keywords):
            return tool_call("now", {}, call_id="call_demo_now")
        return AIMessage(content=f"[offline] 收到你的消息：{text}")

    return ScriptedChatModel(steps=[_first, summarize_last_tool_result("工具返回结果：")])


def _last_human_text(messages: list[BaseMessage]) -> str:
    from langchain_core.messages import HumanMessage

    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            if isinstance(content, str):
                return content
            return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


__all__ = [
    "ScriptedChatModel",
    "demo_model",
    "summarize_last_tool_result",
    "tool_call",
]
