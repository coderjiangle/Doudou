"""飞书事件解析、幂等去重、内容解析与卡片构建。"""

from __future__ import annotations

import json

import pytest

from doudou.channels.feishu.cards import (
    MAX_CARD_BYTES,
    MAX_CARD_CHARS,
    answer_card,
    error_card,
    thinking_card,
    tool_note,
    tool_running_note,
)
from doudou.channels.feishu.events import (
    EventDedup,
    message_allowed,
    parse_message_event,
    strip_mention_keys,
)
from doudou.channels.feishu.schemas import parse_content
from doudou.config.loader import Settings

# --------------------------------------------------------------------------- 事件构造


def make_event(
    *,
    event_id: str = "evt_1",
    chat_id: str = "oc_chat",
    chat_type: str = "p2p",
    message_type: str = "text",
    text: str = "你好",
    content: str | None = None,
    sender_type: str = "user",
    open_id: str = "ou_user",
    mentions: list[dict] | None = None,
    thread_id: str | None = None,
) -> dict:
    """构造一个与 lark ``P2ImMessageReceiveV1`` 同构的原始载荷。

    测试直接用 dict（而不是 lark 对象）：``parse_message_event`` 是鸭子类型的，
    这样断言更直观，也不受 SDK 版本影响。
    """
    message: dict = {
        "message_id": "om_1",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": message_type,
        "content": content if content is not None else json.dumps({"text": text}),
        "create_time": "1700000000000",
    }
    if mentions:
        message["mentions"] = mentions
    if thread_id:
        message["thread_id"] = thread_id
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": sender_type},
            "message": message,
        },
    }


@pytest.fixture
def feishu_settings(settings_factory) -> Settings:  # type: ignore[no-untyped-def]
    """带飞书通道配置的 Settings。"""

    def make(*, extra: str = "") -> Settings:
        yaml = (
            "channels:\n"
            "  feishu:\n"
            "    enabled: true\n"
            "    mode: websocket\n"
            "    bot_name: Doudou\n"
            '    bot_open_id: "ou_bot"\n'
            f"{extra}"
        )
        return settings_factory(config_yaml=yaml)

    return make


def feishu_config(settings: Settings):  # type: ignore[no-untyped-def]
    return settings.app.channels.feishu


# --------------------------------------------------------------------------- 内容解析


def test_parse_text_content() -> None:
    assert parse_content("text", json.dumps({"text": "  你好  "})).text == "你好"


def test_parse_content_tolerates_broken_json() -> None:
    """解析不了就当纯文本用 —— 宁可原文交给模型，也不要丢掉用户说的话。"""
    assert parse_content("text", "不是 JSON").text == "不是 JSON"


def test_parse_content_handles_none() -> None:
    assert parse_content("text", None).text == ""


def test_parse_post_content_flattens_and_keeps_links() -> None:
    raw = json.dumps(
        {
            "title": "标题",
            "content": [
                [{"tag": "text", "text": "第一段"}, {"tag": "at", "user_id": "ou_x"}],
                [{"tag": "a", "text": "链接", "href": "https://example.com/a"}],
            ],
        }
    )

    parsed = parse_content("post", raw)

    assert "标题" in parsed.text
    assert "第一段" in parsed.text
    assert "@ou_x" in parsed.text
    assert parsed.links == ["https://example.com/a"]


def test_parse_content_unsupported_type() -> None:
    assert parse_content("image", json.dumps({"image_key": "img"})).text == ""


# --------------------------------------------------------------------------- 事件解析


def test_parse_p2p_text(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings())

    parsed = parse_message_event(make_event(text="现在几点"), config)

    assert parsed.ok
    message = parsed.message
    assert message is not None
    assert message.channel == "feishu"
    assert message.conversation_id == "oc_chat"
    assert message.text == "现在几点"
    assert message.user_id == "ou_user"
    assert message.message_id == "om_1"
    # 默认按群共享上下文 → 不带 thread_key
    assert message.thread_key is None
    assert message.raw["chat_type"] == "p2p"


def test_parse_group_requires_mention(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings())

    parsed = parse_message_event(make_event(chat_type="group", text="随便说说"), config)

    assert not parsed.ok
    assert parsed.skip is not None
    assert parsed.skip.code == "not_mentioned"


def test_parse_group_with_mention(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings())
    mentions = [
        {"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "Doudou", "mentioned_type": "bot"}
    ]

    parsed = parse_message_event(
        make_event(
            chat_type="group",
            text="@_user_1 帮我查一下",
            mentions=mentions,
        ),
        config,
    )

    assert parsed.ok
    assert parsed.message is not None
    # @ 占位符必须从正文里去掉，模型不需要看到 "@_user_1"
    assert parsed.message.text == "帮我查一下"


def test_group_mention_detected_by_open_id(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    """``mentioned_type`` 缺失时按配置的 bot_open_id 兜底。"""
    config = feishu_config(feishu_settings())
    mentions = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "别的名字"}]

    parsed = parse_message_event(
        make_event(chat_type="group", text="@_user_1 在吗", mentions=mentions), config
    )

    assert parsed.ok


def test_group_mention_detected_by_name(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings())
    mentions = [{"key": "@_user_1", "id": {"open_id": "ou_other"}, "name": "doudou"}]

    parsed = parse_message_event(
        make_event(chat_type="group", text="@_user_1 在吗", mentions=mentions), config
    )

    assert parsed.ok


def test_group_require_at_can_be_disabled(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings(extra="    group_require_at: false\n"))

    parsed = parse_message_event(make_event(chat_type="group", text="不用 @"), config)

    assert parsed.ok


def test_isolate_by_thread(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings(extra="    isolate_by_thread: true\n"))

    parsed = parse_message_event(make_event(thread_id="thr_1"), config)

    assert parsed.ok
    assert parsed.message is not None
    assert parsed.message.thread_key == "thr_1"
    assert parsed.message.session_key == "oc_chat#thr_1"


def test_message_from_bot_is_dropped(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    """自己发出的卡片会回流；不丢掉就会自问自答形成死循环。"""
    config = feishu_config(feishu_settings())

    parsed = parse_message_event(make_event(sender_type="app"), config)

    assert not parsed.ok
    assert parsed.skip is not None
    assert parsed.skip.code == "from_bot"


def test_unsupported_message_type_is_skipped(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings())

    parsed = parse_message_event(
        make_event(message_type="image", content=json.dumps({"image_key": "k"})), config
    )

    assert not parsed.ok
    assert parsed.skip is not None
    assert parsed.skip.code == "unsupported_type"


def test_empty_text_is_skipped(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings())

    parsed = parse_message_event(make_event(text="   "), config)

    assert not parsed.ok
    assert parsed.skip is not None
    assert parsed.skip.code == "empty_text"


def test_event_without_message_is_skipped(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings())

    parsed = parse_message_event({"event": {"sender": {}}}, config)

    assert not parsed.ok
    assert parsed.skip is not None
    assert parsed.skip.code == "no_message"


# --------------------------------------------------------------------------- 白名单


def test_allowed_chats_whitelist() -> None:
    from doudou.config.schema import FeishuChannelConfig

    config = FeishuChannelConfig(allowed_chats=["oc_ok"])
    assert message_allowed("oc_ok", "ou_1", config) is True
    assert message_allowed("oc_other", "ou_1", config) is False


def test_allowed_users_whitelist() -> None:
    from doudou.config.schema import FeishuChannelConfig

    config = FeishuChannelConfig(allowed_users=["ou_ok"])
    assert message_allowed("oc_1", "ou_ok", config) is True
    assert message_allowed("oc_1", "ou_other", config) is False
    assert message_allowed("oc_1", None, config) is False


def test_empty_whitelist_allows_everything(feishu_settings) -> None:  # type: ignore[no-untyped-def]
    config = feishu_config(feishu_settings())

    parsed = parse_message_event(make_event(chat_id="oc_any"), config)

    assert parsed.ok


def test_strip_mention_keys() -> None:
    mentions = [{"key": "@_user_1"}, {"key": "@_user_2"}]

    assert strip_mention_keys("@_user_1 你好 @_user_2", mentions).strip() == "你好"


# --------------------------------------------------------------------------- 幂等


def test_dedup_reports_duplicates() -> None:
    dedup = EventDedup(ttl_seconds=60)

    assert dedup.check_and_mark("evt_1") is True
    assert dedup.check_and_mark("evt_1") is False
    assert dedup.check_and_mark("evt_2") is True


def test_dedup_ignores_missing_event_id() -> None:
    """没有 event_id 就无法判重，一律放行（丢掉真实消息比重复更糟）。"""
    dedup = EventDedup()

    assert dedup.check_and_mark(None) is True
    assert dedup.check_and_mark(None) is True


def test_dedup_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    dedup = EventDedup(ttl_seconds=10)
    now = [1000.0]
    monkeypatch.setattr("doudou.channels.feishu.events.time.monotonic", lambda: now[0])

    dedup.remember("evt_1")
    now[0] += 5
    assert dedup.seen("evt_1") is True

    now[0] += 11  # 超过 TTL
    assert dedup.seen("evt_1") is False


def test_dedup_bounds_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """长时间运行不能把内存吃光：容量上限按插入顺序淘汰最旧的。"""
    dedup = EventDedup(ttl_seconds=10_000, max_entries=3)

    for index in range(5):
        dedup.remember(f"evt_{index}")

    assert len(dedup) == 3
    assert dedup.seen("evt_0") is False
    assert dedup.seen("evt_4") is True


# --------------------------------------------------------------------------- 卡片


def test_thinking_card_shows_pending_without_echoing_question() -> None:
    card = thinking_card(bot_name="Doudou")

    assert "正在思考" in json.dumps(card, ensure_ascii=False)
    # 占位卡片只有一行提示：不再把用户原话抄一遍（卡片就紧跟在那条消息下面）
    assert [element["tag"] for element in card["elements"]] == ["markdown"]


def test_answer_card_includes_tool_notes() -> None:
    card = answer_card(
        bot_name="Doudou",
        answer="X 是 Y",
        tool_notes=["✓ web_search — 找到了 3 条"],
    )
    rendered = json.dumps(card, ensure_ascii=False)

    assert "X 是 Y" in rendered
    assert "web_search" in rendered
    # 让用户看到「查了什么」是检索类回答建立信任的关键
    assert "note" in rendered


def test_answer_card_does_not_echo_question() -> None:
    """回归：回复卡片只放答复，不能把用户消息再打印一遍。"""
    card = answer_card(bot_name="D", answer="上午好，豆豆")

    body = [element for element in card["elements"] if element["tag"] == "markdown"]
    assert [element["content"] for element in body] == ["上午好，豆豆"]


def test_answer_card_truncates_long_body() -> None:
    card = answer_card(bot_name="D", answer="字" * (MAX_CARD_CHARS + 500))
    rendered = json.dumps(card, ensure_ascii=False)

    assert "已截断" in rendered
    # 只留一处提示，不要「（已截断 N 字）」叠加「内容过长已截断」
    assert rendered.count("已截断") == 1


def test_answer_card_respects_feishu_byte_limit() -> None:
    """飞书限制的是 30 KB 体积，不是字符数：4 字节的 emoji 会先撞上限。"""
    card = answer_card(bot_name="D", answer="😀" * (MAX_CARD_CHARS + 1000))
    rendered = json.dumps(card, ensure_ascii=False)

    assert len(rendered.encode("utf-8")) <= MAX_CARD_BYTES
    assert "已截断" in rendered
    # 字节先撑爆，所以实际保留的字符数比字符上限更少
    assert len(rendered) < MAX_CARD_CHARS


def test_error_card_marks_failure() -> None:
    card = error_card(bot_name="D", message="跑挂了")

    assert card["header"]["template"] == "red"
    assert "跑挂了" in json.dumps(card, ensure_ascii=False)


def test_answer_card_handles_empty_answer() -> None:
    card = answer_card(bot_name="D", answer="   ")

    assert "没有给出内容" in json.dumps(card, ensure_ascii=False)


def test_tool_note_marks_success_and_failure() -> None:
    assert tool_note("now", ok=True, result="2026").startswith("✓")
    assert tool_note("now", ok=False, result="超时").startswith("✗")


def test_tool_running_note_renders_args() -> None:
    note = tool_running_note("workspace_write", {"path": "report.md"})

    assert "正在调用 workspace_write" in note
    assert "report.md" in note
