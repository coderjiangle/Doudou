"""Repository package.

每个仓库类接收一个 :class:`sqlalchemy.ext.asyncio.AsyncSession`，
只负责单表读写与必要的聚合，不持有会话生命周期。
"""

from __future__ import annotations

from doudou.storage.repositories.approvals import ApprovalRepository
from doudou.storage.repositories.audit import AuditRepository
from doudou.storage.repositories.channels import SessionBindingRepository
from doudou.storage.repositories.messages import MessageRepository
from doudou.storage.repositories.threads import ThreadRepository
from doudou.storage.repositories.traces import RunTraceRepository
from doudou.storage.repositories.usage import UsageRepository

__all__ = [
    "ApprovalRepository",
    "AuditRepository",
    "MessageRepository",
    "RunTraceRepository",
    "SessionBindingRepository",
    "ThreadRepository",
    "UsageRepository",
]
