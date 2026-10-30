"""SQL-backed run-trace store.

落库版本对应 :class:`~doudou.observability.traces.TraceStore` 协议；
无数据库的部署用 ``MemoryTraceStore``。
"""

from __future__ import annotations

from datetime import datetime

from doudou.observability.logging import get_logger
from doudou.observability.traces import TraceRecord, TraceStore
from doudou.storage.database import Database
from doudou.storage.models import RunTraceModel, as_utc
from doudou.storage.repositories import RunTraceRepository

logger = get_logger(__name__)


class SqlTraceStore(TraceStore):
    """把运行轨迹写进 ``run_traces``。"""

    #: 每写这么多条检查一次行数上限（同 ``SqlAuditStore``）。
    trim_every = 500

    def __init__(self, database: Database, *, max_rows: int = 20000) -> None:
        self.database = database
        self.max_rows = max(0, max_rows)
        self._writes = 0
        self._next_trim = self.trim_every

    async def append_many(self, records: list[TraceRecord]) -> None:
        """一批写入只开一个会话、只提交一次（绕开 SQLite 单写者瓶颈）。"""
        if not records:
            return
        async with self.database.session() as session:
            repository = RunTraceRepository(session)
            for record in records:
                await repository.add(record)
        self._writes += len(records)
        if self.max_rows and self._writes >= self._next_trim:
            self._next_trim = self._writes + self.trim_every
            await self.trim()

    async def append(self, record: TraceRecord) -> None:
        await self.append_many([record])

    async def trim(self) -> int:
        async with self.database.session() as session:
            removed = await RunTraceRepository(session).trim(keep=self.max_rows)
        if removed:
            logger.info("trace.trimmed", removed=removed, keep=self.max_rows)
        return removed

    async def list_recent(
        self,
        *,
        limit: int = 50,
        thread_id: str | None = None,
        channel: str | None = None,
        since: datetime | None = None,
        only_failed: bool = False,
    ) -> list[TraceRecord]:
        async with self.database.session() as session:
            rows = await RunTraceRepository(session).list_recent(
                limit=limit,
                thread_id=thread_id,
                channel=channel,
                since=since,
                only_failed=only_failed,
            )
            # 列表查询用了 load_only（不含 steps_detail），所以这里不能碰它。
            return [_to_summary(row) for row in rows]

    async def get(self, run_id: str) -> TraceRecord | None:
        async with self.database.session() as session:
            row = await RunTraceRepository(session).get(run_id)
            return _to_record(row) if row is not None else None


def _to_summary(row: RunTraceModel) -> TraceRecord:
    """列表 / 统计行 → 记录（不含 steps_detail；它没被加载）。"""
    return TraceRecord(
        run_id=row.run_id,
        thread_id=row.thread_id,
        channel=row.channel or "",
        conversation_id=row.conversation_id or "",
        user_id=row.user_id,
        model=row.model or "",
        ok=row.ok,
        error=row.error,
        steps=row.steps,
        tool_calls=row.tool_calls,
        approvals=row.approvals,
        rejections=row.rejections,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        elapsed=row.elapsed,
        started_at=as_utc(row.started_at),
    )


def _to_record(row: RunTraceModel) -> TraceRecord:
    """ORM 行 → :class:`TraceRecord`（补上 UTC 时区；含步骤明细）。"""
    return TraceRecord(
        run_id=row.run_id,
        thread_id=row.thread_id,
        channel=row.channel or "",
        conversation_id=row.conversation_id or "",
        user_id=row.user_id,
        model=row.model or "",
        ok=row.ok,
        error=row.error,
        steps=row.steps,
        tool_calls=row.tool_calls,
        approvals=row.approvals,
        rejections=row.rejections,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        elapsed=row.elapsed,
        input_preview=row.input_preview,
        output_preview=row.output_preview,
        steps_detail=[dict(item) for item in (row.steps_detail or [])],
        started_at=as_utc(row.started_at),
        created_at=as_utc(row.created_at),
    )


__all__ = ["SqlTraceStore"]
