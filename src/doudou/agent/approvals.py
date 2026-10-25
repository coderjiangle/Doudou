"""人工确认中介：待决登记、决策落库、超时按拒绝。

确认动作可能来自 CLI（就地提问）、HTTP（写一条决策记录）或飞书（卡片按钮）。
它们的共同点是「问出去之后要等一个外部事件」，所以把「等待」与「谁来答」分开：
Agent 只依赖 :class:`ApprovalBroker`，各通道负责把决策送进来。

边界说明：决策（approved / rejected）落库，同一个 ``tool_call_id`` 不会问第二次；
待决请求也落库，但进程重启后那次运行已经结束，不会自动续跑 —— 重启后重试同一操作
会重新问一次。等不到决策就按**拒绝**处理：悬着的确认等于没有确认。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics

logger = get_logger(__name__)

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]

#: 默认等待时长：太长会让人以为卡住，太短来不及看清内容。
DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """一次确认的结果。"""

    approved: bool
    status: ApprovalStatus
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"approved": self.approved, "status": self.status, "reason": self.reason}


@dataclass(slots=True)
class ApprovalRequest:
    """一条待确认（或已确认）的请求。"""

    approval_id: str
    thread_id: str | None
    tool_name: str
    args: dict[str, Any]
    step: int = 0
    call_id: str = ""
    status: ApprovalStatus = "pending"
    reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    #: 内部用：等待方在这里醒来。
    event: asyncio.Event | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "thread_id": self.thread_id,
            "tool_name": self.tool_name,
            "args": dict(self.args),
            "step": self.step,
            "call_id": self.call_id,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


#: 决策持久化：由 storage 层注入，避免 agent 直接认识 SQLAlchemy。
DecisionRecorder = Callable[[ApprovalRequest], Awaitable[None]]
#: 决策查询：按 tool_call_id 找已有决策（跨运行复用）。
DecisionLookup = Callable[[str], Awaitable[ApprovalOutcome | None]]


class ApprovalBroker:
    """登记待决请求、等待决策、处理超时。"""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        on_request: Callable[[ApprovalRequest], Awaitable[None]] | None = None,
        recorder: DecisionRecorder | None = None,
        lookup: DecisionLookup | None = None,
    ) -> None:
        self.timeout = max(1.0, timeout)
        #: 通道用它把「有人要确认」这件事告诉用户（CLI 直接问、飞书发卡片…）。
        self.on_request = on_request
        self._recorder = recorder
        self._lookup = lookup
        self._pending: dict[str, ApprovalRequest] = {}
        self._decisions: dict[str, ApprovalOutcome] = {}
        self._counter = 0

    # ---------------------------------------------------------------- 查询

    @property
    def pending(self) -> list[ApprovalRequest]:
        return [item for item in self._pending.values() if item.status == "pending"]

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._pending.get(approval_id)

    def recent(self, *, limit: int = 50) -> list[ApprovalRequest]:
        items = sorted(self._pending.values(), key=lambda item: item.created_at, reverse=True)
        return items[:limit]

    # ---------------------------------------------------------------- 等待

    async def request(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        call_id: str = "",
        thread_id: str | None = None,
        step: int = 0,
    ) -> ApprovalOutcome:
        """登记一条待决请求并等待决策。

        Returns:
            :class:`ApprovalOutcome`；超时返回 ``rejected``（理由写明超时）。
        """
        # 同一个 emit 的 tool_call_id 已经决策过 → 直接复用，不再打扰用户。
        if call_id and call_id in self._decisions:
            logger.debug("approval.reused", tool=tool_name, call_id=call_id)
            return self._decisions[call_id]
        if call_id and self._lookup is not None:
            stored = await self._lookup(call_id)
            if stored is not None:
                self._decisions[call_id] = stored
                return stored

        self._counter += 1
        request = ApprovalRequest(
            approval_id=f"ap-{self._counter:04d}-{call_id[-8:] or 'anon'}",
            thread_id=thread_id,
            tool_name=tool_name,
            args=dict(args),
            step=step,
            call_id=call_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.timeout),
            event=asyncio.Event(),
        )
        self._pending[request.approval_id] = request
        get_metrics().incr("approval.requested")
        logger.info(
            "approval.requested",
            approval_id=request.approval_id,
            tool=tool_name,
            thread_id=thread_id,
            timeout=self.timeout,
        )

        if self.on_request is not None:
            try:
                await self.on_request(request)
            except Exception as exc:
                logger.warning("approval.notify_failed", error=str(exc), exc_info=True)
        else:
            # 没有任何人负责「把这件事告诉用户」时必须自己喊一声 ——
            # 否则这次运行只会静静等到超时，用户以为卡住了、运维也不知道它在等什么。
            logger.warning(
                "approval.awaiting_decision",
                approval_id=request.approval_id,
                tool=request.tool_name,
                timeout=self.timeout,
                hint=(
                    "无人可问：GET /api/v1/approvals 查看待确认请求，"
                    f"POST /api/v1/approvals/{request.approval_id} 落决策"
                    "（或让通道接管 broker.on_request，例如飞书卡片按钮）。"
                    f"超过 {self.timeout:.0f}s 未决将按拒绝处理。"
                ),
            )

        return await self._wait(request)

    async def _wait(self, request: ApprovalRequest) -> ApprovalOutcome:
        assert request.event is not None
        try:
            await asyncio.wait_for(request.event.wait(), timeout=self.timeout)
        except TimeoutError:
            return await self._expire(request)
        outcome = self._decisions.get(request.call_id)
        if outcome is None:
            # 决策是按 approval_id 落的；这里从状态反推。
            approved = request.status == "approved"
            outcome = ApprovalOutcome(
                approved=approved,
                status="approved" if approved else "rejected",
                reason=request.reason,
            )
        return outcome

    async def _expire(self, request: ApprovalRequest) -> ApprovalOutcome:
        """等不到决策 → 按拒绝处理。

        悬着的确认等于没有确认：默认必须偏向安全的一侧。
        """
        request.status = "expired"
        request.reason = f"等待确认超过 {self.timeout:.0f}s，已按拒绝处理"
        request.decided_at = datetime.now(UTC)
        get_metrics().incr("approval.expired")
        logger.warning("approval.expired", approval_id=request.approval_id, tool=request.tool_name)
        outcome = ApprovalOutcome(approved=False, status="expired", reason=request.reason)
        await self._record(request, outcome)
        return outcome

    # ---------------------------------------------------------------- 决策

    async def decide(
        self, approval_id: str, *, approved: bool, reason: str = ""
    ) -> ApprovalOutcome:
        """落一次决策（由 CLI / HTTP / 飞书按钮调用）。"""
        request = self._pending.get(approval_id)
        if request is None:
            raise KeyError(f"没有待决请求 {approval_id}")
        if request.status != "pending":
            # 幂等：重复点击只认第一次。
            return ApprovalOutcome(
                approved=request.status == "approved", status=request.status, reason=request.reason
            )

        request.status = "approved" if approved else "rejected"
        request.reason = reason or ("用户同意" if approved else "用户拒绝")
        request.decided_at = datetime.now(UTC)
        outcome = ApprovalOutcome(approved=approved, status=request.status, reason=request.reason)
        self._decisions[request.call_id or approval_id] = outcome
        get_metrics().incr("approval.approved" if approved else "approval.rejected")
        logger.info(
            "approval.decided",
            approval_id=approval_id,
            tool=request.tool_name,
            approved=approved,
        )
        await self._record(request, outcome)
        if request.event is not None:
            request.event.set()
        return outcome

    async def _record(self, request: ApprovalRequest, outcome: ApprovalOutcome) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder(request)
        except Exception as exc:
            logger.warning("approval.record_failed", error=str(exc), exc_info=True)
        del outcome

    # ---------------------------------------------------------------- 生成回调

    def hook(self) -> Callable[[Any], Awaitable[bool]]:
        """生成可以直接给 ``ReactAgent`` 用的确认回调。"""

        async def _approve(request: Any) -> bool:
            outcome = await self.request(
                tool_name=str(getattr(request, "tool_name", "")),
                args=dict(getattr(request, "args", {}) or {}),
                call_id=str(getattr(request, "call_id", "")),
                step=int(getattr(request, "step", 0) or 0),
            )
            return outcome.approved

        return _approve

    def describe(self) -> dict[str, Any]:
        return {
            "pending": len(self.pending),
            "recent": [item.as_dict() for item in self.recent(limit=20)],
            "timeout_seconds": self.timeout,
        }


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ApprovalBroker",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalStatus",
    "DecisionLookup",
    "DecisionRecorder",
]
