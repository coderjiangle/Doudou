"""Chat and streaming run endpoints."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from doudou.agent.events import RunEvent
from doudou.api.deps import RunnerDep
from doudou.api.schemas import (
    ChatRequest,
    ChatResponse,
    StreamDone,
    StreamError,
    StreamStart,
    StreamTextDelta,
    StreamToolCall,
    StreamToolResult,
)
from doudou.api.schemas.chat import MessageOutLite
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics
from doudou.utils.text import truncate

logger = get_logger(__name__)

router = APIRouter(tags=["chat"])

#: SSE 里工具结果的单条上限。工具可能返回几十万字，原样推给客户端会拖垮连接；
#: 超出时截断并置 ``truncated``，需要全文的场景应改用工具落盘 + 读文件。
MAX_STREAM_RESULT_CHARS = 8000


def _to_request(payload: ChatRequest) -> RunRequest:
    """把 HTTP 请求转成内部 ``RunRequest``。

    会话归属的三种写法：

    * 传 ``thread_id`` —— 直接用这个会话（客户端自己记账）；
    * 传 ``conversation_id`` —— 按渠道会话复用/分配，服务端持久绑定；
    * 都不传 —— 本次请求自成一个新会话，会话 id 在响应里返回，
      客户端下次带上它即可续聊。
    """
    if payload.thread_id or payload.conversation_id:
        conversation_id = payload.conversation_id or f"api-{payload.thread_id}"
        new_thread = payload.new_thread
    else:
        conversation_id = f"api-{uuid.uuid4().hex[:12]}"
        new_thread = True

    return RunRequest(
        text=payload.message,
        conversation_id=conversation_id,
        channel=payload.channel,
        thread_id=payload.thread_id,
        new_thread=new_thread,
    )


def map_event(event: RunEvent, thread_id: str) -> tuple[str, dict[str, Any]] | None:
    """把 ``RunEvent`` 翻译成一个 SSE 事件 ``(名称, 载荷)``。

    抽成纯函数是为了能脱离 SSE 连接直接做单测（见 tests/integration/test_api.py）。
    """
    if event.type == "text_delta":
        return "delta", StreamTextDelta(text=event.text).model_dump()
    if event.type == "tool_call":
        return "tool_call", StreamToolCall(
            name=event.tool_name or "",
            args=event.tool_args or {},
            call_id=event.tool_call_id or "",
            step=event.step,
        ).model_dump()
    if event.type == "tool_result":
        raw = event.result or ""
        truncated = len(raw) > MAX_STREAM_RESULT_CHARS
        return "tool_result", StreamToolResult(
            name=event.tool_name or "",
            call_id=event.tool_call_id or "",
            ok=event.tool_ok,
            result=truncate(raw, MAX_STREAM_RESULT_CHARS) if truncated else raw,
            truncated=truncated,
            elapsed=event.tool_elapsed,
        ).model_dump()
    if event.type == "run_end":
        return "done", StreamDone(
            thread_id=thread_id, text=event.text, steps=event.step, usage=event.usage
        ).model_dump()
    if event.type == "error":
        return "error", StreamError(thread_id=thread_id, message=event.text).model_dump()
    return None


def _pack(name: str, body: dict[str, Any]) -> dict[str, str]:
    return {"event": name, "data": json.dumps(body, ensure_ascii=False, default=str)}


@router.post("/chat", response_model=ChatResponse, summary="单轮对话（非流式）")
async def chat(payload: ChatRequest, runner: RunnerDep) -> ChatResponse:
    """跑一轮 ReAct 循环，一次性返回完整答复。"""
    request = _to_request(payload)
    # 只解析一次 thread：交给 ainvoke 自己解析会在「匿名新会话」时创建两个 thread。
    thread_id = await runner.resolve_thread(request)
    result = await runner.ainvoke(request, thread_id=thread_id)

    tool_calls = [
        MessageOutLite(
            name=event.tool_name or "",
            args=event.tool_args or {},
            ok=event.tool_ok,
            elapsed=event.tool_elapsed,
        )
        for event in result.events
        if event.type == "tool_result"
    ]

    metrics = get_metrics()
    metrics.incr("run.finished" if result.ok else "run.failed")
    metrics.incr("run.tool_calls", len(tool_calls))

    return ChatResponse(
        thread_id=thread_id,
        text=result.text,
        ok=result.ok,
        steps=result.steps,
        error=result.error,
        usage=result.usage,
        tool_calls=tool_calls,
    )


@router.post("/chat/stream", summary="单轮对话（SSE 流式）")
async def chat_stream(payload: ChatRequest, runner: RunnerDep) -> EventSourceResponse:
    """以 SSE 推送一轮对话。

    事件序列::

        start → (delta | tool_call | tool_result)* → done
                                              \\→ error（异常终止时）
    """
    request = _to_request(payload)
    # 先解析 thread，保证客户端在第一个事件里就拿到会话 id。
    thread_id = await runner.resolve_thread(request)

    async def event_source() -> AsyncIterator[dict[str, str]]:
        yield _pack("start", StreamStart(thread_id=thread_id, channel=request.channel).model_dump())

        metrics = get_metrics()
        tool_calls = 0
        try:
            async for event in runner.astream(request, thread_id=thread_id):
                if event.type == "tool_call":
                    tool_calls += 1
                mapped = map_event(event, thread_id)
                if mapped is not None:
                    yield _pack(*mapped)
        except Exception as exc:
            # 详细信息只进日志，回应给客户端的信息不含内部细节。
            logger.error("api.stream_failed", thread_id=thread_id, error=str(exc), exc_info=True)
            metrics.incr("run.failed")
            yield _pack(
                "error",
                StreamError(thread_id=thread_id, message="运行失败，请查看服务端日志").model_dump(),
            )
            return

        metrics.incr("run.finished")
        metrics.incr("run.tool_calls", tool_calls)

    return EventSourceResponse(event_source(), ping=15)


__all__ = ["MAX_STREAM_RESULT_CHARS", "map_event", "router"]
