"""Scheduled-task inspection endpoint.

定时任务由 Agent 在聊天里管理（``schedule_*`` 工具）；这里只提供一个**只读**视图，
方便在网页/运维里一眼看到有哪些任务、下次什么时候跑、上次结果如何。
没有写接口 —— 改任务请找 Agent，避免两套真相源。
"""

from __future__ import annotations

from fastapi import APIRouter

from doudou.api.deps import StateDep
from doudou.api.schemas.scheduler import ScheduledListResponse, ScheduledTaskOut
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["scheduler"])


@router.get("/scheduled", response_model=ScheduledListResponse, summary="定时任务列表（只读）")
async def list_scheduled(state: StateDep) -> ScheduledListResponse:
    store = state.scheduler_store
    if store is None:
        return ScheduledListResponse(
            count=0,
            backend="disabled",
            tasks=[],
            note="定时任务未启用（scheduler.enabled 为 false）。",
        )
    tasks = await store.list_all()
    return ScheduledListResponse(
        count=len(tasks),
        backend="database" if state.database is not None else "memory",
        tasks=[ScheduledTaskOut.model_validate(task.as_dict()) for task in tasks],
        note=(
            "任务不绑定会话：触发时按 channel/conversation_id 推送；"
            "conversation_id 为 null 表示触发时解析飞书私聊（解析不到就跳过本次）。"
        ),
    )


__all__ = ["router"]
