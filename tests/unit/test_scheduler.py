"""定时任务：存储、投递目标、调度触发。

这里钉住的是需求里最容易写错的几条：

1. **没有可推送目标就跳过整次触发**（且不跑模型）；
2. 触发**先推进 next_run_at**，长运行不会叠触发；
3. 停机期间错过的触发点默认**不补跑**；
4. CLI/Web 建的任务靠飞书私聊绑定解析目标，解析不到就跳过。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from doudou.channels.base import InboundMessage
from doudou.config.schema import StorageConfig
from doudou.scheduler.runtime import SchedulerRuntime
from doudou.scheduler.store import MemorySchedulerStore
from doudou.scheduler.targets import TargetRecorder, creation_target, is_private_chat
from doudou.scheduler.types import ScheduledTask
from doudou.storage.database import create_database
from doudou.storage.scheduled import SqlSchedulerStore


@pytest.fixture
async def database(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = StorageConfig(database_url=f"sqlite+aiosqlite:///{(tmp_path / 'sched.db').as_posix()}")
    db = create_database(config)
    await db.create_schema()
    yield db
    await db.dispose()


class FakeSink:
    """假的 EventSink：只记录被推送的消息。"""

    name = "feishu"

    def __init__(self) -> None:
        self.delivered: list[InboundMessage] = []

    async def send(self, message: object) -> None:  # pragma: no cover - 不会用到
        pass

    async def deliver(self, message: InboundMessage, events: object) -> None:
        self.delivered.append(message)
        async for _ in events:  # type: ignore[attr-defined]
            pass


class FakeRouter:
    """记录 run_now 调用；可选择抛错。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[InboundMessage] = []
        self.fail = fail

    async def run_now(self, message: InboundMessage, sink: object) -> None:
        self.calls.append(message)
        if self.fail:
            raise RuntimeError("boom")


def _task(**overrides: object) -> ScheduledTask:
    base: dict[str, object] = {
        "task_id": "task-1",
        "name": "喝水",
        "prompt": "提醒我喝水",
        "kind": "every",
        "expr": "30m",
        "timezone": None,
        "channel": "feishu",
        "conversation_id": "oc_group",
        "enabled": True,
        "next_run_at": datetime.now(UTC) - timedelta(seconds=1),
    }
    base.update(overrides)
    return ScheduledTask(**base)  # type: ignore[arg-type]


def _runtime(store: MemorySchedulerStore, router: FakeRouter, **kw: object) -> SchedulerRuntime:
    return SchedulerRuntime(store=store, router=router, channels=[FakeSink()], **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 触发


async def test_fires_due_task_and_advances_next_run() -> None:
    store = MemorySchedulerStore()
    task = _task()
    await store.create(task)
    router = FakeRouter()
    runtime = _runtime(store, router)

    assert await runtime.tick() == 1
    await runtime.drain()

    assert len(router.calls) == 1
    assert router.calls[0].conversation_id == "oc_group"
    assert "[定时任务]" in router.calls[0].text
    saved = await store.get("task-1")
    assert saved is not None and saved.last_status == "ok" and saved.run_count == 1
    # 时间点已推进到将来
    assert saved.next_run_at > datetime.now(UTC)


async def test_skips_when_target_unresolved() -> None:
    """没有可推送目标 → 跳过，且**不跑模型**。"""
    store = MemorySchedulerStore()
    await store.create(_task(conversation_id=None))
    router = FakeRouter()
    runtime = _runtime(store, router)

    await runtime.tick()
    await runtime.drain()

    assert router.calls == []
    saved = await store.get("task-1")
    assert saved is not None and saved.last_status == "skipped"


async def test_resolves_feishu_private_for_cli_task() -> None:
    store = MemorySchedulerStore()
    await store.remember_target(channel="feishu", user_id="ou_1", conversation_id="oc_p2p")
    task = _task(conversation_id=None)
    await store.create(task)
    router = FakeRouter()
    runtime = _runtime(store, router)

    await runtime.tick()
    await runtime.drain()

    assert [call.conversation_id for call in router.calls] == ["oc_p2p"]
    saved = await store.get("task-1")
    assert saved is not None and saved.last_status == "ok"


async def test_skips_when_channel_not_running() -> None:
    store = MemorySchedulerStore()
    await store.create(_task(conversation_id="oc_group"))
    router = FakeRouter()
    runtime = SchedulerRuntime(store=store, router=router, channels=[])  # type: ignore[arg-type]

    await runtime.tick()
    await runtime.drain()

    assert router.calls == []
    saved = await store.get("task-1")
    assert saved is not None and saved.last_status == "skipped"


async def test_missed_window_is_not_caught_up_by_default() -> None:
    store = MemorySchedulerStore()
    stale = _task(next_run_at=datetime.now(UTC) - timedelta(hours=5))
    await store.create(stale)
    router = FakeRouter()
    runtime = _runtime(store, router)

    await runtime.tick()
    await runtime.drain()

    assert router.calls == []  # 不补跑
    saved = await store.get("task-1")
    assert saved is not None and saved.run_count == 0
    assert saved.next_run_at > datetime.now(UTC)


async def test_router_failure_marks_failed() -> None:
    store = MemorySchedulerStore()
    await store.create(_task())
    router = FakeRouter(fail=True)
    runtime = _runtime(store, router)

    await runtime.tick()
    await runtime.drain()

    saved = await store.get("task-1")
    assert saved is not None and saved.last_status == "failed"
    assert saved.last_error and "boom" in saved.last_error


# --------------------------------------------------------------------------- 目标


def test_creation_target_by_channel() -> None:
    group = InboundMessage(channel="feishu", conversation_id="oc_g", text="x")
    cli = InboundMessage(channel="cli", conversation_id="local", text="x")

    assert creation_target(group) == ("feishu", "oc_g")
    # CLI/Web 没有渠道会话 → 目标待解析
    assert creation_target(cli) == ("feishu", None)


async def test_target_recorder_only_records_feishu_private() -> None:
    store = MemorySchedulerStore()
    recorder = TargetRecorder(store)

    await recorder.record(
        InboundMessage(
            channel="feishu",
            conversation_id="oc_p2p",
            user_id="ou_1",
            text="hi",
            raw={"chat_type": "p2p"},
        )
    )
    # 群消息不记（群任务直接存群会话，无需绑定）
    await recorder.record(
        InboundMessage(
            channel="feishu",
            conversation_id="oc_g",
            user_id="ou_1",
            text="hi",
            raw={"chat_type": "group"},
        )
    )
    # 其它渠道不记
    await recorder.record(InboundMessage(channel="cli", conversation_id="local", text="hi"))

    assert await store.resolve_target(channel="feishu", owner_user_id="ou_1") == "oc_p2p"
    assert is_private_chat(
        InboundMessage(channel="feishu", conversation_id="oc", text="x", raw={"chat_type": "p2p"})
    )


async def test_resolve_target_needs_owner_when_ambiguous() -> None:
    store = MemorySchedulerStore()
    await store.remember_target(channel="feishu", user_id="ou_1", conversation_id="oc_1")
    await store.remember_target(channel="feishu", user_id="ou_2", conversation_id="oc_2")

    # 多条绑定且没指定 owner → 无法确定
    assert await store.resolve_target(channel="feishu", owner_user_id=None) is None
    assert await store.resolve_target(channel="feishu", owner_user_id="ou_2") == "oc_2"


# --------------------------------------------------------------------------- SQL 存储


async def test_sql_store_roundtrip(database) -> None:  # type: ignore[no-untyped-def]
    store = SqlSchedulerStore(database)
    task = _task()

    await store.create(task)
    loaded = await store.get("task-1")
    assert loaded is not None and loaded.name == "喝水"
    assert await store.count() == 1

    due = await store.list_due(now=datetime.now(UTC))
    assert [item.task_id for item in due] == ["task-1"]

    upcoming = datetime.now(UTC) + timedelta(hours=1)
    await store.reschedule("task-1", next_run_at=upcoming, enabled=True)
    assert await store.list_due(now=datetime.now(UTC)) == []

    await store.record_result("task-1", last_run_at=datetime.now(UTC), status="ok", error=None)
    saved = await store.get("task-1")
    assert saved is not None and saved.run_count == 1 and saved.last_status == "ok"

    assert await store.set_enabled("task-1", False) is True
    assert await store.delete("task-1") is True
    assert await store.get("task-1") is None


async def test_sql_store_targets(database) -> None:  # type: ignore[no-untyped-def]
    store = SqlSchedulerStore(database)

    await store.remember_target(channel="feishu", user_id="ou_1", conversation_id="oc_1")
    assert await store.resolve_target(channel="feishu", owner_user_id="ou_1") == "oc_1"
    assert await store.resolve_target(channel="feishu", owner_user_id=None) == "oc_1"

    await store.remember_target(channel="feishu", user_id="ou_2", conversation_id="oc_2")
    assert await store.resolve_target(channel="feishu", owner_user_id=None) is None
