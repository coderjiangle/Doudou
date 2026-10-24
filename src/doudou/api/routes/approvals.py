"""Human-in-the-loop approval endpoints.

确认动作可能来自 CLI、HTTP 或飞书卡片。这三个接口是「HTTP 这一路」：

* ``GET  /approvals``         —— 看有没有待确认的（以及最近的审计记录）；
* ``POST /approvals/{id}``    —— 落一次决策（前端 / 运维用）。

**待确认的请求会阻塞对应的那条会话** —— 一个 worker 正 await 在那次工具调用上。
因此 ``agent.approval_timeout_seconds`` 到了就按拒绝处理，不会永远挂着。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from doudou.api.deps import StateDep
from doudou.api.errors import ApiError
from doudou.api.schemas.approvals import (
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalOut,
)
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["approvals"])


def _broker(state: StateDep):  # type: ignore[no-untyped-def]
    """取确认中介；未初始化时返回 503 而不是 500。"""
    if state.broker is None:
        raise ApiError(503, "approvals_unavailable", "确认中介未初始化")
    return state.broker


@router.get("/approvals", response_model=ApprovalListResponse, summary="待确认与最近决策")
async def list_approvals(state: StateDep) -> ApprovalListResponse:
    """列出待确认的请求与最近的决策记录。

    ``recent`` 来自数据库（若配了库），因此**进程重启后仍能查到**
    「当时问了什么、谁同意了什么」。但那次被中断的运行**不会自己续跑**。
    """
    broker = _broker(state)
    pending = [ApprovalOut.model_validate(item.as_dict()) for item in broker.pending]
    recent: list[dict[str, Any]] = []
    if state.approval_store is not None:
        recent = await state.approval_store.recent(limit=50)
    return ApprovalListResponse(
        pending=pending,
        recent=recent,
        timeout_seconds=broker.timeout,
        note=(
            "待确认的请求会阻塞对应的那条会话；超时未决策按拒绝处理。"
            "决策落库后可跨重启查询，但中断的运行不会自动续跑。"
        ),
    )


@router.post("/approvals/{approval_id}", response_model=ApprovalOut, summary="同意或拒绝一次确认")
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    state: StateDep,
) -> ApprovalOut:
    """落一次决策。重复调用只认第一次（幂等）—— 飞书卡片可能被点两次。"""
    broker = _broker(state)
    request = broker.get(approval_id)
    if request is None:
        raise ApiError(404, "approval_not_found", f"没有待确认的请求 {approval_id}")
    outcome = await broker.decide(approval_id, approved=payload.approved, reason=payload.reason)
    logger.info(
        "api.approval_decided",
        approval_id=approval_id,
        approved=outcome.approved,
        tool=request.tool_name,
    )
    return ApprovalOut.model_validate(request.as_dict())


__all__ = ["router"]
