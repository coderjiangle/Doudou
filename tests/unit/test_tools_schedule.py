"""定时任务工具（``schedule_create`` / ``schedule_list`` / ``schedule_cancel``）。

重点验证「投递目标由当前会话决定」以及「无存储不注册」。
"""

from __future__ import annotations

from doudou.channels.base import InboundMessage, bind_inbound, unbind_inbound
from doudou.config.loader import Settings
from doudou.scheduler.store import MemorySchedulerStore
from doudou.tools.builtin import schedule


async def _call(tool, **args):  # type: ignore[no-untyped-def]
    return await tool.ainvoke(args)


def _tools(settings: Settings, store: MemorySchedulerStore) -> dict:
    return {tool.name: tool for tool in schedule.build_tools(settings, store=store)}


def test_no_store_means_no_tools(settings: Settings) -> None:
    assert schedule.build_tools(settings, store=None) == []


def test_builds_three_tools(settings: Settings) -> None:
    tools = _tools(settings, MemorySchedulerStore())

    assert set(tools) == {"schedule_create", "schedule_list", "schedule_cancel"}


async def test_create_in_group_targets_that_group(settings: Settings) -> None:
    store = MemorySchedulerStore()
    create = _tools(settings, store)["schedule_create"]
    token = bind_inbound(
        InboundMessage(channel="feishu", conversation_id="oc_group", user_id="ou_1", text="x")
    )
    try:
        result = await _call(create, name="站会", prompt="提醒大家站会", kind="every", expr="1d")
    finally:
        unbind_inbound(token)

    assert "已创建定时任务" in result
    tasks = await store.list_all()
    assert len(tasks) == 1
    assert tasks[0].conversation_id == "oc_group"
    assert tasks[0].channel == "feishu"


async def test_create_in_cli_targets_feishu_private_later(settings: Settings) -> None:
    store = MemorySchedulerStore()
    create = _tools(settings, store)["schedule_create"]
    token = bind_inbound(InboundMessage(channel="cli", conversation_id="local", text="x"))
    try:
        await _call(create, name="喝水", prompt="提醒我喝水", kind="cron", expr="0 9 * * *")
    finally:
        unbind_inbound(token)

    (task,) = await store.list_all()
    assert task.channel == "feishu"
    assert task.conversation_id is None  # 触发时再解析


async def test_create_rejects_bad_kind_and_expr(settings: Settings) -> None:
    store = MemorySchedulerStore()
    create = _tools(settings, store)["schedule_create"]

    assert "kind 只能" in await _call(create, name="x", prompt="y", kind="weekly", expr="1d")
    assert "无法解析" in await _call(create, name="x", prompt="y", kind="every", expr="很快")
    assert await store.count() == 0


async def test_create_refuses_when_at_capacity(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(config_yaml="scheduler:\n  max_tasks: 1\n")
    store = MemorySchedulerStore()
    create = _tools(settings, store)["schedule_create"]

    await _call(create, name="a", prompt="p", kind="every", expr="1d")
    result = await _call(create, name="b", prompt="p", kind="every", expr="1d")

    assert "上限" in result
    assert await store.count() == 1


async def test_list_and_cancel(settings: Settings) -> None:
    store = MemorySchedulerStore()
    tools = _tools(settings, store)
    await _call(tools["schedule_create"], name="喝水", prompt="提醒我喝水", kind="every", expr="1d")

    listing = await _call(tools["schedule_list"])
    assert "喝水" in listing and "共 1 个" in listing

    (task,) = await store.list_all()
    assert "已取消" in await _call(tools["schedule_cancel"], task_id=task.task_id)
    assert await store.count() == 0
    assert "没有找到" in await _call(tools["schedule_cancel"], task_id="task-nope")


async def test_list_empty(settings: Settings) -> None:
    tools = _tools(settings, MemorySchedulerStore())

    assert "没有任何定时任务" in await _call(tools["schedule_list"])
