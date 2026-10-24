"""Thread history and state management endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from doudou.api.deps import RunnerDep
from doudou.api.errors import ApiError
from doudou.api.schemas import (
    ContextReport,
    MessageOut,
    ThreadDetail,
    ThreadListResponse,
    ThreadSummary,
)
from doudou.gateway.history import ThreadInfo
from doudou.observability.logging import get_logger
from doudou.utils.text import content_to_text

logger = get_logger(__name__)

router = APIRouter(tags=["threads"])


def _summary(info: ThreadInfo) -> ThreadSummary:
    return ThreadSummary(
        thread_id=info.thread_id,
        channel=info.channel,
        conversation_id=info.conversation_id,
        message_count=info.message_count,
        created_at=info.created_at,
        updated_at=info.updated_at,
    )


@router.get("/threads", response_model=ThreadListResponse, summary="会话列表")
async def list_threads(
    runner: RunnerDep,
    limit: Annotated[int, Query(ge=1, le=200, description="最多返回多少条")] = 50,
) -> ThreadListResponse:
    """按最近更新倒序列出会话。"""
    infos = await runner.list_threads(limit=limit)
    return ThreadListResponse(threads=[_summary(info) for info in infos])


@router.get("/threads/{thread_id}", response_model=ThreadDetail, summary="会话详情")
async def get_thread(thread_id: str, runner: RunnerDep) -> ThreadDetail:
    """取会话元信息与完整历史。

    ``thread_id`` 不存在时返回 404 而不是空列表 —— 客户端据此区分
    「会话没有历史」与「会话不存在」。
    """
    info = await runner.get_thread(thread_id)
    if info is None:
        raise ApiError(404, "thread_not_found", f"会话 {thread_id} 不存在")

    messages = await runner.history(thread_id)
    return ThreadDetail(
        thread=_summary(info),
        messages=[
            MessageOut(
                role=message.type,
                content=content_to_text(message.content),
                tool_calls=getattr(message, "tool_calls", None) or None,
                tool_call_id=getattr(message, "tool_call_id", None),
                name=getattr(message, "name", None),
                status=getattr(message, "status", None),
            )
            for message in messages
        ],
    )


@router.get("/threads/{thread_id}/context", response_model=ContextReport, summary="上下文占用")
async def get_thread_context(thread_id: str, runner: RunnerDep) -> ContextReport:
    """查看某个会话的上下文占用、压缩情况与预算。

    排查「它为什么忘了刚才说的事」时先看这里：``last_run.compressed`` 为真说明
    这一轮用过摘要，``dropped`` 是被裁掉的消息数，而 ``stored_messages``
    始终是全部原始历史 —— **压缩只影响送给模型的窗口，不删历史**。
    """
    info = await runner.get_thread(thread_id)
    if info is None:
        raise ApiError(404, "thread_not_found", f"会话 {thread_id} 不存在")
    return ContextReport.model_validate(await runner.context_report(thread_id))


@router.delete("/threads/{thread_id}", summary="删除会话")
async def delete_thread(thread_id: str, runner: RunnerDep) -> dict[str, object]:
    """删除会话及其全部历史。"""
    deleted = await runner.clear_thread(thread_id)
    if not deleted:
        raise ApiError(404, "thread_not_found", f"会话 {thread_id} 不存在")
    return {"thread_id": thread_id, "deleted": True}


__all__ = ["router"]
