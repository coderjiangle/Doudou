"""模型 fallback 的执行顺序、工具绑定与流式中断边界（离线）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool

from doudou.agent.context import HistorySummarizer
from doudou.agent.factory import build_agent
from doudou.agent.models import build_model
from doudou.agent.react import ReactAgent
from doudou.agent.testing import ScriptedChatModel, summarize_last_tool_result, tool_call
from doudou.config.loader import Settings
from doudou.config.schema import ModelConfig
from doudou.tools.decorators import annotate


def unavailable(_messages: list[BaseMessage]) -> AIMessage:
    raise RuntimeError("upstream unavailable")


async def test_fallback_keeps_tools_and_complete_history() -> None:
    attempts: list[str] = []

    def first(_messages: list[BaseMessage]) -> AIMessage:
        attempts.append("primary")
        raise RuntimeError("primary unavailable")

    def second(_messages: list[BaseMessage]) -> AIMessage:
        attempts.append("secondary")
        raise RuntimeError("secondary unavailable")

    @tool
    async def echo(value: str) -> str:
        """回传输入。"""
        return value

    annotate(echo, group="test", side_effects=False)
    agent = ReactAgent(
        model=ScriptedChatModel(steps=[first, first]),
        fallback_models=[
            ScriptedChatModel(steps=[second, second]),
            ScriptedChatModel(
                steps=[tool_call("echo", {"value": "ok"}), summarize_last_tool_result()]
            ),
        ],
        tools=[echo],
    )

    result = await agent.ainvoke([HumanMessage(content="echo")])

    assert result.ok and result.text == "工具返回：ok"
    assert attempts == ["primary", "secondary", "primary", "secondary"]
    assert len([message for message in result.messages if isinstance(message, ToolMessage)]) == 1
    assert [event.tool_name for event in result.events if event.type == "tool_call"] == ["echo"]


async def test_successful_primary_does_not_call_fallback() -> None:
    agent = ReactAgent(
        model=ScriptedChatModel(steps=[AIMessage(content="primary")]),
        fallback_models=[ScriptedChatModel(steps=[unavailable])],
    )
    assert (await agent.ainvoke([HumanMessage(content="hello")])).text == "primary"


class InterruptedStream(ScriptedChatModel):
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        yield ChatGenerationChunk(message=AIMessageChunk(content="partial answer"))
        raise RuntimeError("stream interrupted")


async def test_partial_text_is_not_replayed_with_another_model() -> None:
    agent = ReactAgent(
        model=InterruptedStream(),
        fallback_models=[ScriptedChatModel(steps=[AIMessage(content="backup")])],
    )
    text: list[str] = []
    with pytest.raises(RuntimeError, match="stream interrupted"):
        async for event in agent.astream([HumanMessage(content="hello")]):
            if event.type == "text_delta":
                text.append(event.text)
    assert text == ["partial answer"]


async def test_all_models_failing_preserves_the_last_error() -> None:
    agent = ReactAgent(
        model=ScriptedChatModel(steps=[unavailable]),
        fallback_models=[ScriptedChatModel(steps=[unavailable])],
    )
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        await agent.ainvoke([HumanMessage(content="hello")])


async def test_cancellation_does_not_trigger_fallback() -> None:
    def cancel(_messages: list[BaseMessage]) -> AIMessage:
        raise asyncio.CancelledError

    agent = ReactAgent(
        model=ScriptedChatModel(steps=[cancel]),
        fallback_models=[ScriptedChatModel(steps=[AIMessage(content="backup")])],
    )
    with pytest.raises(asyncio.CancelledError):
        await agent.ainvoke([HumanMessage(content="hello")])


async def test_summarizer_uses_configured_fallback() -> None:
    summarizer = HistorySummarizer(
        ScriptedChatModel(steps=[unavailable]),
        fallback_models=[ScriptedChatModel(steps=[AIMessage(content="summary")])],
    )
    assert await summarizer([HumanMessage(content="history")]) == "summary"


def test_factory_loads_fallbacks_and_skips_invalid_optional_models(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        config_yaml=(
            "model:\n  primary: scripted:demo\n  fallbacks: [scripted:missing, scripted:demo]\n"
        ),
        use_env=False,
    )
    bundle = build_agent(settings, tools=[])
    assert len(bundle.agent.fallback_models) == 1
    assert bundle.context is not None
    assert isinstance(bundle.context.summarizer, HistorySummarizer)
    assert bundle.context.summarizer.fallback_models == bundle.agent.fallback_models


def test_injected_model_does_not_construct_configured_network_models(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        config_yaml="model:\n  fallbacks: [openai:requires-credentials]\n", use_env=False
    )
    bundle = build_agent(settings, model=ScriptedChatModel(), tools=[])
    assert bundle.agent.fallback_models == []


async def test_openai_sdk_error_switches_to_backup_without_real_network() -> None:
    """走真实 provider 适配与 SSE 解析，只在 HTTP 传输层离线返回协议响应。"""
    requested: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requested.append(body["model"])
        if body["model"] == "primary":
            return httpx.Response(
                503, json={"error": {"message": "unavailable", "type": "server_error"}}
            )
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "backup",
            "choices": [{"index": 0, "delta": {"content": "backup answer"}, "finish_reason": None}],
        }
        content = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})

    config = ModelConfig(
        primary="openai:primary",
        api_key="test-placeholder",
        base_url="https://model.example/v1",
        max_retries=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        extra = {"http_async_client": client}
        agent = ReactAgent(
            model=build_model(config, extra=extra),
            fallback_models=[build_model(config, spec="openai:backup", extra=extra)],
        )
        result = await agent.ainvoke([HumanMessage(content="hello")])
    assert result.ok and result.text == "backup answer"
    assert requested == ["primary", "backup"]
