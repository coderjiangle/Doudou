"""SQL-backed audit store.

落库版本对应 :class:`~doudou.observability.audit.AuditStore` 协议；
无数据库的部署用 ``MemoryAuditStore``。
"""

from __future__ import annotations

from doudou.observability.audit import AuditEntry, AuditStore
from doudou.observability.logging import get_logger
from doudou.storage.database import Database
from doudou.storage.models import AuditModel, as_utc
from doudou.storage.repositories import AuditRepository

logger = get_logger(__name__)


class SqlAuditStore(AuditStore):
    """把审计记录写进 ``audit_events``。"""

    #: 每写这么多条检查一次行数上限：每条都 count/delete 太浪费，
    #: 而「多留几百条」对审计没有影响。
    trim_every = 500

    def __init__(self, database: Database, *, max_rows: int = 50000) -> None:
        self.database = database
        self.max_rows = max(0, max_rows)
        self._writes = 0
        self._next_trim = self.trim_every

    async def append_many(self, entries: list[AuditEntry]) -> None:
        """一批写入只开一个会话、只提交一次。

        这是压测得出的关键优化：SQLite 同一时刻只允许一个写事务，
        「每次调用提交一次」会把吞吐压到个位数（见 docs/performance.md）。
        """
        if not entries:
            return
        async with self.database.session() as session:
            repository = AuditRepository(session)
            for entry in entries:
                await repository.upsert(
                    audit_id=entry.audit_id,
                    kind=entry.kind,
                    thread_id=entry.thread_id,
                    status=entry.status,
                    channel=entry.channel or None,
                    conversation_id=entry.conversation_id or None,
                    user_id=entry.user_id,
                    tool_name=entry.tool_name or None,
                    tool_call_id=entry.tool_call_id or None,
                    step=entry.step,
                    args=dict(entry.args),
                    args_truncated=entry.args_truncated,
                    result_preview=entry.result_preview or None,
                    result_chars=entry.result_chars,
                    approved=entry.approved,
                    elapsed=entry.elapsed,
                    error=entry.error,
                    created_at=entry.created_at,
                    updated_at=entry.updated_at,
                )
        self._writes += len(entries)
        if self.max_rows and self._writes >= self._next_trim:
            self._next_trim = self._writes + self.trim_every
            await self.trim()

    async def append(self, entry: AuditEntry) -> None:
        await self.append_many([entry])

    async def trim(self) -> int:
        async with self.database.session() as session:
            removed = await AuditRepository(session).trim(keep=self.max_rows)
        if removed:
            logger.info("audit.trimmed", removed=removed, keep=self.max_rows)
        return removed

    async def list_recent(
        self, *, limit: int = 100, thread_id: str | None = None
    ) -> list[AuditEntry]:
        async with self.database.session() as session:
            rows = await AuditRepository(session).list_recent(limit=limit, thread_id=thread_id)
        return [entry_to_model(row) for row in rows]

    async def get(self, audit_id: str) -> AuditEntry | None:
        async with self.database.session() as session:
            row = await AuditRepository(session).get(audit_id)
        return entry_to_model(row) if row is not None else None

    async def delete_for_thread(self, thread_id: str) -> int:
        async with self.database.session() as session:
            return await AuditRepository(session).delete_for_thread(thread_id)


def entry_to_model(row: AuditModel) -> AuditEntry:
    """ORM 行 → :class:`AuditEntry`（补上 UTC 时区）。"""
    return AuditEntry(
        audit_id=row.audit_id,
        kind=row.kind,  # type: ignore[arg-type]
        thread_id=row.thread_id,
        channel=row.channel or "",
        conversation_id=row.conversation_id or "",
        user_id=row.user_id,
        tool_name=row.tool_name or "",
        tool_call_id=row.tool_call_id or "",
        step=row.step,
        args=dict(row.args or {}),
        args_truncated=row.args_truncated,
        result_preview=row.result_preview or "",
        result_chars=row.result_chars,
        status=row.status,  # type: ignore[arg-type]
        approved=row.approved,
        elapsed=row.elapsed,
        error=row.error,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


__all__ = ["SqlAuditStore", "entry_to_model"]
