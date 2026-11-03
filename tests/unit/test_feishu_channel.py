"""飞书客户端封装与通道：卡片生命周期、降级与幂等。

用假客户端替代 lark SDK，因此**完全离线**。这里要验证的是「我们的编排逻辑」：
什么时候发卡、什么时候更新、失败时怎么降级、重复事件怎么处理。
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from doudou.agent.approvals import ApprovalBroker, ApprovalRequest
from doudou.agent.events import RunEvent
from doudou.agent.messages import ImageInput
from doudou.channels.base import (
    InboundMessage,
    OutboundMessage,
    bind_inbound,
    unbind_inbound,
)
from doudou.channels.feishu.channel import FeishuChannel, _CardState
from doudou.channels.feishu.client import FeishuClient, FeishuError, parse_action_value
from doudou.config.loader import Settings
from tests.unit.test_image_input import PNG_BYTES, PNG_URL

# --------------------------------------------------------------------------- 假客户端


class FakeResponse:
    def __init__(
        self,
        *,
        message_id: str | None = None,
        ok: bool = True,
        code: int = 0,
        msg: str = "ok",
        raw: Any | None = None,
    ) -> None:
        self.data = SimpleNamespace(message_id=message_id) if message_id else None
        self._ok = ok
        self.code = code
        self.msg = msg
        #: 通用 ``BaseRequest`` 的原始响应；body 只在 ``raw.content`` 里。
        self.raw = raw

    def success(self) -> bool:
        return self._ok


def bot_info_response(body: dict[str, Any] | str | None = None, *, ok: bool = True) -> FakeResponse:
    """构造 ``GET /open-apis/bot/v3/info`` 的响应。"""
    if body is None:
        body = {"code": 0, "msg": "ok", "bot": {"open_id": "ou_self", "app_name": "Doudou"}}
    content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return FakeResponse(ok=ok, raw=SimpleNamespace(content=content))


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
        bot_response: FakeResponse | None = None,
    ) -> None:
        self.message = FakeMessageApi(
            create_ok=create_ok, patch_ok=patch_ok, fail_msg_types=fail_msg_types
        )
        self.im = SimpleNamespace(v1=SimpleNamespace(message=self.message))
        self.bot_response = bot_response if bot_response is not None else bot_info_response()

    def request(self, payload: Any) -> FakeResponse:  # bot info
        return self.bot_response


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
    """卡片按钮回传的 value 两种形态都要吃下（人工确认的按钮依赖它）。"""
    assert parse_action_value({"action": "approve", "call_id": "c1"}).action == "approve"
    assert parse_action_value('{"action": "reject", "call_id": "c2"}').call_id == "c2"
    assert parse_action_value("不是 JSON").action == ""
    assert parse_action_value(None).action == ""


# --------------------------------------------------------------------------- 机器人身份


def test_fetch_bot_open_id_reads_raw_content(settings: Settings) -> None:
    """回归：``bot/v3/info`` 的 body 只在 ``raw.content`` 里，字段是 ``bot.open_id``。

    之前读的是 ``response.data``（通用 ``BaseRequest`` 通道不会解析 body，恒为
    ``None``），于是「自动获取 bot_open_id」永远返回 ``None``，而且没有任何日志。
    """
    body = {"code": 0, "msg": "ok", "bot": {"open_id": "ou_real", "app_name": "Doudou"}}
    client = FeishuClient(
        settings.app.channels.feishu,
        client=FakeLarkClient(bot_response=bot_info_response(body)),
    )

    assert client.fetch_bot_open_id() == "ou_real"


def test_fetch_bot_open_id_ignores_data_field(settings: Settings) -> None:
    """``data`` 即使有内容也不采信：它不是这个接口的返回位置。"""
    response = bot_info_response({"code": 0, "bot": {"open_id": "ou_raw"}})
    response.data = {"open_id": "ou_wrong"}
    client = FeishuClient(
        settings.app.channels.feishu, client=FakeLarkClient(bot_response=response)
    )

    assert client.fetch_bot_open_id() == "ou_raw"


def test_fetch_bot_open_id_missing_field_returns_none(settings: Settings) -> None:
    """成功但没字段时返回 None（退回按名字匹配），不能抛异常。"""
    client = FeishuClient(
        settings.app.channels.feishu,
        client=FakeLarkClient(bot_response=bot_info_response({"code": 0, "bot": {}})),
    )

    assert client.fetch_bot_open_id() is None


def test_fetch_bot_open_id_handles_broken_json(settings: Settings) -> None:
    client = FeishuClient(
        settings.app.channels.feishu,
        client=FakeLarkClient(bot_response=bot_info_response("不是 JSON")),
    )

    assert client.fetch_bot_open_id() is None


def test_fetch_bot_open_id_handles_unsuccessful_response(settings: Settings) -> None:
    """典型场景：应用没加机器人能力时飞书返回 11205。"""
    client = FeishuClient(
        settings.app.channels.feishu,
        client=FakeLarkClient(
            bot_response=FakeResponse(ok=False, code=11205, msg="app do not have bot")
        ),
    )

    assert client.fetch_bot_open_id() is None


def test_fetch_bot_open_id_handles_exception(settings: Settings) -> None:
    class Exploding(FakeLarkClient):
        def request(self, payload: Any) -> FakeResponse:
            raise RuntimeError("network down")

    client = FeishuClient(settings.app.channels.feishu, client=Exploding())

    assert client.fetch_bot_open_id() is None


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


async def test_download_user_image_uses_message_resource_api(
    feishu_client: FeishuClient, fake_lark: FakeLarkClient
) -> None:
    requests: list[Any] = []

    def get(request: Any) -> Any:
        requests.append(request)
        return SimpleNamespace(success=lambda: True, code=0, file=io.BytesIO(PNG_BYTES))

    fake_lark.im.v1.message_resource = SimpleNamespace(get=get)
    image = await feishu_client.download_image("om_in", "img_key")
    assert image.data_url == PNG_URL
    assert requests[0].message_id == "om_in"
    assert requests[0].file_key == "img_key"
    assert requests[0].type == "image"


async def test_image_download_keeps_inbound_message_order(channel: FeishuChannel) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def download(*_args: Any) -> ImageInput:
        entered.set()
        await release.wait()
        return ImageInput(data_url=PNG_URL)

    channel.client.download_image = AsyncMock(side_effect=download)  # type: ignore[method-assign]
    image = replace(inbound(""), raw={"image_keys": ["img_key"]})
    first = asyncio.create_task(channel._dispatch(image))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(channel._dispatch(inbound("再问一句")))
    await asyncio.sleep(0)
    sink = channel.router
    assert isinstance(sink, RecordingSink)
    assert not sink.dispatched
    release.set()
    await asyncio.gather(first, second)
    assert [message.text for message in sink.dispatched] == ["", "再问一句"]
    assert sink.dispatched[0].images[0].data_url == PNG_URL


async def test_image_download_failure_does_not_run_text_only(
    channel: FeishuChannel, fake_lark: FakeLarkClient
) -> None:
    channel.client.download_image = AsyncMock(side_effect=ValueError("bad image"))  # type: ignore[method-assign]
    await channel._dispatch(replace(inbound("识图"), raw={"image_keys": ["img_key"]}))
    sink = channel.router
    assert isinstance(sink, RecordingSink)
    assert not sink.dispatched
    assert "图片读取失败" in request_json(fake_lark.message.created[-1])["text"]


async def test_excessive_image_count_is_reported_before_download(channel: FeishuChannel) -> None:
    download = AsyncMock()
    channel.client.download_image = download  # type: ignore[method-assign]
    await channel._dispatch(replace(inbound("识图"), raw={"image_keys": ["img_key"] * 5}))
    download.assert_not_called()


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
    state = _CardState()
    state.consume(RunEvent.tool_call(step=1, name="now", call_id="c1", args={}))
    state.consume(RunEvent.tool_result(step=1, name="now", call_id="c1", result="12:00", ok=True))

    assert len(state.tool_notes) == 1
    assert state.tool_notes[0].startswith("✓")


def test_card_state_keeps_multiple_tools() -> None:
    state = _CardState()
    for index, name in enumerate(("now", "workspace_write"), start=1):
        state.consume(RunEvent.tool_call(step=index, name=name, call_id=f"c{index}", args={}))
        state.consume(
            RunEvent.tool_result(step=index, name=name, call_id=f"c{index}", result="ok", ok=True)
        )

    assert len(state.tool_notes) == 2


def test_card_state_marks_failure() -> None:
    state = _CardState()
    state.consume(RunEvent.error("达到最大步数"))

    assert state.finished is True
    assert state.failed is True
    assert (
        "错误" in state.render("D")["header"]["title"]["content"]
        or state.render("D")["header"]["template"] == "red"
    )


def test_card_state_reports_patch_failures() -> None:
    state = _CardState()
    state.answer = "答复"
    state.finished = True
    state.patch_failures = 2

    rendered = json.dumps(state.render("D"), ensure_ascii=False)

    assert "2 次卡片更新失败" in rendered


# --------------------------------------------------------------------------- 人工确认


def _make_channel(
    settings: Settings, feishu_client: FeishuClient, broker: Any | None = None
) -> FeishuChannel:
    return FeishuChannel(
        settings,
        RecordingSink(),  # type: ignore[arg-type]
        client=feishu_client,
        runner=FakeWsRunner(),
        broker=broker,
    )


async def _await_pending(broker: Any, *, budget: float = 3.0) -> str:
    deadline = asyncio.get_running_loop().time() + budget
    while asyncio.get_running_loop().time() < deadline:
        if broker.pending:
            return str(broker.pending[0].approval_id)
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时：没有出现待决请求")


async def test_channel_takes_over_approval_notifications(
    settings: Settings, feishu_client: FeishuClient
) -> None:
    """装了中介的通道要接管「有人要确认」的通知 —— 否则飞书里只能拒绝。"""
    broker = ApprovalBroker(timeout=5.0)

    channel = _make_channel(settings, feishu_client, broker)

    assert broker.on_request is not None
    assert channel._broker is broker


async def test_approval_card_goes_to_bound_conversation(
    settings: Settings, feishu_client: FeishuClient, fake_lark: FakeLarkClient
) -> None:
    """卡片必须发到**发起这次调用的那条会话**。"""
    broker = ApprovalBroker(timeout=5.0)
    channel = _make_channel(settings, feishu_client, broker)
    token = bind_inbound(inbound("把结论写到 report.md"))
    try:
        await channel._on_approval_request(
            ApprovalRequest(
                approval_id="ap-0001-abcd",
                thread_id="t1",
                tool_name="workspace_write",
                args={"path": "report.md", "content": "结论"},
                call_id="call-1",
            )
        )
    finally:
        unbind_inbound(token)

    assert len(fake_lark.message.created) == 1
    sent = request_json(fake_lark.message.created[0])
    assert "workspace_write" in json.dumps(sent, ensure_ascii=False)
    assert "ap-0001-abcd" in json.dumps(sent, ensure_ascii=False)
    # 记下卡片 ID 才能在建完决策后把它换成结果态
    assert channel._approval_cards["ap-0001-abcd"] == "om_1"


async def test_approval_card_skipped_for_other_channels(
    settings: Settings, feishu_client: FeishuClient, fake_lark: FakeLarkClient
) -> None:
    """HTTP / CLI 发起的调用不该往飞书里推卡片 —— 那些路径各有自己的确认方式。"""
    broker = ApprovalBroker(timeout=5.0)
    channel = _make_channel(settings, feishu_client, broker)
    other = InboundMessage(channel="cli", conversation_id="local", text="写个文件")
    token = bind_inbound(other)
    try:
        await channel._on_approval_request(
            ApprovalRequest(
                approval_id="ap-1",
                thread_id=None,
                tool_name="workspace_write",
                args={},
                call_id="c",
            )
        )
    finally:
        unbind_inbound(token)

    assert fake_lark.message.created == []


async def test_approval_card_skipped_without_conversation(
    settings: Settings, feishu_client: FeishuClient, fake_lark: FakeLarkClient
) -> None:
    """不在任何会话上下文里（例如后台任务）也要安静跳过，不能崩。"""
    broker = ApprovalBroker(timeout=5.0)
    channel = _make_channel(settings, feishu_client, broker)

    await channel._on_approval_request(
        ApprovalRequest(
            approval_id="ap-1", thread_id=None, tool_name="workspace_write", args={}, call_id="c"
        )
    )

    assert fake_lark.message.created == []


async def test_card_action_approves_and_replaces_card(
    settings: Settings, feishu_client: FeishuClient, fake_lark: FakeLarkClient
) -> None:
    """点「同意」→ 等待方被唤醒 → 卡片换成结果态（按钮消失，防重复点击）。"""
    broker = ApprovalBroker(timeout=5.0)
    channel = _make_channel(settings, feishu_client, broker)
    channel._main_loop = asyncio.get_running_loop()

    waiter = asyncio.create_task(
        broker.request(tool_name="workspace_write", args={"path": "a.md"}, call_id="c1")
    )
    approval_id = await _await_pending(broker)
    channel._approval_cards[approval_id] = "om_card"

    channel._on_card_action(_click(approval_id, "approve"))

    outcome = await asyncio.wait_for(waiter, timeout=3)
    assert outcome.approved is True

    assert len(fake_lark.message.patched) == 1
    patched = json.dumps(request_json(fake_lark.message.patched[0]), ensure_ascii=False)
    assert "已同意执行" in patched
    assert "button" not in patched


async def test_card_action_rejects(
    settings: Settings, feishu_client: FeishuClient, fake_lark: FakeLarkClient
) -> None:
    broker = ApprovalBroker(timeout=5.0)
    channel = _make_channel(settings, feishu_client, broker)
    channel._main_loop = asyncio.get_running_loop()

    waiter = asyncio.create_task(broker.request(tool_name="workspace_write", args={}, call_id="c1"))
    approval_id = await _await_pending(broker)

    channel._on_card_action(_click(approval_id, "reject"))

    outcome = await asyncio.wait_for(waiter, timeout=3)
    assert outcome.approved is False


def _click(approval_id: str, action: str) -> Any:
    """造一个 lark 风格的卡片回调对象。"""
    return SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value={"action": action, "approval_id": approval_id})
        )
    )


def test_card_action_ignores_foreign_buttons(
    settings: Settings, feishu_client: FeishuClient
) -> None:
    """别的卡片（或老按钮）点了不该影响我们，也不该抛异常。"""
    channel = _make_channel(settings, feishu_client, ApprovalBroker(timeout=5.0))

    for payload in (
        None,
        SimpleNamespace(),
        SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value=None))),
        SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"action": "unknown"}))),
        SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"action": "approve"}))),
    ):
        channel._on_card_action(payload)


async def test_card_action_on_unknown_approval_does_not_crash(
    settings: Settings, feishu_client: FeishuClient, fake_lark: FakeLarkClient
) -> None:
    """超时之后才点的按钮 / 重复点击：安静收场，不崩、不误改卡片。"""
    broker = ApprovalBroker(timeout=5.0)
    channel = _make_channel(settings, feishu_client, broker)

    await channel._handle_card_action("ap-gone", True)

    assert fake_lark.message.patched == []
