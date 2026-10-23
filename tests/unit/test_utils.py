"""通用工具：文本处理与异步重试。"""

from __future__ import annotations

import pytest

from doudou.utils.async_utils import retry_async
from doudou.utils.text import (
    content_to_text,
    html_to_text,
    looks_like_content_blocks,
    stringify_tool_result,
    truncate,
)

# --------------------------------------------------------------------------- html_to_text


def test_html_to_text_strips_tags() -> None:
    text = html_to_text("<h1>标题</h1><p>第一段</p><p>第二段</p>")

    assert text == "标题\n第一段\n第二段"


def test_html_to_text_drops_script_and_style() -> None:
    html = "<style>p{color:red}</style><p>正文</p><script>alert(1)</script>"

    text = html_to_text(html)

    assert text == "正文"


def test_html_to_text_decodes_entities() -> None:
    assert "a<b&c" in html_to_text("<p>a&lt;b&amp;c</p>")


def test_html_to_text_collapses_whitespace_within_lines() -> None:
    text = html_to_text("<p>前    后</p>")

    assert text == "前 后"


def test_html_to_text_keeps_blank_lines_out() -> None:
    assert "\n\n" not in html_to_text("<p>a</p><div></div><p>b</p>")


def test_html_to_text_respects_limit() -> None:
    text = html_to_text(f"<p>{'x' * 500}</p>", limit=100)

    assert "已截断" in text
    assert len(text) < 200


def test_html_to_text_on_plain_text() -> None:
    assert html_to_text("就是普通文本") == "就是普通文本"


# --------------------------------------------------------------------------- 其它文本工具


def test_truncate_noop_when_short() -> None:
    assert truncate("短", 10) == "短"
    assert truncate("短", 0) == "短"


def test_truncate_reports_dropped_length() -> None:
    assert "已截断 5 字" in truncate("x" * 10, 5)


def test_content_to_text_handles_blocks() -> None:
    assert content_to_text([{"type": "text", "text": "a"}, {"type": "image"}]) == "a"
    assert content_to_text(None) == ""
    assert content_to_text("直接") == "直接"


def test_stringify_tool_result_serialises_objects() -> None:
    assert stringify_tool_result({"a": 1}) == '{"a": 1}'
    assert stringify_tool_result("已经是字符串") == "已经是字符串"
    assert "已截断" in stringify_tool_result("y" * 100, limit=10)


# --------------------------------------------------------------------------- 内容块拍平


def test_stringify_content_blocks_flattens_text() -> None:
    """MCP 工具返回的是内容块；模型应该看到纯文本而不是这层包装。"""
    blocks = [{"type": "text", "text": "echo: 你好", "id": "lc_123"}]

    assert stringify_tool_result(blocks) == "echo: 你好"


def test_stringify_content_blocks_joins_multiple() -> None:
    blocks = [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]

    assert stringify_tool_result(blocks) == "第一段\n\n第二段"


def test_stringify_content_blocks_marks_non_text() -> None:
    """直接丢掉图片块会让模型完全察觉不到「这里本来有内容」。"""
    blocks = [{"type": "image", "base64": "..."}, {"type": "text", "text": "配文"}]

    flattened = stringify_tool_result(blocks)

    assert "配文" in flattened
    assert "图片" in flattened


def test_stringify_data_with_unknown_type_stays_json() -> None:
    """判据用白名单：普通 API 数据不能被误判成内容块而改写。"""
    data = [{"type": "invoice", "amount": 100}]

    assert stringify_tool_result(data) == '[{"type": "invoice", "amount": 100}]'


def test_stringify_data_without_type_stays_json() -> None:
    data = [{"name": "a", "score": 1}]

    assert stringify_tool_result(data) == '[{"name": "a", "score": 1}]'


def test_blocks_to_text_without_placeholders_drops_non_text() -> None:
    """``content_to_text`` 服务于模型输出的增量文本，那里没有图片。"""
    assert content_to_text([{"type": "image"}, {"type": "text", "text": "x"}]) == "x"


def test_looks_like_content_blocks_edges() -> None:
    assert looks_like_content_blocks([{"type": "text"}]) is True
    assert looks_like_content_blocks([{"type": "image"}]) is True
    assert looks_like_content_blocks([]) is False
    assert looks_like_content_blocks("str") is False
    assert looks_like_content_blocks([{"no_type": 1}]) is False
    assert looks_like_content_blocks([{"type": "a"}, "mixed"]) is False
    # 未知 type 一律不认——避免把普通数据当内容块改写
    assert looks_like_content_blocks([{"type": "invoice"}]) is False


# --------------------------------------------------------------------------- retry_async


async def test_retry_returns_first_success() -> None:
    calls: list[int] = []

    async def ok() -> str:
        calls.append(1)
        return "done"

    assert await retry_async(ok, label="test") == "done"
    assert len(calls) == 1


async def test_retry_recovers_after_transient_failures() -> None:
    calls: list[int] = []

    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("暂时失败")
        return "ok"

    assert await retry_async(flaky, attempts=3, base_delay=0.01, label="test") == "ok"
    assert len(calls) == 3


async def test_retry_reraises_after_exhausting_attempts() -> None:
    calls: list[int] = []

    async def always_fail() -> str:
        calls.append(1)
        raise ConnectionError("一直失败")

    with pytest.raises(ConnectionError, match="一直失败"):
        await retry_async(always_fail, attempts=2, base_delay=0.01, label="test")

    assert len(calls) == 2


async def test_retry_does_not_retry_unlisted_exceptions() -> None:
    """``retry_on`` 之外的异常必须立刻抛出，不能把「参数错误」重试三次。"""
    calls: list[int] = []

    async def bad_request() -> str:
        calls.append(1)
        raise ValueError("参数不对")

    with pytest.raises(ValueError, match="参数不对"):
        await retry_async(
            bad_request, attempts=3, base_delay=0.01, retry_on=(ConnectionError,), label="test"
        )

    assert len(calls) == 1
