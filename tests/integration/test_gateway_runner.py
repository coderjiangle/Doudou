"""gateway：多轮历史、会话隔离、历史裁剪与残段清理。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from doudou.agent.factory import build_agent
from doudou.agent.testing import ScriptedChatModel, tool_call
from doudou.config.loader import Settings
from doudou.gateway.history import InMemoryHistoryStore, trim_history
from doudou.gateway.runner import AgentRunner, truncate_dangling_tool_calls
from doudou.gateway.schema import RunRequest
from doudou.gateway.session import SessionStore


def make_runner(
    settings: Settings,
    steps: list[object],
    *,
    store: InMemoryHistoryStore | None = None,
) -> AgentRunner:
    bundle = build_agent(settings, model=ScriptedChatModel(steps=list(steps)))
    return AgentRunner(bundle, store=store or InMemoryHistoryStore())


async def history_of(runner: AgentRunner, conversation_id: str) -> list:
    request = RunRequest(text="", conversation_id=conversation_id)
    return await runner.history(await runner.resolve_thread(request))


# --------------------------------------------------------------------------- 多轮


async def test_history_is_kept_across_turns(settings: Settings) -> None:
    """第二轮能看到第一轮的问答 —— 这是「多轮」的基本要求。"""
    runner = make_runner(
        settings, [AIMessage(content="第一轮答复"), AIMessage(content="第二轮答复")]
    )

    first = await runner.ainvoke(RunRequest(text="第一个问题", conversation_id="c1"))
    second = await runner.ainvoke(RunRequest(text="第二个问题", conversation_id="c1"))

    assert first.text == "第一轮答复"
    assert second.text == "第二轮答复"
    # 历史：Human, AI, Human, AI
    assert len(await history_of(runner, "c1")) == 4


async def test_tool_round_trip_is_recorded_in_history(settings: Settings) -> None:
    runner = make_runner(
        settings,
        [
            tool_call("now", {}, call_id="c1"),
            AIMessage(content="现在是某个时刻"),
            AIMessage(content="后续答复"),
        ],
    )

    await runner.ainvoke(RunRequest(text="几点", conversation_id="c1"))
    result = await runner.ainvoke(RunRequest(text="再说一次", conversation_id="c1"))

    assert result.text == "后续答复"
    tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert tool_messages
    assert all(m.tool_call_id for m in tool_messages)


async def test_conversations_are_isolated(settings: Settings) -> None:
    """不同会话拿到不同 thread，历史互不相干。"""
    runner = make_runner(settings, [AIMessage(content="答复")])

    await runner.ainvoke(RunRequest(text="问题 A", conversation_id="conv-a"))
    await runner.ainvoke(RunRequest(text="问题 B", conversation_id="conv-b"))

    thread_a = await runner.resolve_thread(RunRequest(text="", conversation_id="conv-a"))
    thread_b = await runner.resolve_thread(RunRequest(text="", conversation_id="conv-b"))

    assert thread_a != thread_b
    assert len(await runner.history(thread_a)) == 2
    assert len(await runner.history(thread_b)) == 2
    assert (await runner.history(thread_a))[0].content == "问题 A"
    assert (await runner.history(thread_b))[0].content == "问题 B"


async def test_new_thread_resets_history(settings: Settings) -> None:
    """``/new`` 后新 thread 从零开始，旧 thread 的历史保持不动。"""
    runner = make_runner(settings, [AIMessage(content="答复")])

    await runner.ainvoke(RunRequest(text="第一个问题", conversation_id="c1"))
    old_thread = await runner.resolve_thread(RunRequest(text="", conversation_id="c1"))

    await runner.ainvoke(RunRequest(text="第二个问题", conversation_id="c1", new_thread=True))
    new_thread = await runner.resolve_thread(RunRequest(text="", conversation_id="c1"))

    assert new_thread != old_thread
    assert len(await runner.history(new_thread)) == 2
    assert len(await runner.history(old_thread)) == 2
    assert (await runner.history(new_thread))[0].content == "第二个问题"


async def test_explicit_thread_id_wins(settings: Settings) -> None:
    runner = make_runner(settings, [AIMessage(content="ok")])

    await runner.ainvoke(RunRequest(text="问题", conversation_id="c1", thread_id="fixed"))

    assert await runner.history("fixed")


async def test_system_prompt_is_not_stored_in_history(settings: Settings) -> None:
    """历史里不含系统提示词：它由 agent 每轮自己插入，否则会越堆越多。"""
    runner = make_runner(settings, [AIMessage(content="ok")])

    await runner.ainvoke(RunRequest(text="问题", conversation_id="c1"))

    assert not any(isinstance(message, SystemMessage) for message in await history_of(runner, "c1"))


async def test_list_and_clear_threads(settings: Settings) -> None:
    runner = make_runner(settings, [AIMessage(content="ok")])

    await runner.ainvoke(RunRequest(text="问题", conversation_id="c1"))
    infos = await runner.list_threads()

    assert len(infos) == 1
    assert infos[0].message_count == 2

    assert await runner.clear_thread(infos[0].thread_id) is True
    assert await runner.list_threads() == []


async def test_failed_run_still_records_the_question(settings: Settings) -> None:
    """运行失败也要留下用户提问，否则用户会以为消息丢了。"""
    runner = make_runner(
        settings,
        [tool_call("nope", {}, call_id="c1")],
    )
    runner.bundle.agent.max_steps = 1  # 立刻撞上限，产生 error 事件

    result = await runner.ainvoke(RunRequest(text="问题", conversation_id="c1"))

    assert result.ok is False
    stored = await history_of(runner, "c1")
    assert stored[0].content == "问题"


# --------------------------------------------------------------------------- 历史维护


def test_truncate_dangling_tool_calls_drops_incomplete_tail() -> None:
    """有 tool_call 但没有对应 ToolMessage 的尾部必须丢掉，否则下一轮会被 provider 拒绝。"""
    messages = [
        HumanMessage(content="问"),
        AIMessage(
            content="", tool_calls=[{"name": "now", "args": {}, "id": "c1", "type": "tool_call"}]
        ),
    ]

    assert truncate_dangling_tool_calls(messages) == [messages[0]]


def test_truncate_dangling_tool_calls_keeps_complete_tail() -> None:
    messages = [
        HumanMessage(content="问"),
        AIMessage(
            content="", tool_calls=[{"name": "now", "args": {}, "id": "c1", "type": "tool_call"}]
        ),
        ToolMessage(content="结果", tool_call_id="c1", name="now"),
        AIMessage(content="答"),
    ]

    assert truncate_dangling_tool_calls(messages) == messages


def test_truncate_dangling_tool_calls_drops_orphan_tool_message() -> None:
    messages = [HumanMessage(content="问"), ToolMessage(content="孤儿", tool_call_id="c9")]

    assert truncate_dangling_tool_calls(messages) == [messages[0]]


def test_truncate_dangling_tool_calls_partial_coverage_breaks() -> None:
    """两个工具调用只回来一个：整组丢弃。"""
    messages = [
        HumanMessage(content="问"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "now", "args": {}, "id": "c1", "type": "tool_call"},
                {"name": "now", "args": {}, "id": "c2", "type": "tool_call"},
            ],
        ),
        ToolMessage(content="只有一个结果", tool_call_id="c1", name="now"),
    ]

    assert truncate_dangling_tool_calls(messages) == [messages[0]]


def test_trim_history_keeps_recent_and_avoids_orphan_tool_message() -> None:
    messages = [
        HumanMessage(content="1"),
        AIMessage(content="a"),
        HumanMessage(content="2"),
        ToolMessage(content="结果", tool_call_id="c1"),
        HumanMessage(content="3"),
        AIMessage(content="b"),
    ]

    trimmed = trim_history(messages, 3)

    # 裁剪点本应落在索引 3 的 ToolMessage 上，向后顺延
    assert not isinstance(trimmed[0], ToolMessage)
    assert trimmed == messages[4:]


def test_trim_history_noop_when_short() -> None:
    messages = [HumanMessage(content="1"), AIMessage(content="a")]

    assert trim_history(messages, 10) == messages
    assert trim_history(messages, 0) == messages


# --------------------------------------------------------------------------- 会话存储（thread id 生成）


def test_session_store_get_or_create_is_stable() -> None:
    store = SessionStore()

    first = store.get_or_create("chat-1")
    assert store.get_or_create("chat-1") == first
    assert store.get("chat-1") == first


def test_session_store_create_makes_new_thread() -> None:
    store = SessionStore()

    first = store.create("chat-1")
    second = store.create("chat-1")

    assert first != second
    assert store.get("chat-1") == second


def test_session_store_isolates_by_channel() -> None:
    store = SessionStore()

    cli = store.get_or_create("same-id", channel="cli")
    feishu = store.get_or_create("same-id", channel="feishu")

    assert cli != feishu
    assert {record.channel for record in store.list_records()} == {"cli", "feishu"}


def test_session_store_thread_id_is_readable() -> None:
    store = SessionStore()

    thread_id = store.create("oc_abc123!@#")

    # 通道名 + 规范化后的会话 ID + 随机后缀；下划线保留（飞书 ID 里常见）
    assert thread_id.startswith("cli-oc_abc123")
    assert "!" not in thread_id and "@" not in thread_id and "#" not in thread_id


def test_session_store_get_missing_returns_none() -> None:
    assert SessionStore().get("nothing") is None


async def test_in_memory_store_trims_by_count() -> None:
    store = InMemoryHistoryStore(max_messages=3)

    await store.append("t1", [HumanMessage(content=str(index)) for index in range(5)])

    messages = await store.load("t1")
    assert [message.content for message in messages] == ["2", "3", "4"]
    info = await store.get_thread("t1")
    assert info is not None and info.message_count == 3


async def test_runner_persists_periodic_persona_anchor_and_strips_for_display(
    settings_factory,  # type: ignore[no-untyped-def]
) -> None:
    """每隔 N 轮在用户消息后固化锚点并落库（保证下轮前缀严格一致），对外 history 剥离。"""
    seen_prompts: list[list[str]] = []

    def _capture(messages: list) -> AIMessage:
        seen_prompts.append([str(msg.content) for msg in messages])
        return AIMessage(content="汪")

    settings = settings_factory(
        config_yaml=('agent:\n  persona_reminder: "保持豆豆口吻"\n  persona_reminder_interval: 3\n')
    )
    model = ScriptedChatModel(steps=[_capture, _capture, _capture, _capture])
    runner = AgentRunner(build_agent(settings, model=model))

    for idx in range(1, 5):
        result = await runner.ainvoke(RunRequest(text=f"第{idx}问", conversation_id="conv-cache"))
        # 对外展示结果中永远不应看到 [人设提醒]
        assert all("[人设提醒]" not in str(m.content) for m in result.messages)

    # 第 1、2 轮不插入锚点；第 3 轮插入并落库；第 4 轮不插入且第 3 轮前缀一字不变
    assert "[人设提醒]" not in seen_prompts[0][-1]
    assert "[人设提醒]" not in seen_prompts[1][-1]
    assert "[人设提醒] 保持豆豆口吻" in seen_prompts[2][-1]
    assert "[人设提醒]" not in seen_prompts[3][-1]
    # Prefix Cache 关键约束：第 4 轮看到的第 3 轮前缀与第 3 轮发给模型时完全一致
    assert seen_prompts[3][:-2] == seen_prompts[2]

    thread_id = await runner.resolve_thread(RunRequest(text="", conversation_id="conv-cache"))
    raw_stored = await runner.store.load(thread_id)
    display_history = await runner.history(thread_id)
    assert any("[人设提醒] 保持豆豆口吻" in str(m.content) for m in raw_stored)
    assert [str(m.content) for m in display_history if isinstance(m, HumanMessage)] == [
        "第1问",
        "第2问",
        "第3问",
        "第4问",
    ]
