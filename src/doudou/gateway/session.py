"""Session and thread identity: mapping, locking, idle expiry.

M1 为进程内实现：单进程部署够用。M9 多副本时需要把映射下沉到 ``storage``
并换成分布式锁（见 docs/roadmap.md）。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from time import monotonic

from doudou.observability.logging import get_logger

logger = get_logger(__name__)

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_CONVERSATION_CHARS = 48


@dataclass(slots=True)
class SessionRecord:
    """一个会话的映射记录。"""

    conversation_id: str
    channel: str
    thread_id: str
    created_at: float


class SessionStore:
    """把「通道会话标识」映射到「agent thread_id」。

    thread_id 采用 ``<channel>-<conversation>-<随机后缀>`` 的可读形式：
    既能一眼看出归属，又保证 ``/new`` 后的新会话不会与旧会话共用历史。
    """

    def __init__(self, *, idle_timeout: float | None = None) -> None:
        self._records: dict[tuple[str, str], SessionRecord] = {}
        self._idle_timeout = idle_timeout

    # ---------------------------------------------------------------- 查询

    def get(self, conversation_id: str, *, channel: str = "cli") -> str | None:
        self._expire()
        record = self._records.get((channel, conversation_id))
        return record.thread_id if record else None

    def get_or_create(self, conversation_id: str, *, channel: str = "cli") -> str:
        """取已有 thread_id；不存在（或已过期）时创建。"""
        existing = self.get(conversation_id, channel=channel)
        if existing is not None:
            return existing
        return self.create(conversation_id, channel=channel)

    def create(self, conversation_id: str, *, channel: str = "cli") -> str:
        """强制创建新 thread（``/new``）。"""
        thread_id = self._make_thread_id(conversation_id, channel)
        self._records[(channel, conversation_id)] = SessionRecord(
            conversation_id=conversation_id,
            channel=channel,
            thread_id=thread_id,
            created_at=monotonic(),
        )
        logger.debug(
            "session.created", channel=channel, conversation_id=conversation_id, thread_id=thread_id
        )
        return thread_id

    def list_records(self) -> list[SessionRecord]:
        self._expire()
        return sorted(self._records.values(), key=lambda item: (item.channel, item.conversation_id))

    def clear(self) -> None:
        self._records.clear()

    # ---------------------------------------------------------------- 内部

    def _expire(self) -> None:
        if not self._idle_timeout:
            return
        now = monotonic()
        expired = [
            key
            for key, record in self._records.items()
            if now - record.created_at > self._idle_timeout
        ]
        for key in expired:
            del self._records[key]

    @staticmethod
    def _make_thread_id(conversation_id: str, channel: str) -> str:
        safe_channel = _UNSAFE.sub("-", channel).strip("-") or "channel"
        safe_conversation = _UNSAFE.sub("-", conversation_id).strip("-")[:_MAX_CONVERSATION_CHARS]
        suffix = uuid.uuid4().hex[:8]
        if safe_conversation:
            return f"{safe_channel}-{safe_conversation}-{suffix}"
        return f"{safe_channel}-{suffix}"


__all__ = ["SessionRecord", "SessionStore"]
