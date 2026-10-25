"""审计与用量的写后缓冲。

压测（``scripts/loadtest.py``）量出来一件事：SQLite 同一时刻只允许一个写事务，
而一轮运行会产生五六次提交（历史 1 次 + 审计 4 次 + 用量 1 次），16 并发下吞吐
只有约 9 次/秒。瓶颈不在模型、不在工具，而在提交次数。所以把不在关键路径上的写
攒起来批量提交：审计晚几秒出现没关系，用量合并起来甚至更准确。历史消息不能这样
处理 —— 下一轮要靠它，见 ``gateway/history.py``。

代价：进程被 SIGKILL 时可能丢掉最后不足 ``max_pending`` 条（调成 1 就退回逐条
持久化）；读接口会先 flush 再查，所以「刚发生的事情查不到」不会发生，只是读会
顺带触发一次写。
"""

from __future__ import annotations

from typing import Any

from doudou.observability.audit import AuditEntry, AuditStore
from doudou.observability.cost import UsageRecord, UsageStore
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 默认攒多少条写一次。太小失去意义，太大丢数据的窗口变长。
DEFAULT_MAX_PENDING = 50


class BufferedAuditStore(AuditStore):
    """把审计写入攒成批。"""

    def __init__(self, inner: AuditStore, *, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self.inner = inner
        self.max_pending = max(1, max_pending)
        #: 用 dict 去重：同一次工具调用会写两次（先建、后补结果），
        #: 合并成同一批时只应保留最后的状态。
        self._pending: dict[str, AuditEntry] = {}

    async def append(self, entry: AuditEntry) -> None:
        await self.append_many([entry])

    async def append_many(self, entries: list[AuditEntry]) -> None:
        for entry in entries:
            # 用 dict 去重：同一次工具调用会写两次（先建、后补结果），
            # 合并成同一批时只应保留最后的状态。
            self._pending[entry.audit_id] = entry
        if len(self._pending) >= self.max_pending:
            await self.flush()

    async def flush(self) -> int:
        """把缓冲区落盘，返回写出的条数。"""
        if not self._pending:
            return 0
        entries = list(self._pending.values())
        self._pending.clear()
        append_many = getattr(self.inner, "append_many", None)
        try:
            if append_many is not None:
                await append_many(entries)
            else:
                for entry in entries:
                    await self.inner.append(entry)
        except Exception:
            # 失败就把这批放回去？不行 —— 会造成「越攒越多、越写越慢」的雪崩。
            # 审计失败只告警（观察者那层还有一道 try/except），丢一批可接受。
            logger.warning("audit.batch_failed", dropped=len(entries), exc_info=True)
            return 0
        logger.debug("audit.batch_flushed", entries=len(entries))
        return len(entries)

    async def list_recent(
        self, *, limit: int = 100, thread_id: str | None = None
    ) -> list[AuditEntry]:
        await self.flush()  # 读之前先落盘，避免「刚发生的查不到」
        return await self.inner.list_recent(limit=limit, thread_id=thread_id)

    async def get(self, audit_id: str) -> AuditEntry | None:
        await self.flush()
        return await self.inner.get(audit_id)

    async def aclose(self) -> None:
        await self.flush()


class BufferedUsageStore(UsageStore):
    """把用量累加攒成批。

    同类记录先在内存里合并，因此**还顺带减少了「读改写」的往返**：
    同一会话同一秒的多次运行会合并成一次累加。
    """

    def __init__(self, inner: UsageStore, *, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self.inner = inner
        self.max_pending = max(1, max_pending)
        self._pending: dict[tuple[str, str], UsageRecord] = {}

    async def add(self, record: UsageRecord) -> None:
        await self.add_many([record])

    async def add_many(self, records: list[UsageRecord]) -> None:
        for record in records:
            key = (record.day, record.thread_id)
            self._pending[key] = _merge(self._pending.get(key), record)
        if len(self._pending) >= self.max_pending:
            await self.flush()

    async def flush(self) -> int:
        if not self._pending:
            return 0
        records = list(self._pending.values())
        self._pending.clear()
        add_many = getattr(self.inner, "add_many", None)
        try:
            if add_many is not None:
                await add_many(records)
            else:
                for record in records:
                    await self.inner.add(record)
        except Exception:
            logger.warning("usage.batch_failed", dropped=len(records), exc_info=True)
            return 0
        logger.debug("usage.batch_flushed", records=len(records))
        return len(records)

    async def by_thread(self, *, days: int) -> list[UsageRecord]:
        await self.flush()
        return await self.inner.by_thread(days=days)

    async def by_day(self, *, days: int) -> list[UsageRecord]:
        await self.flush()
        return await self.inner.by_day(days=days)

    async def total(
        self, *, day: str | None = None, include_pending: bool = False
    ) -> tuple[int, int, float | None]:
        """``(input, output, cost)``。

        默认与其它读方法一致：**先 flush 再读**。唯一需要「不 flush」的是
        预算检查 —— 它每次运行后都会调用，落盘就等于把攒批的收益全抹掉，
        因此它传 ``include_pending=True``：把缓冲区里的量算进结果，但不落盘。

        这里刻意不用「调用方记得先调 by_thread」这种约定：读方法之间的顺序依赖
        是典型的静默陷阱 —— 顺序一变，总数就变成 0，而且没有任何报错。
        """
        if not include_pending:
            await self.flush()
            return await self.inner.total(day=day)

        committed = await self.inner.total(day=day)
        input_tokens, output_tokens, cost = committed
        for (record_day, _thread), record in self._pending.items():
            if day is not None and record_day != day:
                continue
            input_tokens += record.input_tokens
            output_tokens += record.output_tokens
            cost = _add_cost(cost, record.cost)
        return input_tokens, output_tokens, cost

    async def aclose(self) -> None:
        await self.flush()


def _merge(current: UsageRecord | None, incoming: UsageRecord) -> UsageRecord:
    """合并同一条 (day, thread) 的两次记录。"""
    if current is None:
        return incoming
    current.runs += incoming.runs
    current.input_tokens += incoming.input_tokens
    current.output_tokens += incoming.output_tokens
    current.tool_calls += incoming.tool_calls
    current.approvals += incoming.approvals
    current.rejections += incoming.rejections
    current.cost = _add_cost(current.cost, incoming.cost)
    current.model = incoming.model or current.model
    current.channel = incoming.channel or current.channel
    current.last_at = incoming.last_at
    return current


def _add_cost(left: float | None, right: float | None) -> float | None:
    """合并费用：任何一边未知，结果就是未知（``None`` 不等于 0）。"""
    if left is None or right is None:
        return None
    return round(left + right, 6)


def pending_count(store: object) -> int:
    """给测试与检视用的缓冲区长度。"""
    pending: Any = getattr(store, "_pending", None)
    return len(pending) if pending is not None else 0


__all__ = [
    "DEFAULT_MAX_PENDING",
    "BufferedAuditStore",
    "BufferedUsageStore",
    "pending_count",
]
