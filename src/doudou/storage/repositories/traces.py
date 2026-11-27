"""Run-trace persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from doudou.observability.logging import get_logger
from doudou.observability.traces import TraceRecord
from doudou.storage.database import affected_rows
from doudou.storage.models import RunTraceModel

logger = get_logger(__name__)

#: 统计只需要这些标量列，别把 ``steps_detail``（可能很大）也读出来。
_STAT_COLUMNS = (
    RunTraceModel.run_id,
    RunTraceModel.thread_id,
    RunTraceModel.channel,
    RunTraceModel.conversation_id,
    RunTraceModel.user_id,
    RunTraceModel.model,
    RunTraceModel.ok,
    RunTraceModel.error,
    RunTraceModel.steps,
    RunTraceModel.tool_calls,
    RunTraceModel.approvals,
    RunTraceModel.rejections,
    RunTraceModel.input_tokens,
    RunTraceModel.output_tokens,
    RunTraceModel.elapsed,
    RunTraceModel.started_at,
)


class RunTraceRepository:
    """``run_traces`` 表的读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, record: TraceRecord) -> RunTraceModel:
        """写入一次运行的轨迹。``run_id`` 唯一，重复写入视为覆盖（幂等）。"""
        row = await self.session.get(RunTraceModel, record.run_id)
        if row is None:
            row = RunTraceModel(run_id=record.run_id)
            self.session.add(row)
        row.thread_id = record.thread_id
        row.channel = record.channel or None
        row.conversation_id = record.conversation_id or None
        row.user_id = record.user_id
        row.model = record.model or None
        row.ok = record.ok
        row.error = record.error
        row.steps = record.steps
        row.tool_calls = record.tool_calls
        row.approvals = record.approvals
        row.rejections = record.rejections
        row.input_tokens = record.input_tokens
        row.output_tokens = record.output_tokens
        row.elapsed = record.elapsed
        row.input_preview = record.input_preview
        row.output_preview = record.output_preview
        row.steps_detail = list(record.steps_detail)
        row.started_at = record.started_at
        row.created_at = record.created_at
        await self.session.flush()
        return row

    async def get(self, run_id: str) -> RunTraceModel | None:
        return await self.session.get(RunTraceModel, run_id)

    @staticmethod
    def _filtered(
        statement: Any,
        *,
        thread_id: str | None,
        channel: str | None,
        since: datetime | None,
        only_failed: bool,
    ) -> Any:
        """列表与计数共用同一套筛选，保证「第几页有几条」与总数一致。"""
        if thread_id:
            statement = statement.where(RunTraceModel.thread_id == thread_id)
        if channel:
            statement = statement.where(RunTraceModel.channel == channel)
        if since is not None:
            statement = statement.where(RunTraceModel.started_at >= since)
        if only_failed:
            statement = statement.where(RunTraceModel.ok.is_(False))
        return statement

    async def list_recent(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        thread_id: str | None = None,
        channel: str | None = None,
        since: datetime | None = None,
        only_failed: bool = False,
        include_timing: bool = False,
    ) -> list[RunTraceModel]:
        """列表 / 统计用：默认只加载标量列。

        ``steps_detail`` 可能很大，常规列表用不上它 —— 只有 ``get``（单次
        回放）或显式 ``include_timing=True``（提取 LLM/工具耗时拆分）才读取。

        按 ``(started_at DESC, run_id DESC)`` 排序，用 ``offset``/``limit`` 翻页。
        """
        columns = (*_STAT_COLUMNS, RunTraceModel.steps_detail) if include_timing else _STAT_COLUMNS
        statement = self._filtered(
            select(RunTraceModel).options(load_only(*columns)),
            thread_id=thread_id,
            channel=channel,
            since=since,
            only_failed=only_failed,
        )
        statement = (
            statement.order_by(RunTraceModel.started_at.desc(), RunTraceModel.run_id.desc())
            .offset(max(0, offset))
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def count(
        self,
        *,
        thread_id: str | None = None,
        channel: str | None = None,
        since: datetime | None = None,
        only_failed: bool = False,
    ) -> int:
        """符合条件的总条数（给「第 N / 共 M 页」用）。"""
        statement = self._filtered(
            select(func.count(RunTraceModel.run_id)),
            thread_id=thread_id,
            channel=channel,
            since=since,
            only_failed=only_failed,
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def trim(self, *, keep: int) -> int:
        """只保留最近 ``keep`` 条，返回删除条数。

        轨迹是**每次运行**都写、且带正文的东西，不设上限就是一条缓慢的磁盘泄漏。
        """
        if keep <= 0:
            return 0
        subquery = (
            select(RunTraceModel.run_id)
            .order_by(RunTraceModel.started_at.desc(), RunTraceModel.run_id)
            .limit(keep)
        )
        result = await self.session.execute(
            delete(RunTraceModel).where(RunTraceModel.run_id.not_in(subquery))
        )
        return affected_rows(result)

    async def delete_all(self) -> int:
        result = await self.session.execute(delete(RunTraceModel))
        return affected_rows(result)

    async def delete_for_thread(self, thread_id: str) -> int:
        """删除某条会话的全部轨迹（删会话时调用）。"""
        result = await self.session.execute(
            delete(RunTraceModel).where(RunTraceModel.thread_id == thread_id)
        )
        return affected_rows(result)


__all__ = ["RunTraceRepository"]
