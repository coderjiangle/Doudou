"""gateway 的消息总线与路由：按会话串行、跨会话并发、积压拒绝。"""

from __future__ import annotations

import asyncio

import pytest

from doudou.agent.events import RunEvent
from doudou.channels.base import InboundMessage, OutboundMessage
from doudou.config.loader import Settings
from doudou.gateway.bus import MessageBus
from doudou.gateway.router import BUSY_REPLY, MessageRouter


def inbound(text: str = "你好", *, conversation_id: str = "oc_chat") -> InboundMessage:
    return InboundMessage(channel="feishu", conversation_id=conversation_id, text=text)


# --------------------------------------------------------------------------- 总线


async def test_bus_runs_jobs() -> None:
    bus = MessageBus()
    await bus.start()
    done: list[str] = []

    async def job() -> None:
        done.append("ran")

    assert await bus.submit("c1", job) is True
    assert await bus.drain() is True
    await bus.stop()

    assert done == ["ran"]
    assert bus.stats.completed == 1


async def test_bus_serialises_same_conversation() -> None:
    """同一会话必须按到达顺序串行 —— 否则历史会互相踩踏。"""
    bus = MessageBus()
    await bus.start()
    order: list[int] = []

    def make(index: int) -> object:
        async def job() -> None:
            order.append(index)
            await asyncio.sleep(0.01)
            order.append(index * 10)

        return job

    for index in range(5):
        await bus.submit("same", make(index))  # type: ignore[arg-type]
    await bus.drain()
    await bus.stop()

    # 串行执行的痕迹：成对出现（index 紧跟 index*10），不会被交错
    assert order == [0, 0, 1, 10, 2, 20, 3, 30, 4, 40]


async def test_bus_runs_different_conversations_concurrently() -> None:
    bus = MessageBus()
    await bus.start()
    started: list[str] = []
    release = asyncio.Event()

    def make(name: str) -> object:
        async def job() -> None:
            started.append(name)
            await release.wait()

        return job

    await bus.submit("a", make("a"))  # type: ignore[arg-type]
    await bus.submit("b", make("b"))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    release.set()
    await bus.drain()
    await bus.stop()

    assert sorted(started) == ["a", "b"]


async def test_bus_rejects_when_queue_full() -> None:
    """积压说明这个会话被刷屏了；与其吃光内存，不如明确拒绝。"""
    bus = MessageBus(max_pending_per_conversation=1)
    await bus.start()
    release = asyncio.Event()

    async def blocking() -> None:
        await release.wait()

    assert await bus.submit("c1", blocking) is True
    assert await bus.submit("c1", blocking) is False  # 队列已满

    release.set()
    await bus.drain()
    await bus.stop()

    assert bus.stats.rejected == 1


async def test_bus_counts_failures_without_stopping() -> None:
    """单个任务失败不能拖垮整个会话的队列。"""
    bus = MessageBus()
    await bus.start()
    after: list[str] = []

    async def boom() -> None:
        raise RuntimeError("炸了")

    async def ok() -> None:
        after.append("ok")

    await bus.submit("c1", boom)
    await bus.submit("c1", ok)
    await bus.drain()
    await bus.stop()

    assert after == ["ok"]
    assert bus.stats.failed == 1
    assert bus.stats.completed == 1


async def test_bus_stop_is_idempotent() -> None:
    bus = MessageBus()
    await bus.start()
    await bus.stop()
    await bus.stop()


async def test_bus_drain_timeout() -> None:
    bus = MessageBus()
    await bus.start()
    release = asyncio.Event()

    async def blocking() -> None:
        await release.wait()

    await bus.submit("c1", blocking)
    assert await bus.drain(timeout_seconds=0.05) is False

    release.set()
    await bus.drain()
    await bus.stop()


# --------------------------------------------------------------------------- 路由


class FakeSink:
    """记录被投喂的事件流，并按脚本产出事件。"""

    name = "fake"

    def __init__(self, events: list[RunEvent] | None = None) -> None:
        self.events = events or [RunEvent.run_end("答复", step=1)]
        self.delivered: list[InboundMessage] = []
        self.sent: list[OutboundMessage] = []
        self.consumed: list[RunEvent] = []

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    async def deliver(self, message: InboundMessage, events) -> None:  # type: ignore[no-untyped-def]
        self.delivered.append(message)
        async for event in events:
            self.consumed.append(event)


class FakeRunner:
    """替代真实 AgentRunner：产出预设事件，并记录收到的请求。"""

    def __init__(self, events: list[RunEvent] | None = None) -> None:
        self.events = events or [RunEvent.run_end("答复", step=1)]
        self.requests: list[object] = []
        self.closed = False

    def astream(self, request: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        self.requests.append(request)

        async def gen():  # type: ignore[no-untyped-def]
            try:
                for event in self.events:
                    yield event
            finally:
                self.closed = True

        return gen()


@pytest.fixture
def settings(settings_factory) -> Settings:  # type: ignore[no-untyped-def]
    return settings_factory(config_yaml="agent:\n  name: RouterTest\n")


async def test_router_dispatches_without_bus(settings: Settings) -> None:
    runner = FakeRunner()
    router = MessageRouter(runner)  # type: ignore[arg-type]
    sink = FakeSink()

    accepted = await router.dispatch(inbound("你好"), sink)  # type: ignore[arg-type]

    assert accepted is True
    assert [m.text for m in sink.delivered] == ["你好"]
    assert sink.consumed  # 事件流被消费了
    assert runner.closed is True  # 生成器被关闭，不留悬空协程


async def test_router_builds_request_from_inbound(settings: Settings) -> None:
    runner = FakeRunner()
    router = MessageRouter(runner)  # type: ignore[arg-type]

    await router.dispatch(inbound("你好"), FakeSink())  # type: ignore[arg-type]

    request = runner.requests[0]
    assert request.text == "你好"  # type: ignore[attr-defined]
    assert request.conversation_id == "oc_chat"  # type: ignore[attr-defined]
    assert request.channel == "feishu"  # type: ignore[attr-defined]


async def test_router_uses_bus_and_serialises(settings: Settings) -> None:
    runner = FakeRunner()
    bus = MessageBus()
    await bus.start()
    router = MessageRouter(runner, bus=bus)  # type: ignore[arg-type]
    sink = FakeSink()

    await router.dispatch(inbound("一"), sink)  # type: ignore[arg-type]
    await router.dispatch(inbound("二"), sink)  # type: ignore[arg-type]
    await bus.drain()
    await bus.stop()

    assert [m.text for m in sink.delivered] == ["一", "二"]


async def test_router_replies_busy_when_queue_full(settings: Settings) -> None:
    """队列满时要回话告知，而不是让用户干等。"""
    runner = FakeRunner()
    bus = MessageBus(max_pending_per_conversation=1)
    await bus.start()
    router = MessageRouter(runner, bus=bus)  # type: ignore[arg-type]
    sink = FakeSink()

    # 等 worker 把第一条取走，队列才有空位 —— 否则「二」也会因为还没被取走而被拒，
    # 断言就会依赖调度时序。
    await router.dispatch(inbound("一"), sink)  # type: ignore[arg-type]
    await asyncio.sleep(0.02)
    await router.dispatch(inbound("二"), sink)  # type: ignore[arg-type]
    accepted = await router.dispatch(inbound("三"), sink)  # type: ignore[arg-type]

    await bus.drain()
    await bus.stop()

    assert accepted is False
    assert [m.text for m in sink.sent] == [BUSY_REPLY]


async def test_router_survives_busy_reply_failure(settings: Settings) -> None:
    """连「忙」都回不出去时也不能抛异常。"""
    runner = FakeRunner()
    bus = MessageBus(max_pending_per_conversation=1)
    await bus.start()
    router = MessageRouter(runner, bus=bus)  # type: ignore[arg-type]

    class BrokenSink(FakeSink):
        async def send(self, message: OutboundMessage) -> None:
            raise RuntimeError("通道挂了")

    sink = BrokenSink()
    await router.dispatch(inbound("一"), sink)  # type: ignore[arg-type]
    await router.dispatch(inbound("二"), sink)  # type: ignore[arg-type]
    assert await router.dispatch(inbound("三"), sink) is False  # type: ignore[arg-type]

    await bus.drain()
    await bus.stop()


async def test_router_closes_stream_on_sink_failure(settings: Settings) -> None:
    """sink 渲染到一半失败时也要关掉生成器，避免留下未消费的协程。"""
    runner = FakeRunner()
    router = MessageRouter(runner)  # type: ignore[arg-type]

    class ExplodingSink(FakeSink):
        async def deliver(self, message: InboundMessage, events) -> None:  # type: ignore[no-untyped-def]
            async for event in events:
                self.consumed.append(event)  # 先消费一个，让生成器真正跑起来
                raise RuntimeError("渲染失败")

    with pytest.raises(RuntimeError, match="渲染失败"):
        await router.dispatch(inbound("你好"), ExplodingSink())  # type: ignore[arg-type]

    assert runner.closed is True


async def test_router_aclose_on_untouched_stream(settings: Settings) -> None:
    """sink 一个事件都没消费就失败时，aclose() 也不能报错。"""
    runner = FakeRunner()
    router = MessageRouter(runner)  # type: ignore[arg-type]

    class NeverConsumingSink(FakeSink):
        async def deliver(self, message: InboundMessage, events) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("连事件都没读就失败了")

    with pytest.raises(RuntimeError):
        await router.dispatch(inbound("你好"), NeverConsumingSink())  # type: ignore[arg-type]
