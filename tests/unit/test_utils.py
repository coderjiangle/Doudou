"""通用工具：文本处理与异步重试。"""

from __future__ import annotations

import pytest

from doudou.utils.async_utils import retry_async
from doudou.utils.text import content_to_text, html_to_text, stringify_tool_result, truncate

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
