"""定时任务与投递目标的落点。

协议 + 内存实现；SQL 实现见 :class:`doudou.storage.scheduled.SqlSchedulerStore`。
任务数量级很小（几十条），因此**不做攒批写** —— 直接写反而更简单、更实时。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from doudou.observability.logging import get_logger
from doudou.scheduler.types import ScheduledTask

logger = get_logger(__name__)


class SchedulerStore(Protocol):
    """任务的读写 + 「用户 → 私聊会话」的映射。"""

    async def create(self, task: ScheduledTask) -> None: ...

    async def get(self, task_id: str) -> ScheduledTask | None: ...

    async def list_all(self) -> list[ScheduledTask]: ...

    async def list_due(self, *, now: datetime, limit: int = 50) -> list[ScheduledTask]:
        """取 ``enabled`` 且 ``next_run_at <= now`` 的任务，按到期时间升序。"""
        ...

    async def reschedule(
        self, task_id: str, *, next_run_at: datetime | None, enabled: bool = True
    ) -> None:
        """只推进时间点，不计数：``next_run_at=None`` 表示保持原值（一次性任务已用完）。"""
        ...

    async def record_result(
        self, task_id: str, *, last_run_at: datetime, status: str, error: str | None
    ) -> None:
        """一次触发结束后回写结果（``run_count`` 自增）。"""
        ...

    async def set_enabled(self, task_id: str, enabled: bool) -> bool: ...

    async def delete(self, task_id: str) -> bool: ...

    async def count(self) -> int: ...

    async def remember_target(self, *, channel: str, user_id: str, conversation_id: str) -> None:
        """记下「用户在渠道里的会话」；飞书私聊用过之后据此给 CLI/Web 任务投递。"""
        ...

    async def resolve_target(self, *, channel: str, owner_user_id: str | None) -> str | None:
        """解析 CLI/Web 任务的投递会话。

        * 配了 ``owner_user_id`` → 用该用户的绑定；
        * 否则只有**唯一一条**绑定才能确定，多条或零条一律返回 ``None``（→ 跳过本次）。
        """
        ...


class MemorySchedulerStore:
    """进程内实现（无数据库的部署与测试）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._targets: dict[tuple[str, str], str] = {}

    async def create(self, task: ScheduledTask) -> None:
        self._tasks[task.task_id] = task

    async def get(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    async def list_all(self) -> list[ScheduledTask]:
        return sorted(self._tasks.values(), key=lambda item: item.next_run_at)

    async def list_due(self, *, now: datetime, limit: int = 50) -> list[ScheduledTask]:
        due = [
            task
            for task in self._tasks.values()
            if task.enabled and _aware(task.next_run_at) <= _aware(now)
        ]
        due.sort(key=lambda item: _aware(item.next_run_at))
        return due[:limit]

    async def reschedule(
        self, task_id: str, *, next_run_at: datetime | None, enabled: bool = True
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        if next_run_at is not None:
            task.next_run_at = next_run_at
        task.enabled = enabled
        task.updated_at = datetime.now(UTC)

    async def record_result(
        self, task_id: str, *, last_run_at: datetime, status: str, error: str | None
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.last_run_at = last_run_at
        task.last_status = status
        task.last_error = error
        task.run_count += 1
        task.updated_at = datetime.now(UTC)

    async def set_enabled(self, task_id: str, enabled: bool) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.enabled = enabled
        task.updated_at = datetime.now(UTC)
        return True

    async def delete(self, task_id: str) -> bool:
        return self._tasks.pop(task_id, None) is not None

    async def count(self) -> int:
        return len(self._tasks)

    async def remember_target(self, *, channel: str, user_id: str, conversation_id: str) -> None:
        self._targets[(channel, user_id)] = conversation_id

    async def resolve_target(self, *, channel: str, owner_user_id: str | None) -> str | None:
        if owner_user_id:
            return self._targets.get((channel, owner_user_id))
        candidates = [value for (name, _), value in self._targets.items() if name == channel]
        unique = set(candidates)
        if len(unique) == 1:
            return candidates[0]
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = ["MemorySchedulerStore", "SchedulerStore"]
