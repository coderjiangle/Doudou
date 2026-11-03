"""存储层：仓库读写、消息编解码、SQL 历史存储的裁剪与持久化。"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from doudou.config.schema import StorageConfig
from doudou.storage.database import Database, create_database
from doudou.storage.history import SqlHistoryStore
from doudou.storage.models import ThreadModel, as_utc, utcnow
from doudou.storage.repositories import (
    MessageRepository,
    SessionBindingRepository,
    ThreadRepository,
)

MESSAGES: list[BaseMessage] = [
    HumanMessage(content="现在几点？"),
    AIMessage(
        content="",
        tool_calls=[{"name": "now", "args": {}, "id": "c1", "type": "tool_call"}],
    ),
    ToolMessage(content="2026-10-23 11:50:17 CST（周五）", tool_call_id="c1", name="now"),
    AIMessage(content="现在是 11:50。"),
]


@pytest.fixture
async def database(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = StorageConfig(database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    db = create_database(config)
    await db.create_schema()
    yield db
    await db.dispose()


# --------------------------------------------------------------------------- 建库


async def test_create_schema_creates_parent_directory(tmp_path: Path) -> None:
    """SQLite 文件所在目录不存在时要自动建出来。

    否则首次运行只会看到「unable to open database file」，完全看不出是缺目录。
    """
    nested = tmp_path / "deep" / "nested" / "doudou.db"
    db = create_database(StorageConfig(database_url=f"sqlite+aiosqlite:///{nested.as_posix()}"))

    await db.create_schema()

    assert nested.parent.is_dir()
    await db.dispose()


async def test_create_schema_is_idempotent(database: Database) -> None:
    await database.create_schema()

    async with database.session() as session:
        assert await ThreadRepository(session).list_recent() == []


# --------------------------------------------------------------------------- 仓库


async def test_thread_repository_ensure_and_count(database: Database) -> None:
    async with database.session() as session:
        threads = ThreadRepository(session)
        repository = MessageRepository(session)

        await threads.ensure("t1", channel="api", conversation_id="conv-1")
        for index in range(3):
            await repository.append("t1", role="human", content=str(index))

        assert await threads.message_count("t1") == 3
        rows = await threads.list_recent()
        assert len(rows) == 1
        thread, count = rows[0]
        assert thread.thread_id == "t1"
        assert thread.channel == "api"
        assert count == 3


async def test_thread_repository_ensure_does_not_reset_channel(database: Database) -> None:
    async with database.session() as session:
        threads = ThreadRepository(session)
        await threads.ensure("t1", channel="cli")
        await threads.ensure("t1", channel="api", conversation_id="conv-1")

        thread = await threads.get("t1")
        assert thread is not None
        assert thread.channel == "api"
        assert thread.conversation_id == "conv-1"


async def test_message_seq_is_monotonic(database: Database) -> None:
    """seq 决定模型看到上下文的顺序，必须稳定递增。"""
    async with database.session() as session:
        repository = MessageRepository(session)
        await ThreadRepository(session).ensure("t1")
        for content in ("a", "b", "c"):
            await repository.append("t1", role="human", content=content)

        seqs = [row.seq for row in await repository.list("t1")]
        assert seqs == [1, 2, 3]


async def test_message_trim_keeps_tool_groups_intact(database: Database) -> None:
    """裁剪点落在 tool 消息上时向后顺延，避免留下找不到 tool_call 的孤立结果。"""
    async with database.session() as session:
        repository = MessageRepository(session)
        await ThreadRepository(session).ensure("t1")
        await repository.append("t1", role="human", content="问")
        await repository.append(
            "t1",
            role="ai",
            content="",
            tool_calls=[{"name": "now", "args": {}, "id": "c1", "type": "tool_call"}],
        )
        await repository.append("t1", role="tool", content="结果", tool_call_id="c1")
        await repository.append("t1", role="ai", content="答")
        await repository.append("t1", role="human", content="再问")
        await repository.append("t1", role="ai", content="再答")

        # keep=3 的切点正好落在 tool 行（seq=3）上
        removed = await repository.trim("t1", 3)

        rows = await repository.list("t1")
        assert removed == 3
        assert [row.seq for row in rows] == [4, 5, 6]
        assert rows[0].role == "ai"


async def test_message_trim_noop_when_under_limit(database: Database) -> None:
    async with database.session() as session:
        repository = MessageRepository(session)
        await ThreadRepository(session).ensure("t1")
        await repository.append("t1", role="human", content="问")

        assert await repository.trim("t1", 10) == 0
        assert await repository.count("t1") == 1


async def test_session_binding_roundtrip(database: Database) -> None:
    async with database.session() as session:
        repository = SessionBindingRepository(session)

        assert await repository.get("cli", "conv-1") is None
        await repository.bind("cli", "conv-1", "t1")
        assert await repository.get("cli", "conv-1") == "t1"

        await repository.bind("cli", "conv-1", "t2")
        assert await repository.get("cli", "conv-1") == "t2"

        assert await repository.unbind("cli", "conv-1") is True
        assert await repository.get("cli", "conv-1") is None


async def test_delete_thread_cascades_messages(database: Database) -> None:
    async with database.session() as session:
        threads = ThreadRepository(session)
        repository = MessageRepository(session)
        await threads.ensure("t1")
        await repository.append("t1", role="human", content="问")

        assert await threads.delete("t1") is True

    async with database.session() as session:
        assert await MessageRepository(session).count("t1") == 0


# --------------------------------------------------------------------------- 历史存储


async def test_sql_store_roundtrip_preserves_message_shapes(database: Database) -> None:
    """工具调用与工具结果必须原样还原，否则下一轮请求会被 provider 拒绝。"""
    store = SqlHistoryStore(database)

    await store.append("t1", MESSAGES, channel="api", conversation_id="conv-1")
    restored = await store.load("t1")

    assert [type(message).__name__ for message in restored] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert restored[0].content == "现在几点？"
    assert restored[1].tool_calls == [{"name": "now", "args": {}, "id": "c1", "type": "tool_call"}]
    assert isinstance(restored[2], ToolMessage)
    assert restored[2].tool_call_id == "c1"
    assert restored[2].name == "now"
    assert restored[2].content.startswith("2026-10-23")
    assert restored[3].content == "现在是 11:50。"


async def test_sql_store_resolve_thread_is_stable_and_bindable(database: Database) -> None:
    store = SqlHistoryStore(database)

    first = await store.resolve_thread("conv-1", channel="cli")
    assert await store.resolve_thread("conv-1", channel="cli") == first

    second = await store.resolve_thread("conv-1", channel="cli", new=True)
    assert second != first
    assert await store.resolve_thread("conv-1", channel="cli") == second


async def test_sql_store_recovers_from_stale_binding(database: Database) -> None:
    """绑定指向的 thread 已被删除时，应重新分配而不是返回一个死 id。"""
    store = SqlHistoryStore(database)
    thread_id = await store.resolve_thread("conv-1", channel="cli")

    await store.clear(thread_id)

    recreated = await store.resolve_thread("conv-1", channel="cli")
    assert recreated != thread_id
    assert await store.get_thread(recreated) is not None


async def test_sql_store_clear_removes_binding(database: Database) -> None:
    """删会话要连渠道绑定一起删，不留「渠道会话 → 已删 thread」的悬空指向。"""
    store = SqlHistoryStore(database)
    thread_id = await store.resolve_thread("conv-1", channel="cli")

    async with database.session() as session:
        assert await SessionBindingRepository(session).list_for_thread(thread_id)

    assert await store.clear(thread_id) is True

    async with database.session() as session:
        assert await SessionBindingRepository(session).list_for_thread(thread_id) == []


async def test_sql_store_thread_info(database: Database) -> None:
    store = SqlHistoryStore(database)

    await store.append("t1", MESSAGES, channel="api", conversation_id="conv-1")
    info = await store.get_thread("t1")

    assert info is not None
    assert info.thread_id == "t1"
    assert info.channel == "api"
    assert info.conversation_id == "conv-1"
    assert info.message_count == len(MESSAGES)
    assert info.created_at is not None and info.created_at.tzinfo is not None


async def test_sql_store_list_threads_orders_by_recent(database: Database) -> None:
    store = SqlHistoryStore(database)

    await store.append("old", [HumanMessage(content="旧")])
    await store.append("new", [HumanMessage(content="新")])

    infos = await store.list_threads()
    assert [info.thread_id for info in infos] == ["new", "old"]


async def test_sql_store_trims(database: Database) -> None:
    store = SqlHistoryStore(database, max_messages=2)

    await store.append("t1", [HumanMessage(content=str(index)) for index in range(5)])

    messages = await store.load("t1")
    assert [message.content for message in messages] == ["3", "4"]


async def test_sql_store_unknown_thread_returns_none(database: Database) -> None:
    store = SqlHistoryStore(database)

    assert await store.get_thread("nope") is None
    assert await store.load("nope") == []
    assert await store.clear("nope") is False


async def test_sql_store_skips_corrupt_tool_rows(database: Database) -> None:
    """缺 tool_call_id 的 tool 行是坏数据，读出来时要跳过而不是喂给模型。"""
    store = SqlHistoryStore(database)
    await store.append("t1", [HumanMessage(content="问")])

    async with database.session() as session:
        await MessageRepository(session).append("t1", role="tool", content="坏行")

    assert [message.content for message in await store.load("t1")] == ["问"]


# --------------------------------------------------------------------------- 时间戳


def test_as_utc_attaches_timezone() -> None:
    """SQLite 不保时区，读出来可能是 naive；对外输出前必须补上 UTC。"""
    from datetime import UTC, datetime

    naive = datetime(2026, 10, 23, 12, 0, 0)
    assert as_utc(naive).tzinfo is UTC

    aware = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
    assert as_utc(aware) is aware


def test_utcnow_is_aware() -> None:
    assert utcnow().tzinfo is not None


def test_thread_model_table_name() -> None:
    assert ThreadModel.__tablename__ == "threads"


async def test_multimodal_history_preserves_images_and_text(database: Database) -> None:
    from doudou.agent.messages import ImageInput, human_message
    from doudou.storage.history import CONTENT_PREFIX
    from tests.unit.test_image_input import PNG_URL

    store = SqlHistoryStore(database)
    message = human_message("看这张图", [ImageInput(data_url=PNG_URL)])
    escaped_text = HumanMessage(content=CONTENT_PREFIX + '["普通文字，不是内容块"]')
    await store.append("images", [message, escaped_text, AIMessage(content="看到了")])
    reloaded = await SqlHistoryStore(database).load("images")
    assert [item.content for item in reloaded] == [message.content, escaped_text.content, "看到了"]
