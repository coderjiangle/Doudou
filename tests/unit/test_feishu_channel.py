"""飞书客户端封装与通道：卡片生命周期、降级与幂等。

用假客户端替代 lark SDK，因此**完全离线**。这里要验证的是「我们的编排逻辑」：
什么时候发卡、什么时候更新、失败时怎么降级、重复事件怎么处理。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from doudou.agent.events import RunEvent
from doudou.channels.base import InboundMessage, OutboundMessage
from doudou.channels.feishu.channel import FeishuChannel, _CardState
from doudou.channels.feishu.client import FeishuClient, FeishuError, parse_action_value
from doudou.config.loader import Settings

# --------------------------------------------------------------------------- 假客户端


class FakeResponse:
    def __init__(
        self, *, message_id: str | None = None, ok: bool = True, code: int = 0, msg: str = "ok"
    ) -> None:
        self.data = SimpleNamespace(message_id=message_id) if message_id else None
        self._ok = ok
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self._ok


class FakeMessageApi:
    """记录调用，并可按 msg_type 注入失败。"""

    def __init__(
        self,
        *,
        create_ok: bool = True,
        patch_ok: bool = True,
        fail_msg_types: tuple[str, ...] = (),
    ) -> None:
        self.created: list[Any] = []
        self.patched: list[Any] = []
        self.create_ok = create_ok
        self.patch_ok = patch_ok
        self.fail_msg_types = set(fail_msg_types)

    def create(self, request: Any) -> FakeResponse:
        self.created.append(request)
        msg_type = getattr(request.request_body, "msg_type", "")
        if not self.create_ok or msg_type in self.fail_msg_types:
            return FakeResponse(ok=False, code=99, msg="send failed")
        return FakeResponse(message_id=f"om_{len(self.created)}")

    def patch(self, request: Any) -> FakeResponse:
        self.patched.append(request)
        if not self.patch_ok:
            return FakeResponse(ok=False, code=98, msg="patch failed")
        return FakeResponse()


class FakeLarkClient:
    def __init__(
        self,
        *,
        create_ok: bool = True,
        patch_ok: bool = True,
        fail_msg_types: tuple[str, ...] = (),
    ) -> None:
        self.message = FakeMessageApi(
            create_ok=create_ok, patch_ok=patch_ok, fail_msg_types=fail_msg_types
        )
        self.im = SimpleNamespace(v1=SimpleNamespace(message=self.message))

    def request(self, payload: Any) -> FakeResponse:  # bot info
        return FakeResponse()


class FakeWsRunner:
    """替代真实长连接线程。"""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.running = False
        self.last_error: BaseException | None = None

    def start(self) -> None:
        self.started = True
        self.running = True

    def stop(self, *, timeout: float = 5.0) -> None:
        self.stopped = True
        self.running = False


def request_json(request: Any) -> dict[str, Any]:
    """把 lark 请求对象里的 content 解出来。"""
    return json.loads(request.request_body.content)


def request_field(request: Any, name: str) -> Any:
    return getattr(request.request_body, name, None)


# --------------------------------------------------------------------------- 夹具


@pytest.fixture
def settings(settings_factory) -> Settings:  # type: ignore[no-untyped-def]
    return settings_factory(
        config_yaml=(
            "channels:\n"
            "  feishu:\n"
            "    enabled: true\n"
            "    mode: websocket\n"
            "    bot_name: Doudou\n"
            '    bot_open_id: "ou_bot"\n'
            '    app_id: "cli_test"\n'
            '    app_secret: "secret_test"\n'
            "    card_update_interval_ms: 0\n"
        )
    )


@pytest.fixture
def fake_lark() -> FakeLarkClient:
    return FakeLarkClient()


@pytest.fixture
def feishu_client(settings: Settings, fake_lark: FakeLarkClient) -> FeishuClient:
    return FeishuClient(settings.app.channels.feishu, client=fake_lark)


class RecordingSink:
    """假的 router：只记录被 dispatch 的消息。"""

    def __init__(self) -> None:
        self.dispatched: list[InboundMessage] = []
        self.reject = False

    async def dispatch(self, message: InboundMessage, sink: Any) -> bool:
        self.dispatched.append(message)
        return not self.reject


@pytest.fixture
def channel(settings: Settings, feishu_client: FeishuClient) -> FeishuChannel:
    sink = RecordingSink()
    channel = FeishuChannel(settings, sink, client=feishu_client, runner=FakeWsRunner())  # type: ignore[arg-type]
    channel.router = sink  # type: ignore[assignment]
    return channel


def inbound(text: str = "你好") -> InboundMessage:
    return InboundMessage(
        channel="feishu",
        conversation_id="oc_chat",
        text=text,
        user_id="ou_user",
        message_id="om_in",
    )


async def stream(*events: RunEvent):  # type: ignore[no-untyped-def]
    for event in events:
        yield event


# --------------------------------------------------------------------------- 客户端


def test_send_card_returns_message_id(
    feishu_client: FeishuClient, fake_lark: FakeLarkClient
) -> None:
    message_id = feishu_client.send_card("oc_chat", {"config": {}})

    assert message_id == "om_1"
    request = fake_lark.message.created[0]
    assert request_field(request, "receive_id") == "oc_chat"
    assert request_field(request, "msg_type") == "interactive"
    assert request_json(request) == {"config": {}}


def test_send_text_uses_text_type(feishu_client: FeishuClient, fake_lark: FakeLarkClient) -> None:
    feishu_client.send_text("oc_chat", "排队中")

    request = fake_lark.message.created[0]
    assert request_field(request, "msg_type") == "text"
    assert request_json(request) == {"text": "排队中"}


def test_send_failure_raises(settings: Settings) -> None:
    """lark SDK 不抛异常，只把错误码放在响应里 —— 必须显式检查。"""
    client = FeishuClient(settings.app.channels.feishu, client=FakeLarkClient(create_ok=False))

    with pytest.raises(FeishuError, match="发送消息失败"):
        client.send_card("oc_chat", {})


def test_patch_failure_returns_false(settings: Settings) -> None:
    """更新卡片失败不算致命：只告警，让调用方决定是否降级。"""
    client = FeishuClient(settings.app.channels.feishu, client=FakeLarkClient(patch_ok=False))

    assert client.patch_card("om_1", {}) is False


def test_patch_success(feishu_client: FeishuClient, fake_lark: FakeLarkClient) -> None:
    assert feishu_client.patch_card("om_1", {"elements": []}) is True
    assert request_json(fake_lark.message.patched[0]) == {"elements": []}


def test_parse_action_value_accepts_dict_and_json_string() -> None:
    """卡片按钮回传的 value 两种形态都要吃下（M8 的确认按钮依赖它）。"""
    assert parse_action_value({"action": "approve", "call_id": "c1"}).action == "approve"
    assert parse_action_value('{"action": "reject", "call_id": "c2"}').call_id == "c2"
    assert parse_action_value("不是 JSON").action == ""
    assert parse_action_value(None).action == ""


# --------------------------------------------------------------------------- 通道生命周期


async def test_start_requires_credentials(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """缺凭据必须明确报错，而不是安静地连不上。"""
    bare = settings_factory(
        config_yaml="channels:\n  feishu:\n    enabled: true\n    mode: websocket\n"
    )
    channel = FeishuChannel(bare, RecordingSink(), runner=FakeWsRunner())  # type: ignore[arg-type]

    with pytest.raises(FeishuError, match="缺少凭据"):
        await channel.start()


async def test_start_and_stop(channel: FeishuChannel) -> None:
    await channel.start()
    assert channel._ws is not None
    assert channel._ws.started is True

    await channel.stop()
    assert channel._ws is not None
    assert channel._ws.stopped is True


async def test_send_forwards_to_client(channel: FeishuChannel, fake_lark: FakeLarkClient) -> None:
    await channel.send(OutboundMessage(conversation_id="oc_chat", text="排队中"))

    assert request_json(fake_lark.message.created[0]) == {"text": "排队中"}


# --------------------------------------------------------------------------- 入站（WS 线程回调）


def make_raw_event(
    *, event_id: str = "evt_1", text: str = "你好", sender_type: str = "user"
) -> Any:
    """构造带 ``header`` 的假事件对象。"""
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event={
            "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": sender_type},
            "message": {
                "message_id": "om_in",
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    )


async def test_on_event_dispatches_in_main_loop(channel: FeishuChannel) -> None:
    """事件从 WS 线程进来，必须被转交到主事件循环执行。"""
    await channel.start()
    channel._on_event(make_raw_event())

    # 等 run_coroutine_threadsafe 投递的协程跑完
    await asyncio.sleep(0.05)
    await channel.stop()

    sink = channel.router
    assert isinstance(sink, RecordingSink)
    assert [m.text for m in sink.dispatched] == ["你好"]


async def test_on_event_deduplicates(channel: FeishuChannel) -> None:
    """飞书重连时可能重推同一事件，不能被回答两次。"""
    await channel.start()
    for _ in range(3):
        channel._on_event(make_raw_event(event_id="evt_same"))
    await asyncio.sleep(0.05)
    await channel.stop()

    sink = channel.router
    assert isinstance(sink, RecordingSink)
    assert len(sink.dispatched) == 1


async def test_on_event_drops_bot_messages(channel: FeishuChannel) -> None:
    await channel.start()
    channel._on_event(make_raw_event(sender_type="app"))
    await asyncio.sleep(0.05)
    await channel.stop()

    sink = channel.router
    assert isinstance(sink, RecordingSink)
    assert sink.dispatched == []


def test_on_event_never_raises(channel: FeishuChannel) -> None:
    """回调里抛异常会让 lark 触发重推，所以必须自己吞掉。"""
    channel._on_event(object())
    channel._on_event(None)


# --------------------------------------------------------------------------- 出站渲染


async def test_deliver_sends_then_patches_card(
    channel: FeishuChannel, fake_lark: FakeLarkClient
) -> None:
    await channel.deliver(
        inbound("现在几点"),
        stream(
            RunEvent.step_start(1),
            RunEvent.text_delta("正在", step=1),
            RunEvent.text_delta("查询", step=1),
            RunEvent.run_end("现在是 12:00", step=2),
        ),
    )

    # 1 次建卡 + 至少 1 次终态更新
    assert len(fake_lark.message.created) == 1
    assert fake_lark.message.patched
    final = request_json(fake_lark.message.patched[-1])
    assert "现在是 12:00" in json.dumps(final, ensure_ascii=False)


async def test_deliver_shows_tool_notes(channel: FeishuChannel, fake_lark: FakeLarkClient) -> None:
    await channel.deliver(
        inbound("算一下"),
        stream(
            RunEvent.tool_call(step=1, name="now", call_id="c1", args={}),
            RunEvent.tool_result(step=1, name="now", call_id="c1", result="12:00", ok=True),
            RunEvent.run_end("现在 12:00", step=2),
        ),
    )

    rendered = json.dumps(request_json(fake_lark.message.patched[-1]), ensure_ascii=False)
    assert "now" in rendered
    assert "✓" in rendered
    # 结果回来后「正在调用」痕迹应被替换，而不是留两行
    assert "正在调用" not in rendered


async def test_deliver_falls_back_to_text_when_card_fails(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """卡片发不出去时必须兜底发文本：用户一定要能看到结果。

    这里只让 ``interactive`` 失败，文本通道正常 —— 这正是「卡片权限不足」
    这类线上情况的形态。
    """
    settings = settings_factory(
        config_yaml=("channels:\n  feishu:\n    enabled: true\n    bot_open_id: ou_bot\n")
    )
    fake = FakeLarkClient(fail_msg_types=("interactive",))
    channel = FeishuChannel(
        settings,
        RecordingSink(),  # type: ignore[arg-type]
        client=FeishuClient(settings.app.channels.feishu, client=fake),
        runner=FakeWsRunner(),
    )

    await channel.deliver(inbound("你好"), stream(RunEvent.run_end("答复", step=1)))

    # 没有卡片可更新，因此不应有 patch
    assert fake.message.patched == []
    # 兜底的文本消息必须发出去
    assert len(fake.message.created) == 2
    assert request_json(fake.message.created[-1]) == {"text": "答复"}


async def test_deliver_falls_back_when_final_patch_fails(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=("channels:\n  feishu:\n    enabled: true\n    bot_open_id: ou_bot\n")
    )
    fake = FakeLarkClient(patch_ok=False)
    channel = FeishuChannel(
        settings,
        RecordingSink(),  # type: ignore[arg-type]
        client=FeishuClient(settings.app.channels.feishu, client=fake),
        runner=FakeWsRunner(),
    )

    await channel.deliver(inbound("你好"), stream(RunEvent.run_end("答复", step=1)))

    # 建卡 1 次 + patch 失败后补发 1 条文本
    assert len(fake.message.created) == 2
    assert request_json(fake.message.created[-1]) == {"text": "答复"}


async def test_deliver_text_mode_skips_card(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=(
            "channels:\n"
            "  feishu:\n"
            "    enabled: true\n"
            "    bot_open_id: ou_bot\n"
            "    reply_mode: text\n"
        )
    )
    fake = FakeLarkClient()
    channel = FeishuChannel(
        settings,
        RecordingSink(),  # type: ignore[arg-type]
        client=FeishuClient(settings.app.channels.feishu, client=fake),
        runner=FakeWsRunner(),
    )

    await channel.deliver(inbound("你好"), stream(RunEvent.run_end("答复", step=1)))

    assert len(fake.message.created) == 1
    assert request_json(fake.message.created[0]) == {"text": "答复"}


# --------------------------------------------------------------------------- 卡片状态机


def test_card_state_replaces_tool_note() -> None:
    state = _CardState(question="q")
    state.consume(RunEvent.tool_call(step=1, name="now", call_id="c1", args={}))
    state.consume(RunEvent.tool_result(step=1, name="now", call_id="c1", result="12:00", ok=True))

    assert len(state.tool_notes) == 1
    assert state.tool_notes[0].startswith("✓")


def test_card_state_keeps_multiple_tools() -> None:
    state = _CardState(question="q")
    for index, name in enumerate(("now", "days_between"), start=1):
        state.consume(RunEvent.tool_call(step=index, name=name, call_id=f"c{index}", args={}))
        state.consume(
            RunEvent.tool_result(step=index, name=name, call_id=f"c{index}", result="ok", ok=True)
        )

    assert len(state.tool_notes) == 2


def test_card_state_marks_failure() -> None:
    state = _CardState(question="q")
    state.consume(RunEvent.error("达到最大步数"))

    assert state.finished is True
    assert state.failed is True
    assert (
        "错误" in state.render("D")["header"]["title"]["content"]
        or state.render("D")["header"]["template"] == "red"
    )


def test_card_state_reports_patch_failures() -> None:
    state = _CardState(question="q")
    state.answer = "答复"
    state.finished = True
    state.patch_failures = 2

    rendered = json.dumps(state.render("D"), ensure_ascii=False)

    assert "2 次卡片更新失败" in rendered
