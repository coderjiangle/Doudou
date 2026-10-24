"""Persistence for approval requests and decisions.

存在的理由：确认是**有后果的事实** ——「谁在什么时候同意了对哪个文件做什么」。
进程重启后必须还能查得到。

要说清楚两件事的边界：

* **决策落库** ✅ 同一个 ``tool_call_id`` 不会问第二次；
* **待决请求落库** ✅ 重启后 ``/approvals`` 仍能看到当时问了什么；
* **中断的运行续跑** ❌ 做不到 —— 等待决策的协程随进程消失。
  重启后重试同一操作会重新问一次。这点必须讲清楚，
  否则会被理解成「中断的任务会自己接着跑」。
"""

from __future__ import annotations

from doudou.agent.approvals import ApprovalOutcome, ApprovalRequest
from doudou.observability.logging import get_logger
from doudou.storage.database import Database
from doudou.storage.models import ApprovalModel, as_utc, utcnow
from doudou.storage.repositories import ApprovalRepository

logger = get_logger(__name__)


class SqlApprovalStore:
    """把确认请求与决策落到数据库。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def record(self, request: ApprovalRequest) -> None:
        """写入或更新一条记录（决策时会再写一次，覆盖状态）。"""
        async with self.database.session() as session:
            await ApprovalRepository(session).upsert(
                approval_id=request.approval_id,
                call_id=request.call_id or None,
                thread_id=request.thread_id,
                tool_name=request.tool_name,
                args=dict(request.args),
                step=request.step,
                status=request.status,
                reason=request.reason,
                created_at=request.created_at,
                decided_at=request.decided_at,
                expires_at=request.expires_at,
            )

    async def lookup(self, call_id: str) -> ApprovalOutcome | None:
        """按 ``tool_call_id`` 找已有**决策**。

        ``pending`` 与 ``expired`` 都不算决策：前者是「还没问完」、
        后者是「问了但没人理」。把这两种当成答案会让重试静默跳过确认 ——
        那正是确认机制要防的事。
        """
        if not call_id:
            return None
        async with self.database.session() as session:
            row = await ApprovalRepository(session).get_by_call_id(call_id)
        if row is None or row.status not in {"approved", "rejected"}:
            return None
        outcome = ApprovalOutcome(
            approved=row.status == "approved",
            status=row.status,  # type: ignore[arg-type]
            reason=row.reason or "复用已落库的决策",
        )
        logger.debug("approval.lookup_hit", call_id=call_id, status=row.status)
        return outcome

    async def recent(self, *, limit: int = 50) -> list[dict[str, object]]:
        """最近的确认记录（给检视接口）。"""
        async with self.database.session() as session:
            rows = await ApprovalRepository(session).list_recent(limit=limit)
        return [_to_dict(row) for row in rows]


def _to_dict(row: ApprovalModel) -> dict[str, object]:
    return {
        "approval_id": row.approval_id,
        "call_id": row.call_id,
        "thread_id": row.thread_id,
        "tool_name": row.tool_name,
        "args": dict(row.args or {}),
        "step": row.step,
        "status": row.status,
        "reason": row.reason,
        "created_at": as_utc(row.created_at).isoformat(),
        "decided_at": as_utc(row.decided_at).isoformat() if row.decided_at else None,
        "expires_at": as_utc(row.expires_at).isoformat() if row.expires_at else None,
        "recorded_at": utcnow().isoformat(),
    }


__all__ = ["SqlApprovalStore"]
