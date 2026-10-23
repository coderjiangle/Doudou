"""In-process async message bus with per-conversation queues.

为什么需要它：飞书的事件是从 **lark 的长连接线程** 进来的（那是另一个 event loop），
不能在那里直接跑 Agent —— 会阻塞心跳与后续事件。总线做两件事：

1. 把执行搬回主事件循环（由调用方 ``await``）；
2. 保证**同一会话的消息按到达顺序串行处理**，不同会话并发。

队列满时**拒绝**而不是无限堆积：说明这个会话已经被刷屏，与其把内存吃光，
不如明确告诉用户「上一条还在处理」。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 一个会话最多排队的任务数。
DEFAULT_MAX_PENDING = 8
#: 同时执行的会话数上限。
DEFAULT_MAX_CONCURRENT = 4

Job = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class BusStats:
    """运行计数，挂在 ``/health`` 上。"""

    submitted: int = 0
    rejected: int = 0
    completed: int = 0
    failed: int = 0
    conversations: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "submitted": self.submitted,
            "rejected": self.rejected,
            "completed": self.completed,
            "failed": self.failed,
            "conversations": self.conversations,
        }


class MessageBus:
    """按会话串行、跨会话并发的进程内任务队列。"""

    def __init__(
        self,
        *,
        max_pending_per_conversation: int = DEFAULT_MAX_PENDING,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ) -> None:
        self.max_pending = max(1, max_pending_per_conversation)
        self.max_concurrent = max(1, max_concurrent)
        self._queues: dict[str, asyncio.Queue[Job | None]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._semaphore: asyncio.Semaphore | None = None
        #: 正在执行（已被 worker 取走但还没跑完）的任务数。
        #: 不能只看 queue.empty()：worker 一 get 就把条目移出队列，
        #: 于是「队列空」并不代表「活儿干完了」。
        self._in_flight = 0
        self._stats = BusStats()
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        """准备信号量。真正的 worker 在第一个任务到达时按需创建。"""
        # Semaphore 必须在事件循环里创建（3.10+ 虽可跨循环，但显式绑定更安全）。
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        logger.info("bus.started", max_pending=self.max_pending, max_concurrent=self.max_concurrent)

    async def stop(self) -> None:
        """停掉所有 worker。

        先投毒丸让 worker 自然退出，超时后强杀 —— 直接 cancel 可能打断正在写的会话。
        """
        async with self._lock:
            queues, workers = list(self._queues.values()), list(self._workers.values())
            self._queues.clear()
            self._workers.clear()

        for queue in queues:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                # 队列满时投不进毒丸，只能靠下面的 cancel 收场。
                logger.debug("bus.stop_queue_full")

        if workers:
            _done, pending = await asyncio.wait(workers, timeout=5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        self._semaphore = None
        logger.info("bus.stopped", **self._stats.as_dict())

    # ---------------------------------------------------------------- 提交

    async def submit(self, key: str, job: Job) -> bool:
        """把任务排进 ``key`` 对应的队列。

        Returns:
            ``False`` 表示队列已满（调用方应告知用户稍后重试）。
        """
        async with self._lock:
            queue = self._queues.get(key)
            if queue is None:
                queue = asyncio.Queue(maxsize=self.max_pending)
                self._queues[key] = queue
                self._workers[key] = asyncio.create_task(
                    self._worker(key, queue), name=f"bus:{key}"
                )

        try:
            queue.put_nowait(job)
        except asyncio.QueueFull:
            self._stats.rejected += 1
            logger.warning("bus.rejected", conversation=key, pending=queue.qsize())
            return False

        self._stats.submitted += 1
        logger.debug("bus.submitted", conversation=key, pending=queue.qsize())
        return True

    async def drain(self, *, timeout_seconds: float = 30.0) -> bool:
        """等待所有会话的任务跑完。测试与优雅关闭用。

        判定条件是「队列空 **且** 没有在途任务」—— 只看队列会漏掉正在执行的那条。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            async with self._lock:
                queues = list(self._queues.values())
            if self._in_flight == 0 and all(queue.empty() for queue in queues):
                return True
            if loop.time() > deadline:
                return False
            await asyncio.sleep(0.02)

    @property
    def stats(self) -> BusStats:
        stats = BusStats(
            submitted=self._stats.submitted,
            rejected=self._stats.rejected,
            completed=self._stats.completed,
            failed=self._stats.failed,
            conversations=len(self._queues),
        )
        return stats

    # ---------------------------------------------------------------- worker

    async def _worker(self, key: str, queue: asyncio.Queue[Job | None]) -> None:
        """逐条取任务执行；同一 key 只有这一个 worker，因此天然串行。"""
        try:
            while True:
                job = await queue.get()
                if job is None:  # 毒丸：优雅退出
                    return
                self._in_flight += 1
                try:
                    assert self._semaphore is not None, "bus.start() 必须先于任务提交"
                    # 信号量只限制「同时在跑的会话数」，同一会话内部的顺序由单 worker 保证。
                    async with self._semaphore:
                        await job()
                    self._stats.completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._stats.failed += 1
                    logger.error("bus.job_failed", conversation=key, error=str(exc), exc_info=True)
                finally:
                    self._in_flight -= 1
                    queue.task_done()
        except asyncio.CancelledError:  # pragma: no cover - 关闭时强杀
            logger.debug("bus.worker_cancelled", conversation=key)
            raise

    # ---------------------------------------------------------------- 辅助


__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_PENDING",
    "BusStats",
    "MessageBus",
]
