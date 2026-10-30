"""调度运行期：轮询到期任务 → 交给 router 跑一轮 → 回写状态。

设计要点：

* **DB 是任务的唯一真相源**，进程只做轮询，因此重启天然安全；
* 触发**先推进 ``next_run_at`` 再执行**，长运行不会因为下个 tick 又扫到而叠触发；
* 「没有可推送目标」→ 记 ``skipped`` 并跳过整次触发（不跑模型、不推送）；
* 每个任务在一次独立 asyncio 任务里跑，带超时与并发上限，互不阻塞。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from doudou.channels.base import Channel, EventSink, InboundMessage
from doudou.gateway.router import MessageRouter
from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics
from doudou.scheduler.store import SchedulerStore
from doudou.scheduler.targets import resolve_delivery
from doudou.scheduler.triggers import next_fire_time
from doudou.scheduler.types import ScheduledTask

logger = get_logger(__name__)

#: 一次 tick 最多处理多少条到期任务（防止积压把一轮 tick 拖太久）。
DEFAULT_BATCH = 20
#: 同时执行的定时任务数上限。
DEFAULT_MAX_CONCURRENT = 4


@dataclass(slots=True)
class SchedulerStats:
    fired: int = 0
    skipped: int = 0
    failed: int = 0
    last_tick_at: datetime | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "fired": self.fired,
            "skipped": self.skipped,
            "failed": self.failed,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
        }


class SchedulerRuntime:
    """定时任务的轮询与触发。随服务启停，只在 ``doudou serve`` 下运行。"""

    def __init__(
        self,
        *,
        store: SchedulerStore,
        router: MessageRouter,
        channels: Sequence[Channel],
        tick_seconds: float = 30.0,
        default_timezone: str = "UTC",
        owner_user_id: str | None = None,
        run_timeout_seconds: float = 600.0,
        catch_up: bool = False,
        batch: int = DEFAULT_BATCH,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ) -> None:
        self.store = store
        self.router = router
        self.channels = {channel.name: channel for channel in channels}
        self.tick_seconds = max(1.0, tick_seconds)
        self.default_timezone = default_timezone
        self.owner_user_id = owner_user_id
        self.run_timeout_seconds = max(1.0, run_timeout_seconds)
        self.catch_up = catch_up
        self.batch = max(1, batch)
        self.stats = SchedulerStats()
        self._task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._inflight: set[asyncio.Task[None]] = set()
        #: 超过这个时长还没被处理，就认定是「停机期间错过的一次触发」。
        self._missed_grace_seconds = max(self.tick_seconds * 3, 90.0)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        logger.info(
            "scheduler.started",
            tick_seconds=self.tick_seconds,
            timezone=self.default_timezone,
            owner_user_id=self.owner_user_id,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._inflight:
            for task in list(self._inflight):
                task.cancel()
            await asyncio.gather(*self._inflight, return_exceptions=True)
            self._inflight.clear()
        logger.info("scheduler.stopped", **self.stats.as_dict())

    # ---------------------------------------------------------------- 轮询

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 轮询本身绝不能因为一条坏任务而死
                logger.error("scheduler.tick_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(self.tick_seconds)

    async def tick(self, *, now: datetime | None = None) -> int:
        """扫一次到期任务并触发，返回本轮处理条数（测试可直接调用）。"""
        moment = now or datetime.now(UTC)
        self.stats.last_tick_at = moment
        due = await self.store.list_due(now=moment, limit=self.batch)
        for task in due:
            # 先算下一个触发点、先落库，避免长运行被下一个 tick 重复扫到。
            upcoming = next_fire_time(
                task.kind,
                task.expr,
                task.timezone,
                now=moment,
                default_timezone=self.default_timezone,
            )
            overdue = (moment - _aware(task.next_run_at)).total_seconds()
            if overdue > self._missed_grace_seconds and not self.catch_up:
                # 停机期间错过的触发点：默认不补跑，只把时间点顺延。
                await self.store.reschedule(
                    task.task_id, next_run_at=upcoming, enabled=upcoming is not None
                )
                logger.info(
                    "scheduler.missed_skipped",
                    task_id=task.task_id,
                    overdue_seconds=round(overdue),
                )
                continue
            await self.store.reschedule(
                task.task_id, next_run_at=upcoming, enabled=upcoming is not None
            )
            running = asyncio.create_task(self._fire(task, moment))
            self._inflight.add(running)
            running.add_done_callback(self._inflight.discard)
        return len(due)

    async def drain(self, *, timeout_seconds: float = 30.0) -> None:
        """等待当前在途的触发跑完（测试与优雅关闭用）。"""
        if not self._inflight:
            return
        await asyncio.wait(list(self._inflight), timeout=timeout_seconds)

    async def _fire(self, task: ScheduledTask, now: datetime) -> None:
        async with self._semaphore:
            delivery = await resolve_delivery(self.store, task, owner_user_id=self.owner_user_id)
            if delivery is None:
                # 没有可推送目标 → 跳过整次触发（不跑模型）。
                await self._record(task, now, status="skipped", error="没有可推送的目标")
                self.stats.skipped += 1
                get_metrics().incr("scheduler.skipped")
                return
            channel_name, conversation_id = delivery
            sink = self.channels.get(channel_name)
            if sink is None or not isinstance(sink, EventSink):
                await self._record(task, now, status="skipped", error=f"通道 {channel_name} 未运行")
                self.stats.skipped += 1
                get_metrics().incr("scheduler.skipped")
                logger.warning(
                    "scheduler.channel_unavailable", task_id=task.task_id, channel=channel_name
                )
                return
            status, error = await self._dispatch(task, conversation_id, sink)
            await self._record(task, now, status=status, error=error)
            self.stats.fired += 1
            get_metrics().incr("scheduler.fired")

    async def _dispatch(
        self, task: ScheduledTask, conversation_id: str, sink: EventSink
    ) -> tuple[str, str | None]:
        message = InboundMessage(
            channel=task.channel,
            conversation_id=conversation_id,
            text=_trigger_text(task),
            user_id=task.created_by,
            raw={"scheduled": True, "task_id": task.task_id, "chat_type": "p2p"},
        )
        try:
            await asyncio.wait_for(
                self.router.run_now(message, sink), timeout=self.run_timeout_seconds
            )
        except TimeoutError:
            logger.warning("scheduler.run_timeout", task_id=task.task_id)
            return "failed", f"运行超过 {self.run_timeout_seconds:.0f}s 未结束"
        except Exception as exc:  # 运行异常不该拖垮调度循环
            logger.error(
                "scheduler.run_failed", task_id=task.task_id, error=str(exc), exc_info=True
            )
            return "failed", str(exc)
        return "ok", None

    async def _record(
        self, task: ScheduledTask, now: datetime, *, status: str, error: str | None
    ) -> None:
        await self.store.record_result(task.task_id, last_run_at=now, status=status, error=error)
        logger.info(
            "scheduler.fired_task",
            task_id=task.task_id,
            name=task.name,
            status=status,
            error=error,
        )


def _trigger_text(task: ScheduledTask) -> str:
    """写进会话历史的触发消息。**可见、可追溯**，用统一的 ``[定时任务]`` 前缀。"""
    return f"[定时任务] {task.name}：{task.prompt}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = ["DEFAULT_BATCH", "DEFAULT_MAX_CONCURRENT", "SchedulerRuntime", "SchedulerStats"]
