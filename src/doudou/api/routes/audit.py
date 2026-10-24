"""Audit query endpoint.

审计是**可读**的运维面：出了问题（「它为什么写了这个文件」）时，
这里应该能一次看全「谁、何时、调了什么、参数摘要、结果、有没有经过人」。

只读，没有写接口 —— 审计记录只能由运行产生，
提供「改审计」的接口等于让审计失去意义。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from doudou.api.deps import StateDep
from doudou.api.schemas.observability import AuditEntryOut, AuditListResponse
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["observability"])


@router.get("/audit", response_model=AuditListResponse, summary="审计记录（工具调用与运行）")
async def list_audit(
    state: StateDep,
    thread_id: str | None = Query(default=None, description="只看某条会话。"),
    limit: int = Query(default=50, ge=1, le=500),
) -> AuditListResponse:
    store = state.observability.audit_store
    if store is None:
        return AuditListResponse(
            count=0,
            backend="disabled",
            entries=[],
            note="审计未启用（observability.audit.enabled 为 false）。",
        )
    entries = await store.list_recent(limit=limit, thread_id=thread_id)
    return AuditListResponse(
        count=len(entries),
        backend=state.observability.audit_backend or "memory",
        entries=[AuditEntryOut.model_validate(item.as_dict()) for item in entries],
        note=(
            "参数是打码 + 截断后的摘要（args_truncated 说明截了多少字）；"
            "结果只留开头一段，全文在会话历史与日志里。"
            "「为什么被批准/拒绝」的细节在 /approvals。"
        ),
    )


__all__ = ["router"]
