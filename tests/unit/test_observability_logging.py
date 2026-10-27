"""日志配置：噪音压制与入参脱敏。

这两条都是 AGENTS.md 的硬性约定：
「关键位置必须打印日志，包括出入参，敏感信息请模糊化处理」。
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from doudou.agent.react import ReactAgent
from doudou.agent.testing import ScriptedChatModel, tool_call
from doudou.observability.logging import configure_logging, get_logger, reset_logging


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """把日志输出重定向到内存，结束后还原。"""
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    yield stream
    reset_logging()


# --------------------------------------------------------------------------- 噪音压制


def test_noisy_logger_variants_are_suppressed(captured: io.StringIO) -> None:
    """实测 openai SDK 走的 httpx 变体 logger 叫 ``httpx2``；前缀匹配必须覆盖它。"""
    logging.getLogger("httpx2").info('HTTP Request: POST https://example.test "200 OK"')
    logging.getLogger("httpx").info("HTTP Request: POST https://example.test")
    logging.getLogger("httpcore").debug("connect_tcp.started")

    assert "HTTP Request" not in captured.getvalue()


def test_noisy_loggers_still_emit_warnings(captured: io.StringIO) -> None:
    """压制只针对低于 WARNING 的记录，真正的告警不能丢。"""
    logging.getLogger("httpx2").warning("连接被重置")

    assert "连接被重置" in captured.getvalue()


def test_own_loggers_are_not_suppressed(captured: io.StringIO) -> None:
    get_logger("doudou.probe").info("业务日志应当可见")

    assert "业务日志应当可见" in captured.getvalue()


# --------------------------------------------------------------------------- 时间戳时区


def _timestamp_of(line: str) -> datetime:
    """从日志行里取出 ISO 时间戳（行首到第一个空格）。"""
    return datetime.fromisoformat(line.split(" ", 1)[0])


@pytest.mark.parametrize("name", ["Asia/Shanghai", "UTC"])
def test_timestamp_uses_the_configured_timezone(name: str) -> None:
    """日志时间戳必须落在配置的时区里 —— 否则 UTC+8 的机器上看起来「晚 8 小时」。"""
    stream = io.StringIO()
    configure_logging(level="INFO", timezone=name, stream=stream)
    try:
        get_logger("doudou.probe").info("对表")
        stamp = _timestamp_of(stream.getvalue().splitlines()[0])
    finally:
        reset_logging()

    assert stamp.utcoffset() == datetime.now(ZoneInfo(name)).utcoffset()
    # 与实际墙钟相差不超过几秒（允许跨秒边界）
    assert abs((stamp - datetime.now(ZoneInfo(name))).total_seconds()) < 5


def test_unknown_timezone_falls_back_instead_of_breaking() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", timezone="Mars/Olympus", stream=stream)
    try:
        get_logger("doudou.probe").info("还是得能打日志")
    finally:
        reset_logging()

    output = stream.getvalue()
    assert "还是得能打日志" in output
    assert _timestamp_of(output.splitlines()[-1]).tzinfo is not None


# --------------------------------------------------------------------------- 入参脱敏


@tool
def login(api_key: str, user: str = "me") -> str:
    """假装用 api_key 登录，用于验证日志脱敏。"""
    return f"{user} 已登录"


async def test_tool_args_are_redacted_and_result_is_logged(captured: io.StringIO) -> None:
    """工具入参里的密钥不能落进日志，但工具名与结果要留痕。"""
    agent = ReactAgent(
        model=ScriptedChatModel(
            steps=[
                tool_call("login", {"api_key": "sk-super-secret", "user": "alice"}, call_id="c1"),
                AIMessage(content="好了"),
            ]
        ),
        tools=[login],
        system_prompt="",
    )

    await agent.ainvoke([HumanMessage(content="登录一下")])

    output = captured.getvalue()
    assert "sk-super-secret" not in output  # 密钥必须被模糊化
    assert "agent.tool_started" in output
    assert "agent.tool_finished" in output
    assert "login" in output
    assert "alice" in output  # 非敏感参数保留，便于排查
    assert "alice 已登录" in output


async def test_long_tool_result_is_truncated_in_logs(captured: io.StringIO) -> None:
    """超长结果要截断，否则一次日志就能刷屏。"""

    @tool
    def big() -> str:
        """返回一大段内容。"""
        return "x" * 5000

    agent = ReactAgent(
        model=ScriptedChatModel(
            steps=[tool_call("big", {}, call_id="c1"), AIMessage(content="好了")]
        ),
        tools=[big],
        system_prompt="",
    )

    await agent.ainvoke([HumanMessage(content="给我一大段")])

    output = captured.getvalue()
    assert "已截断" in output
    assert "x" * 5000 not in output
