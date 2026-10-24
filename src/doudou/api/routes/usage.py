"""Token and cost usage endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Query

from doudou.api.deps import StateDep
from doudou.api.schemas.observability import UsageResponse
from doudou.observability.cost import build_usage_report, today_key
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["observability"])


@router.get("/usage", response_model=UsageResponse, summary="token 与费用用量")
async def get_usage(
    state: StateDep,
    days: int = Query(default=7, ge=1, le=90, description="统计最近多少天。"),
) -> UsageResponse:
    """按天 / 按会话的 token 与费用。

    费用是**估算**，不是账单：它按 ``observability.cost.prices`` 计算。
    查不到价格的模型会让 ``cost`` 变成 ``null``（未知 ≠ 0）——
    编一个数字出来会让人以为成本在掌控之中。
    """
    usage = state.observability.usage
    store = state.observability.usage_store
    config = state.settings.app.observability.cost

    if usage is None or store is None:
        return UsageResponse(
            total={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0.0},
            today={
                "day": today_key(),
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost": 0.0,
            },
            budget={"daily": 0.0, "exceeded": False, "cost_known": True},
            note="成本统计未启用（observability.cost.enabled 为 false）。",
        )

    report = build_usage_report(
        records_by_day=await store.by_day(days=days),
        records_by_thread=await store.by_thread(days=days),
        total=await store.total(),
        today=await store.total(day=today_key()),
        model=usage.model,
        budget=config.daily_budget,
        unknown_models=usage.unknown_models,
        prices_known=usage.prices.known,
    )
    return UsageResponse.model_validate(report)


__all__ = ["router"]
